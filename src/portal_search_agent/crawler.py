from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Awaitable, Callable
from urllib.parse import urlparse

import httpx

from .config import Settings
from .db import CrawlStore
from .extractors import (
    extract_actionable_links_from_html,
    extract_document,
    extract_links_from_html,
    extract_sitemap_urls,
    html_to_text,
)
from .indexer import OpenSearchIndexer
from .models import ExtractedDocument, QueueItem
from .urltools import (
    content_hash,
    extension_for_url,
    in_scope,
    is_probably_document,
    normalize_url,
    should_skip_url,
    url_hash,
)


class PortalCrawler:
    def __init__(
        self,
        settings: Settings,
        store: CrawlStore,
        indexer: OpenSearchIndexer,
        source_id: str = "",
        source_name: str = "",
        source_kind: str = "portal",
    ):
        self.settings = settings
        self.store = store
        self.indexer = indexer
        self.source_id = source_id
        self.source_name = source_name
        self.source_kind = source_kind
        self.pages_processed = 0

    async def crawl(
        self,
        reset: bool = False,
        recreate_index: bool = False,
        refresh_seed: bool = False,
        stop_event: asyncio.Event | None = None,
        progress_callback: Callable[[str], Awaitable[None]] | None = None,
    ) -> dict[str, int]:
        if reset:
            self.store.reset_queue()
        else:
            self.store.release_processing()
        if recreate_index:
            self.indexer.recreate_index()
        else:
            self.indexer.ensure_index()

        self.seed(refresh_existing=refresh_seed)
        if progress_callback:
            await progress_callback("Crawl seeded")

        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=self.settings.request_timeout_seconds,
            headers={"User-Agent": self.settings.user_agent},
        ) as client:
            while True:
                if stop_event and stop_event.is_set():
                    break
                if self.settings.max_pages and self.pages_processed >= self.settings.max_pages:
                    break

                batch = self.store.claim_batch(self.settings.concurrency)
                if not batch:
                    break
                if progress_callback:
                    await progress_callback(f"Processing {len(batch)} queued URLs")

                results = await asyncio.gather(
                    *(self.process_item(client, item) for item in batch),
                    return_exceptions=True,
                )
                for item, result in zip(batch, results):
                    if isinstance(result, Exception):
                        self.store.mark_failed(item.url, str(result))
                    else:
                        self.store.mark_done(item.url)
                if self.settings.request_delay_seconds:
                    await asyncio.sleep(self.settings.request_delay_seconds)

        if stop_event and stop_event.is_set():
            self.store.release_processing()
            if progress_callback:
                await progress_callback("Crawl stopped")
        elif progress_callback:
            await progress_callback("Crawl finished")

        return self.store.stats()

    def seed(self, refresh_existing: bool = False) -> None:
        start_url = normalize_url(self.settings.start_url)
        if start_url:
            self.seed_url(start_url, priority=100, refresh_existing=refresh_existing)

        parsed = urlparse(self.settings.start_url)
        base = f"{parsed.scheme}://{parsed.netloc}"
        root_path = "/" + self.settings.root_path.strip("/")
        for sitemap in ("/sitemap.xml", "/wp-sitemap.xml", root_path + "/sitemap.xml", root_path + "/wp-sitemap.xml"):
            url = normalize_url(base + sitemap)
            if url and self.allowed(url):
                self.seed_url(url, priority=90, refresh_existing=refresh_existing)

    def seed_url(self, url: str, priority: int, refresh_existing: bool = False) -> None:
        if refresh_existing:
            self.store.requeue_url(url)
        else:
            self.store.enqueue(url, depth=0, priority=priority)

    async def process_item(self, client: httpx.AsyncClient, item: QueueItem) -> None:
        if item.depth > self.settings.max_depth:
            return
        if not self.allowed(item.url):
            return

        response = await client.get(item.url)
        final_url = normalize_url(str(response.url))
        if final_url and final_url != item.url and self.allowed(final_url):
            self.store.enqueue(final_url, item.depth, item.url, priority=item.priority)

        response.raise_for_status()
        content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        data = response.content
        if len(data) > self.settings.max_file_bytes:
            raise RuntimeError(f"response too large: {len(data)} bytes")

        is_html = "html" in content_type or extension_for_url(item.url) in {"", ".html", ".htm", ".php"}
        is_sitemap = "xml" in content_type and "sitemap" in item.url.lower()

        if is_sitemap:
            for link in extract_sitemap_urls(data):
                normalized = normalize_url(link)
                if normalized and self.allowed(normalized):
                    self.store.enqueue(normalized, item.depth + 1, item.url, priority=80)
            return

        if is_html:
            title, text = html_to_text(data)
            for link in extract_links_from_html(data, item.url):
                if self.allowed(link):
                    priority = 60 if is_probably_document(link) else 20
                    self.store.enqueue(link, item.depth + 1, item.url, priority=priority)

            link_documents = self.build_link_documents(
                data=data,
                source_url=item.url,
                source_title=title,
                depth=item.depth + 1,
                status_code=response.status_code,
            )
            self.index_link_documents(link_documents)
        else:
            title = Path(urlparse(item.url).path).name or item.url
            text = await extract_document(data, content_type, title, self.settings)

        if not text and not title:
            return

        file_path = (
            self.save_raw_file(item.url, data, content_type)
            if self.settings.cache_raw_files and is_probably_document(item.url, content_type)
            else None
        )
        body_hash = content_hash(text or data)
        document = ExtractedDocument(
            url=item.url,
            title=title or item.url,
            text=text,
            content_type=content_type,
            status_code=response.status_code,
            depth=item.depth,
            source_type="web",
            content_hash=body_hash,
            file_path=file_path,
            metadata={
                "found_from": item.found_from,
                "source_id": self.source_id,
                "source_name": self.source_name,
                "source_kind": self.source_kind,
            },
        )
        self.indexer.index_document(document)
        self.store.upsert_document(
            url=document.url,
            title=document.title,
            content_type=document.content_type,
            status_code=document.status_code,
            content_hash=document.content_hash,
            source_type=document.source_type,
            file_path=str(file_path or ""),
            byte_size=len(data),
        )
        self.pages_processed += 1

    def build_link_documents(
        self,
        *,
        data: bytes,
        source_url: str,
        source_title: str,
        depth: int,
        status_code: int,
    ) -> list[ExtractedDocument]:
        documents: list[ExtractedDocument] = []
        for link in extract_actionable_links_from_html(data, source_url, source_title):
            if not self.should_index_link_result(link.url):
                continue

            content = "\n".join(
                part
                for part in (
                    link.text,
                    link.context,
                    link.url,
                    f"Source page: {source_title or source_url}",
                )
                if part
            )
            document_id = url_hash(f"link:{link.url}|{link.text.lower()}")
            documents.append(
                ExtractedDocument(
                    url=link.url,
                    title=link.text,
                    text=content,
                    content_type="text/x-portal-link",
                    status_code=status_code,
                    depth=depth,
                    source_type=link.kind,
                    content_hash=content_hash(content),
                    metadata={
                        "link_text": link.text,
                        "link_context": link.context,
                        "source_url": link.source_url,
                        "source_title": link.source_title,
                        "target_url": link.url,
                        "source_id": self.source_id,
                        "source_name": self.source_name,
                        "source_kind": self.source_kind,
                    },
                    document_id=document_id,
                )
            )
        return documents

    def index_link_documents(self, documents: list[ExtractedDocument]) -> None:
        if not documents:
            return

        self.indexer.bulk_index(documents)
        for document in documents:
            self.store.upsert_document(
                url=document.url,
                title=document.title,
                content_type=document.content_type,
                status_code=document.status_code,
                content_hash=document.content_hash,
                source_type=document.source_type,
                file_path="",
                byte_size=0,
                document_id=document.document_id,
            )

    def should_index_link_result(self, url: str) -> bool:
        parsed = urlparse(url)
        test = parsed.path + (("?" + parsed.query) if parsed.query else "")
        for pattern in self.settings.exclude_patterns:
            if pattern and pattern in test:
                return False
        return not should_skip_url(url)

    def save_raw_file(self, url: str, data: bytes, content_type: str) -> Path:
        suffix = extension_for_url(url) or self.suffix_from_content_type(content_type)
        path = self.settings.cache_dir / "files" / f"{url_hash(url)}{suffix}"
        path.write_bytes(data)
        return path

    def allowed(self, url: str) -> bool:
        normalized = normalize_url(url)
        if not normalized or should_skip_url(normalized):
            return False
        return in_scope(
            normalized,
            self.settings.allowed_hosts,
            self.settings.root_path,
            self.settings.exclude_patterns,
        )

    @staticmethod
    def suffix_from_content_type(content_type: str) -> str:
        mapping = {
            "application/pdf": ".pdf",
            "application/msword": ".doc",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
            "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
            "text/plain": ".txt",
            "text/csv": ".csv",
        }
        return mapping.get(content_type, ".bin")
