from __future__ import annotations

import asyncio
from pathlib import Path

from .config import Settings
from .db import CrawlStore
from .extractors import extract_document
from .indexer import OpenSearchIndexer
from .models import ExtractedDocument
from .urltools import DOCUMENT_EXTENSIONS, content_hash


async def ingest_path(
    root: Path,
    settings: Settings,
    store: CrawlStore,
    indexer: OpenSearchIndexer,
    base_url: str = "file://",
    stop_event: asyncio.Event | None = None,
    progress_callback=None,
    source_id: str = "",
    source_name: str = "",
) -> int:
    indexer.ensure_index()
    root = root.resolve()
    if root.is_file():
        paths = [root]
        url_root = root.parent
    else:
        paths = root.rglob("*")
        url_root = root
    count = 0
    for path in paths:
        if stop_event and stop_event.is_set():
            break
        if not path.is_file() or path.suffix.lower() not in DOCUMENT_EXTENSIONS:
            continue
        if path.stat().st_size > settings.max_file_bytes:
            continue
        data = path.read_bytes()
        virtual_url = make_virtual_url(path, url_root, base_url)
        text = await extract_document(data, guess_content_type(path), path.name, settings)
        document = ExtractedDocument(
            url=virtual_url,
            title=path.name,
            text=text,
            content_type=guess_content_type(path),
            status_code=200,
            depth=0,
            source_type="filesystem",
            content_hash=content_hash(text or data),
            file_path=path,
            metadata={
                "local_root": str(root),
                "source_id": source_id,
                "source_name": source_name,
                "source_kind": "filesystem",
            },
        )
        indexer.index_document(document)
        store.upsert_document(
            url=document.url,
            title=document.title,
            content_type=document.content_type,
            status_code=document.status_code,
            content_hash=document.content_hash,
            source_type=document.source_type,
            file_path=str(path),
            byte_size=path.stat().st_size,
        )
        count += 1
        if count % 25 == 0:
            if progress_callback:
                await progress_callback(f"Filesystem ingest indexed {count} files from {root}")
            await asyncio.sleep(0)
    if progress_callback:
        await progress_callback(f"Filesystem ingest finished for {root}: {count} files")
    return count


def make_virtual_url(path: Path, root: Path, base_url: str) -> str:
    relative = path.resolve().relative_to(root).as_posix()
    if base_url.startswith("file://"):
        return path.resolve().as_uri()
    return base_url.rstrip("/") + "/" + relative


def guess_content_type(path: Path) -> str:
    suffix = path.suffix.lower()
    return {
        ".pdf": "application/pdf",
        ".doc": "application/msword",
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".xls": "application/vnd.ms-excel",
        ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".ppt": "application/vnd.ms-powerpoint",
        ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        ".odt": "application/vnd.oasis.opendocument.text",
        ".ods": "application/vnd.oasis.opendocument.spreadsheet",
        ".odp": "application/vnd.oasis.opendocument.presentation",
        ".rtf": "application/rtf",
        ".txt": "text/plain",
        ".csv": "text/csv",
        ".xml": "application/xml",
    }.get(suffix, "application/octet-stream")
