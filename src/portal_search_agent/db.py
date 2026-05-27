from __future__ import annotations

import sqlite3
import json
from pathlib import Path

from .models import QueueItem, utc_now_iso
from .urltools import url_hash


SCHEMA = """
CREATE TABLE IF NOT EXISTS crawl_queue (
    url_hash TEXT PRIMARY KEY,
    url TEXT NOT NULL,
    depth INTEGER NOT NULL DEFAULT 0,
    found_from TEXT NOT NULL DEFAULT '',
    priority INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'queued',
    attempts INTEGER NOT NULL DEFAULT 0,
    error TEXT NOT NULL DEFAULT '',
    discovered_at TEXT NOT NULL,
    fetched_at TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_crawl_queue_status_priority
ON crawl_queue(status, priority DESC, discovered_at ASC);

CREATE TABLE IF NOT EXISTS documents (
    url_hash TEXT PRIMARY KEY,
    url TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    content_type TEXT NOT NULL DEFAULT '',
    status_code INTEGER NOT NULL DEFAULT 0,
    content_hash TEXT NOT NULL DEFAULT '',
    source_type TEXT NOT NULL DEFAULT 'web',
    file_path TEXT NOT NULL DEFAULT '',
    byte_size INTEGER NOT NULL DEFAULT 0,
    fetched_at TEXT NOT NULL DEFAULT '',
    indexed_at TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS crawl_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_time TEXT NOT NULL,
    message TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS search_synonyms (
    term TEXT PRIMARY KEY,
    variants TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS search_queries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_time TEXT NOT NULL,
    query TEXT NOT NULL,
    filters TEXT NOT NULL DEFAULT '',
    total_results INTEGER NOT NULL DEFAULT 0,
    top_url TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS search_clicks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_time TEXT NOT NULL,
    query TEXT NOT NULL DEFAULT '',
    url TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    source_type TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS search_feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_time TEXT NOT NULL,
    query TEXT NOT NULL DEFAULT '',
    url TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    feedback_type TEXT NOT NULL DEFAULT '',
    message TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS health_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_time TEXT NOT NULL,
    status TEXT NOT NULL,
    summary TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_search_queries_event_time
ON search_queries(event_time DESC);

CREATE INDEX IF NOT EXISTS idx_search_feedback_event_time
ON search_feedback(event_time DESC);

CREATE INDEX IF NOT EXISTS idx_health_reports_event_time
ON health_reports(event_time DESC);

CREATE TABLE IF NOT EXISTS kb_sources (
    id TEXT PRIMARY KEY,
    source_type TEXT NOT NULL,
    name TEXT NOT NULL,
    location TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    options TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_kb_sources_enabled_type
ON kb_sources(enabled, source_type);
"""


class CrawlStore:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def reset_queue(self) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM crawl_queue")
            self.conn.execute("DELETE FROM documents")

    def release_processing(self) -> None:
        with self.conn:
            self.conn.execute("UPDATE crawl_queue SET status = 'queued' WHERE status = 'processing'")

    def enqueue(self, url: str, depth: int, found_from: str = "", priority: int = 0) -> bool:
        with self.conn:
            cur = self.conn.execute(
                """
                INSERT OR IGNORE INTO crawl_queue
                (url_hash, url, depth, found_from, priority, status, attempts, discovered_at)
                VALUES (?, ?, ?, ?, ?, 'queued', 0, ?)
                """,
                (url_hash(url), url, depth, found_from, priority, utc_now_iso()),
            )
            return cur.rowcount > 0

    def claim_batch(self, limit: int) -> list[QueueItem]:
        rows = self.conn.execute(
            """
            SELECT url, depth, found_from, priority, attempts
            FROM crawl_queue
            WHERE status = 'queued'
            ORDER BY priority DESC, discovered_at ASC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

        items = [QueueItem(**dict(row)) for row in rows]
        with self.conn:
            for item in items:
                self.conn.execute(
                    "UPDATE crawl_queue SET status = 'processing', attempts = attempts + 1 WHERE url_hash = ?",
                    (url_hash(item.url),),
                )
        return items

    def mark_done(self, url: str) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE crawl_queue SET status = 'done', fetched_at = ?, error = '' WHERE url_hash = ?",
                (utc_now_iso(), url_hash(url)),
            )

    def mark_failed(self, url: str, error: str) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE crawl_queue SET status = 'failed', fetched_at = ?, error = ? WHERE url_hash = ?",
                (utc_now_iso(), error[:2000], url_hash(url)),
            )

    def upsert_document(
        self,
        *,
        url: str,
        title: str,
        content_type: str,
        status_code: int,
        content_hash: str,
        source_type: str,
        file_path: str,
        byte_size: int,
        document_id: str | None = None,
    ) -> None:
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO documents
                (url_hash, url, title, content_type, status_code, content_hash, source_type, file_path, byte_size, fetched_at, indexed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(url_hash) DO UPDATE SET
                    title = excluded.title,
                    content_type = excluded.content_type,
                    status_code = excluded.status_code,
                    content_hash = excluded.content_hash,
                    source_type = excluded.source_type,
                    file_path = excluded.file_path,
                    byte_size = excluded.byte_size,
                    fetched_at = excluded.fetched_at,
                    indexed_at = excluded.indexed_at
                """,
                (
                    document_id or url_hash(url),
                    url,
                    title,
                    content_type,
                    status_code,
                    content_hash,
                    source_type,
                    file_path,
                    byte_size,
                    utc_now_iso(),
                    utc_now_iso(),
                ),
            )

    def stats(self) -> dict[str, int]:
        rows = self.conn.execute(
            "SELECT status, COUNT(*) AS total FROM crawl_queue GROUP BY status"
        ).fetchall()
        stats = {"queued": 0, "processing": 0, "done": 0, "failed": 0, "documents": 0}
        for row in rows:
            stats[row["status"]] = row["total"]
        stats["documents"] = self.conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        return stats

    def add_event(self, message: str) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO crawl_events (event_time, message) VALUES (?, ?)",
                (utc_now_iso(), message[:2000]),
            )
            self.conn.execute(
                """
                DELETE FROM crawl_events
                WHERE id NOT IN (
                    SELECT id FROM crawl_events ORDER BY id DESC LIMIT 500
                )
                """
            )

    def recent_events(self, limit: int = 30) -> list[dict[str, str]]:
        rows = self.conn.execute(
            """
            SELECT event_time, message
            FROM crawl_events
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [{"time": row["event_time"], "message": row["message"]} for row in reversed(rows)]

    def latest_activity_time(self) -> str:
        row = self.conn.execute(
            """
            SELECT MAX(value) AS latest
            FROM (
                SELECT MAX(NULLIF(fetched_at, '')) AS value FROM crawl_queue
                UNION ALL
                SELECT MAX(NULLIF(discovered_at, '')) AS value FROM crawl_queue
                UNION ALL
                SELECT MAX(NULLIF(indexed_at, '')) AS value FROM documents
            )
            """
        ).fetchone()
        return row["latest"] or ""

    def list_synonyms(self) -> list[dict[str, str | list[str]]]:
        rows = self.conn.execute(
            "SELECT term, variants, updated_at FROM search_synonyms ORDER BY term"
        ).fetchall()
        return [
            {
                "term": row["term"],
                "variants": self._split_variants(row["variants"]),
                "updated_at": row["updated_at"],
            }
            for row in rows
        ]

    def save_synonym(self, term: str, variants: list[str]) -> None:
        clean_term = term.strip()
        clean_variants = [variant.strip() for variant in variants if variant.strip()]
        if not clean_term:
            raise ValueError("term is required")
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO search_synonyms (term, variants, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(term) DO UPDATE SET
                    variants = excluded.variants,
                    updated_at = excluded.updated_at
                """,
                (clean_term, "\n".join(dict.fromkeys(clean_variants)), utc_now_iso()),
            )

    def delete_synonym(self, term: str) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM search_synonyms WHERE term = ?", (term,))

    def synonym_variants_for(self, query: str) -> list[str]:
        lowered = query.casefold()
        matches: list[str] = []
        for row in self.conn.execute("SELECT term, variants FROM search_synonyms").fetchall():
            terms = [row["term"], *self._split_variants(row["variants"])]
            if any(term.casefold() in lowered or lowered in term.casefold() for term in terms if term):
                matches.extend(terms)
        return [item for item in dict.fromkeys(matches) if item.strip()]

    def synonym_suggestions(self, prefix: str, limit: int = 8) -> list[str]:
        lowered = prefix.casefold()
        suggestions: list[str] = []
        for row in self.conn.execute("SELECT term, variants FROM search_synonyms ORDER BY term").fetchall():
            for value in [row["term"], *self._split_variants(row["variants"])]:
                if value and value.casefold().startswith(lowered):
                    suggestions.append(value)
        return list(dict.fromkeys(suggestions))[:limit]

    def record_search(self, query: str, filters: list[str], total_results: int, top_url: str = "") -> None:
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO search_queries (event_time, query, filters, total_results, top_url)
                VALUES (?, ?, ?, ?, ?)
                """,
                (utc_now_iso(), query[:500], ",".join(filters), total_results, top_url[:2000]),
            )

    def record_click(self, query: str, url: str, title: str = "", source_type: str = "") -> None:
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO search_clicks (event_time, query, url, title, source_type)
                VALUES (?, ?, ?, ?, ?)
                """,
                (utc_now_iso(), query[:500], url[:2000], title[:500], source_type[:100]),
            )

    def record_feedback(
        self,
        *,
        query: str,
        url: str = "",
        title: str = "",
        feedback_type: str = "",
        message: str = "",
    ) -> None:
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO search_feedback (event_time, query, url, title, feedback_type, message)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    utc_now_iso(),
                    query[:500],
                    url[:2000],
                    title[:500],
                    feedback_type[:100],
                    message[:2000],
                ),
            )

    def diagnostics(self, limit: int = 25) -> dict:
        return {
            "stats": self.stats(),
            "content_types": self._group_counts("documents", "content_type", limit),
            "source_types": self._group_counts("documents", "source_type", limit),
            "failed_urls": self.failed_urls(limit),
            "largest_documents": self.largest_documents(limit),
            "recent_documents": self.recent_documents(limit),
            "analytics": self.analytics_summary(limit),
            "feedback": self.recent_feedback(limit),
            "latest_health_report": self.latest_health_report(),
        }

    def failed_urls(self, limit: int = 25) -> list[dict[str, str | int]]:
        rows = self.conn.execute(
            """
            SELECT url, attempts, error, fetched_at
            FROM crawl_queue
            WHERE status = 'failed'
            ORDER BY fetched_at DESC, attempts DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]

    def largest_documents(self, limit: int = 25) -> list[dict[str, str | int]]:
        rows = self.conn.execute(
            """
            SELECT url, title, content_type, source_type, byte_size, indexed_at
            FROM documents
            WHERE byte_size > 0
            ORDER BY byte_size DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]

    def recent_documents(self, limit: int = 25) -> list[dict[str, str | int]]:
        rows = self.conn.execute(
            """
            SELECT url, title, content_type, source_type, byte_size, indexed_at
            FROM documents
            ORDER BY indexed_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]

    def document_by_id(self, document_id: str) -> dict[str, str | int]:
        row = self.conn.execute(
            """
            SELECT url_hash, url, title, content_type, source_type, file_path, byte_size, indexed_at
            FROM documents
            WHERE url_hash = ?
            """,
            (document_id,),
        ).fetchone()
        return dict(row) if row else {}

    def analytics_summary(self, limit: int = 20) -> dict:
        top_queries = self.conn.execute(
            """
            SELECT query, COUNT(*) AS count, MAX(event_time) AS last_seen, AVG(total_results) AS avg_results
            FROM search_queries
            WHERE query != ''
            GROUP BY query
            ORDER BY count DESC, last_seen DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        zero_result_queries = self.conn.execute(
            """
            SELECT query, COUNT(*) AS count, MAX(event_time) AS last_seen
            FROM search_queries
            WHERE query != '' AND total_results = 0
            GROUP BY query
            ORDER BY count DESC, last_seen DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        top_clicks = self.conn.execute(
            """
            SELECT url, title, source_type, COUNT(*) AS count, MAX(event_time) AS last_clicked
            FROM search_clicks
            GROUP BY url, title, source_type
            ORDER BY count DESC, last_clicked DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return {
            "top_queries": [dict(row) for row in top_queries],
            "zero_result_queries": [dict(row) for row in zero_result_queries],
            "top_clicks": [dict(row) for row in top_clicks],
        }

    def recent_feedback(self, limit: int = 25) -> list[dict[str, str]]:
        rows = self.conn.execute(
            """
            SELECT event_time, query, url, title, feedback_type, message
            FROM search_feedback
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]

    def requeue_failed(self) -> int:
        with self.conn:
            cur = self.conn.execute(
                """
                UPDATE crawl_queue
                SET status = 'queued', error = ''
                WHERE status = 'failed'
                """
            )
            return cur.rowcount

    def requeue_url(self, url: str) -> int:
        normalized = url.strip()
        if not normalized:
            return 0
        with self.conn:
            cur = self.conn.execute(
                """
                UPDATE crawl_queue
                SET status = 'queued', error = ''
                WHERE url_hash = ?
                """,
                (url_hash(normalized),),
            )
            if cur.rowcount:
                return cur.rowcount
            self.conn.execute(
                """
                INSERT OR IGNORE INTO crawl_queue
                (url_hash, url, depth, found_from, priority, status, attempts, discovered_at)
                VALUES (?, ?, 0, 'manual-requeue', 100, 'queued', 0, ?)
                """,
                (url_hash(normalized), normalized, utc_now_iso()),
            )
            return 1

    def requeue_urls(self, urls: list[str]) -> int:
        count = 0
        for url in dict.fromkeys(url.strip() for url in urls if url.strip()):
            count += self.requeue_url(url)
        return count

    def requeue_category(self, category: str, limit: int = 5000) -> int:
        where = self._category_where(category)
        if not where:
            return 0
        rows = self.conn.execute(
            f"""
            SELECT DISTINCT url
            FROM documents
            WHERE {where}
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return self.requeue_urls([row["url"] for row in rows])

    def save_health_report(self, status: str, summary: str, payload: str = "") -> None:
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO health_reports (event_time, status, summary, payload)
                VALUES (?, ?, ?, ?)
                """,
                (utc_now_iso(), status[:50], summary[:1000], payload[:10000]),
            )

    def latest_health_report(self) -> dict[str, str]:
        row = self.conn.execute(
            """
            SELECT event_time, status, summary, payload
            FROM health_reports
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
        return dict(row) if row else {}

    def ensure_kb_source(
        self,
        *,
        source_type: str,
        name: str,
        location: str,
        enabled: bool = True,
        options: dict | None = None,
    ) -> str:
        source_id = url_hash(f"{source_type}:{location}")
        now = utc_now_iso()
        with self.conn:
            self.conn.execute(
                """
                INSERT OR IGNORE INTO kb_sources
                (id, source_type, name, location, enabled, options, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    source_id,
                    source_type.strip().lower(),
                    name.strip() or location.strip(),
                    location.strip(),
                    1 if enabled else 0,
                    json.dumps(options or {}, ensure_ascii=False),
                    now,
                    now,
                ),
            )
        return source_id

    def save_kb_source(
        self,
        *,
        source_type: str,
        name: str,
        location: str,
        enabled: bool = True,
        options: dict | None = None,
        source_id: str = "",
    ) -> str:
        clean_type = source_type.strip().lower()
        clean_location = location.strip()
        clean_name = name.strip() or clean_location
        if not source_id:
            source_id = url_hash(f"{clean_type}:{clean_location}")
        now = utc_now_iso()
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO kb_sources
                (id, source_type, name, location, enabled, options, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    source_type = excluded.source_type,
                    name = excluded.name,
                    location = excluded.location,
                    enabled = excluded.enabled,
                    options = excluded.options,
                    updated_at = excluded.updated_at
                """,
                (
                    source_id,
                    clean_type,
                    clean_name,
                    clean_location,
                    1 if enabled else 0,
                    json.dumps(options or {}, ensure_ascii=False),
                    now,
                    now,
                ),
            )
        return source_id

    def list_kb_sources(self, enabled_only: bool = False) -> list[dict]:
        where = "WHERE enabled = 1" if enabled_only else ""
        rows = self.conn.execute(
            f"""
            SELECT id, source_type, name, location, enabled, options, created_at, updated_at
            FROM kb_sources
            {where}
            ORDER BY source_type, name
            """
        ).fetchall()
        return [self._kb_source_from_row(row) for row in rows]

    def get_kb_sources(self, source_ids: list[str]) -> list[dict]:
        clean_ids = [item.strip() for item in source_ids if item.strip()]
        if not clean_ids:
            return []
        placeholders = ",".join("?" for _ in clean_ids)
        rows = self.conn.execute(
            f"""
            SELECT id, source_type, name, location, enabled, options, created_at, updated_at
            FROM kb_sources
            WHERE id IN ({placeholders})
            ORDER BY source_type, name
            """,
            clean_ids,
        ).fetchall()
        return [self._kb_source_from_row(row) for row in rows]

    def delete_kb_source(self, source_id: str) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM kb_sources WHERE id = ?", (source_id,))

    @staticmethod
    def _kb_source_from_row(row: sqlite3.Row) -> dict:
        try:
            options = json.loads(row["options"] or "{}")
        except json.JSONDecodeError:
            options = {}
        return {
            "id": row["id"],
            "type": row["source_type"],
            "name": row["name"],
            "location": row["location"],
            "enabled": bool(row["enabled"]),
            "options": options,
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def _group_counts(self, table: str, column: str, limit: int) -> list[dict[str, str | int]]:
        rows = self.conn.execute(
            f"""
            SELECT COALESCE(NULLIF({column}, ''), '(empty)') AS value, COUNT(*) AS count
            FROM {table}
            GROUP BY value
            ORDER BY count DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _split_variants(value: str) -> list[str]:
        return [item.strip() for item in value.replace(",", "\n").splitlines() if item.strip()]

    @staticmethod
    def _category_where(category: str) -> str:
        mapping = {
            "pages": "source_type = 'web' AND (content_type LIKE 'text/html%' OR content_type = '')",
            "documents": "content_type NOT LIKE 'text/html%' AND content_type != 'text/x-portal-link'",
            "pdf": "content_type = 'application/pdf' OR lower(url) LIKE '%.pdf%'",
            "word": "content_type IN ('application/msword', 'application/vnd.openxmlformats-officedocument.wordprocessingml.document') OR lower(url) LIKE '%.doc%'",
            "excel": "content_type IN ('application/vnd.ms-excel', 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet') OR lower(url) LIKE '%.xls%'",
            "powerpoint": "content_type IN ('application/vnd.ms-powerpoint', 'application/vnd.openxmlformats-officedocument.presentationml.presentation') OR lower(url) LIKE '%.ppt%'",
            "applications": "source_type = 'application_link'",
            "links": "source_type LIKE '%_link'",
        }
        return mapping.get(category, "")
