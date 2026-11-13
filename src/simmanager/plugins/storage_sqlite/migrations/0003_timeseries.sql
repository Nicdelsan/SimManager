-- M2: incremental file cursors, chunked time series (float64 + min/max levels), solver run state.

CREATE TABLE file_cursors (
    id                INTEGER PRIMARY KEY,
    run_id            INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    file_path         TEXT    NOT NULL,
    parser_kind       TEXT    NOT NULL,
    file_size         INTEGER NOT NULL DEFAULT 0,
    mtime             REAL    NOT NULL DEFAULT 0,
    head_prefix       BLOB    NOT NULL DEFAULT X'',
    forward_offset    INTEGER NOT NULL DEFAULT 0,
    backfill_start    INTEGER NOT NULL DEFAULT 0,
    backfill_end      INTEGER NOT NULL DEFAULT 0,
    header_json       TEXT    NOT NULL DEFAULT '{}',
    parser_state_json TEXT    NOT NULL DEFAULT '{}',
    status            TEXT    NOT NULL DEFAULT 'active',
    updated_at        TEXT    NOT NULL,
    UNIQUE (run_id, file_path)
);

CREATE INDEX file_cursors_run ON file_cursors (run_id);

CREATE TABLE series (
    id          INTEGER PRIMARY KEY,
    run_id      INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    file_path   TEXT    NOT NULL,
    name        TEXT    NOT NULL,
    qualifier   TEXT    NOT NULL DEFAULT '',
    unit        TEXT    NOT NULL DEFAULT '',
    x_name      TEXT    NOT NULL DEFAULT 'Time',
    x_unit      TEXT    NOT NULL DEFAULT '',
    point_count INTEGER NOT NULL DEFAULT 0,
    last_x      REAL,
    last_y      REAL,
    min_y       REAL,
    max_y       REAL,
    UNIQUE (run_id, file_path, name, qualifier)
);

CREATE INDEX series_run ON series (run_id);

CREATE TABLE sample_chunks (
    id        INTEGER PRIMARY KEY,
    series_id INTEGER NOT NULL REFERENCES series(id) ON DELETE CASCADE,
    level     INTEGER NOT NULL DEFAULT 0,
    count     INTEGER NOT NULL,
    x_min     REAL    NOT NULL,
    x_max     REAL    NOT NULL,
    y_min     REAL,
    y_max     REAL,
    x_blob    BLOB    NOT NULL,
    y_blob    BLOB    NOT NULL
);

CREATE INDEX sample_chunks_lookup ON sample_chunks (series_id, level, x_min, x_max);

CREATE TABLE run_solver_state (
    run_id        INTEGER PRIMARY KEY REFERENCES runs(id) ON DELETE CASCADE,
    solver        TEXT    NOT NULL,
    work_dir      TEXT    NOT NULL,
    case_dir      TEXT,
    output_dir    TEXT,
    solver_state  TEXT    NOT NULL DEFAULT 'unknown',
    progress      REAL,
    current_time  REAL,
    start_time    REAL,
    end_time      REAL,
    ncyc          INTEGER,
    warning_count INTEGER NOT NULL DEFAULT 0,
    error_message TEXT,
    metadata_json TEXT    NOT NULL DEFAULT '{}',
    updated_at    TEXT    NOT NULL
);
