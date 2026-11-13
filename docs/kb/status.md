# Status

_Last updated: 2026-11-13_

## Current milestone

**M1 — SSH and SLURM discovery** (not started; waiting for user go-ahead).
Plan: [roadmap.md#m1](roadmap.md#m1--ssh-and-slurm-discovery).

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

- 2026-11-13 — `tools/dev_setup.py` + Claude Code SessionStart hook: git hooks enabled and
  privacy denylist generated automatically from local sources on every machine.

- 2026-11-13 — Knowledge base created (`AGENTS.md`, `docs/kb/`, KB pre-commit hook and test).
- 2026-11-13 — M0 completed and pushed: kernel, SQLite storage, CLI, docs, ADR 0001–0002.
