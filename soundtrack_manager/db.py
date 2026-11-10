"""SQLite access and forward-only migrations.

Each operation opens a short-lived connection (WAL mode), so the web server
threads, the async event loop and a future worker can share the file safely.
"""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path
from typing import Iterator

MIGRATIONS_PACKAGE = "soundtrack_manager.migrations"


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 30000")
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise

    # ------------------------------------------------------------------
    def migrate(self) -> list[str]:
        """Apply pending migrations. Returns the names applied."""
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                " version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)"
            )
            done = {r[0] for r in conn.execute("SELECT version FROM schema_migrations")}
        applied = []
        for version, name, sql in _migration_files():
            if version in done:
                continue
            with self.transaction() as conn:
                # executescript would COMMIT our transaction, so split statements.
                for stmt in _split_sql(sql):
                    conn.execute(stmt)
                conn.execute(
                    "INSERT INTO schema_migrations(version, name, applied_at) VALUES (?,?,?)",
                    (version, name, utcnow()),
                )
            applied.append(name)
        return applied

    def schema_version(self) -> int:
        with self.connect() as conn:
            try:
                return conn.execute("SELECT COALESCE(MAX(version),0) FROM schema_migrations").fetchone()[0]
            except sqlite3.OperationalError:
                return 0

    # ------------------------------------------------------------------
    # settings overrides
    def get_overrides(self) -> dict:
        with self.connect() as conn:
            return {r["key"]: json.loads(r["value_json"]) for r in conn.execute("SELECT key, value_json FROM settings")}

    def set_override(self, key: str, value) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO settings(key, value_json, updated_at) VALUES (?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json, updated_at=excluded.updated_at",
                (key, json.dumps(value), utcnow()),
            )

    def set_sync_state(self, key: str, value: str | None) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO sync_state(key, value, updated_at) VALUES (?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (key, value, utcnow()),
            )

    def get_sync_state(self, key: str) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute("SELECT value, updated_at FROM sync_state WHERE key=?", (key,)).fetchone()


def _migration_files() -> list[tuple[int, str, str]]:
    out = []
    for entry in resources.files(MIGRATIONS_PACKAGE).iterdir():
        m = re.match(r"^(\d{4})_.+\.sql$", entry.name)
        if m:
            out.append((int(m.group(1)), entry.name, entry.read_text(encoding="utf-8")))
    return sorted(out)


def _split_sql(sql: str) -> list[str]:
    # Drop comments first, so a ';' inside one can't split a statement (no '--' appears inside SQL strings).
    sql = re.sub(r"--[^\n]*", "", sql)
    return [s.strip() for s in sql.split(";") if s.strip()]
