-- Append-only event log: every durable fact (job discovered, state change, confirmed action...).
CREATE TABLE events (
    id         INTEGER PRIMARY KEY,
    ts         TEXT    NOT NULL,          -- ISO 8601, UTC
    kind       TEXT    NOT NULL,          -- dotted name, e.g. job.discovered
    source     TEXT    NOT NULL,          -- plugin that recorded the fact
    cluster_id TEXT,
    run_id     INTEGER,
    payload    TEXT    NOT NULL DEFAULT '{}'  -- JSON object
);

CREATE INDEX events_kind_ts ON events (kind, ts);
CREATE INDEX events_run ON events (run_id) WHERE run_id IS NOT NULL;

CREATE TRIGGER events_no_update BEFORE UPDATE ON events
BEGIN
    SELECT RAISE(ABORT, 'events is append-only');
END;

CREATE TRIGGER events_no_delete BEFORE DELETE ON events
BEGIN
    SELECT RAISE(ABORT, 'events is append-only');
END;
