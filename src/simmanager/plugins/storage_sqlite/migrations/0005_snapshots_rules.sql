-- M6: content-addressed input snapshots, run notes, detection rules, alerts, and rule state.

CREATE TABLE snapshot_blobs (
    sha256           TEXT    PRIMARY KEY,
    size_bytes       INTEGER NOT NULL,
    compressed_bytes INTEGER NOT NULL,
    is_text          INTEGER NOT NULL DEFAULT 1,
    encoding         TEXT    NOT NULL DEFAULT 'utf-8',
    data             BLOB    NOT NULL,
    created_at       TEXT    NOT NULL
);

CREATE TABLE snapshots (
    id              INTEGER PRIMARY KEY,
    run_id          INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    trigger         TEXT    NOT NULL CHECK (trigger IN ('discovered', 'running', 'manual')),
    effective       INTEGER NOT NULL DEFAULT 1,
    status          TEXT    NOT NULL CHECK (status IN ('ok', 'partial', 'failed')),
    late            INTEGER NOT NULL DEFAULT 0,
    late_files_json TEXT    NOT NULL DEFAULT '[]',
    solver          TEXT,
    work_dir        TEXT    NOT NULL,
    manifest_sha256 TEXT    NOT NULL,
    file_count      INTEGER NOT NULL DEFAULT 0,
    total_bytes     INTEGER NOT NULL DEFAULT 0,
    error_message   TEXT,
    captured_at     TEXT    NOT NULL
);

CREATE INDEX snapshots_run ON snapshots (run_id, captured_at, id);
CREATE INDEX snapshots_effective ON snapshots (run_id) WHERE effective = 1;

CREATE TABLE snapshot_files (
    id             INTEGER PRIMARY KEY,
    snapshot_id    INTEGER NOT NULL REFERENCES snapshots(id) ON DELETE CASCADE,
    rel_path       TEXT    NOT NULL,
    size_bytes     INTEGER NOT NULL,
    mtime          REAL    NOT NULL DEFAULT 0,
    sha256         TEXT,
    content_status TEXT    NOT NULL
        CHECK (content_status IN ('stored', 'oversize_hashed', 'oversize_unhashed', 'unreadable')),
    UNIQUE (snapshot_id, rel_path)
);

CREATE INDEX snapshot_files_snapshot ON snapshot_files (snapshot_id);
CREATE INDEX snapshot_files_reuse
    ON snapshot_files (rel_path, size_bytes, mtime)
    WHERE sha256 IS NOT NULL;

CREATE TABLE run_notes (
    run_id     INTEGER PRIMARY KEY REFERENCES runs(id) ON DELETE CASCADE,
    note       TEXT    NOT NULL DEFAULT '',
    updated_at TEXT    NOT NULL
);

CREATE TABLE rule_configs (
    id          INTEGER PRIMARY KEY,
    scope_type  TEXT    NOT NULL CHECK (scope_type IN ('global', 'campaign', 'case')),
    scope_id    INTEGER NOT NULL DEFAULT 0,
    rule_key    TEXT    NOT NULL,
    rule_kind   TEXT    NOT NULL,
    enabled     INTEGER NOT NULL DEFAULT 1,
    severity    TEXT    NOT NULL CHECK (severity IN ('info', 'warning', 'critical')),
    params_json TEXT    NOT NULL DEFAULT '{}',
    updated_at  TEXT    NOT NULL,
    UNIQUE (scope_type, scope_id, rule_key)
);

CREATE INDEX rule_configs_scope ON rule_configs (scope_type, scope_id);

CREATE TABLE alerts (
    id              INTEGER PRIMARY KEY,
    dedup_key       TEXT    NOT NULL,
    rule_key        TEXT    NOT NULL,
    rule_kind       TEXT    NOT NULL,
    severity        TEXT    NOT NULL CHECK (severity IN ('info', 'warning', 'critical')),
    status          TEXT    NOT NULL CHECK (status IN ('open', 'cleared')),
    cluster_id      TEXT,
    run_id          INTEGER REFERENCES runs(id) ON DELETE CASCADE,
    campaign_id     INTEGER REFERENCES campaigns(id) ON DELETE SET NULL,
    case_id         INTEGER REFERENCES cases(id) ON DELETE SET NULL,
    title           TEXT    NOT NULL,
    message         TEXT    NOT NULL,
    details_json    TEXT    NOT NULL DEFAULT '{}',
    raised_at       TEXT    NOT NULL,
    last_seen_at    TEXT    NOT NULL,
    cleared_at      TEXT,
    acknowledged_at TEXT,
    acknowledged_by TEXT
);

CREATE UNIQUE INDEX alerts_open_dedup ON alerts (dedup_key) WHERE status = 'open';
CREATE INDEX alerts_status_raised ON alerts (status, raised_at DESC);
CREATE INDEX alerts_run_raised ON alerts (run_id, raised_at DESC);

CREATE TABLE rule_run_state (
    run_id                INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    rule_key              TEXT    NOT NULL,
    series_tag            TEXT    NOT NULL DEFAULT '',
    last_x                REAL,
    consecutive_hits      INTEGER NOT NULL DEFAULT 0,
    consecutive_clear     INTEGER NOT NULL DEFAULT 0,
    warmup_remaining      INTEGER NOT NULL DEFAULT 0,
    last_observed_bytes   INTEGER,
    last_growth_at        TEXT,
    growth_intervals_json TEXT    NOT NULL DEFAULT '[]',
    updated_at            TEXT    NOT NULL,
    PRIMARY KEY (run_id, rule_key, series_tag)
);
