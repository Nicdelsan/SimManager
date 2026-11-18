# Status

_Last updated: 2026-11-18_

## Current milestone

- Display (2026-11-18, branch `vk/7670-aggiunta-display`): panel in the run detail that runs
  the user's Python scripts on the cluster and shows the images/GIFs they make
  ([ADR 0008](../adr/0008-display-scripts.md), migration `0006`). Awaiting the user's check
  on the real clusters (A402).

- M6 — snapshots, rules, notifications: backend + CLI done on branch `m6-snapshots` and
  merged into `main` on 2026-11-17 (migration `0005`,
  [ADR 0006](../adr/0006-sqlite-content-addressed-snapshots.md),
  [roadmap.md#m6](roadmap.md#m6--snapshots-rules-notifications)).

- M4 — integrated SSH terminal: done 2026-11-13 on `main`
  ([roadmap.md#m4](roadmap.md#m4--integrated-ssh-terminal),
  [ADR 0007](../adr/0007-integrated-terminal.md)). Awaiting the user's manual check on
  the real clusters (full-screen `vi`, `top`/`htop`, `less`, colours, Ctrl+C during
  `sleep`, copy/paste, re-attach after F5, `module load`; A400 shells, A401 MaxSessions).

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
  higher resolution, progress plot, ETA, backfill status, live points with auto-rescaled
  axes, cursor tooltip with x/y of the nearest point, scientific notation for tiny or huge
  values), clusters page
  with Reconnect, password/OTP dialog, manual run registration.
- Campaigns page: case table (computed vs manual state, parameters, linked runs),
  never-launched cases, runs not associated, manual association/removal.
- REST API under `/api` (see architecture.md, `ui-web` row); ETA = linear rate of the
  progress series over the last hour of wall time.
- Live WebSocket: subscribed plots get only new points, ≤ 1 update/s per run.
- `prompter-web`: password/OTP/confirmations in the browser, 300 s timeout →
  `AWAITING_AUTH`; `prompter: auto|cli|web`.

M4 (2026-11-13):
- `transport.open_pty` (login shell, folder checked over SFTP, optional tmux) on a
  separate per-cluster pool `max_terminals` (3); `tmux ls` allowlisted (read-only).
- Plugin `terminal` (web-only): sessions with 1 MiB replay buffer, single attachment with
  takeover, orphan expiry (30 min), backpressure, one explicit reconnect when the cluster
  awaits authentication; durable `terminal.opened/closed` (metadata only).
- `ui-web`: `/api/terminals` + single-use tickets + `/ws/term/{id}`; page `/terminal`
  (tabs, xterm.js 6.0.0, clipboard, re-attach after reload, tmux menu); entry points on
  cluster cards and run detail; quick actions inserted without Enter; "Cancel job…" with
  server-enforced confirmation (`run_mutating`, `action.*` events).
- Tests: fake PTY shell in the test SSH server; transport, service, web (tickets,
  Origin/Host/cookie, re-attach, auth prompt then shell, channel limit with the monitor
  working, cancel job, no terminal content in logs/events). Headless Chrome smoke test.

M6 backend + CLI (2026-11-17, merged into `main`):
- Migration `0005_snapshots_rules.sql` and ADR 0006 (`snapshot_blobs`, `snapshots`,
  `snapshot_files`, `run_notes`, `rule_configs`, `alerts`, `rule_run_state`).
- Contracts `contracts/snapshot.py`, `contracts/rules.py`, `contracts/notifier.py` and
  SQLite stores `snapshot-sqlite` and `rules-sqlite`.
- `snapshot` plugin: auto-capture on `storage.event_appended` + `monitor.polled`,
  `queued -> running` effective replacement, late-file flagging (A300), background oversize
  SHA-256 + hash reuse, semantic YAML/OpenFOAM diffs, run notes (`run.note_changed`).
- `rules` plugin: hierarchical config merge (`global -> campaign -> case`), incremental
  series evaluation (`courant_high`, `residual_high` with warmup + restart reset,
  `monitor_range`), `nan_inf`, `solver_error`, `job_failed`, adaptive `stall`
  (backfill/disconnect-safe), and restart deduplication.
- `notifier` router + `notifier-desktop` (`desktop-notifier`): immediate-first + coalescing
  batching, quiet hours, rate limit, secret-free event formatting, and silent fallback.
- CLI (`cli/snapshot.py`, `cli/rules.py`): `simman snapshot {list,show,diff,take}`,
  `simman run note`, `simman rules {show,set,unset}`, `simman alerts {list,ack}`, and
  `simman notify test`.

Display (2026-11-18):
- Rule 2 changed with the user: monitoring stays read-only; cluster changes need a UI
  approval and are logged (ADR 0008).
- `run_mutating(..., input=)`: stdin to the remote command, never in the event log.
- Migration `0006_display.sql`, contract `display`, plugins `display-sqlite` and `display`
  (web-only): versioned script library (immutable versions, SHA-256, notes, diffs),
  executions on the run folder via login `bash -lc` (setup lines, GNU `timeout`), new
  images found with `find -newer` and copied into SQLite (≤ 20 per execution, ≤ 20 MB
  each, "Download anyway" for bigger), image history per run, read-only folder browser,
  auto-refresh every N min while the job is `RUNNING`, durable `display.executed` /
  `display.schedule_changed`.
- Run detail UI: Display panel (Image / History / Files tabs; script editor with library,
  setup, interpreter, versions + diff, Run, auto-refresh), live via `display.updated`.
- Tests: command building/parsing/path safety, store versioning, execution, size limits,
  interruption, browser, schedules, web endpoints, SSH stdin. Checked in headless Chrome
  with a fake cluster.

## Not yet implemented

PBS is on standby (user decision 2026-11-13). Passphrase-protected keys in
the new OpenSSH format would need `bcrypt`: not added, the user has no passphrases.

## Known limitations

- On clusters without accounting (`uni-a`, `uni-b`) a job that starts and ends between
  two polls (up to ~330 s when idle) is never seen. Possible later fix: poll faster
  for a while after a submission is noticed, or read the case directory (M2).

- Display image history is never pruned: the database grows with every execution (GIFs
  up to 20 MB each). Identical images are stored once.
- Display scripts run on the login node: heavy post-processing there may break cluster
  rules (`timeout_s` 300 s and one script per cluster at a time limit the impact).

## Open questions for the user

0. Display: run a script on each cluster (verifies A402: bash, GNU coreutils/find on the
   login node) and say which `module load` line your Python with matplotlib needs.

1. M4: which login shell runs on each cluster (A400 assumes bash/zsh); and a manual check
   of the terminal on the real clusters (see "Current milestone").
2. When a job is running: `simman monitor --once` + `simman jobs list --all` (verifies
   A2 on real lines); after a job ends on `hpc-national` the same verifies A7.

## Changelog

- 2026-11-18 — Fix frozen run progress: progress derived from start/end/current solver time
  (OpenFOAM always, CONVERGE when stdout has no percentage) was computed only once, so the
  progress bar and the progress-over-time plot stopped after the first sync. Now recomputed
  whenever the solver time advances; an explicit solver percentage still wins.
- 2026-11-18 — Display panel (ADR 0008): user Python scripts run on the cluster in the run
  folder, images/GIFs shown with history and folder browser, versioned script library,
  auto-refresh; migration `0006`; AGENTS.md rule 2 updated (read-only monitoring, explicit
  user-approved actions).

- 2026-11-18 — Plot fixes: axes rescale on live points (they stayed frozen on the first
  load); axis ticks, legend and new cursor tooltip use `js/plotfmt.js` (uPlot's default
  formatter showed residuals and small times as "0"); cursor skips join/log gaps so every
  series shows its nearest value.
- 2026-11-17 — Merged `m6-snapshots` (M6 snapshots, rules, desktop notifications, migration
  `0005`, ADR 0006, CLI commands) into `main`; resolved conflicts in KB and aligned status,
  roadmap, and architecture.
- 2026-11-13 — M4 done: ADR 0007 (why the terminal does not break the read-only rule),
  README terminal section, roadmap/architecture updated.
- 2026-11-13 — M4 step 6: "Cancel job…" in the run detail and in the terminal toolbar.
  Server-enforced two steps: `POST /api/actions/job-cancel/prepare` (only active jobs
  known to SimManager; single-use `action_id`, 60 s) and `POST /api/actions/{id}/confirm`
  → `run_mutating(scheduler.cancel_argv)` with `ActionApproval("job.cancel", "web")`
  (`action.executed` / `action.result` in the event log), then a poll of the cluster.
  Confirmation dialog shows job id, name, cluster and folder. UI only (no CLI).
- 2026-11-13 — M4 step 5: "Terminal" drop-down on cluster cards (home or a work root) and
  button in the run detail (run folder); both reuse the `simman-terminal` window.
  `GET /api/runs/{id}/quick-actions`: drop-down menus Folder / Log / Job / Edit input
  (`vi` on every `.in`, OpenFOAM `system/*Dict`) that insert a shell-quoted command
  without Enter; input files are listed only if the cluster is already connected (no
  login from a page load). "Reconnect" asks first when terminals are open.
- 2026-11-13 — M4 step 4: page `/terminal` (own window `simman-terminal`): tabs, xterm.js
  6.0.0 + addon-fit 0.11.0 vendored (MIT, SHA-256 checked by a test), fit on resize,
  UTF-8, Ctrl+C copies a selection (else SIGINT), Ctrl+V pastes, re-attach of every
  session after a reload (replay + SIGWINCH nudge), "+ Terminal" menu (home, work roots,
  new tmux session, re-attach a live `simman-*` tmux session), auth dialog, notice that
  terminals end with SimManager. Smoke-tested in headless Chrome (no CSP violations).
- 2026-11-13 — M4 step 3: `ui-web` terminal endpoints (`/api/terminals`, single-use
  tickets, `/ws/term/{id}` binary WebSocket, tmux listing), per-page CSP for `/terminal`
  (ADR 0005 updated), `uvicorn.access` disabled outright (URLs carry token/tickets),
  `/api/clusters` reports terminal limits; tests for tickets, Origin/Host/cookie,
  re-attach/takeover, close, auth prompt then shell, no terminal content in logs/events.
- 2026-11-13 — M4 step 2: contract `terminal` v1 and web-only plugin `terminal`
  (sessions, 1 MiB replay buffer, single attachment with takeover, orphan expiry 30 min,
  backpressure, one explicit reconnect for clusters awaiting auth, tmux naming above
  every live `simman-<cluster>-<n>`), durable `terminal.opened/closed`.
- 2026-11-13 — M4 step 1: `transport` contract gets `RemoteSession.open_pty` (login
  shell, optional `cwd` checked over SFTP, optional tmux), `PtyChannel`, `TmuxSpec`,
  `TerminalLimitError`, `RemoteDirectoryMissingError`; profiles get `max_terminals`
  (default 3, own pool, never the monitor's channels) and `terminal_tmux`; read-only
  allowlist gets `tmux ls|list-sessions` (no `#(...)` formats). Fake PTY shell in the
  test SSH server.
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
