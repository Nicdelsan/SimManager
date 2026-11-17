# Architecture

SimManager is a small plugin kernel plus plugins. Everything domain-specific — SSH,
schedulers, solvers, storage, UI, notifications — is a plugin publishing or consuming
**services** through versioned **contracts**. Decisions are recorded in `docs/adr/`.

```
            ┌───────────────────────── kernel (generic, no CFD) ─────────────────────────┐
            │  Kernel: lifecycle + reactive DI   ServiceRegistry   EventBus   Config       │
            └─────────────────────────────────────────────────────────────────────────────┘
                 ▲ provides "storage"     ▲ provides "transport"   ▲ "scheduler.slurm"
   storage-sqlite                transport-ssh (M1)          scheduler-slurm (M1)
                 ▲ inject                  ▲ inject                 ▲ looked up at runtime
                 └──────────────── monitor (M1/M2) ─────────────────┘
```

## Kernel (`src/simmanager/kernel`)

| Concept | Where | Notes |
|---|---|---|
| Plugin | `plugin.py` | `name`, `provides`, `contract_version`, `inject`, `config_schema`, `apply(ctx)` (sync or async). New instance per activation. |
| Lifecycle | `kernel.py` | `PENDING → LOADING → ACTIVE → UNLOADING → DISPOSED`, plus `FAILED`. State changes are emitted as `kernel.plugin_state`. |
| Reactive DI | `kernel.py` | Missing dependency → `PENDING`. Provider appears → consumers activate. Provider leaves → consumers are disposed (consumers first, newest first) and go back to `PENDING`. |
| Services | `services.py` | Dotted keys (`scheduler.slurm`). `inject` lists only mandatory single services; optional/multiple ones use `ctx.services.get()` / `.all("scheduler")` at use time. Contract major-version mismatch → consumer `FAILED`. Two providers of one key → the second is `FAILED`. |
| Context | `context.py` | One per activation. `effect`, `add_disposer`, `task`, `on`, `waterfall_on`, `emit`, `waterfall`. Closed after deactivation. |
| Effects | `effects.py` | Disposers run LIFO; a failing disposer is logged and does not stop the others. `ctx.task` is an effect: cancelled and awaited on unload. A task that dies with an exception marks its plugin `FAILED` and deactivates its dependents. |
| Events | `events.py` | In-memory only. Plain events: ordered by priority, failing handler isolated. Waterfall: each handler returns the transformed payload or raises `Blocked`. |
| Config | `config.py` | YAML layers deep-merged (lists replaced), per-key origin tracking, validation errors reported with the full key path. |
| Discovery | `discovery.py` | Built-ins + entry point group `simmanager.plugins` (a class or a list of classes). Broken entry points are reported, not raised. |

All kernel transitions are serialized by one `asyncio.Lock`; plugins never get a kernel
reference, so they cannot deadlock it.

## Golden rules

1. **Durable state ≠ plugin state.** Jobs, runs, snapshots and samples live in storage
   (`events` table is append-only, enforced by triggers). Unloading or reloading any plugin
   only closes connections and cancels tasks; it never deletes data (tested).
2. **Irreversible actions never live in disposers.**
3. **Restart loses nothing**: monitoring resumes from offsets stored in the database (M2).
4. **No speculative abstractions**: tables and contracts are added by the milestone that
   needs them.

## Application layer

- `app.py`: discovers plugins, composes `defaults → user → project` configuration, decides
  which plugins are enabled (`plugins.<name>.enabled`), starts the kernel.
- `paths.py`: config in `%APPDATA%\SimManager` / `~/.config/simmanager`; data and logs in
  `%LOCALAPPDATA%\SimManager` / `~/.local/share/simmanager`.
- `logging_setup.py`: JSON lines to a rotating file (`<data>/logs/simmanager.log`),
  warnings on the console.
- `cli/main.py`: `simman` (Typer).

## Contracts (`contracts/`)

Consumers import only these modules, never plugin implementations. Each has a
`CONTRACT_VERSION` (major); a breaking change bumps it and is named in the commit message.

| Module | Service key | Since | Content |
|---|---|---|---|
| `storage.py` | `storage` | M0 | Append-only event log (`append_event`, `events`, `latest_events`); jobs/runs (`upsert_job`, `jobs`, `create_run`, `run`, `JobRecord`, `JobUpsert`, `RunRecord`); since M3 `runs`, `job_for_run` and bus event `storage.event_appended` (every durable event, live). |
| `monitor.py` | `monitor` | M1 | `Monitor.poll/poll_all` → `PollReport`; bus event `monitor.polled`. |
| `clusters.py` | — (data) | M1 | `ClusterProfile` / `ClustersFile` (clusters.yaml), `PollConfig`. |
| `transport.py` | `transport` | M1 | `Transport.session/state/reset/clusters`; `RemoteSession.run_readonly/run_mutating/stat/listdir/read_range`, `open_pty` (M4, `PtyChannel`, `TmuxSpec`); `ConnectionState`; errors `TransportError`, `ClusterUnavailableError`, `CommandNotAllowedError`, `TerminalLimitError`, `RemoteDirectoryMissingError`; bus event `transport.state`. |
| `auth.py` | `auth.prompter` | M1 | `AuthPrompter.ask(AuthRequest)` / `confirm`: passwords, passphrases, OTP, yes/no. |
| `scheduler.py` | `scheduler.<kind>` | M1 | `JobState`, `JobInfo`, `Scheduler.list_active/final_states/cancel_argv` (stateless; receives the `RemoteSession`). |
| `timeseries.py` | `timeseries` | M2 | `TimeSeriesStore`, `FileCursor`, `SeriesKey`, `SeriesMeta`, `SeriesPoints`, `RunSolverState` (chunked float64 time series + cursors + solver run state). |
| `solver.py` | `solver.<kind>` | M2 | `Solver` (`kind`, `discover`, `parse_header`, `parse_chunk`, `align_offset`), `WatchedFile`, `ParsedChunk`, `SolverStatusUpdate`. |
| `ingest.py` | `ingest` | M2 | `IngestService.sync_run` (`SyncRunResult`): orchestrates solver detection, incremental reading, backfill and event emission. Since M3: `track_run` (manual runs), `PROGRESS_SERIES` (progress vs wall time in the timeseries store), `DURABLE_EVENT_KINDS` (only facts go to the durable log; `series.updated`, `run.progress`, `backfill.progress` are bus-only). |
| `ui.py` | `ui.web` | M3 | Local web UI as seen by plugins: `base_url`, `client_count`, `broadcast`, `on_message`, `on_client_connected` (shared dashboard WebSocket; per-session channels such as the M4 terminal will be an additive extension). |
| `terminal.py` | `terminal` | M4 | Interactive shells owned by the user: `open(TerminalRequest)` (connects/authenticates in background), `sessions`, `get`, `attach` → `TerminalAttachment` (`replay`, `recv` bytes / `TerminalInfo` / None, `write`, `resize`, `detach`; one attachment per session, a new one takes over), `close`, `tmux_sessions`. `TerminalState`, `TerminalError`. Bus event `terminal.state`; durable `terminal.opened` / `terminal.closed` (metadata only). |
| `campaign.py` | `campaign`, `campaign.manager` | M5 | `CampaignStore` (projects, campaigns, cases, run links, exclusions, ambiguities) and `CampaignService` (Excel preview/import/export, run matching, computed states, durable events). |
| `snapshot.py` | `snapshot`, `snapshot.manager` | M6 | `SnapshotStore` (content-addressed SQLite blobs, manifests, run notes) and `SnapshotService` (input capture, oversize background hashing, textual & semantic diffs). |
| `rules.py` | `rules`, `rules.manager` | M6 | `RuleStore` (layered rule configs, open/cleared alerts, incremental run state) and `RuleEngine` (hysteresis evaluation, adaptive stall, acknowledgment). |
| `notifier.py` | `notifier.router`, `notifier.<kind>` | M6 | `NotifierProvider` (`notifier.desktop`, …) and `NotificationService` (routing, immediate-first batching, quiet hours, rate limit). |

## Storage (`plugins/storage_sqlite`)

SQLite in WAL mode via `aiosqlite`, hand-written SQL, numbered migrations in
`migrations/NNNN_name.sql` applied once each in a transaction (`schema_migrations`).

| Table | Since | Purpose |
|---|---|---|
| `events` | M0 | Append-only log of facts: `id, ts (UTC), kind, source, cluster_id, run_id, payload JSON`. |
| `jobs` | M1 | Latest known facts of each scheduler job, `UNIQUE(cluster_id, job_id)`; `active` = still in the queue. Updates keep stored values for fields the scheduler did not report. |
| `runs` | M1 | One execution of a case: `job_pk` (unique, NULL for manual runs), `origin` discovered/manual, `work_dir`. Created with the job in one transaction. |
| `file_cursors` | M2 | Incremental byte offsets (`forward_offset`, `backfill_start`, `backfill_end`), 256-byte prefix and parser state per `(run_id, file_path)` (`REFERENCES runs(id) ON DELETE CASCADE`). |
| `series` | M2 | Discovered time-series metadata (`run_id, file_path, name, qualifier, unit`, summary stats; `REFERENCES runs(id) ON DELETE CASCADE`). |
| `sample_chunks` | M2 | Dense float64 `array('d')` chunks (`level=0`) and 16:1 / 256:1 min/max pyramids (`level=1, 2`). |
| `run_solver_state` | M2 | Latest solver state, progress, times, `ncyc`, warnings and case/output dirs per `run_id` (`REFERENCES runs(id) ON DELETE CASCADE`). |
| `projects`, `campaigns`, `cases`, `case_runs`, `case_run_exclusions`, `case_match_ambiguities` | M5 | Project → Campaign (`work_root`, `naming_regex`) → Case (`parent_case_id`, `manual_state`, `computed_state`, `params_json`) → Run links (manual lock, exclusions, unresolved ambiguities). |
| `snapshot_blobs`, `snapshots`, `snapshot_files`, `run_notes`, `rule_configs`, `alerts`, `rule_run_state` | M6 | Content-addressed input snapshots (ADR 0006), mutable run notes, layered detection rules, deduplicated alerts, and incremental evaluation/stall state. |

## M1 plugins

| Plugin | Provides | Injects | Notes |
|---|---|---|---|
| `prompter-cli` | `auth.prompter` | — | getpass/input; answers None when stdin is not a TTY. |
| `prompter-web` (M3) | `auth.prompter` | `ui.web` | Prompts over the dashboard WebSocket (`auth.prompt`, `auth.confirm`, `auth.closed`; answers `auth.answer`, `auth.confirm_answer`, `auth.cancel`). First answer wins; late browsers get pending prompts; timeout `prompt_timeout_s` (300 s) → no answer → `AWAITING_AUTH`. Exactly one prompter runs: core key `prompter: auto\|cli\|web` (auto = web under `serve`, cli otherwise), applied by `app.prepare(web=...)`. |
| `transport-ssh` | `transport` | `storage` | asyncssh, one lazy connection per cluster, read-only policy and auth states ([ADR 0004](adr/0004-read-only-ssh-and-auth-states.md)). Reads `clusters.yaml`. |
| `scheduler-slurm` | `scheduler.slurm` | — | Pure parsers in `parse.py`; `sacct`, falling back to `scontrol` when accounting is disabled (remembered per cluster). |
| `monitor` | `monitor` | `storage`, `transport` | One poll loop per cluster (adaptive 60/180/300 s ±10 %); scheduler looked up as `scheduler.<profile.scheduler>`. `loop: false` = on demand only (CLI). |
| `terminal` (M4) | `terminal` | `storage`, `transport` | Web-only (`WEB_ONLY_PLUGINS`). One PTY per session (`transport.open_pty`, own `max_terminals` pool); opening on a cluster in `AWAITING_AUTH`/`BACKOFF`/`FAILED` makes one explicit reconnect (prompt in the browser), serialized per cluster. Ring buffer (`buffer_bytes`, 1 MiB) replayed on re-attach; detached sessions closed after `orphan_timeout_s` (30 min); all closed on stop. Output to a slow browser is backpressured (≤ 64 queued chunks, then the PTY is not read). tmux: new session `simman-<cluster>-<n>` with n above every live one (never `-A`), or explicit attach. No terminal data in logs/events. |
| `ui-web` (M3) | `ui.web` | — | FastAPI + uvicorn in the kernel loop on 127.0.0.1 only; token → cookie, Host/Origin checks, CSP ([ADR 0005](adr/0005-local-web-server-security.md)). Started only by `simman serve` (`WEB_ONLY_PLUGINS`). Dashboard WebSocket hub with per-client bounded queues. REST API (`api.py`, JSON under `/api`): `GET clusters, jobs, runs, runs/{id}, runs/{id}/series, series/{id}/points?x_min&x_max&width, events`; `POST clusters/{id}/reconnect`, `POST runs` (manual run → `storage.create_run` + durable `run.created` + `ingest.track_run`). Services looked up per request (503 when missing). ETA and file read fraction in `derive.py`. WebSocket `/ws` server→browser: `hello`, `cluster.state`, `monitor.polled`, `event` (every durable event), `run.progress`, `backfill.progress` (≤ 1/s per file), `series.updated`, `points` (new points of subscribed series, ≤ 1 flush per `live_interval_s` per run), `series.reload` (> 5000 new points), `error`; browser→server: `subscribe {run_id, series:[{id,last_x}]}`, `unsubscribe` (`live.py`). Frontend in `static/`: vanilla ES modules (`js/`, hash router: dashboard, runs, run detail, compare, clusters, register run), vendored uPlot 1.6.31 (`static/vendor/`, MIT, README with source and SHA-256); no build step, works offline; no `innerHTML`, no inline scripts/styles (CSP). Campaign view (`campaigns.py`, uses `campaign.manager`): `GET campaigns`, `GET campaigns/{id}` (cases with computed + manual state and linked runs, never-launched cases, runs not associated), `POST campaigns/{id}/match {case_key, run_id}` (locked manual link), `POST campaigns/{id}/unmatch {run_id}` (exclusion). Terminal (M4, `terminal.py`, uses `terminal`): `GET terminals`, `POST terminals {cluster_id, cwd?, run_id?, cols, rows, tmux?, attach_tmux?}` → `{session, ticket}`, `POST terminals/{id}/ticket`, `POST terminals/{id}/close`, `GET clusters/{id}/tmux`, `POST actions/job-cancel/prepare {cluster_id, job_id}` → single-use `action_id` (60 s, active known jobs only) and `POST actions/{id}/confirm` → `run_mutating(cancel_argv)` with `ActionApproval(job.cancel, web)` (`actions.py`, the only mutating action of the UI), `GET runs/{id}/quick-actions` (`quick.py`: commands inserted without Enter, absolute shell-quoted paths; SFTP listing of inputs only when already connected); WebSocket `/ws/term/{id}?ticket=` (single-use ticket, 30 s; binary frames = terminal bytes, input ≤ 64 KiB; JSON `resize` in, `replay`/`state` out; close 1000 closed, 4001 taken over, 1009 frame too big); dashboard WS also gets `terminal.state`. Page `/terminal` (`terminal.html`, `js/terminal/`: `main.js` tabs/menu/restore, `tab.js` one xterm.js session, `actions.js` quick actions; vendored xterm.js 6.0.0 + addon-fit 0.11.0) is the only one with `style-src 'unsafe-inline'`. `GET clusters` includes `work_roots` and `terminals {available, max, tmux, open}`. |

Durable events written in M1: `cluster.connected`, `cluster.connection_lost`,
`cluster.unreachable`, `cluster.auth_required`, `cluster.failed`,
`cluster.cert_renew_started|finished|failed`, `cluster.degraded`, `cluster.recovered`,
`job.discovered`, `run.created`, `job.state_changed`, `action.executed`, `action.result`.
Durable events written by `solver-ingest` (M2/M3, `ingest.DURABLE_EVENT_KINDS`, facts only):
`run.solver_state_changed`, `solver.finished`, `solver.error`, `solver.nan_detected`,
`file.truncated`.
Durable events written by `terminal` (M4, never terminal content): `terminal.opened`
`{session_id, cwd, tmux_session}`, `terminal.closed` `{session_id, cwd, reason, duration_s,
exit_status}` (reason `user|exited|connection_lost|expired|shutdown`).
Bus-only events: `terminal.state`, `transport.state`, `monitor.polled`, `series.updated`, `run.progress`,
`backfill.progress`, `storage.event_appended` (each durable event right after commit, so
the UI can stream the log live).
Progress is stored as a timeseries (`ingest.PROGRESS_SERIES`: x = wall time, y = 0..1);
the solver state row is written last in a sync and acts as its commit marker.

## M6 plugins

| Plugin | Provides | Injects | Notes |
|---|---|---|---|
| `snapshot-sqlite` | `snapshot` | `storage` | Content-addressed blob + file manifest store (`snapshot_blobs`, `snapshots`, `snapshot_files`, `run_notes`), sharing the SQLite connection and `write_lock`. |
| `rules-sqlite` | `rules` | `storage` | Layered rule configurations (`rule_configs`), deduplicated alerts (`alerts`), and per-run incremental evaluation/stall state (`rule_run_state`). |
| `snapshot` | `snapshot.manager` | `storage`, `snapshot` | Automatic input capture on `storage.event_appended` (`job.discovered`, `run.created`, `job.state_changed` → `running`) and `monitor.polled` fallback (looks up `transport` and `campaign` at runtime); background SHA-256 hashing up to 100 MiB; oversize hash reuse; semantic CONVERGE YAML + OpenFOAM dict diffs; mutable `run_notes`. |
| `rules` | `rules.manager` | `storage`, `timeseries`, `rules` | Hierarchical rule merge (`global → campaign → case`, optional `campaign` service looked up at runtime), incremental series evaluation (`courant_high`, `residual_high` with warmup + restart reset, `monitor_range`), `nan_inf`, `solver_error`, `job_failed`, and adaptive `stall` detection. |
| `notifier-desktop` | `notifier.desktop` | — | Native Windows/Linux desktop toasts via `desktop-notifier` with silent fallback + log warning when headless/unavailable. |
| `notifier` | `notifier.router` | `storage` | Routes alerts and notable events to registered `notifier.*` providers with immediate-first delivery, batching window coalescing, quiet hours (critical bypass), rate limiting, and secret-free formatting. |

Durable events written in M6: `snapshot.captured`, `run.note_changed`, `alert.raised`,
`alert.cleared`, `alert.acknowledged`.

## Milestones

| | Content | Status |
|---|---|---|
| M0 | Kernel, config, discovery, SQLite + event log, CLI | done |
| M1 | Cluster profiles, SSH transport (key/agent/certificate/OTP), SLURM discovery, jobs/runs | done |
| M2 | Incremental readers, CONVERGE/OpenFOAM parsers, time series | done |
| M3 | Web UI (FastAPI, uPlot, live WebSocket, campaign view) | done |
| M4 | Integrated SSH terminal (xterm.js, [ADR 0007](adr/0007-integrated-terminal.md)) | done |
| M5 | Excel campaigns | done |
| M6 | Input snapshots + diff + notes, detection rules, desktop notifications | done (backend + CLI) |
