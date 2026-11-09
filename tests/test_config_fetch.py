import json
import logging

import httpx
import pytest

from soundtrack_manager import config
from soundtrack_manager.config import Secret, Settings
from soundtrack_manager.khinsider.fetch import (AccessBlocked, AutoFetcher, FetchError, HttpFetcher,
                                                RateLimiter)
from soundtrack_manager.logging_setup import RedactingFilter

from .conftest import fixture_html


# ---------------------------------------------------------------- config

def test_secret_never_printed():
    s = Secret("hunter2")
    assert "hunter2" not in repr(s) and "hunter2" not in str(s) and s.get() == "hunter2"


def test_read_secret_file_and_env(tmp_path, monkeypatch):
    f = tmp_path / "pw.txt"
    f.write_text("from-file\n")
    monkeypatch.setenv("KHINSIDER_PASSWORD_FILE", str(f))
    monkeypatch.setenv("KHINSIDER_PASSWORD", "from-env")
    assert config.read_secret("KHINSIDER_PASSWORD").get() == "from-file"
    monkeypatch.delenv("KHINSIDER_PASSWORD_FILE")
    monkeypatch.setattr(config, "SECRETS_DIR", tmp_path / "none")
    assert config.read_secret("KHINSIDER_PASSWORD").get() == "from-env"


def test_read_secret_docker_secrets_dir(tmp_path, monkeypatch):
    (tmp_path / "khinsider_password").write_text("docker-secret")
    monkeypatch.setattr(config, "SECRETS_DIR", tmp_path)
    monkeypatch.delenv("KHINSIDER_PASSWORD", raising=False)
    monkeypatch.delenv("KHINSIDER_PASSWORD_FILE", raising=False)
    assert config.read_secret("KHINSIDER_PASSWORD").get() == "docker-secret"


def test_public_dict_hides_secrets(tmp_path):
    s = Settings(data_dir=tmp_path, download_dir=tmp_path, web_password=Secret("pw1234"),
                 khinsider_password=Secret("site-pass"))
    dumped = json.dumps(s.public_dict())
    assert "pw1234" not in dumped and "site-pass" not in dumped
    assert s.public_dict()["khinsider_password"] == "set"


def test_network_exposure_requires_password(tmp_path):
    s = Settings(data_dir=tmp_path, download_dir=tmp_path, web_host="0.0.0.0", web_published_bind="0.0.0.0")
    with pytest.raises(ValueError, match="WEB_PASSWORD"):
        s.validate()
    s.web_published_bind = "127.0.0.1"   # container binds 0.0.0.0 but host publishes on loopback only
    s.validate()
    s.web_published_bind = "192.168.1.5"
    s.web_password = Secret("x")
    s.validate()


def test_load_settings_from_env(tmp_path, monkeypatch):
    monkeypatch.setenv("STM_DATA_DIR", str(tmp_path / "d"))
    monkeypatch.setenv("STM_DOWNLOAD_DIR", str(tmp_path / "dl"))
    monkeypatch.setenv("MAX_CONCURRENT_DOWNLOADS", "3")
    monkeypatch.setenv("PREFERRED_FORMAT", "MP3")
    s = config.load_settings()
    assert s.max_concurrent_downloads == 3 and s.preferred_format == "mp3"
    monkeypatch.setenv("MAX_CONCURRENT_DOWNLOADS", "99")
    with pytest.raises(ValueError):
        config.load_settings()


# ---------------------------------------------------------------- fetch

def _http(handler, retries=2):
    return HttpFetcher(limiter=RateLimiter(0), timeout=5, max_retries=retries, backoff=0.01,
                       transport=httpx.MockTransport(handler))


@pytest.mark.anyio
async def test_challenge_raises_access_blocked_without_retry():
    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(403, text=fixture_html("cloudflare_challenge.html"))

    f = _http(handler)
    with pytest.raises(AccessBlocked):
        await f.get_html("https://downloads.khinsider.com/search?search=x")
    assert len(calls) == 1  # never hammer a challenge
    assert f.status.last_blocked_at is not None
    await f.close()


@pytest.mark.anyio
async def test_retry_on_503_then_success():
    seq = iter([httpx.Response(503), httpx.Response(429, headers={"Retry-After": "0"}),
                httpx.Response(200, text=fixture_html("search_resident_evil.html"))])
    f = _http(lambda req: next(seq))
    html = await f.get_html("https://downloads.khinsider.com/search?search=resident+evil")
    assert "albumList" in html
    await f.close()


@pytest.mark.anyio
async def test_gives_up_after_retries():
    f = _http(lambda req: httpx.Response(502), retries=1)
    with pytest.raises(FetchError, match="giving up"):
        await f.get_html("https://downloads.khinsider.com/")
    await f.close()


@pytest.mark.anyio
async def test_404_not_retried():
    calls = []
    f = _http(lambda req: calls.append(1) or httpx.Response(404))
    with pytest.raises(FetchError, match="404"):
        await f.get_html("https://downloads.khinsider.com/game-soundtracks/album/nope")
    assert len(calls) == 1
    await f.close()


@pytest.mark.anyio
async def test_refuses_foreign_hosts():
    f = _http(lambda req: httpx.Response(200, text="x"))
    with pytest.raises(FetchError):
        await f.get_html("https://example.com/")
    await f.close()


@pytest.mark.anyio
async def test_rate_limiter_spacing():
    import time
    lim = RateLimiter(0.05)
    stamps = []
    for _ in range(3):
        await lim.wait()
        stamps.append(lim._last)
    # every gap honours the minimum interval by the monotonic clock (also on Windows' coarse timer)
    assert all(b - a >= 0.05 for a, b in zip(stamps, stamps[1:]))


class _FakeBrowser:
    def __init__(self):
        self.name = "browser"
        self.status = None
        self.urls = []

    async def get_html(self, url):
        self.urls.append(url)
        return "<html>browser</html>"

    async def close(self):
        pass


@pytest.mark.anyio
async def test_auto_switches_to_browser_after_block():
    http = _http(lambda req: httpx.Response(403, text=fixture_html("cloudflare_challenge.html")))
    b = _FakeBrowser()
    auto = AutoFetcher(http, [b], "auto")
    assert await auto.get_html("https://downloads.khinsider.com/a") == "<html>browser</html>"
    assert auto.status.mode == "browser"
    await auto.get_html("https://downloads.khinsider.com/b")
    assert b.urls == ["https://downloads.khinsider.com/a", "https://downloads.khinsider.com/b"]  # sticky


@pytest.mark.anyio
async def test_http_mode_reports_block():
    http = _http(lambda req: httpx.Response(403, text=fixture_html("cloudflare_challenge.html")))
    auto = AutoFetcher(http, [], "http")
    with pytest.raises(AccessBlocked):
        await auto.get_html("https://downloads.khinsider.com/")


# ---------------------------------------------------------------- logging

def test_log_redaction():
    f = RedactingFilter()
    f.add_secret("s3cr3t-pass")
    rec = logging.LogRecord("x", logging.INFO, __file__, 1, "login with %s; Cookie: cf_clearance=abc; password=hunter2",
                            ("s3cr3t-pass",), None)
    f.filter(rec)
    msg = rec.getMessage()
    assert "s3cr3t-pass" not in msg and "abc" not in msg and "hunter2" not in msg
