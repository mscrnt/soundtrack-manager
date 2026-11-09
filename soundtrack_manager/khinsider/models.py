"""Structured records produced by the parsers.

Fields are ``None`` when the site does not provide the value on that page.
Nothing here is guessed or synthesised.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field


@dataclass(slots=True)
class AlbumSummary:
    """One row of a search or browse result table."""

    slug: str
    url: str
    title: str
    platforms: list[str] = field(default_factory=list)
    album_type: str | None = None
    year: int | None = None
    catalog_number: str | None = None
    thumb_url: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(slots=True)
class TrackRow:
    """One row of an album page's ``#songlist`` table."""

    number: int | None
    title: str
    page_url: str
    disc: int | None = None
    duration: str | None = None
    # format name (e.g. "MP3", "FLAC") -> size text as shown on the site ("6.27 MB")
    sizes: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class AlbumDetail:
    slug: str
    url: str
    title: str
    site_album_id: int | None = None
    platforms: list[str] = field(default_factory=list)
    album_type: str | None = None
    year: int | None = None
    catalog_number: str | None = None
    publisher: str | None = None
    developer: str | None = None
    date_added: str | None = None
    file_count: int | None = None
    total_size_text: str | None = None
    formats: list[str] = field(default_factory=list)
    cover_urls: list[str] = field(default_factory=list)
    description: str | None = None
    tracks: list[TrackRow] = field(default_factory=list)
    # every "Label: value" pair found in the metadata block, unnormalised
    raw_metadata: dict[str, str] = field(default_factory=dict)

    @property
    def track_count(self) -> int:
        return len(self.tracks)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["track_count"] = self.track_count
        return d


@dataclass(slots=True)
class TrackFile:
    """A downloadable file link found on a track page."""

    format: str
    url: str
    size_text: str | None = None


@dataclass(slots=True)
class ResultPage:
    """Parsed search/browse page."""

    albums: list[AlbumSummary]
    total_reported: int | None
    next_page_url: str | None = None
    last_page: int | None = None
