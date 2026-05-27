from __future__ import annotations

import argparse
import asyncio
import logging
from pathlib import Path

import uvicorn

from .config import get_settings
from .crawler import PortalCrawler
from .db import CrawlStore
from .filesystem_ingest import ingest_path
from .indexer import OpenSearchIndexer
from .scheduler import start_scheduler


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(prog="portal-search-agent")
    sub = parser.add_subparsers(dest="command", required=True)

    crawl = sub.add_parser("crawl", help="Run a web crawl now")
    crawl.add_argument("--reset", action="store_true", help="Clear crawler queue before seeding")
    crawl.add_argument("--recreate-index", action="store_true", help="Delete and recreate the OpenSearch index")

    ingest = sub.add_parser("ingest-path", help="Index a read-only local/network folder")
    ingest.add_argument("path", help="Folder to scan")
    ingest.add_argument("--base-url", default="file://", help="Public URL prefix for indexed files")

    sub.add_parser("recreate-index", help="Delete and recreate the OpenSearch index")
    sub.add_parser("scheduler", help="Run the nightly scheduler")

    serve = sub.add_parser("serve", help="Run the search web UI/API")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", default=8080, type=int)

    args = parser.parse_args()
    settings = get_settings()

    if args.command == "serve":
        uvicorn.run("portal_search_agent.api:app", host=args.host, port=args.port, reload=False)
        return

    if args.command == "scheduler":
        start_scheduler(settings)
        return

    store = CrawlStore(settings.sqlite_path)
    indexer = OpenSearchIndexer(settings)
    indexer.wait_until_ready()

    if args.command == "recreate-index":
        indexer.recreate_index()
        print("Index recreated.")
        return

    if args.command == "crawl":
        crawler = PortalCrawler(settings, store, indexer)
        stats = asyncio.run(crawler.crawl(reset=args.reset, recreate_index=args.recreate_index))
        print(stats)
        return

    if args.command == "ingest-path":
        count = asyncio.run(ingest_path(Path(args.path), settings, store, indexer, args.base_url))
        print({"indexed_files": count})
        return


if __name__ == "__main__":
    main()
