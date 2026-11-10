from pathlib import Path, PurePosixPath

import pytest

from soundtrack_manager import queue
from soundtrack_manager.db import Database
from soundtrack_manager.paths import UnsafePathError, render_album_dir, safe_join, sanitize_component, validate_template


def _album(db: Database, slug="a1", title="Album One") -> int:
    with db.connect() as c:
        c.execute("INSERT INTO albums(slug, url, title) VALUES (?,?,?)", (slug, f"https://x/{slug}", title))
        return c.execute("SELECT id FROM albums WHERE slug=?", (slug,)).fetchone()[0]


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "t.sqlite3")
    d.migrate()
    return d


def test_migrations_idempotent(db):
    assert db.schema_version() == 3
    assert db.migrate() == []


def test_enqueue_dedupes_across_searches(db):
    a, b = _album(db, "a"), _album(db, "b")
    r = queue.enqueue(db, [a, b, a], "flac")
    assert r.added == [a, b]
    r = queue.enqueue(db, [a], "flac")
    assert r.added == [] and r.already_queued == [a]
    assert queue.enqueue(db, [a], "mp3").added == [a]  # different format is a separate job
    assert queue.enqueue(db, [9999], "flac").missing == [9999]


def test_completed_not_requeued_by_default(db):
    a = _album(db)
    queue.enqueue(db, [a], "flac")
    with db.connect() as c:
        c.execute("UPDATE jobs SET status='completed'")
    assert queue.enqueue(db, [a], "flac").already_completed == [a]
    assert queue.enqueue(db, [a], "flac", allow_completed=True).added == [a]


def test_state_transitions(db):
    a = _album(db)
    queue.enqueue(db, [a], "mp3")
    jid = queue.list_jobs(db)[0]["id"]
    assert queue.pause(db, jid)
    assert not queue.pause(db, jid)
    assert queue.resume(db, jid)
    assert queue.cancel(db, jid)
    assert queue.retry(db, jid)
    assert queue.list_jobs(db)[0]["status"] == "queued"
    assert queue.counts(db)["queued"] == 1


def test_retry_blocked_when_another_active(db):
    a = _album(db)
    queue.enqueue(db, [a], "mp3")
    j1 = queue.list_jobs(db)[0]["id"]
    queue.cancel(db, j1)
    queue.enqueue(db, [a], "mp3")
    assert not queue.retry(db, j1)


def test_remove_never_running(db):
    a = _album(db)
    queue.enqueue(db, [a], "mp3")
    jid = queue.list_jobs(db)[0]["id"]
    with db.connect() as c:
        c.execute("UPDATE jobs SET status='running'")
    assert not queue.remove(db, jid)


def test_recover_interrupted(db):
    a = _album(db)
    queue.enqueue(db, [a], "mp3")
    with db.connect() as c:
        c.execute("UPDATE jobs SET status='running'")
        jid = c.execute("SELECT id FROM jobs").fetchone()[0]
        c.execute("INSERT INTO job_files(job_id, position, rel_path, status) VALUES (?, 0, ?, 'downloading')", (jid, "x.mp3"))
    assert queue.recover_interrupted(db) == 1
    assert queue.list_jobs(db)[0]["status"] == "queued"
    with db.connect() as c:
        assert c.execute("SELECT status FROM job_files").fetchone()[0] == "pending"


# ---------------------------------------------------------------- paths

ALBUM = {"title": "Resident Evil: Save Room Compilation", "platforms": ["PS1", "N64"], "year": 2023,
         "album_type": "Remix", "slug": "re-save-room", "catalog_number": None}


def test_render_default_template():
    assert render_album_dir("{platform}/{album}", ALBUM) == PurePosixPath("PS1/Resident Evil_ Save Room Compilation")


def test_render_with_year_and_missing_values():
    p = render_album_dir("{platform}/{album} ({year})", {"title": "X", "platforms": [], "year": None})
    assert p == PurePosixPath("Unknown Platform/X (Unknown Year)")


@pytest.mark.parametrize("tpl", ["../{album}", "/abs/{album}", "C:/{album}", "{album}/../..", "{nope}",
                                 "{album!r}", "{album:>10}", ""])
def test_bad_templates_rejected(tpl):
    with pytest.raises(UnsafePathError):
        validate_template(tpl)


def test_metadata_cannot_traverse():
    evil = {"title": "../../etc", "platforms": ["..\\..\\Windows"], "year": 1}
    p = render_album_dir("{platform}/{album}", evil)
    assert ".." not in p.parts
    assert all("/" not in part and "\\" not in part for part in p.parts)


@pytest.mark.parametrize("raw,clean", [
    ('a<b>c:"d"|e?f*', "a_b_c__d__e_f_"), ("CON", "_CON"), ("nul.txt", "_nul.txt"),
    ("trailing dots...", "trailing dots"), ("", "Unknown"), ("  ", "Unknown"),
])
def test_sanitize_component(raw, clean):
    assert sanitize_component(raw) == clean


def test_safe_join(tmp_path: Path):
    root = tmp_path / "dl"
    root.mkdir()
    assert safe_join(root, "PS1/Album") == (root / "PS1" / "Album").resolve()
    for bad in ("../x", "/etc/passwd", "C:/Windows", "a/../../x"):
        with pytest.raises(UnsafePathError):
            safe_join(root, bad)
