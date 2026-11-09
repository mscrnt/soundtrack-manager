"""Album discovery: site-side filters where supported, local filters otherwise.

Site capabilities (docs/RESEARCH.md):

* ``/search`` takes one platform, one album type and one exact year, and
  returns every match on a single page.
* ``/game-soundtracks/<platform>`` lists a platform's albums, 500 per page.

Year ranges, several platforms and filters with no text are handled by
fetching the wider listing once, caching it, and filtering locally.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta, timezone

from .db import Database, utcnow
from .khinsider.catalog import (SEARCH_SORTS, AlbumType, Platform, find_album_type,
                                find_platform, matches_type)
from .khinsider.client import KhinsiderClient
from .khinsider.models import AlbumDetail, AlbumSummary

log = logging.getLogger(__name__)


class QueryError(ValueError):
    pass


MAX_TERMS = 10  # cap on '|'-separated search terms (each is one site request)


@dataclass
class SearchQuery:
    text: str = ""
    platforms: list[Platform] = field(default_factory=list)
    album_type: AlbumType | None = None
    year_from: int | None = None
    year_to: int | None = None
    sort: str = "relevance"

    @classmethod
    def build(cls, text: str = "", platforms: list[str] | None = None, album_type: str | None = None,
              year: str | None = None, sort: str | None = None) -> "SearchQuery":
        plats = []
        for p in platforms or []:
            if not p:
                continue
            found = find_platform(p)
            if found is None:
                raise QueryError(f"unknown platform: {p!r}")
            if found not in plats:
                plats.append(found)
        atype = None
        if album_type:
            atype = find_album_type(album_type)
            if atype is None:
                raise QueryError(f"unknown album type: {album_type!r}")
        y_from, y_to = parse_year_range(year)
        sort = (sort or "relevance").lower()
        if sort not in SEARCH_SORTS:
            raise QueryError(f"unknown sort: {sort!r} (use one of {', '.join(SEARCH_SORTS)})")
        q = cls(text=(text or "").strip(), platforms=plats, album_type=atype,
                year_from=y_from, year_to=y_to, sort=sort)
        if not q.texts and not q.platforms:
            raise QueryError("enter search text or choose at least one platform")
        return q

    @property
    def texts(self) -> list[str]:
        """Search terms; 'Resident Evil | Silent Hill' runs one site search per term."""
        return [t.strip() for t in self.text.split("|") if t.strip()][:MAX_TERMS]

    @property
    def single_year(self) -> int | None:
        if self.year_from is not None and self.year_from == self.year_to:
            return self.year_from
        return None

    def to_json(self) -> str:
        d = asdict(self)
        d["platforms"] = [p.name for p in self.platforms]
        d["album_type"] = self.album_type.label if self.album_type else None
        return json.dumps(d, sort_keys=True)


def parse_year_range(value: str | None) -> tuple[int | None, int | None]:
    """'1998' -> (1998, 1998); '1996-2000' -> (1996, 2000); '1996-' -> (1996, None)."""
    if value is None or not str(value).strip():
        return None, None
    s = str(value).strip().replace("..", "-").replace("–", "-")
    try:
        if "-" in s:
            a, _, b = s.partition("-")
            lo = int(a) if a.strip() else None
            hi = int(b) if b.strip() else None
        else:
            lo = hi = int(s)
    except ValueError as exc:
        raise QueryError(f"invalid year or range: {value!r}") from exc
    for y in (lo, hi):
        if y is not None and not 1970 <= y <= 2100:
            raise QueryError(f"year out of range: {y}")
    if lo is not None and hi is not None and lo > hi:
        lo, hi = hi, lo
    return lo, hi


@dataclass
class DiscoveryResult:
    query: SearchQuery
    albums: list[dict]          # DB rows (as dicts) after local filtering/sorting
    page: int
    per_page: int
    total: int                  # after local filtering
    site_total: int | None      # what the site reported for the fetched listing(s)
    from_cache: bool
    notes: list[str] = field(default_factory=list)

    @property
    def page_count(self) -> int:
        return max(1, -(-self.total // self.per_page))

    @property
    def page_albums(self) -> list[dict]:
        start = (self.page - 1) * self.per_page
        return self.albums[start:start + self.per_page]


class DiscoveryService:
    def __init__(self, db: Database, client: KhinsiderClient, *, cache_ttl_hours: int = 24,
                 max_browse_pages: int = 20):
        self.db = db
        self.client = client
        self.cache_ttl = timedelta(hours=cache_ttl_hours)
        self.max_browse_pages = max_browse_pages

    # ------------------------------------------------------------------
    async def discover(self, q: SearchQuery, *, page: int = 1, per_page: int = 50,
                       refresh: bool = False, record_history: bool = True) -> DiscoveryResult:
        if len(q.texts) > 1:
            return await self._discover_terms(q, page=page, per_page=per_page, refresh=refresh,
                                              record_history=record_history)
        q = replace(q, text=q.texts[0] if q.texts else "")
        notes: list[str] = []
        listings: list[tuple[int, bool]] = []  # (listing_id, from_cache)
        site_total = 0
        site_filtered = {"platform": False, "type": False, "year": False}

        if q.text:
            cat = q.platforms[0].category_id if len(q.platforms) == 1 else None
            key = "search:" + json.dumps(
                [q.text.lower(), q.sort, q.album_type.id if q.album_type else None, q.single_year, cat])
            lid, cached, total = await self._listing(key, "search", refresh, lambda: self._fetch_search(q, cat))
            listings.append((lid, cached))
            site_total = total or 0
            site_filtered = {"platform": cat is not None, "type": q.album_type is not None,
                             "year": q.single_year is not None}
            if q.year_from != q.year_to:
                notes.append("The site filters by single years only; the year range was applied locally.")
            if len(q.platforms) > 1:
                notes.append("The site filters by one platform at a time; platforms were matched locally.")
        else:
            for p in q.platforms:
                key = f"browse:{p.slug}"
                lid, cached, total = await self._listing(key, "browse", refresh, lambda p=p: self._fetch_browse(p))
                listings.append((lid, cached))
                site_total += total or 0
                if not self._listing_complete(lid):
                    notes.append(f"{p.name}: listing stopped at the {self.max_browse_pages}-page cap "
                                 "(DISCOVER_MAX_PAGES); some albums are missing.")
            if len(q.platforms) > 1:
                site_filtered["platform"] = False
            else:
                site_filtered["platform"] = True

        rows = self._rows_for_listings([lid for lid, _ in listings])
        rows = [r for r in rows if self._keep(r, q, site_filtered)]
        rows = self._sort(rows, q)
        if q.sort in ("popularity", "timestamp", "relevance") and not q.text:
            notes.append("Browse listings are ordered by name; popularity/date-added sorting needs search text.")

        result = DiscoveryResult(
            query=q, albums=rows, page=max(1, page), per_page=per_page, total=len(rows),
            site_total=site_total, from_cache=all(c for _, c in listings), notes=notes,
        )
        result.page = min(result.page, result.page_count)
        if record_history:
            with self.db.connect() as c:
                c.execute("INSERT INTO search_history(query_json, listing_id, result_count) VALUES (?,?,?)",
                          (q.to_json(), listings[0][0] if listings else None, len(rows)))
        return result

    async def _discover_terms(self, q: SearchQuery, *, page, per_page, refresh, record_history) -> DiscoveryResult:
        rows: list[dict] = []
        seen: set[int] = set()
        notes: list[str] = []
        site_total = 0
        cached = True
        for term in q.texts:
            r = await self.discover(replace(q, text=term), per_page=10**9, refresh=refresh, record_history=False)
            site_total += r.site_total or 0
            cached = cached and r.from_cache
            notes += [n for n in r.notes if n not in notes]
            for a in r.albums:
                if a["id"] not in seen:
                    seen.add(a["id"])
                    rows.append(a)
        rows = self._sort(rows, q)
        notes.insert(0, f"Combined {len(q.texts)} searches: " + ", ".join(q.texts) + ".")
        result = DiscoveryResult(query=q, albums=rows, page=max(1, page), per_page=per_page, total=len(rows),
                                 site_total=site_total, from_cache=cached, notes=notes)
        result.page = min(result.page, result.page_count)
        if record_history:
            with self.db.connect() as c:
                c.execute("INSERT INTO search_history(query_json, result_count) VALUES (?,?)", (q.to_json(), len(rows)))
        return result

    # ------------------------------------------------------------------
    async def _fetch_search(self, q: SearchQuery, cat: int | None):
        _, rp = await self.client.search(
            q.text, sort=q.sort, album_type_id=q.album_type.id if q.album_type else None,
            year=q.single_year, category_id=cat)
        return rp.albums, rp.total_reported, 1, True

    async def _fetch_browse(self, p: Platform):
        return await self.client.browse_platform(p.slug, self.max_browse_pages)

    async def _listing(self, key: str, kind: str, refresh: bool, fetch) -> tuple[int, bool, int | None]:
        with self.db.connect() as c:
            row = c.execute("SELECT id, fetched_at, total_reported FROM listings WHERE cache_key=?", (key,)).fetchone()
        if row and not refresh and self._fresh(row["fetched_at"]):
            return row["id"], True, row["total_reported"]
        albums, total, pages, complete = await fetch()
        lid = self._store_listing(key, kind, albums, total, pages, complete)
        return lid, False, total

    def _fresh(self, fetched_at: str) -> bool:
        if self.cache_ttl.total_seconds() <= 0:
            return False
        ts = datetime.strptime(fetched_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) - ts < self.cache_ttl

    def _listing_complete(self, lid: int) -> bool:
        with self.db.connect() as c:
            return bool(c.execute("SELECT complete FROM listings WHERE id=?", (lid,)).fetchone()[0])

    def _store_listing(self, key, kind, albums: list[AlbumSummary], total, pages, complete) -> int:
        now = utcnow()
        with self.db.transaction() as c:
            c.execute(
                "INSERT INTO listings(cache_key, kind, total_reported, pages_fetched, complete, fetched_at) "
                "VALUES (?,?,?,?,?,?) ON CONFLICT(cache_key) DO UPDATE SET total_reported=excluded.total_reported, "
                "pages_fetched=excluded.pages_fetched, complete=excluded.complete, fetched_at=excluded.fetched_at",
                (key, kind, total, pages, int(complete), now))
            lid = c.execute("SELECT id FROM listings WHERE cache_key=?", (key,)).fetchone()[0]
            c.execute("DELETE FROM listing_albums WHERE listing_id=?", (lid,))
            seen: set[int] = set()
            pos = 0
            for a in albums:
                aid = upsert_album_summary(c, a, now)
                if aid in seen:
                    continue
                seen.add(aid)
                c.execute("INSERT INTO listing_albums(listing_id, position, album_id) VALUES (?,?,?)", (lid, pos, aid))
                pos += 1
        return lid

    def _rows_for_listings(self, lids: list[int]) -> list[dict]:
        out: list[dict] = []
        seen: set[int] = set()
        with self.db.connect() as c:
            for lid in lids:
                for r in c.execute(
                    "SELECT a.*, la.position FROM listing_albums la JOIN albums a ON a.id = la.album_id "
                    "WHERE la.listing_id=? ORDER BY la.position", (lid,)):
                    if r["id"] in seen:
                        continue
                    seen.add(r["id"])
                    out.append(album_row(r))
        return out

    @staticmethod
    def _keep(r: dict, q: SearchQuery, site_filtered: dict) -> bool:
        if q.platforms and not site_filtered["platform"]:
            names = {p.name for p in q.platforms}
            if not names.intersection(r["platforms"]):
                return False
        if q.album_type and not site_filtered["type"] and not matches_type(r["album_type"], q.album_type):
            return False
        if (q.year_from is not None or q.year_to is not None) and not site_filtered["year"]:
            y = r["year"]
            if y is None:
                return False
            if q.year_from is not None and y < q.year_from:
                return False
            if q.year_to is not None and y > q.year_to:
                return False
        return True

    @staticmethod
    def _sort(rows: list[dict], q: SearchQuery) -> list[dict]:
        if q.sort == "name":
            return sorted(rows, key=lambda r: r["title"].casefold())
        if q.sort == "year":
            # site's year sort is newest first; unknown years last
            return sorted(rows, key=lambda r: (r["year"] is None, -(r["year"] or 0), r["title"].casefold()))
        return rows  # keep site order

    # ------------------------------------------------------------------
    async def album_detail(self, slug: str, *, refresh: bool = False) -> dict:
        with self.db.connect() as c:
            row = c.execute("SELECT * FROM albums WHERE slug=?", (slug,)).fetchone()
        if row and row["detail_fetched_at"] and not refresh and self._fresh(row["detail_fetched_at"]):
            return self.get_album(row["id"])
        detail = await self.client.album(slug)
        aid = store_album_detail(self.db, detail)
        return self.get_album(aid)

    def get_album(self, album_id: int) -> dict | None:
        with self.db.connect() as c:
            r = c.execute("SELECT * FROM albums WHERE id=?", (album_id,)).fetchone()
            if r is None:
                return None
            d = album_row(r)
            d["tracks"] = [
                {**dict(t), "sizes": json.loads(t["sizes_json"])}
                for t in c.execute("SELECT * FROM tracks WHERE album_id=? ORDER BY position", (album_id,))
            ]
        return d

    def history(self, limit: int = 20) -> list[dict]:
        with self.db.connect() as c:
            return [dict(r) for r in c.execute(
                "SELECT id, query_json, result_count, created_at FROM search_history ORDER BY id DESC LIMIT ?", (limit,))]


# ----------------------------------------------------------------------
# persistence helpers (module level so the queue and CLI can reuse them)

def album_row(r) -> dict:
    d = dict(r)
    d["platforms"] = json.loads(d.pop("platforms_json") or "[]")
    for k in ("formats", "cover_urls", "raw_metadata"):
        v = d.pop(f"{k}_json", None)
        d[k] = json.loads(v) if v else None
    return d


def upsert_album_summary(c, a: AlbumSummary, now: str) -> int:
    """Insert or refresh summary fields. Existing values are not replaced with NULL."""
    c.execute(
        "INSERT INTO albums(slug, url, title, platforms_json, album_type, year, catalog_number, thumb_url, "
        "first_seen_at, last_seen_at) VALUES (?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(slug) DO UPDATE SET url=excluded.url, title=excluded.title, "
        "platforms_json=CASE WHEN excluded.platforms_json != '[]' THEN excluded.platforms_json ELSE albums.platforms_json END, "
        "album_type=COALESCE(excluded.album_type, albums.album_type), year=COALESCE(excluded.year, albums.year), "
        "catalog_number=COALESCE(excluded.catalog_number, albums.catalog_number), "
        "thumb_url=COALESCE(excluded.thumb_url, albums.thumb_url), last_seen_at=excluded.last_seen_at",
        (a.slug, a.url, a.title, json.dumps(a.platforms), a.album_type, a.year, a.catalog_number,
         a.thumb_url, now, now))
    return c.execute("SELECT id FROM albums WHERE slug=?", (a.slug,)).fetchone()[0]


def store_album_detail(db: Database, d: AlbumDetail) -> int:
    now = utcnow()
    with db.transaction() as c:
        aid = upsert_album_summary(c, AlbumSummary(
            slug=d.slug, url=d.url, title=d.title, platforms=d.platforms, album_type=d.album_type,
            year=d.year, catalog_number=d.catalog_number), now)
        c.execute(
            "UPDATE albums SET site_album_id=?, publisher=?, developer=?, date_added=?, file_count=?, "
            "track_count=?, total_size_text=?, formats_json=?, cover_urls_json=?, description=?, "
            "raw_metadata_json=?, detail_fetched_at=? WHERE id=?",
            (d.site_album_id, d.publisher, d.developer, d.date_added, d.file_count, d.track_count,
             d.total_size_text, json.dumps(d.formats), json.dumps(d.cover_urls), d.description,
             json.dumps(d.raw_metadata), now, aid))
        # Upsert so track ids (referenced by job_files) stay stable across refreshes.
        urls = []
        for i, t in enumerate(d.tracks):
            urls.append(t.page_url)
            c.execute(
                "INSERT INTO tracks(album_id, position, disc, number, title, duration, page_url, sizes_json) "
                "VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(album_id, page_url) DO UPDATE SET position=excluded.position, "
                "disc=excluded.disc, number=excluded.number, title=excluded.title, duration=excluded.duration, "
                "sizes_json=excluded.sizes_json",
                (aid, i, t.disc, t.number, t.title, t.duration, t.page_url, json.dumps(t.sizes)))
        if urls:
            c.execute(f"DELETE FROM tracks WHERE album_id=? AND page_url NOT IN ({','.join('?' * len(urls))})",
                      (aid, *urls))
        else:
            c.execute("DELETE FROM tracks WHERE album_id=?", (aid,))
    return aid
