# Project brief

Condensed from the user's original brief (Italian, kept privately). This is the reference
for scope; changes agreed with the user are recorded here.

## Problem

Today the user's loop is manual: export CONVERGE `.in`/`.dat` (or prepare OpenFOAM cases and
custom solvers), `scp` to the cluster (each with different auth), `sbatch`, watch `.out`
files via gnuplot over X11, download `.h5`/timesteps for ParaView, fix inputs with `vi`
on the cluster (untracked!) and relaunch. Campaigns are planned in an Excel sheet
(case naming rule + varying parameters).

## Scope

SimManager **observes**:
- connects to clusters via SSH, discovers the user's jobs, associates them with campaign
  cases, shows live progress (plots of `.out`/logs), state and notifications;
- offers an **integrated SSH terminal**: the user performs all cluster actions there;
- tracks input changes between runs (snapshot + diff + notes).

It does **not** build setups, modify remote files or submit jobs. The monitoring engine is
read-only; mutating quick actions (e.g. `scancel`) need explicit UI confirmation and are
logged. Runs locally (UI on `127.0.0.1`); remote access must be addable later as a plugin.

## Architecture principle

"Everything is a plugin", concepts from DeepSeek Harness / Cordis implemented as a small
Python kernel (see [ADR 0001](../adr/0001-python-kernel-instead-of-cordis.md)).

| Service | MVP plugin | Future (contract only) |
|---|---|---|
| `transport` | `ssh` | `local` |
| `scheduler` | `slurm` (PBS on standby) | `pbs`, `lsf` |
| `solver` | `converge`, `openfoam` | others |
| `campaign` | `excel` | `csv`, `yaml` |
| `snapshot` | `git-local` | `github` |
| `storage` | `sqlite` | `postgres` |
| `ui` | `web`, `terminal` | `remote` (+ auth) |
| `notifier` | `desktop` | `telegram`, `email` |
| `transfer` | — | `rsync` (download `.h5`/timesteps) |

## Functional requirements (summary)

1. **Cluster profiles & SSH**: host, port, user, auth (key/agent incl. Pageant, key with
   passphrase, password, keyboard-interactive OTP with prompt in UI, jump host), scheduler,
   polling interval, work roots; can read `~/.ssh/config`. One persistent multiplexed
   connection per cluster; auto-reconnect with backoff; ask for a new OTP when needed.
   Never store passwords in clear; OS keyring only on explicit request.
2. **Job discovery**: light periodic polling; normalized states `queued, running, completing,
   completed, failed, cancelled, timeout, unknown`; parsing is pure code tested on fixtures.
3. **Job ↔ case ↔ campaign**: a discovered job becomes a **Run**, matched to a **Case** by
   work dir and/or job name vs the campaign naming rule; ambiguous → "unassociated", manual
   association in UI. Several runs per case (relaunch/restart). Manual run registration.
4. **Excel campaigns**: import `.xlsx`, choose the case-name column, other columns are
   parameters; rows start `planned`; table view with state/job/cluster/progress/last monitor
   values; highlight never-launched cases and runs not in the Excel; export enriched copy,
   **never overwrite the original**. Hierarchy `Project → Campaign → Case → Run`.
5. **Solver plugins**: declare files to watch, parse **incrementally** (byte offsets, handle
   truncation and partial last lines), return time series, solver state, progress.
   CONVERGE: generic header-driven `.out` parser; state/progress from stdout.
   OpenFOAM: solver log (time, residuals, Courant, deltaT, execution time, End, FATAL),
   `postProcessing/**` function objects, parallel runs, restarts.
6. **Detection & notifications**: NaN/Inf, residual above threshold, stall (no file update
   for N minutes while running), job ended, job failed; desktop notifications.
7. **Input snapshots & diff**: on first detection copy small inputs (CONVERGE `*.in`, `*.dat`;
   OpenFOAM `system/`, `constant/` minus big meshes, `0/`) into a local Git repo managed by
   SimManager, one commit per snapshot; diff vs previous run of the same case + notes; size
   cap per file. OpenFOAM: optional custom-solver repo → record commit hash + dirty flag.
8. **Integrated terminal**: xterm.js over WebSocket to a PTY on the existing SSH connection;
   tabs; "open terminal in this folder"; resize, copy/paste, UTF-8; typing is not logged.
9. **Web UI**: FastAPI on `127.0.0.1`, live via WebSocket, no heavy frontend toolchain
   (vendored uPlot, xterm.js, htmx). Views: dashboard, campaign table, run detail (live plots,
   log scale, overlays, job info, input diff, notes, terminal), cluster management (OTP prompt).
   CLI `simman` (Typer): `serve`, `cluster add/test`, campaign import, `config show`.

## Quality bar

Python ≥ 3.11, asyncio everywhere, `uv`. pytest + pytest-asyncio; kernel and parsers
unit-tested on real (anonymized) fixtures; fake SSH server / fake transport so tests never
need a cluster. ruff + mypy (strict on kernel). Rotating structured logs. Windows verified
(paths, event loop, notifications, ssh-agent/Pageant). README + architecture doc + ADRs.
