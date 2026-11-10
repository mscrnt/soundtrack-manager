"""Request protection: optional HTTP Basic auth, CSRF guard, API/MCP access rules, security headers.

Web UI
    Basic auth when ``WEB_PASSWORD`` is set. Mutating requests must carry HTMX's
    ``HX-Request`` header and, if the browser sends one, a same-host ``Origin``.

REST API (``/api/``) and MCP (``/mcp``), each off unless enabled
    * ``API_TOKEN`` set: a bearer token (or ``X-API-Key``) or the web login is required.
    * else ``WEB_PASSWORD`` set: the web login (Basic auth) is required.
    * else: open, but only to this machine. The ``Host`` header must be a loopback
      name or listed in ``API_ALLOWED_HOSTS`` (stops DNS-rebinding attacks).
    In every case a browser ``Origin`` from another site is refused, so a web page
    can't drive the API with your cookies or cached login (CSRF).

Written as plain ASGI middleware (not BaseHTTPMiddleware) so MCP's streamed
responses pass through untouched.
"""

from __future__ import annotations

import base64
import binascii
import hmac
from urllib.parse import urlsplit

from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import JSONResponse, PlainTextResponse

from ..config import Settings

CSP = (
    "default-src 'self'; "
    "img-src 'self' data: https://vgmtreasurechest.com https://*.vgmtreasurechest.com; "
    # Inline style attributes are allowed for progress-bar widths. Scripts stay 'self' only.
    "script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self'; "
    "frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
)
OPEN_PATHS = ("/healthz",)
API_PREFIX = "/api/"
MCP_PREFIX = "/mcp"
# The schema and docs page hold no data. Calls made from the docs page are still checked.
API_OPEN = ("/api/v1/openapi.json", "/api/v1/docs")
SAFE_METHODS = ("GET", "HEAD", "OPTIONS")
LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1", "[::1]"}


def _host_name(host_header: str) -> str:
    host = host_header.strip().lower()
    if host.startswith("["):                 # [::1]:8080
        return host.split("]")[0] + "]"
    return host.rsplit(":", 1)[0] if host.count(":") == 1 else host


class SecurityMiddleware:
    def __init__(self, app, settings: Settings):
        self.app = app
        self.settings = settings

    # ------------------------------------------------------------ checks
    def _basic_ok(self, headers: Headers) -> bool:
        header = headers.get("authorization", "")
        if not header.lower().startswith("basic "):
            return False
        try:
            user, _, pw = base64.b64decode(header[6:]).decode("utf-8").partition(":")
        except (binascii.Error, UnicodeDecodeError, ValueError):
            return False
        ok_user = hmac.compare_digest(user.encode(), self.settings.web_username.encode())
        ok_pw = hmac.compare_digest(pw.encode(), (self.settings.web_password.get() or "").encode())
        return ok_user and ok_pw

    def _token_ok(self, headers: Headers) -> bool:
        token = self.settings.api_token.get()
        header = headers.get("authorization", "")
        given = header[7:].strip() if header.lower().startswith("bearer ") else headers.get("x-api-key", "")
        return bool(token and given) and hmac.compare_digest(given.encode(), token.encode())

    @staticmethod
    def _same_origin(headers: Headers) -> bool:
        origin = headers.get("origin")
        return not origin or urlsplit(origin).netloc.lower() == headers.get("host", "").lower()

    def _ui_csrf_ok(self, method: str, headers: Headers) -> bool:
        """Mutating UI requests must come from our own pages (HTMX header, same-host Origin).

        Cross-site forms cannot set custom headers without a CORS preflight, which we never grant.
        """
        if method in SAFE_METHODS:
            return True
        return headers.get("hx-request") == "true" and self._same_origin(headers)

    def _program_access(self, path: str, headers: Headers):
        """None if allowed, else the response to send (REST API and MCP)."""
        s = self.settings
        is_mcp = path == MCP_PREFIX or path.startswith(MCP_PREFIX + "/")
        if is_mcp and not s.mcp_enabled:
            return JSONResponse({"detail": "The MCP server is off. Set MCP_ENABLED=true and restart."}, status_code=404)
        if not is_mcp and not s.api_enabled:
            return JSONResponse({"detail": "The API is off. Set API_ENABLED=true and restart."}, status_code=404)
        if not self._same_origin(headers):
            return JSONResponse({"detail": "Cross-site request rejected"}, status_code=403)
        if not is_mcp and path.rstrip("/") in API_OPEN:
            return None
        if s.api_token:
            if self._token_ok(headers) or (s.auth_enabled and self._basic_ok(headers)):
                return None
            return JSONResponse({"detail": "Missing or wrong API token"}, status_code=401,
                                headers={"WWW-Authenticate": "Bearer"})
        if s.auth_enabled:
            if self._basic_ok(headers):
                return None
            return JSONResponse({"detail": "Authentication required (the web login, or set API_TOKEN)"},
                                status_code=401, headers={"WWW-Authenticate": 'Basic realm="Soundtrack Manager"'})
        host = _host_name(headers.get("host", ""))
        if host in LOOPBACK_HOSTS or host in s.api_allowed_hosts:
            return None
        return JSONResponse({"detail": f"Host {host!r} is not allowed without authentication. Use localhost, "
                                       "add it to API_ALLOWED_HOSTS, or set API_TOKEN / WEB_PASSWORD."},
                            status_code=403)

    # ------------------------------------------------------------ ASGI
    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        headers = Headers(scope=scope)
        denied = None
        if path.startswith(API_PREFIX) or path == MCP_PREFIX or path.startswith(MCP_PREFIX + "/"):
            denied = self._program_access(path, headers)
        elif not path.startswith(OPEN_PATHS):
            if self.settings.auth_enabled and not self._basic_ok(headers):
                denied = PlainTextResponse("Authentication required", status_code=401,
                                           headers={"WWW-Authenticate": 'Basic realm="Soundtrack Manager"'})
            elif not self._ui_csrf_ok(scope.get("method", "GET"), headers):
                denied = PlainTextResponse("Cross-site request rejected", status_code=403)

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                h = MutableHeaders(scope=message)
                h.setdefault("Content-Security-Policy", CSP)
                h.setdefault("X-Content-Type-Options", "nosniff")
                h.setdefault("X-Frame-Options", "DENY")
                h.setdefault("Referrer-Policy", "no-referrer")
            await send(message)

        if denied is not None:
            await denied(scope, receive, send_with_headers)
            return
        await self.app(scope, receive, send_with_headers)
