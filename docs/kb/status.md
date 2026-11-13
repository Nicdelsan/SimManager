# Status

_Last updated: 2026-11-13_

## Current milestone

**Next: M4 — terminal** (not planned yet; plan to be approved by the user first).

- M3 — web UI: done 2026-11-13 on `main`
  ([roadmap.md#m3](roadmap.md#m3--web-ui)). Awaiting the user's manual check on the
  real clusters.

- M5 — Excel campaigns: backend done on branch `m5-campaigns` (other agent) and merged
  into `main` on 2026-11-13 (migration `0004`). Its web view is built on `main` as part of
  M3. Plan: [roadmap.md#m5](roadmap.md#m5--excel-campaigns-backend).

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
- `prompter-cli`: terminal prompts (`prompter-web` in the browser under `serve`, M3).
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
  backfill, M1 integration via `monitor.polled`.
- CLI: `simman ingest local|status|series`.

M5 backend (2026-11-13, merged into `main`):
- Migration `0004_campaigns.sql`: `projects`, `campaigns` (with `work_root`), `cases`
  (with `parent_case_id`, `manual_state`, `computed_state`, `params_json`, `active`),
  `case_runs`, `case_run_exclusions`, `case_match_ambiguities`.
- Contracts `contracts/campaign.py` (`campaign` v1 `CampaignStore`, `campaign.manager` v1
  `CampaignService`) and durable events `campaign.imported`, `case.matched`,
  `case.unmatched`, `case.state_changed`, `campaign.ambiguous_match`.
- `campaign-sqlite` (`storage_sqlite/campaign.py`) and `campaign-excel`
  (`plugins/campaign_excel/`): Excel preview/idempotent reimport/rename detection,
  `work_root`-scoped matching on `monitor.polled`, immediate state recomputation on
  `run.solver_state_changed`, `solver.finished`, `solver.error`, and `solver.nan_detected`,
  `NNN_MMM` derivation, multi-run computed states (`planned`, `queued`, `running`,
  `completed`, `stopped`, `diverged`, `failed`, `cancelled`, `unknown`), enriched `.xlsx`
  export preserving template sheets/styles after `ws.max_column`.
- CLI (`cli/campaign.py`): `simman campaign import|list|show|match|unmatch|unmatched|export`.

M3 (2026-11-13):
- Durable log holds facts only (`ingest.DURABLE_EVENT_KINDS`, incl. new
  `run.solver_state_changed`); `series.updated`, `run.progress`, `backfill.progress` are
  bus-only; `storage.event_appended` streams the durable log live.
- Progress stored as series `ingest.PROGRESS_SERIES` (wall time → fraction).
- Manual runs: `ingest.track_run`, resumed at start until the solver is done.
- `ui-web`: secure local server (ADR 0005) + WebSocket hub; `simman serve` prints the
  login URL and opens the browser.
- Frontend: dashboard (clusters, active runs, events), runs list + compare, run detail
  (series by file, one plot per unit/x axis, log y for residuals, drag-zoom reloads at
  higher resolution, progress plot, ETA, backfill status, live points), clusters page
  with Reconnect, password/OTP dialog, manual run registration.
- Campaigns page: case table (computed vs manual state, parameters, linked runs),
  never-launched cases, runs not associated, manual association/removal.
- REST API under `/api` (see architecture.md, `ui-web` row); ETA = linear rate of the
  progress series over the last hour of wall time.
- Live WebSocket: subscribed plots get only new points, ≤ 1 update/s per run.
- `prompter-web`: password/OTP/confirmations in the browser, 300 s timeout →
  `AWAITING_AUTH`; `prompter: auto|cli|web`.

## Not yet implemented

Terminal (M4), snapshots, notifications. PBS is on standby (user decision 2026-11-13). Passphrase-protected keys in
the new OpenSSH format would need `bcrypt`: not added, the user has no passphrases.

## M6 (branch m6-snapshots)

- 2026-11-13 — Step 3: implemented `snapshot` plugin (`collector.py`, `diff.py`, `plugin.py`) with `storage.event_appended` + `monitor.polled` auto-capture, `queued -> running` effective replacement, late-file flagging (A300), background oversize SHA-256 + hash reuse, semantic YAML/OpenFOAM diffs, run notes (`run.note_changed`).
- 2026-11-13 — Step 2: implemented `SqliteSnapshotStore` (`snapshot-sqlite`) and `SqliteRuleStore` (`rules-sqlite`) sharing SQLite connection and `write_lock`.
- 2026-11-13 — Step 1: added `desktop-notifier>=6.0`, ADR 0006 (SQLite content-addressed snapshots), migration `0005_snapshots_rules.sql`, and contracts `contracts/snapshot.py`, `contracts/rules.py`, `contracts/notifier.py`.

## Known limitations

- On clusters without accounting (`uni-a`, `uni-b`) a job that starts and ends between
  two polls (up to ~330 s when idle) is never seen. Possible later fix: poll faster
  for a while after a submission is noticed, or read the case directory (M2).

## Open questions for the user

1. When a job is running: `simman monitor --once` + `simman jobs list --all` (verifies
   A2 on real lines); after a job ends on `hpc-national` the same verifies A7.

## Changelog

- 2026-11-13 — M3 closed in roadmap/status; next milestone M4 (terminal).
- 2026-11-13 — README: web UI and campaigns usage.
- 2026-11-13 — M3: campaign view in the web UI (case table with computed and manual
  state, filters, linked runs with unmatch, never-launched cases, runs not associated
  with manual association) on `campaign.manager`.
- 2026-11-13 — Merged `m5-campaigns` (campaign backend, migration `0004`, `simman campaign`)
  into `main`; no conflicts; status sections unified.
- 2026-11-13 — M3 step 7: web frontend (dashboard, runs, run detail with plots per unit,
  progress plot + ETA, zoom → higher resolution, live points, backfill indicator,
  compare, clusters + reconnect, auth dialog, manual run registration); uPlot 1.6.31
  vendored. Checked in headless Chrome on fixture data.
- 2026-11-13 — M3 step 6: `prompter-web` + core key `prompter: auto|cli|web`; OTP login
  end-to-end through the browser tested against the in-process SSH server (OTP absent
  from logs, events and the DB file); key passphrases no longer cached by the transport;
  `uvicorn.error` pinned at ≥ INFO (its DEBUG logs every WebSocket frame).
- 2026-11-13 — M3 step 5: live WebSocket traffic (durable events, cluster state, polls,
  progress, throttled backfill progress, per-run throttled plot points with reload on
  big jumps); `ui-web.live_interval_s` (default 1 s).
- 2026-11-13 — M3 step 4: REST API (clusters with last state/poll, jobs, runs with job,
  solver state and ETA, run detail with files/backfill and series, downsampled points,
  events, reconnect, manual run registration); ETA from the progress series.
- 2026-11-13 — M3 step 3: plugin `ui-web` (uvicorn in the kernel loop, 127.0.0.1 only,
  token → `HttpOnly; SameSite=Strict` cookie, Host/Origin/JSON checks, CSP headers,
  WebSocket hub), ADR 0005. `simman serve` prints the login URL, opens the browser
  (`--no-browser`, `--port`), stops cleanly on Ctrl+C (tested on Windows).
- 2026-11-13 — M3 step 2: storage implements `runs`, `job_for_run` and publishes
  `storage.event_appended`; solver-ingest writes only facts to the durable log
  (`run.solver_state_changed` new), stores progress as a series, tracks manual runs
  (`track_run`, resumed at start until the solver is done). Fixed race: solver state is
  now written after the progress series.
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
