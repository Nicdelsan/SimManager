# Roadmap

Milestones are small; each ends with green tests, a runnable demo and a summary to the user.
Status of each: [status.md](status.md).

## M0 — Skeleton and kernel ✅

Done 2026-11-13. See [architecture.md](../architecture.md).

## M1 — SSH and SLURM discovery

Agreed design (2026-11-13). PBS deferred.

### Cluster profiles

Separate file `clusters.yaml` in the user config dir (never in the repo), written by
`simman cluster add`, hand-editable. Fields: `ssh_config_host` (inherit from `~/.ssh/config`),
`host`, `port`, `user`, `auth` (ordered list: `agent`, `key`, `password`, `kbdint`),
`key_file`, `jump_host`, `scheduler: slurm`, `poll` (see below), `work_roots`, `timezone`
(user-settable per cluster), `login_shell: false`, `remember_password: false`.

### `transport` contract (plugin `transport-ssh`, asyncssh)

```python
class ConnectionState(StrEnum): DISCONNECTED, CONNECTING, AWAITING_AUTH, CONNECTED, BACKOFF, FAILED

class Transport(Protocol):
    async def session(self, cluster_id: str) -> RemoteSession
    def state(self, cluster_id: str) -> ConnectionState
    def subscribe_state(self, cb) -> Disposer

class RemoteSession(Protocol):
    async def run_readonly(self, argv: Sequence[str], *, timeout: float = 30) -> CommandResult
    async def run_mutating(self, argv, *, approval: ActionApproval, timeout=30) -> CommandResult
    async def stat(self, path) -> RemoteStat | None          # SFTP, same connection
    async def listdir(self, path) -> list[RemoteStat]
    async def read_range(self, path, offset, max_bytes) -> bytes
    async def open_pty(self, *, cols, rows, cwd) -> PtyChannel  # M4
```

- One connection per cluster; semaphore on concurrent channels (default 6 < sshd MaxSessions 10);
  keepalive 30 s; zlib compression on (logs compress 5–10×).
- Reconnect with exponential backoff + jitter (5 s → 5 min). If human input is needed (OTP,
  unsaved password) → `AWAITING_AUTH`, event `cluster.auth_required`, wait for the prompter.
- Auth via `AuthPrompter` service (`auth.prompter`): `prompter-cli` in M1, web in M3.
  kbd-interactive challenges are forwarded with prompt text and echo flag.
- **Read-only policy** (ADR 0004, to write): `run_readonly` takes argv only, executable must be
  allowlisted (`squeue, sacct, scontrol show, sinfo, ls, stat, find, head, tail, cat, wc, date,
  id, hostname, echo, git rev-parse/status/log`), args quoted with `shlex.quote`; violations
  raise and are logged. `run_mutating` requires an `ActionApproval` and writes
  `action.executed` / `action.result` events.

### `scheduler` contract (plugin `scheduler-slurm`)

`JobInfo` (job_id str — covers `123_4` arrays —, name, state, raw_state, user, work_dir,
partition, nodes, elapsed_s, time_limit_s, submit/start/end time, exit_code, reason,
stdout_path). Methods: `list_active`, `list_finished(since)`, `job_detail(id)`,
`cancel_argv(id)` (executed only via `run_mutating`). Pure parsers: `parse_squeue`,
`parse_sacct`, `parse_scontrol`, `normalize_state`. Commands in [domain.md](domain.md#slurm).

Final state when a job leaves `squeue`: `sacct` if accounting is enabled, else
`scontrol show job <id>` (works only for ~MinJobAge after the end), else `unknown` until the
solver log tells (M2). `simman cluster test` detects which applies.

### `monitor` plugin

Inject `storage`, `transport`; scheduler looked up per cluster at runtime. Per-cluster poll
loop; **adaptive interval** (default, overridable per cluster): 60 s if any job is running,
180 s if only queued, 300 s if none; ±10 % jitter. New job → `jobs` row + Run + events
`job.discovered`, `run.created`; state change → `job.state_changed`. Per-iteration errors
counted; after K consecutive → `cluster.degraded`. Unloading never touches DB rows.

Job ↔ run: one run per job id; array task `123_4` = its own run; requeue keeps the same run.

### DB migration 0002

`jobs` (UNIQUE cluster_id + job_id), `runs` (job_pk nullable for manual runs, origin
discovered|manual, work_dir, solver, case_id placeholder), `cluster_cursors`
(e.g. last sacct window).

### CLI

`simman cluster add|list|test <id>`, `simman monitor [--once]`, `simman jobs list`.

### Tests

In-process asyncssh server: key, key+passphrase, password, kbd-interactive OTP (fake
prompter), jump host chain, reconnect after server restart, `AWAITING_AUTH`. Allowlist
violations. SLURM parsers on anonymized fixtures (arrays, pending reasons, OOM, NODE_FAIL,
`CANCELLED by`, TIMEOUT, old 18.08 vs 25.x output). Monitor on a fake transport: new job,
state change, vanished job, restart with existing DB (no duplicates), plugin unload keeps rows.

## M2 — Incremental readers and parsers

- Read via SFTP `read_range` (seek), 4–8 MB chunks; truncation detection (`size < offset`,
  fingerprint of first 256 bytes); partial last line kept in parser state.
- **Initial backfill, auto mode**: file ≤ 50 MB read fully at once; larger → read the last
  ~20 MB first (plots appear immediately), then backfill the rest in background with progress.
  Thresholds configurable.
- Series identity `(file, name, qualifier)`; samples stored in chunks (packed float64 blobs,
  ~1k points) + min/max downsampled levels.
- Parsers: see [domain.md](domain.md).

## M3 — Web UI · M4 — Terminal · M5 — Excel campaigns · M6 — Snapshots, rules, notifications

See [project-brief.md](project-brief.md). Detailed plans are written here when each starts.
