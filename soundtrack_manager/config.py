"""Configuration from environment variables (``.env`` in Docker Compose).

Secrets follow the Docker convention: for a secret ``FOO`` the value is read
from the file named by ``FOO_FILE``, else from ``/run/secrets/foo`` if that
file exists, else from the ``FOO`` variable itself. Secret values are wrapped
in :class:`Secret`, so they never appear in ``repr()``, logs or JSON.
"""

from __future__ import annotations

import ipaddress
import os
import re
from dataclasses import dataclass, field, fields
from pathlib import Path

SECRETS_DIR = Path(os.environ.get("STM_SECRETS_DIR", "/run/secrets"))

FORMATS = ("flac", "mp3")
# Native default: FlareSolverr published on localhost (compose sets http://flaresolverr:8191).
DEFAULT_FLARESOLVERR_URL = "http://127.0.0.1:8191"


class Secret:
    __slots__ = ("_value",)

    def __init__(self, value: str | None):
        self._value = value or None

    def get(self) -> str | None:
        return self._value

    def __bool__(self) -> bool:
        return bool(self._value)

    def __repr__(self) -> str:
        return "Secret(set)" if self._value else "Secret(unset)"

    __str__ = __repr__


def _env(name: str, default: str | None = None) -> str | None:
    v = os.environ.get(name)
    return v if v not in (None, "") else default


def read_secret(name: str) -> Secret:
    path = _env(f"{name}_FILE")
    candidates = [Path(path)] if path else [SECRETS_DIR / name.lower()]
    for p in candidates:
        try:
            if p.is_file():
                return Secret(p.read_text(encoding="utf-8").strip())
        except OSError:
            pass
    return Secret(_env(name))


def _bool(name: str, default: bool) -> bool:
    v = _env(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _int(name: str, default: int, lo: int | None = None, hi: int | None = None) -> int:
    v = int(_env(name, str(default)))
    if lo is not None and v < lo or hi is not None and v > hi:
        raise ValueError(f"{name}={v} out of range [{lo}, {hi}]")
    return v


def _float(name: str, default: float, lo: float = 0.0) -> float:
    v = float(_env(name, str(default)))
    if v < lo:
        raise ValueError(f"{name}={v} must be >= {lo}")
    return v


def is_loopback(host: str) -> bool:
    if host in ("localhost", ""):
        return host == "localhost"
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


@dataclass
class Settings:
    data_dir: Path
    download_dir: Path
    web_host: str = "127.0.0.1"
    web_port: int = 8080
    # Host-side address Compose publishes the port on. Lets the app inside a
    # container know whether it is reachable from the network.
    web_published_bind: str | None = None
    web_username: str = "admin"
    web_password: Secret = field(default_factory=lambda: Secret(None))
    web_allow_insecure: bool = False

    # Runtime-tunable (can be overridden on the Settings page; stored in DB)
    preferred_format: str = "flac"
    max_concurrent_downloads: int = 2
    request_delay_seconds: float = 1.5
    max_retries: int = 4
    retry_backoff_seconds: float = 2.0
    folder_template: str = "{platform}/{album}"
    llm_endpoint: str | None = None
    llm_model: str | None = None
    llm_api_key: Secret = field(default_factory=lambda: Secret(None))  # optional, for servers that need one

    worker_enabled: bool = True   # run the download worker inside the web process
    # Extra existing soundtrack folders to scan (container paths, read-only mounts are fine)
    library_extra_dirs: list[Path] = field(default_factory=list)
    request_timeout_seconds: float = 30.0
    cache_ttl_hours: int = 24
    discover_max_pages: int = 20
    log_level: str = "INFO"

    fetch_mode: str = "auto"  # auto | http | flaresolverr | browser
    # FlareSolverr endpoint. On by default; set FLARESOLVERR_URL= (empty) to disable.
    flaresolverr_url: str | None = None
    flaresolverr_timeout_seconds: float = 60.0
    browser_headless: bool = True
    browser_sandbox: bool = False

    khinsider_username: str | None = None
    khinsider_password: Secret = field(default_factory=lambda: Secret(None))

    @property
    def db_path(self) -> Path:
        return self.data_dir / "soundtrack-manager.sqlite3"

    @property
    def browser_profile_dir(self) -> Path:
        return self.data_dir / "browser-profile"

    @property
    def log_path(self) -> Path:
        return self.data_dir / "logs" / "app.log"

    @property
    def library_roots(self) -> list[Path]:
        roots = [self.download_dir] + [p for p in self.library_extra_dirs if p != self.download_dir]
        return [p.resolve() for p in roots]

    @property
    def network_exposed(self) -> bool:
        bind = self.web_published_bind if self.web_published_bind is not None else self.web_host
        return not is_loopback(bind)

    @property
    def auth_enabled(self) -> bool:
        return bool(self.web_password)

    def public_dict(self) -> dict:
        """Settings safe to display: secrets are reduced to set/unset."""
        out = {}
        for f in fields(self):
            v = getattr(self, f.name)
            if isinstance(v, Secret):
                v = "set" if v else "unset"
            elif isinstance(v, Path):
                v = str(v)
            elif isinstance(v, list):
                v = [str(x) for x in v]
            out[f.name] = v
        return out

    def validate(self) -> None:
        if self.preferred_format not in FORMATS:
            raise ValueError(f"PREFERRED_FORMAT must be one of {FORMATS}")
        if self.fetch_mode not in ("auto", "http", "flaresolverr", "browser"):
            raise ValueError("FETCH_MODE must be auto, http, flaresolverr or browser")
        if self.fetch_mode == "flaresolverr" and not self.flaresolverr_url:
            raise ValueError("FETCH_MODE=flaresolverr needs FLARESOLVERR_URL")
        if self.flaresolverr_url and not self.flaresolverr_url.startswith(("http://", "https://")):
            raise ValueError("FLARESOLVERR_URL must be an http(s) URL")
        if self.network_exposed and not self.auth_enabled and not self.web_allow_insecure:
            raise ValueError(
                "The web UI is reachable from the network but WEB_PASSWORD is not set. "
                "Set WEB_PASSWORD (or WEB_PASSWORD_FILE), bind to 127.0.0.1, or set "
                "WEB_ALLOW_INSECURE=true if you accept the risk."
            )


def load_settings() -> Settings:
    s = Settings(
        data_dir=Path(_env("STM_DATA_DIR", "./data")).resolve(),
        download_dir=Path(_env("STM_DOWNLOAD_DIR", "./downloads")).resolve(),
        web_host=_env("WEB_HOST", "127.0.0.1"),
        web_port=_int("WEB_PORT", 8080, 1, 65535),
        web_published_bind=_env("WEB_PUBLISHED_BIND"),
        web_username=_env("WEB_USERNAME", "admin"),
        web_password=read_secret("WEB_PASSWORD"),
        web_allow_insecure=_bool("WEB_ALLOW_INSECURE", False),
        preferred_format=_env("PREFERRED_FORMAT", "flac").lower(),
        max_concurrent_downloads=_int("MAX_CONCURRENT_DOWNLOADS", 2, 1, 8),
        request_delay_seconds=_float("REQUEST_DELAY_SECONDS", 1.5, 0.5),
        max_retries=_int("MAX_RETRIES", 4, 0, 10),
        retry_backoff_seconds=_float("RETRY_BACKOFF_SECONDS", 2.0, 0.1),
        folder_template=_env("FOLDER_TEMPLATE", "{platform}/{album}"),
        llm_endpoint=_env("LLM_ENDPOINT"),
        llm_model=_env("LLM_MODEL"),
        llm_api_key=read_secret("LLM_API_KEY"),
        worker_enabled=_bool("WORKER_ENABLED", True),
        library_extra_dirs=[Path(p.strip()) for p in re.split(r"[;,]", _env("LIBRARY_EXTRA_DIRS", "") or "") if p.strip()],
        request_timeout_seconds=_float("REQUEST_TIMEOUT_SECONDS", 30.0, 1.0),
        cache_ttl_hours=_int("CACHE_TTL_HOURS", 24, 0),
        discover_max_pages=_int("DISCOVER_MAX_PAGES", 20, 1, 200),
        log_level=_env("LOG_LEVEL", "INFO").upper(),
        fetch_mode=_env("FETCH_MODE", "auto").lower(),
        # Unset -> default local FlareSolverr; set but empty -> disabled.
        flaresolverr_url=(os.environ["FLARESOLVERR_URL"].strip() or None) if "FLARESOLVERR_URL" in os.environ
        else DEFAULT_FLARESOLVERR_URL,
        flaresolverr_timeout_seconds=_float("FLARESOLVERR_TIMEOUT_SECONDS", 60.0, 10.0),
        browser_headless=_bool("BROWSER_HEADLESS", True),
        browser_sandbox=_bool("BROWSER_SANDBOX", False),
        khinsider_username=_env("KHINSIDER_USERNAME"),
        khinsider_password=read_secret("KHINSIDER_PASSWORD"),
    )
    s.validate()
    return s


# Settings the Settings page may change at runtime, with validators.
RUNTIME_KEYS: dict[str, type] = {
    "preferred_format": str,
    "max_concurrent_downloads": int,
    "request_delay_seconds": float,
    "max_retries": int,
    "folder_template": str,
    "llm_endpoint": str,
    "llm_model": str,
}
