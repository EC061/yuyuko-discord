"""Security, GameVox compatibility, and reliability regression tests."""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import MagicMock

import discord
import httpx
import pytest
import respx

from wows_bot.bot.gamevox import patch_discord_for_gamevox
from wows_bot.clients.pr_source import PRSourceClient
from wows_bot.clients.wg_api import WGApiClient, WGApiError, sanitize_wg_icon_url
from wows_bot.config import Settings
from wows_bot.db.repository import BattleRecordInput, Repository, ShipMeta
from wows_bot.db.schema import DatabaseManager
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
        gamevox_user_id="u_g1", realm="na", account_id=1001, nickname="Guild1Player",
        guild_id="guild-1", auto_post_channel_id="chan-1",
    )
    repo.bind_account(
        gamevox_user_id="u_g2", realm="na", account_id=2002, nickname="Guild2Stranger",
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
    repo.bind_account(gamevox_user_id="u1", realm="na", account_id=1001, nickname="Edward")
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


def test_gamevox_discord_compat_shims() -> None:
    patch_discord_for_gamevox(Settings())
    state = MagicMock()
    state.create_user.side_effect = lambda d: d

    # AppInfo without `owner`
    app = discord.AppInfo(state, {"id": "1574076427811074049", "name": "WoWS Stats"})
    assert app.id == 1574076427811074049
    assert app.owner["id"] == "1574076427811074049"

    # Attachment without `proxy_url`
    att = discord.Attachment(data={"id": "1", "filename": "card.png", "url": "https://cdn.gamevox.com/a.png"}, state=state)
    assert att.proxy_url == "https://cdn.gamevox.com/a.png"


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
