"""Application entrypoint for wows-gamevox-bot."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import signal
import sqlite3
import sys
import time

from loguru import logger

from wows_bot.bot.gamevox import WoWSGameVoxBot, patch_discord_for_gamevox
from wows_bot.clients.pr_source import PRSourceClient
from wows_bot.clients.wg_api import WGApiClient
from wows_bot.clients.yuyuko_rank import YuyukoRankClient
from wows_bot.config import Settings
from wows_bot.db.repository import Repository
from wows_bot.db.schema import DatabaseManager
from wows_bot.i18n import I18n
from wows_bot.services.poller import PollerService
from wows_bot.services.renderer import CardRenderer
from wows_bot.services.worker import WorkerRunner


class _InterceptHandler(logging.Handler):
    """Forward stdlib logging (discord.py) to Loguru without leaking secrets."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            level: str | int = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno
        logger.opt(depth=6, exception=record.exc_info).log(level, record.getMessage())


def _configure_logging(level: str) -> None:
    logger.remove()
    # diagnose=False prevents local variable values (e.g. tokens/keys) from appearing in tracebacks!
    logger.add(sys.stderr, level=level, diagnose=False, backtrace=False)
    logging.basicConfig(handlers=[_InterceptHandler()], level=logging.WARNING, force=True)
    logging.getLogger("discord").setLevel(logging.INFO)
    # Never log httpx request URLs (which contain WG application_id query params)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def _run_healthcheck(settings: Settings) -> int:
    """Lightweight read-only SQLite healthcheck (never runs DDL or acquires write locks)."""
    db_path = settings.resolved_db_path
    if not db_path.exists():
        return 1
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=3.0)
        try:
            mode = conn.execute("PRAGMA journal_mode").fetchone()
            if not mode or str(mode[0]).lower() != "wal":
                return 1
            row = conn.execute("SELECT updated_at FROM metadata WHERE key = 'heartbeat'").fetchone()
            if row is not None and (time.time() - int(row[0])) > 180:
                return 1
        finally:
            conn.close()
        return 0
    except sqlite3.Error:
        return 1


async def run_app(settings: Settings) -> None:
    _configure_logging(settings.log_level)

    if not settings.gamevox_bot_token:
        raise RuntimeError("GAMEVOX_BOT_TOKEN is required to start the GameVox bot")

    patch_discord_for_gamevox(settings)

    dbm = DatabaseManager(settings.resolved_db_path, settings.honker_watch_interval_ms).open()
    repo = Repository(dbm)
    i18n = I18n(settings.locales_dir, default_lang=settings.default_language)
    wg = WGApiClient(settings.wg_application_id)
    pr_client = PRSourceClient(
        settings.wows_numbers_pr_url,
        fallback_file=settings.templates_dir / "pr_expected_fallback.json",
    )
    yuyuko_rank = YuyukoRankClient(
        settings.yuyuko_api_base,
        enabled=settings.enable_yuyuko_ship_rank,
    )
    renderer = CardRenderer(settings, i18n)
    poller = PollerService(settings, repo, wg, pr_client)

    worker_holder: dict[str, WorkerRunner] = {}

    async def on_bot_ready() -> None:
        await renderer.start()
        worker = WorkerRunner(
            settings=settings,
            repo=repo,
            poller=poller,
            renderer=renderer,
            deliver_image=bot.deliver_image,
            is_gateway_healthy=bot.is_gateway_healthy,
        )
        worker.start()
        worker_holder["worker"] = worker
        repo.touch_heartbeat("ready")

    bot = WoWSGameVoxBot(
        settings=settings,
        repo=repo,
        wg=wg,
        yuyuko_rank=yuyuko_rank,
        renderer=renderer,
        i18n=i18n,
        on_ready_hook=on_bot_ready,
    )

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, lambda: asyncio.create_task(bot.close()))

    try:
        await bot.start(settings.gamevox_bot_token)
    finally:
        if "worker" in worker_holder:
            await worker_holder["worker"].stop()
        await renderer.close()
        await wg.aclose()
        await pr_client.aclose()
        await yuyuko_rank.aclose()
        dbm.close()


def cli() -> None:
    parser = argparse.ArgumentParser(description="Self-hosted WoWS GameVox Bot")
    parser.add_argument("--healthcheck", action="store_true", help="Verify SQLite WAL + heartbeat")
    args = parser.parse_args()

    settings = Settings()
    if args.healthcheck:
        sys.exit(_run_healthcheck(settings))

    asyncio.run(run_app(settings))


if __name__ == "__main__":
    cli()
