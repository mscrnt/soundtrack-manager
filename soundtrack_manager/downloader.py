"""Download worker: runs queued jobs from SQLite, resumable and verified.

* Runs as an asyncio task inside the web process (or in the foreground for
  ``main.py download``). It claims jobs atomically (``queue.claim_next``) and
  runs up to ``max_concurrent_downloads`` jobs at once.
* Each track page is resolved when its turn comes (site requests go through
  the shared rate limiter). The resolved CDN URL is stored, so a resumed job
  doesn't fetch it again.
* Files are written to ``<name>.part``. After a crash or pause, the download
  continues with an HTTP ``Range`` request, then the size is checked against
  ``Content-Length`` / ``Content-Range``, the file is hashed (SHA-256) and
  atomically renamed.
* An existing file that matches the server's size is kept and recorded as
  ``skipped``. An existing file that differs is never overwritten unless the user
  confirms (``queue.allow_overwrite``), and is recorded as ``conflict``.
* Pause and cancel are checked about once a second. ``.part`` files stay on
  disk so a later run can resume them.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit

import httpx

from . import __version__, queue
from .db import utcnow
from .khinsider.fetch import RETRY_STATUSES, USER_AGENT, AccessBlocked, BackendUnavailable, FetchError, _backoff
from .khinsider.parse import LayoutChangedError, size_to_bytes
from .khinsider.urls import filename_from_url, is_cdn_url
from .paths import UnsafePathError, render_album_dir, safe_join, sanitize_component

log = logging.getLogger(__name__)

CHUNK = 256 * 1024
FLUSH_INTERVAL = 0.5     # seconds between progress writes to SQLite
CONTROL_INTERVAL = 1.0   # seconds between pause/cancel checks
INFO_FILE = "album_info.json"
GENERATOR = "soundtrack-manager"


class JobStopped(Exception):
    """The user paused or cancelled the job while it was running."""


class TransientError(Exception):
    """Worth retrying: timeouts, 5xx/429, truncated transfers."""


class FileFailed(Exception):
    """Giving up on this file. The job continues with the next one."""


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


@dataclass
class JobProgress:
    job_id: int
    current_file: str | None = None
    _samples: deque = field(default_factory=lambda: deque(maxlen=64))

    def add(self, n: int) -> None:
        self._samples.append((time.monotonic(), n))

    @property
    def speed_bps(self) -> float:
        now = time.monotonic()
        recent = [(t, n) for t, n in self._samples if now - t <= 5.0]
        if not recent:
            return 0.0
        span = max(now - recent[0][0], 0.5)
        return sum(n for _, n in recent) / span


def human_speed(bps: float) -> str:
    if bps <= 0:
        return ""
    for unit in ("B/s", "KB/s", "MB/s", "GB/s"):
        if bps < 1024 or unit == "GB/s":
            return f"{bps:.0f} {unit}" if unit == "B/s" else f"{bps:.1f} {unit}"
        bps /= 1024
    return ""


def _track_page_filename(page_url: str) -> str:
    # Track page paths double-encode the original name: 01%2520Title.mp3
    return unquote(unquote(urlsplit(page_url).path.rsplit("/", 1)[-1]))


class DownloadWorker:
    def __init__(self, ctx, *, transport: httpx.AsyncBaseTransport | None = None, poll_interval: float = 2.0):
        self.ctx = ctx
        self.db = ctx.db
        self.poll_interval = poll_interval
        self.http = httpx.AsyncClient(
            headers={"User-Agent": USER_AGENT},
            timeout=httpx.Timeout(30.0, read=120.0),
            follow_redirects=True,
            transport=transport,
        )
        self.progress: dict[int, JobProgress] = {}
        self._tasks: dict[int, asyncio.Task] = {}
        self._wake = asyncio.Event()
        self._loop_task: asyncio.Task | None = None

    # ------------------------------------------------------------------ control
    def start(self) -> None:
        if self._loop_task is None:
            self._loop_task = asyncio.create_task(self._run_forever(), name="download-worker")
            log.info("download worker started")

    def wake(self) -> None:
        self._wake.set()

    async def stop(self) -> None:
        if self._loop_task is not None:
            self._loop_task.cancel()
            await asyncio.gather(self._loop_task, return_exceptions=True)
            self._loop_task = None
        for t in self._tasks.values():
            t.cancel()
        await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        self._tasks.clear()
        n = queue.recover_interrupted(self.db)  # interrupted jobs go back to the queue
        if n:
            log.info("stopped %d running job(s); they will resume on next start", n)
        await self.http.aclose()

    def snapshot(self) -> dict[int, dict]:
        return {jid: {"current_file": p.current_file, "speed_bps": p.speed_bps, "speed_text": human_speed(p.speed_bps)}
                for jid, p in self.progress.items()}

    @property
    def active_jobs(self) -> int:
        return sum(1 for t in self._tasks.values() if not t.done())

    async def _run_forever(self) -> None:
        while True:
            try:
                self._fill()
            except Exception:  # never let the loop die
                log.exception("worker loop error")
            try:
                await asyncio.wait_for(self._wake.wait(), self.poll_interval)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()

    def _fill(self) -> None:
        for jid, t in list(self._tasks.items()):
            if t.done():
                self._tasks.pop(jid)
                self.progress.pop(jid, None)
        limit = self.ctx.settings.max_concurrent_downloads
        while len(self._tasks) < limit:
            job = queue.claim_next(self.db)
            if job is None:
                break
            task = asyncio.create_task(self._run_job(job), name=f"job-{job['id']}")
            task.add_done_callback(lambda _t: self._wake.set())
            self._tasks[job["id"]] = task

    async def run_until_idle(self, on_tick=None) -> None:
        """Process the queue in the foreground until nothing is queued or running (CLI)."""
        while True:
            self._fill()
            if not self._tasks:
                return
            await asyncio.wait(list(self._tasks.values()), timeout=0.5, return_when=asyncio.FIRST_COMPLETED)
            if on_tick:
                on_tick()

    # ------------------------------------------------------------------ jobs
    async def _run_job(self, job: dict) -> None:
        jid = job["id"]
        self.progress[jid] = JobProgress(jid)
        log.info("job %d started: %s (%s)", jid, job["title"], job["format"])
        try:
            await self._process(job)
        except JobStopped as exc:
            log.info("job %d %s by user", jid, exc)
            with self.db.connect() as c:
                c.execute("UPDATE job_files SET status='pending' WHERE job_id=? AND status='downloading'", (jid,))
        except asyncio.CancelledError:
            raise
        except AccessBlocked as exc:
            self._finish(jid, "failed", f"Site access check: {exc}")
        except LayoutChangedError as exc:
            self._finish(jid, "failed", f"Site layout not recognised: {exc}")
        except (FetchError, UnsafePathError, OSError) as exc:
            self._finish(jid, "failed", str(exc))
        except Exception as exc:  # pragma: no cover - safety net
            log.exception("job %d crashed", jid)
            self._finish(jid, "failed", f"Unexpected error: {type(exc).__name__}: {exc}")

    async def _process(self, job: dict) -> None:
        jid = job["id"]
        planned = bool(queue.job_files(self.db, jid))
        if planned:
            album = self.ctx.discovery.get_album(job["album_id"])
        else:
            album = await self.ctx.discovery.album_detail(job["slug"], refresh=True)
        if not album or not album["tracks"]:
            raise FetchError("the album page lists no tracks")

        wanted = job["format"].upper()
        rel_dir = PurePosixPath(job["dest_dir"]) if job["dest_dir"] else render_album_dir(
            self.ctx.settings.folder_template, album)
        dest = safe_join(self.ctx.settings.download_dir, rel_dir)
        dest.mkdir(parents=True, exist_ok=True)

        if not planned:
            self._plan(jid, album, wanted)
        with self.db.connect() as c:
            c.execute("UPDATE jobs SET dest_dir=?, files_total=?, updated_at=? WHERE id=?",
                      (str(rel_dir), len(album["tracks"]), utcnow(), jid))
        self._update_job_totals(jid)

        job = {**job, **self._job_row(jid)}
        for jf in queue.job_files(self.db, jid):
            if jf["status"] in ("completed", "skipped"):
                continue
            if jf["status"] == "conflict" and not job["allow_overwrite"]:
                continue
            self._check_control(jid)
            try:
                await self._do_file(job, jf, dest, wanted)
            except FileFailed as exc:
                log.warning("job %d file %s failed: %s", jid, jf["rel_path"] or jf["title"], exc)
                self._set_file(jf["id"], status="failed", error=str(exc))
            self._update_job_totals(jid)

        self.progress[jid].current_file = None
        self._write_album_info(jid, album, dest)
        self._finalize(jid)

    def _plan(self, jid: int, album: dict, wanted: str) -> None:
        with self.db.transaction() as c:
            for i, t in enumerate(album["tracks"]):
                est = size_to_bytes(t["sizes"].get(wanted) or t["sizes"].get("MP3") or next(iter(t["sizes"].values()), None))
                c.execute("INSERT OR IGNORE INTO job_files(job_id, kind, position, track_id, title, page_url, bytes_total) "
                          "VALUES (?,?,?,?,?,?,?)", (jid, "audio", i, t["id"], t["title"], t["page_url"], est))
            covers = [u for u in (album.get("cover_urls") or []) if is_cdn_url(u)]
            if covers:
                ext = os.path.splitext(urlsplit(covers[0]).path)[1].lower()
                ext = ext if ext in (".jpg", ".jpeg", ".png", ".webp", ".gif") else ".jpg"
                c.execute("INSERT OR IGNORE INTO job_files(job_id, kind, position, title, source_url, rel_path, format) "
                          "VALUES (?,?,?,?,?,?,?)", (jid, "cover", 0, "Cover art", covers[0], "cover" + ext, "IMAGE"))

    async def _resolve(self, jid: int, jf: dict, wanted: str) -> dict:
        """Find the CDN URL for a track (one track page request, cached in job_files)."""
        if jf["source_url"]:
            return jf
        files = await self.ctx.client.track_files(jf["page_url"])
        if not files:
            raise FileFailed("the track page offers no download link")
        by_fmt = {f.format: f for f in files}
        chosen = by_fmt.get(wanted) or by_fmt.get("MP3") or files[0]
        if not is_cdn_url(chosen.url):
            raise FileFailed(f"download link points to an unexpected host: {urlsplit(chosen.url).netloc}")
        name = sanitize_component(filename_from_url(chosen.url) or _track_page_filename(jf["page_url"]), "track")
        with self.db.connect() as c:
            taken = {r[0] for r in c.execute("SELECT rel_path FROM job_files WHERE job_id=? AND id!=? AND rel_path IS NOT NULL",
                                             (jid, jf["id"]))}
        stem, ext = os.path.splitext(name)
        n = 2
        while name in taken:
            name = f"{stem} ({n}){ext}"
            n += 1
        est = size_to_bytes(chosen.size_text) or jf["bytes_total"]
        self._set_file(jf["id"], source_url=chosen.url, rel_path=name, format=chosen.format, bytes_total=est)
        return {**jf, "source_url": chosen.url, "rel_path": name, "format": chosen.format, "bytes_total": est}

    async def _do_file(self, job: dict, jf: dict, dest: Path, wanted: str) -> None:
        jid = job["id"]
        if jf["kind"] == "audio":
            try:
                jf = await self._resolve(jid, jf, wanted)
            except BackendUnavailable:
                raise  # FlareSolverr/browser down: stop the job, don't fail every file
            except (FetchError, LayoutChangedError) as exc:
                raise FileFailed(f"track page: {exc}") from exc
        target = safe_join(dest, jf["rel_path"])
        part = target.with_name(target.name + ".part")
        self.progress[jid].current_file = jf["rel_path"]
        retries = self.ctx.settings.max_retries
        for attempt in range(retries + 1):
            self._set_file(jf["id"], status="downloading", attempts=jf["attempts"] + attempt + 1)
            try:
                await self._download(job, jf, target, part)
                return
            except TransientError as exc:
                if attempt >= retries:
                    raise FileFailed(f"{exc} (gave up after {attempt + 1} attempts)") from exc
                wait = _backoff(attempt, self.ctx.settings.retry_backoff_seconds, None)
                log.info("job %d %s: %s; retry in %.1fs", jid, jf["rel_path"], exc, wait)
                self._set_file(jf["id"], error=f"{exc}; retrying")
                await asyncio.sleep(wait)
                self._check_control(jid)

    async def _remote_size(self, url: str) -> int | None:
        try:
            r = await self.http.head(url)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise TransientError(f"HEAD failed: {type(exc).__name__}") from exc
        if r.status_code in RETRY_STATUSES:
            raise TransientError(f"HTTP {r.status_code}")
        cl = r.headers.get("content-length")
        return int(cl) if r.status_code == 200 and cl and cl.isdigit() else None

    async def _download(self, job: dict, jf: dict, target: Path, part: Path) -> None:
        jid, url = job["id"], jf["source_url"]
        if target.exists():
            local = target.stat().st_size
            remote = await self._remote_size(url)
            if remote is not None and local == remote:
                digest = await asyncio.to_thread(sha256_file, target)
                self._set_file(jf["id"], status="skipped", bytes_done=local, bytes_total=local, sha256=digest,
                               error="already on disk (size matches the server)")
                return
            if not job["allow_overwrite"]:
                self._set_file(jf["id"], status="conflict",
                               error=f"a different file already exists ({local} bytes; server has "
                                     f"{remote if remote is not None else 'unknown'}). Kept. Confirm "
                                     "'Overwrite conflicts' to replace it.")
                return

        offset = part.stat().st_size if part.exists() else 0
        headers = {"Range": f"bytes={offset}-"} if offset else {}
        total: int | None = None
        prog = self.progress[jid]
        try:
            async with self.http.stream("GET", url, headers=headers) as r:
                if r.status_code == 416 and offset:
                    remote = await self._remote_size(url)
                    if remote == offset:
                        total = offset
                    else:
                        part.unlink(missing_ok=True)  # our own temp file
                        raise TransientError("partial file did not match the server; restarting")
                elif r.status_code in RETRY_STATUSES:
                    raise TransientError(f"HTTP {r.status_code} from CDN")
                elif r.status_code >= 400:
                    raise FileFailed(f"HTTP {r.status_code} from CDN")
                else:
                    if r.status_code == 206:
                        m = re.match(r"bytes (\d+)-\d+/(\d+|\*)", r.headers.get("content-range", ""))
                        if not m or int(m.group(1)) != offset:
                            part.unlink(missing_ok=True)
                            raise TransientError("server returned an unexpected byte range; restarting")
                        total = int(m.group(2)) if m.group(2) != "*" else None
                    else:
                        if offset:
                            log.info("server ignored Range for %s; restarting file", jf["rel_path"])
                        offset = 0
                        cl = r.headers.get("content-length")
                        total = int(cl) if cl and cl.isdigit() else None
                    if total:
                        self._set_file(jf["id"], bytes_total=total)
                    done = offset
                    last_flush = last_ctl = time.monotonic()
                    with part.open("ab" if offset else "wb") as f:
                        async for chunk in r.aiter_bytes(CHUNK):
                            f.write(chunk)
                            done += len(chunk)
                            prog.add(len(chunk))
                            now = time.monotonic()
                            if now - last_flush >= FLUSH_INTERVAL:
                                self._set_file(jf["id"], bytes_done=done)
                                self._update_job_totals(jid)
                                last_flush = now
                            if now - last_ctl >= CONTROL_INTERVAL:
                                last_ctl = now
                                f.flush()
                                self._check_control(jid)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise TransientError(f"connection problem: {type(exc).__name__}") from exc

        size = part.stat().st_size
        if total is not None and size != total:
            self._set_file(jf["id"], bytes_done=size)
            raise TransientError(f"incomplete transfer ({size} of {total} bytes)")
        digest = await asyncio.to_thread(sha256_file, part)
        os.replace(part, target)
        self._set_file(jf["id"], status="completed", bytes_done=size, bytes_total=size, sha256=digest, error=None)

    # ------------------------------------------------------------------ metadata
    def _write_album_info(self, jid: int, album: dict, dest: Path) -> None:
        path = dest / INFO_FILE
        if path.exists():
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                existing = {}
            if existing.get("generator") != GENERATOR:
                log.warning("job %d: %s exists and was not written by this app; left unchanged", jid, path)
                return
        files = queue.job_files(self.db, jid)
        cover = next((f["rel_path"] for f in files if f["kind"] == "cover" and f["status"] in ("completed", "skipped")), None)
        by_track = {f["track_id"]: f for f in files if f["kind"] == "audio"}
        info = {
            "generator": GENERATOR, "generator_version": __version__, "updated_at": utcnow(),
            "source_url": album["url"], "slug": album["slug"], "title": album["title"],
            "platforms": album["platforms"], "year": album["year"], "album_type": album["album_type"],
            "catalog_number": album.get("catalog_number"), "publisher": album.get("publisher"),
            "developer": album.get("developer"), "date_added": album.get("date_added"),
            "description": album.get("description"), "formats_offered": album.get("formats"),
            "cover": cover,
            "tracks": [
                {"position": i + 1, "disc": t["disc"], "number": t["number"], "title": t["title"],
                 "duration": t["duration"], "file": (by_track.get(t["id"]) or {}).get("rel_path"),
                 "format": (by_track.get(t["id"]) or {}).get("format"),
                 "size": (by_track.get(t["id"]) or {}).get("bytes_total")
                 if (by_track.get(t["id"]) or {}).get("status") in ("completed", "skipped") else None,
                 "sha256": (by_track.get(t["id"]) or {}).get("sha256"),
                 "status": (by_track.get(t["id"]) or {}).get("status"),
                 "source_url": (by_track.get(t["id"]) or {}).get("source_url")}
                for i, t in enumerate(album["tracks"])
            ],
        }
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(info, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)

    # ------------------------------------------------------------------ DB helpers
    def _check_control(self, jid: int) -> None:
        status = queue.job_status(self.db, jid)
        if status != "running":
            raise JobStopped(status or "removed")

    def _job_row(self, jid: int) -> dict:
        with self.db.connect() as c:
            return dict(c.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone())

    _FILE_COLS = {"status", "error", "bytes_done", "bytes_total", "sha256", "attempts", "source_url", "rel_path", "format"}

    def _set_file(self, fid: int, **cols) -> None:
        assert set(cols) <= self._FILE_COLS
        sets = ", ".join(f"{k}=?" for k in cols)
        with self.db.connect() as c:
            c.execute(f"UPDATE job_files SET {sets}, updated_at=? WHERE id=?", (*cols.values(), utcnow(), fid))

    def _update_job_totals(self, jid: int) -> None:
        with self.db.connect() as c:
            c.execute(
                "UPDATE jobs SET "
                "bytes_done=(SELECT COALESCE(SUM(bytes_done),0) FROM job_files WHERE job_id=:j AND kind='audio'), "
                "bytes_total=(SELECT SUM(COALESCE(bytes_total,0)) FROM job_files WHERE job_id=:j AND kind='audio'), "
                "files_done=(SELECT COUNT(*) FROM job_files WHERE job_id=:j AND kind='audio' AND status IN ('completed','skipped')), "
                "files_failed=(SELECT COUNT(*) FROM job_files WHERE job_id=:j AND kind='audio' AND status='failed'), "
                "files_conflict=(SELECT COUNT(*) FROM job_files WHERE job_id=:j AND status='conflict'), "
                "actual_format=(SELECT GROUP_CONCAT(DISTINCT LOWER(format)) FROM job_files WHERE job_id=:j AND kind='audio' AND format IS NOT NULL), "
                "updated_at=:t WHERE id=:j", {"j": jid, "t": utcnow()})

    def _finalize(self, jid: int) -> None:
        self._update_job_totals(jid)
        with self.db.connect() as c:
            files = c.execute("SELECT kind, status, error, rel_path, title FROM job_files WHERE job_id=?", (jid,)).fetchall()
            job = c.execute("SELECT format, actual_format FROM jobs WHERE id=?", (jid,)).fetchone()
            got = set((job["actual_format"] or "").split(",")) - {""}
            if got and job["format"] not in got:
                c.execute("UPDATE jobs SET note=? WHERE id=?",
                          (f"{job['format'].upper()} not offered for this album; downloaded "
                           f"{', '.join(sorted(g.upper() for g in got))}.", jid))
        failed = [f for f in files if f["status"] == "failed" and f["kind"] == "audio"]
        conflicts = [f for f in files if f["status"] == "conflict"]
        notes = []
        if conflicts:
            notes.append(f"{len(conflicts)} existing file(s) differ and were kept.")
        if any(f["status"] == "failed" and f["kind"] == "cover" for f in files):
            notes.append("Cover art could not be downloaded.")
        if failed:
            first = failed[0]
            notes.insert(0, f"{len(failed)} file(s) failed. First: {first['rel_path'] or first['title']}: {first['error']}")
            self._finish(jid, "failed", " ".join(notes))
        else:
            self._finish(jid, "completed", " ".join(notes) or None)

    def _finish(self, jid: int, status: str, error: str | None) -> None:
        with self.db.connect() as c:
            c.execute("UPDATE jobs SET status=?, error=?, finished_at=?, updated_at=?, allow_overwrite=0 "
                      "WHERE id=? AND status='running'", (status, error, utcnow(), utcnow(), jid))
        log.info("job %d %s%s", jid, status, f": {error}" if error else "")
