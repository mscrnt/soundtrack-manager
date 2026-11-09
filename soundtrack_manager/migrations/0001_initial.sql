-- 0001: initial schema
-- JSON columns hold lists/dicts exactly as parsed; NULL means "site did not say".

CREATE TABLE albums (
    id               INTEGER PRIMARY KEY,
    slug             TEXT NOT NULL UNIQUE,
    url              TEXT NOT NULL,
    title            TEXT NOT NULL,
    platforms_json   TEXT NOT NULL DEFAULT '[]',
    album_type       TEXT,
    year             INTEGER,
    catalog_number   TEXT,
    thumb_url        TEXT,
    -- filled from the album page (detail fetch)
    site_album_id    INTEGER,
    publisher        TEXT,
    developer        TEXT,
    date_added       TEXT,
    file_count       INTEGER,
    track_count      INTEGER,
    total_size_text  TEXT,
    formats_json     TEXT,
    cover_urls_json  TEXT,
    description      TEXT,
    raw_metadata_json TEXT,
    detail_fetched_at TEXT,
    first_seen_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    last_seen_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
CREATE INDEX idx_albums_year ON albums(year);
CREATE INDEX idx_albums_type ON albums(album_type);

CREATE TABLE tracks (
    id          INTEGER PRIMARY KEY,
    album_id    INTEGER NOT NULL REFERENCES albums(id) ON DELETE CASCADE,
    position    INTEGER NOT NULL,          -- order on the album page
    disc        INTEGER,
    number      INTEGER,
    title       TEXT NOT NULL,
    duration    TEXT,
    page_url    TEXT NOT NULL,
    sizes_json  TEXT NOT NULL DEFAULT '{}',
    UNIQUE (album_id, page_url)
);

-- A cached remote listing: one site search, or one platform browse (all pages).
CREATE TABLE listings (
    id              INTEGER PRIMARY KEY,
    cache_key       TEXT NOT NULL UNIQUE,   -- canonical request (URL params) that was fetched
    kind            TEXT NOT NULL CHECK (kind IN ('search', 'browse')),
    total_reported  INTEGER,
    pages_fetched   INTEGER NOT NULL DEFAULT 0,
    complete        INTEGER NOT NULL DEFAULT 0,  -- 0 when page cap hit
    fetched_at      TEXT NOT NULL
);

CREATE TABLE listing_albums (
    listing_id INTEGER NOT NULL REFERENCES listings(id) ON DELETE CASCADE,
    position   INTEGER NOT NULL,
    album_id   INTEGER NOT NULL REFERENCES albums(id) ON DELETE CASCADE,
    PRIMARY KEY (listing_id, position)
);

-- What the user asked for (search history), including local filters.
CREATE TABLE search_history (
    id          INTEGER PRIMARY KEY,
    query_json  TEXT NOT NULL,
    listing_id  INTEGER REFERENCES listings(id) ON DELETE SET NULL,
    result_count INTEGER,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

-- Persistent download queue. One active job per album+format.
CREATE TABLE jobs (
    id            INTEGER PRIMARY KEY,
    album_id      INTEGER NOT NULL REFERENCES albums(id) ON DELETE CASCADE,
    format        TEXT NOT NULL,            -- requested: 'flac' or 'mp3'
    status        TEXT NOT NULL DEFAULT 'queued'
                  CHECK (status IN ('queued','running','paused','completed','failed','cancelled')),
    priority      INTEGER NOT NULL DEFAULT 0,
    attempts      INTEGER NOT NULL DEFAULT 0,
    error         TEXT,
    dest_dir      TEXT,
    files_total   INTEGER,
    files_done    INTEGER NOT NULL DEFAULT 0,
    files_failed  INTEGER NOT NULL DEFAULT 0,
    bytes_total   INTEGER,
    bytes_done    INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    updated_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    started_at    TEXT,
    finished_at   TEXT
);
CREATE INDEX idx_jobs_status ON jobs(status, priority DESC, id);
CREATE UNIQUE INDEX uq_jobs_active ON jobs(album_id, format)
    WHERE status IN ('queued','running','paused');

-- Per-file state for a job (used by the download worker, milestone 5/6).
CREATE TABLE job_files (
    id           INTEGER PRIMARY KEY,
    job_id       INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    track_id     INTEGER REFERENCES tracks(id) ON DELETE SET NULL,
    kind         TEXT NOT NULL DEFAULT 'audio' CHECK (kind IN ('audio','cover','metadata')),
    source_url   TEXT,
    rel_path     TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending'
                 CHECK (status IN ('pending','downloading','completed','failed','skipped')),
    bytes_total  INTEGER,
    bytes_done   INTEGER NOT NULL DEFAULT 0,
    sha256       TEXT,
    attempts     INTEGER NOT NULL DEFAULT 0,
    error        TEXT,
    updated_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    UNIQUE (job_id, rel_path)
);

CREATE TABLE settings (
    key        TEXT PRIMARY KEY,
    value_json TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE TABLE sync_state (
    key        TEXT PRIMARY KEY,
    value      TEXT,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
