"""Import lists: parse a shopping list (text) or CSV, match each entry against the site, queue the matches.

Supported files
---------------
* **Text lists**, one album per line. Optional ``[ ]`` / ``[x]`` checkboxes and ``12.``
  numbering, ``PRIORITY 2 - ...`` section headings, a note after two spaces and a
  dash (``Title  - why``), and a khinsider search or album URL on the next line.
  If any line is numbered or has a checkbox, only those lines are entries and the
  rest is treated as headings/prose. ``#`` starts a comment line.
* **CSV** (comma, semicolon or tab) with a header row. Columns (case-insensitive):
  ``title``, ``url``, ``search``, ``platform``, ``year``, ``format``, ``priority``,
  ``note``, ``done``. Every row needs a title or a URL.

Matching
--------
An entry with an album URL is used as-is. Otherwise its search term (from the
URL, the ``search`` column, or derived from the title) runs one site search and
every result is scored against the full title: distinctive words count, generic
ones ("Original Soundtrack", "OST", "the") don't, small spelling differences are
tolerated, numbers must agree, and year/platform hints add or subtract. A clear
winner is matched automatically; close calls are left for review.

After a match the album page is read to check that the wanted format (FLAC) is
offered. Nothing is downloaded until the list is queued.
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
import re
import unicodedata
from dataclasses import asdict, dataclass, field
from difflib import SequenceMatcher
from urllib.parse import parse_qs, urlsplit

from . import queue
from .config import FORMATS
from .db import Database, utcnow
from .discovery import DiscoveryService, QueryError, SearchQuery
from .khinsider.catalog import find_platform
from .khinsider.fetch import AccessBlocked
from .khinsider.urls import SITE_HOST, album_slug

log = logging.getLogger(__name__)

MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_ENTRIES = 5000
AUTO_MATCH = 0.80       # best score needed to match without review
AUTO_MARGIN = 0.08      # ...and how far ahead of the runner-up it must be
MIN_CANDIDATE = 0.35    # below this a result isn't offered as a candidate
REVIEW_MIN = 0.5        # below this the entry counts as not found (weak candidates are still listed)
KEEP_CANDIDATES = 5
ENTRY_STATUSES = ("pending", "matched", "ambiguous", "not_found", "error", "skipped", "queued")


class ImportFormatError(ValueError):
    """The file can't be read as an import list at all."""


@dataclass
class Issue:
    line: int | None
    level: str          # 'error' (row dropped) | 'warning' | 'info'
    message: str


@dataclass
class ImportEntry:
    title: str
    line: int | None = None
    search: str | None = None
    url: str | None = None          # album URL
    platform: str | None = None
    year: int | None = None
    format: str | None = None
    priority: int | None = None
    section: str | None = None
    note: str | None = None
    done: bool = False


@dataclass
class ParsedList:
    source_format: str              # 'text' | 'csv'
    entries: list[ImportEntry] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.level == "error"]

    def summary(self) -> dict:
        by_priority: dict[str, int] = {}
        for e in self.entries:
            k = str(e.priority) if e.priority is not None else "none"
            by_priority[k] = by_priority.get(k, 0) + 1
        return {"source_format": self.source_format, "entries": len(self.entries),
                "done": sum(e.done for e in self.entries),
                "with_album_url": sum(bool(e.url) for e in self.entries),
                "by_priority": by_priority,
                "errors": len(self.errors), "warnings": sum(i.level == "warning" for i in self.issues)}

    def to_dict(self) -> dict:
        return {**self.summary(), "issues": [asdict(i) for i in self.issues],
                "entries_preview": [asdict(e) for e in self.entries[:20]]}


# ----------------------------------------------------------------------
# reading files

def decode(data: bytes) -> str:
    if len(data) > MAX_FILE_BYTES:
        raise ImportFormatError(f"file is larger than {MAX_FILE_BYTES // 1024 // 1024} MB")
    if b"\x00" in data[:4096]:
        raise ImportFormatError("this looks like a binary file, not a text or CSV list")
    for enc in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1")


_CHECKBOX = re.compile(r"^\s*\[(?P<mark>[ xX✓✔-]?)\]\s*")
_NUMBER = re.compile(r"^\s*(?P<n>\d{1,5})[.)]\s+")
_PRIORITY = re.compile(r"^\s*priority\s*(?P<p>\d{1,3})\b", re.I)
_UNDERLINE = re.compile(r"^\s*[=\-_~*#]{3,}\s*$")
_NOTE_SPLIT = re.compile(r"\s{2,}[-–—]\s+|\s+[–—]\s+(?=[a-z])")
_URL = re.compile(r"https?://\S+")
_YEAR_HINT = re.compile(r"\((19[7-9]\d|20\d\d)\)")
_PAREN = re.compile(r"\(([^()]{1,40})\)")


def sniff_format(text: str, filename: str | None = None) -> str:
    name = (filename or "").lower()
    if name.endswith((".csv", ".tsv")):
        return "csv"
    if name.endswith(".txt"):
        return "text"
    first = next((ln for ln in text.splitlines() if ln.strip()), "")
    cells = [c.strip().lower() for c in re.split(r"[,;\t]", first)]
    if len(cells) > 1 and set(cells) & {"title", "album", "name", "url", "link"}:
        return "csv"
    return "text"


def parse_import(data: bytes | str, filename: str | None = None, *, source_format: str | None = None) -> ParsedList:
    text = decode(data) if isinstance(data, bytes) else data
    if not text.strip():
        raise ImportFormatError("the file is empty")
    fmt = source_format or sniff_format(text, filename)
    if fmt not in ("text", "csv"):
        raise ImportFormatError(f"unknown list format {fmt!r} (use text or csv)")
    parsed = _parse_csv(text) if fmt == "csv" else _parse_text(text)
    if len(parsed.entries) > MAX_ENTRIES:
        parsed.issues.append(Issue(None, "warning", f"only the first {MAX_ENTRIES} entries are kept"))
        parsed.entries = parsed.entries[:MAX_ENTRIES]
    _check_duplicates(parsed)
    if not parsed.entries:
        raise ImportFormatError("no entries found" + (f": {parsed.errors[0].message}" if parsed.errors else ""))
    return parsed


def _apply_url(e: ImportEntry, url: str, parsed: ParsedList, line: int | None) -> None:
    url = url.strip().rstrip(".,;)")
    parts = urlsplit(url)
    if parts.netloc.lower() != SITE_HOST:
        parsed.issues.append(Issue(line, "warning", f"ignored a non-khinsider URL: {url[:80]}"))
        return
    if parts.path.rstrip("/") == "/search":
        term = (parse_qs(parts.query).get("search") or [""])[0].strip()
        if term:
            e.search = term[:200]
        return
    try:
        album_slug(url)
    except ValueError:
        parsed.issues.append(Issue(line, "warning", f"URL is neither a search nor an album page: {url[:80]}"))
        return
    e.url = url


def _title_hints(e: ImportEntry) -> None:
    """Pick up a year '(1996)' or platform '(NES)' written in the title."""
    if e.year is None:
        m = _YEAR_HINT.search(e.title)
        if m:
            e.year = int(m.group(1))
    if e.platform is None:
        for inner in _PAREN.findall(e.title):
            p = find_platform(inner)
            if p is not None:
                e.platform = p.name
                break


def _parse_text(text: str) -> ParsedList:
    parsed = ParsedList("text")
    lines = text.splitlines()
    structured = any(_CHECKBOX.match(ln) or _NUMBER.match(ln) for ln in lines)
    priority: int | None = None
    section: str | None = None
    last: ImportEntry | None = None
    ignored = 0
    for i, raw in enumerate(lines, start=1):
        line = raw.strip()
        if not line or line.startswith("#") or _UNDERLINE.match(line):
            continue
        nxt = lines[i].strip() if i < len(lines) else ""
        if _URL.fullmatch(line):
            # A URL right below an entry belongs to it; anywhere else it is an entry of its own.
            if last is not None and not last.url and last.search is None and last.line == i - 1:
                _apply_url(last, line, parsed, i)
            else:
                e = ImportEntry(title="", line=i, priority=priority, section=section)
                _apply_url(e, line, parsed, i)
                if e.url:
                    e.title = album_slug(e.url).replace("-", " ")
                elif e.search:
                    e.title = e.search
                if e.title:
                    parsed.entries.append(e)
                    last = e
            continue
        pm = _PRIORITY.match(line)
        if pm and not (_CHECKBOX.match(line) or _NUMBER.match(line)):
            priority, section = int(pm.group("p")), line
            continue
        if _UNDERLINE.match(nxt) and not (_CHECKBOX.match(line) or _NUMBER.match(line)):
            section = line  # a heading (underlined)
            continue
        body, done = line, False
        cb = _CHECKBOX.match(body)
        if cb:
            done = cb.group("mark").strip() not in ("",)
            body = body[cb.end():]
        num = _NUMBER.match(body)
        if num:
            body = body[num.end():]
        if structured and not (cb or num):
            ignored += 1
            continue
        url_in_line = _URL.search(body)
        url = None
        if url_in_line:
            url = url_in_line.group(0)
            body = (body[:url_in_line.start()] + body[url_in_line.end():]).strip(" -|")
        title, note = body, None
        parts = _NOTE_SPLIT.split(body, maxsplit=1)
        if len(parts) == 2 and parts[0].strip():
            title, note = parts[0], parts[1]
        title = title.strip()
        if not title:
            parsed.issues.append(Issue(i, "error", "entry has no title"))
            continue
        e = ImportEntry(title=title[:300], line=i, priority=priority, section=section,
                        note=(note or "").strip()[:300] or None, done=done)
        if url:
            _apply_url(e, url, parsed, i)
        _title_hints(e)
        parsed.entries.append(e)
        last = e
    if ignored:
        parsed.issues.append(Issue(None, "info", f"{ignored} line(s) without a number or checkbox were treated as "
                                                  "headings or notes"))
    return parsed


CSV_COLUMNS = {
    "title": ("title", "album", "album title", "name", "album name"),
    "url": ("url", "link", "album url", "khinsider"),
    "search": ("search", "query", "search term", "term"),
    "platform": ("platform", "system", "console"),
    "year": ("year",),
    "format": ("format", "audio format", "quality"),
    "priority": ("priority", "prio"),
    "section": ("section", "group", "category"),
    "note": ("note", "notes", "comment", "comments", "reason"),
    "done": ("done", "got", "have", "downloaded", "checked"),
}


def _parse_csv(text: str) -> ParsedList:
    parsed = ParsedList("csv")
    sample = text[:8192]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    reader = csv.reader(io.StringIO(text), dialect)
    try:
        header = next(reader)
    except StopIteration:
        raise ImportFormatError("the CSV file is empty") from None
    cols: dict[str, int] = {}
    for idx, name in enumerate(header):
        key = name.strip().lower().lstrip("\ufeff")
        field_name = next((f for f, aliases in CSV_COLUMNS.items() if key in aliases), None)
        if field_name and field_name not in cols:
            cols[field_name] = idx
        elif key:
            parsed.issues.append(Issue(1, "warning", f"ignored column {name.strip()!r}"))
    if "title" not in cols and "url" not in cols:
        raise ImportFormatError("the CSV header needs a 'title' or 'url' column "
                                f"(found: {', '.join(h.strip() for h in header) or 'nothing'})")
    for row in reader:
        line = reader.line_num
        if not any(c.strip() for c in row):
            continue

        def get(name: str) -> str:
            idx = cols.get(name)
            return row[idx].strip() if idx is not None and idx < len(row) else ""

        e = ImportEntry(title=get("title")[:300], line=line, note=get("note")[:300] or None,
                        section=get("section")[:200] or None)
        if get("url"):
            _apply_url(e, get("url"), parsed, line)
        if get("search"):
            e.search = get("search")[:200]
        if not e.title:
            if e.url:
                e.title = album_slug(e.url).replace("-", " ")
            elif e.search:
                e.title = e.search
            else:
                parsed.issues.append(Issue(line, "error", "row has no title or URL"))
                continue
        if get("platform"):
            p = find_platform(get("platform"))
            if p is None:
                parsed.issues.append(Issue(line, "warning", f"unknown platform {get('platform')!r} ignored"))
            else:
                e.platform = p.name
        if get("year"):
            if re.fullmatch(r"(19[7-9]\d|20\d\d)", get("year")):
                e.year = int(get("year"))
            else:
                parsed.issues.append(Issue(line, "warning", f"invalid year {get('year')!r} ignored"))
        if get("format"):
            f = get("format").lower()
            if f in FORMATS:
                e.format = f
            else:
                parsed.issues.append(Issue(line, "error", f"unknown format {get('format')!r} "
                                                          f"(use {' or '.join(FORMATS)})"))
                continue
        if get("priority"):
            if get("priority").isdigit():
                e.priority = int(get("priority"))
            else:
                parsed.issues.append(Issue(line, "warning", f"priority {get('priority')!r} is not a number; ignored"))
        e.done = get("done").lower() in ("1", "x", "yes", "y", "true", "done", "✓")
        _title_hints(e)
        parsed.entries.append(e)
    return parsed


CSV_HEADER = ["title", "search", "url", "platform", "year", "format", "priority", "section", "note", "done"]


def to_csv(parsed: ParsedList) -> str:
    """Write parsed entries as an import CSV (the format :func:`parse_import` reads back)."""
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(CSV_HEADER)
    for e in parsed.entries:
        w.writerow([e.title, e.search or "", e.url or "", e.platform or "", e.year or "", e.format or "",
                    e.priority if e.priority is not None else "", e.section or "", e.note or "",
                    "x" if e.done else ""])
    return buf.getvalue()


def _check_duplicates(parsed: ParsedList) -> None:
    seen: dict[str, int | None] = {}
    for e in parsed.entries:
        key = e.url or normalize(e.title)
        if key in seen:
            parsed.issues.append(Issue(e.line, "warning", f"duplicate of the entry on line {seen[key]}: {e.title}"))
        else:
            seen[key] = e.line


# ----------------------------------------------------------------------
# matching

GENERIC = {"original", "soundtrack", "soundtracks", "ost", "the", "a", "an", "of", "to", "and", "music", "from",
           "game", "video", "official", "score", "sound", "track", "tracks", "o", "s", "t"}
_ROMAN = {"ii": "2", "iii": "3", "iv": "4", "v": "5", "vi": "6", "vii": "7", "viii": "8", "ix": "9", "x": "10",
          "xi": "11", "xii": "12", "xiii": "13", "xiv": "14", "xv": "15", "xvi": "16"}


def normalize(text: str) -> str:
    t = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    t = t.replace("&", " and ").replace("'", "").replace("’", "")
    return " ".join(re.sub(r"[^a-z0-9]+", " ", t).split())


def tokens(text: str) -> list[str]:
    out = []
    for tok in normalize(text).split():
        out.append(_ROMAN.get(tok, tok))
    return out


_BRACKETS = re.compile(r"\s*[\(\[]([^()\[\]]*)[\)\]]")
_YEAR = re.compile(r"(19[7-9]\d|20\d\d)")
# Words that mean "not the original soundtrack"; site album types with the same meaning.
ARRANGE_WORDS = {"arrange", "arranged", "arrangement", "arrangements", "arrangetracks", "remix", "remixes", "remixed",
                 "piano", "orchestra", "orchestral", "orchestrated", "jazz", "cover", "covers", "acoustic", "chiptune",
                 "live", "concert", "symphonic", "tribute", "medley"}
ARRANGE_TYPES = {"arrangement", "remix", "inspired by"}
OST_WORDS = {"soundtrack", "soundtracks", "ost", "score"}


def _strip_hints(title: str, *, platforms: bool = True) -> str:
    """Drop bracketed years '(1996)' and platforms '(NES)': they are hints, not part of the name."""
    def repl(m: re.Match) -> str:
        inner = m.group(1).strip()
        if _YEAR.fullmatch(inner) or (platforms and find_platform(inner) is not None):
            return " "
        return m.group(0)
    return " ".join(_BRACKETS.sub(repl, title).split())


def _tok_match(a: str, pool: set[str]) -> bool:
    if a in pool:
        return True
    if len(a) >= 5 and not a.isdigit():
        return any(len(b) >= 4 and SequenceMatcher(None, a, b).ratio() >= 0.8 for b in pool)
    return False


def _core(want_title: str, have_title: str) -> float:
    """Similarity of the distinctive words (generic ones ignored); numbers must agree."""
    want_all, have_all = tokens(want_title), tokens(have_title)
    want = {t for t in want_all if t not in GENERIC} or set(want_all)
    have = {t for t in have_all if t not in GENERIC} or set(have_all)
    if not want or not have:
        return 0.0
    recall = sum(_tok_match(t, have) for t in want) / len(want)
    precision = sum(_tok_match(t, want) for t in have) / len(have)
    seq = SequenceMatcher(None, " ".join(sorted(want)), " ".join(sorted(have))).ratio()
    core = 0.5 * recall + 0.25 * precision + 0.25 * seq
    nums_w = {t for t in want if t.isdigit()}
    nums_h = {t for t in have if t.isdigit()}
    if nums_w - nums_h:
        core -= 0.3       # 'Resident Evil 2' is not 'Resident Evil 3'; 'KOF 2002' is not 'KOF 2000'
    if nums_h - nums_w:
        core -= 0.15
    return core


def score_title(want_title: str, have_title: str, *, want_year: int | None = None, have_year: int | None = None,
                want_platform: str | None = None, have_platforms: list[str] | None = None,
                have_type: str | None = None, raw: bool = False) -> float:
    """0..1 similarity between a wanted album title and a site album title (``raw``: uncapped, for ranking).

    * core (85 %): distinctive words, see :func:`_core`. A bracketed part of the wanted title may be an
      alternate name ('Wild Arms 3 (Wild Arms Advanced 3rd)'), so it is also scored without it, a little lower.
    * full (15 %): the whole titles in order, generic words included. Breaks ties between
      'X Original Soundtrack' and plain 'X'.
    * album type: a list entry that doesn't say arranged/remix/piano... shouldn't get an arrangement
      album; one that says "soundtrack" prefers the site's Soundtrack type over a Gamerip.
    * year and platform hints.
    """
    want = _strip_hints(want_title)
    have = _strip_hints(have_title, platforms=False)
    core = _core(want, have)
    bare = " ".join(_BRACKETS.sub(" ", want).split())
    if bare != want and bare:
        core = max(core, _core(bare, have) - 0.2)
    full = 1.0 if normalize(want) == normalize(have) else SequenceMatcher(None, normalize(want), normalize(have)).ratio()
    base = 0.85 * core + 0.15 * full
    want_toks = set(tokens(want))
    kind = (have_type or "").strip().lower()
    if want_toks & ARRANGE_WORDS:
        if kind in ARRANGE_TYPES:
            base += 0.03
    else:
        if kind in ARRANGE_TYPES:
            base -= 0.15
        elif want_toks & OST_WORDS:   # "... Original Soundtrack": the official release over a game rip
            base += 0.06 if kind == "soundtrack" else (-0.03 if kind == "gamerip" else 0)
    if want_year and have_year:
        base += 0.08 if want_year == have_year else (-0.15 if abs(want_year - have_year) > 1 else 0)
    if want_platform and have_platforms:
        base += 0.05 if want_platform in have_platforms else -0.1
    return round(base if raw else max(0.0, min(1.0, base)), 3)


def is_exact(want_title: str, have_title: str) -> bool:
    """Same title once hints ('(1996)', '(NES)') and punctuation are ignored."""
    return normalize(_strip_hints(want_title)) == normalize(_strip_hints(have_title, platforms=False))


_STRIP_FOR_SEARCH = re.compile(r"\b(original\s+(game\s+)?soundtrack|original\s+score|soundtrack|ost)\b", re.I)


def search_term(e: ImportEntry | dict) -> str:
    """The site search for an entry: given, or the title without '(...)' and soundtrack words."""
    get = e.get if isinstance(e, dict) else lambda k: getattr(e, k)
    if get("search"):
        return get("search")
    t = re.sub(r"[\(\[][^)\]]*[\)\]]", " ", get("title"))
    t = _STRIP_FOR_SEARCH.sub(" ", t)
    t = re.sub(r"\s[-–—:]\s.*$", "", t) if len(t.split()) > 6 else t
    t = re.sub(r"[:;,!?\"]+", " ", t)
    t = " ".join(t.replace(" - ", " ").split())
    return t or get("title")


def _fallback_term(term: str) -> str | None:
    """A shorter search when the first finds nothing: drop the last word(s)."""
    words = term.split()
    if len(words) <= 2:
        return None
    return " ".join(words[: max(2, len(words) - 2)])


def rank(entry: dict, albums: list[dict]) -> list[dict]:
    out = []
    for a in albums:
        r = score_title(entry["title"], a["title"], want_year=entry.get("year"), have_year=a.get("year"),
                        want_platform=entry.get("platform"), have_platforms=a.get("platforms"),
                        have_type=a.get("album_type"), raw=True)
        if r >= MIN_CANDIDATE:
            out.append({"album_id": a["id"], "title": a["title"], "score": round(max(0.0, min(1.0, r)), 3),
                        "rank": r, "exact": is_exact(entry["title"], a["title"]), "year": a.get("year"),
                        "platforms": a.get("platforms") or [], "album_type": a.get("album_type"),
                        "url": a.get("url")})
    out.sort(key=lambda c: -c["rank"])
    return out[:KEEP_CANDIDATES]


def decide(cands: list[dict]) -> tuple[str, dict | None]:
    if not cands:
        return "not_found", None
    best = cands[0]
    key = lambda c: c.get("rank", c["score"])
    if key(best) < REVIEW_MIN:
        return "not_found", None
    runner = key(cands[1]) if len(cands) > 1 else 0.0
    if key(best) >= AUTO_MATCH and key(best) - runner >= AUTO_MARGIN:
        return "matched", best
    # The very title you wrote (hints aside), and no other candidate has it: clear enough.
    if best.get("exact") and key(best) >= AUTO_MATCH and not any(c.get("exact") for c in cands[1:]):
        return "matched", best
    return "ambiguous", None


# ----------------------------------------------------------------------
# service

class ImportService:
    def __init__(self, db: Database, discovery: DiscoveryService):
        self.db = db
        self.discovery = discovery
        self._lock = asyncio.Lock()     # one list matches at a time (shares the site rate limit)

    # ---------------------------- create/read ----------------------------
    def create(self, parsed: ParsedList, *, name: str, fmt: str, include_done: bool = False) -> int:
        if fmt not in FORMATS:
            raise ValueError(f"format must be one of {FORMATS}")
        with self.db.transaction() as c:
            lid = c.execute("INSERT INTO import_lists(name, source_format, format) VALUES (?,?,?) RETURNING id",
                            ((name or "Import").strip()[:120], parsed.source_format, fmt)).fetchone()[0]
            for pos, e in enumerate(parsed.entries):
                skip = e.done and not include_done
                c.execute(
                    "INSERT INTO import_entries(list_id, position, line, title, search, url, platform, year, format, "
                    "priority, section, note, status, error) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (lid, pos, e.line, e.title, e.search, e.url, e.platform, e.year, e.format, e.priority,
                     e.section, e.note, "skipped" if skip else "pending", "checked off in the file" if skip else None))
        return lid

    def lists(self) -> list[dict]:
        with self.db.connect() as c:
            rows = [dict(r) for r in c.execute("SELECT * FROM import_lists ORDER BY id DESC")]
        for r in rows:
            r["counts"] = self.counts(r["id"])
        return rows

    def get(self, list_id: int) -> dict | None:
        with self.db.connect() as c:
            r = c.execute("SELECT * FROM import_lists WHERE id=?", (list_id,)).fetchone()
        if r is None:
            return None
        d = dict(r)
        d["counts"] = self.counts(list_id)
        d["total"] = sum(d["counts"].values())
        d["done"] = d["total"] - d["counts"]["pending"]
        d["no_format"] = self._count(list_id, "status='matched' AND has_format=0")
        d["queueable"] = self._count(list_id, "status='matched' AND COALESCE(has_format,1)=1")
        return d

    def _count(self, list_id: int, where: str) -> int:
        with self.db.connect() as c:
            return c.execute(f"SELECT COUNT(*) FROM import_entries WHERE list_id=? AND {where}", (list_id,)).fetchone()[0]

    def counts(self, list_id: int) -> dict[str, int]:
        out = {s: 0 for s in ENTRY_STATUSES}
        with self.db.connect() as c:
            for r in c.execute("SELECT status, COUNT(*) n FROM import_entries WHERE list_id=? GROUP BY status", (list_id,)):
                out[r["status"]] = r["n"]
        return out

    def entries(self, list_id: int, status: str | None = None, q: str = "", *, limit: int = 10000) -> list[dict]:
        sql = ("SELECT e.*, l.format AS list_format, a.title AS album_title, a.url AS album_url, a.year AS album_year, a.platforms_json, "
               "(SELECT j.status FROM jobs j WHERE j.album_id=e.album_id AND j.format=COALESCE(e.format, l.format) "
               " ORDER BY j.id DESC LIMIT 1) AS job_status, "
               "(SELECT j.id FROM jobs j WHERE j.album_id=e.album_id AND j.format=COALESCE(e.format, l.format) "
               " ORDER BY j.id DESC LIMIT 1) AS job_id, "
               "EXISTS(SELECT 1 FROM library_albums la WHERE la.album_id=e.album_id AND la.status IN ('ok','unverified')) "
               " AS in_library "
               "FROM import_entries e JOIN import_lists l ON l.id=e.list_id "
               "LEFT JOIN albums a ON a.id=e.album_id WHERE e.list_id=?")
        args: list = [list_id]
        if status == "review":
            sql += " AND e.status IN ('ambiguous','not_found','error')"
        elif status == "no_format":
            sql += " AND e.status='matched' AND e.has_format=0"
        elif status:
            sql += " AND e.status=?"
            args.append(status)
        if q.strip():
            sql += " AND (e.title LIKE ? OR a.title LIKE ?)"
            args += [f"%{q.strip()}%"] * 2
        sql += " ORDER BY e.position LIMIT ?"
        args.append(limit)
        with self.db.connect() as c:
            rows = [dict(r) for r in c.execute(sql, args)]
        for r in rows:
            r["candidates"] = json.loads(r.pop("candidates_json") or "[]")
            r["formats"] = json.loads(r.pop("formats_json") or "null")
            r["album_platforms"] = json.loads(r.pop("platforms_json") or "[]")
            r["search_term"] = search_term(r)
        return rows

    def entry(self, entry_id: int) -> dict | None:
        with self.db.connect() as c:
            r = c.execute("SELECT list_id FROM import_entries WHERE id=?", (entry_id,)).fetchone()
        if r is None:
            return None
        return next((e for e in self.entries(r["list_id"]) if e["id"] == entry_id), None)

    def delete(self, list_id: int) -> bool:
        with self.db.connect() as c:
            return c.execute("DELETE FROM import_lists WHERE id=?", (list_id,)).rowcount == 1

    def _set_list(self, list_id: int, **cols) -> None:
        cols["updated_at"] = utcnow()
        with self.db.connect() as c:
            c.execute(f"UPDATE import_lists SET {', '.join(f'{k}=?' for k in cols)} WHERE id=?",
                      (*cols.values(), list_id))

    def _set_entry(self, entry_id: int, **cols) -> None:
        cols["updated_at"] = utcnow()
        with self.db.connect() as c:
            c.execute(f"UPDATE import_entries SET {', '.join(f'{k}=?' for k in cols)} WHERE id=?",
                      (*cols.values(), entry_id))

    def lists_to_resume(self) -> list[int]:
        with self.db.connect() as c:
            return [r[0] for r in c.execute("SELECT id FROM import_lists WHERE status='matching' ORDER BY id")]

    # ------------------------------ matching ------------------------------
    async def match_list(self, list_id: int) -> dict:
        """Match every pending entry. Safe to call again after a stop or crash (continues)."""
        async with self._lock:
            info = self.get(list_id)
            if info is None:
                raise ValueError("unknown import list")
            self._set_list(list_id, status="matching", error=None)
            fmt = info["format"]
            try:
                while True:
                    with self.db.connect() as c:
                        st = c.execute("SELECT status FROM import_lists WHERE id=?", (list_id,)).fetchone()
                        if st is None or st["status"] != "matching":
                            return self.get(list_id) or {}   # paused or deleted
                        row = c.execute("SELECT * FROM import_entries WHERE list_id=? AND status='pending' "
                                        "ORDER BY position LIMIT 1", (list_id,)).fetchone()
                    if row is None:
                        break
                    await self.match_entry(dict(row), fmt)
            except AccessBlocked as exc:
                self._set_list(list_id, status="failed", error=f"Access check: {exc}")
                raise
            except Exception as exc:
                log.exception("import list %d: matching stopped", list_id)
                self._set_list(list_id, status="failed", error=f"{type(exc).__name__}: {exc}")
                raise
            self._set_list(list_id, status="ready")
            log.info("import list %d matched: %s", list_id, self.counts(list_id))
            return self.get(list_id) or {}

    def pause(self, list_id: int) -> bool:
        with self.db.connect() as c:
            return c.execute("UPDATE import_lists SET status='paused', updated_at=? WHERE id=? AND status='matching'",
                             (utcnow(), list_id)).rowcount == 1

    async def match_entry(self, e: dict, fmt: str) -> None:
        want = e.get("format") or fmt
        try:
            if e.get("url"):
                album = await self.discovery.album_detail(album_slug(e["url"]))
                self._set_entry(e["id"], status="matched", match_method="url", album_id=album["id"], score=1.0,
                                candidates_json="[]", error=None, **self._format_cols(album, want))
                return
            term = search_term(e)
            albums = await self._search(term)
            if not albums and (short := _fallback_term(term)):
                albums = await self._search(short)
            cands = rank(e, albums)
            status, best = decide(cands)
            cols = {"status": status, "candidates_json": json.dumps(cands), "error": None,
                    "score": cands[0]["score"] if cands else None, "album_id": None, "match_method": None,
                    "has_format": None, "formats_json": None}
            if best:
                cols.update(album_id=best["album_id"], match_method="auto")
                try:
                    album = await self.discovery.album_detail(self._slug(best["album_id"]))
                    cols.update(self._format_cols(album, want))
                except AccessBlocked:
                    raise
                except Exception as exc:  # keep the match; the format is just unknown (checked at download)
                    log.warning("import entry %s: format check failed: %s", e["id"], exc)
                    cols["error"] = f"format not checked: {type(exc).__name__}"[:300]
            self._set_entry(e["id"], **cols)
        except AccessBlocked:
            raise
        except (QueryError, ValueError) as exc:
            self._set_entry(e["id"], status="error", error=str(exc)[:300])
        except Exception as exc:  # one bad entry must not stop a 500-entry list
            log.warning("import entry %s (%s) failed: %s", e["id"], e["title"], exc)
            self._set_entry(e["id"], status="error", error=f"{type(exc).__name__}: {exc}"[:300])

    async def _search(self, term: str) -> list[dict]:
        res = await self.discovery.discover(SearchQuery.build(term), per_page=10**6, record_history=False)
        return res.albums

    def _slug(self, album_id: int) -> str:
        with self.db.connect() as c:
            r = c.execute("SELECT slug FROM albums WHERE id=?", (album_id,)).fetchone()
        if r is None:
            raise ValueError("unknown album")
        return r["slug"]

    @staticmethod
    def _format_cols(album: dict, want: str) -> dict:
        offered = [f.upper() for f in (album.get("formats") or [])]
        return {"formats_json": json.dumps(offered), "has_format": int(want.upper() in offered) if offered else None}

    # ------------------------------ review ------------------------------
    async def choose(self, entry_id: int, album_id: int) -> dict:
        e = self.entry(entry_id)
        if e is None:
            raise ValueError("unknown import entry")
        if e["status"] == "queued":
            raise ValueError("this entry is already queued")
        album = await self.discovery.album_detail(self._slug(album_id))
        info = self.get(e["list_id"])
        cand = next((c for c in e["candidates"] if c["album_id"] == album_id), None)
        self._set_entry(entry_id, status="matched", match_method="manual", album_id=album_id,
                        score=cand["score"] if cand else None, error=None,
                        **self._format_cols(album, e["format"] or info["format"]))
        return self.entry(entry_id)

    async def choose_url(self, entry_id: int, url: str) -> dict:
        album = await self.discovery.album_detail(album_slug(url))
        return await self.choose(entry_id, album["id"])

    async def research(self, entry_id: int, term: str) -> dict:
        e = self.entry(entry_id)
        if e is None:
            raise ValueError("unknown import entry")
        term = term.strip()[:200]
        if not term:
            raise ValueError("enter a search term")
        self._set_entry(entry_id, search=term, url=None, status="pending")
        info = self.get(e["list_id"])
        e = self.entry(entry_id)
        await self.match_entry(e, info["format"])
        return self.entry(entry_id)

    def skip(self, entry_id: int) -> bool:
        with self.db.connect() as c:
            return c.execute("UPDATE import_entries SET status='skipped', updated_at=? WHERE id=? AND status != 'queued'",
                             (utcnow(), entry_id)).rowcount == 1

    def restore(self, entry_id: int) -> bool:
        """Undo a skip: back to matched / review / pending, depending on what is known."""
        with self.db.connect() as c:
            return c.execute(
                "UPDATE import_entries SET updated_at=?, error=NULL, status=CASE WHEN album_id IS NOT NULL THEN 'matched' "
                "WHEN candidates_json IS NULL THEN 'pending' WHEN candidates_json='[]' THEN 'not_found' ELSE 'ambiguous' END "
                "WHERE id=? AND status='skipped'", (utcnow(), entry_id)).rowcount == 1

    # ------------------------------ queue ------------------------------
    def queue(self, list_id: int, *, include_without_format: bool = False,
              entry_ids: list[int] | None = None) -> dict:
        """Queue matched entries in priority order. Albums lacking the format are skipped unless included."""
        info = self.get(list_id)
        if info is None:
            raise ValueError("unknown import list")
        sql = "SELECT * FROM import_entries WHERE list_id=? AND status='matched' AND album_id IS NOT NULL"
        args: list = [list_id]
        if entry_ids is not None:
            sql += f" AND id IN ({','.join('?' * len(entry_ids)) or 'NULL'})"
            args += entry_ids
        with self.db.connect() as c:
            rows = [dict(r) for r in c.execute(sql + " ORDER BY COALESCE(priority, 1000000), position", args)]
        out = {"queued": 0, "already_queued": 0, "already_downloaded": 0, "skipped_no_format": 0}
        for r in rows:
            if r["has_format"] == 0 and not include_without_format:
                out["skipped_no_format"] += 1
                continue
            fmt = r["format"] or info["format"]
            # Lower list priority number = sooner; manual queue picks (priority 0) stay ahead.
            res = queue.enqueue(self.db, [r["album_id"]], fmt, priority=-(r["priority"] or 0))
            if res.added:
                out["queued"] += 1
            elif res.already_queued:
                out["already_queued"] += 1
            elif res.already_completed:
                out["already_downloaded"] += 1
            if res.added or res.already_queued:
                self._set_entry(r["id"], status="queued")
        return out

    # ------------------------------ export ------------------------------
    def export_csv(self, list_id: int) -> str:
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["position", "title", "priority", "section", "note", "status", "score", "album_title", "url",
                    "year", "platform", "formats", "has_format", "job_status", "search"])
        for e in self.entries(list_id):
            w.writerow([e["position"] + 1, e["title"], e["priority"] or "", e["section"] or "", e["note"] or "",
                        e["status"], e["score"] if e["score"] is not None else "", e["album_title"] or "",
                        e["album_url"] or e["url"] or "", e["year"] or "", e["platform"] or "",
                        " ".join(e["formats"] or []), "" if e["has_format"] is None else ("yes" if e["has_format"] else "no"),
                        e["job_status"] or "", e["search_term"]])
        return buf.getvalue()
