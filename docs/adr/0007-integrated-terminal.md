# ADR 0007 — Integrated terminal: the user acts, the monitor stays read-only

- Status: accepted (M4, 2026-11-13)

## Context

The user wants to work on the clusters (`sbatch`, `vi` on the `.in` files, `scancel`,
`reconstructPar`, …) from SimManager. Today this means a separate SSH window for each
cluster. ADR 0004 says the monitoring engine runs only allowlisted read-only commands,
and anything that changes the cluster needs explicit confirmation and an event. An
interactive shell can do anything, so we need to say why it does not break that rule.

## Decision

1. **The terminal is the user, not SimManager.** A PTY opens only on an explicit click in
   the UI (`POST /api/terminals`). After that SimManager never writes into it: bytes
   arrive only from the attached browser tab, as keys, pastes or quick-action clicks.
   Quick actions *insert* a command without Enter, so the user still decides to run it.
   The read-only rule of ADR 0004 is about what SimManager does on its own (monitor,
   ingest, campaigns), and none of that goes through a terminal.
2. **Separate channels.**
   - The monitor's commands keep their own semaphore (`max_channels`, 6). Terminals use a
     separate per-cluster pool (`max_terminals`, 3) that fails fast when full and never
     waits for the monitor's channels.
   - 6 + 1 SFTP + 3 = 10, the usual sshd `MaxSessions` (A401).
   - The monitor still uses `run_readonly` only. `open_pty` lives on the same
     authenticated connection, but it is a different method.
3. **Login shell.** A plain `shell` request, or `cd -- <dir>; exec "$SHELL" -l` for a
   folder (A400), so `module load` and the user's profile work. This differs from the
   monitor's commands, which run without a login shell.
4. **Authentication.**
   - Opening a terminal on a cluster in `AWAITING_AUTH`/`BACKOFF`/`FAILED` is an explicit
     reconnect, the same as the "Reconnect" button: one attempt, and the prompt appears in
     the browser. There are no retries.
   - "Reconnect" asks for confirmation when terminals are open, because it closes the
     connection.
5. **Mutating actions outside the terminal.** "Cancel job" is the only button that
   changes the cluster. It needs a server-issued, single-use confirmation id; it runs
   `run_mutating` with an `ActionApproval`, which writes `action.executed` /
   `action.result`.
6. **Privacy.** Terminal input and output never reach a logger, an exception message or
   the event log.
   - Only metadata is recorded: `terminal.opened` and `terminal.closed`, with cluster,
     folder, reason, duration and exit status.
   - `uvicorn.access` is disabled, because URLs carry tickets.
   - A test at DEBUG level checks both directions.
7. **Lifetime.**
   - A session survives a closed or reloaded tab for `orphan_timeout_s` (30 min), keeping a
     1 MiB replay buffer.
   - It does not survive SimManager. The terminal page says so and suggests `tmux`/`screen`.
   - Per cluster, `terminal_tmux` (off by default) opens each new tab in a new tmux session
     `simman-<cluster>-<n>`. Names are always new and `-A` is never used, so a tab never
     joins an old session in another folder by accident.
   - "Re-attach an existing tmux session" lists the live sessions through the
     allowlisted read-only `tmux ls`; `#(...)` formats are rejected.
8. **Transport to the browser.**
   - One WebSocket per session (`/ws/term/{id}`), not the dashboard broadcast.
   - It has the same cookie/Host/Origin checks as ADR 0005, plus a single-use ticket bound
     to the session (30 s).
   - Frames are binary and output is backpressured.
   - The `/terminal` page alone allows inline styles, because xterm.js needs them.

## Consequences

- The user can do on the cluster everything they can do in `ssh`: that is the purpose.
  SimManager's own behaviour stays auditable: read-only commands, plus confirmed
  actions that are logged.
- A connection drop or a SimManager restart ends the shells. Long work belongs in
  tmux/screen on the cluster.
- A browser tab that is open on `/terminal` keeps a session alive: closing it or the
  orphan timeout ends the session.
