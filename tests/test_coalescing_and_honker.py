"""Integration tests for SQLite WAL, Honker outbox, division coalescing, and PollerService."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx

from wows_bot.clients.pr_source import PRSourceClient
from wows_bot.clients.wg_api import WGApiClient
from wows_bot.config import Settings
from wows_bot.db.repository import BattleRecordInput, Repository, ShipMeta
from wows_bot.db.schema import DatabaseManager
from wows_bot.services.poller import PollerService
from wows_bot.services.pr import ExpectedShipValues


@pytest.fixture()
def repo(tmp_path: Path):
    dbm = DatabaseManager(tmp_path / "test_wows.db", watch_interval_ms=10).open()
    r = Repository(dbm)
    r.upsert_ships(
        [
            ShipMeta(4276041424, "Yamato", "大和", 10, "Battleship", "japan", False, False, "", "", ""),
            ShipMeta(4179506480, "Halland", "哈兰", 10, "Destroyer", "europe", False, False, "", "", ""),
            ShipMeta(4282267600, "Des Moines", "得梅因", 10, "Cruiser", "usa", False, False, "", "", ""),
            ShipMeta(4290656208, "Kongo", "金刚", 5, "Battleship", "japan", False, False, "", "", ""),
        ]
    )
    r.upsert_pr_expected(
        {
            4276041424: ExpectedShipValues(4276041424, 95000.0, 0.95, 50.0),
            4179506480: ExpectedShipValues(4179506480, 58000.0, 0.80, 51.0),
            4282267600: ExpectedShipValues(4282267600, 78000.0, 0.92, 50.5),
            4290656208: ExpectedShipValues(4290656208, 42000.0, 0.85, 50.0),
        }
    )
    yield r
    dbm.close()


def _make_record(
    *,
    account_id: int,
    nickname: str,
    ship_id: int,
    ship_tier: int,
    battle_type: str,
    battle_time: int,
    wins_delta: int = 1,
    damage_delta: int = 140_000,
) -> BattleRecordInput:
    return BattleRecordInput(
        realm="na",
        account_id=account_id,
        nickname=nickname,
        clan_tag="ESX",
        ship_id=ship_id,
        ship_tier=ship_tier,
        battle_type=battle_type,
        battle_time=battle_time,
        battles_delta=1,
        wins_delta=wins_delta,
        survived_delta=1,
        damage_delta=damage_delta,
        frags_delta=2,
        xp_delta=2200,
        spotting_delta=45000,
        potential_delta=1200000,
        planes_delta=5,
        main_hits_delta=50,
        main_shots_delta=150,
        pr_value=2100.0,
    )


def test_sqlite_wal_mode_enabled(repo: Repository) -> None:
    mode = repo.db.query("PRAGMA journal_mode")[0]["journal_mode"]
    assert str(mode).lower() == "wal"


def test_two_player_division_coalesces_into_single_match_and_one_post(repo: Repository) -> None:
    # Bind both squadmates to the same GameVox channel (#战舰世界)
    repo.bind_account(
        gamevox_user_id="u1",
        realm="na",
        account_id=1001,
        nickname="Edward",
        guild_id="1487625003808419840",
        auto_post_channel_id="1574077276956221440",
    )
    repo.bind_account(
        gamevox_user_id="u2",
        realm="na",
        account_id=1002,
        nickname="Wingman",
        guild_id="1487625003808419840",
        auto_post_channel_id="1574077276956221440",
    )

    t0 = 1_760_000_100
    # Player 1 finishes div2 -> collecting + delayed jobs enqueued in Honker
    out1 = repo.record_battle_and_coalesce(
        _make_record(account_id=1001, nickname="Edward", ship_id=4276041424, ship_tier=10, battle_type="pvp_div2", battle_time=t0),
        recheck_delay_s=12,
        finalize_delay_s=28,
    )
    assert out1.created_new_group is True
    assert out1.finalized_immediately is False
    assert out1.member_count == 1

    # Player 2 settles 12s later with battle_time within tolerance -> merges into SAME group & finalizes!
    out2 = repo.record_battle_and_coalesce(
        _make_record(account_id=1002, nickname="Wingman", ship_id=4179506480, ship_tier=10, battle_type="pvp_div2", battle_time=t0 + 6),
        recheck_delay_s=12,
        finalize_delay_s=28,
    )
    assert out2.created_new_group is False
    assert out2.match_group_id == out1.match_group_id
    assert out2.finalized_immediately is True
    assert out2.member_count == 2

    # Delayed finalize is now a clean no-op
    assert repo.finalize_match_group(out1.match_group_id) is False

    # Honker render queue has exactly ONE render_and_post job for the combined match group
    render_job = repo.dbm.render_queue.claim_one("test-renderer")  # type: ignore[union-attr]
    assert render_job is not None
    assert render_job.payload == {"type": "render_and_post", "match_group_id": out1.match_group_id}
    render_job.ack()
    assert repo.dbm.render_queue.claim_one("test-renderer") is None  # type: ignore[union-attr]

    # Target channels deduplicated to 1 channel; delivery slot prevents double-posting
    targets = repo.get_target_channels_for_match_group(out1.match_group_id)
    assert targets == [("1487625003808419840", "1574077276956221440")]
    assert repo.claim_delivery_slot(out1.match_group_id, "1574077276956221440") is True
    repo.mark_delivery_result(out1.match_group_id, "1574077276956221440", success=True, message_id="msg-1")
    assert repo.claim_delivery_slot(out1.match_group_id, "1574077276956221440") is False


def test_three_player_division_coalesces_all_three(repo: Repository) -> None:
    t0 = 1_760_001_000
    o1 = repo.record_battle_and_coalesce(
        _make_record(account_id=1001, nickname="P1", ship_id=4276041424, ship_tier=10, battle_type="pvp_div3", battle_time=t0)
    )
    o2 = repo.record_battle_and_coalesce(
        _make_record(account_id=1002, nickname="P2", ship_id=4179506480, ship_tier=10, battle_type="pvp_div3", battle_time=t0 + 2)
    )
    o3 = repo.record_battle_and_coalesce(
        _make_record(account_id=1003, nickname="P3", ship_id=4282267600, ship_tier=10, battle_type="pvp_div3", battle_time=t0 + 4)
    )
    assert o1.match_group_id == o2.match_group_id == o3.match_group_id
    assert o1.finalized_immediately is False
    assert o2.finalized_immediately is False
    assert o3.finalized_immediately is True
    group = repo.get_match_group_with_records(o1.match_group_id)
    assert group is not None
    assert len(group["records"]) == 3


def test_unrelated_divisions_do_not_merge_on_tier_or_outcome_mismatch(repo: Repository) -> None:
    t0 = 1_760_002_000
    # Tier 10 win vs Tier 5 win -> tier spread 5 > 1 => separate groups
    g_t10 = repo.record_battle_and_coalesce(
        _make_record(account_id=1001, nickname="P1", ship_id=4276041424, ship_tier=10, battle_type="pvp_div2", battle_time=t0, wins_delta=1)
    )
    g_t5 = repo.record_battle_and_coalesce(
        _make_record(account_id=1002, nickname="P2", ship_id=4290656208, ship_tier=5, battle_type="pvp_div2", battle_time=t0, wins_delta=1)
    )
    assert g_t10.match_group_id != g_t5.match_group_id

    # Tier 10 win vs Tier 10 loss -> outcome mismatch => separate groups
    g_loss = repo.record_battle_and_coalesce(
        _make_record(account_id=1003, nickname="P3", ship_id=4179506480, ship_tier=10, battle_type="pvp_div2", battle_time=t0, wins_delta=0)
    )
    assert g_loss.match_group_id != g_t10.match_group_id


def test_solo_match_finalizes_immediately(repo: Repository) -> None:
    out = repo.record_battle_and_coalesce(
        _make_record(account_id=1001, nickname="SoloGuy", ship_id=4276041424, ship_tier=10, battle_type="pvp_solo", battle_time=1_760_003_000)
    )
    assert out.finalized_immediately is True
    job = repo.dbm.render_queue.claim_one("test-w")  # type: ignore[union-attr]
    assert job is not None
    assert job.payload["match_group_id"] == out.match_group_id
    job.ack()


@pytest.mark.asyncio
async def test_end_to_end_poller_seed_then_squad_match(repo: Repository, tmp_path: Path) -> None:
    repo.bind_account(
        gamevox_user_id="u1", realm="na", account_id=1001, nickname="Edward",
        guild_id="g1", auto_post_channel_id="c1",
    )
    repo.bind_account(
        gamevox_user_id="u2", realm="na", account_id=1002, nickname="Friend",
        guild_id="g1", auto_post_channel_id="c1",
    )
    settings = Settings(
        wg_application_id="demo_key",
        data_dir=tmp_path,
        db_path=tmp_path / "test_wows.db",
    )
    wg = WGApiClient("demo_key")
    pr_client = PRSourceClient("https://example.invalid")
    poller = PollerService(settings, repo, wg, pr_client)

    with respx.mock(assert_all_called=False) as mock:
        mock.get("https://api.worldofwarships.com/wows/clans/accountinfo/").mock(
            return_value=httpx.Response(200, json={"status": "ok", "data": {"1001": None, "1002": None}})
        )
        # Seed baseline (battles=10)
        mock.get("https://api.worldofwarships.com/wows/ships/stats/", params__contains={"account_id": "1001"}).mock(
            return_value=httpx.Response(
                200,
                json={"status": "ok", "data": {"1001": [{"ship_id": 4276041424, "pvp_div2": {"battles": 10, "wins": 5, "damage_dealt": 1000000, "frags": 10, "xp": 18000}}]}},
            )
        )
        mock.get("https://api.worldofwarships.com/wows/ships/stats/", params__contains={"account_id": "1002"}).mock(
            return_value=httpx.Response(
                200,
                json={"status": "ok", "data": {"1002": [{"ship_id": 4179506480, "pvp_div2": {"battles": 20, "wins": 11, "damage_dealt": 1200000, "frags": 18, "xp": 35000}}]}},
            )
        )
        assert await poller.process_player_diff("na", 1001, nickname="Edward", last_battle_time=1000) == []
        assert await poller.process_player_diff("na", 1002, nickname="Friend", last_battle_time=1000) == []

        # Both play 1 div2 match together at t=2000
        mock.get("https://api.worldofwarships.com/wows/ships/stats/", params__contains={"account_id": "1001"}).mock(
            return_value=httpx.Response(
                200,
                json={"status": "ok", "data": {"1001": [{"ship_id": 4276041424, "pvp_div2": {"battles": 11, "wins": 6, "damage_dealt": 1185000, "frags": 13, "xp": 20500}}]}},
            )
        )
        mock.get("https://api.worldofwarships.com/wows/ships/stats/", params__contains={"account_id": "1002"}).mock(
            return_value=httpx.Response(
                200,
                json={"status": "ok", "data": {"1002": [{"ship_id": 4179506480, "pvp_div2": {"battles": 21, "wins": 12, "damage_dealt": 1295000, "frags": 20, "xp": 37400}}]}},
            )
        )
        g1 = await poller.process_player_diff("na", 1001, nickname="Edward", last_battle_time=2000)
        g2 = await poller.process_player_diff("na", 1002, nickname="Friend", last_battle_time=2003)

    await wg.aclose()
    await pr_client.aclose()

    assert len(g1) == 1 and len(g2) == 1
    assert g1[0] == g2[0]
    group = repo.get_match_group_with_records(g1[0])
    assert group is not None
    assert group["status"] == "finalized"
    assert len(group["records"]) == 2
