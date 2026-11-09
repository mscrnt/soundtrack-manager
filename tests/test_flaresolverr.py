"""FlareSolverr backend against a mocked FlareSolverr API (no network)."""

import json
import logging
from urllib.parse import parse_qs

import httpx
import pytest

from soundtrack_manager import config
from soundtrack_manager.khinsider.fetch import (AccessBlocked, AutoFetcher, BackendUnavailable, FetchError,
                                                FlareSolverrFetcher, HttpFetcher, RateLimiter)

from .conftest import fixture_html

pytestmark = pytest.mark.anyio

CHALLENGE = fixture_html("cloudflare_challenge.html")


def ok(html: str, status: int = 200, message: str = "Challenge solved!") -> httpx.Response:
    return httpx.Response(200, json={"status": "ok", "message": message,
                                     "solution": {"url": "x", "status": status, "headers": {}, "response": html,
                                                  "cookies": [], "userAgent": "ua"}})


class FakeFS:
    """Records commands; ``pages`` maps (cmd, url-substring) -> response factory."""

    def __init__(self, pages):
        self.pages = pages
        self.calls: list[dict] = []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/v1"
        body = json.loads(req.content)
        self.calls.append(body)
        if body["cmd"] in ("sessions.create", "sessions.destroy"):
            return httpx.Response(200, json={"status": "ok", "message": "", "session": body["session"]})
        for (cmd, needle), resp in self.pages.items():
            if body["cmd"] == cmd and needle in body["url"]:
                return resp() if callable(resp) else resp
        raise AssertionError(f"unexpected FlareSolverr call {body}")


def fs_fetcher(handler, retries=2):
    return FlareSolverrFetcher(endpoint="http://flaresolverr:8191", limiter=RateLimiter(0), timeout=60,
                               max_retries=retries, backoff=0.01, transport=httpx.MockTransport(handler))


async def test_get_html_via_session():
    fs = FakeFS({("request.get", "/search"): ok(fixture_html("search_resident_evil.html"))})
    f = fs_fetcher(fs)
    html = await f.get_html("https://downloads.khinsider.com/search?search=resident+evil")
    assert "albumList" in html
    assert [c["cmd"] for c in fs.calls] == ["sessions.create", "request.get"]
    assert fs.calls[1]["session"] == "soundtrack-manager" and fs.calls[1]["maxTimeout"] == 60000
    await f.get_html("https://downloads.khinsider.com/search?search=resident+evil")
    assert [c["cmd"] for c in fs.calls].count("sessions.create") == 1  # session reused
    await f.close()
    assert fs.calls[-1]["cmd"] == "sessions.destroy"


async def test_still_challenged_is_reported_not_looped():
    fs = FakeFS({("request.get", "/"): ok(CHALLENGE, status=403)})
    f = fs_fetcher(fs)
    with pytest.raises(AccessBlocked, match="FlareSolverr"):
        await f.get_html("https://downloads.khinsider.com/")
    assert sum(c["cmd"] == "request.get" for c in fs.calls) == 1


async def test_flaresolverr_error_retried_then_ok():
    seq = iter([httpx.Response(500, json={"status": "error", "message": "Error solving the challenge. Timeout"}),
                ok("<div id='pageContent'><p>Found 0 matching results.</p></div>")])
    f = fs_fetcher(FakeFS({("request.get", "/search"): lambda: next(seq)}))
    assert "Found 0" in await f.get_html("https://downloads.khinsider.com/search?search=x")


async def test_site_503_through_flaresolverr_retried():
    seq = iter([ok("busy", status=503), ok("<html>fine</html>")])
    f = fs_fetcher(FakeFS({("request.get", "/"): lambda: next(seq)}))
    assert await f.get_html("https://downloads.khinsider.com/") == "<html>fine</html>"


async def test_404_and_foreign_url():
    f = fs_fetcher(FakeFS({("request.get", "/album/"): ok("gone", status=404)}))
    with pytest.raises(FetchError, match="404"):
        await f.get_html("https://downloads.khinsider.com/game-soundtracks/album/x")
    with pytest.raises(FetchError, match="non-khinsider"):
        await f.get_html("https://example.com/")


async def test_unreachable_raises_backend_unavailable():
    def refuse(req):
        raise httpx.ConnectError("connection refused")
    with pytest.raises(BackendUnavailable, match="container running"):
        await fs_fetcher(refuse).get_html("https://downloads.khinsider.com/")


async def test_session_recreated_after_flaresolverr_restart():
    state = {"n": 0}

    def get():
        state["n"] += 1
        if state["n"] == 1:
            return httpx.Response(500, json={"status": "error", "message": "This session does not exist."})
        return ok("<html>ok</html>")

    fs = FakeFS({("request.get", "/"): get})
    f = fs_fetcher(fs)
    assert await f.get_html("https://downloads.khinsider.com/") == "<html>ok</html>"
    assert [c["cmd"] for c in fs.calls].count("sessions.create") == 2


async def test_login_posts_site_form_with_token(caplog):
    logged_in = '<div id="leftColumn"><a href="/forums/index.php?logout/&t=1">Log Out</a></div>'
    fs = FakeFS({("request.get", "/forums/login"): ok(fixture_html("login.html")),
                 ("request.post", "login/login"): ok(logged_in)})
    f = fs_fetcher(fs)
    with caplog.at_level(logging.DEBUG):
        assert await f.login("me", "s3cret-pw!") is True
    post = next(c for c in fs.calls if c["cmd"] == "request.post")
    assert post["url"] == "https://downloads.khinsider.com/forums/index.php?login/login"
    fields = parse_qs(post["postData"])
    assert fields["login"] == ["me"] and fields["password"] == ["s3cret-pw!"] and fields["remember"] == ["1"]
    assert fields["_xfToken"] == ["REDACTED"]  # hidden token from the fixture is sent back
    assert post["session"] == "soundtrack-manager"
    assert "s3cret-pw!" not in caplog.text
    assert f.status.logged_in is True


async def test_login_stops_on_captcha():
    page = fixture_html("login.html").replace("</form>", '<div class="cf-turnstile" data-sitekey="x"></div></form>', 1)
    fs = FakeFS({("request.get", "/forums/login"): ok(page)})
    with pytest.raises(AccessBlocked, match="CAPTCHA"):
        await fs_fetcher(fs).login("me", "pw")
    assert not any(c["cmd"] == "request.post" for c in fs.calls)


class _Fake:
    def __init__(self, name, exc=None):
        self.name, self.exc, self.status, self.urls = name, exc, None, []

    async def get_html(self, url):
        self.urls.append(url)
        if self.exc:
            raise self.exc
        return self.name

    async def close(self):
        pass


def _blocked_http():
    return HttpFetcher(limiter=RateLimiter(0), timeout=5, max_retries=0, backoff=0.01,
                       transport=httpx.MockTransport(lambda r: httpx.Response(403, text=CHALLENGE)))


async def test_auto_prefers_flaresolverr_then_browser():
    fs, br = _Fake("flaresolverr"), _Fake("browser")
    auto = AutoFetcher(_blocked_http(), [fs, br], "auto")
    assert await auto.get_html("https://downloads.khinsider.com/") == "flaresolverr"
    assert auto.status.mode == "flaresolverr" and br.urls == []


async def test_auto_skips_unreachable_flaresolverr():
    fs, br = _Fake("flaresolverr", BackendUnavailable("down")), _Fake("browser")
    auto = AutoFetcher(_blocked_http(), [fs, br], "auto")
    assert await auto.get_html("https://downloads.khinsider.com/") == "browser"
    assert auto.status.mode == "browser"


async def test_flaresolverr_mode_skips_plain_http():
    fs = _Fake("flaresolverr")
    http = _blocked_http()
    auto = AutoFetcher(http, [fs], "flaresolverr")
    assert await auto.get_html("https://downloads.khinsider.com/") == "flaresolverr"


def test_flaresolverr_url_defaults_and_disable(tmp_path, monkeypatch):
    monkeypatch.setenv("STM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("STM_DOWNLOAD_DIR", str(tmp_path))
    monkeypatch.delenv("FLARESOLVERR_URL", raising=False)
    assert config.load_settings().flaresolverr_url == "http://127.0.0.1:8191"  # on by default
    monkeypatch.setenv("FLARESOLVERR_URL", "")
    assert config.load_settings().flaresolverr_url is None
    monkeypatch.setenv("FLARESOLVERR_URL", "http://flaresolverr:8191")
    assert config.load_settings().flaresolverr_url == "http://flaresolverr:8191"
    monkeypatch.setenv("FLARESOLVERR_URL", "ftp://nope")
    with pytest.raises(ValueError):
        config.load_settings()
