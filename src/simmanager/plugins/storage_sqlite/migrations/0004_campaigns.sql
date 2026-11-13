-- M5: projects, campaigns, cases, case_runs, manual exclusions and match ambiguities.

CREATE TABLE projects (
    id          INTEGER PRIMARY KEY,
    name        TEXT    NOT NULL UNIQUE,
    description TEXT    NOT NULL DEFAULT '',
    created_at  TEXT    NOT NULL,
    updated_at  TEXT    NOT NULL
);

CREATE TABLE campaigns (
    id                   INTEGER PRIMARY KEY,
    project_id           INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    name                 TEXT    NOT NULL,
    source_path          TEXT,
    sheet_name           TEXT,
    header_row           INTEGER NOT NULL DEFAULT 1,
    case_column          TEXT    NOT NULL DEFAULT '# Case',
    work_dir_column      TEXT,
    manual_state_column  TEXT,
    work_root            TEXT,
    naming_regex         TEXT,
    columns_json         TEXT    NOT NULL DEFAULT '[]',
    export_monitors_json TEXT    NOT NULL DEFAULT '[]',
    created_at           TEXT    NOT NULL,
    updated_at           TEXT    NOT NULL,
    UNIQUE (project_id, name)
);

CREATE INDEX campaigns_project ON campaigns (project_id);

CREATE TABLE cases (
    id              INTEGER PRIMARY KEY,
    campaign_id     INTEGER NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    case_key        TEXT    NOT NULL,
    row_index       INTEGER,
    parent_case_id  INTEGER REFERENCES cases(id) ON DELETE SET NULL,
    parent_key_raw  TEXT,
    work_dir        TEXT,
    manual_state    TEXT,
    computed_state  TEXT    NOT NULL DEFAULT 'planned'
        CHECK (computed_state IN (
            'planned', 'queued', 'running', 'completed',
            'stopped', 'diverged', 'failed', 'cancelled', 'unknown'
        )),
    params_json     TEXT    NOT NULL DEFAULT '{}',
    active          INTEGER NOT NULL DEFAULT 1,
    notes           TEXT,
    created_at      TEXT    NOT NULL,
    updated_at      TEXT    NOT NULL,
    UNIQUE (campaign_id, case_key)
);

CREATE INDEX cases_campaign_active ON cases (campaign_id, active);
CREATE INDEX cases_work_dir ON cases (work_dir);

CREATE TABLE case_runs (
    id           INTEGER PRIMARY KEY,
    campaign_id  INTEGER NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    case_id      INTEGER NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
    run_id       INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    match_kind   TEXT    NOT NULL
        CHECK (match_kind IN ('auto_work_dir', 'auto_dir_name', 'auto_regex', 'manual')),
    match_detail TEXT,
    locked       INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT    NOT NULL,
    UNIQUE (campaign_id, run_id)
);

CREATE INDEX case_runs_case ON case_runs (case_id);
CREATE INDEX case_runs_run ON case_runs (run_id);

CREATE TABLE case_run_exclusions (
    campaign_id INTEGER NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    run_id      INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    created_at  TEXT    NOT NULL,
    PRIMARY KEY (campaign_id, run_id)
);

CREATE TABLE case_match_ambiguities (
    campaign_id        INTEGER NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    run_id             INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    candidate_ids_json TEXT    NOT NULL DEFAULT '[]',
    resolved           INTEGER NOT NULL DEFAULT 0,
    updated_at         TEXT    NOT NULL,
    PRIMARY KEY (campaign_id, run_id)
);
