"""Official WoWS Numbers Personal Rating (PR) calculation and color helpers."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ExpectedShipValues:
    ship_id: int
    average_damage: float
    average_frags: float
    win_rate: float


@dataclass(frozen=True, slots=True)
class PRRating:
    value: int
    tier_key: str
    color: str
    next_value: int


PR_THRESHOLDS: tuple[tuple[int, str, str], ...] = (
    (2450, "super_unicum", "#8b0dc5"),
    (2100, "unicum", "#b832e6"),
    (1750, "great", "#00a699"),
    (1550, "very_good", "#2b7a0b"),
    (1350, "good", "#42a800"),
    (1100, "average", "#d48b00"),
    (750, "below_avg", "#ea580c"),
    (1, "bad", "#dc2626"),
    (0, "unrated", "#64748b"),
)


def classify_pr(pr_value: float) -> PRRating:
    val = max(0, int(round(pr_value)))
    if val <= 0:
        return PRRating(value=0, tier_key="unrated", color="#8c96a6", next_value=750)
    for idx, (threshold, key, color) in enumerate(PR_THRESHOLDS):
        if val >= threshold:
            next_val = PR_THRESHOLDS[idx - 1][0] - val if idx > 0 else 0
            return PRRating(value=val, tier_key=key, color=color, next_value=max(0, next_val))
    return PRRating(value=val, tier_key="bad", color="#fe0e00", next_value=max(0, 750 - val))


def win_rate_color(win_rate_pct: float) -> str:
    if win_rate_pct >= 65:
        return "#8b0dc5"
    if win_rate_pct >= 60:
        return "#b832e6"
    if win_rate_pct >= 56:
        return "#00a699"
    if win_rate_pct >= 54:
        return "#2b7a0b"
    if win_rate_pct >= 52:
        return "#42a800"
    if win_rate_pct >= 49:
        return "#d48b00"
    if win_rate_pct >= 47:
        return "#ea580c"
    return "#dc2626"


def calculate_pr_single_ship(
    battles: int,
    damage: float,
    frags: float,
    wins: float,
    expected: ExpectedShipValues | None,
) -> float:
    """Calculate PR for one ship (works for single game or cumulative totals)."""
    if battles <= 0 or expected is None:
        return 0.0
    exp_dmg = expected.average_damage * battles
    exp_frags = expected.average_frags * battles
    exp_wins = (expected.win_rate / 100.0) * battles
    return _pr_from_totals(damage, frags, wins, exp_dmg, exp_frags, exp_wins)


def calculate_pr_aggregate(
    ship_rows: Iterable[tuple[int, int, float, float, float]],
    expected_map: dict[int, ExpectedShipValues],
) -> float:
    """Calculate aggregate PR across multiple ships.

    Each item in ``ship_rows`` is ``(ship_id, battles, total_damage, total_frags, total_wins)``.
    """
    act_dmg = 0.0
    act_frags = 0.0
    act_wins = 0.0
    exp_dmg = 0.0
    exp_frags = 0.0
    exp_wins = 0.0

    for ship_id, battles, dmg, frags, wins in ship_rows:
        if battles <= 0:
            continue
        exp = expected_map.get(ship_id)
        if exp is None or exp.average_damage <= 0:
            continue
        act_dmg += dmg
        act_frags += frags
        act_wins += wins
        exp_dmg += exp.average_damage * battles
        exp_frags += exp.average_frags * battles
        exp_wins += (exp.win_rate / 100.0) * battles

    return _pr_from_totals(act_dmg, act_frags, act_wins, exp_dmg, exp_frags, exp_wins)


def _pr_from_totals(
    act_dmg: float,
    act_frags: float,
    act_wins: float,
    exp_dmg: float,
    exp_frags: float,
    exp_wins: float,
) -> float:
    if exp_dmg <= 0 or exp_frags <= 0 or exp_wins <= 0:
        return 0.0

    r_dmg = act_dmg / exp_dmg
    r_frags = act_frags / exp_frags
    r_wins = act_wins / exp_wins

    n_dmg = max(0.0, (r_dmg - 0.4) / (1.0 - 0.4))
    n_frags = max(0.0, (r_frags - 0.1) / (1.0 - 0.1))
    n_wins = max(0.0, (r_wins - 0.7) / (1.0 - 0.7))

    return 700.0 * n_dmg + 300.0 * n_frags + 150.0 * n_wins
