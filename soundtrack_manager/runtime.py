"""Wires settings, database, fetchers and services together (used by CLI and web)."""

from __future__ import annotations

import logging
import os
from dataclasses import replace

from . import queue
from .config import FORMATS, RUNTIME_KEYS, Settings, load_settings
from .db import Database
from .discovery import DiscoveryService
from .khinsider.client import KhinsiderClient
from .khinsider.fetch import (AutoFetcher, BackendUnavailable, BrowserFetcher, Fetcher, FlareSolverrFetcher,
                              HttpFetcher, RateLimiter, SessionStatus)
from .logging_setup import REDACTOR, configure
from .paths import validate_template

log = logging.getLogger(__name__)


def _check_writable(label: str, path) -> None:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".write-test"
        probe.write_text("ok")
        probe.unlink()
    except OSError as exc:
        uid = getattr(os, "getuid", lambda: "?")()
        raise SystemExit(
            f"The {label} directory {path} is not writable by this process (uid {uid}): {exc}. "
            "On Docker, create the host folder first and make sure it is owned by PUID/PGID "
            "from .env (e.g. `sudo chown -R 1000:1000 ./data ./downloads`)."
        ) from exc


class AppContext:
    def __init__(self, settings: Settings | None = None, *, fetcher: Fetcher | None = None,
                 configure_logging: bool = True):
        self.base_settings = settings or load_settings()
        for label, d in (("data", self.base_settings.data_dir), ("download", self.base_settings.download_dir)):
            _check_writable(label, d)
        if configure_logging:
            configure(self.base_settings.log_level, self.base_settings.log_path)
        REDACTOR.add_secret(self.base_settings.web_password.get())
        REDACTOR.add_secret(self.base_settings.khinsider_password.get())
        REDACTOR.add_secret(self.base_settings.llm_api_key.get())
        self.db = Database(self.base_settings.db_path)
        applied = self.db.migrate()
        if applied:
            log.info("applied migrations: %s", ", ".join(applied))
        recovered = queue.recover_interrupted(self.db)
        if recovered:
            log.info("re-queued %d job(s) interrupted by a previous shutdown", recovered)
        self.settings = self.effective_settings()
        self.status = SessionStatus()
        self.limiter: RateLimiter | None = None
        self.browser: BrowserFetcher | None = None
        self.flaresolverr: FlareSolverrFetcher | None = None
        self.worker = None  # DownloadWorker, started by the web app or `main.py download`
        self.fetcher = fetcher or self._build_fetcher()
        if fetcher is not None and getattr(fetcher, "status", None) is not None:
            self.status = fetcher.status
        self.client = KhinsiderClient(self.fetcher)
        self.discovery = DiscoveryService(
            self.db, self.client, cache_ttl_hours=self.settings.cache_ttl_hours,
            max_browse_pages=self.settings.discover_max_pages)

    def effective_settings(self) -> Settings:
        overrides = {k: v for k, v in self.db.get_overrides().items() if k in RUNTIME_KEYS}
        return replace(self.base_settings, **overrides)

    def update_runtime_setting(self, key: str, raw: str) -> None:
        if key not in RUNTIME_KEYS:
            raise ValueError(f"{key} cannot be changed at runtime")
        typ = RUNTIME_KEYS[key]
        raw = (raw or "").strip()
        value = typ(raw) if raw or typ is str else None
        if key == "preferred_format" and value not in FORMATS:
            raise ValueError(f"format must be one of {FORMATS}")
        if key == "max_concurrent_downloads" and not 1 <= value <= 8:
            raise ValueError("concurrency must be between 1 and 8")
        if key == "request_delay_seconds" and value < 0.5:
            raise ValueError("request delay must be at least 0.5 seconds")
        if key == "max_retries" and not 0 <= value <= 10:
            raise ValueError("retries must be between 0 and 10")
        if key == "folder_template":
            validate_template(value)
        if key == "llm_endpoint" and value and not value.startswith(("http://", "https://")):
            raise ValueError("LLM endpoint must be an http(s) URL")
        self.db.set_override(key, value or None)
        self.settings = self.effective_settings()
        if key == "request_delay_seconds" and self.limiter is not None:
            self.limiter.min_interval = self.settings.request_delay_seconds

    def _build_fetcher(self) -> Fetcher:
        s = self.settings
        self.limiter = RateLimiter(s.request_delay_seconds)
        http = HttpFetcher(limiter=self.limiter, timeout=s.request_timeout_seconds,
                           max_retries=s.max_retries, backoff=s.retry_backoff_seconds, status=self.status)
        fallbacks = []
        if s.flaresolverr_url and s.fetch_mode in ("auto", "flaresolverr"):
            self.flaresolverr = FlareSolverrFetcher(
                endpoint=s.flaresolverr_url, limiter=self.limiter, timeout=s.flaresolverr_timeout_seconds,
                max_retries=s.max_retries, backoff=s.retry_backoff_seconds, status=self.status)
            fallbacks.append(self.flaresolverr)
        if s.fetch_mode in ("auto", "browser"):
            self.browser = BrowserFetcher(profile_dir=s.browser_profile_dir, limiter=self.limiter,
                                          headless=s.browser_headless, sandbox=s.browser_sandbox,
                                          timeout=s.request_timeout_seconds, status=self.status)
            fallbacks.append(self.browser)
        return AutoFetcher(http, fallbacks, s.fetch_mode)

    async def login(self) -> bool:
        """Log in with configured credentials (KHINSIDER_USERNAME + password secret).

        Uses the backend that later requests will use: FlareSolverr if configured
        (its session keeps the cookies), otherwise the browser profile.
        """
        s = self.base_settings
        if not s.khinsider_username or not s.khinsider_password:
            raise ValueError("KHINSIDER_USERNAME and KHINSIDER_PASSWORD (or its secret file) are not configured")
        for backend in (self.flaresolverr, self.browser):
            if backend is None:
                continue
            try:
                ok = await backend.login(s.khinsider_username, s.khinsider_password.get())
            except BackendUnavailable as exc:
                log.warning("%s; trying next login method", exc)
                continue
            if ok and isinstance(self.fetcher, AutoFetcher):
                self.fetcher._active = backend  # keep using the logged-in session
                self.status.mode = backend.name
            return ok
        raise ValueError("login needs FlareSolverr (FLARESOLVERR_URL) or the browser session (FETCH_MODE=auto/browser)")

    def wake_worker(self) -> None:
        if self.worker is not None:
            self.worker.wake()

    async def close(self) -> None:
        if self.worker is not None:
            await self.worker.stop()
            self.worker = None
        await self.fetcher.close()
