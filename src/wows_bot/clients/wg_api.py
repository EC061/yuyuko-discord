"""Official Wargaming Developer API & Clan Portal API client (NA / EU / ASIA)."""

from __future__ import annotations

import asyncio
import time
from typing import Any
from urllib.parse import urlparse

import httpx
from loguru import logger

from wows_bot.config import (
    ALLOWED_WG_ICON_SUFFIXES,
    WG_API_BASES,
    WG_CLAN_PORTAL_BASES,
    Realm,
)
from wows_bot.db.repository import ShipMeta

SHIP_EXTRA_FIELDS = "pvp_solo,pvp_div2,pvp_div3,rank_solo"


class WGApiError(RuntimeError):
    """Sanitized exception raised on WG API errors (never includes application_id)."""


def sanitize_wg_icon_url(raw_url: str | None) -> str:
    """Allow only HTTPS URLs hosted on official Wargaming CDN domains."""
    if not raw_url or not isinstance(raw_url, str):
        return ""
    parsed = urlparse(raw_url.strip())
    if parsed.scheme != "https" or not parsed.hostname:
        return ""
    host = parsed.hostname.lower()
    if any(host.endswith(suffix) for suffix in ALLOWED_WG_ICON_SUFFIXES):
        return raw_url.strip()
    return ""


class _AsyncRateLimiter:
    """Simple async token-bucket rate limiter (default 8 req/s to stay under WG's 10 req/s)."""

    def __init__(self, rate_per_sec: float = 8.0) -> None:
        self._interval = 1.0 / max(1.0, rate_per_sec)
        self._lock = asyncio.Lock()
        self._next_allowed = 0.0

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            if now < self._next_allowed:
                await asyncio.sleep(self._next_allowed - now)
                now = time.monotonic()
            self._next_allowed = now + self._interval


class WGApiClient:
    def __init__(self, application_id: str, timeout: float = 15.0, rate_per_sec: float = 8.0) -> None:
        self.application_id = application_id.strip()
        self._client = httpx.AsyncClient(timeout=timeout, http2=False)
        self._limiter = _AsyncRateLimiter(rate_per_sec=rate_per_sec)

    async def aclose(self) -> None:
        await self._client.aclose()

    def _ensure_key(self) -> None:
        if not self.application_id:
            raise WGApiError("WG_APPLICATION_ID is not configured")

    async def _request_raw(
        self,
        realm: Realm,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        max_retries: int = 3,
    ) -> dict[str, Any]:
        """Issue a rate-limited WG API GET with retries; never leaks application_id in errors."""
        self._ensure_key()
        query = {"application_id": self.application_id}
        if params:
            query.update({k: v for k, v in params.items() if v is not None})
        url = f"{WG_API_BASES[realm]}{path}"

        last_err: WGApiError | None = None
        for attempt in range(max_retries):
            await self._limiter.acquire()
            try:
                resp = await self._client.get(url, params=query)
            except httpx.TimeoutException as exc:
                last_err = WGApiError(f"WG API timeout on {realm}:{path}")
                if attempt + 1 < max_retries:
                    await asyncio.sleep(0.5 * (2**attempt))
                    continue
                raise last_err from exc
            except httpx.HTTPError as exc:
                # Never include exc's raw message (may contain request URL with application_id)
                raise WGApiError(f"WG API network error ({type(exc).__name__}) on {realm}:{path}") from None

            if resp.status_code in (429, 500, 502, 503, 504):
                last_err = WGApiError(f"WG API HTTP {resp.status_code} on {realm}:{path}")
                if attempt + 1 < max_retries:
                    await asyncio.sleep(0.75 * (2**attempt))
                    continue
                raise last_err

            if resp.status_code >= 400:
                raise WGApiError(f"WG API HTTP {resp.status_code} on {realm}:{path}")

            try:
                payload = resp.json()
            except ValueError as exc:
                raise WGApiError(f"WG API invalid JSON on {realm}:{path}") from exc

            if payload.get("status") != "ok":
                err = payload.get("error") or {}
                code = err.get("code")
                msg = str(err.get("message") or "unknown")
                if code == 407 and attempt + 1 < max_retries:
                    await asyncio.sleep(1.0 * (2**attempt))
                    continue
                raise WGApiError(f"WG API error ({code}) on {realm}:{path}: {msg}")

            return payload

        raise last_err or WGApiError(f"WG API failed on {realm}:{path}")

    async def _get(self, realm: Realm, path: str, params: dict[str, Any] | None = None) -> Any:
        payload = await self._request_raw(realm, path, params)
        return payload.get("data")

    async def search_account(self, realm: Realm, nickname: str, limit: int = 5) -> list[dict[str, Any]]:
        data = await self._get(realm, "/account/list/", {"search": nickname, "limit": limit})
        return data or []

    async def get_accounts_info_batch(
        self,
        realm: Realm,
        account_ids: list[int],
        *,
        fields: str | None = None,
    ) -> dict[int, dict[str, Any]]:
        """Fetch up to 100 accounts per HTTP call (chunks automatically)."""
        result: dict[int, dict[str, Any]] = {}
        for i in range(0, len(account_ids), 100):
            chunk = account_ids[i : i + 100]
            data = await self._get(
                realm,
                "/account/info/",
                {
                    "account_id": ",".join(str(aid) for aid in chunk),
                    "fields": fields,
                },
            )
            if isinstance(data, dict):
                for aid_str, info in data.items():
                    if info is not None:
                        result[int(aid_str)] = info
        return result

    async def get_account_ships_stats(
        self,
        realm: Realm,
        account_id: int,
        *,
        ship_id: int | None = None,
        extra: str = SHIP_EXTRA_FIELDS,
    ) -> list[dict[str, Any]]:
        data = await self._get(
            realm,
            "/ships/stats/",
            {
                "account_id": account_id,
                "ship_id": ship_id,
                "extra": extra,
            },
        )
        if not isinstance(data, dict):
            return []
        return data.get(str(account_id)) or []

    async def get_account_clan(self, realm: Realm, account_id: int) -> dict[str, Any] | None:
        data = await self._get(
            realm,
            "/clans/accountinfo/",
            {"account_id": account_id, "extra": "clan"},
        )
        if not isinstance(data, dict):
            return None
        entry = data.get(str(account_id))
        if not entry:
            return None
        return entry.get("clan")

    async def search_clan(self, realm: Realm, tag: str, limit: int = 5) -> list[dict[str, Any]]:
        data = await self._get(realm, "/clans/list/", {"search": tag, "limit": limit})
        return data or []

    async def get_clan_info(self, realm: Realm, clan_id: int) -> dict[str, Any] | None:
        data = await self._get(
            realm,
            "/clans/info/",
            {"clan_id": clan_id, "extra": "members"},
        )
        if not isinstance(data, dict):
            return None
        return data.get(str(clan_id))

    async def get_clan_wars_ladder(
        self,
        realm: Realm,
        *,
        season: int | None = None,
        league: int = 0,
        division: int = 1,
    ) -> list[dict[str, Any]]:
        """Query WG's official Clan Portal ladder API (no API key needed)."""
        url = f"{WG_CLAN_PORTAL_BASES[realm]}/ladder/structure/"
        params: dict[str, Any] = {"realm": realm, "league": league, "division": division}
        if season is not None:
            params["season"] = season
        try:
            resp = await self._client.get(url, params=params)
            if resp.status_code >= 400:
                raise WGApiError(f"WG Clan Portal HTTP {resp.status_code}")
            data = resp.json()
            return data if isinstance(data, list) else []
        except httpx.HTTPError as exc:
            raise WGApiError(f"WG Clan Portal network error ({type(exc).__name__})") from None

    async def fetch_encyclopedia_ships(self, realm: Realm = "na") -> list[ShipMeta]:
        """Fetch full WG ship encyclopedia in both English and Simplified Chinese."""
        en_map = await self._fetch_encyclopedia_lang(realm, "en")
        zh_map = await self._fetch_encyclopedia_lang(realm, "zh-cn")

        ships: list[ShipMeta] = []
        for sid, en in en_map.items():
            zh = zh_map.get(sid) or {}
            images = en.get("images") or {}
            ships.append(
                ShipMeta(
                    ship_id=sid,
                    name_en=str(en.get("name") or f"Ship-{sid}"),
                    name_zh=str(zh.get("name") or en.get("name") or f"Ship-{sid}"),
                    tier=int(en.get("tier") or 0),
                    ship_type=str(en.get("type") or ""),
                    nation=str(en.get("nation") or ""),
                    is_premium=bool(en.get("is_premium")),
                    is_special=bool(en.get("is_special")),
                    icon_small=sanitize_wg_icon_url(images.get("small")),
                    icon_medium=sanitize_wg_icon_url(images.get("medium")),
                    icon_large=sanitize_wg_icon_url(images.get("large")),
                )
            )
        logger.info("Fetched {} ships from WG encyclopedia (en + zh-cn)", len(ships))
        return ships

    async def _fetch_encyclopedia_lang(self, realm: Realm, language: str) -> dict[int, dict[str, Any]]:
        out: dict[int, dict[str, Any]] = {}
        page = 1
        while True:
            payload = await self._request_raw(
                realm,
                "/encyclopedia/ships/",
                {
                    "language": language,
                    "limit": 100,
                    "page_no": page,
                    "fields": "ship_id,name,tier,type,nation,is_premium,is_special,images",
                },
            )
            data = payload.get("data") or {}
            for sid_str, info in data.items():
                if info:
                    out[int(sid_str)] = info
            meta = payload.get("meta") or {}
            page_total = int(meta.get("page_total") or 1)
            if page >= page_total:
                break
            page += 1
        return out
