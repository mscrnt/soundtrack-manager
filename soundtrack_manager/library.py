"""Local library: scan, verify, find duplicates and missing albums, import account history.

This module only reads the filesystem. It never deletes, moves or rewrites files.
Removing a library record removes the database row only.

Album folders are matched to site albums in two ways:

1. ``album_info.json`` written by this app (slug, expected files, sizes, SHA-256).
2. Otherwise by folder name, compared to known album titles after normalising
   (case, punctuation, a trailing "(1998)"), with a strict fuzzy cutoff.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import queue
from .db import Database, utcnow
from .discovery import upsert_album_summary
from .khinsider.client import ACCOUNT_PAGES
from .khinsider.models import AlbumSummary
from .khinsider.urls import album_url

log = logging.getLogger(__name__)

AUDIO_EXT = {".mp3", ".flac", ".ogg", ".m4a", ".aac", ".wav", ".opus", ".wma", ".ape", ".wv"}
INFO_FILE = "album_info.json"
_DISC_DIR = re.compile(r"^(disc|disk|cd)\s*[-_ ]?\d+\b", re.I)
_YEAR_SUFFIX = re.compile(r"\s*[\(\[]\s*(19|20)\d{2}\s*[\)\]]\s*$")
FUZZY_CUTOFF = 0.92


def norm_title(s: str) -> str:
    s = _YEAR_SUFFIX.sub("", s or "")
    return re.sub(r"[^0-9a-z]+", "", s.casefold())


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


@dataclass
class ScanSummary:
    root: str
    folders: int = 0
    matched: int = 0
    unmatched: int = 0
    incomplete: int = 0
    files: int = 0
    bytes: int = 0
    removed_records: int = 0
    errors: list[str] = field(default_factory=list)


def _album_folders(root: Path):
    """Yield (album_dir, [files]) for every folder holding audio or album_info.json.

    "Disc 1" / "CD2" subfolders are merged into their parent album folder.
    """
    groups: dict[Path, list[Path]] = {}
    for dirpath, dirnames, filenames in os.walk(root, onerror=lambda e: log.warning("scan: %s", e)):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        d = Path(dirpath)
        files = [d / f for f in filenames if Path(f).suffix.lower() in AUDIO_EXT or f == INFO_FILE]
        if not files:
            continue
        album_dir = d.parent if _DISC_DIR.match(d.name) and d != root else d
        groups.setdefault(album_dir, []).extend(files)
    yield from groups.items()


class Library:
    def __init__(self, db: Database):
        self.db = db

    # ------------------------------------------------------------------ scan
    def scan(self, root: Path, *, deep: bool = False, progress=None) -> ScanSummary:
        root = root.resolve()
        if not root.is_dir():
            raise ValueError(f"not a directory: {root}")
        summary = ScanSummary(root=str(root))
        titles = self._title_index()
        seen: set[str] = set()
        now = utcnow()
        for album_dir, files in _album_folders(root):
            rel = album_dir.relative_to(root).as_posix() or "."
            seen.add(rel)
            if progress:
                progress(rel)
            try:
                rec = self._scan_folder(root, album_dir, files, deep, titles)
            except OSError as exc:
                summary.errors.append(f"{rel}: {exc}")
                continue
            self._store(str(root), rel, rec, now, deep)
            summary.folders += 1
            summary.files += rec["file_count"]
            summary.bytes += rec["total_bytes"]
            summary.matched += rec["album_id"] is not None
            summary.unmatched += rec["album_id"] is None
            summary.incomplete += rec["status"] == "incomplete"
        with self.db.connect() as c:
            stale = [r["id"] for r in c.execute("SELECT id, rel_path FROM library_albums WHERE root=?", (str(root),))
                     if r["rel_path"] not in seen]
            for lid in stale:
                c.execute("DELETE FROM library_albums WHERE id=?", (lid,))  # DB record only
        summary.removed_records = len(stale)
        self.db.set_sync_state(f"library_scan:{root}", now)
        log.info("library scan of %s: %d folders, %d matched, %d incomplete", root, summary.folders,
                 summary.matched, summary.incomplete)
        return summary

    def _title_index(self) -> dict[str, int]:
        with self.db.connect() as c:
            return {norm_title(r["title"]): r["id"] for r in c.execute("SELECT id, title FROM albums")}

    def _scan_folder(self, root: Path, album_dir: Path, files: list[Path], deep: bool, titles: dict) -> dict:
        audio = {f.relative_to(album_dir).as_posix(): f for f in files if f.name != INFO_FILE}
        info_path = album_dir / INFO_FILE
        info = None
        if info_path.is_file():
            try:
                info = json.loads(info_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                info = None
        rows: list[dict] = []
        album_id = None
        method = None
        if info and info.get("slug") and isinstance(info.get("tracks"), list):
            album_id = self._album_from_info(info)
            method = "album_info"
            # Every track of the album is expected. Tracks that never finished (failed/pending) count as
            # missing unless a file is present. 'conflict' files belong to the user: present, but not verifiable.
            expected: dict[str, dict] = {}
            for t in info["tracks"]:
                name = t.get("file") or f"(not downloaded) {t.get('number') or t.get('position')}. {t.get('title') or ''}".strip()
                expected[name] = t
            for name, t in expected.items():
                p = album_dir / name
                if not t.get("file") or not p.is_file():
                    rows.append({"rel_path": name, "size": None, "expected_size": t.get("size"), "sha256": None,
                                 "expected_sha256": t.get("sha256"), "status": "missing"})
                    continue
                size = p.stat().st_size
                status = "ok"
                digest = None
                if t.get("status") not in ("completed", "skipped"):
                    rows.append({"rel_path": name, "size": size, "expected_size": None,
                                 "sha256": sha256_file(p) if deep else None, "expected_sha256": None,
                                 "status": "unverified"})
                    continue
                if t.get("size") and size != t["size"]:
                    status = "size_mismatch"
                elif deep:
                    digest = sha256_file(p)
                    if t.get("sha256") and digest != t["sha256"]:
                        status = "hash_mismatch"
                rows.append({"rel_path": name, "size": size, "expected_size": t.get("size"), "sha256": digest,
                             "expected_sha256": t.get("sha256"), "status": status})
            for name, p in audio.items():  # extra files not listed in album_info.json
                if name not in expected:
                    rows.append(self._plain_row(name, p, deep))
        else:
            for name, p in audio.items():
                rows.append(self._plain_row(name, p, deep))
            guess = norm_title(album_dir.name)
            album_id = titles.get(guess)
            if album_id is None and guess:
                close = difflib.get_close_matches(guess, list(titles), n=1, cutoff=FUZZY_CUTOFF)
                album_id = titles[close[0]] if close else None
            method = "title" if album_id is not None else None
        missing = sum(r["status"] == "missing" for r in rows)
        mismatch = sum(r["status"] in ("size_mismatch", "hash_mismatch") for r in rows)
        if missing or mismatch:
            status = "incomplete"
        elif album_id is None:
            status = "unmatched"
        elif method == "album_info":
            status = "ok"
        else:
            status = "unverified"
        present = [r for r in rows if r["size"] is not None]
        return {"album_id": album_id, "method": method, "title_guess": (info or {}).get("title") or album_dir.name,
                "rows": rows, "file_count": len(present), "total_bytes": sum(r["size"] for r in present),
                "missing": missing, "mismatch": mismatch, "status": status}

    @staticmethod
    def _plain_row(name: str, p: Path, deep: bool) -> dict:
        return {"rel_path": name, "size": p.stat().st_size, "expected_size": None,
                "sha256": sha256_file(p) if deep else None, "expected_sha256": None, "status": "unverified"}

    def _album_from_info(self, info: dict) -> int:
        with self.db.transaction() as c:
            return upsert_album_summary(c, AlbumSummary(
                slug=info["slug"], url=info.get("source_url") or album_url(info["slug"]), title=info.get("title") or info["slug"],
                platforms=info.get("platforms") or [], album_type=info.get("album_type"), year=info.get("year"),
                catalog_number=info.get("catalog_number")), utcnow())

    def _store(self, root: str, rel: str, rec: dict, now: str, deep: bool) -> None:
        with self.db.transaction() as c:
            c.execute(
                "INSERT INTO library_albums(root, rel_path, album_id, title_guess, match_method, file_count, total_bytes, "
                "missing_count, mismatch_count, status, scanned_at, verified_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(root, rel_path) DO UPDATE SET album_id=excluded.album_id, title_guess=excluded.title_guess, "
                "match_method=excluded.match_method, file_count=excluded.file_count, total_bytes=excluded.total_bytes, "
                "missing_count=excluded.missing_count, mismatch_count=excluded.mismatch_count, status=excluded.status, "
                "scanned_at=excluded.scanned_at, verified_at=COALESCE(excluded.verified_at, library_albums.verified_at)",
                (root, rel, rec["album_id"], rec["title_guess"], rec["method"], rec["file_count"], rec["total_bytes"],
                 rec["missing"], rec["mismatch"], rec["status"], now, now if deep else None))
            lid = c.execute("SELECT id FROM library_albums WHERE root=? AND rel_path=?", (root, rel)).fetchone()[0]
            if not deep:  # keep hashes from an earlier deep verify when the size is unchanged
                old = {r["rel_path"]: r for r in c.execute(
                    "SELECT rel_path, size, sha256 FROM library_files WHERE library_album_id=?", (lid,))}
                for r in rec["rows"]:
                    o = old.get(r["rel_path"])
                    if r["sha256"] is None and o and o["size"] == r["size"]:
                        r["sha256"] = o["sha256"]
            c.execute("DELETE FROM library_files WHERE library_album_id=?", (lid,))
            for r in rec["rows"]:
                c.execute("INSERT INTO library_files(library_album_id, rel_path, size, expected_size, sha256, "
                          "expected_sha256, status) VALUES (?,?,?,?,?,?,?)",
                          (lid, r["rel_path"], r["size"], r["expected_size"], r["sha256"], r["expected_sha256"],
                           r["status"]))

    # ------------------------------------------------------------------ queries
    def albums(self, q: str = "", status: str | None = None, limit: int = 500) -> list[dict]:
        sql = ("SELECT l.*, a.title AS album_title, a.slug, a.year, a.platforms_json FROM library_albums l "
               "LEFT JOIN albums a ON a.id = l.album_id WHERE 1=1")
        args: list = []
        if status:
            sql += " AND l.status=?"
            args.append(status)
        if q:
            sql += " AND (l.title_guess LIKE ? OR a.title LIKE ? OR l.rel_path LIKE ?)"
            args += [f"%{q}%"] * 3
        sql += " ORDER BY COALESCE(a.title, l.title_guess) COLLATE NOCASE LIMIT ?"
        args.append(limit)
        with self.db.connect() as c:
            out = []
            for r in c.execute(sql, args):
                d = dict(r)
                d["platforms"] = json.loads(d.pop("platforms_json") or "[]")
                out.append(d)
            return out

    def stats(self) -> dict:
        with self.db.connect() as c:
            row = c.execute("SELECT COUNT(*) n, COALESCE(SUM(total_bytes),0) b, COALESCE(SUM(file_count),0) f FROM library_albums").fetchone()
            by = {r["status"]: r["n"] for r in c.execute("SELECT status, COUNT(*) n FROM library_albums GROUP BY status")}
            acc = c.execute("SELECT COUNT(*) FROM account_albums").fetchone()[0]
        return {"folders": row["n"], "bytes": row["b"], "files": row["f"], "by_status": by, "account_albums": acc}

    def files(self, library_album_id: int) -> list[dict]:
        with self.db.connect() as c:
            return [dict(r) for r in c.execute(
                "SELECT * FROM library_files WHERE library_album_id=? ORDER BY rel_path", (library_album_id,))]

    def duplicates(self) -> list[dict]:
        groups: list[dict] = []
        with self.db.connect() as c:
            for r in c.execute(
                    "SELECT a.id, a.title, GROUP_CONCAT(l.root || '/' || l.rel_path, '\n') paths, COUNT(*) n "
                    "FROM library_albums l JOIN albums a ON a.id=l.album_id GROUP BY a.id HAVING n > 1"):
                groups.append({"kind": "same album", "label": r["title"], "paths": r["paths"].split("\n")})
            rows = c.execute("SELECT root, rel_path, title_guess FROM library_albums WHERE album_id IS NULL").fetchall()
            by_title: dict[str, list[str]] = {}
            for r in rows:
                by_title.setdefault(norm_title(r["title_guess"]), []).append(f"{r['root']}/{r['rel_path']}")
            for t, paths in by_title.items():
                if t and len(paths) > 1:
                    groups.append({"kind": "same folder name", "label": t, "paths": paths})
            for r in c.execute(
                    "SELECT f.sha256, MIN(f.rel_path) name, GROUP_CONCAT(l.root || '/' || l.rel_path || '/' || f.rel_path, '\n') paths, "
                    "COUNT(DISTINCT f.library_album_id) n FROM library_files f JOIN library_albums l ON l.id=f.library_album_id "
                    "WHERE f.sha256 IS NOT NULL GROUP BY f.sha256 HAVING n > 1"):
                groups.append({"kind": "identical file", "label": r["name"], "paths": r["paths"].split("\n")})
        return groups

    def missing(self) -> list[dict]:
        """Albums you had (account history or earlier downloads) that are absent or damaged locally."""
        out: dict[int, dict] = {}
        with self.db.connect() as c:
            present = {r[0] for r in c.execute(
                "SELECT album_id FROM library_albums WHERE album_id IS NOT NULL AND status != 'incomplete'")}
            lib_titles = {norm_title(r[0]) for r in c.execute("SELECT title_guess FROM library_albums")}
            scanned = c.execute("SELECT 1 FROM sync_state WHERE key LIKE 'library_scan:%'").fetchone() is not None
            for r in c.execute("SELECT a.id, a.title, a.slug, a.year, acc.source FROM account_albums acc "
                               "JOIN albums a ON a.id = acc.album_id"):
                if r["id"] not in present and norm_title(r["title"]) not in lib_titles:
                    out[r["id"]] = {**dict(r), "reason": f"in your khinsider {r['source']} but not in the library"}
            if scanned:
                for r in c.execute("SELECT DISTINCT a.id, a.title, a.slug, a.year FROM jobs j JOIN albums a ON a.id=j.album_id "
                                   "WHERE j.status='completed'"):
                    if r["id"] not in present and r["id"] not in out:
                        out[r["id"]] = {**dict(r), "reason": "downloaded earlier, folder no longer found"}
            for r in c.execute("SELECT a.id, a.title, a.slug, a.year, l.missing_count, l.mismatch_count, l.rel_path "
                               "FROM library_albums l JOIN albums a ON a.id=l.album_id WHERE l.status='incomplete'"):
                if r["id"] not in present:
                    out[r["id"]] = {"id": r["id"], "title": r["title"], "slug": r["slug"], "year": r["year"],
                                    "reason": f"{r['missing_count']} missing / {r['mismatch_count']} damaged file(s) in {r['rel_path']}"}
        return sorted(out.values(), key=lambda d: d["title"].casefold())

    def recover(self, album_ids: list[int], fmt: str) -> queue.EnqueueResult:
        """Queue missing albums again. Existing good files are kept by the worker (skipped)."""
        return queue.enqueue(self.db, album_ids, fmt, allow_completed=True)

    # ------------------------------------------------------------------ account history
    async def sync_history(self, client, sources: list[str] | None = None) -> dict[str, int]:
        sources = sources or list(ACCOUNT_PAGES)
        counts: dict[str, int] = {}
        for src in sources:
            albums = await client.account_albums(ACCOUNT_PAGES[src])
            now = utcnow()
            with self.db.transaction() as c:
                for a in albums:
                    aid = upsert_album_summary(c, a, now)
                    c.execute("INSERT INTO account_albums(slug, album_id, title, source, first_seen_at, last_seen_at) "
                              "VALUES (?,?,?,?,?,?) ON CONFLICT(slug) DO UPDATE SET album_id=excluded.album_id, "
                              "title=excluded.title, last_seen_at=excluded.last_seen_at",
                              (a.slug, aid, a.title, src, now, now))
            counts[src] = len(albums)
        self.db.set_sync_state("history_sync", utcnow())
        log.info("account history import: %s", counts)
        return counts
