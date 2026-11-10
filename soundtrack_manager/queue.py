"""Persistent download queue (SQLite ``jobs`` table): enqueue, list, state changes, crash recovery.

The download worker (downloader.py) claims jobs with :func:`claim_next`.
Higher ``priority`` runs first; ties run in the order they were queued.
"""

from __future__ import annotations

from dataclasses import dataclass

from .db import Database, utcnow

ACTIVE = ("queued", "running", "paused")


@dataclass
class EnqueueResult:
    added: list[int]
    already_queued: list[int]
    already_completed: list[int]
    missing: list[int]


def enqueue(db: Database, album_ids: list[int], fmt: str, *, allow_completed: bool = False,
            priority: int = 0) -> EnqueueResult:
    """Queue albums. Skips any already queued or completed in this format (no duplicates across searches)."""
    res = EnqueueResult([], [], [], [])
    with db.transaction() as c:
        for aid in dict.fromkeys(album_ids):
            if c.execute("SELECT 1 FROM albums WHERE id=?", (aid,)).fetchone() is None:
                res.missing.append(aid)
                continue
            if c.execute(f"SELECT 1 FROM jobs WHERE album_id=? AND format=? AND status IN ({','.join('?' * len(ACTIVE))})",
                         (aid, fmt, *ACTIVE)).fetchone():
                res.already_queued.append(aid)
                continue
            if not allow_completed and c.execute(
                    "SELECT 1 FROM jobs WHERE album_id=? AND format=? AND status='completed'", (aid, fmt)).fetchone():
                res.already_completed.append(aid)
                continue
            c.execute("INSERT INTO jobs(album_id, format, priority) VALUES (?,?,?)", (aid, fmt, priority))
            res.added.append(aid)
    return res


def list_jobs(db: Database, statuses: tuple[str, ...] | None = None, limit: int = 500) -> list[dict]:
    sql = ("SELECT j.*, a.title, a.slug, a.platforms_json, a.year, a.thumb_url FROM jobs j "
           "JOIN albums a ON a.id = j.album_id")
    args: list = []
    if statuses:
        sql += f" WHERE j.status IN ({','.join('?' * len(statuses))})"
        args.extend(statuses)
    sql += " ORDER BY CASE j.status WHEN 'running' THEN 0 WHEN 'queued' THEN 1 WHEN 'paused' THEN 2 ELSE 3 END, j.priority DESC, j.id LIMIT ?"
    args.append(limit)
    with db.connect() as c:
        return [dict(r) for r in c.execute(sql, args)]


def counts(db: Database) -> dict[str, int]:
    with db.connect() as c:
        out = {s: 0 for s in ("queued", "running", "paused", "completed", "failed", "cancelled")}
        for r in c.execute("SELECT status, COUNT(*) n FROM jobs GROUP BY status"):
            out[r["status"]] = r["n"]
        return out


def _transition(db: Database, job_id: int, allowed_from: tuple[str, ...], to: str) -> bool:
    with db.connect() as c:
        cur = c.execute(
            f"UPDATE jobs SET status=?, updated_at=? WHERE id=? AND status IN ({','.join('?' * len(allowed_from))})",
            (to, utcnow(), job_id, *allowed_from))
        return cur.rowcount == 1


def cancel(db: Database, job_id: int) -> bool:
    return _transition(db, job_id, ("queued", "paused", "running"), "cancelled")


def pause(db: Database, job_id: int) -> bool:
    return _transition(db, job_id, ("queued", "running"), "paused")


def resume(db: Database, job_id: int) -> bool:
    return _transition(db, job_id, ("paused",), "queued")


def retry(db: Database, job_id: int) -> bool:
    """Re-queue a failed/cancelled job. Completed and conflicting files are kept; failed files retried."""
    with db.connect() as c:
        cur = c.execute("UPDATE jobs SET status='queued', error=NULL, updated_at=? WHERE id=? AND status IN ('failed','cancelled') "
                        "AND NOT EXISTS (SELECT 1 FROM jobs j2 WHERE j2.album_id=jobs.album_id AND j2.format=jobs.format "
                        "AND j2.status IN ('queued','running','paused'))", (utcnow(), job_id))
        if cur.rowcount:
            c.execute("UPDATE job_files SET status='pending', attempts=0, error=NULL WHERE job_id=? AND status='failed'", (job_id,))
        return cur.rowcount == 1


def remove(db: Database, job_id: int) -> bool:
    """Delete a queue entry that is not running. Never touches files on disk."""
    with db.connect() as c:
        return c.execute("DELETE FROM jobs WHERE id=? AND status != 'running'", (job_id,)).rowcount == 1


def claim_next(db: Database) -> dict | None:
    """Atomically move the next queued job to 'running' and return it (safe across processes)."""
    with db.connect() as c:
        row = c.execute(
            "UPDATE jobs SET status='running', updated_at=?, started_at=COALESCE(started_at, ?), "
            "attempts=attempts+1 WHERE id = (SELECT id FROM jobs WHERE status='queued' "
            "ORDER BY priority DESC, id LIMIT 1) AND status='queued' RETURNING id", (utcnow(), utcnow())).fetchone()
        if row is None:
            return None
        return dict(c.execute("SELECT j.*, a.slug, a.title FROM jobs j JOIN albums a ON a.id=j.album_id "
                              "WHERE j.id=?", (row["id"],)).fetchone())


def job_status(db: Database, job_id: int) -> str | None:
    with db.connect() as c:
        r = c.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
        return r["status"] if r else None


def allow_overwrite(db: Database, job_id: int) -> bool:
    """User confirmed: replace conflicting existing files for this job, then re-run it."""
    with db.connect() as c:
        cur = c.execute("UPDATE jobs SET allow_overwrite=1, status='queued', error=NULL, updated_at=? "
                        "WHERE id=? AND status IN ('completed','failed') AND files_conflict > 0", (utcnow(), job_id))
        if cur.rowcount:
            c.execute("UPDATE job_files SET status='pending', error=NULL WHERE job_id=? AND status='conflict'", (job_id,))
        return cur.rowcount == 1


def job_files(db: Database, job_id: int) -> list[dict]:
    with db.connect() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM job_files WHERE job_id=? ORDER BY kind DESC, position", (job_id,))]


def recover_interrupted(db: Database) -> int:
    """At startup, jobs left 'running' by a crash or restart go back to 'queued'."""
    with db.connect() as c:
        n = c.execute("UPDATE jobs SET status='queued', updated_at=? WHERE status='running'", (utcnow(),)).rowcount
        c.execute("UPDATE job_files SET status='pending' WHERE status='downloading'")
        return n
