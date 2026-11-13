# Status

_Last updated: 2026-11-13_

## Current milestone

**M1 — SSH and SLURM discovery**: done 2026-11-13. All three clusters connect and pass
`simman cluster test`; real job lines still to be seen (A2, A7).
**M2 — incremental readers and parsers**: developed in parallel in a separate worktree
(branch `m2-parsers`), on top of the M1 contracts.

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

## Not yet implemented

Solver parsers and incremental readers (M2), web UI, terminal, campaigns, snapshots,
notifications. PBS is on standby (user decision 2026-11-13). Passphrase-protected keys in
the new OpenSSH format would need `bcrypt`: not added, the user has no passphrases.

## Known limitations

- On clusters without accounting (`uni-a`, `uni-b`) a job that starts and ends between
  two polls (up to ~330 s when idle) is never seen. Possible later fix: poll faster
  for a while after a submission is noticed, or read the case directory (M2).

## Open questions for the user

1. When a job is running: `simman monitor --once` + `simman jobs list --all` (verifies
   A2 on real lines); after a job ends on `hpc-national` the same verifies A7.

## Changelog

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
