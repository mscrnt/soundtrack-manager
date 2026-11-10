"""JSON API for other programs: ``/api/v1`` (off unless ``API_ENABLED=true``).

Interactive docs (Swagger UI) at ``/api/v1/docs``; schema at ``/api/v1/openapi.json``.
Access rules live in web/security.py: an ``API_TOKEN`` bearer token, else the web
login, else localhost only. The MCP server (web/mcp.py) is generated from these
routes, so each handler's name is also its MCP tool name.
"""

from __future__ import annotations

from typing import Callable, Literal

from fastapi import FastAPI, HTTPException, Query, Request, Security
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.routing import APIRoute
from fastapi.security import HTTPBasic, HTTPBearer
from pydantic import BaseModel, Field

from .. import __version__, queue
from ..config import FORMATS
from ..discovery import QueryError, SearchQuery
from ..imports import MAX_FILE_BYTES, ImportFormatError, parse_import
from ..khinsider.fetch import AccessBlocked, FetchError
from ..khinsider.parse import LayoutChangedError
from ..khinsider.urls import album_slug

Format = Literal["flac", "mp3"]
JOB_ACTIONS = {"cancel": queue.cancel, "pause": queue.pause, "resume": queue.resume,
               "retry": queue.retry, "remove": queue.remove, "overwrite": queue.allow_overwrite}


class QueueRequest(BaseModel):
    album_ids: list[int] = Field(default_factory=list, description="album ids from /search or /albums")
    urls: list[str] = Field(default_factory=list, description="khinsider album URLs or slugs")
    format: Format | None = Field(None, description="defaults to the preferred format")


class ImportRequest(BaseModel):
    content: str = Field(..., description="the list file's text (shopping list or CSV)")
    filename: str | None = Field(None, description="helps detect the format (.txt / .csv)")
    source_format: Literal["auto", "text", "csv"] = "auto"
    name: str | None = None
    format: Format | None = None
    include_done: bool = Field(False, description="also import entries checked off in the file")
    start: bool = Field(True, description="start matching right away")


class ChooseRequest(BaseModel):
    album_id: int | None = None
    url: str | None = None


class ResearchRequest(BaseModel):
    search: str


class ImportQueueRequest(BaseModel):
    include_without_format: bool = Field(False, description="also queue albums that lack the format "
                                                             "(they download as MP3)")
    entry_ids: list[int] | None = Field(None, description="only these entries (default: every match)")


DOCS_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Soundtrack Manager API</title><link rel="stylesheet" href="/static/vendor/swagger-ui.css">
<style>body{margin:0;background:#fff}</style></head>
<body><div id="swagger-ui"></div>
<script src="/static/vendor/swagger-ui-bundle.js"></script><script src="/static/swagger-init.js"></script>
</body></html>"""

DESCRIPTION = """Search khinsider, queue downloads, and import shopping lists (text or CSV).

**Access:** with `API_TOKEN` set, send `Authorization: Bearer <token>` (or `X-API-Key`), or use the web
login. Without a token, the web login (`WEB_PASSWORD`) is required when one is set; otherwise only this
machine can connect. Use **Authorize** to try calls from this page.

The same operations are available to agents as MCP tools at `/mcp` when `MCP_ENABLED=true`."""


def _operation_id(route: APIRoute) -> str:
    return route.name  # the handler's name: also the MCP tool name


def create_api(get_ctx: Callable) -> FastAPI:
    api = FastAPI(title="Soundtrack Manager API", version=__version__, docs_url=None, redoc_url=None,
                  openapi_url="/openapi.json", description=DESCRIPTION,
                  generate_unique_id_function=_operation_id,
                  # Declares the accepted credentials in the schema (for Swagger's Authorize button).
                  # Enforcement is in the security middleware, before any route runs.
                  dependencies=[Security(HTTPBearer(auto_error=False, description="API_TOKEN")),
                                Security(HTTPBasic(auto_error=False, description="web login"))])

    @api.get("/docs", include_in_schema=False)
    async def docs():
        return HTMLResponse(DOCS_HTML)

    @api.exception_handler(AccessBlocked)
    async def _blocked(_, exc):
        return JSONResponse({"detail": f"Access check: {exc}"}, status_code=503)

    @api.exception_handler(LayoutChangedError)
    async def _layout(_, exc):
        return JSONResponse({"detail": f"site layout not recognised: {exc}"}, status_code=502)

    @api.exception_handler(FetchError)
    async def _fetch(_, exc):
        return JSONResponse({"detail": str(exc)}, status_code=502)

    @api.exception_handler(ImportFormatError)
    async def _import_fmt(_, exc):
        return JSONResponse({"detail": str(exc)}, status_code=422)

    @api.exception_handler(QueryError)
    async def _query(_, exc):
        return JSONResponse({"detail": str(exc)}, status_code=422)

    @api.exception_handler(ValueError)
    async def _value(_, exc):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    def ctx():
        return get_ctx()

    def need(obj, what: str):
        if obj is None:
            raise HTTPException(404, f"unknown {what}")
        return obj

    # ------------------------------------------------------------ status
    @api.get("/status", summary="Version, session, worker and queue counts")
    async def get_status():
        c = ctx()
        return {"app_version": __version__, "schema_version": c.db.schema_version(),
                "session": c.status.public(), "worker": c.worker is not None,
                "queue": queue.counts(c.db), "preferred_format": c.settings.preferred_format}

    # ------------------------------------------------------------ discovery
    @api.get("/search", summary="Search albums (same filters as the Discover page)")
    async def search_albums(q: str = "", platform: list[str] = Query(default=[]), type: str | None = None,
                     year: str | None = None, sort: str = "relevance", page: int = 1,
                     per_page: int = Query(100, ge=1, le=1000), refresh: bool = False):
        query = SearchQuery.build(q, platform, type, year, sort)
        res = await ctx().discovery.discover(query, page=page, per_page=per_page, refresh=refresh)
        return {"total": res.total, "page": res.page, "page_count": res.page_count, "notes": res.notes,
                "from_cache": res.from_cache, "albums": res.page_albums}

    @api.get("/albums/{album_id}", summary="Album details with tracks and formats (fetched if needed)")
    async def get_album(album_id: int, refresh: bool = False):
        a = need(ctx().discovery.get_album(album_id), "album")
        return await ctx().discovery.album_detail(a["slug"], refresh=refresh)

    @api.get("/albums", summary="Album details by URL or slug")
    async def get_album_by_url(url: str, refresh: bool = False):
        return await ctx().discovery.album_detail(album_slug(url), refresh=refresh)

    # ------------------------------------------------------------ queue
    @api.post("/queue", summary="Queue albums by id and/or URL")
    async def queue_albums(body: QueueRequest):
        c = ctx()
        ids = list(body.album_ids)
        for u in body.urls:
            ids.append((await c.discovery.album_detail(album_slug(u)))["id"])
        if not ids:
            raise HTTPException(422, "give album_ids or urls")
        fmt = body.format or c.settings.preferred_format
        res = queue.enqueue(c.db, ids, fmt)
        c.wake_worker()
        return {"format": fmt, "added": res.added, "already_queued": res.already_queued,
                "already_completed": res.already_completed, "unknown": res.missing}

    @api.get("/jobs", summary="Download jobs")
    async def list_jobs(status: list[str] = Query(default=[]), limit: int = Query(500, ge=1, le=5000)):
        c = ctx()
        live = c.worker.snapshot() if c.worker else {}
        return {"counts": queue.counts(c.db),
                "jobs": [{**j, **live.get(j["id"], {})} for j in queue.list_jobs(c.db, tuple(status) or None, limit)]}

    @api.get("/jobs/{job_id}", summary="One job with its files")
    async def get_job(job_id: int):
        c = ctx()
        j = need(next((j for j in queue.list_jobs(c.db, limit=100000) if j["id"] == job_id), None), "job")
        if c.worker:
            j = {**j, **c.worker.snapshot().get(job_id, {})}
        return {**j, "files": queue.job_files(c.db, job_id)}

    @api.post("/jobs/{job_id}/{action}", summary="cancel, pause, resume, retry, remove or overwrite")
    async def job_action(job_id: int, action: Literal["cancel", "pause", "resume", "retry", "remove", "overwrite"]):
        c = ctx()
        ok = JOB_ACTIONS[action](c.db, job_id)
        c.wake_worker()
        if not ok:
            raise HTTPException(409, f"can't {action} job {job_id} in its current state (or unknown id)")
        return {"ok": True, "status": queue.job_status(c.db, job_id)}

    # ------------------------------------------------------------ imports
    async def _import_body(request: Request) -> ImportRequest:
        """JSON body, or the raw file as the body (text/plain, text/csv) with options in the query string."""
        ctype = request.headers.get("content-type", "").split(";")[0].strip().lower()
        if ctype == "application/json":
            return ImportRequest.model_validate_json(await request.body())
        data = await request.body()
        if len(data) > MAX_FILE_BYTES:
            raise ImportFormatError(f"file is larger than {MAX_FILE_BYTES // 1024 // 1024} MB")
        from ..imports import decode
        qp = request.query_params
        return ImportRequest(content=decode(data), filename=qp.get("filename"),
                             source_format=qp.get("source_format", "csv" if ctype == "text/csv" else "auto"),
                             name=qp.get("name"), format=qp.get("format"),
                             include_done=qp.get("include_done") in ("1", "true"),
                             start=qp.get("start", "true") not in ("0", "false"))

    def _parse(body: ImportRequest):
        if len(body.content.encode("utf-8")) > MAX_FILE_BYTES:
            raise ImportFormatError(f"file is larger than {MAX_FILE_BYTES // 1024 // 1024} MB")
        return parse_import(body.content, body.filename,
                            source_format=None if body.source_format == "auto" else body.source_format)

    @api.post("/imports/validate", summary="Check a list file without importing it",
              openapi_extra={"requestBody": {"content": {
                  "application/json": {"schema": ImportRequest.model_json_schema()},
                  "text/plain": {"schema": {"type": "string"}}, "text/csv": {"schema": {"type": "string"}}}}})
    async def validate_import_list(request: Request):
        return _parse(await _import_body(request)).to_dict()

    @api.post("/imports", status_code=201, summary="Import a list and start matching it",
              openapi_extra={"requestBody": {"content": {
                  "application/json": {"schema": ImportRequest.model_json_schema()},
                  "text/plain": {"schema": {"type": "string"}}, "text/csv": {"schema": {"type": "string"}}}}})
    async def create_import_list(request: Request):
        c = ctx()
        body = await _import_body(request)
        parsed = _parse(body)
        fmt = body.format or c.settings.preferred_format
        lid = c.imports.create(parsed, name=body.name or body.filename or "API import", fmt=fmt,
                               include_done=body.include_done)
        if body.start:
            c.start_import_matching(lid)
        return {**c.imports.get(lid), "validation": parsed.summary(), "issues": parsed.to_dict()["issues"]}

    @api.get("/imports", summary="Import lists")
    async def list_import_lists():
        return {"lists": ctx().imports.lists()}

    @api.get("/imports/{list_id}", summary="One import list with counts")
    async def get_import_list(list_id: int):
        c = ctx()
        return {**need(c.imports.get(list_id), "import list"), "matching_now": c.matching_now(list_id)}

    @api.get("/imports/{list_id}/entries", summary="Entries; status = pending, matched, ambiguous, not_found, "
                                                   "error, skipped, queued, review or no_format")
    async def list_import_entries(list_id: int, status: str | None = None, q: str = ""):
        need(ctx().imports.get(list_id), "import list")
        return {"entries": ctx().imports.entries(list_id, status, q)}

    @api.get("/imports/{list_id}/export.csv", summary="Match results as CSV", response_class=Response)
    async def export_import_results(list_id: int):
        need(ctx().imports.get(list_id), "import list")
        return Response(ctx().imports.export_csv(list_id), media_type="text/csv; charset=utf-8")

    @api.post("/imports/{list_id}/pause", summary="Stop matching (resume later)")
    async def pause_import_matching(list_id: int):
        need(ctx().imports.get(list_id), "import list")
        return {"ok": ctx().imports.pause(list_id)}

    @api.post("/imports/{list_id}/resume", summary="Continue matching pending entries")
    async def resume_import_matching(list_id: int):
        c = ctx()
        need(c.imports.get(list_id), "import list")
        c.imports._set_list(list_id, status="matching", error=None)
        c.start_import_matching(list_id)
        return {"ok": True}

    @api.post("/imports/{list_id}/queue", summary="Queue the matched albums (priority order)")
    async def queue_import_list(list_id: int, body: ImportQueueRequest | None = None):
        c = ctx()
        need(c.imports.get(list_id), "import list")
        body = body or ImportQueueRequest()
        res = c.imports.queue(list_id, include_without_format=body.include_without_format, entry_ids=body.entry_ids)
        c.wake_worker()
        return res

    @api.delete("/imports/{list_id}", summary="Delete an import list (queued downloads are kept)")
    async def delete_import_list(list_id: int):
        c = ctx()
        need(c.imports.get(list_id), "import list")
        c.imports.pause(list_id)
        return {"ok": c.imports.delete(list_id)}

    @api.post("/imports/entries/{entry_id}/choose", summary="Set an entry's album (a candidate id or any album URL)")
    async def choose_import_album(entry_id: int, body: ChooseRequest):
        imp = ctx().imports
        need(imp.entry(entry_id), "import entry")
        if body.album_id is not None:
            return await imp.choose(entry_id, body.album_id)
        if body.url:
            return await imp.choose_url(entry_id, body.url)
        raise HTTPException(422, "give album_id or url")

    @api.post("/imports/entries/{entry_id}/research", summary="Search again with a different term")
    async def research_import_entry(entry_id: int, body: ResearchRequest):
        imp = ctx().imports
        need(imp.entry(entry_id), "import entry")
        return await imp.research(entry_id, body.search)

    @api.post("/imports/entries/{entry_id}/{action}", summary="skip or restore an entry")
    async def skip_or_restore_import_entry(entry_id: int, action: Literal["skip", "restore"]):
        imp = ctx().imports
        need(imp.entry(entry_id), "import entry")
        ok = imp.skip(entry_id) if action == "skip" else imp.restore(entry_id)
        return {"ok": ok, "entry": imp.entry(entry_id)}

    # ------------------------------------------------------------ library
    @api.get("/library", summary="Library statistics")
    async def library_stats():
        from ..library import Library
        return Library(ctx().db).stats()

    @api.get("/library/missing", summary="Albums known to be missing or damaged locally")
    async def library_missing():
        from ..library import Library
        return {"missing": Library(ctx().db).missing()}

    # Group operations in the docs by area.
    for route in api.routes:
        if isinstance(route, APIRoute) and not route.tags:
            first = route.path.strip("/").split("/")[0]
            route.tags = [{"status": "Status", "search": "Discovery", "albums": "Discovery", "queue": "Downloads",
                           "jobs": "Downloads", "imports": "Import lists", "library": "Library"}.get(first, "Other")]
    return api


__all__ = ["create_api", "FORMATS"]
