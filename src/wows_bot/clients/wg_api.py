"""Official Wargaming Developer API & Clan Portal API client (NA / EU / ASIA)."""

from __future__ import annotations

from typing import Any

import httpx
from loguru import logger

from wows_bot.config import WG_API_BASES, WG_CLAN_PORTAL_BASES, Realm
from wows_bot.db.repository import ShipMeta

SHIP_EXTRA_FIELDS = "pvp_solo,pvp_div2,pvp_div3,rank_solo"


class WGApiError(RuntimeError):
    """Raised when Wargaming API returns a non-ok status or HTTP error."""


class WGApiClient:
    def __init__(self, application_id: str, timeout: float = 15.0) -> None:
        self.application_id = application_id.strip()
        self._client = httpx.AsyncClient(timeout=timeout, http2=False)

    async def aclose(self) -> None:
        await self._client.aclose()

    def _ensure_key(self) -> None:
        if not self.application_id:
            raise WGApiError("WG_APPLICATION_ID is not configured")

    async def _get(self, realm: Realm, path: str, params: dict[str, Any] | None = None) -> Any:
        self._ensure_key()
        query = {"application_id": self.application_id}
        if params:
            query.update({k: v for k, v in params.items() if v is not None})
        url = f"{WG_API_BASES[realm]}{path}"
        resp = await self._client.get(url, params=query)
        resp.raise_for_status()
        payload = resp.json()
        if payload.get("status") != "ok":
            err = payload.get("error") or {}
            raise WGApiError(f"WG API error ({err.get('code')}): {err.get('message')}")
        return payload.get("data")

    async def search_account(self, realm: Realm, nickname: str, limit: int = 5) -> list[dict[str, Any]]:
        data = await self._get(
            realm,
            "/account/list/",
            {"search": nickname, "limit": limit},
        )
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
        resp = await self._client.get(url, params=params)
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, list) else []

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
                    icon_small=str(images.get("small") or ""),
                    icon_medium=str(images.get("medium") or ""),
                    icon_large=str(images.get("large") or ""),
                )
            )
        logger.info("Fetched {} ships from WG encyclopedia (en + zh-cn)", len(ships))
        return ships

    async def _fetch_encyclopedia_lang(self, realm: Realm, language: str) -> dict[int, dict[str, Any]]:
        out: dict[int, dict[str, Any]] = {}
        page = 1
        while True:
            self._ensure_key()
            url = f"{WG_API_BASES[realm]}/encyclopedia/ships/"
            resp = await self._client.get(
                url,
                params={
                    "application_id": self.application_id,
                    "language": language,
                    "limit": 100,
                    "page_no": page,
                    "fields": "ship_id,name,tier,type,nation,is_premium,is_special,images",
                },
            )
            resp.raise_for_status()
            payload = resp.json()
            if payload.get("status") != "ok":
                break
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
