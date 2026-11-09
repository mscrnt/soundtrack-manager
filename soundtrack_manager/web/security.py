"""Web UI protection: optional HTTP Basic auth, CSRF guard, security headers."""

from __future__ import annotations

import base64
import binascii
import hmac
from urllib.parse import urlsplit

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response

from ..config import Settings

CSP = (
    "default-src 'self'; "
    "img-src 'self' data: https://vgmtreasurechest.com https://*.vgmtreasurechest.com; "
    # Inline style attributes are allowed for progress-bar widths. Scripts stay 'self' only.
    "script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self'; "
    "frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
)
OPEN_PATHS = ("/healthz",)
SAFE_METHODS = ("GET", "HEAD", "OPTIONS")


class SecurityMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, settings: Settings):
        super().__init__(app)
        self.settings = settings

    def _authorized(self, request: Request) -> bool:
        if not self.settings.auth_enabled:
            return True
        header = request.headers.get("authorization", "")
        if not header.lower().startswith("basic "):
            return False
        try:
            user, _, pw = base64.b64decode(header[6:]).decode("utf-8").partition(":")
        except (binascii.Error, UnicodeDecodeError):
            return False
        ok_user = hmac.compare_digest(user.encode(), self.settings.web_username.encode())
        ok_pw = hmac.compare_digest(pw.encode(), (self.settings.web_password.get() or "").encode())
        return ok_user and ok_pw

    @staticmethod
    def _csrf_ok(request: Request) -> bool:
        """Mutating requests must come from our own pages.

        HTMX sends ``HX-Request: true``. Cross-site forms cannot set custom
        headers without a CORS preflight, which we never grant. If the browser
        sends an ``Origin``, it must also match the host.
        """
        if request.method in SAFE_METHODS:
            return True
        if request.headers.get("hx-request") != "true":
            return False
        origin = request.headers.get("origin")
        if origin:
            return urlsplit(origin).netloc == request.headers.get("host")
        return True

    async def dispatch(self, request: Request, call_next) -> Response:
        if not request.url.path.startswith(OPEN_PATHS):
            if not self._authorized(request):
                return PlainTextResponse("Authentication required", status_code=401,
                                         headers={"WWW-Authenticate": 'Basic realm="Soundtrack Manager"'})
            if not self._csrf_ok(request):
                return PlainTextResponse("Cross-site request rejected", status_code=403)
        response = await call_next(request)
        response.headers.setdefault("Content-Security-Policy", CSP)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        return response
