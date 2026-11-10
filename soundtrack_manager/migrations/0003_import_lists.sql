-- 0003: import lists (shopping lists / CSV) matched against the site, then queued.

CREATE TABLE import_lists (
    id            INTEGER PRIMARY KEY,
    name          TEXT NOT NULL,
    source_format TEXT NOT NULL,             -- 'text' | 'csv'
    format        TEXT NOT NULL,             -- audio format to queue in ('flac' | 'mp3')
    status        TEXT NOT NULL DEFAULT 'matching'
                  CHECK (status IN ('matching','paused','ready','failed')),
    error         TEXT,
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    updated_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE TABLE import_entries (
    id              INTEGER PRIMARY KEY,
    list_id         INTEGER NOT NULL REFERENCES import_lists(id) ON DELETE CASCADE,
    position        INTEGER NOT NULL,
    line            INTEGER,                 -- line number in the source file
    title           TEXT NOT NULL,
    search          TEXT,                    -- site search term
    url             TEXT,                    -- album URL given in the file (skips matching)
    platform        TEXT,                    -- hint
    year            INTEGER,                 -- hint
    format          TEXT,                    -- per-entry override of the list format
    priority        INTEGER,
    section         TEXT,
    note            TEXT,
    status          TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending','matched','ambiguous','not_found','error','skipped','queued')),
    match_method    TEXT,                    -- 'url' | 'auto' | 'manual'
    album_id        INTEGER REFERENCES albums(id) ON DELETE SET NULL,
    score           REAL,
    candidates_json TEXT,                    -- [{album_id, title, score}, ...] best first
    has_format      INTEGER,                 -- 1 or 0: album offers the wanted format, NULL = not checked
    formats_json    TEXT,                    -- formats the album offers
    error           TEXT,
    updated_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    UNIQUE (list_id, position)
);
CREATE INDEX idx_import_entries_list ON import_entries(list_id, status);
