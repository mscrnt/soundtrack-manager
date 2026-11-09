"""AI planning (mocked LLM), multi-term search, and web routes for downloads/library/AI."""

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from soundtrack_manager import queue
from soundtrack_manager.ai import AiError, plan_from_request, validate_plan
from soundtrack_manager.discovery import SearchQuery
from soundtrack_manager.web.app import create_app

HX = {"HX-Request": "true"}


def llm(reply, status=200, reject_response_format=False):
    calls = []

    def handler(req: httpx.Request):
        body = json.loads(req.content)
        calls.append(body)
        if reject_response_format and "response_format" in body:
            return httpx.Response(400, json={"error": "unsupported"})
        if status != 200:
            return httpx.Response(status)
        return httpx.Response(200, json={"choices": [{"message": {"content": reply}}]})
    return httpx.MockTransport(handler), calls


def test_validate_plan_filters_to_site_vocabulary():
    p = validate_plan({"titles": ["Resident Evil", "Silent Hill", "resident evil", "a|b"], "platforms": ["PlayStation", "Atari 9000"],
                       "album_type": "Soundtracks", "year": "1996-2000", "sort": "bogus"})
    assert p.titles == ["Resident Evil", "Silent Hill"]
    assert p.platforms == ["PS1"] and p.album_type == "soundtrack" and p.year == "1996-2000" and p.sort == "relevance"
    assert any("Atari 9000" in w for w in p.warnings)
    assert ("q", "Resident Evil | Silent Hill") in p.query_params() and ("type", "1") in p.query_params()


def test_validate_plan_needs_something():
    with pytest.raises(AiError):
        validate_plan({"titles": [], "platforms": ["nope"]})


@pytest.mark.anyio
async def test_plan_from_request_with_fenced_json_and_fallback():
    reply = '```json\n{"titles": ["Parasite Eve"], "platforms": ["PS1"], "year": "1998"}\n```'
    transport, calls = llm(reply, reject_response_format=True)
    plan = await plan_from_request("ps1 rpg 1998", endpoint="http://llm:11434/v1", model="m", transport=transport)
    assert plan.titles == ["Parasite Eve"] and plan.platforms == ["PS1"] and plan.year == "1998"
    assert "response_format" in calls[0] and "response_format" not in calls[1]
    assert calls[0]["messages"][0]["role"] == "system" and "NO genre" in calls[0]["messages"][0]["content"]


@pytest.mark.anyio
async def test_plan_errors_are_clear():
    with pytest.raises(AiError, match="no LLM endpoint"):
        await plan_from_request("x", endpoint="", model=None)
    t, _ = llm("not json at all")
    with pytest.raises(AiError, match="valid JSON"):
        await plan_from_request("x", endpoint="http://llm/v1", model=None, transport=t)
    t, _ = llm("", status=500)
    with pytest.raises(AiError, match="HTTP 500"):
        await plan_from_request("x", endpoint="http://llm/v1", model=None, transport=t)


@pytest.mark.anyio
async def test_multi_term_search_unions_results(ctx, fake_fetcher):
    r = await ctx.discovery.discover(SearchQuery.build("resident evil | final fantasy"))
    assert len(fake_fetcher.requests) == 2
    assert r.total == 16  # 10 + 6 rows in the fixtures, no overlap
    assert r.notes[0].startswith("Combined 2 searches")
    with pytest.raises(Exception):
        SearchQuery.build(" | ")


# ---------------------------------------------------------------- web

@pytest.fixture
def client(ctx):
    with TestClient(create_app(ctx)) as c:
        yield c


def test_ai_route_redirects_to_filtered_discover(client, ctx, monkeypatch):
    ctx.settings.llm_endpoint = "http://llm/v1"
    assert "Ask AI" in client.get("/discover").text

    async def fake_plan(prompt, **kw):
        return validate_plan({"titles": ["Resident Evil"], "platforms": ["ps1"], "year": "1996-2000"})
    monkeypatch.setattr("soundtrack_manager.ai.plan_from_request", fake_plan)
    r = client.post("/discover/ai", data={"prompt": "ps1 horror"}, headers=HX)
    loc = r.headers["HX-Redirect"]
    assert loc.startswith("/discover?q=Resident+Evil&platform=PS1&year=1996-2000")
    page = client.get(loc)
    assert "AI turned" in page.text and "ps1 horror" in page.text


def test_job_page_and_overwrite_action(client, ctx):
    client.get("/discover/results", params={"q": "resident evil"})
    queue.enqueue(ctx.db, [1], "mp3")
    jid = queue.list_jobs(ctx.db)[0]["id"]
    assert client.get(f"/downloads/{jid}").status_code == 200
    assert "Tracks appear here" in client.get(f"/downloads/{jid}/files").text
    assert client.get("/downloads/jobs").status_code == 200  # not shadowed by /downloads/{id}
    with ctx.db.connect() as c:
        c.execute("UPDATE jobs SET status='completed', files_conflict=1 WHERE id=?", (jid,))
    r = client.post(f"/jobs/{jid}/overwrite", headers=HX)
    assert r.status_code == 200 and queue.list_jobs(ctx.db)[0]["status"] == "queued"


def test_library_routes(client, ctx, settings):
    d = settings.download_dir / "PS1" / "Some Album"
    d.mkdir(parents=True)
    (d / "01.mp3").write_bytes(b"123")
    page = client.get("/library")
    assert page.status_code == 200 and str(settings.download_dir.resolve()) in page.text
    r = client.post("/library/scan", data={"root": "0"}, headers=HX)
    assert r.status_code == 200
    import time
    for _ in range(50):
        if "Scanned 1 folder" in client.get("/library/scan-status").text:
            break
        time.sleep(0.05)
    assert "Scanned 1 folder" in client.get("/library/scan-status").text
    assert "Some Album" in client.get("/library/albums").text
    assert client.get("/library/duplicates").status_code == 200
    assert "Nothing missing" in client.get("/library/missing").text
    assert "Unknown library folder" in client.post("/library/scan", data={"root": "7"}, headers=HX).text
    r = client.post("/library/history-sync", headers=HX)  # fake fetcher has no account page rule
    assert r.status_code == 200
