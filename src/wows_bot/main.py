"""Application entrypoint for wows-gamevox-bot."""

from __future__ import annotations

import argparse
import asyncio
import sys

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


def _run_healthcheck(settings: Settings) -> int:
    dbm = DatabaseManager(settings.resolved_db_path, settings.honker_watch_interval_ms)
    try:
        dbm.open()
        mode = dbm.db.query("PRAGMA journal_mode")[0]["journal_mode"]
        if str(mode).lower() != "wal":
            return 1
        return 0
    finally:
        dbm.close()


async def run_app(settings: Settings) -> None:
    logger.remove()
    logger.add(sys.stderr, level=settings.log_level.upper())

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
        )
        worker.start()
        worker_holder["worker"] = worker

    bot = WoWSGameVoxBot(
        settings=settings,
        repo=repo,
        wg=wg,
        yuyuko_rank=yuyuko_rank,
        renderer=renderer,
        i18n=i18n,
        on_ready_hook=on_bot_ready,
    )

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
    parser.add_argument("--healthcheck", action="store_true", help="Verify SQLite WAL + Honker DB")
    args = parser.parse_args()

    settings = Settings()
    if args.healthcheck:
        sys.exit(_run_healthcheck(settings))

    asyncio.run(run_app(settings))


if __name__ == "__main__":
    cli()
