"""Folder templates and download-path safety.

Templates use ``{field}`` placeholders. Every rendered component is sanitised
for Windows and Linux, and the final path must resolve inside the download
root, so templates and site metadata cannot escape it with ``..``, absolute
paths, drive letters or UNC prefixes.
"""

from __future__ import annotations

import re
import string
from pathlib import Path, PurePosixPath

TEMPLATE_FIELDS = {"platform", "platforms", "album", "year", "type", "catalog", "slug"}
_WIN_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
_BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


class UnsafePathError(ValueError):
    pass


def validate_template(template: str) -> None:
    if not template or len(template) > 200:
        raise UnsafePathError("template must be 1-200 characters")
    if template.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", template):
        raise UnsafePathError("template must be relative")
    for _, fname, spec, conv in string.Formatter().parse(template):
        if fname is None:
            continue
        if fname not in TEMPLATE_FIELDS or spec or conv:
            raise UnsafePathError(f"unknown template field {{{fname}}}; allowed: {', '.join(sorted(TEMPLATE_FIELDS))}")
    for part in re.split(r"[\\/]", template):
        if part.strip() in ("..", "."):
            raise UnsafePathError("template must not contain '.' or '..' segments")


def sanitize_component(value: str, fallback: str = "Unknown") -> str:
    v = _BAD_CHARS.sub("_", value or "")
    v = re.sub(r"\s+", " ", v).strip().rstrip(". ")
    if not v or v in (".", ".."):
        v = fallback
    if v.split(".")[0].upper() in _WIN_RESERVED:
        v = "_" + v
    return v[:120]


def render_album_dir(template: str, album: dict) -> PurePosixPath:
    """Relative album directory from a template and an album record."""
    validate_template(template)
    platforms = album.get("platforms") or []
    values = {
        "platform": platforms[0] if platforms else "Unknown Platform",
        "platforms": ", ".join(platforms) if platforms else "Unknown Platform",
        "album": album.get("title") or album.get("slug") or "Unknown Album",
        "year": str(album["year"]) if album.get("year") else "Unknown Year",
        "type": album.get("album_type") or "Unknown Type",
        "catalog": album.get("catalog_number") or "",
        "slug": album.get("slug") or "",
    }
    parts = []
    for raw_part in re.split(r"[\\/]", template):
        rendered = raw_part.format(**{k: sanitize_component(v, "") for k, v in values.items()})
        parts.append(sanitize_component(rendered))
    return PurePosixPath(*parts)


def safe_join(root: Path, rel: PurePosixPath | str) -> Path:
    """Join and make sure the result stays inside ``root`` (no traversal)."""
    root = root.resolve()
    rel = PurePosixPath(str(rel).replace("\\", "/"))
    if rel.is_absolute() or any(p in ("..",) for p in rel.parts) or re.match(r"^[A-Za-z]:", str(rel)):
        raise UnsafePathError(f"unsafe relative path: {rel}")
    target = (root / Path(*rel.parts)).resolve()
    if target != root and root not in target.parents:
        raise UnsafePathError(f"path escapes download root: {rel}")
    return target
