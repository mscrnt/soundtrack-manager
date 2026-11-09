"""Page fetching backends.

* ``HttpFetcher``: plain httpx. Used first because it is the cheapest.
* ``FlareSolverrFetcher``: sends requests through a FlareSolverr service the
  user runs (on by default in compose.yaml). It keeps a persistent FlareSolverr
  session, so cookies and the khinsider login are reused.
* ``BrowserFetcher``: Playwright with a persistent profile. On a desktop with
  ``BROWSER_HEADLESS=false`` the user can complete checks or log in themselves.

``AutoFetcher`` tries plain HTTP. When it detects a Cloudflare check, it moves
(sticky) to the first fallback that is reachable. This module adds no
challenge-solving logic of its own.

All backends share one rate limiter. 429/5xx and timeouts back off
exponentially (honouring ``Retry-After``). A check that cannot be passed raises
``AccessBlocked`` and is never retried in a loop.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

import httpx

from urllib.parse import urlencode

from .. import __version__
from .parse import is_challenge_page, login_state, parse_login_form
from .urls import BASE_URL, is_site_url

log = logging.getLogger(__name__)

USER_AGENT = f"soundtrack-manager/{__version__} (personal soundtrack library tool)"
RETRY_STATUSES = {429, 500, 502, 503, 504}


class AccessBlocked(Exception):
    """The site served an access check (e.g. Cloudflare) instead of content."""


class FetchError(Exception):
    pass


class BackendUnavailable(FetchError):
    """A fallback backend (e.g. the FlareSolverr container) cannot be reached."""


@dataclass
class SessionStatus:
    mode: str = "http"                 # client currently used for site pages
    last_ok_at: float | None = None
    last_blocked_at: float | None = None
    last_blocked_reason: str | None = None
    logged_in: bool | None = None      # None = unknown
    browser_available: bool | None = None
    notes: list[str] = field(default_factory=list)

    def public(self) -> dict:
        """Safe for UI and API responses: no cookies or tokens."""
        return {
            "mode": self.mode,
            "last_ok_at": self.last_ok_at,
            "last_blocked_at": self.last_blocked_at,
            "last_blocked_reason": self.last_blocked_reason,
            "logged_in": self.logged_in,
            "browser_available": self.browser_available,
        }


class RateLimiter:
    """Minimum interval between requests, shared across tasks."""

    def __init__(self, min_interval: float):
        self.min_interval = min_interval
        self._lock = asyncio.Lock()
        self._last = 0.0

    async def wait(self) -> None:
        async with self._lock:
            # Loop: on Windows the event loop's timer can wake a few ms early, and the
            # minimum gap must hold by the monotonic clock.
            while (delay := self._last + self.min_interval - time.monotonic()) > 0:
                await asyncio.sleep(delay)
            self._last = time.monotonic()


class Fetcher(Protocol):
    status: SessionStatus

    async def get_html(self, url: str) -> str: ...
    async def close(self) -> None: ...


def _backoff(attempt: int, base: float, retry_after: str | None) -> float:
    if retry_after and retry_after.isdigit():
        return min(float(retry_after), 300.0)
    return base * (2 ** attempt) + random.uniform(0, base)


class HttpFetcher:
    name = "http"

    def __init__(self, *, limiter: RateLimiter, timeout: float, max_retries: int,
                 backoff: float, status: SessionStatus | None = None,
                 transport: httpx.AsyncBaseTransport | None = None):
        self.limiter = limiter
        self.max_retries = max_retries
        self.backoff = backoff
        self.status = status or SessionStatus()
        self._client = httpx.AsyncClient(
            headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"},
            timeout=timeout,
            follow_redirects=True,
            transport=transport,
        )

    async def get_html(self, url: str) -> str:
        if not is_site_url(url):
            raise FetchError(f"refusing to fetch non-khinsider URL: {url}")
        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            await self.limiter.wait()
            try:
                r = await self._client.get(url)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_exc = exc
                log.warning("fetch %s failed (%s), attempt %d", url, type(exc).__name__, attempt + 1)
                await asyncio.sleep(_backoff(attempt, self.backoff, None))
                continue
            text = r.text
            if is_challenge_page(text):
                self.status.last_blocked_at = time.time()
                self.status.last_blocked_reason = f"Cloudflare check on HTTP {r.status_code} for plain HTTP client"
                raise AccessBlocked(
                    "khinsider answered plain HTTP with a Cloudflare check. Configure FlareSolverr "
                    "(FLARESOLVERR_URL) or the browser session (FETCH_MODE=auto/browser)."
                )
            if r.status_code in RETRY_STATUSES:
                last_exc = FetchError(f"HTTP {r.status_code}")
                wait = _backoff(attempt, self.backoff, r.headers.get("Retry-After"))
                log.warning("fetch %s -> %d, retrying in %.1fs", url, r.status_code, wait)
                await asyncio.sleep(wait)
                continue
            if r.status_code == 404:
                raise FetchError(f"not found (404): {url}")
            if r.status_code >= 400:
                raise FetchError(f"HTTP {r.status_code} for {url}")
            self.status.last_ok_at = time.time()
            return text
        raise FetchError(f"giving up on {url} after {self.max_retries + 1} attempts: {last_exc}")

    async def close(self) -> None:
        await self._client.aclose()


class FlareSolverrFetcher:
    """Requests through a FlareSolverr service (https://github.com/FlareSolverr/FlareSolverr).

    API: ``POST {endpoint}/v1`` with ``{"cmd": "request.get" | "request.post" |
    "sessions.create" | "sessions.destroy", ...}``. The reply has
    ``solution.response`` (HTML) and ``solution.status``.
    """

    name = "flaresolverr"

    def __init__(self, *, endpoint: str, limiter: RateLimiter, timeout: float, max_retries: int,
                 backoff: float, status: SessionStatus | None = None, session: str = "soundtrack-manager",
                 transport: httpx.AsyncBaseTransport | None = None):
        base = endpoint.rstrip("/")
        self.endpoint = base if base.endswith("/v1") else base + "/v1"
        self.limiter = limiter
        self.max_retries = max_retries
        self.backoff = backoff
        self.status = status or SessionStatus()
        self.session = session
        self.max_timeout_ms = int(max(timeout, 60.0) * 1000)
        # FlareSolverr holds the request open while it works, so allow extra time.
        self._client = httpx.AsyncClient(timeout=self.max_timeout_ms / 1000 + 30, transport=transport)
        self._session_ready = False
        self._lock = asyncio.Lock()  # one request at a time per FlareSolverr session

    async def _call(self, payload: dict) -> dict:
        try:
            r = await self._client.post(self.endpoint, json=payload)
        except httpx.ConnectError as exc:
            raise BackendUnavailable(f"cannot reach FlareSolverr at {self.endpoint} (is the container running?)") from exc
        try:
            data = r.json()
        except ValueError as exc:
            raise FetchError(f"FlareSolverr answered HTTP {r.status_code} without JSON") from exc
        if data.get("status") != "ok":
            raise FetchError(f"FlareSolverr: {data.get('message') or 'HTTP ' + str(r.status_code)}")
        return data

    async def _ensure_session(self) -> None:
        if self._session_ready:
            return
        try:
            await self._call({"cmd": "sessions.create", "session": self.session})
        except BackendUnavailable:
            raise
        except FetchError as exc:
            if "already exists" not in str(exc).lower():
                raise
        self._session_ready = True

    async def _request(self, url: str, post_data: str | None = None) -> str:
        if not is_site_url(url):
            raise FetchError(f"refusing to fetch non-khinsider URL: {url}")
        last: Exception | None = None
        for attempt in range(self.max_retries + 1):
            await self.limiter.wait()
            payload = {"cmd": "request.post" if post_data is not None else "request.get", "url": url,
                       "session": self.session, "maxTimeout": self.max_timeout_ms}
            if post_data is not None:
                payload["postData"] = post_data
            try:
                await self._ensure_session()
                data = await self._call(payload)
            except BackendUnavailable:
                raise
            except (httpx.TimeoutException, httpx.TransportError, FetchError) as exc:
                last = exc
                if "session" in str(exc).lower():
                    self._session_ready = False  # FlareSolverr restarted: recreate the session
                wait = _backoff(attempt, self.backoff, None)
                log.warning("FlareSolverr request for %s failed (%s), retry in %.1fs", url, exc, wait)
                await asyncio.sleep(wait)
                continue
            sol = data.get("solution") or {}
            code = int(sol.get("status") or 0)
            html = sol.get("response") or ""
            if is_challenge_page(html):
                self.status.last_blocked_at = time.time()
                self.status.last_blocked_reason = "check still shown after FlareSolverr"
                raise AccessBlocked("FlareSolverr returned the site's check page instead of content "
                                    f"({data.get('message') or 'no message'}). Check the FlareSolverr logs/version.")
            if code in RETRY_STATUSES:
                last = FetchError(f"HTTP {code}")
                wait = _backoff(attempt, self.backoff, (sol.get("headers") or {}).get("retry-after"))
                log.warning("FlareSolverr %s -> %d, retry in %.1fs", url, code, wait)
                await asyncio.sleep(wait)
                continue
            if code == 404:
                raise FetchError(f"not found (404): {url}")
            if code >= 400:
                raise FetchError(f"HTTP {code} for {url}")
            self.status.last_ok_at = time.time()
            state = login_state(html)
            if state is not None:
                self.status.logged_in = state
            return html
        raise FetchError(f"giving up on {url} after {self.max_retries + 1} attempts: {last}")

    async def get_html(self, url: str) -> str:
        async with self._lock:
            return await self._request(url)

    async def login(self, username: str, password: str) -> bool:
        """Submit the site's own login form inside the FlareSolverr session. Never logs the password."""
        async with self._lock:
            html = await self._request(BASE_URL + "/forums/login")
            if login_state(html):
                return True
            form = parse_login_form(html)
            if form["captcha"]:
                raise AccessBlocked("The login form shows a CAPTCHA. Log in in a normal browser instead.")
            fields = {**form["hidden"], "login": username, "password": password}
            if form["has_remember"]:
                fields["remember"] = "1"
            html = await self._request(form["action"], post_data=urlencode(fields))
            ok = bool(login_state(html))
            self.status.logged_in = ok
            log.info("khinsider login via FlareSolverr %s for user %s", "succeeded" if ok else "failed", username)
            return ok

    async def close(self) -> None:
        if self._session_ready:
            try:
                await self._call({"cmd": "sessions.destroy", "session": self.session})
            except Exception:  # shutting down; FlareSolverr may already be gone
                pass
        await self._client.aclose()


class BrowserFetcher:
    """Playwright Chromium with a persistent profile under the data directory."""

    name = "browser"

    def __init__(self, *, profile_dir: Path, limiter: RateLimiter, headless: bool,
                 sandbox: bool, timeout: float, challenge_wait: float = 90.0,
                 status: SessionStatus | None = None):
        self.profile_dir = profile_dir
        self.limiter = limiter
        self.headless = headless
        self.sandbox = sandbox
        self.timeout_ms = int(timeout * 1000)
        self.challenge_wait = challenge_wait
        self.status = status or SessionStatus()
        self._pw = None
        self._ctx = None
        self._lock = asyncio.Lock()

    async def _ensure(self):
        if self._ctx is not None:
            return self._ctx
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:  # pragma: no cover
            self.status.browser_available = False
            raise FetchError("Playwright is not installed") from exc
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        try:
            self.profile_dir.chmod(0o700)
        except OSError:
            pass
        self._pw = await async_playwright().start()
        try:
            self._ctx = await self._pw.chromium.launch_persistent_context(
                str(self.profile_dir),
                headless=self.headless,
                chromium_sandbox=self.sandbox,
                accept_downloads=False,
            )
        except Exception as exc:
            self.status.browser_available = False
            await self._pw.stop()
            self._pw = None
            raise BackendUnavailable(f"could not start browser: {exc}") from exc
        self.status.browser_available = True
        return self._ctx

    async def _goto(self, page, url: str) -> str:
        await self.limiter.wait()
        resp = await page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)
        html = await page.content()
        if is_challenge_page(html):
            self.status.last_blocked_at = time.time()
            if self.headless:
                self.status.last_blocked_reason = "Cloudflare check shown to headless browser"
                raise AccessBlocked(
                    "khinsider showed a Cloudflare check to the headless browser. Run the "
                    "interactive session (`python main.py browser open`, BROWSER_HEADLESS=false) "
                    "and complete the check yourself in the window."
                )
            log.warning("Cloudflare check shown; waiting up to %.0fs for the user to complete it in the browser window", self.challenge_wait)
            deadline = time.monotonic() + self.challenge_wait
            while time.monotonic() < deadline:
                await page.wait_for_timeout(1500)
                html = await page.content()
                if not is_challenge_page(html):
                    break
            else:
                self.status.last_blocked_reason = "Cloudflare check not completed in time"
                raise AccessBlocked("The Cloudflare check in the browser window was not completed in time.")
        if resp is not None and resp.status == 404:
            raise FetchError(f"not found (404): {url}")
        self.status.last_ok_at = time.time()
        state = login_state(html)
        if state is not None:
            self.status.logged_in = state
        return html

    async def get_html(self, url: str) -> str:
        if not is_site_url(url):
            raise FetchError(f"refusing to fetch non-khinsider URL: {url}")
        async with self._lock:
            ctx = await self._ensure()
            page = await ctx.new_page()
            try:
                return await self._goto(page, url)
            finally:
                await page.close()

    async def login(self, username: str, password: str) -> bool:
        """Log in with the site's normal form. Never logs the password.

        Stops with AccessBlocked if the form shows a CAPTCHA: that must be done
        by a person in the visible browser window.
        """
        async with self._lock:
            ctx = await self._ensure()
            page = await ctx.new_page()
            try:
                html = await self._goto(page, BASE_URL + "/forums/login")
                if login_state(html):
                    return True
                form = parse_login_form(html)
                if form["captcha"]:
                    raise AccessBlocked("The login form shows a CAPTCHA. Log in manually in the browser window.")
                await page.fill('form input[name="login"]', username)
                await page.fill('form input[name="password"]', password)
                if form["has_remember"]:
                    await page.check('form input[name="remember"]')
                await self.limiter.wait()
                async with page.expect_navigation(wait_until="domcontentloaded", timeout=self.timeout_ms):
                    await page.click('form:has(input[name="password"]) button[type="submit"]')
                html = await page.content()
                ok = bool(login_state(html))
                self.status.logged_in = ok
                log.info("khinsider login %s for user %s", "succeeded" if ok else "failed", username)
                return ok
            finally:
                await page.close()

    async def open_interactive(self, url: str = BASE_URL) -> None:
        """Open a visible page and leave it for the user (CLI `browser open`)."""
        ctx = await self._ensure()
        page = await ctx.new_page()
        await page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)
        await page.wait_for_event("close", timeout=0)

    async def close(self) -> None:
        if self._ctx is not None:
            await self._ctx.close()
            self._ctx = None
        if self._pw is not None:
            await self._pw.stop()
            self._pw = None


class AutoFetcher:
    """Plain HTTP until the site shows a check, then the fallbacks in order (sticky).

    ``fallbacks`` is ordered by preference, e.g. [FlareSolverr, browser]. A
    fallback that is unreachable (``BackendUnavailable``) is skipped for the next.
    In mode ``http`` there are no fallbacks. In mode ``flaresolverr`` or
    ``browser`` plain HTTP is skipped.
    """

    def __init__(self, http: HttpFetcher, fallbacks: list, mode: str = "auto"):
        self.http = http
        self.fallbacks = [f for f in fallbacks if f is not None]
        self.mode = mode
        self.status = http.status
        for f in self.fallbacks:
            f.status = self.status
        self._active = None if mode in ("auto", "http") else (self.fallbacks[0] if self.fallbacks else None)
        self.status.mode = self._active.name if self._active else "http"

    def backend(self, name: str):
        return next((f for f in self.fallbacks if getattr(f, "name", None) == name), None)

    async def get_html(self, url: str) -> str:
        if self._active is None:
            try:
                return await self.http.get_html(url)
            except AccessBlocked:
                if self.mode == "http" or not self.fallbacks:
                    raise
                log.info("plain HTTP got a Cloudflare check; switching to %s", self.fallbacks[0].name)
                self._active = self.fallbacks[0]
                self.status.mode = self._active.name
        while True:
            try:
                return await self._active.get_html(url)
            except BackendUnavailable as exc:
                idx = self.fallbacks.index(self._active)
                if idx + 1 >= len(self.fallbacks):
                    raise
                log.warning("%s; trying %s", exc, self.fallbacks[idx + 1].name)
                self._active = self.fallbacks[idx + 1]
                self.status.mode = self._active.name

    async def close(self) -> None:
        await self.http.close()
        for f in self.fallbacks:
            await f.close()
