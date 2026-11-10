"""MCP server for agents, generated from the REST API with FastMCP.

Every ``/api/v1`` route becomes an MCP tool with the same name, arguments and
checks. Tool calls go to the API in-process (no network hop). The endpoint is
streamable HTTP at ``/mcp``, off unless ``MCP_ENABLED=true``. Access rules are
the API's (web/security.py).
"""

from __future__ import annotations

import logging

from fastapi import FastAPI

log = logging.getLogger(__name__)

INSTRUCTIONS = """Soundtrack Manager downloads video game music albums from downloads.khinsider.com.

Typical flows:
- Find and download albums: search_albums -> (get_album to check formats/tracks) -> queue_albums.
  Watch progress with list_jobs / get_job.
- Download a whole list: validate_import_list (optional) -> create_import_list with the file's text
  (a numbered text list or a CSV with a 'title' column). Matching runs in the background: poll
  get_import_list until status is 'ready'. Then fix entries under status 'review' with
  choose_import_album (a candidate album_id or an album URL), research_import_entry or skip.
  Finally queue_import_list.
Albums that don't offer the wanted format (usually FLAC) are skipped when queueing unless
include_without_format is true; they would download as MP3.
Searching and matching are rate-limited and can take a while. Don't loop quickly."""


def create_mcp_app(api: FastAPI):
    """Return the MCP Starlette app (endpoint ``/mcp``). Its lifespan must run with the main app's."""
    from fastmcp import FastMCP
    from fastmcp.server.providers.openapi import MCPType, RouteMap

    route_maps = [RouteMap(pattern=r".*/export\.csv$", mcp_type=MCPType.EXCLUDE),
                  RouteMap(mcp_type=MCPType.TOOL)]  # everything else is a tool (GETs included)
    mcp = FastMCP.from_fastapi(
        app=api, name="Soundtrack Manager", route_maps=route_maps,
        # Site requests go through FlareSolverr and the rate limit; allow for slow ones.
        httpx_client_kwargs={"timeout": 300.0},
        instructions=INSTRUCTIONS,
    )
    log.info("MCP server enabled at /mcp")
    # Host/Origin checks and auth happen in our security middleware, before this app.
    return mcp, mcp.http_app(path="/mcp", host_origin_protection=False)
