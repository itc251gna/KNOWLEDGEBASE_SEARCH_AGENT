from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@dataclass
class QueueItem:
    url: str
    depth: int
    found_from: str = ""
    priority: int = 0
    attempts: int = 0


@dataclass
class LinkCandidate:
    url: str
    text: str
    context: str = ""
    kind: str = "portal_link"
    source_url: str = ""
    source_title: str = ""


@dataclass
class ExtractedDocument:
    url: str
    title: str
    text: str
    content_type: str
    status_code: int
    depth: int
    source_type: str = "web"
    content_hash: str = ""
    fetched_at: str = field(default_factory=utc_now_iso)
    modified_at: str | None = None
    file_path: Path | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    document_id: str | None = None

    @property
    def byte_size(self) -> int:
        if self.file_path and self.file_path.exists():
            return self.file_path.stat().st_size
        return 0
