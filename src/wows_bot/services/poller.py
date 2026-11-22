"""Batched WG poller, diff engine, and fast squadmate recheck logic."""

from __future__ import annotations

from datetime import UTC, datetime

from loguru import logger

from wows_bot.clients.pr_source import PRSourceClient
from wows_bot.clients.wg_api import WGApiClient
from wows_bot.config import SUPPORTED_REALMS, Realm, Settings
from wows_bot.db.repository import Repository
from wows_bot.services.stats_builder import compact_ship_snapshot, compute_ship_deltas


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

    async def sync_reference_data(self) -> None:
        """Refresh PR expected values and WG ship encyclopedia (zh-cn + en)."""
        pr_values = await self.pr_client.fetch_expected_values()
        if pr_values:
            self.repo.upsert_pr_expected(pr_values)

        if self.settings.wg_application_id:
            try:
                ships = await self.wg.fetch_encyclopedia_ships(self.settings.default_realm)
                if ships:
                    self.repo.upsert_ships(ships)
            except Exception as exc:
                logger.warning("Failed to sync WG encyclopedia: {}", exc)

    async def poll_all_realms(self) -> int:
        """Run batched last_battle_time pre-checks across NA/EU/ASIA.

        Returns number of accounts enqueued for detailed diffing.
        """
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
        exclude_account_ids: set[int] | None = None,
    ) -> int:
        accounts = self.repo.list_tracked_accounts(realm)
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
                if poll_q is not None:
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
            else:
                # Touch nickname/hidden state if changed
                if nickname != tracked.nickname or tracked.hidden_profile:
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
        """Fetch per-ship stats for one account, diff against snapshot, and coalesce battles.

        Returns list of affected match_group_ids.
        """
        tracked = self.repo.get_tracked_account(realm, account_id)
        if tracked is None:
            return []

        if last_battle_time is None or nickname is None:
            batch = await self.wg.get_accounts_info_batch(
                realm,
                [account_id],
                fields="account_id,nickname,last_battle_time,hidden_profile",
            )
            info = batch.get(account_id)
            if not info or info.get("hidden_profile"):
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
            self.repo.save_ship_snapshots(
                realm,
                account_id,
                new_snapshots,
                last_battle_time=last_battle_time,
                nickname=nickname,
                clan_id=clan_id,
                clan_tag=clan_tag,
                mark_seeded=True,
            )
            self.repo.upsert_daily_snapshot(
                realm, account_id, today_str, {"last_battle_time": last_battle_time}, new_snapshots
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

        # Save new snapshot & daily baseline
        self.repo.save_ship_snapshots(
            realm,
            account_id,
            new_snapshots,
            last_battle_time=last_battle_time,
            nickname=nickname,
            clan_id=clan_id,
            clan_tag=clan_tag,
            mark_seeded=True,
        )
        self.repo.upsert_daily_snapshot(
            realm, account_id, today_str, {"last_battle_time": last_battle_time}, new_snapshots
        )

        group_ids: list[int] = []
        for delta in deltas:
            outcome = self.repo.record_battle_and_coalesce(
                delta,
                time_tolerance_s=self.settings.squad_time_tolerance_seconds,
                recheck_delay_s=self.settings.squad_recheck_delay_seconds,
                finalize_delay_s=self.settings.squad_finalize_delay_seconds,
                enqueue_jobs=True,
            )
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
