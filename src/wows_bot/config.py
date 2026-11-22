"""Application configuration loaded from environment variables / .env."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

Realm = Literal["na", "eu", "asia"]
Language = Literal["zh", "en"]

SUPPORTED_REALMS: tuple[Realm, ...] = ("na", "eu", "asia")
REALM_ALIASES: dict[str, Realm] = {
    "na": "na",
    "us": "na",
    "美服": "na",
    "北美": "na",
    "eu": "eu",
    "欧服": "eu",
    "欧洲": "eu",
    "asia": "asia",
    "sea": "asia",
    "亚服": "asia",
    "亚洲": "asia",
}

WG_API_BASES: dict[Realm, str] = {
    "na": "https://api.worldofwarships.com/wows",
    "eu": "https://api.worldofwarships.eu/wows",
    "asia": "https://api.worldofwarships.asia/wows",
}

WG_CLAN_PORTAL_BASES: dict[Realm, str] = {
    "na": "https://clans.worldofwarships.com/api",
    "eu": "https://clans.worldofwarships.eu/api",
    "asia": "https://clans.worldofwarships.asia/api",
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # GameVox (Discord API v10 compatible)
    gamevox_bot_token: str = ""
    gamevox_api_base: str = "https://app-api.gamevox.com/api/v10"
    gamevox_gateway_url: str = "wss://gateway.gamevox.com/?v=10&encoding=json"
    default_guild_id: str | None = None
    default_auto_post_channel_id: str | None = None

    # Wargaming Developer API (NA / EU / ASIA)
    wg_application_id: str = ""

    # Bot defaults
    command_prefix: str = "wws"
    default_language: Language = "zh"
    default_realm: Realm = "na"

    # Storage & Honker Queue (SQLite WAL)
    data_dir: Path = Field(default=Path("/data"))
    db_path: Path | None = None
    honker_watch_interval_ms: int = 25

    # Polling & Division Coalescing
    poll_interval_seconds: int = 60
    squad_recheck_delay_seconds: int = 12
    squad_finalize_delay_seconds: int = 28
    squad_time_tolerance_seconds: int = 20

    # Playwright Rendering
    render_concurrency: int = 2
    render_image_format: Literal["png", "jpeg", "webp"] = "png"
    render_timeout_seconds: float = 30.0

    # Optional tokenless public data sources
    enable_yuyuko_ship_rank: bool = True
    yuyuko_api_base: str = "https://v3-api.wows.shinoaki.com"
    wows_numbers_pr_url: str = "https://api.wows-numbers.com/personal/rating/expected/json/"

    log_level: str = "INFO"

    @property
    def resolved_db_path(self) -> Path:
        if self.db_path is not None:
            return self.db_path
        return self.data_dir / "wows_bot.db"

    @property
    def project_root(self) -> Path:
        # src/wows_bot/config.py -> repo root
        return Path(__file__).resolve().parents[2]

    @property
    def locales_dir(self) -> Path:
        return self.project_root / "locales"

    @property
    def templates_dir(self) -> Path:
        return self.project_root / "templates"


def normalize_realm(raw: str) -> Realm | None:
    return REALM_ALIASES.get(raw.strip().lower())
