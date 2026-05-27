# Portal Search Agent

External search portal for `http://251gna/wp_root`.

This app does not install anything inside WordPress. It runs separately, crawls the portal as an HTTP client, extracts text from pages and documents, and stores everything in its own OpenSearch index.

The crawler also indexes actionable page elements such as menu links, application links, document links, iframe/frame links, and hash-tab links. These appear as normal search results, but their result URL is the real target URL, so users can click a result like a hospital application and open that application directly.

The search UI includes result filters, autocomplete suggestions, result feedback, click analytics, and clearer result cards. The admin panel includes diagnostics, failed URL visibility, targeted requeue tools, a managed synonym dictionary, query analytics, and health reports.

## Roles

- Public users use only the search page: `http://localhost:8080/`
- WordPress iframe/menu integration should use: `http://localhost:8080/embed`
- The administrator uses the control panel: `http://localhost:8080/admin`
- Only the admin panel can start, stop, or rebuild crawling.
- Start/Stop/Rebuild actions require admin login.

Default local admin login:

```text
username: admin
password: local-admin-change-me
```

Change `ADMIN_USERNAME`, `ADMIN_PASSWORD`, and `ADMIN_SESSION_SECRET` in `.env` before production use.

## Fast local test

From this project folder:

```powershell
.\scripts\start-portal.ps1
```

This will:

- create `.env` from `.env.example` if missing
- build/start OpenSearch, Tika, and the app
- open `http://localhost:8080/admin`

In the admin page:

1. Login with the admin username/password from `.env`.
2. Click **Full Rebuild** for the first crawl.
3. Watch progress in **Knowledge Build**.
4. Open **Search** or `http://localhost:8080/` to test queries.

Stop everything:

```powershell
.\scripts\stop-portal.ps1
```

## User URLs

Use these for normal staff users:

```text
http://localhost:8080/
http://localhost:8080/embed
```

They do not show crawler controls.

Use this only for administrators:

```text
http://localhost:8080/admin
```

## WordPress portal integration

Recommended simple approach:

1. In WordPress admin, add a menu item or button named `Portal Search`.
2. Point it to:

```text
http://search-server:8080/embed
```

For a WordPress page iframe:

```html
<iframe src="http://search-server:8080/embed" style="width:100%;height:900px;border:0;" title="Portal Search"></iframe>
```

For a plain button/link:

```html
<a class="portal-search-button" href="http://search-server:8080/embed">Portal Search</a>
```

Replace `http://search-server:8080` with the final server address. Set the same address in `.env`:

```env
PUBLIC_BASE_URL=http://search-server:8080
```

The admin panel also shows these snippets under **Portal Integration**.

## Docker services

- `app`: FastAPI portal and crawler control API
- `opensearch`: search index
- `tika`: document extraction and OCR container

Manual start:

```powershell
docker compose up -d --build opensearch tika app
```

Manual stop:

```powershell
docker compose down
```

## Production Docker deployment

The production stack is isolated from WordPress. Docker runs the app, OpenSearch, Tika, and the optional scheduler. The app is bound only to the local host as an upstream service, and the existing production Nginx terminates HTTPS on the intranet.

Prepare the production environment on the server:

```bash
cp .env.production.example .env.production
mkdir -p data
```

Edit `.env.production` before starting:

```env
PORTAL_HOST_IP=10.4.51.65
PUBLIC_BASE_URL=https://10.4.51.232:18443
APP_UPSTREAM_PORT=18085
OPENSEARCH_INITIAL_ADMIN_PASSWORD=<long-random-bootstrap-password>
ADMIN_USERNAME=admin
ADMIN_PASSWORD=<long-random-password>
ADMIN_SESSION_SECRET=<long-random-secret>
ADMIN_TOKEN=<long-random-token>
ADMIN_COOKIE_SECURE=true
```

Install the production Nginx site on the server:

```bash
sudo mkdir -p /etc/ssl/portal-search
sudo cp deploy/nginx/portal-search.conf /etc/nginx/sites-available/portal-search.conf
sudo ln -sfn ../sites-available/portal-search.conf /etc/nginx/sites-enabled/portal-search.conf
sudo nginx -t
sudo systemctl reload nginx
```

The Nginx site expects TLS files at `/etc/ssl/portal-search/portal-search.crt` and `/etc/ssl/portal-search/portal-search.key`.

On Linux, if the app cannot write to `data/`, grant the app container UID access:

```bash
sudo chown -R 10001:10001 data
```

Start production:

```bash
docker compose -f docker-compose.prod.yml up -d --build
```

Start the nightly scheduler profile when you want automated incremental crawls:

```bash
docker compose -f docker-compose.prod.yml --profile scheduler up -d scheduler
```

Smoke test:

```powershell
.\scripts\production-smoke-test.ps1 -BaseUrl https://10.4.51.232:18443
```

## Nightly crawling

For production nightly runs:

```powershell
docker compose --profile scheduler up -d scheduler
```

Schedule is configured in `.env`:

```env
CRAWL_CRON=0 2 * * *
SCHEDULER_RESET_EACH_RUN=false
```

With `SCHEDULER_RESET_EACH_RUN=false`, the scheduler performs an incremental run and refreshes only the seed portal URLs/sitemaps so it can discover new or changed links without rebuilding the whole index. Each nightly run stores a health report in the admin diagnostics area.

## Incremental resume

Use incremental resume when the crawl is almost complete and you only want to finish queued or interrupted URLs:

```powershell
.\scripts\resume-crawl.ps1
```

This calls `POST /api/crawl/start` without `reset=true` and without `recreate_index=true`.

Incremental resume:

- keeps the existing OpenSearch index
- keeps already completed URLs as `done`
- requeues stale `processing` URLs after a service restart
- processes only remaining/newly discovered queued URLs

Full rebuild is only for cases where already indexed pages must be reprocessed from scratch.

Search ranking/query rules normally need only an app rebuild/restart, not a crawl. Crawler extraction rules affect future crawled URLs; if they must apply to pages already marked `done`, use either a full rebuild or a targeted requeue for the affected URLs.

## Search tuning and diagnostics

The admin panel provides:

- filters and improved result cards in the Search tab
- autocomplete suggestions from indexed titles/link text and synonyms
- managed synonyms/variants for hospital acronyms and mixed Greek/Latin spellings
- diagnostics for indexed documents, content types, source types, failed URLs, and large documents
- targeted requeue for failed URLs, one URL, documents, links, and applications
- query analytics, zero-result queries, click tracking, and user feedback
- health report generation for OpenSearch, Tika, crawl stats, and failed coverage

## Full coverage note

The web crawler finds pages and files that are reachable through links or sitemaps. If there are files in `wp-content/uploads` that are not linked anywhere, no external web crawler can reliably discover them.

For full coverage, mount the uploads/document folder read-only and run filesystem ingest:

```powershell
docker compose run --rm app python -m portal_search_agent.cli ingest-path /app/data/uploads --base-url http://251gna/wp_root/wp-content/uploads
```

This still does not modify WordPress.

## Extended knowledge base sources

The user-facing search is portal-first by default. The embedded page sends `source=portal`, so the existing Portal Search behavior stays stable even if extra sources are added later.

Admins and users can switch the source selector to:

- `Portal only`: portal pages, portal documents, and actionable links discovered from the portal
- `All knowledge base`: portal plus any configured extra sources
- `Files only`: local or network files indexed through filesystem ingest

Local/network files should be mounted read-only into the app container and listed in `EXTRA_FILE_ROOTS`. Search results for those files open through the app endpoint `/api/files/{document_id}` instead of `file://` paths, so browsers can open them from the intranet page. The endpoint only serves files that are already indexed as `filesystem` and are inside configured `EXTRA_FILE_ROOTS`.

The admin **Knowledge Base Build** controls use the same source language:

- enabled `portal` / `web` sources run the HTTP crawler
- enabled `filesystem` sources run local/network folder ingest
- `database` sources can be registered now, but need a source-specific adapter before they ingest rows

The target is the configured OpenSearch index (`OPENSEARCH_INDEX`). Full rebuild is intentionally limited to `All configured sources`, because it recreates the whole target index.

Admins can add sources in the **Configured Knowledge Sources** section. Supported source locations:

- portal or web app: `http://host/path`
- local/network files: a folder path mounted read-only inside the app container
- database: DSN/connection descriptor for a future adapter

The knowledge database folder is controlled by `DATA_DIR`; backups are written to `KNOWLEDGE_BACKUP_DIR`.

## Important settings

```env
START_URL=http://251gna/wp_root
ALLOWED_HOSTS=251gna
ROOT_PATH=/wp_root
ADMIN_TOKEN=local-admin-change-me
ADMIN_USERNAME=admin
ADMIN_PASSWORD=local-admin-change-me
ADMIN_SESSION_SECRET=local-admin-change-me
PUBLIC_BASE_URL=http://localhost:8080
CONCURRENCY=4
REQUEST_DELAY_SECONDS=0.25
MAX_FILE_MB=250
```

For heavier night crawls:

```env
CONCURRENCY=8
REQUEST_DELAY_SECONDS=0
REQUEST_TIMEOUT_SECONDS=120
MAX_FILE_MB=500
```

If Docker cannot resolve hostname `251gna`, use the portal IP in `START_URL` or add `extra_hosts` in `docker-compose.yml`.

## API endpoints

- Public search page: `GET /`
- Embed search page: `GET /embed`
- Admin panel: `GET /admin`
- Admin session: `GET /api/admin/session`
- Admin login/logout: `POST /api/admin/login`, `POST /api/admin/logout`
- Search API: `GET /api/search?q=...&size=30&types=pdf,word,applications`
- Suggestions: `GET /api/suggest?q=...`
- Click tracking: `POST /api/search/click`
- Feedback: `POST /api/search/feedback`
- Crawl status: `GET /api/crawl/status`
- Start crawl: `POST /api/crawl/start`
- Stop crawl: `POST /api/crawl/stop`
- Targeted requeue: `POST /api/crawl/requeue`
- Admin diagnostics: `GET /api/admin/diagnostics`
- Synonyms: `GET/POST/DELETE /api/admin/synonyms`
- Health report: `POST /api/admin/health-report`
- Health: `GET /health`
