-- Display: versioned script library, script executions on runs, image history (ADR 0008).

CREATE TABLE display_scripts (
    id         INTEGER PRIMARY KEY,
    name       TEXT    NOT NULL UNIQUE,
    created_at TEXT    NOT NULL
);

-- Immutable: a change of the script makes a new row with the next number.
CREATE TABLE display_script_versions (
    id          INTEGER PRIMARY KEY,
    script_id   INTEGER NOT NULL REFERENCES display_scripts(id) ON DELETE CASCADE,
    number      INTEGER NOT NULL,
    sha256      TEXT    NOT NULL,
    body        TEXT    NOT NULL,
    setup       TEXT    NOT NULL,
    interpreter TEXT    NOT NULL,
    message     TEXT    NOT NULL,
    created_at  TEXT    NOT NULL,
    UNIQUE (script_id, number)
);

CREATE TABLE display_executions (
    id          INTEGER PRIMARY KEY,
    run_id      INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    version_id  INTEGER NOT NULL REFERENCES display_script_versions(id),
    trigger     TEXT    NOT NULL CHECK (trigger IN ('manual', 'auto')),
    status      TEXT    NOT NULL
        CHECK (status IN ('running', 'ok', 'failed', 'error', 'interrupted')),
    started_at  TEXT    NOT NULL,
    finished_at TEXT,
    exit_status INTEGER,
    stdout_tail TEXT    NOT NULL DEFAULT '',
    stderr_tail TEXT    NOT NULL DEFAULT '',
    error       TEXT    NOT NULL DEFAULT ''
);

CREATE INDEX display_executions_run ON display_executions (run_id, id);

-- Content-addressed image bytes: an unchanged image made again is stored once.
CREATE TABLE display_image_blobs (
    sha256     TEXT    PRIMARY KEY,
    size_bytes INTEGER NOT NULL,
    data       BLOB    NOT NULL
);

CREATE TABLE display_images (
    id           INTEGER PRIMARY KEY,
    execution_id INTEGER NOT NULL REFERENCES display_executions(id) ON DELETE CASCADE,
    path         TEXT    NOT NULL,
    size_bytes   INTEGER NOT NULL,
    mtime        REAL    NOT NULL,
    media_type   TEXT    NOT NULL,
    sha256       TEXT    REFERENCES display_image_blobs(sha256),
    skipped      TEXT    CHECK (skipped IN ('too_large', 'read_error', 'limit')),
    detail       TEXT    NOT NULL DEFAULT ''
);

CREATE INDEX display_images_execution ON display_images (execution_id, mtime);

CREATE TABLE display_schedules (
    run_id      INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    script_id   INTEGER NOT NULL REFERENCES display_scripts(id) ON DELETE CASCADE,
    interval_s  INTEGER NOT NULL CHECK (interval_s > 0),
    enabled     INTEGER NOT NULL,
    approved_at TEXT    NOT NULL,
    last_run_at TEXT,
    PRIMARY KEY (run_id, script_id)
);
