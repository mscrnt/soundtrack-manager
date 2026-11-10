"""Matching against real search results captured from the site (2026-11-10, a sample of a real list).

Each case: the list entry, the site's candidate albums (title, year, type, platforms), and what should
happen: matched to a given title, or left for review with a given title on top.
"""

from __future__ import annotations

import pytest

from soundtrack_manager.imports import ImportEntry, _title_hints, decide, rank

C = lambda title, year, kind, *plats: {"title": title, "year": year, "album_type": kind, "platforms": list(plats)}

CASES = [
    ("Dark Souls Original Soundtrack (Prepare to Die Edition)", [
        C("Dark Souls - Prepare to Die Edition", 2011, None, "PS3", "Windows", "Xbox 360"),
        C("Dark Souls - Prepare To Die Edition", 2012, "Gamerip", "PS3", "Xbox 360"),
        C("DARK SOULS Soundtracks", 2011, "Soundtrack", "PS3", "Xbox 360"),
        C("DARK SOULS Original Soundtrack", 2011, "Soundtrack", "PS3", "Windows", "Xbox 360")],
     ("review", "Dark Souls - Prepare")),                       # two Prepare to Die albums: your call
    ("Shadow Hearts - Near Death Experience (Arrange Tracks)", [
        C("near death experience, SHADOW HEARTS Arrangetracks", 2005, "Arrangement", "PS2")],
     ("matched", "near death experience, SHADOW HEARTS Arrangetracks")),
    ("Radiant Silvergun Original Soundtrack", [
        C("Radiant Silvergun", 1998, "Gamerip", "Saturn"), C("Radiant Silvergun", 2022, "Gamerip", "Switch"),
        C("RADIANT SILVERGUN SOUNDTRACK+", 1998, "Soundtrack", "Arcade", "Saturn"),
        C("RADIANT SILVERGUN SOUNDTRACK-", 1998, "Soundtrack", "Arcade", "Saturn")],
     ("review", "RADIANT SILVERGUN SOUNDTRACK")),               # SOUNDTRACK+ and SOUNDTRACK- both exist
    ("Silent Hill Homecoming Original Soundtrack", [
        C("Silent Hill Homecoming Soundtrack", 2008, "Soundtrack", "PS3"),
        C("Silent Hill: Homecoming", 2008, "Gamerip", "PS3"),
        C("Silent Hill: Homecoming Unofficial Soundtrack", 2008, "Gamerip", "PS3")],
     ("matched", "Silent Hill Homecoming Soundtrack")),
    ("Grandia II Original Soundtrack", [
        C("Grandia II", 2000, "Gamerip", "Dreamcast"),
        C("Grandia II Original Soundtrack", 2023, "Soundtrack", "Dreamcast"),
        C("GRANDIA ORIGINAL SOUNDTRACKS II", 1998, "Soundtrack", "PS1", "Saturn"),
        C("Grandia II ~DEUS~", 2000, "Soundtrack", "Dreamcast", "PS2")],
     ("matched", "Grandia II Original Soundtrack")),
    ("The King of Fighters 2002 Original Soundtrack", [
        C("The King of Fighters 2002", 2003, "Gamerip", "Dreamcast"),
        C("The King of Fighters 2002", 2004, "Gamerip", "PS2"),
        C("The King of Fighters 2000", 2002, "Gamerip", "Dreamcast")],
     ("review", "The King of Fighters 2002")),                  # Dreamcast vs PS2 rip: your call
    ("Castlevania: The Dracula X Chronicles Soundtrack", [
        C("Castlevania - The Dracula X Chronicles", 2007, "Gamerip", "PSP"),
        C("Castlevania: The Dracula X Chronicles Original Soundtrack", 2007, "Soundtrack", "PS1", "PSP")],
     ("matched", "Castlevania: The Dracula X Chronicles Original Soundtrack")),
    ("Castlevania Chronicles Soundtrack", [
        C("Castlevania Chronicles", 2001, "Gamerip", "PS1", "X68000"),
        C("CASTLEVANIA CHRONICLE AKUMAJO DRACULA Original Soundtrack", 2001, "Soundtrack", "PS1", "X68000"),
        C("Castlevania Chronicles Soundtrack Collection (Revised Version)", 2001, "Soundtrack", "PS1"),
        C("Castlevania: The Lecarde Chronicles 2", 2017, "Gamerip", "Windows")],
     ("matched", "Castlevania Chronicles")),
    ("Contra (NES) Soundtrack", [
        C("Contra", 2012, "Arrangement", "NES"), C("Contra", 1988, "Gamerip", "Family Computer", "NES"),
        C("MUSIC from CONTRA", 2024, "Compilation", "Arcade", "DS"), C("Concert NES", 2015, "Arrangement", "NES")],
     ("matched", "Contra")),
    ("Wild Arms 3 (Wild Arms Advanced 3rd) Original Soundtrack", [
        C("Wild Arms 3", 2002, "Gamerip", "PS2"), C("WILD ARMS 3 Original Soundtrack", 2002, "Soundtrack", "PS2")],
     ("matched", "WILD ARMS 3 Original Soundtrack")),
    ("Armored Core 4 - Original Soundtrack (2006)", [
        C("Armored Core 4", 2006, "Gamerip", "PS3", "Xbox 360"),
        C("Armored Core 4 ORIGINAL SOUNDTRACK", 2006, "Soundtrack", "PS3", "Xbox 360")],
     ("matched", "Armored Core 4 ORIGINAL SOUNDTRACK")),
    ("Tekken Tag Tournament - Direct Audio (2000)", [
        C("Tekken Tag Tournament Direct Audio", 2000, "Soundtrack", "PS2")],
     ("matched", "Tekken Tag Tournament Direct Audio")),
    ("Final Fantasy XV Piano Collections", [
        C("End Of The Road: Melancholy Music from Final Fantasy XV", 2019, "Arrangement", "PS4")],
     ("review", "End Of The Road")),                            # not on the site; don't guess
]


@pytest.mark.parametrize("title, albums, expected", CASES, ids=[c[0][:40] for c in CASES])
def test_real_cases(title, albums, expected):
    e = ImportEntry(title=title)
    _title_hints(e)
    cands = rank({"title": e.title, "year": e.year, "platform": e.platform},
                 [{**a, "id": i} for i, a in enumerate(albums)])
    status, best = decide(cands)
    want_status, want_title = expected
    top = cands[0]
    detail = [(c["title"], c["year"], c["album_type"], c["score"]) for c in cands]
    if want_status == "matched":
        assert status == "matched" and best["title"] == want_title, detail
        if want_title == "Contra":
            assert best["year"] == 1988, detail      # the NES game, not the 2012 arrangement
    else:
        assert status == "ambiguous" and top["title"].startswith(want_title), detail
