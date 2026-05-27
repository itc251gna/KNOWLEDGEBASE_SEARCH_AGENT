from __future__ import annotations

import base64
import hashlib
import hmac
import json
import mimetypes
import time
import zipfile
from pathlib import Path
from urllib.parse import urlparse

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from .config import get_settings
from .crawl_manager import CrawlJobManager
from .db import CrawlStore
from .search import SearchService
from .urltools import in_scope, normalize_url, should_skip_url


ROOT_DIR = Path(__file__).resolve().parents[2]
WEB_DIR = ROOT_DIR / "web"

settings = get_settings()
templates = Jinja2Templates(directory=str(WEB_DIR / "templates"))
store = CrawlStore(settings.sqlite_path)
search_service = SearchService(settings, store)
crawl_manager = CrawlJobManager(settings, store)
crawl_manager.ensure_default_sources()

app = FastAPI(title="Portal Search Agent", version="1.0.0")
app.mount("/static", StaticFiles(directory=str(WEB_DIR / "static")), name="static")

ADMIN_SESSION_COOKIE = "portal_search_admin"


def admin_session_seconds() -> int:
    return max(settings.admin_session_hours, 1) * 3600


def sign_admin_session(username: str, expires_at: int) -> str:
    payload = f"{username}|{expires_at}"
    signature = hmac.new(
        settings.admin_session_secret.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return base64.urlsafe_b64encode(f"{payload}|{signature}".encode("utf-8")).decode("ascii")


def verify_admin_session(token: str) -> str:
    if not token:
        return ""
    try:
        decoded = base64.urlsafe_b64decode(token.encode("ascii")).decode("utf-8")
        username, expires_raw, signature = decoded.rsplit("|", 2)
        expires_at = int(expires_raw)
    except Exception:
        return ""
    if expires_at < int(time.time()):
        return ""
    expected = hmac.new(
        settings.admin_session_secret.encode("utf-8"),
        f"{username}|{expires_at}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(signature, expected):
        return ""
    return username if hmac.compare_digest(username, settings.admin_username) else ""


def current_admin_username(request: Request, x_admin_token: str = "") -> str:
    session_username = verify_admin_session(request.cookies.get(ADMIN_SESSION_COOKIE, ""))
    if session_username:
        return session_username
    if x_admin_token and hmac.compare_digest(x_admin_token, settings.admin_token):
        return settings.admin_username
    return ""


def require_admin(request: Request, x_admin_token: str = Header(default="")) -> None:
    if not current_admin_username(request, x_admin_token):
        raise HTTPException(status_code=401, detail="Admin login required")


def set_admin_cookie(response: Response, username: str) -> None:
    max_age = admin_session_seconds()
    token = sign_admin_session(username, int(time.time()) + max_age)
    response.set_cookie(
        key=ADMIN_SESSION_COOKIE,
        value=token,
        max_age=max_age,
        httponly=True,
        secure=settings.admin_cookie_secure,
        samesite="lax",
        path="/",
    )


class SynonymRequest(BaseModel):
    term: str
    variants: list[str] = []


class LoginRequest(BaseModel):
    username: str = ""
    password: str = ""


class ClickRequest(BaseModel):
    query: str = ""
    url: str
    title: str = ""
    source_type: str = ""


class FeedbackRequest(BaseModel):
    query: str = ""
    url: str = ""
    title: str = ""
    feedback_type: str = "not_useful"
    message: str = ""


class RequeueRequest(BaseModel):
    mode: str
    url: str = ""
    category: str = ""


class KbSourceRequest(BaseModel):
    id: str = ""
    type: str
    name: str = ""
    location: str
    enabled: bool = True
    options: dict = Field(default_factory=dict)


class KbBuildRequest(BaseModel):
    source_ids: list[str] = Field(default_factory=list)
    reset: bool = False
    recreate_index: bool = False


def parse_types(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def allowed_file_roots() -> list[Path]:
    roots = []
    crawl_manager.ensure_default_sources()
    configured_roots = settings.extra_file_roots + [
        source["location"]
        for source in store.list_kb_sources()
        if source.get("type") == "filesystem"
    ]
    for raw_root in dict.fromkeys(configured_roots):
        try:
            roots.append(Path(raw_root).resolve())
        except OSError:
            continue
    return roots


def is_allowed_indexed_file(path: Path) -> bool:
    try:
        resolved = path.resolve()
    except OSError:
        return False
    if not resolved.is_file():
        return False
    for root in allowed_file_roots():
        if resolved == root or resolved.is_relative_to(root):
            return True
    return False


def normalize_source_request(request: KbSourceRequest) -> dict:
    source_type = request.type.strip().lower()
    options = dict(request.options or {})
    if source_type in {"folder", "file"}:
        options.setdefault("path_kind", source_type)
        source_type = "filesystem"
    if source_type not in {"portal", "web", "filesystem", "database"}:
        raise HTTPException(status_code=400, detail="Source type must be portal, web, filesystem, file, folder, or database")
    location = request.location.strip()
    if not location:
        raise HTTPException(status_code=400, detail="Source location is required")
    if source_type in {"portal", "web"}:
        normalized = normalize_url(location)
        if not normalized:
            raise HTTPException(status_code=400, detail="Invalid source URL")
        parsed = urlparse(normalized)
        options.setdefault("allowed_hosts", [parsed.hostname] if parsed.hostname else [])
        options.setdefault("root_path", parsed.path or "/")
        location = normalized
    return {
        "source_id": request.id.strip(),
        "source_type": source_type,
        "name": request.name.strip() or location,
        "location": location,
        "enabled": request.enabled,
        "options": options,
    }


def enriched_kb_sources() -> list[dict]:
    crawl_manager.ensure_default_sources()
    sources = store.list_kb_sources()
    for source in sources:
        if source["type"] == "filesystem":
            path = Path(source["location"])
            source["exists"] = path.exists()
            path_kind = (source.get("options") or {}).get("path_kind", "")
            if path_kind == "file":
                source["readable"] = path.exists() and path.is_file()
            elif path_kind == "folder":
                source["readable"] = path.exists() and path.is_dir()
            else:
                source["readable"] = path.exists() and (path.is_dir() or path.is_file())
        elif source["type"] == "database":
            source["adapter_status"] = "registered_only"
            source["readable"] = False
        else:
            source["readable"] = True
    return sources


def create_kb_backup() -> dict:
    backup_dir = settings.knowledge_backup_dir
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    path = backup_dir / f"portal-search-kb-backup-{stamp}.zip"
    manifest = {
        "created_at": stamp,
        "data_dir": str(settings.data_dir),
        "sqlite_path": str(settings.sqlite_path),
        "opensearch_index": settings.opensearch_index,
        "opensearch_url": settings.opensearch_url,
        "note": "This backup contains local metadata/configuration. OpenSearch index snapshot/export is separate.",
    }
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        archive.writestr("kb_sources.json", json.dumps(store.list_kb_sources(), ensure_ascii=False, indent=2))
        if settings.sqlite_path.exists():
            archive.write(settings.sqlite_path, arcname="crawler.sqlite3")
    store.add_event(f"Knowledge backup created: {path}")
    return {"ok": True, "path": str(path), "filename": path.name}


def validate_requeue_url(url: str) -> str:
    normalized = normalize_url(url)
    if not normalized or should_skip_url(normalized):
        raise HTTPException(status_code=400, detail="Invalid URL")
    if not in_scope(normalized, settings.allowed_hosts, settings.root_path, settings.exclude_patterns):
        raise HTTPException(status_code=400, detail="URL is outside the configured portal scope")
    return normalized


@app.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        "index.html",
        {"request": request, "start_url": settings.start_url},
    )


@app.get("/admin", response_class=HTMLResponse)
def admin(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        "admin.html",
        {
            "request": request,
            "start_url": settings.start_url,
            "public_base_url": settings.public_base_url,
            "admin_username": settings.admin_username,
        },
    )


@app.get("/embed", response_class=HTMLResponse)
def embed(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        "embed.html",
        {"request": request, "start_url": settings.start_url},
    )


@app.get("/api/admin/session")
def admin_session(request: Request, x_admin_token: str = Header(default="")) -> dict:
    username = current_admin_username(request, x_admin_token)
    return {"authenticated": bool(username), "username": username}


@app.post("/api/admin/login")
def admin_login(request: LoginRequest, response: Response) -> dict:
    username = request.username.strip()
    password = request.password
    if not (
        hmac.compare_digest(username, settings.admin_username)
        and hmac.compare_digest(password, settings.admin_password)
    ):
        raise HTTPException(status_code=401, detail="Invalid admin username or password")
    set_admin_cookie(response, username)
    return {"ok": True, "username": username, "session_hours": settings.admin_session_hours}


@app.post("/api/admin/logout")
def admin_logout(response: Response) -> dict:
    response.delete_cookie(ADMIN_SESSION_COOKIE, path="/")
    return {"ok": True}


@app.get("/api/search")
def search(
    q: str = Query("", min_length=0),
    size: int = Query(20, ge=1, le=100),
    types: str = Query("", description="Comma-separated result filters"),
    source: str = Query("portal", description="portal, all, or filesystem"),
) -> dict:
    return search_service.search(q, size=size, filters=parse_types(types), source_scope=source)


@app.get("/api/suggest")
def suggest(
    q: str = Query("", min_length=0),
    size: int = Query(8, ge=1, le=20),
    source: str = Query("portal", description="portal, all, or filesystem"),
) -> dict:
    return search_service.suggest(q, size=size, source_scope=source)


@app.get("/api/files/{document_id}")
def open_indexed_file(document_id: str) -> FileResponse:
    document = store.document_by_id(document_id.strip())
    if not document or document.get("source_type") != "filesystem":
        raise HTTPException(status_code=404, detail="File not found")
    file_path = Path(str(document.get("file_path") or ""))
    if not is_allowed_indexed_file(file_path):
        raise HTTPException(status_code=404, detail="File not found")
    media_type = mimetypes.guess_type(file_path.name)[0] or str(document.get("content_type") or "application/octet-stream")
    return FileResponse(
        path=file_path,
        media_type=media_type,
        filename=file_path.name,
        content_disposition_type="inline",
    )


@app.post("/api/search/click")
def record_click(request: ClickRequest) -> dict:
    store.record_click(
        query=request.query,
        url=request.url,
        title=request.title,
        source_type=request.source_type,
    )
    return {"ok": True}


@app.post("/api/search/feedback")
def record_feedback(request: FeedbackRequest) -> dict:
    store.record_feedback(
        query=request.query,
        url=request.url,
        title=request.title,
        feedback_type=request.feedback_type,
        message=request.message,
    )
    return {"ok": True}


@app.get("/api/stats")
def stats() -> dict:
    return store.stats()


@app.get("/api/crawl/status")
def crawl_status() -> dict:
    return crawl_manager.status()


@app.get("/api/kb/sources")
def kb_sources(_: None = Depends(require_admin)) -> dict:
    return {
        "target": {
            "type": "opensearch",
            "index": settings.opensearch_index,
            "url": settings.opensearch_url,
        },
        "store": {
            "data_dir": str(settings.data_dir),
            "sqlite_path": str(settings.sqlite_path),
            "cache_dir": str(settings.cache_dir),
            "backup_dir": str(settings.knowledge_backup_dir),
        },
        "sources": enriched_kb_sources(),
    }


@app.post("/api/kb/sources")
def save_kb_source(request: KbSourceRequest, _: None = Depends(require_admin)) -> dict:
    source = normalize_source_request(request)
    source_id = store.save_kb_source(**source)
    store.add_event(f"Knowledge source saved: {source['source_type']} {source['name']}")
    return {"ok": True, "id": source_id, "sources": enriched_kb_sources()}


@app.delete("/api/kb/sources/{source_id}")
def delete_kb_source(source_id: str, _: None = Depends(require_admin)) -> dict:
    store.delete_kb_source(source_id)
    store.add_event(f"Knowledge source deleted: {source_id}")
    return {"ok": True, "sources": enriched_kb_sources()}


@app.post("/api/kb/build")
async def kb_build(request: KbBuildRequest, _: None = Depends(require_admin)) -> dict:
    if not request.source_ids:
        raise HTTPException(status_code=400, detail="Select at least one knowledge source")
    try:
        return await crawl_manager.start(
            reset=request.reset,
            recreate_index=request.recreate_index,
            source="all" if request.recreate_index else "selected",
            source_ids=request.source_ids,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/kb/backup")
def kb_backup(_: None = Depends(require_admin)) -> dict:
    return create_kb_backup()


@app.post("/api/crawl/start")
async def crawl_start(
    reset: bool = Query(False),
    recreate_index: bool = Query(False),
    source: str = Query("portal", description="portal, filesystem, or all"),
    _: None = Depends(require_admin),
) -> dict:
    try:
        return await crawl_manager.start(reset=reset, recreate_index=recreate_index, source=source)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/crawl/stop")
async def crawl_stop(_: None = Depends(require_admin)) -> dict:
    return await crawl_manager.stop()


@app.post("/api/crawl/requeue")
def crawl_requeue(request: RequeueRequest, _: None = Depends(require_admin)) -> dict:
    if request.mode == "failed":
        count = store.requeue_failed()
    elif request.mode == "url":
        count = store.requeue_url(validate_requeue_url(request.url))
    elif request.mode == "category":
        if request.category == "links":
            count = store.requeue_urls(search_service.link_source_urls())
        else:
            count = store.requeue_category(request.category)
    else:
        raise HTTPException(status_code=400, detail="Unknown requeue mode")
    store.add_event(f"Requeued {count} URLs ({request.mode}{':' + request.category if request.category else ''})")
    return {"ok": True, "requeued": count}


@app.get("/api/admin/diagnostics")
def diagnostics(_: None = Depends(require_admin)) -> dict:
    return store.diagnostics()


@app.get("/api/admin/synonyms")
def list_synonyms(_: None = Depends(require_admin)) -> dict:
    return {"synonyms": store.list_synonyms()}


@app.post("/api/admin/synonyms")
def save_synonym(request: SynonymRequest, _: None = Depends(require_admin)) -> dict:
    try:
        store.save_synonym(request.term, request.variants)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "synonyms": store.list_synonyms()}


@app.delete("/api/admin/synonyms/{term}")
def delete_synonym(term: str, _: None = Depends(require_admin)) -> dict:
    store.delete_synonym(term)
    return {"ok": True}


@app.post("/api/admin/health-report")
def create_health_report(_: None = Depends(require_admin)) -> dict:
    report = build_health_report()
    store.save_health_report(report["status"], report["summary"], json.dumps(report, ensure_ascii=False))
    return report


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


def build_health_report() -> dict:
    stats = store.stats()
    diagnostics_data = store.diagnostics(limit=10)
    opensearch_status = "unknown"
    tika_status = "unknown"
    try:
        opensearch_status = search_service.client.cluster.health(request_timeout=10).get("status", "unknown")
    except Exception as exc:
        opensearch_status = f"error: {exc}"
    try:
        with httpx.Client(timeout=10) as client:
            response = client.get(f"{settings.tika_url}/version")
            tika_status = "ok" if response.is_success else f"http {response.status_code}"
    except Exception as exc:
        tika_status = f"error: {exc}"

    failed = stats.get("failed", 0)
    queued = stats.get("queued", 0)
    status = "ok" if failed == 0 else "warning"
    summary = (
        f"{stats.get('documents', 0)} indexed documents, "
        f"{stats.get('done', 0)} done, "
        f"{failed} failed, "
        f"{queued} queued. "
        f"OpenSearch: {opensearch_status}. Tika: {tika_status}."
    )
    return {
        "status": status,
        "summary": summary,
        "stats": stats,
        "opensearch": opensearch_status,
        "tika": tika_status,
        "failed_urls": diagnostics_data.get("failed_urls", []),
        "zero_result_queries": diagnostics_data.get("analytics", {}).get("zero_result_queries", []),
    }
