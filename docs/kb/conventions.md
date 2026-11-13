# Conventions

## Language

- Chat with the user: Italian. Everything in the repo (code, identifiers, comments,
  docstrings, docs, commits): English.

## Code

- Python ≥ 3.11 (dev on 3.12), `asyncio` for all I/O. Type hints everywhere.
- `mypy --strict` on `simmanager.kernel` and `simmanager.contracts`; `disallow_untyped_defs`
  elsewhere. `ruff` (line length 100) for lint and format.
- Consumers depend on contracts (`simmanager.contracts.*` Protocols), never on plugin
  implementations. Service keys are dotted (`scheduler.slurm`); contract versions are integers.
- Parsing of remote output is pure functions, separate from I/O, tested on fixtures.
- Prefer simple, readable code: the user maintains it. Ask before adding a heavy dependency
  (no pandas/numpy/ORM/frontend build without agreement).
- Unverified hypotheses: `# ASSUMPTION(A<n>): <what>` in code **and** row `A<n>` in
  [assumptions.md](assumptions.md). `tests/test_knowledge_base.py` fails otherwise.

## Plugins

- `name` kebab-case, also the config section `plugins.<name>`.
- Long-running work via `ctx.task(...)`; loops catch their own per-iteration errors.
- Teardown only releases resources (connections, tasks, listeners). Never delete data,
  never perform irreversible remote actions in a disposer.

## Tests

- `uv run pytest` must stay green; `faulthandler_timeout = 30` dumps stacks of hangs.
- Tests never touch real user dirs (`tests/conftest.py` redirects `SIMMANAGER_CONFIG_DIR` and
  `SIMMANAGER_DATA_DIR`) and never need a real cluster (fake transport / in-process SSH server).
- Fixtures in `tests/fixtures/` are small (KB), truncated and anonymized.

## Privacy

`samples/` holds raw user data and is git-ignored. The pre-commit hook
(`.githooks/pre-commit`) blocks staged lines containing:

- IPv4 addresses and e-mail addresses (except `noreply@anthropic.com`, `@example.org/com`);
- any term of the **local denylist** `.git/info/privacy-denylist` (one term per line: real
  usernames, hostnames, institutions, project accounts, path fragments). It lives inside
  `.git/`, so it is never committed; each clone creates its own.

Enable once per clone: `git config core.hooksPath .githooks`.

When anonymizing: replace users with `user1`, `otheruser`; paths with `/work/user1/case_a`;
hosts with `cluster.example.org`; accounts with `ACCOUNT_X`; drop other users' jobs.

## Git

- Small, descriptive commits; KB updated in the same commit (pre-commit hook checks
  `src/` → `docs/kb/`). Enable hooks once per clone: `git config core.hooksPath .githooks`.
- Never push or force-push unless the user asks. Never bypass hooks unless the user asks.
- Line endings normalized to LF (`.gitattributes`).

## Milestone workflow

1. Plan → user approval → implement.
2. End of milestone: tests green, demo command, KB updated, summary to the user
   (works / missing / how to try / assumptions).
