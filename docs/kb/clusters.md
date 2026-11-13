# Target clusters (anonymized)

Real hostnames, usernames, accounts and paths are **not** stored here. The user keeps them
in `~/.ssh/config` and in SimManager's `clusters.yaml` (outside the repo).

Verified with `simman cluster test` on 2026-11-13 (connection, `squeue` format accepted,
accounting, MinJobAge).

| Alias | Kind | Scheduler | Accounting (`sacct`) | MinJobAge | Auth | Notes |
|---|---|---|---|---|---|---|
| `uni-a` | university cluster | Slurm 18.08.3 | **disabled** | 300 s | ed25519 key | `squeue`, `sacct` in `/usr/bin`; SLURM stdout `%x_%n_%j.Qtxt`; ANSI colors in CONVERGE stdout |
| `uni-b` | group server | Slurm 22.05.9 | **disabled** | 300 s | ed25519 key | no file samples yet; private LAN |
| `hpc-national` | national HPC centre | Slurm 25.05.7 (vendor build) | enabled | 30 s | **short-lived SSH certificate** (see below) | login node round-robin (host key may differ per node); job arrays common; budget-related pending reasons (`Comment=... budget`) |

Authentication on `hpc-national` (verified 2026-11-13 on the user's script): no OTP
prompt over SSH. The user runs a local script calling `step ssh certificate ...
--provisioner <centre> <key_path> --force --no-password --insecure` (smallstep CLI;
browser login with OTP) which writes an ECDSA key plus `<key>-cert.pub` valid ~12 h.
SSH then authenticates with the certificate. In SimManager: `key_files: [<key>]`,
`cert_renew_command: [step, ssh, certificate, ...]` (asked before running). The key may
live outside `~/.ssh` (e.g. in a terminal emulator's home): set `known_hosts` too if
that is where the host keys were recorded.

Common facts:
- All SLURM; PBS on standby (2026-11-13).
- `squeue`/`sacct` reachable in a non-interactive SSH session → no login shell needed.
- The user's `~/.ssh/config` has `ForwardX11`, `ForwardX11Trusted`, `ForwardAgent`,
  `IdentitiesOnly`: harmless for the transport (checked 2026-11-13). Windows paths with
  backslashes in `IdentityFile` are mangled by asyncssh's shell-like config parser
  (`C:\a\b` → `C:ab`): use forward slashes or `key_files` in clusters.yaml (the
  error message says so). `hpc-national` needs `key_files` anyway for the certificate
  expiry check.
- Host keys: only `hpc-national` is in the user's `~/.ssh/known_hosts`; run `ssh <alias>
  hostname` once for the others (the terminal emulator keeps its own host key store).
- `uni-b` is on a private LAN: reachable only from that network / VPN.
- No user key is passphrase-protected (user, 2026-11-13): `bcrypt` not needed.
- SSH from the user's Windows machine prints X11 warnings (`ForwardX11` in their config):
  ignore stderr noise when parsing command output.
- Polling: adaptive 60/180/300 s (see [roadmap.md](roadmap.md#monitor-plugin)).
