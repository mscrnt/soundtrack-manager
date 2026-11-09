import pytest
from bs4 import BeautifulSoup

from soundtrack_manager.khinsider import catalog, urls
from soundtrack_manager.khinsider.parse import parse_search_filters

from .conftest import fixture_html


def test_platform_table_matches_live_fixtures():
    ids = {name: int(v) for v, name in parse_search_filters(fixture_html("search_resident_evil.html"))["album_category"]}
    soup = BeautifulSoup(fixture_html("console_list.html"), "lxml")
    slugs = {a.get_text(strip=True): a["href"].rsplit("/", 1)[1]
             for a in soup.select("#pageContent a[href^='/game-soundtracks/']")}
    table = {p.name: (p.slug, p.category_id) for p in catalog.PLATFORMS}
    assert table == {n: (slugs[n], ids[n]) for n in ids}


@pytest.mark.parametrize("value,name", [
    ("ps1", "PS1"), ("PSX", "PS1"), ("playstation", "PS1"), ("snes", "SNES"), ("n64", "N64"),
    ("pc", "Windows"), ("genesis", "Genesis / Mega Drive"), ("Xbox Series X/S", "Xbox Series X/S"),
    ("9", "PS1"), ("nintendo-64", "N64"), ("switch 2", "Switch 2"),
])
def test_find_platform(value, name):
    assert catalog.find_platform(value).name == name


def test_unknown_platform():
    assert catalog.find_platform("atari 2600 deluxe") is None


@pytest.mark.parametrize("value,tid", [
    ("soundtrack", 1), ("Soundtracks", 1), ("ost", 1), ("gamerip", 2), ("remixes", 4), ("remix", 4),
    ("arrangement", 5), ("inspired by", 7), ("6", 6),
])
def test_find_album_type(value, tid):
    assert catalog.find_album_type(value).id == tid


def test_matches_type_singular_plural():
    remix = catalog.find_album_type("remix")
    assert catalog.matches_type("Remix", remix)
    assert catalog.matches_type("Remixes", remix)
    assert not catalog.matches_type("Arrangement", remix)
    assert not catalog.matches_type(None, remix)


def test_search_url_uses_site_parameters():
    u = urls.search_url("final fantasy", sort="year", album_type_id=1, year=1997, category_id=9)
    assert u == ("https://downloads.khinsider.com/search?search=final+fantasy&type=album&sort=year"
                 "&album_type=1&album_year=1997&album_category=9")
    assert urls.search_url("x").endswith("album_type=&album_year=&album_category=")


def test_browse_url():
    assert urls.browse_url("playstation") == "https://downloads.khinsider.com/game-soundtracks/playstation"
    assert urls.browse_url("playstation", 3).endswith("/playstation?page=3&orderby=name&orderdirection=ASC")
    with pytest.raises(ValueError):
        urls.browse_url("../etc")


@pytest.mark.parametrize("ref", [
    "resident-evil-save-room-compilation-2023",
    "https://downloads.khinsider.com/game-soundtracks/album/resident-evil-save-room-compilation-2023",
    "/game-soundtracks/album/resident-evil-save-room-compilation-2023/01%2520R.E.mp3",
])
def test_album_slug(ref):
    assert urls.album_slug(ref) == "resident-evil-save-room-compilation-2023"


@pytest.mark.parametrize("bad", ["https://evil.example/game-soundtracks/album/x", "../../etc/passwd",
                                 "https://downloads.khinsider.com/search?x=1", "a b"])
def test_album_slug_rejects(bad):
    with pytest.raises(ValueError):
        urls.album_slug(bad)


def test_url_host_checks():
    assert urls.is_site_url("https://downloads.khinsider.com/x")
    assert not urls.is_site_url("http://downloads.khinsider.com/x")
    assert not urls.is_site_url("https://downloads.khinsider.com.evil.com/x")
    assert urls.is_cdn_url("https://jetta.vgmtreasurechest.com/a.mp3")
    assert not urls.is_cdn_url("https://vgmtreasurechest.com.evil.com/a.mp3")


def test_filename_from_url():
    assert urls.filename_from_url(
        "https://vgmtreasurechest.com/soundtracks/x/pmseqpfe/01%20R.E.%201%20Save%20Room-%20Safe%20Haven.mp3"
    ) == "01 R.E. 1 Save Room- Safe Haven.mp3"
