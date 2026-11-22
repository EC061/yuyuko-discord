"""Fetch canonical WoWS Numbers PR expected ship values (with local fallback)."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
from loguru import logger

from wows_bot.services.pr import ExpectedShipValues


class PRSourceClient:
    def __init__(self, url: str, fallback_file: Path | None = None, timeout: float = 20.0) -> None:
        self.url = url
        self.fallback_file = fallback_file
        self._client = httpx.AsyncClient(timeout=timeout)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def fetch_expected_values(self) -> dict[int, ExpectedShipValues]:
        try:
            resp = await self._client.get(self.url)
            resp.raise_for_status()
            raw = resp.json()
            parsed = self._parse_payload(raw)
            if parsed:
                logger.info("Synced {} ship PR expected values from {}", len(parsed), self.url)
                return parsed
        except Exception as exc:
            logger.warning("Failed to fetch PR expected values from {}: {}", self.url, exc)

        if self.fallback_file and self.fallback_file.exists():
            raw = json.loads(self.fallback_file.read_text(encoding="utf-8"))
            parsed = self._parse_payload(raw)
            logger.info("Loaded {} ship PR expected values from fallback {}", len(parsed), self.fallback_file)
            return parsed
        return {}

    @staticmethod
    def _parse_payload(payload: dict) -> dict[int, ExpectedShipValues]:
        data = payload.get("data") if isinstance(payload, dict) else {}
        if not isinstance(data, dict):
            return {}
        result: dict[int, ExpectedShipValues] = {}
        for sid_str, val in data.items():
            if not isinstance(val, dict):
                continue
            avg_dmg = float(val.get("average_damage_dealt") or 0.0)
            avg_frags = float(val.get("average_frags") or 0.0)
            win_rate = float(val.get("win_rate") or 0.0)
            if avg_dmg > 0 and win_rate > 0:
                result[int(sid_str)] = ExpectedShipValues(
                    ship_id=int(sid_str),
                    average_damage=avg_dmg,
                    average_frags=avg_frags,
                    win_rate=win_rate,
                )
        return result
