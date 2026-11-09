"""High-level khinsider operations: fetch and parse, with no persistence."""

from __future__ import annotations

import logging

from .fetch import FetchError, Fetcher
from .models import AlbumDetail, AlbumSummary, ResultPage, TrackFile
from .parse import login_state, parse_account_albums, parse_album, parse_result_page, parse_track_page
from .urls import BASE_URL, album_url, browse_url, search_url

log = logging.getLogger(__name__)

# "My Profile" lists downloads; "My History" lists recently viewed albums.
ACCOUNT_PAGES = {"downloads": "/cp/downloads", "history": "/cp/history"}


class NotLoggedIn(FetchError):
    pass


class KhinsiderClient:
    def __init__(self, fetcher: Fetcher):
        self.fetcher = fetcher

    async def search(self, text: str, *, sort: str | None = None, album_type_id: int | None = None,
                     year: int | None = None, category_id: int | None = None) -> tuple[str, ResultPage]:
        """One /search request. The site returns all matches on a single page."""
        url = search_url(text, sort=sort, album_type_id=album_type_id, year=year, category_id=category_id)
        html = await self.fetcher.get_html(url)
        return url, parse_result_page(html, url)

    async def browse_platform(self, slug: str, max_pages: int) -> tuple[list[AlbumSummary], int | None, int, bool]:
        """All albums for a platform, following ?page=N up to ``max_pages``.

        Returns (albums, total_reported, pages_fetched, complete).
        """
        albums: list[AlbumSummary] = []
        total = None
        page = 1
        while True:
            url = browse_url(slug, page)
            html = await self.fetcher.get_html(url)
            rp = parse_result_page(html, url)
            total = rp.total_reported if total is None else total
            albums.extend(rp.albums)
            if not rp.next_page_url:
                return albums, total, page, True
            if page >= max_pages:
                log.warning("browse %s stopped at page cap %d (of %s)", slug, max_pages, rp.last_page)
                return albums, total, page, False
            page += 1

    async def album(self, slug: str) -> AlbumDetail:
        url = album_url(slug)
        return parse_album(await self.fetcher.get_html(url), url)

    async def track_files(self, track_page_url: str) -> list[TrackFile]:
        return parse_track_page(await self.fetcher.get_html(track_page_url))

    async def account_albums(self, path: str, max_pages: int = 50) -> list[AlbumSummary]:
        """Albums on an account page such as /cp/downloads. Requires a logged-in session."""
        if path not in ACCOUNT_PAGES.values():
            raise ValueError(f"unsupported account page: {path}")
        url = BASE_URL + path
        out: list[AlbumSummary] = []
        seen: set[str] = set()
        for _ in range(max_pages):
            html = await self.fetcher.get_html(url)
            if login_state(html) is False:
                raise NotLoggedIn("khinsider shows you as logged out. Log in first (Settings → Log in now, "
                                  "or `python main.py browser login`).")
            rp = parse_account_albums(html, url)
            for a in rp.albums:
                if a.slug not in seen:
                    seen.add(a.slug)
                    out.append(a)
            if not rp.next_page_url or rp.next_page_url == url:
                break
            url = rp.next_page_url
        return out
