-- 0002: download worker state, local library, account history.

-- job_files was unused before this migration; rebuild it with per-track keys.
DROP TABLE job_files;
CREATE TABLE job_files (
    id           INTEGER PRIMARY KEY,
    job_id       INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    kind         TEXT NOT NULL DEFAULT 'audio' CHECK (kind IN ('audio','cover')),
    position     INTEGER NOT NULL,           -- track order on the album page
    track_id     INTEGER REFERENCES tracks(id) ON DELETE SET NULL,
    title        TEXT,
    page_url     TEXT,                       -- track page (resolved lazily)
    format       TEXT,                       -- actual format chosen for this file
    source_url   TEXT,                       -- CDN URL once resolved
    rel_path     TEXT,                       -- file name inside the album folder
    status       TEXT NOT NULL DEFAULT 'pending'
                 CHECK (status IN ('pending','downloading','completed','skipped','failed','conflict')),
    bytes_total  INTEGER,
    bytes_done   INTEGER NOT NULL DEFAULT 0,
    sha256       TEXT,
    attempts     INTEGER NOT NULL DEFAULT 0,
    error        TEXT,
    updated_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    UNIQUE (job_id, kind, position)
);
CREATE INDEX idx_job_files_job ON job_files(job_id, status);

ALTER TABLE jobs ADD COLUMN actual_format TEXT;           -- e.g. 'mp3' when FLAC was not offered
ALTER TABLE jobs ADD COLUMN allow_overwrite INTEGER NOT NULL DEFAULT 0;
ALTER TABLE jobs ADD COLUMN files_conflict INTEGER NOT NULL DEFAULT 0;
ALTER TABLE jobs ADD COLUMN note TEXT;

-- One row per album folder found on disk.
CREATE TABLE library_albums (
    id             INTEGER PRIMARY KEY,
    root           TEXT NOT NULL,
    rel_path       TEXT NOT NULL,
    album_id       INTEGER REFERENCES albums(id) ON DELETE SET NULL,
    title_guess    TEXT NOT NULL,
    match_method   TEXT,                     -- 'album_info' | 'title' | NULL (unmatched)
    file_count     INTEGER NOT NULL DEFAULT 0,
    total_bytes    INTEGER NOT NULL DEFAULT 0,
    missing_count  INTEGER NOT NULL DEFAULT 0,
    mismatch_count INTEGER NOT NULL DEFAULT 0,
    status         TEXT NOT NULL CHECK (status IN ('ok','incomplete','unverified','unmatched')),
    scanned_at     TEXT NOT NULL,
    verified_at    TEXT,
    UNIQUE (root, rel_path)
);
CREATE INDEX idx_library_album ON library_albums(album_id);

CREATE TABLE library_files (
    id               INTEGER PRIMARY KEY,
    library_album_id INTEGER NOT NULL REFERENCES library_albums(id) ON DELETE CASCADE,
    rel_path         TEXT NOT NULL,
    size             INTEGER,
    expected_size    INTEGER,
    sha256           TEXT,
    expected_sha256  TEXT,
    status           TEXT NOT NULL CHECK (status IN ('ok','unverified','missing','size_mismatch','hash_mismatch')),
    UNIQUE (library_album_id, rel_path)
);
CREATE INDEX idx_library_files_hash ON library_files(sha256);

-- Albums listed on the user's khinsider account pages (history import).
CREATE TABLE account_albums (
    id            INTEGER PRIMARY KEY,
    slug          TEXT NOT NULL UNIQUE,
    album_id      INTEGER REFERENCES albums(id) ON DELETE SET NULL,
    title         TEXT NOT NULL,
    source        TEXT NOT NULL,             -- account page it came from
    first_seen_at TEXT NOT NULL,
    last_seen_at  TEXT NOT NULL
);
