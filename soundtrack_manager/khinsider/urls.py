"""URL builders and validators for khinsider.

Search parameters are the ones the site's own search form puts in its
``onchange`` URLs. The platform ``<select>`` on the site is mislabelled
``name="album_year"``, so we build URLs ourselves instead of submitting the form.
"""

from __future__ import annotations

import re
from urllib.parse import unquote, urlencode, urljoin, urlsplit

BASE_URL = "https://downloads.khinsider.com"
SITE_HOST = "downloads.khinsider.com"
# Audio/artwork CDN hosts seen on album and track pages (e.g. jetta., nu., lambda.)
CDN_ROOT_DOMAIN = "vgmtreasurechest.com"

_ALBUM_PATH = re.compile(r"^/game-soundtracks/album/([^/?#]+)/?$")
_TRACK_PATH = re.compile(r"^/game-soundtracks/album/([^/?#]+)/([^?#]+)$")


def search_url(
    text: str = "",
    *,
    sort: str | None = None,
    album_type_id: int | None = None,
    year: int | None = None,
    category_id: int | None = None,
) -> str:
    params = {
        "search": text,
        "type": "album",
        "sort": sort or "relevance",
        "album_type": album_type_id or "",
        "album_year": year or "",
        "album_category": category_id or "",
    }
    return f"{BASE_URL}/search?{urlencode(params)}"


def browse_url(slug: str, page: int = 1) -> str:
    if not re.fullmatch(r"[a-z0-9-]+", slug):
        raise ValueError(f"invalid browse slug: {slug!r}")
    url = f"{BASE_URL}/game-soundtracks/{slug}"
    if page > 1:
        url += "?" + urlencode({"page": page, "orderby": "name", "orderdirection": "ASC"})
    return url


def album_url(slug: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9._~%-]+", slug):
        raise ValueError(f"invalid album slug: {slug!r}")
    return f"{BASE_URL}/game-soundtracks/album/{slug}"


def absolute(href: str, base: str = BASE_URL + "/") -> str:
    """Resolve ``href`` against ``base`` (a page URL or the site root)."""
    return urljoin(base, href)


def album_slug(url_or_slug: str) -> str:
    """Return the album slug from a full URL, a site-relative path, or a bare slug."""
    s = url_or_slug.strip()
    if "/" not in s:
        album_url(s)  # validates
        return s
    parts = urlsplit(s)
    if parts.netloc and parts.netloc.lower() != SITE_HOST:
        raise ValueError(f"not a khinsider URL: {s!r}")
    m = _ALBUM_PATH.match(parts.path) or _TRACK_PATH.match(parts.path)
    if not m:
        raise ValueError(f"not a khinsider album URL: {s!r}")
    return m.group(1)


def is_site_url(url: str) -> bool:
    p = urlsplit(url)
    return p.scheme == "https" and p.netloc.lower() == SITE_HOST


def is_cdn_url(url: str) -> bool:
    p = urlsplit(url)
    host = p.netloc.lower()
    return p.scheme in ("https", "http") and (
        host == CDN_ROOT_DOMAIN or host.endswith("." + CDN_ROOT_DOMAIN)
    )


def filename_from_url(url: str) -> str:
    """Original file name from a CDN URL, percent-decoded ("01 Title.mp3")."""
    name = unquote(urlsplit(url).path.rsplit("/", 1)[-1])
    return name
