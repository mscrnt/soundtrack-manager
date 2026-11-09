"""Download worker tests: mocked site pages + mocked CDN (Range-capable). No network."""

import hashlib
import json
from urllib.parse import unquote, urlsplit

import httpx
import pytest

from soundtrack_manager import queue
from soundtrack_manager.discovery import SearchQuery
from soundtrack_manager.downloader import DownloadWorker
from soundtrack_manager.runtime import AppContext

from .conftest import DEFAULT_RULES, FakeFetcher, fixture_html

pytestmark = pytest.mark.anyio

SLUG = "resident-evil-save-room-compilation-2023"
CDN = f"https://vgmtreasurechest.com/soundtracks/{SLUG}/pmseqpfe/"
FIXTURE_NAME = "01%20R.E.%201%20Save%20Room-%20Safe%20Haven.mp3"


def track_page(flac: bool = False):
    """Build a track page for any track URL from the real fixture."""
    def render(url: str) -> str:
        enc = unquote(urlsplit(url).path.rsplit("/", 1)[-1])  # 01%20Name.mp3
        html = fixture_html("track_save_room_01.html").replace(FIXTURE_NAME, enc)
        if flac:
            flac_name = enc[:-4] + ".flac"
            html = html.replace(
                "(right click, Save Link As",
                f'<a href="{CDN}{flac_name}"><span class="songDownloadLink">Click here to download as FLAC</span></a> (20 MB)</p>'
                "<p>(right click, Save Link As", 1)
        return html
    return render


def content_for(path: str) -> bytes:
    seed = hashlib.sha256(path.encode()).digest()
    return seed * 900  # ~28 KB, deterministic per file


class FakeCDN:
    def __init__(self, fail_first: int = 0, truncate_once: set | None = None, status_for: dict | None = None):
        self.fail_first = fail_first
        self.truncate_once = set(truncate_once or ())
        self.status_for = status_for or {}
        self.requests: list[httpx.Request] = []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        path = unquote(req.url.path)
        if req.url.host != "vgmtreasurechest.com" and not req.url.host.endswith(".vgmtreasurechest.com"):
            return httpx.Response(404)
        for needle, code in self.status_for.items():
            if needle in path:
                return httpx.Response(code)
        body = content_for(path)
        if req.method == "HEAD":
            return httpx.Response(200, headers={"Content-Length": str(len(body)), "Accept-Ranges": "bytes"})
        if self.fail_first > 0:
            self.fail_first -= 1
            return httpx.Response(503)
        rng = req.headers.get("range")
        if rng:
            start = int(rng.split("=")[1].split("-")[0])
            if start >= len(body):
                return httpx.Response(416)
            chunk = body[start:]
            return httpx.Response(206, content=chunk, headers={
                "Content-Range": f"bytes {start}-{len(body) - 1}/{len(body)}", "Content-Length": str(len(chunk))})
        if path in self.truncate_once:
            self.truncate_once.discard(path)
            # headers promise the full file, the body stops halfway (dropped connection)
            return httpx.Response(200, content=body[: len(body) // 2], headers={"Content-Length": str(len(body))})
        return httpx.Response(200, content=body, headers={"Content-Length": str(len(body))})


def make_ctx(settings, flac=False):
    rules = [(".mp3", track_page(flac))] + DEFAULT_RULES
    settings.max_retries = 2
    settings.retry_backoff_seconds = 0.01
    return AppContext(settings, fetcher=FakeFetcher(rules), configure_logging=False)


async def queue_album(ctx, fmt="mp3") -> int:
    await ctx.discovery.discover(SearchQuery.build("resident evil"))
    a = next(r for r in (ctx.discovery.get_album(i) for i in range(1, 11)) if r["slug"] == SLUG)
    queue.enqueue(ctx.db, [a["id"]], fmt)
    return queue.list_jobs(ctx.db)[0]["id"]


def album_dir(settings):
    return settings.download_dir / "GC" / "Resident Evil_ Save Room Compilation"


async def run(ctx, cdn):
    w = DownloadWorker(ctx, transport=httpx.MockTransport(cdn))
    await w.run_until_idle()
    await w.http.aclose()
    return w


async def test_full_album_download(settings):
    ctx = make_ctx(settings)
    jid = await queue_album(ctx)
    cdn = FakeCDN()
    await run(ctx, cdn)
    job = queue.list_jobs(ctx.db)[0]
    assert job["status"] == "completed", job["error"]
    assert job["files_done"] == 7 and job["files_total"] == 7 and job["files_failed"] == 0
    d = album_dir(settings)
    first = d / "01 R.E. 1 Save Room- Safe Haven.mp3"  # original file name preserved
    assert first.read_bytes() == content_for("/soundtracks/" + SLUG + "/pmseqpfe/01 R.E. 1 Save Room- Safe Haven.mp3")
    assert (d / "cover.png").exists()
    assert not list(d.glob("*.part"))
    info = json.loads((d / "album_info.json").read_text(encoding="utf-8"))
    assert info["generator"] == "soundtrack-manager" and info["title"].startswith("Resident Evil")
    assert info["cover"] == "cover.png" and len(info["tracks"]) == 7
    t1 = info["tracks"][0]
    assert t1["file"] == first.name and t1["sha256"] == hashlib.sha256(first.read_bytes()).hexdigest()
    assert job["dest_dir"] == "GC/Resident Evil_ Save Room Compilation"
    files = queue.job_files(ctx.db, jid)
    assert all(f["status"] == "completed" and f["sha256"] for f in files)


async def test_retries_transient_errors_and_resumes_truncated(settings):
    ctx = make_ctx(settings)
    await queue_album(ctx)
    p = f"/soundtracks/{SLUG}/pmseqpfe/03 R.E. 3 Save Room- Free From Fear.mp3"
    cdn = FakeCDN(fail_first=2, truncate_once={p})
    await run(ctx, cdn)
    job = queue.list_jobs(ctx.db)[0]
    assert job["status"] == "completed", job["error"]
    ranged = [r for r in cdn.requests if r.headers.get("range") and "03 R.E. 3" in unquote(r.url.path)]
    assert ranged and ranged[0].headers["range"] == f"bytes={len(content_for(p)) // 2}-"
    assert (album_dir(settings) / "03 R.E. 3 Save Room- Free From Fear.mp3").read_bytes() == content_for(p)


async def test_resume_from_existing_part_file(settings):
    ctx = make_ctx(settings)
    await queue_album(ctx)
    d = album_dir(settings)
    d.mkdir(parents=True)
    p = f"/soundtracks/{SLUG}/pmseqpfe/01 R.E. 1 Save Room- Safe Haven.mp3"
    (d / "01 R.E. 1 Save Room- Safe Haven.mp3.part").write_bytes(content_for(p)[:10000])
    cdn = FakeCDN()
    await run(ctx, cdn)
    r = next(r for r in cdn.requests if r.method == "GET" and "01 R.E. 1" in unquote(r.url.path))
    assert r.headers["range"] == "bytes=10000-"
    assert (d / "01 R.E. 1 Save Room- Safe Haven.mp3").read_bytes() == content_for(p)


async def test_existing_files_skipped_or_kept_as_conflict(settings):
    ctx = make_ctx(settings)
    jid = await queue_album(ctx)
    d = album_dir(settings)
    d.mkdir(parents=True)
    same = f"/soundtracks/{SLUG}/pmseqpfe/01 R.E. 1 Save Room- Safe Haven.mp3"
    (d / "01 R.E. 1 Save Room- Safe Haven.mp3").write_bytes(content_for(same))
    mine = d / "02 R.E. 2 Save Room- Secure Place.mp3"
    mine.write_bytes(b"my own edited file")
    await run(ctx, FakeCDN())
    job = queue.list_jobs(ctx.db)[0]
    assert job["status"] == "completed" and job["files_conflict"] == 1
    assert "differ and were kept" in job["error"]
    assert mine.read_bytes() == b"my own edited file"  # never overwritten without confirmation
    st = {f["rel_path"]: f["status"] for f in queue.job_files(ctx.db, jid)}
    assert st["01 R.E. 1 Save Room- Safe Haven.mp3"] == "skipped"
    assert st["02 R.E. 2 Save Room- Secure Place.mp3"] == "conflict"

    # explicit confirmation -> replaced
    assert queue.allow_overwrite(ctx.db, jid)
    await run(ctx, FakeCDN())
    assert mine.read_bytes() == content_for(f"/soundtracks/{SLUG}/pmseqpfe/02 R.E. 2 Save Room- Secure Place.mp3")
    assert queue.list_jobs(ctx.db)[0]["files_conflict"] == 0


async def test_failed_file_does_not_stop_album(settings):
    ctx = make_ctx(settings)
    jid = await queue_album(ctx)
    await run(ctx, FakeCDN(status_for={"05 R.E. 0": 404}))
    job = queue.list_jobs(ctx.db)[0]
    assert job["status"] == "failed" and job["files_done"] == 6 and job["files_failed"] == 1
    assert "HTTP 404" in job["error"]
    # retry only re-downloads the failed file
    assert queue.retry(ctx.db, jid)
    cdn = FakeCDN()
    await run(ctx, cdn)
    assert queue.list_jobs(ctx.db)[0]["status"] == "completed"
    assert {unquote(r.url.path).rsplit("/", 1)[1] for r in cdn.requests if r.method == "GET"} == {
        "05 R.E. 0 Save Room.mp3"}


async def test_flac_preferred_and_fallback(settings):
    ctx = make_ctx(settings, flac=True)
    await queue_album(ctx, "flac")
    await run(ctx, FakeCDN())
    job = queue.list_jobs(ctx.db)[0]
    assert job["status"] == "completed"
    assert (album_dir(settings) / "01 R.E. 1 Save Room- Safe Haven.flac").exists()
    assert job["actual_format"] == "flac"
    assert job["note"] is None  # album page listed only MP3, but the track pages offered FLAC


async def test_flac_requested_but_only_mp3(settings):
    ctx = make_ctx(settings, flac=False)
    await queue_album(ctx, "flac")
    await run(ctx, FakeCDN())
    job = queue.list_jobs(ctx.db)[0]
    assert job["status"] == "completed" and job["actual_format"] == "mp3"
    assert "FLAC not offered" in job["note"]


async def test_pause_then_resume(settings):
    ctx = make_ctx(settings)
    jid = await queue_album(ctx)
    claimed = queue.claim_next(ctx.db)
    queue.pause(ctx.db, jid)  # user pauses right after the worker claimed it
    w = DownloadWorker(ctx, transport=httpx.MockTransport(FakeCDN()))
    await w._run_job(claimed)
    assert queue.list_jobs(ctx.db)[0]["status"] == "paused"
    assert queue.resume(ctx.db, jid)
    await w.run_until_idle()
    await w.http.aclose()
    assert queue.list_jobs(ctx.db)[0]["status"] == "completed"


async def test_access_blocked_fails_job_clearly(settings):
    ctx = AppContext(settings, fetcher=FakeFetcher(blocked=True), configure_logging=False)
    with ctx.db.connect() as c:
        c.execute("INSERT INTO albums(slug, url, title) VALUES (?,?,?)", (SLUG, "https://x", "X"))
    queue.enqueue(ctx.db, [1], "mp3")
    await run(ctx, FakeCDN())
    job = queue.list_jobs(ctx.db)[0]
    assert job["status"] == "failed" and job["error"].startswith("Site access check")


async def test_concurrency_limit(settings):
    ctx = make_ctx(settings)
    await ctx.discovery.discover(SearchQuery.build("resident evil"))
    queue.enqueue(ctx.db, list(range(1, 11)), "mp3")
    ctx.settings.max_concurrent_downloads = 2
    w = DownloadWorker(ctx, transport=httpx.MockTransport(FakeCDN()))
    w._fill()
    assert w.active_jobs == 2
    assert queue.counts(ctx.db)["running"] == 2
    await w.stop()  # graceful stop puts running jobs back in the queue
    assert queue.counts(ctx.db)["running"] == 0 and queue.counts(ctx.db)["queued"] == 10


async def test_foreign_album_info_not_overwritten(settings):
    ctx = make_ctx(settings)
    await queue_album(ctx)
    d = album_dir(settings)
    d.mkdir(parents=True)
    (d / "album_info.json").write_text('{"mine": true}')
    await run(ctx, FakeCDN())
    assert json.loads((d / "album_info.json").read_text()) == {"mine": True}
