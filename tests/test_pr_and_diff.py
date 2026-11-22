"""Tests for PR calculation, stat diffing, and overview/recent builders."""

from __future__ import annotations

from wows_bot.db.repository import ShipMeta
from wows_bot.services.pr import (
    ExpectedShipValues,
    calculate_pr_aggregate,
    calculate_pr_single_ship,
    classify_pr,
)
from wows_bot.services.stats_builder import (
    build_player_overview,
    build_recent_stats_view,
    compact_ship_snapshot,
    compute_ship_deltas,
)


def _sample_meta() -> dict[int, ShipMeta]:
    return {
        4276041424: ShipMeta(
            ship_id=4276041424,
            name_en="Yamato",
            name_zh="大和",
            tier=10,
            ship_type="Battleship",
            nation="japan",
            is_premium=False,
            is_special=False,
            icon_small="",
            icon_medium="",
            icon_large="",
        ),
        4179506480: ShipMeta(
            ship_id=4179506480,
            name_en="Halland",
            name_zh="哈兰",
            tier=10,
            ship_type="Destroyer",
            nation="europe",
            is_premium=False,
            is_special=False,
            icon_small="",
            icon_medium="",
            icon_large="",
        ),
    }


def _sample_expected() -> dict[int, ExpectedShipValues]:
    return {
        4276041424: ExpectedShipValues(4276041424, average_damage=95000.0, average_frags=0.95, win_rate=50.0),
        4179506480: ExpectedShipValues(4179506480, average_damage=58000.0, average_frags=0.80, win_rate=51.0),
    }


def test_pr_exact_expected_is_1150() -> None:
    exp = _sample_expected()[4276041424]
    # Exact server average => nDmg=1, nFrags=1, nWins=1 => 700+300+150 = 1150
    pr = calculate_pr_single_ship(10, 950_000, 9.5, 5.0, exp)
    assert abs(pr - 1150.0) < 0.01
    rating = classify_pr(pr)
    assert rating.tier_key == "average"
    assert rating.value == 1150


def test_pr_aggregate_super_unicum() -> None:
    pr = calculate_pr_aggregate(
        [
            (4276041424, 10, 1_800_000, 20, 8),
            (4179506480, 10, 1_100_000, 18, 8),
        ],
        _sample_expected(),
    )
    assert pr > 2450
    assert classify_pr(pr).tier_key == "super_unicum"


def test_compute_ship_deltas_single_and_multi() -> None:
    old = {
        4276041424: {
            "pvp_div2": {
                "battles": 100,
                "wins": 60,
                "survived_battles": 50,
                "damage_dealt": 10_000_000,
                "frags": 120,
                "xp": 180_000,
                "damage_scouting": 2_000_000,
                "art_agro": 80_000_000,
                "torpedo_agro": 10_000_000,
                "planes_killed": 400,
                "main_hits": 2000,
                "main_shots": 6000,
            }
        }
    }
    new = {
        4276041424: {
            "pvp_div2": {
                "battles": 101,
                "wins": 61,
                "survived_battles": 51,
                "damage_dealt": 10_165_000,
                "frags": 123,
                "xp": 182_450,
                "damage_scouting": 2_045_000,
                "art_agro": 81_200_000,
                "torpedo_agro": 10_100_000,
                "planes_killed": 407,
                "main_hits": 2032,
                "main_shots": 6090,
            }
        }
    }

    deltas = compute_ship_deltas(
        realm="na",
        account_id=1001,
        nickname="AdmiralA",
        clan_tag="TEST",
        battle_time=1_750_000_000,
        old_snapshots=old,
        new_snapshots=new,
        ships_meta=_sample_meta(),
        expected_map=_sample_expected(),
    )
    assert len(deltas) == 1
    d = deltas[0]
    assert d.battle_type == "pvp_div2"
    assert d.battles_delta == 1
    assert d.wins_delta == 1
    assert d.damage_delta == 165_000
    assert d.frags_delta == 3
    assert d.xp_delta == 2_450
    assert d.potential_delta == 1_300_000
    assert d.pr_value > 2000


def test_overview_and_recent_builders() -> None:
    ship_entry = {
        "ship_id": 4276041424,
        "pvp": {
            "battles": 20,
            "wins": 13,
            "survived_battles": 10,
            "damage_dealt": 2_400_000,
            "frags": 26,
            "xp": 40_000,
            "main_battery": {"hits": 500, "shots": 1500},
            "max_damage_dealt": 210_000,
            "max_frags_battle": 5,
        },
        "pvp_solo": {
            "battles": 12,
            "wins": 7,
            "survived_battles": 6,
            "damage_dealt": 1_400_000,
            "frags": 15,
            "xp": 24_000,
        },
    }
    overview = build_player_overview(
        realm="na",
        account_info={"account_id": 1001, "nickname": "AdmiralA", "last_battle_time": 1_750_000_000},
        ships_stats=[ship_entry],
        clan_info={"tag": "ESX", "name": "Edward Server"},
        ships_meta=_sample_meta(),
        expected_map=_sample_expected(),
        lang="zh",
    )
    assert overview["pvp"]["battles"] == 20
    assert overview["pvp"]["avg_damage"] == 120_000
    assert overview["top_ships"][0]["name"] == "大和"

    cur_snap = {4276041424: compact_ship_snapshot(ship_entry)}
    base_snap = {
        4276041424: {
            "pvp": {
                "battles": 15,
                "wins": 9,
                "survived_battles": 7,
                "damage_dealt": 1_700_000,
                "frags": 18,
                "xp": 30_000,
            }
        }
    }
    recent = build_recent_stats_view(
        realm="na",
        account_id=1001,
        nickname="AdmiralA",
        clan_tag="ESX",
        days=7,
        since_date="2026-11-15",
        current_snapshots=cur_snap,
        baseline_ships=base_snap,
        ships_meta=_sample_meta(),
        expected_map=_sample_expected(),
        lang="zh",
    )
    assert recent is not None
    assert recent["summary"]["battles"] == 5
    assert recent["summary"]["avg_damage"] == 140_000
