# Target clusters (anonymized)

Real hostnames, usernames, accounts and paths are **not** stored here. The user keeps them
in `~/.ssh/config` and in SimManager's `clusters.yaml` (outside the repo).

| Alias | Kind | Scheduler | Accounting (`sacct`) | Auth | Notes |
|---|---|---|---|---|---|
| `uni-a` | university cluster | Slurm 18.08.3 | **disabled** | ed25519 key | `squeue`, `sacct` in `/usr/bin`; SLURM stdout `%x_%n_%j.Qtxt`; ANSI colors in CONVERGE stdout |
| `uni-b` | group server | Slurm (similar to `uni-a`) | to check | ed25519 key | no samples; "more or less the same" as `uni-a` |
| `hpc-national` | national HPC centre | Slurm 25.05 (vendor build) | enabled | **OTP-based** (user script, sample pending) | login node round-robin (host key may differ per node); job arrays common; budget-related pending reasons |

Common facts:
- All SLURM; PBS on standby (2026-11-13).
- `squeue`/`sacct` reachable in a non-interactive SSH session → no login shell needed.
- SSH from the user's Windows machine prints X11 warnings (`ForwardX11` in their config):
  ignore stderr noise when parsing command output.
- Polling: adaptive 60/180/300 s (see [roadmap.md](roadmap.md#monitor-plugin)).
