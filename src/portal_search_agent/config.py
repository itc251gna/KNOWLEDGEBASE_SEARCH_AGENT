from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv


load_dotenv()


def _csv_env(name: str, default: str = "") -> list[str]:
    raw = os.getenv(name, default)
    return [item.strip() for item in raw.split(",") if item.strip()]


def _bool_env(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    start_url: str = os.getenv("START_URL", "http://251gna/wp_root")
    allowed_hosts: list[str] = field(default_factory=lambda: _csv_env("ALLOWED_HOSTS", "251gna"))
    root_path: str = os.getenv("ROOT_PATH", "/wp_root")

    opensearch_url: str = os.getenv("OPENSEARCH_URL", "http://localhost:9200")
    opensearch_index: str = os.getenv("OPENSEARCH_INDEX", "portal_knowledge_base")
    opensearch_user: str = os.getenv("OPENSEARCH_USER", "")
    opensearch_password: str = os.getenv("OPENSEARCH_PASSWORD", "")

    tika_url: str = os.getenv("TIKA_URL", "http://localhost:9998").rstrip("/")
    ocr_languages: str = os.getenv("OCR_LANGUAGES", "ell+eng")

    data_dir: Path = Path(os.getenv("DATA_DIR", "./data"))
    cache_dir: Path = Path(os.getenv("CACHE_DIR", "./data/cache"))
    knowledge_backup_dir_raw: str = os.getenv("KNOWLEDGE_BACKUP_DIR", "")
    cache_raw_files: bool = _bool_env("CACHE_RAW_FILES", True)

    max_depth: int = int(os.getenv("MAX_DEPTH", "100"))
    max_pages: int = int(os.getenv("MAX_PAGES", "0"))
    max_file_mb: int = int(os.getenv("MAX_FILE_MB", "250"))
    concurrency: int = int(os.getenv("CONCURRENCY", "4"))
    request_delay_seconds: float = float(os.getenv("REQUEST_DELAY_SECONDS", "0.25"))
    request_timeout_seconds: float = float(os.getenv("REQUEST_TIMEOUT_SECONDS", "60"))
    user_agent: str = os.getenv("USER_AGENT", "PortalSearchAgent/1.0 internal-hospital-search")
    public_base_url: str = os.getenv("PUBLIC_BASE_URL", "http://localhost:8080").rstrip("/")
    admin_token: str = os.getenv("ADMIN_TOKEN") or "local-admin-change-me"
    admin_username: str = os.getenv("ADMIN_USERNAME", "admin")
    admin_password: str = os.getenv("ADMIN_PASSWORD") or os.getenv("ADMIN_TOKEN") or "local-admin-change-me"
    admin_session_secret: str = os.getenv("ADMIN_SESSION_SECRET") or os.getenv("ADMIN_TOKEN") or "local-admin-change-me"
    admin_session_hours: int = int(os.getenv("ADMIN_SESSION_HOURS", "12"))
    admin_cookie_secure: bool = _bool_env("ADMIN_COOKIE_SECURE", False)

    crawl_cron: str = os.getenv("CRAWL_CRON", "0 2 * * *")
    scheduler_reset_each_run: bool = _bool_env("SCHEDULER_RESET_EACH_RUN", False)
    extra_file_roots: list[str] = field(default_factory=lambda: _csv_env("EXTRA_FILE_ROOTS", ""))
    exclude_patterns: list[str] = field(
        default_factory=lambda: _csv_env(
            "EXCLUDE_PATTERNS",
            "/wp-admin,/wp-login.php,/xmlrpc.php,/wp-json/,?feed=,/feed/,/comments/feed,?s=",
        )
    )

    @property
    def sqlite_path(self) -> Path:
        return self.data_dir / "crawler.sqlite3"

    @property
    def max_file_bytes(self) -> int:
        return self.max_file_mb * 1024 * 1024

    @property
    def knowledge_backup_dir(self) -> Path:
        return Path(self.knowledge_backup_dir_raw) if self.knowledge_backup_dir_raw else self.data_dir / "backups"


def get_settings() -> Settings:
    settings = Settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.cache_dir.mkdir(parents=True, exist_ok=True)
    if settings.cache_raw_files:
        (settings.cache_dir / "files").mkdir(parents=True, exist_ok=True)
    settings.knowledge_backup_dir.mkdir(parents=True, exist_ok=True)
    return settings
