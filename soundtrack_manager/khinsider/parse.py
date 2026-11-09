"""Pure HTML parsers for khinsider pages. No network access here.

Every parser works from table headers and labels rather than fixed positions
where it can. If a page has none of the structure it expects, it raises
``LayoutChangedError`` instead of returning empty results, so a site redesign
is reported rather than treated as "no albums found".
"""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlsplit

from bs4 import BeautifulSoup, Tag

from .models import AlbumDetail, AlbumSummary, ResultPage, TrackFile, TrackRow
from .urls import BASE_URL, absolute, album_slug


class LayoutChangedError(Exception):
    """The page did not have the structure the parser expects."""


_FOUND_RE = re.compile(r"Found\s+([\d,]+)\s+(?:matching\s+)?(?:albums?|results?)", re.I)
_YEAR_RE = re.compile(r"^(19|20)\d{2}$")
_CHALLENGE_MARKERS = (
    "<title>just a moment...</title>",
    "challenges.cloudflare.com",
    "cf-chl-",
    "challenge-platform",
)
_CAPTCHA_MARKERS = ("g-recaptcha", "h-captcha", "cf-turnstile", "qa-captcha", "data-sitekey")


def _soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "lxml")


def _text(el: Tag | None) -> str:
    return el.get_text(" ", strip=True) if el else ""


def is_challenge_page(html: str) -> bool:
    """True if the HTML is a Cloudflare interstitial rather than site content."""
    head = html[:20000].lower()
    return any(m in head for m in _CHALLENGE_MARKERS) and "id=\"pagecontent\"" not in head


def _expand_headers(row: Tag) -> list[str]:
    cols: list[str] = []
    for th in row.find_all(["th", "td"], recursive=False):
        span = int(th.get("colspan", 1) or 1)
        cols.extend([_text(th)] * span)
    return cols


def _to_int(s: str | None) -> int | None:
    if not s:
        return None
    m = re.search(r"\d[\d,]*", s)
    return int(m.group(0).replace(",", "")) if m else None


# --------------------------------------------------------------------------
# search / browse result tables
# --------------------------------------------------------------------------

def parse_result_page(html: str, page_url: str = BASE_URL) -> ResultPage:
    """Parse a /search page or a /game-soundtracks/<platform> browse page."""
    soup = _soup(html)
    content = soup.select_one("#pageContent") or soup
    found = _FOUND_RE.search(_text(content))
    total = int(found.group(1).replace(",", "")) if found else None

    table = content.select_one("table.albumList")
    if table is None:
        if total == 0:
            return ResultPage(albums=[], total_reported=0)
        raise LayoutChangedError("no table.albumList and no 'Found N' message on result page")

    rows = table.find_all("tr")
    header = _expand_headers(rows[0]) if rows and rows[0].find("th") else []
    col = {"title": None, "platform": None, "type": None, "year": None}
    for i, h in enumerate(header):
        hl = h.lower()
        for key, prefix in (("title", "album"), ("platform", "platform"), ("type", "type"), ("year", "year")):
            if col[key] is None and hl.startswith(prefix):
                col[key] = i
    if col["title"] is None:
        raise LayoutChangedError(f"result table header not recognised: {header!r}")

    albums: list[AlbumSummary] = []
    for tr in rows[1:]:
        tds = tr.find_all("td", recursive=False)
        if not tds:
            continue
        link = None
        if col["title"] < len(tds):
            link = tds[col["title"]].find("a", href=re.compile(r"/game-soundtracks/album/"))
        if link is None:
            link = tr.find("a", href=re.compile(r"/game-soundtracks/album/"), string=True)
        if link is None:
            continue
        url = absolute(link["href"])
        try:
            slug = album_slug(url)
        except ValueError:
            continue
        title_cell = link.find_parent("td")
        catalog = None
        if title_cell is not None:
            span = title_cell.find("span")
            if span:
                catalog = _text(span).strip("[] ") or None

        def cell(key: str) -> Tag | None:
            i = col[key]
            return tds[i] if i is not None and i < len(tds) else None

        plat_cell = cell("platform")
        platforms = [_text(a) for a in plat_cell.find_all("a")] if plat_cell else []
        if plat_cell is not None and not platforms and _text(plat_cell):
            platforms = [p.strip() for p in _text(plat_cell).split(",") if p.strip()]
        year_txt = _text(cell("year"))
        img = tr.select_one("td.albumIcon img")
        albums.append(
            AlbumSummary(
                slug=slug,
                url=url,
                title=_text(link),
                platforms=platforms,
                album_type=_text(cell("type")) or None,
                year=int(year_txt) if _YEAR_RE.match(year_txt) else None,
                catalog_number=catalog,
                thumb_url=img.get("src") if img else None,
            )
        )

    next_url, last_page = _pagination(content, page_url)
    return ResultPage(albums=albums, total_reported=total, next_page_url=next_url, last_page=last_page)


def _pagination(content: Tag, page_url: str) -> tuple[str | None, int | None]:
    current = int(parse_qs(urlsplit(page_url).query).get("page", ["1"])[0] or 1)
    pages: dict[int, str] = {}
    for a in content.find_all("a", href=True):
        q = parse_qs(urlsplit(a["href"]).query)
        if "page" in q and q["page"][0].isdigit():
            pages[int(q["page"][0])] = absolute(a["href"], page_url)
    if not pages:
        return None, None
    last = max(pages)
    return pages.get(current + 1), max(last, current)


def parse_search_filters(html: str) -> dict[str, list[tuple[str, str]]]:
    """Read the filter <select> options from a search page (for catalog refresh).

    The site's platform select is wrongly named ``album_year``. Both year-like
    selects are told apart by whether their option values look like years.
    """
    soup = _soup(html)
    form = soup.select_one('form[action="/music/search"]')
    if form is None:
        raise LayoutChangedError("search filter form not found")
    out: dict[str, list[tuple[str, str]]] = {}
    for sel in form.find_all("select"):
        opts = [(o.get("value", ""), _text(o)) for o in sel.find_all("option") if o.get("value") not in ("", "0")]
        name = sel.get("name", "")
        if name == "album_year" and opts and not all(_YEAR_RE.match(v) for v, _ in opts):
            name = "album_category"
        out[name] = opts
    return out


# --------------------------------------------------------------------------
# album page
# --------------------------------------------------------------------------

_META_KEYS = {
    "platforms": "platforms",
    "platform": "platforms",
    "year": "year",
    "published by": "publisher",
    "developed by": "developer",
    "number of files": "file_count",
    "total filesize": "total_size_text",
    "date added": "date_added",
    "album type": "album_type",
    "catalog number": "catalog_number",
}


def _metadata_block(content: Tag) -> dict[str, str]:
    for p in content.find_all("p"):
        txt = p.get_text()
        if "Number of Files" not in txt and "Album type" not in txt and "Platforms:" not in txt:
            continue
        for br in p.find_all("br"):
            br.replace_with("\n")
        meta: dict[str, str] = {}
        for line in p.get_text().splitlines():
            if ":" in line:
                k, _, v = line.partition(":")
                k, v = k.strip(), " ".join(v.split())
                if k and v:
                    meta[k] = v
        return meta
    return {}


def parse_album(html: str, url: str) -> AlbumDetail:
    soup = _soup(html)
    content = soup.select_one("#pageContent")
    if content is None:
        raise LayoutChangedError("album page has no #pageContent")
    h2 = content.find("h2")
    songlist = content.select_one("table#songlist")
    if h2 is None or songlist is None:
        raise LayoutChangedError("album page missing title or #songlist table")

    raw = _metadata_block(content)
    norm: dict[str, str] = {}
    for k, v in raw.items():
        key = _META_KEYS.get(k.lower())
        if key:
            norm[key] = v

    year_txt = norm.get("year", "")
    detail = AlbumDetail(
        slug=album_slug(url),
        url=url,
        title=_text(h2),
        platforms=[p.strip() for p in norm["platforms"].split(",")] if "platforms" in norm else [],
        album_type=norm.get("album_type"),
        year=int(year_txt) if _YEAR_RE.match(year_txt) else None,
        catalog_number=norm.get("catalog_number"),
        publisher=norm.get("publisher"),
        developer=norm.get("developer"),
        date_added=norm.get("date_added"),
        file_count=_to_int(norm.get("file_count")),
        total_size_text=norm.get("total_size_text"),
        raw_metadata=raw,
    )

    mass = content.find("a", href=re.compile(r"^/cp/add_album/\d+"))
    if mass:
        detail.site_album_id = int(mass["href"].rsplit("/", 1)[1])

    detail.cover_urls = [a["href"] for a in content.select(".albumImage a[href]")]

    desc_h2 = next((h for h in content.find_all("h2") if _text(h).lower() == "description"), None)
    if desc_h2:
        p = desc_h2.find_next_sibling("p")
        detail.description = _text(p) or None

    detail.formats, detail.tracks = _parse_songlist(songlist)
    return detail


def _parse_songlist(table: Tag) -> tuple[list[str], list[TrackRow]]:
    header_row = table.find("tr", id="songlist_header") or table.find("tr")
    header = _expand_headers(header_row)
    try:
        name_idx = next(i for i, h in enumerate(header) if h.lower() == "song name")
    except StopIteration as exc:
        raise LayoutChangedError(f"#songlist header has no 'Song Name': {header!r}") from exc
    formats = [h for h in header[name_idx + 1:] if h and h.lower() != "song name" and re.fullmatch(r"[A-Za-z0-9]{2,5}", h)]
    fmt_idx = {i: h for i, h in enumerate(header) if h in formats}
    num_idx = next((i for i, h in enumerate(header) if h == "#"), None)
    cd_idx = next((i for i, h in enumerate(header) if h.upper() == "CD"), None)

    tracks: list[TrackRow] = []
    for tr in table.find_all("tr"):
        if tr.get("id") in ("songlist_header", "songlist_footer") or tr.find("th"):
            continue
        tds = tr.find_all("td", recursive=False)
        link = tr.select_one("td.clickable-row a[href]") or tr.find("a", href=re.compile(r"/game-soundtracks/album/.+/.+"))
        if link is None:
            continue
        aligned = len(tds) >= len(header)
        number = disc = None
        duration = None
        sizes: dict[str, str] = {}
        if aligned:
            if num_idx is not None:
                number = _to_int(_text(tds[num_idx]))
            if cd_idx is not None:
                disc = _to_int(_text(tds[cd_idx]))
            if name_idx + 1 < len(tds) and header[name_idx + 1].lower() == "song name":
                duration = _text(tds[name_idx + 1]) or None
            for i, fmt in fmt_idx.items():
                val = _text(tds[i])
                if val:
                    sizes[fmt] = val
        else:  # header/row mismatch: fall back to the clickable cells
            cells = [_text(td) for td in tr.select("td.clickable-row")]
            duration = cells[1] if len(cells) > 1 else None
            for fmt, val in zip(formats, cells[2:]):
                if val:
                    sizes[fmt] = val
            nums = [_to_int(_text(td)) for td in tds if re.fullmatch(r"\d+\.", _text(td))]
            number = nums[0] if nums else None
        tracks.append(
            TrackRow(
                number=number,
                disc=disc,
                title=_text(link),
                page_url=absolute(link["href"]),
                duration=duration,
                sizes=sizes,
            )
        )
    return formats, tracks


# --------------------------------------------------------------------------
# track page
# --------------------------------------------------------------------------

_DL_TEXT = re.compile(r"download as\s+([A-Za-z0-9]+)", re.I)


def parse_track_page(html: str) -> list[TrackFile]:
    """Return the file links the track page offers ("Click here to download as MP3")."""
    soup = _soup(html)
    content = soup.select_one("#pageContent") or soup
    files: list[TrackFile] = []
    for span in content.select("span.songDownloadLink"):
        a = span.find_parent("a", href=True)
        m = _DL_TEXT.search(_text(span))
        if a is None or m is None:
            continue
        size = None
        tail = a.next_sibling
        if isinstance(tail, str):
            sm = re.search(r"\(([\d.,]+\s*[KMG]?B)\)", tail)
            size = sm.group(1) if sm else None
        files.append(TrackFile(format=m.group(1).upper(), url=a["href"], size_text=size))
    if not files and content.find("audio") is None:
        raise LayoutChangedError("track page has no download links and no audio element")
    return files


# --------------------------------------------------------------------------
# account / login
# --------------------------------------------------------------------------

_ALBUM_HREF = re.compile(r"^(?:https://downloads\.khinsider\.com)?/game-soundtracks/album/([^/?#]+)/?$")


def parse_account_albums(html: str, page_url: str) -> ResultPage:
    """Albums listed on an account page (/cp/downloads, /cp/history).

    The logged-in layout was not available when this was written, so this is
    deliberately generic. If the page has a standard ``table.albumList``, that
    parser is used. Otherwise every album link inside ``#pageContent`` is
    collected (track links and the left navigation are ignored).
    """
    soup = _soup(html)
    content = soup.select_one("#pageContent")
    if content is None:
        raise LayoutChangedError("account page has no #pageContent")
    if content.select_one("table.albumList"):
        return parse_result_page(html, page_url)
    albums: list[AlbumSummary] = []
    seen: set[str] = set()
    for a in content.find_all("a", href=True):
        m = _ALBUM_HREF.match(a["href"])
        if not m or m.group(1) in seen:
            continue
        slug = m.group(1)
        title = _text(a)
        if not title:
            img = a.find("img")
            title = (img.get("alt") or "").strip() if img else ""
        if not title:
            # image-only link: use a later text link to the same album if there is one
            continue
        seen.add(slug)
        albums.append(AlbumSummary(slug=slug, url=absolute(a["href"]), title=title))
    found = _FOUND_RE.search(_text(content))
    next_url, last = _pagination(content, page_url)
    return ResultPage(albums=albums, total_reported=int(found.group(1).replace(",", "")) if found else None,
                      next_page_url=next_url, last_page=last)

def login_state(html: str) -> bool | None:
    """True if logged in, False if a 'Log In' link is shown, None if unknown."""
    soup = _soup(html)
    nav = soup.select_one("#leftColumn") or soup
    if nav.find("a", href=re.compile(r"logout", re.I)):
        return True
    if nav.find("a", href=re.compile(r"^/forums/(login|index\.php\?login)")):
        return False
    return None


def parse_login_form(html: str) -> dict:
    soup = _soup(html)
    form = None
    for f in soup.find_all("form"):
        if f.find("input", attrs={"name": "login"}) and f.find("input", attrs={"name": "password"}):
            form = f
            break
    if form is None:
        raise LayoutChangedError("login form with login/password fields not found")
    lower = str(form).lower() + html.lower()[:200000]
    hidden = {i["name"]: i.get("value", "") for i in form.find_all("input", attrs={"type": "hidden"}) if i.get("name")}
    return {
        "action": absolute(form.get("action", "/forums/index.php?login/login")),
        "has_remember": form.find("input", attrs={"name": "remember"}) is not None,
        "captcha": any(m in lower for m in _CAPTCHA_MARKERS),
        "hidden": hidden,  # _xfToken, _xfRedirect: must be posted back with the form
    }


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

_UNITS = {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3}


def size_to_bytes(text: str | None) -> int | None:
    """'6.27 MB' -> approx bytes. The site rounds, so this is an estimate."""
    if not text:
        return None
    m = re.search(r"([\d.,]+)\s*([KMG]?B)\b", text, re.I)
    if not m:
        return None
    return int(float(m.group(1).replace(",", "")) * _UNITS[m.group(2).upper()])


def sizes_by_format(total_size_text: str | None) -> dict[str, str]:
    """Split 'Total Filesize' text into per-format sizes when labelled.

    '30 MB' -> {'': '30 MB'}; '120 MB (MP3) / 500 MB (FLAC)' -> {'MP3': ..., 'FLAC': ...}
    """
    if not total_size_text:
        return {}
    labelled = re.findall(r"([\d.,]+\s*[KMG]?B)\s*\(([A-Za-z0-9]+)\)", total_size_text)
    if labelled:
        return {fmt.upper(): size for size, fmt in labelled}
    return {"": total_size_text}
