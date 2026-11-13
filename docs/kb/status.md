# Status

_Last updated: 2026-11-13_

## Current milestone

**M3 — web UI**: in progress on `main` (plan approved 2026-11-13, see
[roadmap.md#m3](roadmap.md#m3--web-ui)). **M5 — campaigns** is developed in parallel by
another agent on branch `m5-campaigns` and owns migration `0004`; M3 uses `0005` if it
needs one.

### M5 (branch m5-campaigns)

- Migration `0004_campaigns.sql`, `contracts/campaign.py` (`campaign` v1,
  `campaign.manager` v1), `campaign-sqlite`, and `campaign-excel` (`.xlsx` import/export,
  `work_root`-scoped auto-matching on `monitor.polled`, `NNN_MMM` derivation, computed
  case states, durable events) implemented and tested (incl. slow test on real sample).
- Next: `simman campaign` CLI (`cli/campaign.py`).

- M1 — SSH and SLURM discovery: done 2026-11-13. All three clusters pass
  `simman cluster test`; real job lines still to be seen (A2, A7).
- M2 — incremental readers and parsers: done and merged into `main` on 2026-11-13
  (`cf643ea`). Plan: [roadmap.md#m2](roadmap.md#m2--incremental-readers-and-parsers).

## What works

M0 (2026-11-13):
- Plugin kernel: lifecycle, reactive DI, LIFO effects, owned tasks, plain + waterfall events,
  layered config with origins, entry point discovery.
- `storage-sqlite`: WAL, numbered migrations, append-only `events` table (triggers).
- `example-clock` / `example-ticker` demo plugins (disabled by default).

M1 (2026-11-13):
- `clusters.yaml` profiles (`simman cluster add|list`), inheriting from `~/.ssh/config`.
- `transport-ssh`: one lazy asyncssh connection per cluster; keys (agent off when
  `key_files` set), encrypted keys (passphrase asked), password, keyboard-interactive/OTP,
  short-lived SSH certificates (expiry checked, renew command after confirmation);
  states CONNECTED/BACKOFF/AWAITING_AUTH/FAILED with no blind auth retries; read-only
  allowlist; `run_mutating` logged; SFTP `stat/listdir/read_range` (for M2).
- `prompter-cli`: terminal prompts.
- `scheduler-slurm`: squeue/sacct/scontrol parsers, accounting auto-detection.
- `monitor`: per-cluster adaptive polling, jobs + runs + events, degraded/recovered.
- Storage migration 0002: `jobs`, `runs`.
- CLI: `simman cluster add|list|test`, `simman monitor [--once]`, `simman jobs list [--all]`,
  plus M0 commands. `serve` now polls the clusters.
- Tests: in-process SSH server (auth paths, certificates, backoff, host key, reconnect,
  SFTP), policy, parsers on anonymized fixtures, monitor on a fake transport, CLI.
  ruff clean; mypy strict on kernel + contracts.

M2 (2026-11-13, merged into `main`):
- `contracts/storage.py` & `storage_sqlite`: `create_run` (`origin='manual'`), `run(run_id)`,
  `RunRecord`.
- `contracts/timeseries.py` (`timeseries` v1): `FileCursor`, `SeriesKey`, `SeriesMeta`,
  `SeriesPoints`, `RunSolverState`, `TimeSeriesStore`.
- `storage_sqlite`: migration `0003_timeseries.sql` (`file_cursors`, `series`,
  `sample_chunks` with float64 blobs + 16:1 and 256:1 min/max levels, `run_solver_state`,
  runs FK) and `timeseries-sqlite` plugin sharing the SQLite connection.
- `contracts/solver.py` (`solver.<kind>` v1): `WatchedFile`, `SolverStatusUpdate`,
  `ParsedChunk`, `Solver`.
- `readers/`: `IncrementalReader` (chunked `read_range`, truncation/replacement detection,
  partial last line, tail-first auto-backfill) + `LocalRemoteSession`.
- `solver-converge`, `solver-openfoam`: discovery + pure parsers (OpenFOAM
  `postProcessing/**` deferred until a sample exists).
- `contracts/ingest.py` (`ingest` v1) and `solver-ingest`: solver detection, sync, background
  backfill, M1 integration via `monitor.polled`, durable events `series.updated`,
  `run.progress`.
- CLI: `simman ingest local|status|series`.

## Not yet implemented

Web UI (M3), terminal, campaigns, snapshots, notifications. PBS is on standby (user decision 2026-11-13). Passphrase-protected keys in
the new OpenSSH format would need `bcrypt`: not added, the user has no passphrases.

## Known limitations

- On clusters without accounting (`uni-a`, `uni-b`) a job that starts and ends between
  two polls (up to ~330 s when idle) is never seen. Possible later fix: poll faster
  for a while after a submission is noticed, or read the case directory (M2).

## Open questions for the user

1. When a job is running: `simman monitor --once` + `simman jobs list --all` (verifies
   A2 on real lines); after a job ends on `hpc-national` the same verifies A7.

## Changelog

- 2026-11-13 — M3 step 1: contracts (`storage.runs/job_for_run/EVENT_APPENDED`,
  `ingest.track_run/PROGRESS_SERIES/DURABLE_EVENT_KINDS`, new `ui.web`) and dependencies
  `fastapi`, `uvicorn`, `websockets` (dev: `httpx`). Implementations follow.
- 2026-11-13 — M2 merged into `main` (`cf643ea`); status corrected (was "branch
  m2-parsers"). M3 planning started.
- 2026-11-13 — M1 verified on the three real clusters (`simman cluster test`): A3
  verified, A2 partially; `uni-b` is Slurm 22.05.9 without accounting.
- 2026-11-13 — transport-ssh: clear hint when a Windows `IdentityFile` path in the SSH
  config was mangled (backslashes); KB notes on the user's SSH setup.
- 2026-11-13 — M1 implemented: transport-ssh, prompter-cli, scheduler-slurm, monitor,
  migration 0002 (jobs, runs), storage contract extended with jobs, monitor contract,
  CLI `cluster`/`monitor`/`jobs`, ADR 0004.
- 2026-11-13 — M1 contracts committed (clusters, transport, auth, scheduler) + dependencies
  `asyncssh`, `tzdata` (Windows only, for IANA time zones).
- 2026-11-13 — `tools/dev_setup.py` + Claude Code SessionStart hook: git hooks enabled and
  privacy denylist generated automatically from local sources on every machine.
- 2026-11-13 — Knowledge base created (`AGENTS.md`, `docs/kb/`, KB pre-commit hook and test).
- 2026-11-13 — M0 completed and pushed: kernel, SQLite storage, CLI, docs, ADR 0001–0002.
