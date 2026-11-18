# AGENTS.md — start here

Entry point for every coding agent (Claude Code, Codex, Cursor, Copilot, …) and human
contributor. Read this file, then `docs/kb/README.md`, before changing anything.

## What SimManager is

A local (Windows + Linux) app that **monitors** CFD simulations (CONVERGE CFD, OpenFOAM)
running on remote SLURM clusters: it discovers the user's jobs over SSH, follows output
files incrementally, plots progress, tracks input changes between runs and notifies.
It **observes**; it never builds setups or submits jobs. On explicit user request it runs
the user's own display scripts on the cluster (ADR 0008).
Full scope: `docs/kb/project-brief.md`.

## Non-negotiable rules

1. **No personal or cluster data in git.** No real usernames, hostnames, paths, project
   accounts, IPs, emails, or other users' jobs. `samples/` (raw, private) is git-ignored;
   tests use anonymized, truncated extracts in `tests/fixtures/`. The pre-commit hook scans
   staged changes; see `docs/kb/conventions.md#privacy`.
2. **Read-only monitoring, explicit user-approved actions.** The monitoring engine runs only
   allowlisted read-only commands. Anything that changes the cluster (`scancel`, running a
   display script) goes through `run_mutating` with an approval given by the user in the UI
   (a click, or enabling an auto-refresh) and is written to the event log (ADR 0004, 0008).
3. **Durable state ≠ plugin state.** Jobs, runs, snapshots, samples live in storage. Unloading
   a plugin never deletes or alters data. Irreversible actions never live in disposers.
4. **Don't guess file formats.** Derive them from samples; if a sample is missing, ask the
   user. Mark every unverified hypothesis with `# ASSUMPTION(A<n>): ...` in code **and** a
   row `A<n>` in `docs/kb/assumptions.md` (a test checks they match).
5. **Keep the kernel small and generic** (no CFD imports in `simmanager.kernel`). No
   abstraction that no current-milestone plugin needs. No heavy dependency without asking.
6. **Never push, never force anything** without the user asking. Small descriptive commits.
7. **Keep the knowledge base current** — see below. A change is not done until the KB says so.

## Commands

```powershell
uv sync                                   # install
uv run pytest                             # tests (must stay green)
uv run ruff check src tests ; uv run ruff format src tests
uv run mypy                               # strict on kernel + contracts
uv run simman --help
```

## Repository map

| Path | What |
|---|---|
| `src/simmanager/kernel/` | Generic plugin kernel (lifecycle, DI, effects, events, config, discovery) |
| `src/simmanager/contracts/` | Versioned service Protocols — consumers import only these |
| `src/simmanager/plugins/` | Built-in plugins (`storage_sqlite`, `example`, …) |
| `src/simmanager/app.py`, `cli/` | Bootstrap and `simman` CLI |
| `tests/` | pytest; `tests/fixtures/` = anonymized sample extracts |
| `tools/` | Developer tooling (`dev_setup.py`) |
| `.githooks/`, `.claude/settings.json` | Versioned git hooks; Claude Code project hooks |
| `docs/architecture.md` | How the system is built (authoritative) |
| `docs/adr/` | Architecture decision records |
| `docs/kb/` | Shared knowledge base (status, domain facts, conventions, assumptions) |

## Knowledge base: how to keep it current

The KB is the shared memory between agents and sessions. Private agent memories are not
visible to others: anything another agent would need goes here.

Update in the **same commit** as the change:

| You changed… | Update |
|---|---|
| any code under `src/` | `docs/kb/status.md` (what works / next / changelog line) |
| architecture, contracts, DB schema, plugin list | `docs/architecture.md` (+ new ADR in `docs/adr/` for structural decisions) |
| learned a fact about a format, cluster, solver | `docs/kb/domain.md` or `docs/kb/clusters.md` |
| made or verified an assumption | `docs/kb/assumptions.md` |
| agreed a workflow/coding rule with the user | `docs/kb/conventions.md` |
| finished or re-planned a milestone | `docs/kb/status.md` and `docs/kb/roadmap.md` |

A git pre-commit hook enforces the first row and scans for private data
(`.githooks/pre-commit`). `tests/test_knowledge_base.py` checks that KB files exist,
internal links resolve and every `ASSUMPTION(A<n>)` is registered.

## Environment setup (automatic)

`tools/dev_setup.py` enables the git hooks and rebuilds the local privacy denylist from
private sources on this machine (OS/git user, `~/.ssh/config`, `clusters.yaml`, `samples/`,
manual extras). It is idempotent.

- **Claude Code** runs it at every session start (`.claude/settings.json`, SessionStart hook).
- **Any other agent or human**: run `uv run python tools/dev_setup.py` first in a new
  clone. If you forget, `tests/test_dev_setup.py::test_dev_environment_is_set_up` fails and
  tells you to.

## Talking to the user

The user is a CFD engineer (expert in CFD/HPC, not in software architecture). Chat in
**Italian**; code, identifiers, comments, docstrings, docs and commits in **English**.
Work in small milestones, each with green tests and a runnable demo; at the end summarize
what works, what is missing and how to try it.
