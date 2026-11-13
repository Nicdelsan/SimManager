# ADR 0004 — Read-only remote commands and human-gated authentication

- Status: accepted (M1, 2026-11-13)

## Context

SimManager watches jobs on shared HPC clusters through the user's own SSH account. A bug
in a monitor loop must never change anything on the cluster, and a background loop must
never hammer a login node with failing logins (accounts get locked; one cluster uses
short-lived SSH certificates issued through a browser login).

## Decision

1. **Read-only by construction.** `RemoteSession.run_readonly(argv)` accepts only an
   argv (never a shell string) whose executable is in an allowlist (`squeue`, `sacct`,
   `sinfo`, `scontrol show`, `ls`, `stat`, `find`, `head`, `tail`, `cat`, `wc`, `date`,
   `id`, `hostname`, `echo`, `git rev-parse|status|log|diff|show`), with options that write
   or execute (`find -delete/-exec`, `git -c/--output`, `tail -f`, …) rejected. Arguments
   are quoted with `shlex.join`. Violations raise `CommandNotAllowedError`.
2. **Mutations need proof of consent.** `run_mutating(argv, approval=ActionApproval(...))`
   is the only other way to run a command; it records `action.executed` and
   `action.result` in the append-only event log. Schedulers return e.g. `cancel_argv()`
   instead of running it.
3. **One lazy connection per cluster** with a state machine: `CONNECTED`, `BACKOFF`
   (network errors, retried after 5 s doubling up to 5 min, ±20 % jitter, one
   `cluster.unreachable` event per outage), `AWAITING_AUTH` (expired certificate, rejected
   login, missing passphrase/OTP: **no automatic retry** until `reset()` or until the key
   or certificate files change on disk), `FAILED` (unverifiable host key, broken key or
   config file: no retry until `reset()`).
4. **Humans answer through a service.** Passwords, passphrases, keyboard-interactive
   (OTP) prompts and yes/no questions go to `auth.prompter` (terminal now, web UI in M3).
   With no interactive prompter the answer is "no", never a hang. Answers live in memory
   only and are dropped after the attempt (since M3 a key passphrase is asked again at
   reconnection instead of being cached).
5. **Short-lived certificates** (`<key>-cert.pub`) are checked before connecting; when
   expired the user is asked to run the profile's `cert_renew_command` locally.
6. **Host keys are always verified** against known_hosts; there is no "accept new key"
   switch.

## Consequences

- New monitoring needs (e.g. `du`) require an explicit allowlist change, reviewed in a
  test.
- The SSH agent is not used when a profile lists `key_files` (IdentitiesOnly semantics),
  so the server sees only the intended key.
- Passphrase-protected keys in the new OpenSSH format need the optional `bcrypt` package
  (not installed by default; PEM/PKCS#8 encrypted keys work without it).
