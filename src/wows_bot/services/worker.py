"""Supervised background Honker queue workers, heartbeat, and retention pruning."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from loguru import logger

from wows_bot.config import Settings
from wows_bot.db.repository import Repository
from wows_bot.services.poller import PollerService, ShipsStatsLagError
from wows_bot.services.renderer import CardRenderer

DeliverFn = Callable[[str, bytes, str], Awaitable[str | None]]
LivenessFn = Callable[[], bool]


class WorkerRunner:
    """Consumes Honker's wows_poll, wows_match, and wows_render queues with supervision."""

    def __init__(
        self,
        settings: Settings,
        repo: Repository,
        poller: PollerService,
        renderer: CardRenderer,
        deliver_image: DeliverFn,
        is_gateway_healthy: LivenessFn | None = None,
    ) -> None:
        self.settings = settings
        self.repo = repo
        self.poller = poller
        self.renderer = renderer
        self.deliver_image = deliver_image
        self.is_gateway_healthy = is_gateway_healthy or (lambda: True)
        self._stop = asyncio.Event()
        self._tasks: list[asyncio.Task] = []

    def start(self) -> None:
        self._stop.clear()
        self._tasks = [
            asyncio.create_task(self._heartbeat_loop(), name="heartbeat"),
            asyncio.create_task(self._periodic_reference_sync(), name="periodic-ref-sync"),
            asyncio.create_task(self._periodic_realm_poll(), name="periodic-realm-poll"),
            asyncio.create_task(self._supervise("worker-poll", self._consume_poll_queue), name="worker-poll"),
            asyncio.create_task(self._supervise("worker-match", self._consume_match_queue), name="worker-match"),
            asyncio.create_task(self._supervise("worker-render", self._consume_render_queue), name="worker-render"),
        ]
        logger.info("Started {} supervised background Honker worker tasks", len(self._tasks))

    async def stop(self) -> None:
        self._stop.set()
        for t in self._tasks:
            t.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

    async def _supervise(self, name: str, fn: Callable[[], Awaitable[None]]) -> None:
        while not self._stop.is_set():
            try:
                await fn()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("Consumer '{}' crashed ({}); restarting in 5s", name, type(exc).__name__)
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=5.0)
                except TimeoutError:
                    continue

    async def _heartbeat_loop(self) -> None:
        while not self._stop.is_set():
            try:
                workers_alive = all(not t.done() for t in self._tasks)
                if self.is_gateway_healthy() and workers_alive:
                    await asyncio.to_thread(self.repo.touch_heartbeat, "ok")
            except Exception as exc:
                logger.warning("Heartbeat write failed: {}", type(exc).__name__)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=30.0)
            except TimeoutError:
                continue

    async def _periodic_reference_sync(self) -> None:
        while not self._stop.is_set():
            try:
                await self.poller.sync_reference_data()
                await asyncio.to_thread(self.repo.prune_old_data)
            except Exception as exc:
                logger.warning("Reference data sync / prune failed: {}", exc)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=6 * 3600)
            except TimeoutError:
                continue

    async def _periodic_realm_poll(self) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=5)
            return
        except TimeoutError:
            pass

        while not self._stop.is_set():
            try:
                count = await self.poller.poll_all_realms()
                if count:
                    logger.debug("Enqueued {} player diff jobs", count)
            except Exception as exc:
                logger.warning("Periodic realm poll failed: {}", exc)
            try:
                await asyncio.wait_for(
                    self._stop.wait(),
                    timeout=max(15, self.settings.poll_interval_seconds),
                )
            except TimeoutError:
                continue

    async def _consume_poll_queue(self) -> None:
        q = self.repo.dbm.poll_queue
        assert q is not None
        async for job in q.claim("poll-worker", idle_poll_s=1.0):
            if self._stop.is_set():
                break
            payload = job.payload or {}
            try:
                if payload.get("type") == "fetch_player_diff":
                    await self.poller.process_player_diff(
                        payload["realm"],
                        int(payload["account_id"]),
                        nickname=payload.get("nickname"),
                        last_battle_time=payload.get("last_battle_time"),
                    )
                job.ack()
            except ShipsStatsLagError as exc:
                logger.info("poll job #{} delayed due to WG ships/stats lag: {}", job.id, exc)
                job.retry(12, "wg_ships_stats_lag")
            except Exception as exc:
                logger.warning("poll job #{} failed ({}): {}", job.id, type(exc).__name__, exc)
                job.retry(15, type(exc).__name__)

    async def _consume_match_queue(self) -> None:
        q = self.repo.dbm.match_queue
        assert q is not None
        async for job in q.claim("match-worker", idle_poll_s=1.0):
            if self._stop.is_set():
                break
            payload = job.payload or {}
            try:
                jtype = payload.get("type")
                if jtype == "fast_squad_recheck":
                    realm = payload["realm"]
                    src_aid = payload.get("source_account_id") or payload.get("exclude_account_id")
                    if src_aid is not None:
                        related = self.repo.list_related_tracked_account_ids(realm, int(src_aid))
                        if related:
                            await self.poller.poll_realm(realm, only_account_ids=set(related))
                elif jtype == "finalize_match_group":
                    mg_id = int(payload["match_group_id"])
                    finalized = self.repo.finalize_match_group(mg_id, enqueue_render=True)
                    logger.debug("Delayed finalize match_group #{} -> {}", mg_id, finalized)
                job.ack()
            except Exception as exc:
                logger.warning("match job #{} failed ({}): {}", job.id, type(exc).__name__, exc)
                job.retry(10, type(exc).__name__)

    async def _consume_render_queue(self) -> None:
        q = self.repo.dbm.render_queue
        assert q is not None
        async for job in q.claim("render-worker", idle_poll_s=1.0):
            if self._stop.is_set():
                break
            payload = job.payload or {}
            try:
                if payload.get("type") == "render_and_post":
                    await self._handle_render_and_post(int(payload["match_group_id"]))
                job.ack()
            except Exception as exc:
                logger.warning("render job #{} failed ({}): {}", job.id, type(exc).__name__, exc)
                job.retry(20, type(exc).__name__)

    async def _handle_render_and_post(self, match_group_id: int) -> None:
        group = self.repo.get_match_group_with_records(match_group_id)
        if not group or not group["records"]:
            return

        targets = self.repo.get_target_channels_for_match_group(match_group_id)
        if not targets:
            logger.debug("match_group #{} has no subscribed auto-post channels", match_group_id)
            return

        ship_ids = [r["ship_id"] for r in group["records"]]
        ships_meta = self.repo.get_ships_map(ship_ids)

        # Render BEFORE claiming delivery slots so a Chromium failure NEVER strands a slot in 'sending'
        rendered_by_lang: dict[str, bytes] = {}
        langs_needed = {
            self.repo.resolve_language(guild_id=gid, fallback=self.settings.default_language)
            for gid, _ in targets
        }
        for lang in langs_needed:
            rendered_by_lang[lang] = await self.renderer.render_match_group(group, ships_meta, lang=lang)

        filename = f"match_{match_group_id}.{self.settings.image_file_ext}"
        failures: list[Exception] = []

        # Deliver independently per channel so one broken destination never blocks other channels
        for guild_id, channel_id in targets:
            if not self.repo.claim_delivery_slot(match_group_id, channel_id, lease_seconds=180):
                continue
            lang = self.repo.resolve_language(guild_id=guild_id, fallback=self.settings.default_language)
            try:
                msg_id = await self.deliver_image(channel_id, rendered_by_lang[lang], filename)
                self.repo.mark_delivery_result(
                    match_group_id, channel_id, success=True, message_id=msg_id
                )
                logger.info(
                    "Posted match_group #{} ({} players) to channel {} (msg={})",
                    match_group_id,
                    len(group["records"]),
                    channel_id,
                    msg_id,
                )
            except Exception as exc:
                self.repo.mark_delivery_result(
                    match_group_id, channel_id, success=False, error=type(exc).__name__
                )
                failures.append(exc)

        if failures:
            raise failures[0]
