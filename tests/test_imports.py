"""Import lists: parsing (text + CSV), validation, scoring, matching, queueing, web, API, CLI."""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from soundtrack_manager import queue
from soundtrack_manager.imports import (ImportFormatError, decide, parse_import, rank, score_title, search_term,
                                        to_csv)
from soundtrack_manager.web.app import create_app

ROOT = Path(__file__).parent.parent
HX = {"HX-Request": "true"}
SAVE_ROOM = "https://downloads.khinsider.com/game-soundtracks/album/resident-evil-save-room-compilation-2023"

SHOPPING = """GAME SOUNDTRACK SHOPPING LIST (FLAC)
====================================
One album per line. Download whole albums in FLAC.


PRIORITY 1 - BROKEN / DAMAGED
-----------------------------
[ ]   1. Resident Evil: Save Room Compilation (2023)  - track 3 was corrupt
           https://downloads.khinsider.com/search?search=resident+evil

PRIORITY 2 - MISSING
--------------------
[ ]   2. Castlevania (NES) Soundtrack
           https://downloads.khinsider.com/search?search=Castlevania
[x]   3. Undertale - Soundtrack (2015)
           https://downloads.khinsider.com/search?search=Undertale
[ ]   4. Wild Arms 3 - Soundtrack (2002)
[ ]   5. Exact Album
           https://downloads.khinsider.com/game-soundtracks/album/resident-evil-save-room-compilation-2023
"""


# ------------------------------------------------------------------ parsing

def test_text_list_structure():
    p = parse_import(SHOPPING.encode(), "list.txt")
    assert p.source_format == "text" and len(p.entries) == 5
    e1, e2, e3, e4, e5 = p.entries
    assert e1.title == "Resident Evil: Save Room Compilation (2023)" and e1.note == "track 3 was corrupt"
    assert e1.search == "resident evil" and e1.priority == 1 and e1.year == 2023 and e1.line == 8
    assert e2.platform == "NES" and e2.priority == 2 and e2.section.startswith("PRIORITY 2")
    assert e3.done and e3.year == 2015
    assert e4.search is None and search_term(e4) == "Wild Arms 3"   # derived from the title
    assert e5.url == SAVE_ROOM and e5.search is None
    assert any("headings or notes" in i.message for i in p.issues)  # the prose line
    assert p.summary()["by_priority"] == {"1": 1, "2": 4}


def test_real_shopping_list_shape():
    """The user's list layout: 514 entries over 4 priorities (sample of the same shape)."""
    lines = ["PRIORITY 4 - OPTIONAL", "-" * 20]
    for n in range(1, 31):
        lines += [f"[ ] {n:3d}. Game {n} - Soundtrack (199{n % 10})",
                  f"           https://downloads.khinsider.com/search?search=Game+{n}"]
    p = parse_import("\n".join(lines))
    assert len(p.entries) == 30 and all(e.priority == 4 and e.search == f"Game {i}" for i, e in
                                        enumerate(p.entries, start=1))


def test_plain_lines_without_numbers():
    p = parse_import("# my list\nChrono Cross Original Soundtrack\n\nhttps://downloads.khinsider.com/search?search=Xenogears\n")
    assert [e.title for e in p.entries] == ["Chrono Cross Original Soundtrack", "Xenogears"]
    assert p.entries[1].search == "Xenogears"


def test_csv_columns_aliases_and_errors():
    data = ("﻿Album;Platform;Year;Format;Priority;Notes;Done;Extra\n"
            "Chrono Cross OST;ps1;1999;FLAC;1;great;;x\n"
            ";;;;;;;\n"
            "Bad Format;;;wav;;;;\n"
            "Unknown Platform;Atari 9000;19x9;;high;;yes;\n"
            "Chrono Cross OST;;;;;;;\n").encode("utf-8")
    p = parse_import(data, "list.csv")
    assert p.source_format == "csv" and [e.title for e in p.entries] == ["Chrono Cross OST", "Unknown Platform", "Chrono Cross OST"]
    a, b, _ = p.entries
    assert a.platform == "PS1" and a.year == 1999 and a.format == "flac" and a.priority == 1 and a.note == "great"
    assert b.done and b.platform is None and b.year is None and b.priority is None
    msgs = [(i.level, i.message) for i in p.issues]
    assert ("warning", "ignored column 'Extra'") in msgs
    assert any(lvl == "error" and "unknown format 'wav'" in m for lvl, m in msgs)
    assert any("Atari 9000" in m for _, m in msgs) and any("invalid year" in m for _, m in msgs)
    assert any("duplicate" in m for _, m in msgs)
    assert p.summary()["errors"] == 1


def test_csv_needs_title_or_url_and_files_must_be_text():
    with pytest.raises(ImportFormatError, match="title"):
        parse_import(b"foo,bar\n1,2\n", "x.csv")
    with pytest.raises(ImportFormatError, match="binary"):
        parse_import(b"\x00\x01\x02", "x.txt")
    with pytest.raises(ImportFormatError, match="empty"):
        parse_import(b"   \n", "x.txt")
    with pytest.raises(ImportFormatError, match="larger"):
        parse_import(b"a" * (2 * 1024 * 1024 + 1), "x.txt")


def test_non_khinsider_urls_are_ignored():
    p = parse_import("[ ] 1. Game\n    https://evil.example/search?search=x\n")
    assert p.entries[0].search is None and any("non-khinsider" in i.message for i in p.issues)


def test_text_to_csv_roundtrip():
    p = parse_import(SHOPPING)
    q = parse_import(to_csv(p).encode(), "x.csv")
    key = lambda e: (e.title, e.search, e.url, e.platform, e.year, e.priority, e.section, e.note, e.done)
    assert [key(e) for e in p.entries] == [key(e) for e in q.entries]


@pytest.mark.parametrize("name", ["import-template.txt", "import-template.csv"])
def test_shipped_templates_parse_cleanly(name):
    p = parse_import((ROOT / "examples" / name).read_bytes(), name)
    assert len(p.entries) == 6 and not p.errors
    assert sum(e.done for e in p.entries) == 1 and sum(bool(e.url) for e in p.entries) == 1


# ------------------------------------------------------------------ scoring

def _rank(want, titles, **kw):
    albums = [{"id": i, "title": t, "year": kw.get("years", {}).get(t), "platforms": []} for i, t in enumerate(titles)]
    return rank({"title": want, "year": kw.get("year"), "platform": None}, albums)


def test_distinctive_words_beat_generic_ones():
    c = _rank("Dark Souls Original Soundtrack (Prepare to Die Edition)",
              ["Dark Souls Original Soundtrack", "Dark Souls: Prepare to Die Edition Special Soundtrack",
               "Dark Souls II Original Soundtrack"])
    assert decide(c)[0] == "matched" and c[0]["title"].startswith("Dark Souls: Prepare")


def test_numbers_and_roman_numerals_must_agree():
    c = _rank("Final Fantasy IX Original Soundtrack", ["Final Fantasy X Original Soundtrack",
                                                       "Final Fantasy IX Original Soundtrack"])
    assert c[0]["title"] == "Final Fantasy IX Original Soundtrack" and decide(c)[0] == "matched"
    assert score_title("Resident Evil 2", "Resident Evil 3") < 0.6


def test_year_hint_breaks_ties_and_close_calls_need_review():
    titles = ["Tomb Raider", "Tomb Raider (2013)"]
    years = {"Tomb Raider": 1996, "Tomb Raider (2013)": 2013}
    assert decide(_rank("Tomb Raider (1996) Soundtrack", titles))[0] == "ambiguous"
    c = _rank("Tomb Raider (1996) Soundtrack", titles, year=1996, years=years)
    assert decide(c) == ("matched", c[0]) and c[0]["title"] == "Tomb Raider"
    assert decide([])[0] == "not_found"


def test_small_spelling_differences_still_match():
    assert score_title("Castlevania Symphony of the Night", "Castlevania: Symphony of the Nigth OST") > 0.85


# ------------------------------------------------------------------ service

@pytest.mark.anyio
async def test_match_queue_and_review_flow(ctx, fake_fetcher):
    imp = ctx.imports
    lid = imp.create(parse_import(SHOPPING), name="t", fmt="flac")
    info = await imp.match_list(lid)
    assert info["status"] == "ready"
    rows = {e["position"]: e for e in imp.entries(lid)}
    save_room = rows[0]
    assert save_room["status"] == "matched" and save_room["match_method"] == "auto"
    assert save_room["formats"] == ["MP3"] and save_room["has_format"] == 0   # FLAC not offered
    assert rows[2]["status"] == "skipped"                                      # checked off
    assert rows[4]["status"] == "matched" and rows[4]["match_method"] == "url"
    assert rows[1]["status"] == "not_found"                                    # 'Castlevania' fixture is empty
    assert info["no_format"] == 2 and info["queueable"] == 0

    # Without FLAC nothing is queued unless asked; then it queues once (same album twice -> one job).
    assert imp.queue(lid)["skipped_no_format"] == 2
    r = imp.queue(lid, include_without_format=True)
    assert r["queued"] == 1 and r["already_queued"] == 1
    jobs = queue.list_jobs(ctx.db)
    assert len(jobs) == 1 and jobs[0]["priority"] == -1 and jobs[0]["format"] == "flac"
    assert {e["status"] for e in imp.entries(lid) if e["position"] in (0, 4)} == {"queued"}

    # Review: choose by URL, skip/restore, search again.
    nf = rows[1]["id"]
    assert (await imp.choose_url(nf, SAVE_ROOM))["match_method"] == "manual"
    assert imp.skip(nf) and imp.entry(nf)["status"] == "skipped"
    assert imp.restore(nf) and imp.entry(nf)["status"] == "matched"
    again = await imp.research(nf, "resident evil")
    assert again["search"] == "resident evil" and again["status"] in ("matched", "ambiguous", "not_found")
    assert "Save Room" in imp.export_csv(lid)
    assert imp.delete(lid) and imp.get(lid) is None and len(queue.list_jobs(ctx.db)) == 1  # jobs stay


@pytest.mark.anyio
async def test_access_blocked_stops_list_and_can_resume(ctx, fake_fetcher):
    from soundtrack_manager.khinsider.fetch import AccessBlocked
    imp = ctx.imports
    lid = imp.create(parse_import(SHOPPING), name="t", fmt="mp3")
    fake_fetcher.blocked = True
    with pytest.raises(AccessBlocked):
        await imp.match_list(lid)
    info = imp.get(lid)
    assert info["status"] == "failed" and "Access check" in info["error"] and info["counts"]["pending"] == 4
    fake_fetcher.blocked = False
    info = await imp.match_list(lid)
    assert info["status"] == "ready" and info["counts"]["pending"] == 0 and info["queueable"] == 2  # MP3 offered


def test_pause_stops_matching_between_entries(ctx):
    imp = ctx.imports
    lid = imp.create(parse_import(SHOPPING), name="t", fmt="flac")
    assert imp.pause(lid) and imp.get(lid)["status"] == "paused"
    assert ctx.imports.lists_to_resume() == []


# ------------------------------------------------------------------ web

@pytest.fixture
def client(ctx):
    with TestClient(create_app(ctx)) as c:
        yield c


def _wait_ready(client, lid):
    for _ in range(100):
        if "ready" in client.get(f"/imports/{lid}/progress").text:
            return
        time.sleep(0.05)
    raise AssertionError("matching did not finish")


def test_web_check_import_review_queue(client, ctx):
    assert "Import a list" in client.get("/imports").text
    files = {"file": ("list.txt", SHOPPING.encode(), "text/plain")}
    r = client.post("/imports/check", files=files, data={"source_format": "auto"}, headers=HX)
    assert "reads as a <strong>text list</strong>" in r.text and "5 entries" in r.text
    assert "the file is empty" in client.post("/imports/check", data={"content": ""}, headers=HX).text

    r = client.post("/imports", files=files, data={"format": "mp3", "name": "Mine"}, headers=HX)
    loc = r.headers["HX-Redirect"]
    lid = int(loc.rsplit("/", 1)[1])
    _wait_ready(client, lid)
    page = client.get(loc)
    assert page.status_code == 200 and "Mine" in page.text
    entries = client.get(f"/imports/{lid}/entries", params={"status": "review"}).text
    assert "Castlevania (NES) Soundtrack" in entries and "Resident Evil: Save Room" not in entries
    nf = next(e for e in ctx.imports.entries(lid) if e["status"] == "not_found")
    r = client.post(f"/imports/entries/{nf['id']}/choose", data={"url": SAVE_ROOM}, headers=HX)
    assert "by you" in r.text
    r = client.post(f"/imports/entries/{nf['id']}/choose", data={}, headers=HX)
    assert "pick a candidate" in r.text
    r = client.post(f"/imports/{lid}/queue", headers=HX)
    assert "Queued 1 album" in r.text and r.headers["HX-Trigger"] == "refresh-entries"
    csv_text = client.get(f"/imports/{lid}/export.csv").text
    assert csv_text.startswith("position,title") and "queued" in csv_text
    assert client.post(f"/imports/{lid}/delete", headers=HX).headers["HX-Redirect"] == "/imports"


def test_web_import_requires_htmx_header(client):
    assert client.post("/imports", data={"content": SHOPPING}).status_code == 403


# ------------------------------------------------------------------ CLI

def test_cli_check_and_convert(tmp_path, capsys):
    from soundtrack_manager.cli import main
    src = tmp_path / "list.txt"
    src.write_text(SHOPPING, encoding="utf-8")
    assert main(["import", "check", str(src)]) == 0
    assert main(["import", "convert", str(src)]) == 0
    out = tmp_path / "list.csv"
    assert len(parse_import(out.read_bytes(), "list.csv").entries) == 5
    assert main(["import", "convert", str(out), "-o", str(out)]) == 2   # refuses to overwrite its input
    bad = tmp_path / "bad.csv"
    bad.write_text("title,format\nx,wav\ny,\n", encoding="utf-8")
    assert main(["import", "check", str(bad)]) == 1
    assert main(["import", "check", str(tmp_path / "missing.txt")]) == 2


@pytest.mark.anyio
async def test_failed_format_check_keeps_the_match(ctx):
    """The album page fails to load after a clear match: keep the album, format unknown."""
    imp = ctx.imports
    text = "[ ] 1. Final Fantasy 35th Piano Covers\n    https://downloads.khinsider.com/search?search=final+fantasy\n"
    lid = imp.create(parse_import(text), name="t", fmt="flac")
    await imp.match_list(lid)  # the fake fetcher has no fixture for that album page
    e = imp.entries(lid)[0]
    assert e["status"] == "matched" and e["album_title"] == "Final Fantasy 35th Piano Covers"
    assert e["has_format"] is None and e["error"].startswith("format not checked")
    assert imp.get(lid)["queueable"] == 1  # unknown format still queues (the worker falls back to MP3)


def test_weak_candidates_mean_not_found():
    c = _rank("Zzzz Not A Real Game Soundtrack", ["No, I'm not a Human (Original Soundtrack)", "Not a Hero"])
    assert c and decide(c)[0] == "not_found"   # listed for reference, but not a "choose" entry
