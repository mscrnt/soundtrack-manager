import pytest

from soundtrack_manager.khinsider import parse
from soundtrack_manager.khinsider.parse import LayoutChangedError

from .conftest import fixture_html

ALBUM_URL = "https://downloads.khinsider.com/game-soundtracks/album/resident-evil-save-room-compilation-2023"


class TestResultPages:
    def test_search_results(self):
        rp = parse.parse_result_page(fixture_html("search_resident_evil.html"),
                                     "https://downloads.khinsider.com/search?search=resident+evil")
        assert rp.total_reported == 10
        assert len(rp.albums) == 10
        assert rp.next_page_url is None  # search returns everything on one page
        a = rp.albums[0]
        assert a.slug == "resident-evil-save-room-compilation-2023"
        assert a.url == ALBUM_URL
        assert a.title == "Resident Evil: Save Room Compilation"
        assert a.platforms == ["GC", "N64", "PS1", "PS2"]
        assert a.album_type == "Remix"
        assert a.year == 2023
        assert a.catalog_number is None
        assert a.thumb_url.startswith("https://jetta.vgmtreasurechest.com/")

    def test_catalog_number_from_bracket_span(self):
        rp = parse.parse_result_page(fixture_html("search_resident_evil.html"))
        assert rp.albums[1].catalog_number == "CPDA-10106"

    def test_platform_with_slash_is_one_platform(self):
        rp = parse.parse_result_page(fixture_html("search_resident_evil.html"))
        badass = next(a for a in rp.albums if a.slug.startswith("badass-boss-themes-volume-iii"))
        assert "Genesis / Mega Drive" in badass.platforms
        assert len(badass.platforms) == 12

    def test_large_search_reports_total(self):
        rp = parse.parse_result_page(fixture_html("search_final_fantasy_trimmed.html"))
        assert rp.total_reported == 533
        assert len(rp.albums) == 6  # fixture trimmed to 6 rows

    def test_browse_pagination(self):
        url = "https://downloads.khinsider.com/game-soundtracks/playstation"
        rp = parse.parse_result_page(fixture_html("browse_ps1_trimmed.html"), url)
        assert rp.total_reported == 4296
        assert rp.last_page == 9
        assert rp.next_page_url == url + "?page=2&orderby=name&orderdirection=ASC"
        assert rp.albums[0].platforms == ["PS1"]
        assert rp.albums[0].album_type == "Gamerip"

    def test_browse_next_page_from_middle(self):
        url = "https://downloads.khinsider.com/game-soundtracks/playstation?page=2&orderby=name&orderdirection=ASC"
        rp = parse.parse_result_page(fixture_html("browse_ps1_trimmed.html"), url)
        assert "page=3" in rp.next_page_url

    def test_empty_search(self):
        rp = parse.parse_result_page(fixture_html("search_empty.html"))
        assert rp.total_reported == 0 and rp.albums == []

    def test_layout_change_is_reported(self):
        with pytest.raises(LayoutChangedError):
            parse.parse_result_page("<html><body><div id='pageContent'><p>Hello</p></div></body></html>")

    def test_header_driven_columns_survive_reordering(self):
        html = """<div id="pageContent"><p>Found 1 matching albums.</p><table class="albumList">
        <tr><th>Year</th><th>Type</th><th>Album</th><th>Platform</th></tr>
        <tr><td>1997</td><td>Soundtrack</td><td><a href="/game-soundtracks/album/x-ost">X OST</a></td>
        <td><a href="/game-soundtracks/playstation">PS1</a></td></tr></table></div>"""
        a = parse.parse_result_page(html).albums[0]
        assert (a.title, a.year, a.album_type, a.platforms) == ("X OST", 1997, "Soundtrack", ["PS1"])

    def test_search_filter_options(self):
        f = parse.parse_search_filters(fixture_html("search_resident_evil.html"))
        assert ("9", "PS1") in f["album_category"]  # mislabelled select is recognised
        assert ("1998", "1998") in f["album_year"]
        assert ("1", "Soundtracks") in f["album_type"]
        assert [k for k, _ in f["sort"]] == ["name", "timestamp", "popularity", "year", "relevance"]


class TestAlbumPage:
    def test_metadata(self):
        a = parse.parse_album(fixture_html("album_save_room.html"), ALBUM_URL)
        assert a.title == "Resident Evil: Save Room Compilation"
        assert a.site_album_id == 61496
        assert a.platforms == ["GC", "N64", "PS1", "PS2"]
        assert a.year == 2023
        assert a.publisher == "Bronson Bragg"
        assert a.developer is None  # not on the page, not invented
        assert a.file_count == 7
        assert a.total_size_text == "30 MB"
        assert a.date_added == "Mar 18th, 2024"
        assert a.album_type == "Remix"
        assert a.formats == ["MP3"]
        assert a.description.startswith("Bronson Bragg's renditions")
        assert len(a.cover_urls) == 2 and a.cover_urls[0].endswith("/cover.png")
        assert a.raw_metadata["Uploaded by"] == "Solex Gruthor"

    def test_tracks(self):
        a = parse.parse_album(fixture_html("album_save_room.html"), ALBUM_URL)
        assert a.track_count == 7
        t = a.tracks[0]
        assert (t.number, t.title, t.duration, t.sizes) == (1, "R.E. 1 Save Room- Safe Haven", "2:43", {"MP3": "6.27 MB"})
        assert t.page_url.startswith(ALBUM_URL + "/01%2520R.E.")
        assert a.tracks[-1].number == 7 and a.tracks[-1].title == "R.E. 1 Basement Theme- Director's Cut"

    def test_flac_and_cd_columns(self):
        # Synthetic variant of the real table with CD and FLAC columns (multi-disc / lossless albums).
        html = fixture_html("album_save_room.html")
        def sub(old, new):
            nonlocal html
            assert old in html, old
            html = html.replace(old, new, 1)

        sub('<th align="right" width="60px"><b>MP3</b></th>',
            '<th align="right" width="60px"><b>MP3</b></th><th align="right" width="60px"><b>FLAC</b></th>')
        sub('<th align="center" width="25px"><b>#</b></th>',
            '<th align="center"><b>CD</b></th><th align="center" width="25px"><b>#</b></th>')
        sub('<td align="right" style="padding-right: 8px;">1.</td>',
            '<td align="center">2</td><td align="right" style="padding-right: 8px;">1.</td>')
        sub('style="font-weight:normal;">6.27 MB</a></td>',
            'style="font-weight:normal;">6.27 MB</a></td><td class="clickable-row" align="right"><a href="#">31.10 MB</a></td>')
        a = parse.parse_album(html, ALBUM_URL)
        assert a.formats == ["MP3", "FLAC"]
        t = a.tracks[0]
        assert t.disc == 2 and t.number == 1
        assert t.sizes == {"MP3": "6.27 MB", "FLAC": "31.10 MB"}

    def test_missing_songlist_is_layout_change(self):
        with pytest.raises(LayoutChangedError):
            parse.parse_album("<div id='pageContent'><h2>X</h2></div>", ALBUM_URL)


class TestTrackPage:
    def test_download_links(self):
        files = parse.parse_track_page(fixture_html("track_save_room_01.html"))
        assert len(files) == 1
        f = files[0]
        assert f.format == "MP3"
        assert f.size_text == "6.26 MB"
        assert f.url == ("https://vgmtreasurechest.com/soundtracks/resident-evil-save-room-compilation-2023/"
                         "pmseqpfe/01%20R.E.%201%20Save%20Room-%20Safe%20Haven.mp3")


class TestAccessAndLogin:
    def test_challenge_detection(self):
        assert parse.is_challenge_page(fixture_html("cloudflare_challenge.html"))
        for name in ("album_save_room.html", "search_resident_evil.html", "track_save_room_01.html"):
            assert not parse.is_challenge_page(fixture_html(name))

    def test_logged_out_state(self):
        assert parse.login_state(fixture_html("home_logged_out_trimmed.html")) is False

    def test_logged_in_state(self):
        assert parse.login_state('<div id="leftColumn"><a href="/forums/index.php?logout/&t=x">Log Out</a></div>') is True

    def test_login_form(self):
        f = parse.parse_login_form(fixture_html("login.html"))
        assert f["action"] == "https://downloads.khinsider.com/forums/index.php?login/login"
        assert f["has_remember"] is True
        assert f["captcha"] is False

    def test_login_form_captcha_detected(self):
        html = fixture_html("login.html").replace("</form>", '<div class="g-recaptcha" data-sitekey="x"></div></form>', 1)
        assert parse.parse_login_form(html)["captcha"] is True


@pytest.mark.parametrize("text,expected", [
    ("6.27 MB", int(6.27 * 1024 ** 2)), ("30 MB", 30 * 1024 ** 2), ("1.5 GB", int(1.5 * 1024 ** 3)),
    ("512 KB", 512 * 1024), ("1,024 KB", 1024 * 1024), (None, None), ("n/a", None),
])
def test_size_to_bytes(text, expected):
    assert parse.size_to_bytes(text) == expected


def test_sizes_by_format():
    assert parse.sizes_by_format("30 MB") == {"": "30 MB"}
    assert parse.sizes_by_format("120 MB (MP3) / 500 MB (FLAC)") == {"MP3": "120 MB", "FLAC": "500 MB"}
