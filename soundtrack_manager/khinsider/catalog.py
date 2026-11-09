"""Static filter vocabularies taken from the live site (captured 2026-11-09).

* Platform names and search ``album_category`` ids come from the search page's
  platform ``<select>``.
* Browse slugs come from ``/console-list``.
* Album type ids come from the search page's ``album_type`` ``<select>``.

``parse.parse_search_filters`` can re-read these from a live page if the site
adds platforms. ``tests/test_catalog.py`` checks this table against the
fixtures.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Platform:
    name: str          # display name exactly as the site shows it ("PS1")
    slug: str          # browse slug: /game-soundtracks/<slug>
    category_id: int   # search parameter album_category=<id>


PLATFORMS: tuple[Platform, ...] = (
    Platform('3DO', '3do', 56),
    Platform('3DS', 'nintendo-3ds', 15),
    Platform('Amiga', 'amiga', 24),
    Platform('Android', 'android', 45),
    Platform('Anime', 'anime', 49),
    Platform('Arcade', 'arcade', 23),
    Platform('Atari 8-Bit', 'atari-8bit', 25),
    Platform('Atari Jaguar', 'atari-jaguar', 58),
    Platform('Atari ST', 'atari-st', 26),
    Platform('CD-i', 'cd-i', 28),
    Platform('Commodore 64', 'commodore-64', 27),
    Platform('Dreamcast', 'sega-dreamcast', 20),
    Platform('DS', 'nintendo-ds', 16),
    Platform('Family Computer', 'family-computer', 60),
    Platform('FDS', 'famicom-disk-system', 62),
    Platform('FM Towns', 'fm-towns', 29),
    Platform('Fortnite', 'fortnite', 71),
    Platform('Fujitsu FM77AV', 'fujitsu-fm77av', 59),
    Platform('Game Gear', 'sega-game-gear', 6),
    Platform('GB', 'gameboy', 3),
    Platform('GBA', 'gameboy-advance', 4),
    Platform('GC', 'nintendo-gamecube', 19),
    Platform('Genesis / Mega Drive', 'sega-mega-drive-genesis', 8),
    Platform('IBM PC', 'ibm-pc', 63),
    Platform('IBM PC/AT', 'ibm-pc-at', 61),
    Platform('iOS', 'ios', 46),
    Platform('Linux', 'linux', 54),
    Platform('MacOS', 'mac-os', 53),
    Platform('Master System', 'sega-master-system', 7),
    Platform('Mobile', 'mobile', 41),
    Platform('Movie', 'movie', 50),
    Platform('MS-DOS', 'ms-dos', 30),
    Platform('MSX', 'msx', 5),
    Platform('MSX2', 'msx2', 31),
    Platform('N64', 'nintendo-64', 10),
    Platform('Neo Geo', 'neo-geo', 52),
    Platform('NES', 'nintendo-nes', 1),
    Platform('Online', 'online', 51),
    Platform('PC-88', 'pc-8801', 32),
    Platform('PC-98', 'pc-9801', 33),
    Platform('PC-9821', 'pc-9821', 34),
    Platform('PC-FX', 'pc-fx', 48),
    Platform('PS Vita', 'playstation-vita', 42),
    Platform('PS1', 'playstation', 9),
    Platform('PS2', 'playstation-2', 11),
    Platform('PS3', 'playstation-3', 12),
    Platform('PS4', 'playstation-4', 44),
    Platform('PS5', 'playstation-5', 57),
    Platform('PSP', 'playstation-portable-psp', 13),
    Platform('Roblox', 'roblox', 70),
    Platform('Saturn', 'sega-saturn', 21),
    Platform('Sharp X1', 'sharp-x1', 35),
    Platform('SNES', 'nintendo-snes', 2),
    Platform('Spectrum', 'spectrum', 36),
    Platform('Stadia', 'stadia', 65),
    Platform('Steam', 'steam', 69),
    Platform('Switch', 'nintendo-switch', 43),
    Platform('Switch 2', 'switch-2', 68),
    Platform('TurboGrafx-16', 'turbografx-16', 22),
    Platform('Virtual Boy', 'virtual-boy', 55),
    Platform('VR', 'virtual-reality', 66),
    Platform('Wii', 'nintendo-wii', 14),
    Platform('Wii U', 'nintendo-wii-u', 39),
    Platform('Windows', 'windows', 37),
    Platform('X68000', 'x68000', 38),
    Platform('Xbox', 'xbox', 17),
    Platform('Xbox 360', 'xbox-360', 18),
    Platform('Xbox One', 'xbox-one', 47),
    Platform('Xbox Series X/S', 'xbox-series-x', 64),
)

# Friendly aliases for CLI/UI input. Keys are normalised with _norm().
_ALIASES: dict[str, str] = {
    "ps": "PS1", "psx": "PS1", "playstation": "PS1", "playstation1": "PS1",
    "playstation2": "PS2", "playstation3": "PS3", "playstation4": "PS4",
    "playstation5": "PS5", "vita": "PS Vita", "psvita": "PS Vita",
    "pc": "Windows", "win": "Windows", "dos": "MS-DOS",
    "genesis": "Genesis / Mega Drive", "megadrive": "Genesis / Mega Drive",
    "md": "Genesis / Mega Drive", "gamecube": "GC", "ngc": "GC",
    "gameboy": "GB", "gameboyadvance": "GBA", "nintendo64": "N64",
    "famicom": "Family Computer", "superfamicom": "SNES", "sfc": "SNES",
    "pcengine": "TurboGrafx-16", "tg16": "TurboGrafx-16",
    "x360": "Xbox 360", "xb1": "Xbox One", "xsx": "Xbox Series X/S",
    "xboxseries": "Xbox Series X/S", "mac": "MacOS", "macos": "MacOS",
    "switch2": "Switch 2", "ns": "Switch",
}


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


_BY_NORM: dict[str, Platform] = {}
for _p in PLATFORMS:
    _BY_NORM[_norm(_p.name)] = _p
    _BY_NORM.setdefault(_norm(_p.slug), _p)
for _alias, _name in _ALIASES.items():
    _BY_NORM.setdefault(_alias, next(p for p in PLATFORMS if p.name == _name))


def find_platform(value: str) -> Platform | None:
    """Resolve a user-supplied platform ('ps1', 'PS1', 'playstation', '9')."""
    value = value.strip()
    if value.isdigit():
        return next((p for p in PLATFORMS if p.category_id == int(value)), None)
    return _BY_NORM.get(_norm(value))


@dataclass(frozen=True, slots=True)
class AlbumType:
    id: int
    label: str       # filter label on the search form ("Soundtracks")
    row_label: str   # label used in result rows ("Soundtrack")


# Row labels verified in fixtures: Gamerip, Remix, Arrangement. The others
# follow the same singular pattern. Matching (see matches_type) tolerates
# singular/plural differences either way.
ALBUM_TYPES: tuple[AlbumType, ...] = (
    AlbumType(1, "Soundtracks", "Soundtrack"),
    AlbumType(2, "Gamerips", "Gamerip"),
    AlbumType(3, "Singles", "Single"),
    AlbumType(4, "Remixes", "Remix"),
    AlbumType(5, "Arrangements", "Arrangement"),
    AlbumType(6, "Compilations", "Compilation"),
    AlbumType(7, "Inspired By", "Inspired By"),
)


def _type_key(s: str) -> str:
    k = _norm(s)
    for suffix in ("es", "s"):
        if k.endswith(suffix) and k[: -len(suffix)] in {_norm(t.row_label) for t in ALBUM_TYPES}:
            return k[: -len(suffix)]
    return k


def find_album_type(value: str) -> AlbumType | None:
    value = value.strip()
    if value.isdigit():
        return next((t for t in ALBUM_TYPES if t.id == int(value)), None)
    k = _type_key(value)
    if k == "ost":
        k = "soundtrack"
    return next((t for t in ALBUM_TYPES if _norm(t.row_label) == k), None)


def matches_type(row_label: str | None, wanted: AlbumType) -> bool:
    return row_label is not None and _type_key(row_label) == _norm(wanted.row_label)


# Sort values accepted by /search. Only 'name' and 'year' can be re-applied
# locally, because result rows carry no popularity or date-added data.
SEARCH_SORTS: dict[str, str] = {
    "relevance": "Relevance",
    "popularity": "Popularity",
    "name": "Name",
    "year": "Year",
    "timestamp": "Date added",
}
