from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from apscheduler.schedulers.blocking import BlockingScheduler

from .config import Settings
from .crawler import PortalCrawler
from .db import CrawlStore
from .filesystem_ingest import ingest_path
from .indexer import OpenSearchIndexer


logger = logging.getLogger(__name__)


def start_scheduler(settings: Settings) -> None:
    scheduler = BlockingScheduler(timezone="Europe/Athens")
    minute, hour, day, month, day_of_week = parse_cron(settings.crawl_cron)
    scheduler.add_job(
        lambda: asyncio.run(run_nightly(settings)),
        "cron",
        minute=minute,
        hour=hour,
        day=day,
        month=month,
        day_of_week=day_of_week,
        id="nightly_crawl",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    logger.warning("Nightly crawler scheduled with cron: %s", settings.crawl_cron)
    scheduler.start()


async def run_nightly(settings: Settings) -> None:
    logger.warning("Nightly crawl started")
    store = CrawlStore(settings.sqlite_path)
    indexer = OpenSearchIndexer(settings)
    indexer.wait_until_ready()
    crawler = PortalCrawler(settings, store, indexer)
    stats = await crawler.crawl(
        reset=settings.scheduler_reset_each_run,
        refresh_seed=not settings.scheduler_reset_each_run,
    )
    logger.warning("Nightly web crawl finished: %s", stats)

    for root in settings.extra_file_roots:
        count = await ingest_path(Path(root), settings, store, indexer)
        logger.warning("Filesystem ingest finished for %s: %s files", root, count)

    final_stats = store.stats()
    status = "ok" if final_stats.get("failed", 0) == 0 else "warning"
    summary = (
        f"Nightly incremental finished: {final_stats.get('documents', 0)} indexed documents, "
        f"{final_stats.get('done', 0)} done, "
        f"{final_stats.get('failed', 0)} failed, "
        f"{final_stats.get('queued', 0)} queued."
    )
    store.save_health_report(status, summary)
    store.add_event(summary)


def parse_cron(spec: str) -> tuple[str, str, str, str, str]:
    parts = spec.split()
    if len(parts) != 5:
        raise ValueError("CRAWL_CRON must have five fields: minute hour day month day_of_week")
    return tuple(parts)  # type: ignore[return-value]
