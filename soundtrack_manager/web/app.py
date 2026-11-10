"""FastAPI + Jinja2 + HTMX web interface."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlencode

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import __version__, queue
from ..config import FORMATS
from ..discovery import QueryError, SearchQuery
from ..khinsider.catalog import ALBUM_TYPES, PLATFORMS, SEARCH_SORTS
from ..khinsider.fetch import AccessBlocked, FetchError
from ..khinsider.parse import LayoutChangedError, size_to_bytes
from ..logging_setup import tail
from ..paths import TEMPLATE_FIELDS, UnsafePathError, render_album_dir
from ..runtime import AppContext
from .security import SecurityMiddleware

log = logging.getLogger(__name__)
HERE = Path(__file__).parent
PER_PAGE_CHOICES = (25, 50, 100)


def _human_bytes(n: int | None) -> str:
    if not n:
        return "—"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return str(n)


def _error_message(exc: Exception) -> tuple[str, str]:
    if isinstance(exc, AccessBlocked):
        return "warning", f"Access check: {exc}"
    if isinstance(exc, LayoutChangedError):
        return "danger", f"The site's page layout was not recognised (it may have changed): {exc}"
    if isinstance(exc, (FetchError, QueryError, UnsafePathError, ValueError)):
        return "danger", str(exc)
    return "danger", f"Unexpected error: {type(exc).__name__}"


def create_app(ctx: AppContext | None = None) -> FastAPI:
    """App factory (``uvicorn --factory soundtrack_manager.web.app:create_app``)."""
    holder: dict[str, AppContext] = {"ctx": ctx or AppContext()}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        ctx_ = holder["ctx"]
        if ctx_.settings.worker_enabled:
            from ..downloader import DownloadWorker
            ctx_.worker = DownloadWorker(ctx_)
            ctx_.worker.start()
        resumed = ctx_.resume_import_matching()
        if resumed:
            log.info("continuing to match %d import list(s)", resumed)
        s = ctx_.base_settings
        access = "token" if s.api_token else ("web login" if s.auth_enabled else "localhost only")
        log.info("web UI ready (auth %s, worker %s, API %s, MCP %s)", "on" if s.auth_enabled else "off",
                 "on" if ctx_.worker else "off", f"on, {access}" if s.api_enabled else "off",
                 f"on at /mcp, {access}" if s.mcp_enabled else "off")
        try:
            if mcp_app is not None:
                async with mcp_app.lifespan(mcp_app):  # MCP session manager
                    yield
            else:
                yield
        finally:
            await ctx_.close()  # stops the worker; running jobs go back to 'queued'
            log.info("shutdown complete")

    app = FastAPI(title="Soundtrack Manager", version=__version__, lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(SecurityMiddleware, settings=holder["ctx"].base_settings)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    from .api import create_api
    api = create_api(lambda: holder["ctx"])
    app.mount("/api/v1", api)
    mcp_app = None
    if holder["ctx"].base_settings.mcp_enabled:
        from .mcp import create_mcp_app
        _, mcp_app = create_mcp_app(api)
    templates = Jinja2Templates(directory=HERE / "templates")
    templates.env.filters["human_bytes"] = _human_bytes
    templates.env.globals["version"] = __version__

    def c() -> AppContext:
        return holder["ctx"]

    def render(request: Request, name: str, **kw) -> HTMLResponse:
        kw.setdefault("session", c().status.public())
        return templates.TemplateResponse(request, name, kw)

    def alert(request: Request, exc: Exception) -> HTMLResponse:
        level, msg = _error_message(exc)
        if not isinstance(exc, (AccessBlocked, LayoutChangedError, FetchError, QueryError, ValueError)):
            log.exception("request failed")
        return render(request, "partials/_alert.html", level=level, message=msg)

    # ------------------------------------------------------------------
    @app.get("/healthz")
    async def healthz():
        try:
            version = c().db.schema_version()
        except Exception:  # pragma: no cover
            return JSONResponse({"status": "error"}, status_code=503)
        return {"status": "ok", "schema_version": version, "app_version": __version__}

    @app.get("/")
    async def index():
        return RedirectResponse("/discover", status_code=303)

    # ---------------------------- discover ----------------------------
    def _query_from(request: Request) -> tuple[SearchQuery | None, dict]:
        qp = request.query_params
        form = {
            "q": qp.get("q", ""),
            "platform": [p for p in qp.getlist("platform") if p],
            "type": qp.get("type", ""),
            "year": qp.get("year", ""),
            "sort": qp.get("sort", "relevance"),
            "per_page": int(qp.get("per_page", 50)) if qp.get("per_page", "50").isdigit() else 50,
            "page": int(qp.get("page", 1)) if qp.get("page", "1").isdigit() else 1,
        }
        if form["per_page"] not in PER_PAGE_CHOICES:
            form["per_page"] = 50
        if not form["q"] and not form["platform"]:
            return None, form
        return SearchQuery.build(form["q"], form["platform"], form["type"] or None,
                                 form["year"] or None, form["sort"]), form

    async def _results(request: Request, refresh: bool = False) -> dict:
        query, form = _query_from(request)
        out = {"form": form, "result": None, "queued_ids": set()}
        if query is None:
            return out
        res = await c().discovery.discover(query, page=form["page"], per_page=form["per_page"], refresh=refresh)
        out["result"] = res
        active = queue.list_jobs(c().db, statuses=("queued", "running", "paused", "completed"))
        out["queued_ids"] = {j["album_id"] for j in active}
        out["base_qs"] = urlencode([(k, v) for k, v in request.query_params.multi_items()
                                    if k not in ("page", "refresh")])
        return out

    def _discover_ctx(**kw):
        return dict(platforms=PLATFORMS, album_types=ALBUM_TYPES, sorts=SEARCH_SORTS,
                    formats=FORMATS, per_page_choices=PER_PAGE_CHOICES,
                    preferred_format=c().settings.preferred_format, ai_enabled=bool(c().settings.llm_endpoint), **kw)

    @app.get("/discover", response_class=HTMLResponse)
    async def discover_page(request: Request):
        error = None
        try:
            data = await _results(request)
        except Exception as exc:  # show the form with the error
            _, form = None, {"q": request.query_params.get("q", ""), "platform": request.query_params.getlist("platform"),
                             "type": request.query_params.get("type", ""), "year": request.query_params.get("year", ""),
                             "sort": request.query_params.get("sort", "relevance"), "per_page": 50, "page": 1}
            data = {"form": form, "result": None, "queued_ids": set()}
            error = _error_message(exc)
        history = c().discovery.history(8)
        return render(request, "discover.html", **_discover_ctx(**data, error=error, history=history))

    @app.get("/discover/results", response_class=HTMLResponse)
    async def discover_results(request: Request):
        try:
            data = await _results(request, refresh=request.query_params.get("refresh") == "1")
        except Exception as exc:
            return alert(request, exc)
        return render(request, "partials/_results.html", **_discover_ctx(**data, error=None))

    @app.post("/discover/ai", response_class=HTMLResponse)
    async def discover_ai(request: Request):
        from ..ai import AiError, plan_from_request
        form = await request.form()
        prompt = str(form.get("prompt", ""))
        s = c().settings
        try:
            plan = await plan_from_request(prompt, endpoint=s.llm_endpoint or "", model=s.llm_model,
                                           api_key=c().base_settings.llm_api_key.get())
        except AiError as exc:
            return render(request, "partials/_alert.html", level="warning", message=f"AI: {exc}")
        params = plan.query_params() + [("ai", prompt.strip()[:200])]
        if plan.warnings:
            params.append(("ai_warn", "; ".join(plan.warnings)))
        return HTMLResponse("", headers={"HX-Redirect": "/discover?" + urlencode(params)})

    @app.get("/albums/{album_id}/details", response_class=HTMLResponse)
    async def album_details(request: Request, album_id: int):
        album = c().discovery.get_album(album_id)
        if album is None:
            return render(request, "partials/_alert.html", level="danger", message="Unknown album")
        try:
            album = await c().discovery.album_detail(album["slug"], refresh=request.query_params.get("refresh") == "1")
        except Exception as exc:
            return alert(request, exc)
        est = {}
        for t in album["tracks"]:
            for fmt, size in t["sizes"].items():
                est[fmt] = est.get(fmt, 0) + (size_to_bytes(size) or 0)
        try:
            dest = str(render_album_dir(c().settings.folder_template, album))
        except UnsafePathError:
            dest = None
        return render(request, "partials/_album_details.html", album=album, size_estimates=est, dest=dest)

    # ------------------------------ queue ------------------------------
    @app.post("/queue/add", response_class=HTMLResponse)
    async def queue_add(request: Request):
        form = await request.form()
        fmt = form.get("format") or c().settings.preferred_format
        if fmt not in FORMATS:
            return render(request, "partials/_alert.html", level="danger", message="Unknown format")
        ids = [int(v) for v in form.getlist("album_id") if str(v).isdigit()]
        if not ids:
            return render(request, "partials/_alert.html", level="warning", message="No albums selected.")
        res = queue.enqueue(c().db, ids, fmt)
        c().wake_worker()
        return render(request, "partials/_queue_result.html", res=res, fmt=fmt)

    @app.post("/queue/add-all", response_class=HTMLResponse)
    async def queue_add_all(request: Request):
        """Queue every album matching the current filters (all result pages)."""
        form = await request.form()
        fmt = form.get("format") or c().settings.preferred_format
        try:
            query = SearchQuery.build(form.get("q", ""), [p for p in form.getlist("platform") if p],
                                      form.get("type") or None, form.get("year") or None, form.get("sort"))
            res = await c().discovery.discover(query, per_page=100000, record_history=False)
        except Exception as exc:
            return alert(request, exc)
        result = queue.enqueue(c().db, [a["id"] for a in res.albums], fmt)
        c().wake_worker()
        return render(request, "partials/_queue_result.html", res=result, fmt=fmt)

    # ---------------------------- downloads ----------------------------
    def _jobs_ctx():
        live = c().worker.snapshot() if c().worker else {}
        jobs = [{**j, **live.get(j["id"], {})} for j in queue.list_jobs(c().db)]
        return {"jobs": jobs, "counts": queue.counts(c().db), "worker_on": c().worker is not None,
                "concurrency": c().settings.max_concurrent_downloads}

    def _job_detail_ctx(job_id: int):
        job = next((j for j in queue.list_jobs(c().db) if j["id"] == job_id), None)
        if job and c().worker:
            job = {**job, **c().worker.snapshot().get(job_id, {})}
        return {"job": job, "files": queue.job_files(c().db, job_id) if job else []}

    @app.get("/downloads/{job_id:int}", response_class=HTMLResponse)
    async def job_page(request: Request, job_id: int):
        return render(request, "job.html", **_job_detail_ctx(job_id))

    @app.get("/downloads/{job_id:int}/files", response_class=HTMLResponse)
    async def job_files_partial(request: Request, job_id: int):
        return render(request, "partials/_job_files.html", **_job_detail_ctx(job_id))

    @app.get("/downloads", response_class=HTMLResponse)
    async def downloads_page(request: Request):
        return render(request, "downloads.html", **_jobs_ctx())

    @app.get("/downloads/jobs", response_class=HTMLResponse)
    async def downloads_jobs(request: Request):
        return render(request, "partials/_jobs.html", **_jobs_ctx())

    @app.post("/jobs/{job_id}/{action}", response_class=HTMLResponse)
    async def job_action(request: Request, job_id: int, action: str):
        fn = {"cancel": queue.cancel, "pause": queue.pause, "resume": queue.resume,
              "retry": queue.retry, "remove": queue.remove, "overwrite": queue.allow_overwrite}.get(action)
        if fn is None:
            return render(request, "partials/_alert.html", level="danger", message="Unknown action")
        fn(c().db, job_id)
        c().wake_worker()
        if request.headers.get("hx-target") == "job-files":
            return render(request, "partials/_job_files.html", **_job_detail_ctx(job_id))
        return render(request, "partials/_jobs.html", **_jobs_ctx())

    @app.post("/downloads/concurrency", response_class=HTMLResponse)
    async def set_concurrency(request: Request, value: str = Form(...)):
        try:
            c().update_runtime_setting("max_concurrent_downloads", value)
        except Exception as exc:
            return alert(request, exc)
        return render(request, "partials/_alert.html", level="success",
                      message=f"Concurrency set to {c().settings.max_concurrent_downloads}.")

    # ----------------------------- library -----------------------------
    scan_state: dict = {"running": False, "current": None, "result": None, "error": None, "root": None}

    def _lib():
        from ..library import Library
        return Library(c().db)

    def _library_ctx(request: Request) -> dict:
        lib = _lib()
        roots = c().settings.library_roots
        return {"stats": lib.stats(), "roots": [(i, str(r), c().db.get_sync_state(f"library_scan:{r}")) for i, r in enumerate(roots)],
                "scan": scan_state, "history_sync": c().db.get_sync_state("history_sync"),
                "has_credentials": bool(c().base_settings.khinsider_username and c().base_settings.khinsider_password),
                "formats": FORMATS, "preferred_format": c().settings.preferred_format,
                "q": request.query_params.get("q", ""), "status": request.query_params.get("status", "")}

    @app.get("/library", response_class=HTMLResponse)
    async def library_page(request: Request):
        return render(request, "library.html", **_library_ctx(request))

    @app.get("/library/albums", response_class=HTMLResponse)
    async def library_albums(request: Request):
        q = request.query_params.get("q", "").strip()
        status = request.query_params.get("status") or None
        return render(request, "partials/_library_albums.html", albums=_lib().albums(q, status))

    @app.get("/library/albums/{lib_id:int}/files", response_class=HTMLResponse)
    async def library_files(request: Request, lib_id: int):
        return render(request, "partials/_library_files.html", files=_lib().files(lib_id))

    @app.get("/library/duplicates", response_class=HTMLResponse)
    async def library_duplicates(request: Request):
        return render(request, "partials/_library_duplicates.html", groups=_lib().duplicates())

    @app.get("/library/missing", response_class=HTMLResponse)
    async def library_missing(request: Request):
        return render(request, "partials/_library_missing.html", missing=_lib().missing(), formats=FORMATS,
                      preferred_format=c().settings.preferred_format)

    @app.get("/library/scan-status", response_class=HTMLResponse)
    async def library_scan_status(request: Request):
        return render(request, "partials/_library_scan.html", scan=scan_state)

    @app.post("/library/scan", response_class=HTMLResponse)
    async def library_scan(request: Request):
        form = await request.form()
        roots = c().settings.library_roots
        idx = str(form.get("root", "0"))
        if not idx.isdigit() or int(idx) >= len(roots):
            return render(request, "partials/_alert.html", level="danger", message="Unknown library folder")
        if scan_state["running"]:
            return render(request, "partials/_library_scan.html", scan=scan_state)
        root, deep = roots[int(idx)], form.get("deep") == "1"
        scan_state.update(running=True, current=None, result=None, error=None, root=str(root), deep=deep)

        async def run_scan():
            try:
                scan_state["result"] = await asyncio.to_thread(
                    _lib().scan, root, deep=deep, progress=lambda rel: scan_state.update(current=rel))
            except Exception as exc:
                log.exception("library scan failed")
                scan_state["error"] = str(exc)
            finally:
                scan_state["running"] = False

        holder["scan_task"] = asyncio.create_task(run_scan())
        return render(request, "partials/_library_scan.html", scan=scan_state)

    @app.post("/library/recover", response_class=HTMLResponse)
    async def library_recover(request: Request):
        form = await request.form()
        fmt = form.get("format") or c().settings.preferred_format
        ids = [int(v) for v in form.getlist("album_id") if str(v).isdigit()]
        if fmt not in FORMATS or not ids:
            return render(request, "partials/_alert.html", level="warning", message="Select at least one album.")
        res = _lib().recover(ids, fmt)
        c().wake_worker()
        return render(request, "partials/_queue_result.html", res=res, fmt=fmt)

    @app.post("/library/history-sync", response_class=HTMLResponse)
    async def library_history_sync(request: Request):
        try:
            counts = await _lib().sync_history(c().client)
        except Exception as exc:
            return alert(request, exc)
        return render(request, "partials/_alert.html", level="success",
                      message="Imported account albums: " + ", ".join(f"{k}: {v}" for k, v in counts.items()) +
                      ". Scan the library, then check Missing albums.")

    # ----------------------------- imports -----------------------------
    from ..imports import MAX_FILE_BYTES, ImportFormatError, parse_import

    async def _read_upload(form) -> tuple[bytes, str | None]:
        """The list from an uploaded file, else from the paste box."""
        upload = form.get("file")
        if upload is not None and getattr(upload, "filename", None):
            data = await upload.read(MAX_FILE_BYTES + 1)
            return data, upload.filename
        return str(form.get("content", "")).encode("utf-8"), None

    def _parse_form(form, data: bytes, filename: str | None):
        kind = str(form.get("source_format", "auto"))
        return parse_import(data, filename, source_format=None if kind == "auto" else kind)

    def _imports_ctx() -> dict:
        return {"lists": c().imports.lists(), "formats": FORMATS, "preferred_format": c().settings.preferred_format}

    templates_dir = HERE.parent.parent / "examples"   # repo checkout and Docker image (/app/examples)

    @app.get("/imports/templates/{name}")
    async def import_template(name: str):
        if name not in ("import-template.csv", "import-template.txt") or not (templates_dir / name).is_file():
            return PlainTextResponse("Unknown template", status_code=404)
        media = "text/csv" if name.endswith(".csv") else "text/plain"
        return Response((templates_dir / name).read_bytes(), media_type=f"{media}; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})

    @app.get("/imports", response_class=HTMLResponse)
    async def imports_page(request: Request):
        return render(request, "imports.html", **_imports_ctx())

    @app.post("/imports/check", response_class=HTMLResponse)
    async def imports_check(request: Request):
        form = await request.form(max_part_size=MAX_FILE_BYTES + 1)
        try:
            data, filename = await _read_upload(form)
            parsed = _parse_form(form, data, filename)
        except ImportFormatError as exc:
            return render(request, "partials/_alert.html", level="danger", message=f"Can't import this file: {exc}")
        return render(request, "partials/_import_check.html", parsed=parsed, summary=parsed.summary(), filename=filename)

    @app.post("/imports", response_class=HTMLResponse)
    async def imports_create(request: Request):
        form = await request.form(max_part_size=MAX_FILE_BYTES + 1)
        fmt = str(form.get("format") or c().settings.preferred_format)
        try:
            data, filename = await _read_upload(form)
            parsed = _parse_form(form, data, filename)
            name = str(form.get("name") or "").strip() or (filename or "Pasted list")
            lid = c().imports.create(parsed, name=name, fmt=fmt, include_done=form.get("include_done") == "1")
        except (ImportFormatError, ValueError) as exc:
            return render(request, "partials/_alert.html", level="danger", message=f"Can't import this file: {exc}")
        c().start_import_matching(lid)
        return HTMLResponse("", headers={"HX-Redirect": f"/imports/{lid}"})

    def _import_ctx(request: Request, list_id: int) -> dict:
        info = c().imports.get(list_id)
        return {"imp": info, "running": c().matching_now(list_id), "formats": FORMATS,
                "status": request.query_params.get("status", ""), "q": request.query_params.get("q", "")}

    @app.get("/imports/{list_id:int}", response_class=HTMLResponse)
    async def import_page(request: Request, list_id: int):
        return render(request, "import.html", **_import_ctx(request, list_id))

    @app.get("/imports/{list_id:int}/progress", response_class=HTMLResponse)
    async def import_progress(request: Request, list_id: int):
        return render(request, "partials/_import_progress.html", **_import_ctx(request, list_id))

    @app.get("/imports/{list_id:int}/entries", response_class=HTMLResponse)
    async def import_entries(request: Request, list_id: int):
        qp = request.query_params
        rows = c().imports.entries(list_id, qp.get("status") or None, qp.get("q", ""))
        return render(request, "partials/_import_entries.html", entries=rows, list_id=list_id)

    @app.get("/imports/{list_id:int}/export.csv")
    async def import_export(list_id: int):
        info = c().imports.get(list_id)
        if info is None:
            return PlainTextResponse("Unknown import list", status_code=404)
        return Response(c().imports.export_csv(list_id), media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="import-{list_id}-results.csv"'})

    @app.post("/imports/{list_id:int}/{action}", response_class=HTMLResponse)
    async def import_action(request: Request, list_id: int, action: str):
        imp = c().imports
        if imp.get(list_id) is None:
            return render(request, "partials/_alert.html", level="danger", message="Unknown import list")
        form = await request.form()
        if action == "pause":
            imp.pause(list_id)
        elif action == "resume":
            imp._set_list(list_id, status="matching", error=None)
            c().start_import_matching(list_id)
        elif action == "delete":
            imp.pause(list_id)
            imp.delete(list_id)
            return HTMLResponse("", headers={"HX-Redirect": "/imports"})
        elif action == "queue":
            res = imp.queue(list_id, include_without_format=form.get("include_without_format") == "1")
            c().wake_worker()
            msg = (f"Queued {res['queued']} album(s)" + (f"; {res['already_queued']} were already queued" if res["already_queued"] else "")
                   + (f"; {res['already_downloaded']} already downloaded" if res["already_downloaded"] else "")
                   + (f"; {res['skipped_no_format']} skipped because the format isn't offered" if res["skipped_no_format"] else "")
                   + ". Progress is on the Downloads page.")
            resp = render(request, "partials/_alert.html", level="success" if res["queued"] else "info", message=msg)
            resp.headers["HX-Trigger"] = "refresh-entries"
            return resp
        else:
            return render(request, "partials/_alert.html", level="danger", message="Unknown action")
        return render(request, "partials/_import_progress.html", **_import_ctx(request, list_id))

    @app.post("/imports/entries/{entry_id:int}/{action}", response_class=HTMLResponse)
    async def import_entry_action(request: Request, entry_id: int, action: str):
        imp = c().imports
        form = await request.form()
        try:
            if action == "choose":
                ref = str(form.get("album_id") or "").strip()
                url = str(form.get("url") or "").strip()
                if url:  # a pasted URL wins over the pre-selected candidate
                    await imp.choose_url(entry_id, url)
                elif ref.isdigit():
                    await imp.choose(entry_id, int(ref))
                else:
                    raise ValueError("pick a candidate or paste an album URL")
            elif action == "research":
                await imp.research(entry_id, str(form.get("search", "")))
            elif action == "skip":
                imp.skip(entry_id)
            elif action == "restore":
                imp.restore(entry_id)
            else:
                raise ValueError("Unknown action")
        except Exception as exc:
            level, msg = _error_message(exc)
            e = imp.entry(entry_id)
            if e is None:
                return render(request, "partials/_alert.html", level=level, message=msg)
            return render(request, "partials/_import_entry.html", e=e, error=msg)
        e = imp.entry(entry_id)
        if e is None:
            return render(request, "partials/_alert.html", level="danger", message="Unknown import entry")
        return render(request, "partials/_import_entry.html", e=e, error=None)

    # ----------------------------- settings ----------------------------
    def _settings_ctx():
        s = c().settings
        return {"s": s, "public": s.public_dict(), "formats": FORMATS, "template_fields": sorted(TEMPLATE_FIELDS),
                "template_example": _template_example(s.folder_template),
                "has_credentials": bool(c().base_settings.khinsider_username and c().base_settings.khinsider_password)}

    def _template_example(t: str) -> str:
        try:
            return str(render_album_dir(t, {"title": "Resident Evil: Save Room Compilation", "platforms": ["PS1", "N64"],
                                            "year": 2023, "album_type": "Remix", "slug": "example"}))
        except UnsafePathError as exc:
            return f"invalid: {exc}"

    @app.get("/settings", response_class=HTMLResponse)
    async def settings_page(request: Request):
        return render(request, "settings.html", **_settings_ctx(), message=None)

    @app.post("/settings", response_class=HTMLResponse)
    async def settings_save(request: Request):
        form = await request.form()
        errors = []
        for key in ("preferred_format", "max_concurrent_downloads", "request_delay_seconds", "max_retries",
                    "folder_template", "llm_endpoint", "llm_model"):
            if key in form:
                current = getattr(c().settings, key)
                new = str(form.get(key, "")).strip()
                if str(current if current is not None else "") == new:
                    continue
                try:
                    c().update_runtime_setting(key, new)
                except Exception as exc:
                    errors.append(f"{key}: {exc}")
        msg = ("danger", "; ".join(errors)) if errors else ("success", "Settings saved.")
        return render(request, "partials/_settings_form.html", **_settings_ctx(), message=msg)

    @app.get("/settings/logs", response_class=HTMLResponse)
    async def settings_logs(request: Request):
        return render(request, "partials/_logs.html", lines=tail(c().settings.log_path, 200))

    @app.get("/session/status", response_class=HTMLResponse)
    async def session_status(request: Request):
        return render(request, "partials/_session.html")

    @app.post("/session/login", response_class=HTMLResponse)
    async def session_login(request: Request):
        try:
            ok = await c().login()
        except Exception as exc:
            return alert(request, exc)
        return render(request, "partials/_alert.html", level="success" if ok else "danger",
                      message="Logged in to khinsider." if ok else "Login failed: check username/password.")

    if mcp_app is not None:
        app.mount("/", mcp_app)  # last, so it only receives /mcp (and paths nothing else matched)
    return app
