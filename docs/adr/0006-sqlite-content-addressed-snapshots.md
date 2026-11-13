# ADR 0006 — Content-addressed input snapshots in SQLite

- Status: accepted (M6)

## Context

The initial project brief suggested storing input snapshots in a local Git repository (one
commit per snapshot). In practice SimManager runs on both Windows and Linux, already keeps
all durable state (events, jobs, runs, time series, campaigns) in a single SQLite database
([ADR 0002](0002-sqlite-aiosqlite-plain-sql.md)), and tracks runs that are frequently
relaunched or derived (`NNN_MMM`) with mostly identical input files plus a few edited keys.

Managing a local `.git` working tree introduces operational friction:
- external dependency on a `git` executable in `PATH`;
- index lock files (`.git/index.lock`) and line-ending / path issues across Windows and Linux;
- subprocess overhead for every snapshot, diff, and blob lookup;
- split backup story (database file + separate Git directory).

## Decision

Store input snapshots in the shared SQLite database using a **content-addressed** model:
- `snapshot_blobs`: immutable file contents keyed by `sha256`, compressed with `zlib`.
- `snapshots`: one metadata row per capture (`run_id`, `trigger`, `effective`, `late`,
  `manifest_sha256`), without modifying existing data when a new snapshot is taken.
- `snapshot_files`: manifest entries linking each `(snapshot_id, rel_path)` to its size,
  `mtime`, `content_status`, and optional `sha256` (allowing large files above the capture
  limit to record size, `mtime`, and hash without storing their bytes).

`SnapshotStore` (`contracts/snapshot.py`) exposes `get_blob(sha256)` and manifest queries,
so a separate Git export plugin can be built later if ever needed without changing the schema
or consumer contracts.

## Alternatives

- **Local Git repository (`git init` + `git commit`)**: rejected for the MVP due to the
  external `git` dependency, cross-platform subprocess/locking concerns, and split backup.

## Consequences

- Zero external binaries needed for snapshots and diffs; single-file backup via `simmanager.db`.
- Automatic deduplication across `queued -> running` captures, restarts, and derived cases.
- Textual and semantic diffs run directly on stored blobs in pure Python (`difflib`, PyYAML,
  OpenFOAM dictionary parser).
