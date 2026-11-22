"""SQLite WAL repository + atomic Honker outbox operations."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

from wows_bot.config import Language, Realm
from wows_bot.db.schema import DatabaseManager
from wows_bot.services.pr import ExpectedShipValues


@dataclass(frozen=True, slots=True)
class UserBinding:
    id: int
    gamevox_user_id: str
    realm: Realm
    account_id: int
    nickname: str
    is_default: bool
    lang_override: str | None


@dataclass(frozen=True, slots=True)
class TrackedAccount:
    realm: Realm
    account_id: int
    nickname: str
    clan_id: int | None
    clan_tag: str | None
    last_battle_time: int
    last_polled_at: int
    baseline_seeded: bool
    hidden_profile: bool


@dataclass(frozen=True, slots=True)
class ShipMeta:
    ship_id: int
    name_en: str
    name_zh: str
    tier: int
    ship_type: str
    nation: str
    is_premium: bool
    is_special: bool
    icon_small: str
    icon_medium: str
    icon_large: str

    def display_name(self, lang: str = "zh") -> str:
        return self.name_zh if lang == "zh" and self.name_zh else self.name_en


@dataclass(frozen=True, slots=True)
class BattleRecordInput:
    realm: Realm
    account_id: int
    nickname: str
    clan_tag: str | None
    ship_id: int
    ship_tier: int
    battle_type: str
    battle_time: int
    battles_delta: int
    wins_delta: int
    survived_delta: int
    damage_delta: int
    frags_delta: int
    xp_delta: int
    spotting_delta: int
    potential_delta: int
    planes_delta: int
    main_hits_delta: int
    main_shots_delta: int
    pr_value: float


@dataclass(frozen=True, slots=True)
class CoalesceOutcome:
    match_group_id: int
    created_new_group: bool
    finalized_immediately: bool
    member_count: int
    expected_div_size: int


def _row_to_ship_meta(r: dict[str, Any]) -> ShipMeta:
    return ShipMeta(
        ship_id=int(r["ship_id"]),
        name_en=str(r["name_en"]),
        name_zh=str(r["name_zh"]),
        tier=int(r["tier"]),
        ship_type=str(r["ship_type"]),
        nation=str(r["nation"]),
        is_premium=bool(r["is_premium"]),
        is_special=bool(r["is_special"]),
        icon_small=str(r["icon_small"]),
        icon_medium=str(r["icon_medium"]),
        icon_large=str(r["icon_large"]),
    )


def _row_to_tracked(r: dict[str, Any]) -> TrackedAccount:
    return TrackedAccount(
        realm=r["realm"],
        account_id=int(r["account_id"]),
        nickname=str(r["nickname"]),
        clan_id=int(r["clan_id"]) if r["clan_id"] is not None else None,
        clan_tag=r["clan_tag"],
        last_battle_time=int(r["last_battle_time"]),
        last_polled_at=int(r["last_polled_at"]),
        baseline_seeded=bool(r["baseline_seeded"]),
        hidden_profile=bool(r["hidden_profile"]),
    )


def _row_to_binding(r: dict[str, Any]) -> UserBinding:
    return UserBinding(
        id=int(r["id"]),
        gamevox_user_id=str(r["gamevox_user_id"]),
        realm=r["realm"],
        account_id=int(r["account_id"]),
        nickname=str(r["nickname"]),
        is_default=bool(r["is_default"]),
        lang_override=r["lang_override"],
    )


class Repository:
    """High-level data access over SQLite WAL + Honker transactional outbox."""

    def __init__(self, db_manager: DatabaseManager) -> None:
        self.dbm = db_manager

    @property
    def db(self):
        return self.dbm.db

    # ------------------------------------------------------------------
    # Guild settings & language resolution
    # ------------------------------------------------------------------
    def get_guild_settings(self, guild_id: str) -> dict[str, Any]:
        rows = self.db.query(
            """
            SELECT guild_id, default_lang, auto_post_channel_id, auto_group_divisions, command_prefix
            FROM guild_settings
            WHERE guild_id = ?
            """,
            [guild_id],
        )
        if not rows:
            return {
                "guild_id": guild_id,
                "default_lang": "zh",
                "auto_post_channel_id": None,
                "auto_group_divisions": True,
                "command_prefix": "wws",
            }
        r = rows[0]
        return {
            "guild_id": str(r["guild_id"]),
            "default_lang": str(r["default_lang"]),
            "auto_post_channel_id": r["auto_post_channel_id"],
            "auto_group_divisions": bool(r["auto_group_divisions"]),
            "command_prefix": str(r["command_prefix"]),
        }

    def upsert_guild_settings(
        self,
        guild_id: str,
        *,
        default_lang: Language | None = None,
        auto_post_channel_id: str | None = None,
        auto_group_divisions: bool | None = None,
    ) -> None:
        cur = self.get_guild_settings(guild_id)
        now = int(time.time())
        with self.db.transaction() as tx:
            tx.execute(
                """
                INSERT INTO guild_settings (
                    guild_id, default_lang, auto_post_channel_id, auto_group_divisions, command_prefix, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(guild_id) DO UPDATE SET
                    default_lang = excluded.default_lang,
                    auto_post_channel_id = excluded.auto_post_channel_id,
                    auto_group_divisions = excluded.auto_group_divisions,
                    updated_at = excluded.updated_at
                """,
                [
                    guild_id,
                    default_lang if default_lang is not None else cur["default_lang"],
                    auto_post_channel_id if auto_post_channel_id is not None else cur["auto_post_channel_id"],
                    int(auto_group_divisions if auto_group_divisions is not None else cur["auto_group_divisions"]),
                    cur["command_prefix"],
                    now,
                ],
            )

    def resolve_language(
        self,
        *,
        user_id: str | None = None,
        guild_id: str | None = None,
        fallback: Language = "zh",
    ) -> Language:
        if user_id:
            rows = self.db.query(
                """
                SELECT lang_override FROM user_bindings
                WHERE gamevox_user_id = ? AND lang_override IS NOT NULL
                ORDER BY is_default DESC, updated_at DESC LIMIT 1
                """,
                [user_id],
            )
            if rows and rows[0]["lang_override"] in ("zh", "en"):
                return rows[0]["lang_override"]
        if guild_id:
            gs = self.get_guild_settings(guild_id)
            if gs["default_lang"] in ("zh", "en"):
                return gs["default_lang"]
        return fallback

    def set_user_language(self, user_id: str, lang: Language) -> bool:
        now = int(time.time())
        with self.db.transaction() as tx:
            rows = tx.query(
                "SELECT id FROM user_bindings WHERE gamevox_user_id = ?",
                [user_id],
            )
            if not rows:
                return False
            tx.execute(
                "UPDATE user_bindings SET lang_override = ?, updated_at = ? WHERE gamevox_user_id = ?",
                [lang, now, user_id],
            )
        return True

    # ------------------------------------------------------------------
    # Bindings & Tracking Subscriptions
    # ------------------------------------------------------------------
    def bind_account(
        self,
        *,
        gamevox_user_id: str,
        realm: Realm,
        account_id: int,
        nickname: str,
        guild_id: str | None = None,
        auto_post_channel_id: str | None = None,
    ) -> None:
        now = int(time.time())
        with self.db.transaction() as tx:
            tx.execute(
                "UPDATE user_bindings SET is_default = 0, updated_at = ? WHERE gamevox_user_id = ?",
                [now, gamevox_user_id],
            )
            tx.execute(
                """
                INSERT INTO user_bindings (
                    gamevox_user_id, realm, account_id, nickname, is_default, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 1, ?, ?)
                ON CONFLICT(gamevox_user_id, realm, account_id) DO UPDATE SET
                    nickname = excluded.nickname,
                    is_default = 1,
                    updated_at = excluded.updated_at
                """,
                [gamevox_user_id, realm, account_id, nickname, now, now],
            )
            tx.execute(
                """
                INSERT INTO tracked_accounts (realm, account_id, nickname)
                VALUES (?, ?, ?)
                ON CONFLICT(realm, account_id) DO UPDATE SET
                    nickname = excluded.nickname
                """,
                [realm, account_id, nickname],
            )
            if guild_id and auto_post_channel_id:
                tx.execute(
                    """
                    INSERT INTO tracked_subscriptions (
                        realm, account_id, guild_id, channel_id, subscribed_by_user_id, enabled, created_at
                    ) VALUES (?, ?, ?, ?, ?, 1, ?)
                    ON CONFLICT(realm, account_id, channel_id) DO UPDATE SET
                        enabled = 1
                    """,
                    [realm, account_id, guild_id, auto_post_channel_id, gamevox_user_id, now],
                )

    def get_default_binding(self, gamevox_user_id: str) -> UserBinding | None:
        rows = self.db.query(
            """
            SELECT id, gamevox_user_id, realm, account_id, nickname, is_default, lang_override
            FROM user_bindings
            WHERE gamevox_user_id = ?
            ORDER BY is_default DESC, updated_at DESC
            LIMIT 1
            """,
            [gamevox_user_id],
        )
        return _row_to_binding(rows[0]) if rows else None

    def list_bindings(self, gamevox_user_id: str) -> list[UserBinding]:
        rows = self.db.query(
            """
            SELECT id, gamevox_user_id, realm, account_id, nickname, is_default, lang_override
            FROM user_bindings
            WHERE gamevox_user_id = ?
            ORDER BY is_default DESC, id ASC
            """,
            [gamevox_user_id],
        )
        return [_row_to_binding(r) for r in rows]

    def set_subscription(
        self,
        *,
        realm: Realm,
        account_id: int,
        nickname: str,
        guild_id: str,
        channel_id: str,
        user_id: str,
        enabled: bool,
    ) -> None:
        now = int(time.time())
        with self.db.transaction() as tx:
            tx.execute(
                """
                INSERT INTO tracked_accounts (realm, account_id, nickname)
                VALUES (?, ?, ?)
                ON CONFLICT(realm, account_id) DO UPDATE SET nickname = excluded.nickname
                """,
                [realm, account_id, nickname],
            )
            tx.execute(
                """
                INSERT INTO tracked_subscriptions (
                    realm, account_id, guild_id, channel_id, subscribed_by_user_id, enabled, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(realm, account_id, channel_id) DO UPDATE SET
                    enabled = excluded.enabled
                """,
                [realm, account_id, guild_id, channel_id, user_id, int(enabled), now],
            )

    def list_tracked_accounts(self, realm: Realm) -> list[TrackedAccount]:
        rows = self.db.query(
            """
            SELECT realm, account_id, nickname, clan_id, clan_tag,
                   last_battle_time, last_polled_at, baseline_seeded, hidden_profile
            FROM tracked_accounts
            WHERE realm = ?
            ORDER BY account_id ASC
            """,
            [realm],
        )
        return [_row_to_tracked(r) for r in rows]

    def get_tracked_account(self, realm: Realm, account_id: int) -> TrackedAccount | None:
        rows = self.db.query(
            """
            SELECT realm, account_id, nickname, clan_id, clan_tag,
                   last_battle_time, last_polled_at, baseline_seeded, hidden_profile
            FROM tracked_accounts
            WHERE realm = ? AND account_id = ?
            """,
            [realm, account_id],
        )
        return _row_to_tracked(rows[0]) if rows else None

    def update_tracked_account_poll_state(
        self,
        *,
        realm: Realm,
        account_id: int,
        nickname: str | None = None,
        clan_id: int | None = None,
        clan_tag: str | None = None,
        last_battle_time: int | None = None,
        baseline_seeded: bool | None = None,
        hidden_profile: bool | None = None,
    ) -> None:
        now = int(time.time())
        cur = self.get_tracked_account(realm, account_id)
        if cur is None:
            return
        with self.db.transaction() as tx:
            tx.execute(
                """
                UPDATE tracked_accounts
                SET nickname = ?,
                    clan_id = ?,
                    clan_tag = ?,
                    last_battle_time = ?,
                    last_polled_at = ?,
                    baseline_seeded = ?,
                    hidden_profile = ?
                WHERE realm = ? AND account_id = ?
                """,
                [
                    nickname if nickname is not None else cur.nickname,
                    clan_id if clan_id is not None else cur.clan_id,
                    clan_tag if clan_tag is not None else cur.clan_tag,
                    last_battle_time if last_battle_time is not None else cur.last_battle_time,
                    now,
                    int(baseline_seeded if baseline_seeded is not None else cur.baseline_seeded),
                    int(hidden_profile if hidden_profile is not None else cur.hidden_profile),
                    realm,
                    account_id,
                ],
            )

    # ------------------------------------------------------------------
    # Ship snapshots & daily snapshots
    # ------------------------------------------------------------------
    def get_ship_snapshots(self, realm: Realm, account_id: int) -> dict[int, dict[str, Any]]:
        rows = self.db.query(
            "SELECT ship_id, stats_json FROM ship_snapshots WHERE realm = ? AND account_id = ?",
            [realm, account_id],
        )
        return {int(r["ship_id"]): json.loads(r["stats_json"]) for r in rows}

    def save_ship_snapshots(
        self,
        realm: Realm,
        account_id: int,
        snapshots: dict[int, dict[str, Any]],
        *,
        last_battle_time: int,
        nickname: str | None = None,
        clan_id: int | None = None,
        clan_tag: str | None = None,
        mark_seeded: bool = True,
    ) -> None:
        now = int(time.time())
        with self.db.transaction() as tx:
            for ship_id, stats in snapshots.items():
                tx.execute(
                    """
                    INSERT INTO ship_snapshots (realm, account_id, ship_id, stats_json, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(realm, account_id, ship_id) DO UPDATE SET
                        stats_json = excluded.stats_json,
                        updated_at = excluded.updated_at
                    """,
                    [realm, account_id, ship_id, json.dumps(stats, separators=(",", ":")), now],
                )
            tx.execute(
                """
                UPDATE tracked_accounts
                SET nickname = COALESCE(?, nickname),
                    clan_id = COALESCE(?, clan_id),
                    clan_tag = COALESCE(?, clan_tag),
                    last_battle_time = MAX(last_battle_time, ?),
                    last_polled_at = ?,
                    baseline_seeded = CASE WHEN ? = 1 THEN 1 ELSE baseline_seeded END
                WHERE realm = ? AND account_id = ?
                """,
                [
                    nickname,
                    clan_id,
                    clan_tag,
                    last_battle_time,
                    now,
                    int(mark_seeded),
                    realm,
                    account_id,
                ],
            )

    def upsert_daily_snapshot(
        self,
        realm: Realm,
        account_id: int,
        snapshot_date: str,
        summary: dict[str, Any],
        ships: dict[int, dict[str, Any]],
    ) -> None:
        now = int(time.time())
        with self.db.transaction() as tx:
            tx.execute(
                """
                INSERT INTO daily_snapshots (realm, account_id, snapshot_date, summary_json, ships_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(realm, account_id, snapshot_date) DO NOTHING
                """,
                [
                    realm,
                    account_id,
                    snapshot_date,
                    json.dumps(summary, separators=(",", ":")),
                    json.dumps(ships, separators=(",", ":")),
                    now,
                ],
            )

    def get_daily_snapshot_on_or_before(
        self, realm: Realm, account_id: int, target_date: str
    ) -> dict[str, Any] | None:
        rows = self.db.query(
            """
            SELECT snapshot_date, summary_json, ships_json
            FROM daily_snapshots
            WHERE realm = ? AND account_id = ? AND snapshot_date <= ?
            ORDER BY snapshot_date DESC LIMIT 1
            """,
            [realm, account_id, target_date],
        )
        if not rows:
            rows = self.db.query(
                """
                SELECT snapshot_date, summary_json, ships_json
                FROM daily_snapshots
                WHERE realm = ? AND account_id = ?
                ORDER BY snapshot_date ASC LIMIT 1
                """,
                [realm, account_id],
            )
        if not rows:
            return None
        r = rows[0]
        return {
            "snapshot_date": str(r["snapshot_date"]),
            "summary": json.loads(r["summary_json"]),
            "ships": {int(k): v for k, v in json.loads(r["ships_json"]).items()},
        }

    # ------------------------------------------------------------------
    # Atomic Battle Recording + Division/Squad Coalescing + Honker Outbox
    # ------------------------------------------------------------------
    def record_battle_and_coalesce(
        self,
        record: BattleRecordInput,
        *,
        time_tolerance_s: int = 20,
        recheck_delay_s: int = 12,
        finalize_delay_s: int = 28,
        enqueue_jobs: bool = True,
    ) -> CoalesceOutcome:
        """Atomically persist a battle record, coalesce divisions into 1 match_group,
        and enqueue Honker jobs inside the exact same SQLite WAL transaction.
        """
        now = int(time.time())
        is_win = 1 if (record.battles_delta > 0 and record.wins_delta >= record.battles_delta) else 0
        expected_div = 2 if record.battle_type == "pvp_div2" else (3 if record.battle_type == "pvp_div3" else 1)
        is_division = expected_div > 1 and record.battles_delta == 1

        match_q = self.dbm.match_queue
        render_q = self.dbm.render_queue

        with self.db.transaction() as tx:
            player_channels = {
                str(r["channel_id"])
                for r in tx.query(
                    """
                    SELECT channel_id FROM tracked_subscriptions
                    WHERE realm = ? AND account_id = ? AND enabled = 1
                    """,
                    [record.realm, record.account_id],
                )
            }

            chosen_group_id: int | None = None
            current_members = 0

            if is_division:
                candidates = tx.query(
                    """
                    SELECT id, battle_time, min_tier, max_tier
                    FROM match_groups
                    WHERE status = 'collecting'
                      AND realm = ?
                      AND battle_type = ?
                      AND is_win = ?
                      AND ABS(battle_time - ?) <= ?
                    ORDER BY ABS(battle_time - ?) ASC, id ASC
                    """,
                    [
                        record.realm,
                        record.battle_type,
                        is_win,
                        record.battle_time,
                        time_tolerance_s,
                        record.battle_time,
                    ],
                )

                best_score = -1
                for cand in candidates:
                    cand_id = int(cand["id"])
                    min_t = int(cand["min_tier"])
                    max_t = int(cand["max_tier"])

                    if record.ship_tier > 0 and min_t > 0:
                        new_min = min(min_t, record.ship_tier)
                        new_max = max(max_t, record.ship_tier)
                        if new_max - new_min > 1:
                            continue

                    members = tx.query(
                        "SELECT account_id FROM battle_records WHERE match_group_id = ?",
                        [cand_id],
                    )
                    member_ids = {int(m["account_id"]) for m in members}
                    if record.account_id in member_ids:
                        continue
                    if len(member_ids) >= expected_div:
                        continue

                    score = 0
                    if player_channels and member_ids:
                        placeholders = ",".join("?" for _ in member_ids)
                        shared = tx.query(
                            f"""
                            SELECT DISTINCT channel_id FROM tracked_subscriptions
                            WHERE realm = ? AND enabled = 1 AND account_id IN ({placeholders})
                            """,
                            [record.realm, *member_ids],
                        )
                        if any(str(r["channel_id"]) in player_channels for r in shared):
                            score += 100

                    if score > best_score:
                        best_score = score
                        chosen_group_id = cand_id
                        current_members = len(member_ids)

            created_new = False
            finalized_now = False

            if chosen_group_id is None:
                created_new = True
                initial_status = "collecting" if is_division else "finalized"
                finalized_at = None if is_division else now
                tx.execute(
                    """
                    INSERT INTO match_groups (
                        realm, battle_type, battle_time, is_win, expected_div_size,
                        min_tier, max_tier, status, created_at, finalized_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        record.realm,
                        record.battle_type,
                        record.battle_time,
                        is_win,
                        expected_div,
                        record.ship_tier,
                        record.ship_tier,
                        initial_status,
                        now,
                        finalized_at,
                    ],
                )
                chosen_group_id = int(tx.query("SELECT last_insert_rowid() AS id")[0]["id"])
                new_member_count = 1
                finalized_now = not is_division
            else:
                new_member_count = current_members + 1
                tx.execute(
                    """
                    UPDATE match_groups
                    SET min_tier = CASE WHEN min_tier = 0 THEN ? ELSE MIN(min_tier, ?) END,
                        max_tier = MAX(max_tier, ?)
                    WHERE id = ?
                    """,
                    [record.ship_tier, record.ship_tier, record.ship_tier, chosen_group_id],
                )
                if new_member_count >= expected_div:
                    tx.execute(
                        """
                        UPDATE match_groups
                        SET status = 'finalized', finalized_at = ?
                        WHERE id = ? AND status = 'collecting'
                        """,
                        [now, chosen_group_id],
                    )
                    finalized_now = True

            tx.execute(
                """
                INSERT INTO battle_records (
                    match_group_id, realm, account_id, nickname, clan_tag, ship_id, ship_tier,
                    battle_type, battle_time, battles_delta, wins_delta, survived_delta,
                    damage_delta, frags_delta, xp_delta, spotting_delta, potential_delta,
                    planes_delta, main_hits_delta, main_shots_delta, pr_value, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    chosen_group_id,
                    record.realm,
                    record.account_id,
                    record.nickname,
                    record.clan_tag,
                    record.ship_id,
                    record.ship_tier,
                    record.battle_type,
                    record.battle_time,
                    record.battles_delta,
                    record.wins_delta,
                    record.survived_delta,
                    record.damage_delta,
                    record.frags_delta,
                    record.xp_delta,
                    record.spotting_delta,
                    record.potential_delta,
                    record.planes_delta,
                    record.main_hits_delta,
                    record.main_shots_delta,
                    record.pr_value,
                    now,
                ],
            )

            if enqueue_jobs:
                if finalized_now and render_q is not None:
                    render_q.enqueue(
                        {"type": "render_and_post", "match_group_id": chosen_group_id},
                        tx=tx,
                    )
                elif created_new and is_division and match_q is not None:
                    match_q.enqueue(
                        {
                            "type": "fast_squad_recheck",
                            "realm": record.realm,
                            "match_group_id": chosen_group_id,
                            "exclude_account_id": record.account_id,
                        },
                        tx=tx,
                        delay=float(recheck_delay_s),
                    )
                    match_q.enqueue(
                        {"type": "finalize_match_group", "match_group_id": chosen_group_id},
                        tx=tx,
                        delay=float(finalize_delay_s),
                    )

        return CoalesceOutcome(
            match_group_id=chosen_group_id,
            created_new_group=created_new,
            finalized_immediately=finalized_now,
            member_count=new_member_count,
            expected_div_size=expected_div,
        )

    def finalize_match_group(self, match_group_id: int, *, enqueue_render: bool = True) -> bool:
        """Atomically transition collecting -> finalized. Returns False if already finalized."""
        now = int(time.time())
        render_q = self.dbm.render_queue
        with self.db.transaction() as tx:
            rows = tx.query(
                "SELECT status FROM match_groups WHERE id = ?",
                [match_group_id],
            )
            if not rows or rows[0]["status"] != "collecting":
                return False
            tx.execute(
                """
                UPDATE match_groups
                SET status = 'finalized', finalized_at = ?
                WHERE id = ? AND status = 'collecting'
                """,
                [now, match_group_id],
            )
            if enqueue_render and render_q is not None:
                render_q.enqueue(
                    {"type": "render_and_post", "match_group_id": match_group_id},
                    tx=tx,
                )
        return True

    def get_match_group_with_records(self, match_group_id: int) -> dict[str, Any] | None:
        g_rows = self.db.query(
            """
            SELECT id, realm, battle_type, battle_time, is_win, expected_div_size,
                   min_tier, max_tier, status, created_at, finalized_at, image_path
            FROM match_groups
            WHERE id = ?
            """,
            [match_group_id],
        )
        if not g_rows:
            return None
        g = g_rows[0]
        r_rows = self.db.query(
            """
            SELECT id, realm, account_id, nickname, clan_tag, ship_id, ship_tier,
                   battle_type, battle_time, battles_delta, wins_delta, survived_delta,
                   damage_delta, frags_delta, xp_delta, spotting_delta, potential_delta,
                   planes_delta, main_hits_delta, main_shots_delta, pr_value
            FROM battle_records
            WHERE match_group_id = ?
            ORDER BY damage_delta DESC, id ASC
            """,
            [match_group_id],
        )
        records = [
            {
                "id": int(r["id"]),
                "realm": r["realm"],
                "account_id": int(r["account_id"]),
                "nickname": r["nickname"],
                "clan_tag": r["clan_tag"],
                "ship_id": int(r["ship_id"]),
                "ship_tier": int(r["ship_tier"]),
                "battle_type": r["battle_type"],
                "battle_time": int(r["battle_time"]),
                "battles_delta": int(r["battles_delta"]),
                "wins_delta": int(r["wins_delta"]),
                "survived_delta": int(r["survived_delta"]),
                "damage_delta": int(r["damage_delta"]),
                "frags_delta": int(r["frags_delta"]),
                "xp_delta": int(r["xp_delta"]),
                "spotting_delta": int(r["spotting_delta"]),
                "potential_delta": int(r["potential_delta"]),
                "planes_delta": int(r["planes_delta"]),
                "main_hits_delta": int(r["main_hits_delta"]),
                "main_shots_delta": int(r["main_shots_delta"]),
                "pr_value": float(r["pr_value"]),
            }
            for r in r_rows
        ]
        return {
            "id": int(g["id"]),
            "realm": g["realm"],
            "battle_type": g["battle_type"],
            "battle_time": int(g["battle_time"]),
            "is_win": bool(g["is_win"]),
            "expected_div_size": int(g["expected_div_size"]),
            "min_tier": int(g["min_tier"]),
            "max_tier": int(g["max_tier"]),
            "status": g["status"],
            "created_at": int(g["created_at"]),
            "finalized_at": int(g["finalized_at"]) if g["finalized_at"] is not None else None,
            "image_path": g["image_path"],
            "records": records,
        }

    def get_latest_match_group_for_account(self, realm: Realm, account_id: int) -> dict[str, Any] | None:
        rows = self.db.query(
            """
            SELECT match_group_id FROM battle_records
            WHERE realm = ? AND account_id = ?
            ORDER BY battle_time DESC, id DESC LIMIT 1
            """,
            [realm, account_id],
        )
        if not rows:
            return None
        return self.get_match_group_with_records(int(rows[0]["match_group_id"]))

    def get_target_channels_for_match_group(self, match_group_id: int) -> list[tuple[str, str]]:
        """Return deduplicated list of (guild_id, channel_id) subscribed to any player in group."""
        rows = self.db.query(
            """
            SELECT DISTINCT ts.guild_id, ts.channel_id
            FROM battle_records br
            JOIN tracked_subscriptions ts
              ON ts.realm = br.realm AND ts.account_id = br.account_id
            WHERE br.match_group_id = ? AND ts.enabled = 1
            """,
            [match_group_id],
        )
        return [(str(r["guild_id"]), str(r["channel_id"])) for r in rows]

    def claim_delivery_slot(self, match_group_id: int, channel_id: str) -> bool:
        """Idempotently claim a (match_group_id, channel_id) delivery slot."""
        with self.db.transaction() as tx:
            existing = tx.query(
                """
                SELECT status FROM match_deliveries
                WHERE match_group_id = ? AND channel_id = ?
                """,
                [match_group_id, channel_id],
            )
            if existing and existing[0]["status"] in ("sent", "sending"):
                return False
            if existing:
                tx.execute(
                    """
                    UPDATE match_deliveries
                    SET status = 'sending', error = NULL
                    WHERE match_group_id = ? AND channel_id = ?
                    """,
                    [match_group_id, channel_id],
                )
            else:
                tx.execute(
                    """
                    INSERT INTO match_deliveries (match_group_id, channel_id, status)
                    VALUES (?, ?, 'sending')
                    """,
                    [match_group_id, channel_id],
                )
        return True

    def mark_delivery_result(
        self,
        match_group_id: int,
        channel_id: str,
        *,
        success: bool,
        message_id: str | None = None,
        error: str | None = None,
    ) -> None:
        now = int(time.time())
        with self.db.transaction() as tx:
            tx.execute(
                """
                UPDATE match_deliveries
                SET status = ?, message_id = ?, error = ?, delivered_at = ?
                WHERE match_group_id = ? AND channel_id = ?
                """,
                [
                    "sent" if success else "failed",
                    message_id,
                    error,
                    now if success else None,
                    match_group_id,
                    channel_id,
                ],
            )
            if success:
                tx.execute(
                    "UPDATE match_groups SET status = 'posted' WHERE id = ?",
                    [match_group_id],
                )

    # ------------------------------------------------------------------
    # Encyclopedia & PR Expected Values Cache
    # ------------------------------------------------------------------
    def upsert_ships(self, ships: list[ShipMeta]) -> None:
        now = int(time.time())
        with self.db.transaction() as tx:
            for s in ships:
                tx.execute(
                    """
                    INSERT INTO encyclopedia_ships (
                        ship_id, name_en, name_zh, tier, ship_type, nation,
                        is_premium, is_special, icon_small, icon_medium, icon_large, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(ship_id) DO UPDATE SET
                        name_en = excluded.name_en,
                        name_zh = excluded.name_zh,
                        tier = excluded.tier,
                        ship_type = excluded.ship_type,
                        nation = excluded.nation,
                        is_premium = excluded.is_premium,
                        is_special = excluded.is_special,
                        icon_small = excluded.icon_small,
                        icon_medium = excluded.icon_medium,
                        icon_large = excluded.icon_large,
                        updated_at = excluded.updated_at
                    """,
                    [
                        s.ship_id,
                        s.name_en,
                        s.name_zh,
                        s.tier,
                        s.ship_type,
                        s.nation,
                        int(s.is_premium),
                        int(s.is_special),
                        s.icon_small,
                        s.icon_medium,
                        s.icon_large,
                        now,
                    ],
                )

    def get_ship(self, ship_id: int) -> ShipMeta | None:
        rows = self.db.query(
            """
            SELECT ship_id, name_en, name_zh, tier, ship_type, nation,
                   is_premium, is_special, icon_small, icon_medium, icon_large
            FROM encyclopedia_ships WHERE ship_id = ?
            """,
            [ship_id],
        )
        return _row_to_ship_meta(rows[0]) if rows else None

    def get_ships_map(self, ship_ids: list[int] | None = None) -> dict[int, ShipMeta]:
        if ship_ids is None:
            rows = self.db.query(
                """
                SELECT ship_id, name_en, name_zh, tier, ship_type, nation,
                       is_premium, is_special, icon_small, icon_medium, icon_large
                FROM encyclopedia_ships
                """
            )
        elif not ship_ids:
            return {}
        else:
            placeholders = ",".join("?" for _ in ship_ids)
            rows = self.db.query(
                f"""
                SELECT ship_id, name_en, name_zh, tier, ship_type, nation,
                       is_premium, is_special, icon_small, icon_medium, icon_large
                FROM encyclopedia_ships WHERE ship_id IN ({placeholders})
                """,
                ship_ids,
            )
        return {int(r["ship_id"]): _row_to_ship_meta(r) for r in rows}

    def search_ships(self, query: str, limit: int = 10) -> list[ShipMeta]:
        q = query.strip()
        if not q:
            return []
        like = f"%{q}%"
        rows = self.db.query(
            """
            SELECT ship_id, name_en, name_zh, tier, ship_type, nation,
                   is_premium, is_special, icon_small, icon_medium, icon_large
            FROM encyclopedia_ships
            WHERE lower(name_en) = lower(?) OR lower(name_zh) = lower(?)
               OR lower(name_en) LIKE lower(?) OR lower(name_zh) LIKE lower(?)
            ORDER BY
                CASE WHEN lower(name_en) = lower(?) OR lower(name_zh) = lower(?) THEN 0 ELSE 1 END,
                tier DESC,
                name_en ASC
            LIMIT ?
            """,
            [q, q, like, like, q, q, limit],
        )
        return [_row_to_ship_meta(r) for r in rows]

    def upsert_pr_expected(self, values: dict[int, ExpectedShipValues]) -> None:
        now = int(time.time())
        with self.db.transaction() as tx:
            for sid, v in values.items():
                tx.execute(
                    """
                    INSERT INTO pr_expected_values (ship_id, average_damage, average_frags, win_rate, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(ship_id) DO UPDATE SET
                        average_damage = excluded.average_damage,
                        average_frags = excluded.average_frags,
                        win_rate = excluded.win_rate,
                        updated_at = excluded.updated_at
                    """,
                    [sid, v.average_damage, v.average_frags, v.win_rate, now],
                )

    def get_pr_expected_map(self) -> dict[int, ExpectedShipValues]:
        rows = self.db.query(
            "SELECT ship_id, average_damage, average_frags, win_rate FROM pr_expected_values"
        )
        return {
            int(r["ship_id"]): ExpectedShipValues(
                int(r["ship_id"]),
                float(r["average_damage"]),
                float(r["average_frags"]),
                float(r["win_rate"]),
            )
            for r in rows
        }

    def get_local_ship_leaderboard(self, realm: Realm, ship_id: int, limit: int = 15) -> list[dict[str, Any]]:
        """Local tracked-player leaderboard fallback when Yuyuko public rank is unavailable/disabled."""
        rows = self.db.query(
            """
            SELECT ta.account_id, ta.nickname, ta.clan_tag, ss.stats_json
            FROM ship_snapshots ss
            JOIN tracked_accounts ta
              ON ta.realm = ss.realm AND ta.account_id = ss.account_id
            WHERE ss.realm = ? AND ss.ship_id = ?
            """,
            [realm, ship_id],
        )
        entries: list[dict[str, Any]] = []
        for r in rows:
            stats = json.loads(r["stats_json"])
            pvp = stats.get("pvp") or {}
            battles = int(pvp.get("battles") or 0)
            if battles <= 0:
                continue
            wins = int(pvp.get("wins") or 0)
            dmg = int(pvp.get("damage_dealt") or 0)
            frags = int(pvp.get("frags") or 0)
            xp = int(pvp.get("xp") or 0)
            entries.append(
                {
                    "account_id": int(r["account_id"]),
                    "nickname": str(r["nickname"]),
                    "clan_tag": str(r["clan_tag"] or ""),
                    "battles": battles,
                    "win_rate": round(wins / battles * 100.0, 2),
                    "avg_damage": round(dmg / battles, 1),
                    "avg_frags": round(frags / battles, 2),
                    "avg_xp": round(xp / battles, 1),
                    "pr": 0,
                }
            )
        entries.sort(key=lambda e: (e["avg_damage"], e["win_rate"]), reverse=True)
        return entries[:limit]
