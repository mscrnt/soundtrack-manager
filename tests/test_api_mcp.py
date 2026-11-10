"""REST API (/api/v1), Swagger docs and the MCP server (/mcp): switches, access rules, calls."""

from __future__ import annotations

import base64
import json
import time

import pytest
from fastapi.testclient import TestClient

from soundtrack_manager.config import Secret
from soundtrack_manager.runtime import AppContext
from soundtrack_manager.web.app import create_app

TOKEN = "t" * 32
SAVE_ROOM = "https://downloads.khinsider.com/game-soundtracks/album/resident-evil-save-room-compilation-2023"
LIST = """PRIORITY 1 - BROKEN
-------------------
[ ]   1. Resident Evil: Save Room Compilation (2023)  - track 3 was corrupt
           https://downloads.khinsider.com/search?search=resident+evil
[ ]   2. Castlevania (NES) Soundtrack
           https://downloads.khinsider.com/search?search=Castlevania
[ ]   3. Wild Arms 3 - Soundtrack (2002)
"""
MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


def make_client(settings, fake_fetcher, **overrides):
    for k, v in overrides.items():
        setattr(settings, k, v)
    ctx = AppContext(settings, fetcher=fake_fetcher, configure_logging=False)
    return TestClient(create_app(ctx))


@pytest.fixture
def api(settings, fake_fetcher):
    with make_client(settings, fake_fetcher, api_enabled=True, api_token=Secret(TOKEN)) as c:
        c.headers["Authorization"] = f"Bearer {TOKEN}"
        yield c


def basic(user, pw):
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()}


# ------------------------------------------------------------ switches and access

def test_api_and_mcp_are_off_by_default(settings, fake_fetcher):
    with make_client(settings, fake_fetcher) as c:
        r = c.get("/api/v1/status")
        assert r.status_code == 404 and "API_ENABLED" in r.json()["detail"]
        r = c.post("/mcp", json={}, headers=MCP_HEADERS)
        assert r.status_code == 404 and "MCP_ENABLED" in r.json()["detail"]


def test_open_mode_is_localhost_only(settings, fake_fetcher):
    with make_client(settings, fake_fetcher, api_enabled=True) as c:
        assert c.get("/api/v1/status", headers={"Host": "127.0.0.1:8080"}).status_code == 200
        assert c.get("/api/v1/status", headers={"Host": "localhost"}).status_code == 200
        assert c.get("/api/v1/status", headers={"Host": "[::1]:8080"}).status_code == 200
        r = c.get("/api/v1/status", headers={"Host": "attacker.example"})  # DNS rebinding
        assert r.status_code == 403 and "API_ALLOWED_HOSTS" in r.json()["detail"]
        assert c.get("/api/v1/status").status_code == 403  # Host 'testserver' is not loopback
        # A web page can't use the API cross-site, even on localhost.
        r = c.post("/api/v1/imports/validate", content=LIST,
                   headers={"Host": "127.0.0.1:8080", "Origin": "https://evil.example", "Content-Type": "text/plain"})
        assert r.status_code == 403


def test_allowed_hosts_setting(settings, fake_fetcher):
    with make_client(settings, fake_fetcher, api_enabled=True, api_allowed_hosts=["testserver"]) as c:
        assert c.get("/api/v1/status").status_code == 200


def test_web_login_protects_api_without_token(settings, fake_fetcher):
    with make_client(settings, fake_fetcher, api_enabled=True, web_password=Secret("letmein")) as c:
        assert c.get("/api/v1/status", headers={"Host": "127.0.0.1"}).status_code == 401  # loopback isn't enough
        assert c.get("/api/v1/status", headers=basic("admin", "letmein")).status_code == 200
        assert c.get("/api/v1/status", headers=basic("admin", "nope")).status_code == 401


def test_token_mode(api):
    assert api.get("/api/v1/status", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert api.get("/api/v1/status", headers={"Authorization": "", "X-API-Key": TOKEN}).status_code == 200
    assert api.get("/api/v1/status", headers={"Authorization": ""}).status_code == 401
    # The API skips the UI's HX-Request CSRF rule (it is token-checked) ...
    assert api.post("/api/v1/queue", json={"album_ids": []}).status_code == 422
    # ... and the UI still requires it.
    assert api.post("/queue/add", data={"album_id": "1"}).status_code == 403


def test_short_token_rejected():
    from soundtrack_manager.config import Settings
    s = Settings(data_dir=".", download_dir=".", api_token=Secret("short"))
    with pytest.raises(ValueError, match="at least 16"):
        s.validate()


def test_swagger_docs_and_schema(api):
    page = api.get("/api/v1/docs", headers={"Authorization": ""})
    assert page.status_code == 200 and "/static/vendor/swagger-ui-bundle.js" in page.text
    assert "<script>" not in page.text  # inline scripts would break the CSP
    assert "script-src 'self'" in page.headers["Content-Security-Policy"]
    assert api.get("/static/vendor/swagger-ui-bundle.js").status_code == 200
    assert api.get("/static/swagger-init.js").status_code == 200
    schema = api.get("/api/v1/openapi.json", headers={"Authorization": ""}).json()
    assert set(schema["components"]["securitySchemes"]) == {"HTTPBearer", "HTTPBasic"}
    ops = {op["operationId"] for path in schema["paths"].values() for op in path.values()}
    assert {"search_albums", "queue_albums", "create_import_list", "queue_import_list"} <= ops


# ------------------------------------------------------------ REST calls

def test_api_search_queue_jobs(api):
    r = api.get("/api/v1/search", params={"q": "resident evil"})
    assert r.status_code == 200 and r.json()["total"] == 10
    aid = r.json()["albums"][0]["id"]
    r = api.post("/api/v1/queue", json={"album_ids": [aid], "format": "mp3"})
    assert r.json()["added"] == [aid]
    jid = api.get("/api/v1/jobs").json()["jobs"][0]["id"]
    assert api.get(f"/api/v1/jobs/{jid}").json()["files"] == []
    assert api.post(f"/api/v1/jobs/{jid}/pause").json()["status"] == "paused"
    assert api.post(f"/api/v1/jobs/{jid}/retry").status_code == 409
    assert api.post(f"/api/v1/jobs/{jid}/explode").status_code == 422
    assert api.get("/api/v1/jobs/99999").status_code == 404
    assert api.get("/api/v1/albums", params={"url": SAVE_ROOM}).json()["formats"] == ["MP3"]
    assert api.get("/api/v1/search", params={"q": "x", "platform": "nope"}).status_code == 422


def _wait_ready(api, lid):
    for _ in range(100):
        if api.get(f"/api/v1/imports/{lid}").json()["status"] == "ready":
            return
        time.sleep(0.05)
    raise AssertionError("matching did not finish")


def test_api_import_flow(api):
    r = api.post("/api/v1/imports/validate", content=LIST, headers={"Content-Type": "text/plain"})
    assert r.status_code == 200 and r.json()["entries"] == 3
    assert api.post("/api/v1/imports/validate", json={"content": "a,b\n1,2", "filename": "x.csv"}).status_code == 422
    r = api.post("/api/v1/imports", json={"content": LIST, "name": "api", "format": "mp3"})
    assert r.status_code == 201
    lid = r.json()["id"]
    _wait_ready(api, lid)
    review = api.get(f"/api/v1/imports/{lid}/entries", params={"status": "review"}).json()["entries"]
    assert [e["title"] for e in review] == ["Castlevania (NES) Soundtrack", "Wild Arms 3 - Soundtrack (2002)"]
    assert api.post(f"/api/v1/imports/entries/{review[0]['id']}/choose", json={"url": SAVE_ROOM}).json()["status"] == "matched"
    assert api.post(f"/api/v1/imports/entries/{review[1]['id']}/skip").json()["entry"]["status"] == "skipped"
    assert api.post(f"/api/v1/imports/{lid}/queue", json={}).json()["queued"] == 1
    assert "api" in [x["name"] for x in api.get("/api/v1/imports").json()["lists"]]
    assert api.get(f"/api/v1/imports/{lid}/export.csv").text.startswith("position,")
    assert api.delete(f"/api/v1/imports/{lid}").json()["ok"]
    assert api.get(f"/api/v1/imports/{lid}").status_code == 404

    # Raw CSV body with options in the query string
    r = api.post("/api/v1/imports", params={"name": "csv", "start": "false"}, content="title\nChrono Cross\n",
                 headers={"Content-Type": "text/csv"})
    assert r.status_code == 201 and r.json()["source_format"] == "csv" and r.json()["status"] == "matching"


# ------------------------------------------------------------ MCP

class Mcp:
    def __init__(self, client):
        self.c = client
        self.sid = None
        self.n = 0

    def call(self, method, params=None, notify=False, **headers):
        h = {**MCP_HEADERS, **headers}
        if self.sid:
            h["mcp-session-id"] = self.sid
        body = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        if not notify:
            self.n += 1
            body["id"] = self.n
        r = self.c.post("/mcp", json=body, headers=h)
        self.sid = r.headers.get("mcp-session-id", self.sid)
        text = r.text
        if r.headers.get("content-type", "").startswith("text/event-stream"):
            data = [ln[5:].strip() for ln in text.splitlines() if ln.startswith("data:")]
            text = data[-1] if data else ""
        return r, (json.loads(text) if text.strip() else None)

    def start(self):
        r, init = self.call("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                           "clientInfo": {"name": "test", "version": "0"}})
        assert r.status_code == 200, r.text
        self.call("notifications/initialized", notify=True)
        return init["result"]

    def tool(self, tool_name, /, **arguments):
        r, res = self.call("tools/call", {"name": tool_name, "arguments": arguments})
        assert r.status_code == 200, r.text
        result = res["result"]
        if result.get("isError") or not result.get("content"):
            return result, None
        return result, json.loads(result["content"][0]["text"])


@pytest.fixture
def mcp(settings, fake_fetcher):
    with make_client(settings, fake_fetcher, mcp_enabled=True, api_token=Secret(TOKEN)) as c:
        c.headers["Authorization"] = f"Bearer {TOKEN}"
        m = Mcp(c)
        yield m


def test_mcp_lists_api_tools_with_instructions(mcp):
    info = mcp.start()
    assert info["serverInfo"]["name"] == "Soundtrack Manager" and "create_import_list" in info["instructions"]
    _, res = mcp.call("tools/list")
    names = {t["name"] for t in res["result"]["tools"]}
    assert {"search_albums", "get_album", "queue_albums", "list_jobs", "create_import_list", "get_import_list",
            "list_import_entries", "choose_import_album", "queue_import_list", "get_status"} <= names
    assert "export_import_results" not in names  # CSV download: not a tool


def test_mcp_works_while_rest_api_is_off(mcp):
    mcp.start()
    assert mcp.c.get("/api/v1/status").status_code == 404  # API_ENABLED is false here
    result, data = mcp.tool("search_albums", q="resident evil", per_page=3)
    assert not result.get("isError") and data["total"] == 10 and len(data["albums"]) == 3


def test_mcp_import_and_queue(mcp):
    mcp.start()
    _, created = mcp.tool("create_import_list", content=LIST, name="agent", format="mp3")
    lid = created["id"]
    for _ in range(100):
        _, info = mcp.tool("get_import_list", list_id=lid)
        if info["status"] == "ready":
            break
        time.sleep(0.05)
    _, entries = mcp.tool("list_import_entries", list_id=lid, status="review")
    eid = entries["entries"][0]["id"]
    _, chosen = mcp.tool("choose_import_album", entry_id=eid, url=SAVE_ROOM)
    assert chosen["status"] == "matched"
    _, queued = mcp.tool("queue_import_list", list_id=lid)
    assert queued["queued"] == 1
    _, jobs = mcp.tool("list_jobs")
    assert len(jobs["jobs"]) == 1


def test_mcp_errors_come_back_as_tool_errors(mcp):
    mcp.start()
    result, _ = mcp.tool("get_job", job_id=424242)
    assert result.get("isError") and "unknown job" in result["content"][0]["text"]


def test_mcp_access_rules(mcp):
    r = mcp.c.post("/mcp", json={}, headers={**MCP_HEADERS, "Authorization": "Bearer nope"})
    assert r.status_code == 401
    r = mcp.c.post("/mcp", json={}, headers={**MCP_HEADERS, "Origin": "https://evil.example"})
    assert r.status_code == 403


def test_mcp_open_mode_rejects_rebinding(settings, fake_fetcher):
    with make_client(settings, fake_fetcher, mcp_enabled=True) as c:
        m = Mcp(c)
        r, _ = m.call("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                     "clientInfo": {"name": "t", "version": "0"}}, Host="evil.example")
        assert r.status_code == 403
        r, init = m.call("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                        "clientInfo": {"name": "t", "version": "0"}}, Host="127.0.0.1:8080")
        assert r.status_code == 200 and init["result"]["serverInfo"]["name"] == "Soundtrack Manager"


def test_repo_mcp_json_works_without_a_token(settings, fake_fetcher):
    """.mcp.json sends 'Bearer ' (empty) when API_TOKEN isn't set; open localhost mode must accept it."""
    from pathlib import Path
    cfg = json.loads((Path(__file__).parent.parent / ".mcp.json").read_text(encoding="utf-8"))
    server = cfg["mcpServers"]["soundtrack-manager"]
    assert server["type"] == "http" and server["url"].endswith("/mcp")
    with make_client(settings, fake_fetcher, mcp_enabled=True) as c:
        m = Mcp(c)
        r, init = m.call("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                        "clientInfo": {"name": "t", "version": "0"}},
                         Host="127.0.0.1:8080", Authorization="Bearer ")
        assert r.status_code == 200 and init["result"]["serverInfo"]["name"] == "Soundtrack Manager"


def test_import_tools_require_content(api):
    schema = api.get("/api/v1/openapi.json").json()
    for path in ("/imports", "/imports/validate"):
        body = schema["paths"][path]["post"]["requestBody"]
        assert body["required"] and "content" in body["content"]["application/json"]["schema"]["required"]
