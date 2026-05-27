from __future__ import annotations

import hashlib
import posixpath
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse


TRACKING_PREFIXES = ("utm_",)
TRACKING_NAMES = {"fbclid", "gclid", "replytocom"}

DOCUMENT_EXTENSIONS = {
    ".pdf",
    ".doc",
    ".docx",
    ".xls",
    ".xlsx",
    ".ppt",
    ".pptx",
    ".odt",
    ".ods",
    ".odp",
    ".rtf",
    ".txt",
    ".csv",
    ".xml",
}

HTML_EXTENSIONS = {"", ".html", ".htm", ".php", ".asp", ".aspx", ".jsp"}

SKIP_EXTENSIONS = {
    ".7z",
    ".avi",
    ".bmp",
    ".css",
    ".gif",
    ".gz",
    ".ico",
    ".jpeg",
    ".jpg",
    ".js",
    ".m4a",
    ".m4v",
    ".mov",
    ".mp3",
    ".mp4",
    ".mpeg",
    ".mpg",
    ".png",
    ".rar",
    ".svg",
    ".tar",
    ".tgz",
    ".ttf",
    ".wav",
    ".webm",
    ".webp",
    ".woff",
    ".woff2",
    ".zip",
}


def url_hash(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def content_hash(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8", errors="ignore")
    return hashlib.sha256(data).hexdigest()


def normalize_url(url: str, base_url: str | None = None, keep_fragment: bool = False) -> str | None:
    if base_url:
        url = urljoin(base_url, url)

    parsed = urlparse(url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None

    query_items = []
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        if key in TRACKING_NAMES or any(key.startswith(prefix) for prefix in TRACKING_PREFIXES):
            continue
        query_items.append((key, value))

    normalized_path = posixpath.normpath(parsed.path or "/")
    if parsed.path.endswith("/") and not normalized_path.endswith("/"):
        normalized_path += "/"
    if not normalized_path.startswith("/"):
        normalized_path = "/" + normalized_path

    return urlunparse(
        (
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            normalized_path,
            "",
            urlencode(sorted(query_items), doseq=True),
            parsed.fragment if keep_fragment else "",
        )
    )


def extension_for_url(url: str) -> str:
    path = urlparse(url).path.lower()
    if "." not in path.rsplit("/", 1)[-1]:
        return ""
    return "." + path.rsplit(".", 1)[-1]


def is_probably_document(url: str, content_type: str = "") -> bool:
    ext = extension_for_url(url)
    content_type = content_type.lower()
    if ext in DOCUMENT_EXTENSIONS:
        return True
    return any(
        marker in content_type
        for marker in (
            "application/pdf",
            "application/msword",
            "officedocument",
            "opendocument",
            "text/plain",
            "text/csv",
            "application/rtf",
        )
    )


def should_skip_url(url: str) -> bool:
    ext = extension_for_url(url)
    return ext in SKIP_EXTENSIONS


def in_scope(url: str, allowed_hosts: list[str], root_path: str, exclude_patterns: list[str]) -> bool:
    parsed = urlparse(url)
    if parsed.hostname not in {host.lower() for host in allowed_hosts}:
        return False

    root = "/" + root_path.strip("/")
    path = parsed.path or "/"
    if root != "/" and path != root and not path.startswith(root + "/"):
        return False

    test = path + (("?" + parsed.query) if parsed.query else "")
    for pattern in exclude_patterns:
        if pattern and pattern in test:
            return False

    return not should_skip_url(url)
