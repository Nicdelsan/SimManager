-- Scheduler jobs (latest known facts) and runs (one execution of a case).
-- History of changes lives in the append-only events table.
CREATE TABLE jobs (
    id           INTEGER PRIMARY KEY,
    cluster_id   TEXT    NOT NULL,
    job_id       TEXT    NOT NULL,          -- scheduler id; array tasks look like 123_4
    name         TEXT    NOT NULL DEFAULT '',
    user         TEXT,
    state        TEXT    NOT NULL,          -- normalized (queued, running, ..., unknown)
    raw_state    TEXT    NOT NULL DEFAULT '',
    reason       TEXT,
    work_dir     TEXT,
    partition    TEXT,
    nodes        INTEGER,
    node_list    TEXT,
    exit_code    TEXT,
    elapsed_s    INTEGER,
    time_limit_s INTEGER,
    submit_time  TEXT,                      -- ISO 8601 with offset
    start_time   TEXT,
    end_time     TEXT,
    stdout_path  TEXT,
    active       INTEGER NOT NULL DEFAULT 1, -- 1 while the job is in the scheduler queue
    first_seen   TEXT    NOT NULL,          -- ISO 8601, UTC
    last_seen    TEXT    NOT NULL,
    UNIQUE (cluster_id, job_id)             -- ASSUMPTION(A1): ids are not reused
);

CREATE INDEX jobs_active ON jobs (cluster_id) WHERE active = 1;
CREATE INDEX jobs_work_dir ON jobs (work_dir);

CREATE TABLE runs (
    id         INTEGER PRIMARY KEY,
    job_pk     INTEGER UNIQUE REFERENCES jobs (id),  -- NULL for manually registered runs
    origin     TEXT    NOT NULL CHECK (origin IN ('discovered', 'manual')),
    cluster_id TEXT,
    work_dir   TEXT,
    created_at TEXT    NOT NULL
);

CREATE INDEX runs_work_dir ON runs (work_dir);
