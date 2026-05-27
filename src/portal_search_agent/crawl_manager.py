from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from pathlib import Path
from urllib.parse import urlparse

from .config import Settings
from .crawler import PortalCrawler
from .db import CrawlStore
from .filesystem_ingest import ingest_path
from .indexer import OpenSearchIndexer
from .models import utc_now_iso


@dataclass
class CrawlJobState:
    running: bool = False
    stopping: bool = False
    started_at: str = ""
    finished_at: str = ""
    last_message: str = "Idle"
    last_error: str = ""
    active_source: str = ""
    target_index: str = ""
    progress_percent: float = 0
    events: list[dict[str, str]] = field(default_factory=list)


class CrawlJobManager:
    def __init__(self, settings: Settings, store: CrawlStore):
        self.settings = settings
        self.store = store
        self.state = CrawlJobState()
        self.stop_event: asyncio.Event | None = None
        self.task: asyncio.Task | None = None
        self.lock = asyncio.Lock()

    async def start(
        self,
        reset: bool = False,
        recreate_index: bool = False,
        source: str = "portal",
        source_ids: list[str] | None = None,
    ) -> dict:
        source = self.normalize_source(source)
        selected_sources = self.selected_sources(source, source_ids or [])
        if not selected_sources:
            raise ValueError("No enabled knowledge sources selected")
        if recreate_index and not self.is_all_enabled_selected(selected_sources):
            raise ValueError("Full rebuild recreates the whole index and requires source=all")
        async with self.lock:
            if self.task and not self.task.done():
                return self.status()

            self.stop_event = asyncio.Event()
            self.state = CrawlJobState(
                running=True,
                stopping=False,
                started_at=utc_now_iso(),
                finished_at="",
                last_message=f"Starting knowledge build ({self.source_label(selected_sources)})",
                active_source=self.source_label(selected_sources),
                target_index=self.settings.opensearch_index,
            )
            self._event(f"Starting knowledge build ({self.source_label(selected_sources)})")
            self.task = asyncio.create_task(
                self._run(reset=reset, recreate_index=recreate_index, sources=selected_sources)
            )
            return self.status()

    async def stop(self) -> dict:
        async with self.lock:
            if self.stop_event:
                self.stop_event.set()
            if self.state.running:
                self.state.stopping = True
                self.state.last_message = "Stopping after current batch"
                self._event("Stop requested")
            return self.status()

    def status(self) -> dict:
        stats = self.store.stats()
        total_known = stats.get("queued", 0) + stats.get("processing", 0) + stats.get("done", 0) + stats.get("failed", 0)
        finished = stats.get("done", 0) + stats.get("failed", 0)
        progress = round((finished / total_known) * 100, 2) if total_known else 0
        events = self.store.recent_events(30)
        if not events and total_known:
            events = [
                {
                    "time": self.store.latest_activity_time() or utc_now_iso(),
                    "message": (
                        "Existing crawl state loaded from database: "
                        f"{stats.get('done', 0)} done, "
                        f"{stats.get('failed', 0)} failed, "
                        f"{stats.get('queued', 0)} queued, "
                        f"{stats.get('documents', 0)} indexed documents"
                    ),
                }
            ]
        last_message = self.state.last_message
        if last_message == "Idle" and total_known:
            last_message = "Crawl state loaded from database"
        return {
            "running": self.state.running,
            "stopping": self.state.stopping,
            "started_at": self.state.started_at,
            "finished_at": self.state.finished_at,
            "last_message": last_message,
            "last_error": self.state.last_error,
            "active_source": self.state.active_source,
            "target_index": self.settings.opensearch_index,
            "progress_percent": self.state.progress_percent if self.state.running and self.state.progress_percent else progress,
            "total_known": total_known,
            "events": events,
            "stats": stats,
        }

    async def _run(self, reset: bool, recreate_index: bool, sources: list[dict]) -> None:
        try:
            indexer = OpenSearchIndexer(self.settings)
            indexer.wait_until_ready()
            if recreate_index:
                indexer.recreate_index()
                self._event(f"Target index recreated: {self.settings.opensearch_index}")

            reset_next_web = reset
            web_sources = [item for item in sources if item["type"] in {"portal", "web"}]
            file_sources = [item for item in sources if item["type"] == "filesystem"]
            passive_sources = [item for item in sources if item["type"] == "database"]

            if reset and not web_sources:
                self.store.reset_queue()

            for source in web_sources:
                crawler_settings = self.settings_for_web_source(source)
                crawler = PortalCrawler(
                    crawler_settings,
                    self.store,
                    indexer,
                    source_id=source["id"],
                    source_name=source["name"],
                    source_kind=source["type"],
                )
                await crawler.crawl(
                    reset=reset_next_web,
                    recreate_index=False,
                    stop_event=self.stop_event,
                    progress_callback=self._progress,
                )
                reset_next_web = False
                if self.stop_event and self.stop_event.is_set():
                    break

            if file_sources and not (self.stop_event and self.stop_event.is_set()):
                await self._run_filesystem_ingest(indexer, file_sources)

            for source in passive_sources:
                self._event(f"Database source registered but no adapter configured yet: {source['name']}")
        except Exception as exc:
            self.state.last_error = str(exc)
            self.state.last_message = "Knowledge build failed"
            self._event(f"Knowledge build failed: {exc}")
        finally:
            self.state.running = False
            self.state.stopping = False
            self.state.finished_at = utc_now_iso()
            if not self.state.last_error and self.state.last_message != "Crawl stopped":
                self.state.progress_percent = 100
                self.state.last_message = "Knowledge build finished"
            self._event(self.state.last_message)

    async def _run_filesystem_ingest(self, indexer: OpenSearchIndexer, sources: list[dict]) -> None:
        if not sources:
            await self._progress("No filesystem roots configured")
            return
        total = 0
        for index, source in enumerate(sources, start=1):
            if self.stop_event and self.stop_event.is_set():
                self.state.last_message = "Crawl stopped"
                self._event("Filesystem ingest stopped")
                break
            self.state.progress_percent = max(1, round((index - 1) / max(len(sources), 1) * 100, 2))
            root = Path(source["location"])
            await self._progress(f"Starting filesystem ingest: {root}")
            count = await ingest_path(
                root,
                self.settings,
                self.store,
                indexer,
                stop_event=self.stop_event,
                progress_callback=self._progress,
                source_id=source["id"],
                source_name=source["name"],
            )
            total += count
            self.state.progress_percent = round(index / max(len(sources), 1) * 100, 2)
        await self._progress(f"Filesystem ingest indexed {total} files total")

    async def _progress(self, message: str) -> None:
        self.state.last_message = message
        self._event(message)

    def _event(self, message: str) -> None:
        self.state.events.append({"time": utc_now_iso(), "message": message})
        self.state.events = self.state.events[-100:]
        self.store.add_event(message)

    @staticmethod
    def normalize_source(source: str) -> str:
        value = (source or "portal").strip().lower()
        if value in {"files", "filesystem", "local", "network"}:
            return "filesystem"
        if value in {"all", "everything", "extended"}:
            return "all"
        return "portal"

    def ensure_default_sources(self) -> None:
        self.store.ensure_kb_source(
            source_type="portal",
            name="251GNA Portal",
            location=self.settings.start_url,
            enabled=True,
            options={
                "allowed_hosts": self.settings.allowed_hosts,
                "root_path": self.settings.root_path,
            },
        )
        for root in self.settings.extra_file_roots:
            self.store.ensure_kb_source(
                source_type="filesystem",
                name=Path(root).name or root,
                location=root,
                enabled=True,
            )

    def selected_sources(self, source: str, source_ids: list[str]) -> list[dict]:
        self.ensure_default_sources()
        if source_ids:
            return [item for item in self.store.get_kb_sources(source_ids) if item.get("enabled")]
        enabled = self.store.list_kb_sources(enabled_only=True)
        if source == "all":
            return enabled
        if source == "filesystem":
            return [item for item in enabled if item["type"] == "filesystem"]
        return [item for item in enabled if item["type"] == "portal"]

    def is_all_enabled_selected(self, selected_sources: list[dict]) -> bool:
        enabled_ids = {
            item["id"]
            for item in self.store.list_kb_sources(enabled_only=True)
            if item["type"] != "database"
        }
        selected_ids = {item["id"] for item in selected_sources if item["type"] != "database"}
        return bool(enabled_ids) and enabled_ids == selected_ids

    @staticmethod
    def source_label(sources: list[dict]) -> str:
        if not sources:
            return "no sources"
        if len(sources) == 1:
            return sources[0]["name"]
        return f"{len(sources)} sources"

    def settings_for_web_source(self, source: dict) -> Settings:
        options = source.get("options") or {}
        parsed = urlparse(source["location"])
        allowed_hosts = options.get("allowed_hosts") or ([parsed.hostname] if parsed.hostname else self.settings.allowed_hosts)
        if isinstance(allowed_hosts, str):
            allowed_hosts = [item.strip() for item in allowed_hosts.split(",") if item.strip()]
        root_path = options.get("root_path") or parsed.path or "/"
        return replace(
            self.settings,
            start_url=source["location"],
            allowed_hosts=allowed_hosts,
            root_path=root_path,
        )
