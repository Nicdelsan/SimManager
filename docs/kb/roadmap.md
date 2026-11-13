# Roadmap

Milestones are small; each ends with green tests, a runnable demo and a summary to the user.
Status of each: [status.md](status.md).

## M0 — Skeleton and kernel ✅

Done 2026-11-13. See [architecture.md](../architecture.md).

## M1 — SSH and SLURM discovery ✅

Done 2026-11-13; checked on the three real clusters (see [status.md](status.md)).
As built: contracts in `src/simmanager/contracts/` (table in
[architecture.md](../architecture.md#contracts-contracts)), policy and auth states in
[ADR 0004](../adr/0004-read-only-ssh-and-auth-states.md), SLURM commands in
[domain.md](domain.md#slurm).

Changes from the plan agreed on 2026-11-13:
- Profile fields: `key_files` (list) instead of `key_file`; `auth` uses SSH method names
  (`publickey`, `keyboard-interactive`, `password`); new `cert_renew_command`,
  `ssh_config_file`, `known_hosts`, `max_channels`, `keepalive_s`, `connect_timeout_s`,
  `compression`. Dropped: `jump_host` (use `ProxyJump` in the SSH config),
  `login_shell`, `remember_password` (no password storage).
- `hpc-national` uses short-lived SSH certificates, not an SSH OTP prompt (see
  [clusters.md](clusters.md)). Keyboard-interactive/OTP is still supported.
- `Transport.subscribe_state` replaced by the bus event `transport.state`; added
  `Transport.clusters()` and `reset()`; `RemoteSession.user`. Timeouts are `timeout_s`.
- `open_pty` deferred to M4. `cluster_cursors` table not needed (final states are
  queried by job id), so not created.
- Monitor: `loop: false` config for on-demand use; CLI commands other than `serve` and
  `monitor` never poll.

## M2 — Incremental readers and parsers

- Read via SFTP `read_range` (seek), 4–8 MB chunks; truncation detection (`size < offset`,
  fingerprint of first 256 bytes); partial last line kept in parser state.
- **Initial backfill, auto mode**: file ≤ 50 MB read fully at once; larger → read the last
  ~20 MB first (plots appear immediately), then backfill the rest in background with progress.
  Thresholds configurable.
- Series identity `(file, name, qualifier)`; samples stored in chunks (packed float64 blobs,
  ~1k points) + min/max downsampled levels.
- Parsers: see [domain.md](domain.md).

## M3 — Web UI

Done 2026-11-13 (steps 1–7 + campaign view; see status.md changelog).

Plan approved 2026-11-13 with these decisions:
- Plugin `ui_web`: `ui-web` (FastAPI + uvicorn in the kernel's event loop, service
  `ui.web`) and `prompter-web` (`auth.prompter` over the dashboard WebSocket). No
  frontend toolchain: ES modules + vendored uPlot; works offline.
- Security: bind 127.0.0.1 only; random token per start exchanged for an
  `HttpOnly; SameSite=Strict` cookie; Host check (DNS rebinding) on HTTP and WS; Origin
  required on WS and POST; JSON-only POST; CSP `default-src 'self'`,
  `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`.
- Durable log holds only facts (`ingest.DURABLE_EVENT_KINDS`); `series.updated`,
  `run.progress`, `backfill.progress` are bus-only. `storage.event_appended` forwards
  the durable log live.
- ETA from the progress series (`ingest.PROGRESS_SERIES`, x = wall time), also plotted.
- One plot per unit; log scale default for residuals; zoom reloads at higher
  resolution through `query_series` levels; live points ≤ 1 update/s per run.
- `prompter: auto|cli|web` (auto: `serve` → web). Unanswered prompt → timeout →
  `AWAITING_AUTH`, no blind retry. Secrets never logged or kept; the transport no
  longer caches key passphrases. Browser opened automatically by `serve`.

## M5 — Excel campaigns (backend)

Backend done and merged into `main` 2026-11-13; web view delivered with M3.

Plan approved 2026-11-13 (branch `m5-campaigns`, migration `0004_campaigns.sql`):
- Hierarchy `Project → Campaign → Case → Run`, preserving manual `State` separately from
  `computed_state` (`planned`, `queued`, `running`, `completed`, `stopped`, `diverged`,
  `failed`, `cancelled`, `unknown`) and original parameters verbatim.
- `work_root`-scoped matching (`auto_work_dir` → `auto_dir_name` → `auto_regex`) on import
  and `monitor.polled`; configurable `NNN_MMM` derivation regex; manual match locking and
  unmatch exclusions.
- Template-preserving `.xlsx` export appending `SM_*` columns after `ws.max_column`
  (never overwriting the source file) and `simman campaign` CLI.

## M4 — Terminal · M6 — Snapshots, rules, notifications

See [project-brief.md](project-brief.md). Detailed plans are written here when each starts.
