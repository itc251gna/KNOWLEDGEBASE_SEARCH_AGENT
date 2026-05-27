from __future__ import annotations

from opensearchpy import OpenSearch, helpers

from .config import Settings
from .models import ExtractedDocument
from .urltools import url_hash


class OpenSearchIndexer:
    def __init__(self, settings: Settings):
        self.settings = settings
        auth = None
        if settings.opensearch_user:
            auth = (settings.opensearch_user, settings.opensearch_password)
        self.client = OpenSearch(
            hosts=[settings.opensearch_url],
            http_auth=auth,
            use_ssl=settings.opensearch_url.startswith("https"),
            verify_certs=False,
            timeout=60,
            max_retries=3,
            retry_on_timeout=True,
            pool_maxsize=10,
        )

    def wait_until_ready(self) -> None:
        self.client.cluster.health(wait_for_status="yellow", request_timeout=120)

    def recreate_index(self) -> None:
        if self.client.indices.exists(index=self.settings.opensearch_index):
            self.client.indices.delete(index=self.settings.opensearch_index)
        self.ensure_index()

    def ensure_index(self) -> None:
        index = self.settings.opensearch_index
        if self.client.indices.exists(index=index):
            return

        body = {
            "settings": {
                "index": {"number_of_shards": 1, "number_of_replicas": 0},
                "analysis": {
                    "filter": {
                        "greek_stop": {"type": "stop", "stopwords": "_greek_"},
                        "greek_stemmer": {"type": "stemmer", "language": "greek"},
                    },
                    "analyzer": {
                        "portal_text": {
                            "type": "custom",
                            "tokenizer": "standard",
                            "filter": ["lowercase", "greek_stop", "greek_stemmer"],
                        }
                    },
                },
            },
            "mappings": {
                "properties": {
                    "url": {"type": "text", "analyzer": "portal_text", "fields": {"raw": {"type": "keyword"}}},
                    "title": {"type": "text", "analyzer": "portal_text", "fields": {"raw": {"type": "keyword"}}},
                    "content": {"type": "text", "analyzer": "portal_text"},
                    "content_type": {"type": "keyword"},
                    "source_type": {"type": "keyword"},
                    "status_code": {"type": "integer"},
                    "depth": {"type": "integer"},
                    "content_hash": {"type": "keyword"},
                    "fetched_at": {"type": "date"},
                    "modified_at": {"type": "date", "ignore_malformed": True},
                    "file_path": {"type": "keyword"},
                    "metadata": {"type": "object", "enabled": True},
                }
            },
        }
        self.client.indices.create(index=index, body=body)

    def index_document(self, document: ExtractedDocument) -> None:
        self.ensure_index()
        self.client.index(
            index=self.settings.opensearch_index,
            id=document.document_id or url_hash(document.url),
            body=self._body(document),
        )

    def bulk_index(self, documents: list[ExtractedDocument]) -> None:
        if not documents:
            return
        self.ensure_index()
        actions = [
            {
                "_op_type": "index",
                "_index": self.settings.opensearch_index,
                "_id": document.document_id or url_hash(document.url),
                "_source": self._body(document),
            }
            for document in documents
        ]
        helpers.bulk(self.client, actions)

    @staticmethod
    def _body(document: ExtractedDocument) -> dict:
        return {
            "url": document.url,
            "document_id": document.document_id or url_hash(document.url),
            "title": document.title,
            "content": document.text,
            "content_type": document.content_type,
            "source_type": document.source_type,
            "status_code": document.status_code,
            "depth": document.depth,
            "content_hash": document.content_hash,
            "fetched_at": document.fetched_at,
            "modified_at": document.modified_at,
            "file_path": str(document.file_path or ""),
            "metadata": document.metadata,
        }
