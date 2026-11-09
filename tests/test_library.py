"""Library scan/verify/duplicates/missing + account history import (no network)."""

import json

import httpx
import pytest

from soundtrack_manager import queue
from soundtrack_manager.khinsider.client import KhinsiderClient, NotLoggedIn
from soundtrack_manager.khinsider.parse import parse_account_albums
from soundtrack_manager.library import Library, norm_title

from .conftest import FakeFetcher, fixture_html
from .test_downloader import FakeCDN, album_dir, make_ctx, queue_album, run

pytestmark = pytest.mark.anyio

LOGGED_IN_NAV = '<div id="leftColumn"><a href="/forums/index.php?logout/&t=1">Log Out</a></div>'


def account_page(slugs_titles, next_page=None):
    links = "".join(f'<tr><td><a href="/game-soundtracks/album/{s}"><img alt=""></a></td>'
                    f'<td><a href="/game-soundtracks/album/{s}">{t}</a></td>'
                    f'<td><a href="/game-soundtracks/album/{s}/01%2520x.mp3">track</a></td></tr>'
                    for s, t in slugs_titles)
    nxt = f'<a href="?page={next_page}">Next</a>' if next_page else ""
    return f"<html><body>{LOGGED_IN_NAV}<div id='pageContent'><h2>My Downloads</h2><table>{links}</table>{nxt}</div></body></html>"


def test_norm_title():
    assert norm_title("Resident Evil: Save Room Compilation") == norm_title("Resident Evil_ Save Room Compilation (2023)")
    assert norm_title("FINAL FANTASY VII [1997]") == "finalfantasyvii"


def test_parse_account_albums_generic():
    rp = parse_account_albums(account_page([("a-1", "Album One"), ("b-2", "Album Two")], next_page=2),
                              "https://downloads.khinsider.com/cp/downloads")
    assert [(a.slug, a.title) for a in rp.albums] == [("a-1", "Album One"), ("b-2", "Album Two")]
    assert rp.next_page_url == "https://downloads.khinsider.com/cp/downloads?page=2"


async def test_account_albums_requires_login():
    f = FakeFetcher([("/cp/downloads", "home_logged_out_trimmed.html")])
    with pytest.raises(NotLoggedIn):
        await KhinsiderClient(f).account_albums("/cp/downloads")


async def test_scan_downloaded_album_ok_then_damaged(settings):
    ctx = make_ctx(settings)
    await queue_album(ctx)
    await run(ctx, FakeCDN())
    lib = Library(ctx.db)
    s = lib.scan(settings.download_dir)
    assert s.folders == 1 and s.matched == 1 and s.incomplete == 0
    rec = lib.albums()[0]
    assert rec["status"] == "ok" and rec["match_method"] == "album_info" and rec["file_count"] == 7
    assert rec["slug"] == "resident-evil-save-room-compilation-2023"

    d = album_dir(settings)
    (d / "02 R.E. 2 Save Room- Secure Place.mp3").unlink()
    with (d / "03 R.E. 3 Save Room- Free From Fear.mp3").open("ab") as f:
        f.write(b"garbage")
    lib.scan(settings.download_dir)
    rec = lib.albums()[0]
    assert rec["status"] == "incomplete" and rec["missing_count"] == 1 and rec["mismatch_count"] == 1
    miss = lib.missing()
    assert len(miss) == 1 and "1 missing / 1 damaged" in miss[0]["reason"]

    # recovery: re-queue; worker keeps good files and re-fetches the rest
    r = lib.recover([miss[0]["id"]], "mp3")
    assert r.added == [miss[0]["id"]]
    (d / "03 R.E. 3 Save Room- Free From Fear.mp3").unlink()  # user removed the damaged copy
    await run(ctx, FakeCDN())
    lib.scan(settings.download_dir, deep=True)
    assert lib.albums()[0]["status"] == "ok"
    assert lib.missing() == []


async def test_failed_and_conflict_tracks_reflected_in_scan(settings):
    ctx = make_ctx(settings)
    await queue_album(ctx)
    d = album_dir(settings)
    d.mkdir(parents=True)
    (d / "02 R.E. 2 Save Room- Secure Place.mp3").write_bytes(b"user copy")  # conflict
    await run(ctx, FakeCDN(status_for={"05 R.E. 0": 404}))                   # track 5 fails
    lib = Library(ctx.db)
    lib.scan(settings.download_dir)
    rec = lib.albums()[0]
    assert rec["status"] == "incomplete" and rec["missing_count"] == 1 and rec["mismatch_count"] == 0
    st = {f["rel_path"]: f["status"] for f in lib.files(rec["id"])}
    assert st["05 R.E. 0 Save Room.mp3"] == "missing"
    assert st["02 R.E. 2 Save Room- Secure Place.mp3"] == "unverified"
    assert [m["reason"] for m in lib.missing()] == ["1 missing / 0 damaged file(s) in GC/Resident Evil_ Save Room Compilation"]


async def test_deep_verify_detects_bitrot(settings):
    ctx = make_ctx(settings)
    await queue_album(ctx)
    await run(ctx, FakeCDN())
    p = album_dir(settings) / "01 R.E. 1 Save Room- Safe Haven.mp3"
    data = bytearray(p.read_bytes())
    data[100] ^= 0xFF  # same size, different content
    p.write_bytes(bytes(data))
    lib = Library(ctx.db)
    lib.scan(settings.download_dir)
    assert lib.albums()[0]["status"] == "ok"  # quick scan checks sizes only
    lib.scan(settings.download_dir, deep=True)
    rec = lib.albums()[0]
    assert rec["status"] == "incomplete" and rec["mismatch_count"] == 1
    assert {f["status"] for f in lib.files(rec["id"])} == {"ok", "hash_mismatch"}


def test_scan_matches_by_title_merges_discs_and_finds_duplicates(ctx, tmp_path):
    with ctx.db.connect() as c:
        c.execute("INSERT INTO albums(slug, url, title, year) VALUES ('ff7', 'https://x/ff7', 'Final Fantasy VII Original Soundtrack', 1997)")
    root = tmp_path / "music"
    a = root / "PS1" / "Final Fantasy VII Original Soundtrack (1997)"
    (a / "Disc 1").mkdir(parents=True)
    (a / "Disc 2").mkdir()
    (a / "Disc 1" / "01.flac").write_bytes(b"x" * 10)
    (a / "Disc 2" / "01.flac").write_bytes(b"y" * 10)
    b = root / "Backup" / "Final Fantasy VII Original Soundtrack"
    b.mkdir(parents=True)
    (b / "01.flac").write_bytes(b"x" * 10)
    u = root / "Some Unknown Album"
    u.mkdir()
    (u / "track.mp3").write_bytes(b"z")
    lib = Library(ctx.db)
    s = lib.scan(root, deep=True)
    assert s.folders == 3 and s.matched == 2 and s.unmatched == 1
    recs = {r["rel_path"]: r for r in lib.albums()}
    assert recs["PS1/Final Fantasy VII Original Soundtrack (1997)"]["file_count"] == 2  # discs merged
    assert recs["PS1/Final Fantasy VII Original Soundtrack (1997)"]["status"] == "unverified"
    assert recs["Some Unknown Album"]["status"] == "unmatched"
    kinds = {g["kind"] for g in lib.duplicates()}
    assert kinds == {"same album", "identical file"}


def test_scan_never_modifies_files_and_forgets_removed_folders(ctx, tmp_path):
    root = tmp_path / "m"
    (root / "A").mkdir(parents=True)
    f = root / "A" / "1.mp3"
    f.write_bytes(b"abc")
    lib = Library(ctx.db)
    lib.scan(root)
    before = (f.stat().st_mtime, f.read_bytes())
    lib.scan(root, deep=True)
    assert (f.stat().st_mtime, f.read_bytes()) == before
    f.unlink()
    (root / "A").rmdir()
    assert lib.scan(root).removed_records == 1 and lib.albums() == []


async def test_history_sync_and_missing(settings):
    pages = {
        "/cp/downloads?page=2": account_page([("c-3", "Album Three")]),
        "/cp/downloads": account_page([("a-1", "Album One"), ("b-2", "Album Two")], next_page=2),
        "/cp/history": account_page([("a-1", "Album One")]),
    }
    f = FakeFetcher([(k, (lambda html: lambda url: html)(v)) for k, v in pages.items()])
    from soundtrack_manager.runtime import AppContext
    ctx = AppContext(settings, fetcher=f, configure_logging=False)
    lib = Library(ctx.db)
    counts = await lib.sync_history(ctx.client)
    assert counts == {"downloads": 3, "history": 1}
    root = settings.download_dir
    (root / "Album One").mkdir(parents=True)
    (root / "Album One" / "01.mp3").write_bytes(b"1")
    lib.scan(root)
    missing = {m["slug"] for m in lib.missing()}
    assert missing == {"b-2", "c-3"}
    assert ctx.db.get_sync_state("history_sync") is not None
    # recovery queue avoids duplicates
    ids = [m["id"] for m in lib.missing()]
    assert len(lib.recover(ids, "flac").added) == 2
    assert lib.recover(ids, "flac").already_queued == ids
