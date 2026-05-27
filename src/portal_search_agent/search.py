from __future__ import annotations

from opensearchpy import OpenSearch
from opensearchpy.exceptions import NotFoundError, OpenSearchException

from .config import Settings
from .db import CrawlStore


VISUAL_GREEK_TO_LATIN = str.maketrans(
    {
        "\u0391": "A",
        "\u0392": "B",
        "\u0395": "E",
        "\u0396": "Z",
        "\u0397": "H",
        "\u0399": "I",
        "\u039a": "K",
        "\u039c": "M",
        "\u039d": "N",
        "\u039f": "O",
        "\u03a1": "P",
        "\u03a4": "T",
        "\u03a5": "Y",
        "\u03a7": "X",
        "\u03b1": "a",
        "\u03b2": "b",
        "\u03b5": "e",
        "\u03b6": "z",
        "\u03b7": "h",
        "\u03b9": "i",
        "\u03ba": "k",
        "\u03bc": "m",
        "\u03bd": "n",
        "\u03bf": "o",
        "\u03c1": "p",
        "\u03c4": "t",
        "\u03c5": "y",
        "\u03c7": "x",
    }
)

VISUAL_LATIN_TO_GREEK = str.maketrans(
    {
        "A": "\u0391",
        "B": "\u0392",
        "E": "\u0395",
        "Z": "\u0396",
        "H": "\u0397",
        "I": "\u0399",
        "K": "\u039a",
        "M": "\u039c",
        "N": "\u039d",
        "O": "\u039f",
        "P": "\u03a1",
        "T": "\u03a4",
        "Y": "\u03a5",
        "X": "\u03a7",
        "a": "\u03b1",
        "b": "\u03b2",
        "e": "\u03b5",
        "z": "\u03b6",
        "h": "\u03b7",
        "i": "\u03b9",
        "k": "\u03ba",
        "m": "\u03bc",
        "n": "\u03bd",
        "o": "\u03bf",
        "p": "\u03c1",
        "t": "\u03c4",
        "y": "\u03c5",
        "x": "\u03c7",
    }
)

GREEKLISH_TO_GREEK = str.maketrans(
    {
        "a": "\u03b1",
        "b": "\u03b2",
        "g": "\u03b3",
        "d": "\u03b4",
        "e": "\u03b5",
        "z": "\u03b6",
        "h": "\u03b7",
        "i": "\u03b9",
        "k": "\u03ba",
        "l": "\u03bb",
        "m": "\u03bc",
        "n": "\u03bd",
        "j": "\u03be",
        "o": "\u03bf",
        "p": "\u03c0",
        "r": "\u03c1",
        "s": "\u03c3",
        "t": "\u03c4",
        "y": "\u03c5",
        "f": "\u03c6",
        "x": "\u03c7",
        "c": "\u03c8",
        "w": "\u03c9",
        "A": "\u0391",
        "B": "\u0392",
        "G": "\u0393",
        "D": "\u0394",
        "E": "\u0395",
        "Z": "\u0396",
        "H": "\u0397",
        "I": "\u0399",
        "K": "\u039a",
        "L": "\u039b",
        "M": "\u039c",
        "N": "\u039d",
        "J": "\u039e",
        "O": "\u039f",
        "P": "\u03a0",
        "R": "\u03a1",
        "S": "\u03a3",
        "T": "\u03a4",
        "Y": "\u03a5",
        "F": "\u03a6",
        "X": "\u03a7",
        "C": "\u03a8",
        "W": "\u03a9",
    }
)


def query_variants(query: str) -> list[str]:
    variants = {
        query,
        query.translate(VISUAL_GREEK_TO_LATIN),
        query.translate(VISUAL_LATIN_TO_GREEK),
        query.translate(GREEKLISH_TO_GREEK),
    }
    return [variant for variant in variants if variant.strip()]


def wants_application(query: str) -> bool:
    lowered = query.lower()
    return (
        "application" in lowered
        or "efarmog" in lowered
        or "\u03b5\u03c6\u03b1\u03c1\u03bc\u03bf\u03b3" in lowered
    )


class SearchService:
    def __init__(self, settings: Settings, store: CrawlStore | None = None):
        auth = None
        if settings.opensearch_user:
            auth = (settings.opensearch_user, settings.opensearch_password)
        self.settings = settings
        self.store = store
        self.client = OpenSearch(
            hosts=[settings.opensearch_url],
            http_auth=auth,
            use_ssl=settings.opensearch_url.startswith("https"),
            verify_certs=False,
            timeout=30,
            pool_maxsize=10,
        )

    def search(
        self,
        query: str,
        size: int = 20,
        filters: list[str] | None = None,
        source_scope: str = "portal",
        track: bool = True,
    ) -> dict:
        query = query.strip()
        active_filters = [item.strip() for item in (filters or []) if item.strip()]
        source_scope = self._normalize_source_scope(source_scope)
        if len(query) < 2:
            return {"total": 0, "results": []}

        if not self.client.indices.exists(index=self.settings.opensearch_index):
            return {"total": 0, "results": []}

        variants = query_variants(query)
        if self.store:
            variants.extend(self.store.synonym_variants_for(query))
        variants = [variant for variant in dict.fromkeys(variants) if variant.strip()]

        should = []
        for variant in variants:
            should.extend(
                [
                    {
                        "multi_match": {
                            "query": variant,
                            "fields": [
                                "title^7",
                                "metadata.link_text^7",
                                "metadata.source_title^3",
                                "metadata.link_context^3",
                                "url^3",
                                "content^1.2",
                            ],
                            "type": "best_fields",
                            "operator": "and",
                        }
                    },
                    {"match_phrase": {"title": {"query": variant, "boost": 8}}},
                    {"match_phrase": {"metadata.link_text": {"query": variant, "boost": 8}}},
                    {"match_phrase": {"url": {"query": variant, "boost": 5}}},
                    {"match_phrase": {"content": {"query": variant, "boost": 4}}},
                    {
                        "multi_match": {
                            "query": variant,
                            "fields": ["title^4", "metadata.link_text^4", "metadata.link_context^2", "content"],
                            "fuzziness": "AUTO",
                            "prefix_length": 2,
                        }
                    },
                ]
            )

        query_body = {
            "bool": {
                "should": should,
                "minimum_should_match": 1,
            }
        }
        category_filters = self._category_filters(active_filters)
        if category_filters:
            query_body["bool"]["filter"] = category_filters
        source_filter = self._source_scope_filter(source_scope)
        if source_filter:
            query_body["bool"].setdefault("filter", []).append(source_filter)

        functions = [
            {"filter": {"term": {"source_type": "application_link"}}, "weight": 28},
            {"filter": {"term": {"source_type": "document_link"}}, "weight": 16},
            {"filter": {"term": {"source_type": "tab_link"}}, "weight": 14},
            {"filter": {"term": {"source_type": "navigation_link"}}, "weight": 8},
            {"filter": {"term": {"content_type": "application/pdf"}}, "weight": 6},
        ]
        if wants_application(query):
            functions.append({"filter": {"term": {"source_type": "application_link"}}, "weight": 120})

        query_body = {
            "function_score": {
                "query": query_body,
                "functions": functions,
                "score_mode": "sum",
                "boost_mode": "sum",
            }
        }

        body = {
            "size": min(max(size, 1), 100),
            "query": query_body,
            "highlight": {
                "pre_tags": ["<mark>"],
                "post_tags": ["</mark>"],
                "fields": {
                    "title": {"number_of_fragments": 0},
                    "metadata.link_text": {"number_of_fragments": 0},
                    "metadata.link_context": {"fragment_size": 160, "number_of_fragments": 1},
                    "content": {"fragment_size": 180, "number_of_fragments": 3},
                },
            },
        }

        try:
            response = self.client.search(index=self.settings.opensearch_index, body=body)
        except NotFoundError:
            return {"total": 0, "results": []}
        except OpenSearchException as exc:
            return {"total": 0, "results": [], "error": str(exc)}
        hits = response.get("hits", {})
        results = []
        for hit in hits.get("hits", []):
            source = hit.get("_source", {})
            highlight = hit.get("highlight", {})
            snippets = highlight.get("content", [])
            category = self._result_category(source)
            open_url = self._open_url(source)
            results.append(
                {
                    "score": hit.get("_score", 0),
                    "url": source.get("url", ""),
                    "open_url": open_url,
                    "display_url": self._display_url(source),
                    "title": source.get("title", "") or source.get("url", ""),
                    "content_type": source.get("content_type", ""),
                    "source_type": source.get("source_type", ""),
                    "source_label": self._source_label(source),
                    "document_id": source.get("document_id", ""),
                    "category": category,
                    "kind_label": self._kind_label(source, category),
                    "why": self._why(source, highlight),
                    "metadata": source.get("metadata", {}),
                    "fetched_at": source.get("fetched_at", ""),
                    "snippet": " ... ".join(snippets) if snippets else source.get("content", "")[:360],
                }
            )
        total = hits.get("total", {}).get("value", 0)
        if self.store and track:
            self.store.record_search(
                query=query,
                filters=[*active_filters, f"source:{source_scope}"],
                total_results=total,
                top_url=results[0]["url"] if results else "",
            )
        return {
            "total": total,
            "results": results,
            "filters": active_filters,
            "source_scope": source_scope,
            "variants": variants[:12],
        }

    def suggest(self, query: str, size: int = 8, source_scope: str = "portal") -> dict:
        query = query.strip()
        source_scope = self._normalize_source_scope(source_scope)
        if len(query) < 1:
            return {"suggestions": []}

        suggestions: list[str] = []
        if self.store:
            suggestions.extend(self.store.synonym_suggestions(query, limit=size))

        if self.client.indices.exists(index=self.settings.opensearch_index):
            body_query = {
                "bool": {
                    "should": [
                        {"match_phrase_prefix": {"title": {"query": query, "boost": 4}}},
                        {"match_phrase_prefix": {"metadata.link_text": {"query": query, "boost": 5}}},
                        {"match_phrase_prefix": {"content": {"query": query}}},
                    ],
                    "minimum_should_match": 1,
                }
            }
            source_filter = self._source_scope_filter(source_scope)
            if source_filter:
                body_query["bool"].setdefault("filter", []).append(source_filter)
            body = {
                "size": min(max(size * 2, 1), 20),
                "_source": ["title", "metadata.link_text"],
                "query": body_query,
            }
            try:
                response = self.client.search(index=self.settings.opensearch_index, body=body)
                for hit in response.get("hits", {}).get("hits", []):
                    source = hit.get("_source", {})
                    metadata = source.get("metadata", {}) or {}
                    for value in (metadata.get("link_text"), source.get("title")):
                        if value and len(value) <= 120:
                            suggestions.append(value)
            except (NotFoundError, OpenSearchException):
                pass

        return {"suggestions": list(dict.fromkeys(suggestions))[:size]}

    def link_source_urls(self, limit: int = 5000) -> list[str]:
        if not self.client.indices.exists(index=self.settings.opensearch_index):
            return []
        body = {
            "size": min(max(limit, 1), 10000),
            "_source": ["metadata.source_url"],
            "query": {"wildcard": {"source_type": "*_link"}},
        }
        try:
            response = self.client.search(index=self.settings.opensearch_index, body=body)
        except (NotFoundError, OpenSearchException):
            return []
        urls = []
        for hit in response.get("hits", {}).get("hits", []):
            url = ((hit.get("_source", {}).get("metadata") or {}).get("source_url") or "").strip()
            if url:
                urls.append(url)
        return list(dict.fromkeys(urls))

    @staticmethod
    def _category_filters(filters: list[str]) -> list[dict]:
        should = []
        for item in filters:
            if item == "pages":
                should.append({"bool": {"must": [{"term": {"source_type": "web"}}], "should": [{"term": {"content_type": "text/html"}}, {"term": {"content_type": ""}}], "minimum_should_match": 1}})
            elif item == "documents":
                should.append({"bool": {"must_not": [{"term": {"content_type": "text/html"}}, {"term": {"content_type": "text/x-portal-link"}}]}})
            elif item == "pdf":
                should.append({"bool": {"should": [{"term": {"content_type": "application/pdf"}}, {"wildcard": {"url.raw": "*.pdf*"}}], "minimum_should_match": 1}})
            elif item == "word":
                should.append({"bool": {"should": [{"term": {"content_type": "application/msword"}}, {"term": {"content_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"}}, {"wildcard": {"url.raw": "*.doc*"}}], "minimum_should_match": 1}})
            elif item == "excel":
                should.append({"bool": {"should": [{"term": {"content_type": "application/vnd.ms-excel"}}, {"term": {"content_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}}, {"wildcard": {"url.raw": "*.xls*"}}], "minimum_should_match": 1}})
            elif item == "powerpoint":
                should.append({"bool": {"should": [{"term": {"content_type": "application/vnd.ms-powerpoint"}}, {"term": {"content_type": "application/vnd.openxmlformats-officedocument.presentationml.presentation"}}, {"wildcard": {"url.raw": "*.ppt*"}}], "minimum_should_match": 1}})
            elif item == "applications":
                should.append({"term": {"source_type": "application_link"}})
            elif item == "links":
                should.append({"wildcard": {"source_type": "*_link"}})
        return [{"bool": {"should": should, "minimum_should_match": 1}}] if should else []

    @staticmethod
    def _normalize_source_scope(source_scope: str) -> str:
        value = (source_scope or "portal").strip().lower()
        if value in {"all", "everything", "extended"}:
            return "all"
        if value in {"filesystem", "files", "local", "network"}:
            return "filesystem"
        return "portal"

    def _source_scope_filter(self, source_scope: str) -> dict | None:
        if source_scope == "all":
            return None
        if source_scope == "filesystem":
            return {"term": {"source_type": "filesystem"}}
        portal_prefix = self.settings.start_url.rstrip("/")
        return {
            "bool": {
                "should": [
                    {"term": {"metadata.source_kind.keyword": "portal"}},
                    {
                        "bool": {
                            "must_not": [{"exists": {"field": "metadata.source_kind"}}],
                            "should": [
                                {"prefix": {"url.raw": portal_prefix}},
                                {"wildcard": {"source_type": "*_link"}},
                            ],
                            "minimum_should_match": 1,
                        }
                    },
                ],
                "minimum_should_match": 1,
            }
        }

    @staticmethod
    def _result_category(source: dict) -> str:
        source_type = source.get("source_type", "")
        content_type = source.get("content_type", "")
        url = source.get("url", "").lower()
        if source_type == "application_link":
            return "applications"
        if source_type.endswith("_link"):
            return "links"
        if content_type == "application/pdf" or ".pdf" in url:
            return "pdf"
        if "wordprocessingml" in content_type or "msword" in content_type or ".doc" in url:
            return "word"
        if "spreadsheetml" in content_type or "excel" in content_type or ".xls" in url:
            return "excel"
        if "presentationml" in content_type or "powerpoint" in content_type or ".ppt" in url:
            return "powerpoint"
        if content_type.startswith("text/html") or source_type == "web":
            return "pages"
        return "documents"

    @staticmethod
    def _kind_label(source: dict, category: str) -> str:
        labels = {
            "applications": "Application",
            "links": "Portal link",
            "pdf": "PDF",
            "word": "Word",
            "excel": "Excel",
            "powerpoint": "PowerPoint",
            "pages": "Portal page",
            "documents": "Document",
        }
        return labels.get(category, source.get("source_type") or "Result")

    @staticmethod
    def _source_label(source: dict) -> str:
        metadata = source.get("metadata", {}) or {}
        if source.get("source_type") == "filesystem":
            return metadata.get("source_name") or "Local/Network file"
        if metadata.get("source_kind") == "web":
            return metadata.get("source_name") or "Web source"
        if str(source.get("source_type", "")).endswith("_link"):
            return "Portal link"
        return "Portal"

    @staticmethod
    def _open_url(source: dict) -> str:
        if source.get("source_type") == "filesystem" and source.get("document_id"):
            return f"/api/files/{source.get('document_id')}"
        return source.get("url", "")

    @staticmethod
    def _display_url(source: dict) -> str:
        if source.get("source_type") == "filesystem":
            metadata = source.get("metadata", {}) or {}
            local_root = metadata.get("local_root")
            title = source.get("title") or PathLikeName.name_from_path(source.get("file_path", ""))
            return f"{local_root} / {title}" if local_root else f"Local file / {title}"
        return source.get("url", "")

    @staticmethod
    def _why(source: dict, highlight: dict) -> str:
        if highlight.get("title") or highlight.get("metadata.link_text"):
            return "Matched title/link text"
        if highlight.get("metadata.link_context"):
            return "Matched link context"
        if source.get("source_type") == "application_link":
            return "Application link indexed from portal"
        if source.get("content_type") == "text/x-portal-link":
            return "Actionable portal link"
        if source.get("source_type") == "filesystem":
            return "Matched indexed local/network file"
        return "Matched document/page text"


class PathLikeName:
    @staticmethod
    def name_from_path(value: str) -> str:
        return value.replace("\\", "/").rstrip("/").split("/")[-1] if value else ""
