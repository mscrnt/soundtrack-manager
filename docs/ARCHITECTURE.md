# Architecture

One Python process and one SQLite file in the app container, plus the FlareSolverr container (on by default). There is no Celery, Redis or
Postgres. One user's download queue doesn't need them.

```
┌──────────────── container (non-root, read-only rootfs, no caps) ───────────────┐
│                                                                                │
│  uvicorn ── FastAPI (web/app.py) ── Jinja2 + HTMX pages                        │
│                │                                                               │
│                ├── DiscoveryService (discovery.py) ─┐                          │
│                ├── queue.py (jobs table)            │                          │
│                ├── library.py · ai.py (optional)    │                          │
│                └── DownloadWorker (downloader.py)   │ asyncio task, same       │
│                                                     │ process, polls the DB    │
│                     KhinsiderClient (client.py) ◄───┘                          │
│                       │ parse.py (pure HTML → dataclasses)                     │
│                       ▼                                                        │
│                     AutoFetcher (fetch.py)                                     │
│                       ├── HttpFetcher (httpx) ──── Cloudflare check? ─┐        │
│                       ├── FlareSolverrFetcher ──► flaresolverr:8191 ◄─┤ (default)
│                       └── BrowserFetcher (Playwright profile) ◄───────┘ fallback│
│                       shared RateLimiter, retries + backoff                    │
│                                                                                │
│  /data       soundtrack-manager.sqlite3 · browser-profile/ · logs/app.log      │
│  /downloads  <template>/<album>/01 - Track.flac, cover.jpg, album_info.json    │
└────────────────────────────────────────────────────────────────────────────────┘
```

## Modules

| Module | Role |
|---|---|
| `khinsider/urls.py` | URL builders and host validation (site + CDN only) |
| `khinsider/catalog.py` | Platform / album type / sort tables captured from the site |
| `khinsider/parse.py` | Pure parsers that work from headers and labels and raise `LayoutChangedError` when the structure is missing |
| `khinsider/fetch.py` | `HttpFetcher`, `FlareSolverrFetcher`, `BrowserFetcher`, `AutoFetcher` (HTTP → FlareSolverr → browser), `RateLimiter`, `AccessBlocked` |
| `khinsider/client.py` | search / browse (all pages) / album / track-files |
| `discovery.py` | Site filters where they exist, local filters otherwise. SQLite cache with TTL. Search history |
| `queue.py` | Persistent jobs: enqueue (de-duplicated), atomic claim, state changes, crash recovery |
| `downloader.py` | `DownloadWorker`: resumable, verified downloads, progress/speed, pause/cancel, conflict protection |
| `library.py` | Folder scan and verify, duplicates, missing albums, account-history import, recovery |
| `ai.py` | Optional LLM → validated search plan (titles + filters) |
| `paths.py` | Folder templates, Windows/Linux-safe names, traversal-proof `safe_join` |
| `config.py` | Settings from env. `Secret` values come from `*_FILE` or `/run/secrets` |
| `runtime.py` | `AppContext`, which wires everything for both the CLI and the web app |
| `web/` | FastAPI app, security middleware, templates, vendored Bootstrap/HTMX |
| `cli.py` | argparse + Rich CLI, including interactive selection |

## Data model (migration `0001_initial.sql`)

* `albums`: one row per slug. Summary fields come from listings; detail fields
  come from the album page (`detail_fetched_at`). Upserts never overwrite a
  known value with NULL.
* `tracks`: rows from `#songlist`, with per-format sizes as JSON. IDs stay the
  same across refreshes.
* `listings` + `listing_albums`: the cached remote result of one search or one
  platform browse, in site order.
* `search_history`: each user query with its local filters and result count.
* `jobs`: the download queue. A partial unique index allows only one *active*
  job per album+format, so the same album queued from different searches can't
  duplicate.
* `job_files`: per-file progress, checksum and errors (used by the worker).
* `settings`: runtime overrides from the Settings page. `sync_state`: last sync
  timestamps.

## Decisions

* **Search vs. browse.** With text, use `/search`, which returns everything at
  once. Without text, browse each selected platform (500 per page, capped by
  `DISCOVER_MAX_PAGES`) and filter locally. Results are cached for
  `CACHE_TTL_HOURS`, so paging and re-filtering in the UI never contacts the site.
* **No invented metadata.** Track count, formats and sizes show "—" until the
  album page has been fetched (the Details button, or queueing).
* **Rate limiting.** One limiter covers every site page request, CLI or web,
  with a minimum `REQUEST_DELAY_SECONDS` gap. Retries cover timeouts, 429 and
  5xx, with exponential backoff and jitter and respect for `Retry-After`. A
  Cloudflare challenge is never retried.
* **Background work.** An asyncio worker in the uvicorn process
  claims `queued` jobs from SQLite. Each file is written to `*.part`, resumed
  with HTTP `Range`, checked against `Content-Length`, hashed (SHA-256), then
  renamed. On startup, `running` jobs return to `queued`
  (`queue.recover_interrupted`), so a container restart loses nothing. SIGTERM
  (uvicorn graceful shutdown, `init: true`, 30 s grace) lets in-flight chunks
  finish.
* **Security.** The UI listens on loopback by default. If it is published
  beyond loopback, the app refuses to start without `WEB_PASSWORD`. Mutating
  requests need the `HX-Request` header plus a matching `Origin` (CSRF). A
  strict CSP allows scripts only from `'self'`. Secrets are `Secret` objects,
  redacted in logs and shown only as set/unset. The browser profile directory is
  `0700`.

## Milestones

| # | Milestone | Status |
|---|---|---|
| 1 | Research site and existing tools | ✅ `docs/RESEARCH.md` |
| 2 | Docker environment and app skeleton | ✅ Dockerfile, compose (+ FlareSolverr), `.env.example`, migrations |
| 3 | FastAPI and web UI | ✅ Discover / Downloads (+ per-job page) / Library / Settings |
| 4 | Album discovery and filtering | ✅ search, multi-term, browse, filters, cache, details, CLI |
| 5 | Persistent download queue | ✅ `jobs`/`job_files`, atomic claim, pause/resume/cancel/retry, crash recovery |
| 6 | Authorized download handling | ✅ `downloader.py`: track pages → CDN, `.part` + Range resume, size check, SHA-256, cover, `album_info.json`, conflict protection |
| 7 | Library management and history import | ✅ `library.py`: scan/deep verify, duplicates, missing, account import, recovery queue |
| 8 | Optional AI-assisted discovery | ✅ `ai.py`: OpenAI-compatible local LLM → validated search plan |

Release automation: `.github/workflows/ci.yml` (tests on Linux and Windows, Docker build) and
`release.yml` (`v*` tag → multi-arch GHCR image + GitHub Release).
