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
| `storage.py` | `storage` | M0 | Append-only event log (`append_event`, `events`, `latest_events`); since M1 jobs/runs (`upsert_job`, `jobs`, `JobRecord`, `JobUpsert`). |
| `monitor.py` | `monitor` | M1 | `Monitor.poll/poll_all` → `PollReport`; bus event `monitor.polled`. |
| `clusters.py` | — (data) | M1 | `ClusterProfile` / `ClustersFile` (clusters.yaml), `PollConfig`. |
| `transport.py` | `transport` | M1 | `Transport.session/state/reset/clusters`; `RemoteSession.run_readonly/run_mutating/stat/listdir/read_range`; `ConnectionState`; errors `TransportError`, `ClusterUnavailableError`, `CommandNotAllowedError`; bus event `transport.state`. |
| `auth.py` | `auth.prompter` | M1 | `AuthPrompter.ask(AuthRequest)` / `confirm`: passwords, passphrases, OTP, yes/no. |
| `scheduler.py` | `scheduler.<kind>` | M1 | `JobState`, `JobInfo`, `Scheduler.list_active/final_states/cancel_argv` (stateless; receives the `RemoteSession`). |
| `timeseries.py` | `timeseries` | M2 | `TimeSeriesStore`, `FileCursor`, `SeriesKey`, `SeriesMeta`, `SeriesPoints`, `RunSolverState` (chunked float64 time series + cursors + solver run state). |
| `solver.py` | `solver.<kind>` | M2 | `Solver` (`kind`, `discover`, `parse_header`, `parse_chunk`, `align_offset`), `WatchedFile`, `ParsedChunk`, `SolverStatusUpdate`. |
| `ingest.py` | `ingest` | M2 | `IngestService.sync_run` (`SyncRunResult`): orchestrates solver detection, incremental reading, backfill and event emission. |

## Storage (`plugins/storage_sqlite`)

SQLite in WAL mode via `aiosqlite`, hand-written SQL, numbered migrations in
`migrations/NNNN_name.sql` applied once each in a transaction (`schema_migrations`).

| Table | Since | Purpose |
|---|---|---|
| `events` | M0 | Append-only log of facts: `id, ts (UTC), kind, source, cluster_id, run_id, payload JSON`. |
| `jobs` | M1 | Latest known facts of each scheduler job, `UNIQUE(cluster_id, job_id)`; `active` = still in the queue. Updates keep stored values for fields the scheduler did not report. |
| `runs` | M1 | One execution of a case: `job_pk` (unique, NULL for manual runs), `origin` discovered/manual, `work_dir`. Created with the job in one transaction. |
| `file_cursors` | M2 | Incremental byte offsets (`forward_offset`, `backfill_start`, `backfill_end`), 256-byte prefix and parser state per `(run_id, file_path)`. |
| `series` | M2 | Discovered time-series metadata (`run_id, file_path, name, qualifier, unit`, summary stats). |
| `sample_chunks` | M2 | Dense float64 `array('d')` chunks (`level=0`) and 16:1 / 256:1 min/max pyramids (`level=1, 2`). |
| `run_solver_state` | M2 | Latest solver state, progress, times, `ncyc`, warnings and case/output dirs per `run_id`. |

## M1 plugins

| Plugin | Provides | Injects | Notes |
|---|---|---|---|
| `prompter-cli` | `auth.prompter` | — | getpass/input; answers None when stdin is not a TTY. |
| `transport-ssh` | `transport` | `storage` | asyncssh, one lazy connection per cluster, read-only policy and auth states ([ADR 0004](adr/0004-read-only-ssh-and-auth-states.md)). Reads `clusters.yaml`. |
| `scheduler-slurm` | `scheduler.slurm` | — | Pure parsers in `parse.py`; `sacct`, falling back to `scontrol` when accounting is disabled (remembered per cluster). |
| `monitor` | `monitor` | `storage`, `transport` | One poll loop per cluster (adaptive 60/180/300 s ±10 %); scheduler looked up as `scheduler.<profile.scheduler>`. `loop: false` = on demand only (CLI). |

Durable events written in M1: `cluster.connected`, `cluster.connection_lost`,
`cluster.unreachable`, `cluster.auth_required`, `cluster.failed`,
`cluster.cert_renew_started|finished|failed`, `cluster.degraded`, `cluster.recovered`,
`job.discovered`, `run.created`, `job.state_changed`, `action.executed`, `action.result`.
Bus-only events: `transport.state`, `monitor.polled`.

## Milestones

| | Content | Status |
|---|---|---|
| M0 | Kernel, config, discovery, SQLite + event log, CLI | done |
| M1 | Cluster profiles, SSH transport (key/agent/certificate/OTP), SLURM discovery, jobs/runs | done |
| M2 | Incremental readers, CONVERGE/OpenFOAM parsers, time series | done (branch `m2-parsers`) |
| M3 | Web UI (FastAPI, htmx, uPlot) | |
| M4 | Integrated SSH terminal (xterm.js) | |
| M5 | Excel campaigns | |
| M6 | Input snapshots + diff + notes, detection rules, desktop notifications | |
