"""Playwright Chromium HTML -> image renderer with bounded concurrency."""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape
from loguru import logger
from playwright.async_api import Browser, Playwright, async_playwright

from wows_bot.config import Settings
from wows_bot.db.repository import ShipMeta
from wows_bot.i18n import I18n
from wows_bot.services.pr import classify_pr, win_rate_color

ROMAN_TIERS: dict[int, str] = {
    1: "I",
    2: "II",
    3: "III",
    4: "IV",
    5: "V",
    6: "VI",
    7: "VII",
    8: "VIII",
    9: "IX",
    10: "X",
    11: "★",
}


class CardRenderer:
    def __init__(self, settings: Settings, i18n: I18n) -> None:
        self.settings = settings
        self.i18n = i18n
        self._sem = asyncio.Semaphore(max(1, settings.render_concurrency))
        self._pw: Playwright | None = None
        self._browser: Browser | None = None
        self._start_lock = asyncio.Lock()

        self._jinja = Environment(
            loader=FileSystemLoader(str(settings.templates_dir)),
            autoescape=select_autoescape(["html", "xml"]),
        )
        self._jinja.filters["intcomma"] = lambda v: f"{int(round(float(v or 0))):,}"
        self._jinja.filters["roman"] = lambda t: ROMAN_TIERS.get(int(t or 0), str(t or ""))

    async def start(self) -> None:
        async with self._start_lock:
            if self._browser is not None:
                return
            self._pw = await async_playwright().start()
            self._browser = await self._pw.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                ],
            )
            logger.info("Playwright Chromium renderer started (concurrency={})", self.settings.render_concurrency)

    async def close(self) -> None:
        if self._browser is not None:
            await self._browser.close()
            self._browser = None
        if self._pw is not None:
            await self._pw.stop()
            self._pw = None

    def render_html(self, template_name: str, context: dict[str, Any], lang: str = "zh") -> str:
        base_css = (self.settings.templates_dir / "base.css").read_text(encoding="utf-8")
        tpl = self._jinja.get_template(template_name)
        return tpl.render(
            **context,
            base_css=base_css,
            lang=lang,
            cat=self.i18n.catalog(lang),
            t=lambda key, **kw: self.i18n.t(key, lang=lang, **kw),
        )

    async def render_image(
        self,
        template_name: str,
        context: dict[str, Any],
        *,
        lang: str = "zh",
        width: int = 1080,
    ) -> bytes:
        html = self.render_html(template_name, context, lang=lang)
        if self._browser is None:
            await self.start()

        async with self._sem:
            assert self._browser is not None
            ctx = await self._browser.new_context(
                viewport={"width": width, "height": 600},
                device_scale_factor=2,
            )
            try:
                page = await ctx.new_page()
                await page.set_content(
                    html,
                    wait_until="domcontentloaded",
                    timeout=int(self.settings.render_timeout_seconds * 1000),
                )
                # Give external ship icons up to 2.5s to finish loading, without blocking forever
                with contextlib.suppress(Exception):
                    await page.wait_for_load_state("networkidle", timeout=2500)
                card = page.locator(".card-root")
                img_type = "jpeg" if self.settings.render_image_format == "jpeg" else "png"
                kwargs: dict[str, Any] = {"type": img_type}
                if img_type == "jpeg":
                    kwargs["quality"] = 92
                if await card.count() > 0:
                    return await card.first.screenshot(**kwargs)
                return await page.screenshot(full_page=True, **kwargs)
            finally:
                await ctx.close()

    async def render_match_group(
        self,
        group: dict[str, Any],
        ships_meta: dict[int, ShipMeta],
        *,
        lang: str = "zh",
    ) -> bytes:
        """Render either a solo match card or a combined 2/3-player division match card."""
        enriched_records = []
        total_damage = 0
        total_frags = 0
        total_xp = 0
        pr_vals: list[float] = []

        for rec in group["records"]:
            meta = ships_meta.get(rec["ship_id"])
            b_cnt = max(1, int(rec["battles_delta"]))
            pr_info = classify_pr(rec["pr_value"])
            hit_pct = (
                round(rec["main_hits_delta"] / rec["main_shots_delta"] * 100.0, 1)
                if rec["main_shots_delta"] > 0
                else 0.0
            )
            total_damage += int(rec["damage_delta"])
            total_frags += int(rec["frags_delta"])
            total_xp += int(rec["xp_delta"])
            if rec["pr_value"] > 0:
                pr_vals.append(float(rec["pr_value"]))

            enriched_records.append(
                {
                    **rec,
                    "ship_name": meta.display_name(lang) if meta else f"#{rec['ship_id']}",
                    "ship_type": meta.ship_type if meta else "",
                    "ship_icon": meta.icon_medium if meta else "",
                    "avg_damage": int(round(rec["damage_delta"] / b_cnt)),
                    "avg_xp": int(round(rec["xp_delta"] / b_cnt)),
                    "hit_ratio": hit_pct,
                    "survived": rec["survived_delta"] > 0,
                    "pr_int": pr_info.value,
                    "pr_tier_key": pr_info.tier_key,
                    "pr_color": pr_info.color,
                }
            )

        avg_pr = sum(pr_vals) / len(pr_vals) if pr_vals else 0.0
        avg_pr_info = classify_pr(avg_pr)
        bt_str = datetime.fromtimestamp(group["battle_time"], tz=UTC).strftime(
            "%Y-%m-%d %H:%M UTC"
        )
        ctx = {
            "group": group,
            "battle_time_str": bt_str,
            "records": enriched_records,
            "division_totals": {
                "damage": total_damage,
                "frags": total_frags,
                "avg_xp": int(round(total_xp / max(1, len(enriched_records)))),
                "avg_pr": avg_pr_info.value,
                "avg_pr_tier_key": avg_pr_info.tier_key,
                "avg_pr_color": avg_pr_info.color,
            },
        }
        if len(enriched_records) > 1:
            return await self.render_image("match_division.html", ctx, lang=lang, width=1160)
        return await self.render_image("match_solo.html", ctx, lang=lang, width=960)

    async def render_player_overview(self, view: dict[str, Any], *, lang: str = "zh") -> bytes:
        return await self.render_image("player_overview.html", {"view": view}, lang=lang, width=1120)

    async def render_ship_stats(self, view: dict[str, Any], *, lang: str = "zh") -> bytes:
        return await self.render_image("ship_stats.html", {"view": view}, lang=lang, width=1040)

    async def render_recent_summary(self, view: dict[str, Any], *, lang: str = "zh") -> bytes:
        return await self.render_image("recent_summary.html", {"view": view}, lang=lang, width=1120)

    async def render_ship_rank(
        self,
        *,
        realm: str,
        ship: ShipMeta,
        entries: list[dict[str, Any]],
        is_global: bool,
        lang: str = "zh",
    ) -> bytes:
        decorated = []
        for idx, e in enumerate(entries, start=1):
            pr_info = classify_pr(e.get("pr", 0))
            decorated.append(
                {
                    **e,
                    "rank": idx,
                    "win_rate_color": win_rate_color(float(e.get("win_rate", 0))),
                    "pr_color": pr_info.color,
                }
            )
        return await self.render_image(
            "ship_rank.html",
            {
                "realm": realm,
                "ship": ship,
                "ship_name": ship.display_name(lang),
                "entries": decorated,
                "is_global": is_global,
            },
            lang=lang,
            width=1080,
        )

    async def render_clan_info(self, view: dict[str, Any], *, lang: str = "zh") -> bytes:
        return await self.render_image("clan_info.html", {"view": view}, lang=lang, width=1040)
