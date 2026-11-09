import base64

import pytest
from fastapi.testclient import TestClient

from soundtrack_manager import queue
from soundtrack_manager.cli import build_parser, parse_selection
from soundtrack_manager.config import Secret
from soundtrack_manager.runtime import AppContext
from soundtrack_manager.web.app import create_app

from .conftest import FakeFetcher

HX = {"HX-Request": "true"}


@pytest.fixture
def client(ctx):
    with TestClient(create_app(ctx)) as c:
        yield c


def test_healthz(client):
    r = client.get("/healthz")
    assert r.status_code == 200 and r.json()["status"] == "ok"


def test_pages_render(client):
    for path in ("/discover", "/downloads", "/library", "/settings", "/downloads/jobs", "/settings/logs"):
        r = client.get(path)
        assert r.status_code == 200, path
    assert "default-src 'self'" in client.get("/discover").headers["content-security-policy"]


def test_discover_full_page_and_partial(client):
    r = client.get("/discover", params={"q": "resident evil"})
    assert "Resident Evil: Save Room Compilation" in r.text and "Select page" in r.text
    r = client.get("/discover/results", params={"q": "resident evil", "per_page": 25})
    assert "<html" not in r.text and "10</strong> albums" in r.text


def test_discover_validation_error_shown(client):
    r = client.get("/discover/results", params={"q": "x", "platform": "nope"})
    assert "unknown platform" in r.text


def test_access_blocked_message(settings):
    ctx = AppContext(settings, fetcher=FakeFetcher(blocked=True), configure_logging=False)
    with TestClient(create_app(ctx)) as c:
        r = c.get("/discover/results", params={"q": "resident evil"})
        assert "Access check" in r.text


def test_queue_add_and_actions(client, ctx):
    client.get("/discover/results", params={"q": "resident evil"})
    ids = [ctx.discovery.get_album(1)["id"], ctx.discovery.get_album(2)["id"]]
    r = client.post("/queue/add", data={"album_id": [str(i) for i in ids], "format": "mp3"}, headers=HX)
    assert "Added <strong>2</strong>" in r.text
    r = client.post("/queue/add", data={"album_id": [str(ids[0])], "format": "mp3"}, headers=HX)
    assert "1 already in the queue" in r.text
    job = queue.list_jobs(ctx.db)[0]
    r = client.post(f"/jobs/{job['id']}/pause", headers=HX)
    assert "paused" in r.text


def test_queue_add_all_matching(client, ctx):
    r = client.post("/queue/add-all", data={"q": "resident evil", "year": "2005-2016", "format": "flac"}, headers=HX)
    assert "Added <strong>" in r.text
    assert all(2005 <= j["year"] <= 2016 for j in queue.list_jobs(ctx.db))


def test_csrf_requires_htmx_header_and_same_origin(client):
    assert client.post("/queue/add", data={"album_id": "1"}).status_code == 403
    r = client.post("/queue/add", data={"album_id": "1"}, headers={**HX, "Origin": "https://evil.example"})
    assert r.status_code == 403


def test_album_details_partial(client, ctx):
    client.get("/discover/results", params={"q": "resident evil"})
    aid = next(r["id"] for r in [ctx.discovery.get_album(i) for i in range(1, 11)]
               if r["slug"] == "resident-evil-save-room-compilation-2023")
    r = client.get(f"/albums/{aid}/details")
    assert "Bronson Bragg" in r.text and "7 track(s)" in r.text and "Est. MP3" in r.text


def test_settings_update_and_validation(client, ctx):
    r = client.post("/settings", data={"folder_template": "{platform}/{year}/{album}", "preferred_format": "mp3"}, headers=HX)
    assert "Settings saved" in r.text
    assert ctx.settings.folder_template == "{platform}/{year}/{album}"
    assert ctx.settings.preferred_format == "mp3"
    r = client.post("/settings", data={"folder_template": "../{album}"}, headers=HX)
    assert "folder_template" in r.text and ctx.settings.folder_template == "{platform}/{year}/{album}"
    # survives restart (stored in DB)
    ctx2 = AppContext(ctx.base_settings, fetcher=FakeFetcher(), configure_logging=False)
    assert ctx2.settings.preferred_format == "mp3"


def test_basic_auth(settings):
    settings.web_password = Secret("letmein")
    settings.khinsider_password = Secret("site-secret")
    settings.khinsider_username = "me"
    ctx = AppContext(settings, fetcher=FakeFetcher(), configure_logging=False)
    with TestClient(create_app(ctx)) as c:
        assert c.get("/discover").status_code == 401
        assert c.get("/healthz").status_code == 200
        good = {"Authorization": "Basic " + base64.b64encode(b"admin:letmein").decode()}
        bad = {"Authorization": "Basic " + base64.b64encode(b"admin:nope").decode()}
        assert c.get("/discover", headers=bad).status_code == 401
        page = c.get("/settings", headers=good)
        assert page.status_code == 200
        assert "letmein" not in page.text and "site-secret" not in page.text


# ---------------------------------------------------------------- CLI

@pytest.mark.parametrize("text,n,expected", [
    ("1,3,5-7", 10, [0, 2, 4, 5, 6]), ("all", 3, [0, 1, 2]), ("", 5, []), ("2 4", 5, [1, 3]), ("3-1", 5, [0, 1, 2]),
])
def test_parse_selection(text, n, expected):
    assert parse_selection(text, n) == expected


@pytest.mark.parametrize("bad", ["0", "11", "a", "1-99"])
def test_parse_selection_rejects(bad):
    with pytest.raises(ValueError):
        parse_selection(bad, 10)


def test_cli_parser_matches_documented_commands():
    p = build_parser()
    for argv in (["search", "Final Fantasy", "--platform", "ps1"], ["search", "--type", "gamerip", "--platform", "snes"],
                 ["discover", "--platform", "n64", "--year", "1998"], ["queue", "list"], ["download", "--resume"],
                 ["download", "--dry-run"], ["history", "sync"], ["library", "scan", "/music"], ["browser", "open"]):
        p.parse_args(argv)
