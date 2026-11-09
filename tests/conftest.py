from __future__ import annotations

from pathlib import Path

import pytest

from soundtrack_manager.config import Secret, Settings
from soundtrack_manager.khinsider.fetch import AccessBlocked, SessionStatus

FIXTURES = Path(__file__).parent / "fixtures"


def fixture_html(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class FakeFetcher:
    """Serves saved HTML fixtures by URL rule. Records every URL requested."""

    def __init__(self, rules: list[tuple[str, str]] | None = None, blocked: bool = False):
        self.rules = rules if rules is not None else DEFAULT_RULES
        self.blocked = blocked
        self.requests: list[str] = []
        self.status = SessionStatus()

    async def get_html(self, url: str) -> str:
        self.requests.append(url)
        if self.blocked:
            raise AccessBlocked("Cloudflare check (test)")
        for needle, name in self.rules:
            if needle in url:
                return name(url) if callable(name) else fixture_html(name)
        raise AssertionError(f"FakeFetcher has no fixture for {url}")

    async def close(self) -> None:
        pass


DEFAULT_RULES = [
    ("search=resident+evil&type=album&sort=year&album_type=1", "search_empty.html"),
    ("search=resident+evil", "search_resident_evil.html"),
    ("search=final+fantasy", "search_final_fantasy_trimmed.html"),
    ("search=", "search_empty.html"),
    ("/game-soundtracks/playstation", "browse_ps1_trimmed.html"),
    ("/game-soundtracks/album/resident-evil-save-room-compilation-2023", "album_save_room.html"),
]


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    # worker off: web tests must never start background downloads
    return Settings(data_dir=tmp_path / "data", download_dir=tmp_path / "downloads", log_level="WARNING",
                    worker_enabled=False)


@pytest.fixture
def fake_fetcher() -> FakeFetcher:
    return FakeFetcher()


@pytest.fixture
def ctx(settings, fake_fetcher):
    from soundtrack_manager.runtime import AppContext
    return AppContext(settings, fetcher=fake_fetcher, configure_logging=False)


@pytest.fixture
def anyio_backend():
    return "asyncio"


__all__ = ["FakeFetcher", "fixture_html", "Secret"]
