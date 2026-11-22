"""WG stat normalization, diff computation, and view-model builders."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from wows_bot.db.repository import BattleRecordInput, ShipMeta
from wows_bot.services.pr import (
    ExpectedShipValues,
    calculate_pr_aggregate,
    calculate_pr_single_ship,
    classify_pr,
    win_rate_color,
)

TRACKED_MODES: tuple[str, ...] = ("pvp_solo", "pvp_div2", "pvp_div3", "rank_solo")


def extract_mode_bucket(ship_entry: dict[str, Any], mode: str) -> dict[str, int]:
    b = ship_entry.get(mode) or {}
    main_bat = b.get("main_battery") or {}
    return {
        "battles": int(b.get("battles") or 0),
        "wins": int(b.get("wins") or 0),
        "survived_battles": int(b.get("survived_battles") or 0),
        "damage_dealt": int(b.get("damage_dealt") or 0),
        "frags": int(b.get("frags") or 0),
        "xp": int(b.get("xp") or 0),
        "damage_scouting": int(b.get("damage_scouting") or 0),
        "art_agro": int(b.get("art_agro") or 0),
        "torpedo_agro": int(b.get("torpedo_agro") or 0),
        "planes_killed": int(b.get("planes_killed") or 0),
        "main_hits": int(main_bat.get("hits") or 0),
        "main_shots": int(main_bat.get("shots") or 0),
        "max_damage_dealt": int(b.get("max_damage_dealt") or 0),
        "max_frags_battle": int(b.get("max_frags_battle") or 0),
    }


def compact_ship_snapshot(ship_entry: dict[str, Any]) -> dict[str, Any]:
    """Store a compact, deterministic representation of a ship's cumulative stats."""
    out: dict[str, Any] = {}
    for mode in ("pvp", *TRACKED_MODES):
        bucket = extract_mode_bucket(ship_entry, mode)
        if bucket["battles"] > 0:
            out[mode] = bucket
    return out


def compute_ship_deltas(
    *,
    realm: str,
    account_id: int,
    nickname: str,
    clan_tag: str | None,
    battle_time: int,
    old_snapshots: dict[int, dict[str, Any]],
    new_snapshots: dict[int, dict[str, Any]],
    ships_meta: dict[int, ShipMeta],
    expected_map: dict[int, ExpectedShipValues],
) -> list[BattleRecordInput]:
    """Compare old vs new ship snapshots and emit a BattleRecordInput for every mode delta."""
    records: list[BattleRecordInput] = []
    for ship_id, new_ship in new_snapshots.items():
        old_ship = old_snapshots.get(ship_id) or {}
        meta = ships_meta.get(ship_id)
        tier = meta.tier if meta else 0
        expected = expected_map.get(ship_id)

        for mode in TRACKED_MODES:
            new_b = new_ship.get(mode) or {}
            old_b = old_ship.get(mode) or {}
            b_delta = int(new_b.get("battles") or 0) - int(old_b.get("battles") or 0)
            if b_delta <= 0:
                continue

            wins_d = max(0, int(new_b.get("wins") or 0) - int(old_b.get("wins") or 0))
            surv_d = max(0, int(new_b.get("survived_battles") or 0) - int(old_b.get("survived_battles") or 0))
            dmg_d = max(0, int(new_b.get("damage_dealt") or 0) - int(old_b.get("damage_dealt") or 0))
            frags_d = max(0, int(new_b.get("frags") or 0) - int(old_b.get("frags") or 0))
            xp_d = max(0, int(new_b.get("xp") or 0) - int(old_b.get("xp") or 0))
            spot_d = max(0, int(new_b.get("damage_scouting") or 0) - int(old_b.get("damage_scouting") or 0))
            pot_d = max(
                0,
                (int(new_b.get("art_agro") or 0) + int(new_b.get("torpedo_agro") or 0))
                - (int(old_b.get("art_agro") or 0) + int(old_b.get("torpedo_agro") or 0)),
            )
            planes_d = max(0, int(new_b.get("planes_killed") or 0) - int(old_b.get("planes_killed") or 0))
            hits_d = max(0, int(new_b.get("main_hits") or 0) - int(old_b.get("main_hits") or 0))
            shots_d = max(0, int(new_b.get("main_shots") or 0) - int(old_b.get("main_shots") or 0))

            pr_val = calculate_pr_single_ship(b_delta, dmg_d, frags_d, wins_d, expected)

            records.append(
                BattleRecordInput(
                    realm=realm,  # type: ignore[arg-type]
                    account_id=account_id,
                    nickname=nickname,
                    clan_tag=clan_tag,
                    ship_id=ship_id,
                    ship_tier=tier,
                    battle_type=mode,
                    battle_time=battle_time,
                    battles_delta=b_delta,
                    wins_delta=wins_d,
                    survived_delta=surv_d,
                    damage_delta=dmg_d,
                    frags_delta=frags_d,
                    xp_delta=xp_d,
                    spotting_delta=spot_d,
                    potential_delta=pot_d,
                    planes_delta=planes_d,
                    main_hits_delta=hits_d,
                    main_shots_delta=shots_d,
                    pr_value=pr_val,
                )
            )
    return records


def summarize_mode_across_ships(
    ships_stats: list[dict[str, Any]],
    mode: str,
    expected_map: dict[int, ExpectedShipValues],
) -> dict[str, Any]:
    battles = wins = survived = damage = frags = xp = spotting = potential = planes = hits = shots = 0
    pr_rows: list[tuple[int, int, float, float, float]] = []

    for entry in ships_stats:
        sid = int(entry.get("ship_id") or 0)
        b = extract_mode_bucket(entry, mode)
        if b["battles"] <= 0:
            continue
        battles += b["battles"]
        wins += b["wins"]
        survived += b["survived_battles"]
        damage += b["damage_dealt"]
        frags += b["frags"]
        xp += b["xp"]
        spotting += b["damage_scouting"]
        potential += b["art_agro"] + b["torpedo_agro"]
        planes += b["planes_killed"]
        hits += b["main_hits"]
        shots += b["main_shots"]
        pr_rows.append((sid, b["battles"], b["damage_dealt"], b["frags"], b["wins"]))

    pr_val = calculate_pr_aggregate(pr_rows, expected_map) if battles > 0 else 0.0
    pr_info = classify_pr(pr_val)
    deaths = max(1, battles - survived)
    wr = (wins / battles * 100.0) if battles > 0 else 0.0
    return {
        "mode": mode,
        "battles": battles,
        "wins": wins,
        "win_rate": round(wr, 2),
        "win_rate_color": win_rate_color(wr),
        "survival_rate": round(survived / battles * 100.0, 1) if battles > 0 else 0.0,
        "avg_damage": int(round(damage / battles)) if battles > 0 else 0,
        "avg_frags": round(frags / battles, 2) if battles > 0 else 0.0,
        "kd": round(frags / deaths, 2) if battles > 0 else 0.0,
        "avg_xp": int(round(xp / battles)) if battles > 0 else 0,
        "avg_spotting": int(round(spotting / battles)) if battles > 0 else 0,
        "avg_potential": int(round(potential / battles)) if battles > 0 else 0,
        "avg_planes": round(planes / battles, 2) if battles > 0 else 0.0,
        "hit_ratio": round(hits / shots * 100.0, 1) if shots > 0 else 0.0,
        "pr": pr_info.value,
        "pr_tier_key": pr_info.tier_key,
        "pr_color": pr_info.color,
        "pr_next": pr_info.next_value,
    }


def build_player_overview(
    *,
    realm: str,
    account_info: dict[str, Any],
    ships_stats: list[dict[str, Any]],
    clan_info: dict[str, Any] | None,
    ships_meta: dict[int, ShipMeta],
    expected_map: dict[int, ExpectedShipValues],
    lang: str = "zh",
) -> dict[str, Any]:
    pvp = summarize_mode_across_ships(ships_stats, "pvp", expected_map)
    modes = {
        m: summarize_mode_across_ships(ships_stats, m, expected_map)
        for m in ("pvp_solo", "pvp_div2", "pvp_div3", "rank_solo")
    }

    # Ship class & tier breakdown for PVP
    by_type: dict[str, dict[str, Any]] = {}
    by_tier: dict[int, int] = {t: 0 for t in range(1, 12)}
    top_ships: list[dict[str, Any]] = []

    for entry in ships_stats:
        sid = int(entry.get("ship_id") or 0)
        b = extract_mode_bucket(entry, "pvp")
        if b["battles"] <= 0:
            continue
        meta = ships_meta.get(sid)
        stype = meta.ship_type if meta else "Unknown"
        tier = meta.tier if meta else 0
        if 1 <= tier <= 11:
            by_tier[tier] += b["battles"]

        acc = by_type.setdefault(
            stype, {"battles": 0, "wins": 0, "damage": 0, "frags": 0, "pr_rows": []}
        )
        acc["battles"] += b["battles"]
        acc["wins"] += b["wins"]
        acc["damage"] += b["damage_dealt"]
        acc["frags"] += b["frags"]
        acc["pr_rows"].append((sid, b["battles"], b["damage_dealt"], b["frags"], b["wins"]))

        ship_pr = classify_pr(
            calculate_pr_single_ship(
                b["battles"], b["damage_dealt"], b["frags"], b["wins"], expected_map.get(sid)
            )
        )
        wr = b["wins"] / b["battles"] * 100.0
        top_ships.append(
            {
                "ship_id": sid,
                "name": meta.display_name(lang) if meta else f"#{sid}",
                "tier": tier,
                "ship_type": stype,
                "battles": b["battles"],
                "win_rate": round(wr, 2),
                "win_rate_color": win_rate_color(wr),
                "avg_damage": int(round(b["damage_dealt"] / b["battles"])),
                "avg_frags": round(b["frags"] / b["battles"], 2),
                "pr": ship_pr.value,
                "pr_color": ship_pr.color,
            }
        )

    class_summary = []
    for stype in ("Battleship", "Cruiser", "Destroyer", "AirCarrier", "Submarine"):
        if stype not in by_type:
            continue
        acc = by_type[stype]
        b_cnt = acc["battles"]
        wr = acc["wins"] / b_cnt * 100.0
        pr_info = classify_pr(calculate_pr_aggregate(acc["pr_rows"], expected_map))
        class_summary.append(
            {
                "ship_type": stype,
                "battles": b_cnt,
                "win_rate": round(wr, 2),
                "win_rate_color": win_rate_color(wr),
                "avg_damage": int(round(acc["damage"] / b_cnt)),
                "avg_frags": round(acc["frags"] / b_cnt, 2),
                "pr": pr_info.value,
                "pr_color": pr_info.color,
            }
        )

    top_ships.sort(key=lambda s: s["battles"], reverse=True)
    max_tier_battles = max(by_tier.values()) if by_tier else 1
    tier_bars = [
        {"tier": t, "battles": cnt, "pct": round(cnt / max(1, max_tier_battles) * 100.0, 1)}
        for t, cnt in by_tier.items()
    ]

    last_bt = int(account_info.get("last_battle_time") or 0)
    last_bt_str = (
        datetime.fromtimestamp(last_bt, tz=UTC).strftime("%Y-%m-%d %H:%M UTC")
        if last_bt > 0
        else "-"
    )

    return {
        "realm": realm,
        "account_id": int(account_info.get("account_id") or 0),
        "nickname": str(account_info.get("nickname") or ""),
        "clan_tag": str(clan_info.get("tag") or "") if clan_info else "",
        "clan_name": str(clan_info.get("name") or "") if clan_info else "",
        "last_battle_str": last_bt_str,
        "pvp": pvp,
        "modes": modes,
        "class_summary": class_summary,
        "tier_bars": tier_bars,
        "top_ships": top_ships[:8],
    }


def build_recent_stats_view(
    *,
    realm: str,
    account_id: int,
    nickname: str,
    clan_tag: str,
    days: int,
    since_date: str,
    current_snapshots: dict[int, dict[str, Any]],
    baseline_ships: dict[int, dict[str, Any]],
    ships_meta: dict[int, ShipMeta],
    expected_map: dict[int, ExpectedShipValues],
    lang: str = "zh",
) -> dict[str, Any] | None:
    """Compute recent delta across ships between baseline snapshot and current cumulative stats."""
    total_battles = total_wins = total_surv = total_dmg = total_frags = total_xp = 0
    pr_rows: list[tuple[int, int, float, float, float]] = []
    ship_rows: list[dict[str, Any]] = []

    for sid, cur_ship in current_snapshots.items():
        cur_pvp = cur_ship.get("pvp") or {}
        base_pvp = (baseline_ships.get(sid) or {}).get("pvp") or {}
        b_delta = int(cur_pvp.get("battles") or 0) - int(base_pvp.get("battles") or 0)
        if b_delta <= 0:
            continue
        w_delta = max(0, int(cur_pvp.get("wins") or 0) - int(base_pvp.get("wins") or 0))
        s_delta = max(0, int(cur_pvp.get("survived_battles") or 0) - int(base_pvp.get("survived_battles") or 0))
        d_delta = max(0, int(cur_pvp.get("damage_dealt") or 0) - int(base_pvp.get("damage_dealt") or 0))
        f_delta = max(0, int(cur_pvp.get("frags") or 0) - int(base_pvp.get("frags") or 0))
        x_delta = max(0, int(cur_pvp.get("xp") or 0) - int(base_pvp.get("xp") or 0))

        total_battles += b_delta
        total_wins += w_delta
        total_surv += s_delta
        total_dmg += d_delta
        total_frags += f_delta
        total_xp += x_delta
        pr_rows.append((sid, b_delta, d_delta, f_delta, w_delta))

        meta = ships_meta.get(sid)
        spr = classify_pr(calculate_pr_single_ship(b_delta, d_delta, f_delta, w_delta, expected_map.get(sid)))
        swr = w_delta / b_delta * 100.0
        ship_rows.append(
            {
                "ship_id": sid,
                "name": meta.display_name(lang) if meta else f"#{sid}",
                "tier": meta.tier if meta else 0,
                "ship_type": meta.ship_type if meta else "",
                "battles": b_delta,
                "win_rate": round(swr, 2),
                "win_rate_color": win_rate_color(swr),
                "avg_damage": int(round(d_delta / b_delta)),
                "avg_frags": round(f_delta / b_delta, 2),
                "avg_xp": int(round(x_delta / b_delta)),
                "pr": spr.value,
                "pr_color": spr.color,
            }
        )

    if total_battles <= 0:
        return None

    ship_rows.sort(key=lambda r: (r["battles"], r["avg_damage"]), reverse=True)
    pr_info = classify_pr(calculate_pr_aggregate(pr_rows, expected_map))
    wr = total_wins / total_battles * 100.0
    deaths = max(1, total_battles - total_surv)
    return {
        "realm": realm,
        "account_id": account_id,
        "nickname": nickname,
        "clan_tag": clan_tag,
        "days": days,
        "since_date": since_date,
        "summary": {
            "battles": total_battles,
            "wins": total_wins,
            "win_rate": round(wr, 2),
            "win_rate_color": win_rate_color(wr),
            "survival_rate": round(total_surv / total_battles * 100.0, 1),
            "avg_damage": int(round(total_dmg / total_battles)),
            "avg_frags": round(total_frags / total_battles, 2),
            "kd": round(total_frags / deaths, 2),
            "avg_xp": int(round(total_xp / total_battles)),
            "pr": pr_info.value,
            "pr_tier_key": pr_info.tier_key,
            "pr_color": pr_info.color,
            "pr_next": pr_info.next_value,
        },
        "ships": ship_rows[:12],
    }
