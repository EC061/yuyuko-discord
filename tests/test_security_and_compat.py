"""Security, native Discord policy, migration, and reliability regression tests."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from unittest.mock import MagicMock

import discord
import httpx
import pytest
import respx

from wows_bot.bot.discord_bot import WoWSDiscordBot, normalize_command_tokens
from wows_bot.clients.pr_source import PRSourceClient
from wows_bot.clients.wg_api import WGApiClient, WGApiError, sanitize_wg_icon_url
from wows_bot.config import Settings
from wows_bot.db.repository import BattleRecordInput, Repository, ShipMeta
from wows_bot.db.schema import MIGRATION_V1_SQL, MIGRATION_V2_SQL, DatabaseManager
from wows_bot.i18n import I18n
from wows_bot.main import _run_healthcheck
from wows_bot.services.poller import PollerService, ShipsStatsLagError


@pytest.fixture()
def repo(tmp_path: Path):
    dbm = DatabaseManager(tmp_path / "sec_test.db", watch_interval_ms=10).open()
    r = Repository(dbm)
    r.upsert_ships(
        [ShipMeta(4276041424, "Yamato", "大和", 10, "Battleship", "japan", False, False, "", "", "")]
    )
    yield r
    dbm.close()


@pytest.mark.asyncio
async def test_wg_application_id_never_leaks_on_http_error() -> None:
    secret_key = "SUPER_SECRET_WG_APP_ID_99999999"
    wg = WGApiClient(secret_key, rate_per_sec=100.0)
    with respx.mock() as mock:
        mock.get("https://api.worldofwarships.com/wows/account/list/").mock(
            return_value=httpx.Response(503, text="Service Unavailable")
        )
        with pytest.raises(WGApiError) as exc_info:
            await wg.search_account("na", "Edward")
    await wg.aclose()
    assert secret_key not in str(exc_info.value)
    assert "503" in str(exc_info.value)


def test_wg_icon_url_sanitizer_blocks_imds_and_file() -> None:
    assert sanitize_wg_icon_url("https://glossary-wows-global.gcdn.co/icons/ship.png").startswith("https://")
    assert sanitize_wg_icon_url("https://wows-gloss-icons.wgcdn.co/icons/ship.png").startswith("https://")
    assert sanitize_wg_icon_url("http://169.254.169.254/opc/v1/instance/") == ""
    assert sanitize_wg_icon_url("file:///etc/passwd") == ""
    assert sanitize_wg_icon_url("http://glossary-wows-global.gcdn.co/icons/ship.png") == ""
    assert sanitize_wg_icon_url("https://evil.example.com/x.png") == ""


def test_cross_guild_divisions_never_merge(repo: Repository) -> None:
    repo.bind_account(
        discord_user_id="u_g1", realm="na", account_id=1001, nickname="Guild1Player",
        guild_id="guild-1", auto_post_channel_id="chan-1",
    )
    repo.bind_account(
        discord_user_id="u_g2", realm="na", account_id=2002, nickname="Guild2Stranger",
        guild_id="guild-2", auto_post_channel_id="chan-2",
    )
    t0 = 1_760_100_000
    r1 = BattleRecordInput(
        "na", 1001, "Guild1Player", "C1", 4276041424, 10, "pvp_div2", t0,
        1, 1, 1, 120000, 2, 2000, 30000, 1000000, 3, 40, 120, 1800.0,
    )
    r2 = BattleRecordInput(
        "na", 2002, "Guild2Stranger", "C2", 4276041424, 10, "pvp_div2", t0,
        1, 1, 1, 130000, 2, 2100, 35000, 1100000, 4, 45, 130, 1900.0,
    )
    o1 = repo.record_battle_and_coalesce(r1)
    o2 = repo.record_battle_and_coalesce(r2)
    assert o1.match_group_id != o2.match_group_id


def test_unbound_user_language_does_not_mutate_guild_settings(repo: Repository) -> None:
    repo.set_user_language("unbound-user", "en")
    assert repo.resolve_language(user_id="unbound-user", guild_id="guild-1", fallback="zh") == "en"
    assert repo.get_guild_settings("guild-1") is None
    assert repo.resolve_language(user_id="other-user", guild_id="guild-1", fallback="zh") == "zh"


def test_delivery_slot_reclaims_stale_sending_lease(repo: Repository) -> None:
    r1 = BattleRecordInput(
        "na", 1001, "Solo", None, 4276041424, 10, "pvp_solo", 1_760_200_000,
        1, 1, 1, 100000, 1, 1800, 20000, 800000, 2, 30, 100, 1500.0,
    )
    out = repo.record_battle_and_coalesce(r1)
    assert repo.claim_delivery_slot(out.match_group_id, "chan-1", lease_seconds=180) is True
    # Active lease blocks concurrent re-claim
    assert repo.claim_delivery_slot(out.match_group_id, "chan-1", lease_seconds=180) is False
    # Simulate stale lease (claimed 300s ago)
    with repo.db.transaction() as tx:
        tx.execute(
            "UPDATE match_deliveries SET claimed_at = ? WHERE match_group_id = ?",
            [int(time.time()) - 300, out.match_group_id],
        )
    assert repo.claim_delivery_slot(out.match_group_id, "chan-1", lease_seconds=180) is True
    repo.mark_delivery_result(out.match_group_id, "chan-1", success=True, message_id="m-1")
    # Once sent, never re-claimable
    assert repo.claim_delivery_slot(out.match_group_id, "chan-1", lease_seconds=0) is False


@pytest.mark.asyncio
async def test_poller_retries_when_wg_ships_stats_lags(repo: Repository, tmp_path: Path) -> None:
    repo.bind_account(discord_user_id="u1", realm="na", account_id=1001, nickname="Edward")
    settings = Settings(wg_application_id="demo_key", data_dir=tmp_path, db_path=tmp_path / "sec_test.db")
    wg = WGApiClient("demo_key", rate_per_sec=100.0)
    pr_client = PRSourceClient("https://example.invalid")
    poller = PollerService(settings, repo, wg, pr_client)

    now = int(time.time())
    with respx.mock(assert_all_called=False) as mock:
        mock.get("https://api.worldofwarships.com/wows/clans/accountinfo/").mock(
            return_value=httpx.Response(200, json={"status": "ok", "data": {"1001": None}})
        )
        mock.get("https://api.worldofwarships.com/wows/ships/stats/").mock(
            return_value=httpx.Response(
                200,
                json={"status": "ok", "data": {"1001": [{"ship_id": 4276041424, "pvp_solo": {"battles": 10, "wins": 5, "damage_dealt": 1000000, "frags": 10, "xp": 18000}}]}},
            )
        )
        # Seed baseline at now - 300
        await poller.process_player_diff("na", 1001, nickname="Edward", last_battle_time=now - 300)
        # Fresh last_battle_time (now - 10s) arrives before ships/stats increments -> must raise ShipsStatsLagError
        with pytest.raises(ShipsStatsLagError):
            await poller.process_player_diff("na", 1001, nickname="Edward", last_battle_time=now - 10)
        # Watermark must NOT have advanced!
        tracked = repo.get_tracked_account("na", 1001)
        assert tracked is not None
        assert tracked.last_battle_time == now - 300

    await wg.aclose()
    await pr_client.aclose()


def test_strict_admin_and_bound_channel_policy(repo: Repository, tmp_path: Path) -> None:
    settings = Settings(
        discord_bot_token="fake-token",
        default_guild_id="815711272262828063",
        default_auto_post_channel_id="1576054680713166848",
        restrict_to_bound_channel=True,
        admin_user_ids="363790401582268416",
        data_dir=tmp_path,
        db_path=tmp_path / "sec_test.db",
    )
    bot = WoWSDiscordBot(
        settings=settings,
        repo=repo,
        wg=MagicMock(),
        yuyuko_rank=MagicMock(),
        renderer=MagicMock(),
        i18n=I18n(settings.locales_dir),
    )

    def _msg(user_id: int, guild_id: int, channel_id: int, *, owner_id: int = 999, is_guild_admin: bool = False):
        m = MagicMock(spec=discord.Message)
        m.guild = MagicMock()
        m.guild.id = guild_id
        m.guild.owner_id = owner_id
        m.channel = MagicMock()
        m.channel.id = channel_id
        m.author = MagicMock(spec=discord.Member)
        m.author.id = user_id
        m.author.guild_permissions = discord.Permissions(administrator=is_guild_admin)
        return m

    admin_msg = _msg(363790401582268416, 815711272262828063, 1576054680713166848)
    other_owner_msg = _msg(999, 815711272262828063, 1576054680713166848, owner_id=999, is_guild_admin=True)
    wrong_ch_user_msg = _msg(12345, 815711272262828063, 1111111111111111111)
    wrong_ch_admin_msg = _msg(363790401582268416, 815711272262828063, 1111111111111111111)

    # Strict single-admin enforcement: ONLY 363790401582268416 is admin, even if another user owns the server
    assert bot.is_admin_user_id("363790401582268416") is True
    assert bot._is_admin(admin_msg) is True
    assert bot._is_admin(other_owner_msg) is False

    # Bound channel enforcement
    assert bot._should_accept_channel(admin_msg, "help") is True
    assert bot._should_accept_channel(wrong_ch_user_msg, "help") is False
    assert bot._should_accept_channel(wrong_ch_user_msg, "channel") is False
    assert bot._should_accept_channel(wrong_ch_admin_msg, "help") is False
    assert bot._should_accept_channel(wrong_ch_admin_msg, "channel") is True


def test_schema_migration_v2_to_v3_preserves_bindings(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy_v2.db"
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        for stmt in (*MIGRATION_V1_SQL, *MIGRATION_V2_SQL):
            conn.execute(stmt)
        conn.execute(
            """
            INSERT INTO user_bindings (gamevox_user_id, realm, account_id, nickname, is_default, created_at, updated_at)
            VALUES ('363790401582268416', 'na', 1001, 'LegacyPlayer', 1, 1000, 1000)
            """
        )
        conn.execute(
            "INSERT INTO user_preferences (gamevox_user_id, lang, updated_at) VALUES ('363790401582268416', 'en', 1000)"
        )
        conn.execute("PRAGMA user_version = 2")
        conn.commit()
    finally:
        conn.close()

    dbm = DatabaseManager(db_path, watch_interval_ms=10).open()
    try:
        repo = Repository(dbm)
        binding = repo.get_default_binding("363790401582268416")
        assert binding is not None
        assert binding.discord_user_id == "363790401582268416"
        assert binding.nickname == "LegacyPlayer"
        assert repo.resolve_language(user_id="363790401582268416") == "en"
    finally:
        dbm.close()


def test_readonly_healthcheck_checks_heartbeat(tmp_path: Path) -> None:
    db_path = tmp_path / "hc.db"
    settings = Settings(data_dir=tmp_path, db_path=db_path)
    assert _run_healthcheck(settings) == 1

    dbm = DatabaseManager(db_path).open()
    repo = Repository(dbm)
    repo.touch_heartbeat("ok")
    assert _run_healthcheck(settings) == 0

    # Stale heartbeat (> 180s) fails healthcheck
    with dbm.db.transaction() as tx:
        tx.execute("UPDATE metadata SET updated_at = ? WHERE key = 'heartbeat'", [int(time.time()) - 300])
    assert _run_healthcheck(settings) == 1
    dbm.close()


def test_me_subcommand_normalization_and_subscription_status(repo: Repository) -> None:
    assert normalize_command_tokens(["me", "last"]) == ("last", [])
    assert normalize_command_tokens(["last", "me"]) == ("last", [])
    assert normalize_command_tokens(["me", "recents"]) == ("recents", [])
    assert normalize_command_tokens(["me", "recent", "14"]) == ("recent", ["14"])
    assert normalize_command_tokens(["recent", "me", "7"]) == ("recent", ["7"])
    assert normalize_command_tokens(["me", "ship", "大和"]) == ("ship", ["大和"])
    assert normalize_command_tokens(["me", "track", "off"]) == ("track", ["off"])
    assert normalize_command_tokens(["me"]) == ("me", [])
    assert normalize_command_tokens(["na", "PlayerOne"]) == ("na", ["PlayerOne"])

    repo.bind_account(
        discord_user_id="u1",
        realm="na",
        account_id=1001,
        nickname="PlayerOne",
        guild_id="g1",
        auto_post_channel_id="c1",
    )
    assert repo.get_enabled_subscription_channels("na", 1001, guild_id="g1") == ["c1"]
    assert repo.get_enabled_subscription_channels("na", 1001, guild_id="g2") == []
