# Status

_Last updated: 2026-11-13_

## M2 (branch m2-parsers)

In progress since 2026-11-13 (parallel worktree on branch `m2-parsers`, separate from M1 on `main`).
Plan: [roadmap.md#m2](roadmap.md#m2--incremental-readers-and-parsers).

### Implemented in M2 so far

- `contracts/timeseries.py` (`timeseries` v1): `FileCursor`, `SeriesKey`, `SeriesMeta`, `SeriesPoints`, `RunSolverState`, `TimeSeriesStore` protocol.
- `storage_sqlite`: migration `0003_timeseries.sql` (`file_cursors`, `series`, `sample_chunks` with float64 `array('d')` blobs + 16:1 and 256:1 min/max downsampled levels, `run_solver_state`) and `timeseries-sqlite` plugin (`SqliteTimeSeriesStore`) sharing the single SQLite connection.

### To wire at M1+M2 merge

- `0003_timeseries.sql` stores `run_id INTEGER NOT NULL` without a hard `FOREIGN KEY REFERENCES runs(id)` because M1 migration `0002` lives on `main`; add the FK check/migration after merging M1 and M2.

## Current milestone

**M1 — SSH and SLURM discovery** (in progress since 2026-11-13).
Plan: [roadmap.md#m1](roadmap.md#m1--ssh-and-slurm-discovery).
M1 contracts are committed (`contracts/clusters.py`, `transport.py`, `auth.py`,
`scheduler.py`) so that M2 can start in parallel in a separate worktree; the plugins that
implement them are still being written.

## What works (M0, done 2026-11-13)

- Plugin kernel: lifecycle, reactive DI, LIFO effects, owned tasks, plain + waterfall events,
  layered config with origins, entry point discovery.
- `storage-sqlite`: WAL, numbered migrations, append-only `events` table (triggers).
- `example-clock` / `example-ticker` demo plugins (disabled by default).
- CLI: `simman version | plugins | serve | events | config show [--origin] | config paths`.
- 47 tests green; ruff clean; mypy strict on kernel + contracts.

## Not yet implemented

SSH transport, SLURM scheduler, monitor loop, solver parsers, web UI, terminal, campaigns,
snapshots, notifications. PBS is on standby (user decision 2026-11-13).

## Open questions for the user

1. OTP authentication on `hpc-national` (see clusters.md): user will provide the script they run to authenticate
   (not yet in `samples/`).
2. A real `squeue` output with the user's own jobs, using the format in
   [domain.md](domain.md#slurm). Otherwise fixtures stay synthetic and marked as assumptions.

## Changelog

- 2026-11-13 — M1 contracts committed (clusters, transport, auth, scheduler) + dependencies
  `asyncssh`, `tzdata` (Windows only, for IANA time zones). Plugins pending.

- 2026-11-13 — `tools/dev_setup.py` + Claude Code SessionStart hook: git hooks enabled and
  privacy denylist generated automatically from local sources on every machine.

- 2026-11-13 — Knowledge base created (`AGENTS.md`, `docs/kb/`, KB pre-commit hook and test).
- 2026-11-13 — M0 completed and pushed: kernel, SQLite storage, CLI, docs, ADR 0001–0002.
