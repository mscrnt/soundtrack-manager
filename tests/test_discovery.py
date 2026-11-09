import json

import pytest

from soundtrack_manager.discovery import QueryError, SearchQuery, parse_year_range
from soundtrack_manager.khinsider.parse import parse_album
from soundtrack_manager.discovery import store_album_detail

from .conftest import FakeFetcher, fixture_html

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize("value,expected", [
    (None, (None, None)), ("", (None, None)), ("1998", (1998, 1998)), ("1996-2000", (1996, 2000)),
    ("2000-1996", (1996, 2000)), ("1996-", (1996, None)), ("-2000", (None, 2000)), ("1996..2000", (1996, 2000)),
])
def test_parse_year_range(value, expected):
    assert parse_year_range(value) == expected


@pytest.mark.parametrize("bad", ["abc", "1800", "19x8"])
def test_parse_year_range_rejects(bad):
    with pytest.raises(QueryError):
        parse_year_range(bad)


def test_query_requires_text_or_platform():
    with pytest.raises(QueryError):
        SearchQuery.build("", [], "soundtrack", "1998")
    with pytest.raises(QueryError):
        SearchQuery.build("x", ["not-a-console"])
    with pytest.raises(QueryError):
        SearchQuery.build("x", sort="bogus")


async def test_search_uses_site_filters_and_caches(ctx, fake_fetcher):
    q = SearchQuery.build("resident evil")
    r1 = await ctx.discovery.discover(q)
    assert r1.total == 10 and not r1.from_cache
    assert len(fake_fetcher.requests) == 1
    r2 = await ctx.discovery.discover(q)
    assert r2.from_cache and r2.total == 10
    assert len(fake_fetcher.requests) == 1  # served from SQLite
    await ctx.discovery.discover(q, refresh=True)
    assert len(fake_fetcher.requests) == 2


async def test_single_platform_goes_to_site(ctx, fake_fetcher):
    q = SearchQuery.build("resident evil", ["ps1"], "soundtrack", None, "year")
    r = await ctx.discovery.discover(q)
    assert "album_category=9" in fake_fetcher.requests[0]
    assert "album_type=1" in fake_fetcher.requests[0]
    assert r.total == 0


async def test_year_range_filtered_locally(ctx, fake_fetcher):
    q = SearchQuery.build("resident evil", year="2005-2016")
    r = await ctx.discovery.discover(q)
    assert "album_year=&" in fake_fetcher.requests[0]  # range not sent to site
    assert r.total > 0
    assert all(2005 <= a["year"] <= 2016 for a in r.albums)
    assert any("range" in n for n in r.notes)


async def test_multiple_platforms_matched_locally(ctx):
    q = SearchQuery.build("resident evil", ["PS4", "N64"])
    r = await ctx.discovery.discover(q)
    assert r.total > 0
    assert all({"PS4", "N64"} & set(a["platforms"]) for a in r.albums)


async def test_type_filter_when_site_not_asked(ctx):
    # two platforms -> no site category; type is sent to site, so local filter is skipped for type
    q = SearchQuery.build("resident evil", ["PS2", "GC"], "arrangement")
    r = await ctx.discovery.discover(q)
    # site fixture ignores the param (same page served), so all rows come back; ensure no crash and platforms filtered
    assert all({"PS2", "GC"} & set(a["platforms"]) for a in r.albums)


async def test_local_sort_by_name_and_year(ctx):
    r = await ctx.discovery.discover(SearchQuery.build("resident evil", sort="name"))
    titles = [a["title"].casefold() for a in r.albums]
    assert titles == sorted(titles)


async def test_browse_platform_follows_pages_and_caps(ctx, fake_fetcher):
    ctx.discovery.max_browse_pages = 3
    r = await ctx.discovery.discover(SearchQuery.build("", ["ps1"], year="1998"))
    browse = [u for u in fake_fetcher.requests if "/game-soundtracks/playstation" in u]
    assert len(browse) == 3  # capped
    assert any("cap" in n for n in r.notes)
    assert all(a["year"] == 1998 for a in r.albums)
    # same albums on every (fake) page are stored once
    assert len({a["slug"] for a in r.albums}) == len(r.albums)


async def test_pagination(ctx):
    r = await ctx.discovery.discover(SearchQuery.build("resident evil"), page=2, per_page=4)
    assert r.page_count == 3 and len(r.page_albums) == 4
    r = await ctx.discovery.discover(SearchQuery.build("resident evil"), page=99, per_page=4)
    assert r.page == 3 and len(r.page_albums) == 2


async def test_history_recorded(ctx):
    await ctx.discovery.discover(SearchQuery.build("final fantasy", ["ps1"]))
    h = ctx.discovery.history()
    assert len(h) == 1
    assert json.loads(h[0]["query_json"])["platforms"] == ["PS1"]


async def test_album_detail_cached_and_summary_not_nulled(ctx, fake_fetcher):
    await ctx.discovery.discover(SearchQuery.build("resident evil"))
    slug = "resident-evil-save-room-compilation-2023"
    a = await ctx.discovery.album_detail(slug)
    assert a["track_count"] == 7 and a["formats"] == ["MP3"]
    assert a["thumb_url"]  # kept from the search row even though the album page has no thumb field
    assert len(a["tracks"]) == 7
    n = len(fake_fetcher.requests)
    await ctx.discovery.album_detail(slug)
    assert len(fake_fetcher.requests) == n  # cached


def test_track_ids_stable_across_refresh(ctx):
    url = "https://downloads.khinsider.com/game-soundtracks/album/resident-evil-save-room-compilation-2023"
    d = parse_album(fixture_html("album_save_room.html"), url)
    aid = store_album_detail(ctx.db, d)
    ids1 = [t["id"] for t in ctx.discovery.get_album(aid)["tracks"]]
    store_album_detail(ctx.db, d)
    ids2 = [t["id"] for t in ctx.discovery.get_album(aid)["tracks"]]
    assert ids1 == ids2


async def test_access_blocked_propagates(settings):
    from soundtrack_manager.khinsider.fetch import AccessBlocked
    from soundtrack_manager.runtime import AppContext
    ctx = AppContext(settings, fetcher=FakeFetcher(blocked=True), configure_logging=False)
    with pytest.raises(AccessBlocked):
        await ctx.discovery.discover(SearchQuery.build("resident evil"))
