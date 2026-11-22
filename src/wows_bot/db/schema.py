"""SQLite WAL initialization, Honker database wrapper, and versioned schema migrations."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import honker
from loguru import logger

SCHEMA_VERSION = 2

MIGRATION_V1_SQL: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS guild_settings (
        guild_id TEXT PRIMARY KEY,
        default_lang TEXT NOT NULL DEFAULT 'zh',
        auto_post_channel_id TEXT,
        auto_group_divisions INTEGER NOT NULL DEFAULT 1,
        command_prefix TEXT NOT NULL DEFAULT 'wws',
        updated_at INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS user_bindings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        gamevox_user_id TEXT NOT NULL,
        realm TEXT NOT NULL,
        account_id INTEGER NOT NULL,
        nickname TEXT NOT NULL,
        is_default INTEGER NOT NULL DEFAULT 1,
        lang_override TEXT,
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL,
        UNIQUE(gamevox_user_id, realm, account_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_user_bindings_user ON user_bindings(gamevox_user_id, is_default DESC)",
    """
    CREATE TABLE IF NOT EXISTS tracked_accounts (
        realm TEXT NOT NULL,
        account_id INTEGER NOT NULL,
        nickname TEXT NOT NULL DEFAULT '',
        clan_id INTEGER,
        clan_tag TEXT,
        last_battle_time INTEGER NOT NULL DEFAULT 0,
        last_polled_at INTEGER NOT NULL DEFAULT 0,
        baseline_seeded INTEGER NOT NULL DEFAULT 0,
        hidden_profile INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (realm, account_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_tracked_accounts_realm ON tracked_accounts(realm, last_polled_at)",
    """
    CREATE TABLE IF NOT EXISTS tracked_subscriptions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        realm TEXT NOT NULL,
        account_id INTEGER NOT NULL,
        guild_id TEXT NOT NULL,
        channel_id TEXT NOT NULL,
        subscribed_by_user_id TEXT NOT NULL DEFAULT '',
        enabled INTEGER NOT NULL DEFAULT 1,
        created_at INTEGER NOT NULL,
        UNIQUE(realm, account_id, channel_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_tracked_subs_lookup ON tracked_subscriptions(realm, account_id, enabled)",
    "CREATE INDEX IF NOT EXISTS idx_tracked_subs_guild ON tracked_subscriptions(guild_id, enabled)",
    """
    CREATE TABLE IF NOT EXISTS ship_snapshots (
        realm TEXT NOT NULL,
        account_id INTEGER NOT NULL,
        ship_id INTEGER NOT NULL,
        stats_json TEXT NOT NULL,
        updated_at INTEGER NOT NULL,
        PRIMARY KEY (realm, account_id, ship_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS daily_snapshots (
        realm TEXT NOT NULL,
        account_id INTEGER NOT NULL,
        snapshot_date TEXT NOT NULL,
        summary_json TEXT NOT NULL,
        ships_json TEXT NOT NULL,
        created_at INTEGER NOT NULL,
        PRIMARY KEY (realm, account_id, snapshot_date)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS match_groups (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        realm TEXT NOT NULL,
        battle_type TEXT NOT NULL,
        battle_time INTEGER NOT NULL,
        is_win INTEGER NOT NULL,
        expected_div_size INTEGER NOT NULL DEFAULT 1,
        min_tier INTEGER NOT NULL DEFAULT 0,
        max_tier INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'collecting',
        created_at INTEGER NOT NULL,
        finalized_at INTEGER,
        image_path TEXT
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_match_groups_collecting
    ON match_groups(status, realm, battle_type, is_win, battle_time)
    """,
    """
    CREATE TABLE IF NOT EXISTS battle_records (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        match_group_id INTEGER NOT NULL,
        realm TEXT NOT NULL,
        account_id INTEGER NOT NULL,
        nickname TEXT NOT NULL,
        clan_tag TEXT,
        ship_id INTEGER NOT NULL,
        ship_tier INTEGER NOT NULL DEFAULT 0,
        battle_type TEXT NOT NULL,
        battle_time INTEGER NOT NULL,
        battles_delta INTEGER NOT NULL DEFAULT 1,
        wins_delta INTEGER NOT NULL DEFAULT 0,
        survived_delta INTEGER NOT NULL DEFAULT 0,
        damage_delta INTEGER NOT NULL DEFAULT 0,
        frags_delta INTEGER NOT NULL DEFAULT 0,
        xp_delta INTEGER NOT NULL DEFAULT 0,
        spotting_delta INTEGER NOT NULL DEFAULT 0,
        potential_delta INTEGER NOT NULL DEFAULT 0,
        planes_delta INTEGER NOT NULL DEFAULT 0,
        main_hits_delta INTEGER NOT NULL DEFAULT 0,
        main_shots_delta INTEGER NOT NULL DEFAULT 0,
        pr_value REAL NOT NULL DEFAULT 0.0,
        created_at INTEGER NOT NULL,
        FOREIGN KEY (match_group_id) REFERENCES match_groups(id) ON DELETE CASCADE
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_battle_records_group ON battle_records(match_group_id)",
    "CREATE INDEX IF NOT EXISTS idx_battle_records_account ON battle_records(realm, account_id, battle_time DESC)",
    """
    CREATE TABLE IF NOT EXISTS match_deliveries (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        match_group_id INTEGER NOT NULL,
        channel_id TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        message_id TEXT,
        error TEXT,
        delivered_at INTEGER,
        UNIQUE(match_group_id, channel_id),
        FOREIGN KEY (match_group_id) REFERENCES match_groups(id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS encyclopedia_ships (
        ship_id INTEGER PRIMARY KEY,
        name_en TEXT NOT NULL,
        name_zh TEXT NOT NULL,
        tier INTEGER NOT NULL DEFAULT 0,
        ship_type TEXT NOT NULL DEFAULT '',
        nation TEXT NOT NULL DEFAULT '',
        is_premium INTEGER NOT NULL DEFAULT 0,
        is_special INTEGER NOT NULL DEFAULT 0,
        icon_small TEXT NOT NULL DEFAULT '',
        icon_medium TEXT NOT NULL DEFAULT '',
        icon_large TEXT NOT NULL DEFAULT '',
        updated_at INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS pr_expected_values (
        ship_id INTEGER PRIMARY KEY,
        average_damage REAL NOT NULL,
        average_frags REAL NOT NULL,
        win_rate REAL NOT NULL,
        updated_at INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS metadata (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL,
        updated_at INTEGER NOT NULL
    )
    """,
)

MIGRATION_V2_SQL: tuple[str, ...] = (
    # Dedicated user language preferences table (works even for unbound users)
    """
    CREATE TABLE IF NOT EXISTS user_preferences (
        gamevox_user_id TEXT PRIMARY KEY,
        lang TEXT NOT NULL DEFAULT 'zh',
        updated_at INTEGER NOT NULL
    )
    """,
    # Lease timestamp on match_deliveries so crashed 'sending' slots can be reclaimed
    "ALTER TABLE match_deliveries ADD COLUMN claimed_at INTEGER",
)

MIGRATIONS: dict[int, tuple[str, ...]] = {
    1: MIGRATION_V1_SQL,
    2: MIGRATION_V2_SQL,
}


class DatabaseManager:
    """Owns the single SQLite WAL file + Honker queues."""

    def __init__(self, db_path: Path, watch_interval_ms: int = 25) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.watch_interval_ms = watch_interval_ms
        self._db: honker.Database | None = None
        self.poll_queue: honker.Queue | None = None
        self.match_queue: honker.Queue | None = None
        self.render_queue: honker.Queue | None = None

    @property
    def db(self) -> honker.Database:
        if self._db is None:
            raise RuntimeError("DatabaseManager is not opened yet. Call open() first.")
        return self._db

    def open(self) -> DatabaseManager:
        if self._db is not None:
            return self

        self._db = honker.open(
            str(self.db_path),
            max_readers=8,
            watcher_poll_interval_ms=self.watch_interval_ms,
        )

        mode = str(self._db.query("PRAGMA journal_mode")[0]["journal_mode"]).lower()
        if mode != "wal":
            logger.warning("Expected SQLite WAL mode, got {}", mode)
        with self._db.transaction() as tx:
            tx.execute("PRAGMA foreign_keys=ON")

        self._run_migrations()

        # Construct queues outside any transaction (Honker requirement)
        self.poll_queue = self._db.queue("wows_poll", visibility_timeout_s=120, max_attempts=3)
        self.match_queue = self._db.queue("wows_match", visibility_timeout_s=120, max_attempts=5)
        self.render_queue = self._db.queue("wows_render", visibility_timeout_s=180, max_attempts=5)
        return self

    def _backup_wal_consistent(self, current_version: int) -> None:
        """Create a WAL-consistent online SQLite backup before running schema migrations."""
        if not self.db_path.exists():
            return
        backup_path = self.db_path.with_suffix(f".db.v{current_version}.{int(time.time())}.bak")
        src = sqlite3.connect(str(self.db_path))
        dst = sqlite3.connect(str(backup_path))
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
        logger.info("Created WAL-consistent SQLite backup at {}", backup_path)

    def _run_migrations(self) -> None:
        rows = self.db.query("PRAGMA user_version")
        current = int(rows[0]["user_version"]) if rows else 0
        if current > SCHEMA_VERSION:
            raise RuntimeError(
                f"Database schema v{current} is newer than supported v{SCHEMA_VERSION}"
            )
        if current == SCHEMA_VERSION:
            return

        if current > 0:
            self._backup_wal_consistent(current)

        for target_ver in range(current + 1, SCHEMA_VERSION + 1):
            stmts = MIGRATIONS[target_ver]
            logger.info("Applying SQLite schema migration v{} to {}", target_ver, self.db_path)
            with self.db.transaction() as tx:
                for stmt in stmts:
                    tx.execute(stmt)
                tx.execute(f"PRAGMA user_version = {target_ver}")

    def close(self) -> None:
        if self._db is not None:
            self._db.close()
            self._db = None
            self.poll_queue = None
            self.match_queue = None
            self.render_queue = None
