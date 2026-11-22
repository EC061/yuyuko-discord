"""Batched WG poller, atomic diff engine, and scoped squadmate recheck logic."""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime

from loguru import logger

from wows_bot.clients.pr_source import PRSourceClient
from wows_bot.clients.wg_api import WGApiClient
from wows_bot.config import SUPPORTED_REALMS, Realm, Settings
from wows_bot.db.repository import Repository
from wows_bot.services.stats_builder import compact_ship_snapshot, compute_ship_deltas


class ShipsStatsLagError(RuntimeError):
    """Raised when WG /account/info last_battle_time advances before /ships/stats updates."""


class PollerService:
    def __init__(
        self,
        settings: Settings,
        repo: Repository,
        wg_client: WGApiClient,
        pr_client: PRSourceClient,
    ) -> None:
        self.settings = settings
        self.repo = repo
        self.wg = wg_client
        self.pr_client = pr_client
        self._in_flight_diffs: set[tuple[str, int]] = set()

    async def sync_reference_data(self) -> None:
        """Refresh PR expected values and WG ship encyclopedia (offloaded to thread)."""
        pr_values = await self.pr_client.fetch_expected_values()
        if pr_values:
            await asyncio.to_thread(self.repo.upsert_pr_expected, pr_values)

        if self.settings.wg_application_id:
            try:
                ships = await self.wg.fetch_encyclopedia_ships(self.settings.default_realm)
                if ships:
                    await asyncio.to_thread(self.repo.upsert_ships, ships)
            except Exception as exc:
                logger.warning("Failed to sync WG encyclopedia: {}", exc)

    async def poll_all_realms(self) -> int:
        if not self.settings.wg_application_id:
            return 0
        enqueued = 0
        for realm in SUPPORTED_REALMS:
            enqueued += await self.poll_realm(realm)
        return enqueued

    async def poll_realm(
        self,
        realm: Realm,
        *,
        only_account_ids: set[int] | None = None,
        exclude_account_ids: set[int] | None = None,
    ) -> int:
        accounts = self.repo.list_tracked_accounts(realm)
        if only_account_ids is not None:
            accounts = [a for a in accounts if a.account_id in only_account_ids]
        if exclude_account_ids:
            accounts = [a for a in accounts if a.account_id not in exclude_account_ids]
        if not accounts:
            return 0

        aids = [a.account_id for a in accounts]
        by_id = {a.account_id: a for a in accounts}
        try:
            batch = await self.wg.get_accounts_info_batch(
                realm,
                aids,
                fields="account_id,nickname,last_battle_time,hidden_profile",
            )
        except Exception as exc:
            logger.warning("Batch account/info poll failed for {}: {}", realm, exc)
            return 0

        poll_q = self.repo.dbm.poll_queue
        enqueued = 0
        for aid, info in batch.items():
            tracked = by_id.get(aid)
            if tracked is None:
                continue
            hidden = bool(info.get("hidden_profile"))
            nickname = str(info.get("nickname") or tracked.nickname)
            last_bt = int(info.get("last_battle_time") or 0)

            if hidden:
                if not tracked.hidden_profile:
                    self.repo.update_tracked_account_poll_state(
                        realm=realm,
                        account_id=aid,
                        nickname=nickname,
                        hidden_profile=True,
                    )
                continue

            needs_seed = not tracked.baseline_seeded
            has_new_battle = last_bt > tracked.last_battle_time

            if needs_seed or has_new_battle:
                key = (realm, aid)
                if key in self._in_flight_diffs:
                    continue
                if poll_q is not None:
                    self._in_flight_diffs.add(key)
                    poll_q.enqueue(
                        {
                            "type": "fetch_player_diff",
                            "realm": realm,
                            "account_id": aid,
                            "nickname": nickname,
                            "last_battle_time": last_bt,
                        }
                    )
                    enqueued += 1
            elif nickname != tracked.nickname or tracked.hidden_profile:
                self.repo.update_tracked_account_poll_state(
                    realm=realm,
                    account_id=aid,
                    nickname=nickname,
                    hidden_profile=False,
                )
        return enqueued

    async def process_player_diff(
        self,
        realm: Realm,
        account_id: int,
        *,
        nickname: str | None = None,
        last_battle_time: int | None = None,
    ) -> list[int]:
        """Fetch per-ship stats, compute diffs, and commit snapshot + battles + outbox atomically."""
        try:
            return await self._process_player_diff_inner(
                realm, account_id, nickname=nickname, last_battle_time=last_battle_time
            )
        finally:
            self._in_flight_diffs.discard((realm, account_id))

    async def _process_player_diff_inner(
        self,
        realm: Realm,
        account_id: int,
        *,
        nickname: str | None,
        last_battle_time: int | None,
    ) -> list[int]:
        tracked = self.repo.get_tracked_account(realm, account_id)
        if tracked is None:
            return []

        if tracked.hidden_profile and last_battle_time is None:
            return []

        if last_battle_time is None or nickname is None:
            batch = await self.wg.get_accounts_info_batch(
                realm,
                [account_id],
                fields="account_id,nickname,last_battle_time,hidden_profile",
            )
            info = batch.get(account_id)
            if not info:
                return []
            if info.get("hidden_profile"):
                self.repo.update_tracked_account_poll_state(
                    realm=realm, account_id=account_id, hidden_profile=True
                )
                return []
            nickname = str(info.get("nickname") or tracked.nickname)
            last_battle_time = int(info.get("last_battle_time") or 0)

        clan = await self.wg.get_account_clan(realm, account_id)
        clan_id = int(clan["clan_id"]) if clan and clan.get("clan_id") else tracked.clan_id
        clan_tag = str(clan["tag"]) if clan and clan.get("tag") else tracked.clan_tag

        raw_ships = await self.wg.get_account_ships_stats(realm, account_id)
        new_snapshots = {
            int(entry["ship_id"]): compact_ship_snapshot(entry)
            for entry in raw_ships
            if entry.get("ship_id")
        }

        today_str = datetime.now(tz=UTC).strftime("%Y-%m-%d")
        old_snapshots = self.repo.get_ship_snapshots(realm, account_id)

        # First-time seed: record baseline without spamming historical matches
        if not tracked.baseline_seeded or not old_snapshots:
            await asyncio.to_thread(
                self.repo.save_ship_snapshots,
                realm,
                account_id,
                new_snapshots,
                last_battle_time=last_battle_time,
                old_snapshots=None,
                nickname=nickname,
                clan_id=clan_id,
                clan_tag=clan_tag,
                mark_seeded=True,
                daily_snapshot_date=today_str,
            )
            logger.info("Seeded baseline snapshot for {}:{} ({})", realm, account_id, nickname)
            return []

        ships_meta = self.repo.get_ships_map(list(new_snapshots.keys()))
        expected_map = self.repo.get_pr_expected_map()

        deltas = compute_ship_deltas(
            realm=realm,
            account_id=account_id,
            nickname=nickname,
            clan_tag=clan_tag,
            battle_time=last_battle_time,
            old_snapshots=old_snapshots,
            new_snapshots=new_snapshots,
            ships_meta=ships_meta,
            expected_map=expected_map,
        )

        # WG /account/info last_battle_time can advance 5-30s before /ships/stats reflects it.
        # Do NOT advance watermark while fresh (<90s); retry shortly instead of losing the battle!
        if not deltas and last_battle_time > tracked.last_battle_time:
            age_s = time.time() - last_battle_time
            if 0 <= age_s < 90:
                raise ShipsStatsLagError(
                    f"WG ships/stats lagging behind last_battle_time for {realm}:{account_id}"
                )

        outcomes = await asyncio.to_thread(
            self.repo.commit_player_poll_and_coalesce,
            realm=realm,
            account_id=account_id,
            new_snapshots=new_snapshots,
            old_snapshots=old_snapshots,
            deltas=deltas,
            last_battle_time=last_battle_time,
            nickname=nickname,
            clan_id=clan_id,
            clan_tag=clan_tag,
            daily_snapshot_date=today_str,
            time_tolerance_s=self.settings.squad_time_tolerance_seconds,
            recheck_delay_s=self.settings.squad_recheck_delay_seconds,
            finalize_delay_s=self.settings.squad_finalize_delay_seconds,
            enqueue_jobs=True,
        )

        group_ids: list[int] = []
        for delta, outcome in zip(deltas, outcomes, strict=True):
            group_ids.append(outcome.match_group_id)
            logger.info(
                "Recorded battle {}:{} ({}) {} dmg={} pr={:.0f} -> group #{} (members={}/{}, finalized={})",
                realm,
                account_id,
                nickname,
                delta.battle_type,
                delta.damage_delta,
                delta.pr_value,
                outcome.match_group_id,
                outcome.member_count,
                outcome.expected_div_size,
                outcome.finalized_immediately,
            )
        return group_ids
