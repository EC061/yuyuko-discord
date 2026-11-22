"""Optional tokenless client for Yuyuko's public global ship leaderboard endpoint."""

from __future__ import annotations

from typing import Any

import httpx
from loguru import logger

from wows_bot.config import Realm


class YuyukoRankClient:
    """Queries `/public/rank/yuyuko_ship/{realm}` without requiring any Yuyuko API token.

    Returns None on any error so callers fall back cleanly to the local SQLite leaderboard.
    """

    def __init__(self, base_url: str, enabled: bool = True, timeout: float = 12.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.enabled = enabled
        self._client = httpx.AsyncClient(timeout=timeout)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def fetch_ship_rank(
        self,
        realm: Realm,
        ship_id: int,
        page: int = 1,
        limit: int = 20,
    ) -> list[dict[str, Any]] | None:
        if not self.enabled:
            return None
        url = f"{self.base_url}/public/rank/yuyuko_ship/{realm}"
        try:
            resp = await self._client.get(url, params={"shipId": ship_id, "page": page})
            resp.raise_for_status()
            payload = resp.json()
            if payload.get("code") != 200:
                return None
            data = payload.get("data") or {}
            items = data.get("value") if isinstance(data, dict) else data
            if not isinstance(items, list):
                return None
            parsed: list[dict[str, Any]] = []
            for item in items[:limit]:
                user_info = item.get("userInfo") or {}
                clan_info = user_info.get("clanInfo") or {}
                battle_info = item.get("battleInfo") or {}
                avg_info = battle_info.get("avgInfo") or {}
                pr_info = item.get("prInfo") or {}
                b_count = (battle_info.get("battleInfo") or {}).get("battle") or item.get("battles") or 0
                parsed.append(
                    {
                        "account_id": int(user_info.get("accountId") or 0),
                        "nickname": str(user_info.get("userName") or item.get("userName") or "Unknown"),
                        "clan_tag": str(clan_info.get("tag") or ""),
                        "battles": int(b_count),
                        "win_rate": round(float(avg_info.get("win") or item.get("winRate") or 0.0), 2),
                        "avg_damage": round(float(avg_info.get("damage") or item.get("avgDamage") or 0.0), 1),
                        "avg_frags": round(float(avg_info.get("frags") or item.get("avgFrags") or 0.0), 2),
                        "avg_xp": round(float(avg_info.get("xp") or item.get("avgXp") or 0.0), 1),
                        "pr": int(pr_info.get("value") or item.get("pr") or 0),
                    }
                )
            return parsed
        except Exception as exc:
            logger.warning("Yuyuko public ship.rank unavailable ({}): {}", url, exc)
            return None
