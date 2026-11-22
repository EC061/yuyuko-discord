"""Verify HTML templates and zh/en i18n rendering."""

from __future__ import annotations

from wows_bot.config import Settings
from wows_bot.db.repository import ShipMeta
from wows_bot.i18n import I18n
from wows_bot.services.renderer import CardRenderer


def test_all_card_templates_render_zh_and_en() -> None:
    settings = Settings()
    i18n = I18n(settings.locales_dir, default_lang="zh")
    renderer = CardRenderer(settings, i18n)

    division_ctx = {
        "group": {"id": 42, "realm": "na", "battle_type": "pvp_div2", "is_win": True},
        "battle_time_str": "2026-11-22 14:00 UTC",
        "records": [
            {
                "nickname": "Edward",
                "clan_tag": "ESX",
                "ship_name": "大和",
                "ship_tier": 10,
                "ship_type": "Battleship",
                "ship_icon": "",
                "battles_delta": 1,
                "damage_delta": 185000,
                "frags_delta": 3,
                "xp_delta": 2450,
                "spotting_delta": 42000,
                "planes_delta": 6,
                "hit_ratio": 34.2,
                "pr_int": 2650,
                "pr_tier_key": "super_unicum",
                "pr_color": "#a00dc5",
            },
            {
                "nickname": "Wingman",
                "clan_tag": "ESX",
                "ship_name": "哈兰",
                "ship_tier": 10,
                "ship_type": "Destroyer",
                "ship_icon": "",
                "battles_delta": 1,
                "damage_delta": 98000,
                "frags_delta": 2,
                "xp_delta": 2300,
                "spotting_delta": 88000,
                "planes_delta": 24,
                "hit_ratio": 41.0,
                "pr_int": 2200,
                "pr_tier_key": "unicum",
                "pr_color": "#d042f3",
            },
        ],
        "division_totals": {
            "damage": 283000,
            "frags": 5,
            "avg_xp": 2375,
            "avg_pr": 2425,
            "avg_pr_tier_key": "unicum",
            "avg_pr_color": "#d042f3",
        },
    }

    zh_div = renderer.render_html("match_division.html", division_ctx, lang="zh")
    en_div = renderer.render_html("match_division.html", division_ctx, lang="en")
    assert "车队战绩结算" in zh_div
    assert "283,000" in zh_div
    assert "Division Battle Report" in en_div
    assert "VICTORY" in en_div

    zh_solo = renderer.render_html("match_solo.html", division_ctx, lang="zh")
    assert "单局战绩结算" in zh_solo

    ship = ShipMeta(4276041424, "Yamato", "大和", 10, "Battleship", "japan", False, False, "", "", "")
    rank_html = renderer.render_html(
        "ship_rank.html",
        {
            "realm": "na",
            "ship": ship,
            "ship_name": "大和",
            "entries": [
                {"rank": 1, "nickname": "TopPlayer", "clan_tag": "NYARU", "battles": 300, "win_rate": 68.5, "win_rate_color": "#a00dc5", "avg_damage": 152000, "avg_frags": 1.6, "pr": 2850, "pr_color": "#a00dc5"}
            ],
            "is_global": True,
        },
        lang="zh",
    )
    assert "全服榜单" in rank_html
    assert "NYARU" in rank_html
