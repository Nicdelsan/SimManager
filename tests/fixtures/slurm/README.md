# SLURM fixtures

All names, users, paths, accounts and node names are synthetic.

| File | Origin |
|---|---|
| `scontrol_18_running.txt` | Layout of real `scontrol show job` output (Slurm 18.08), anonymized; second record edited to a finished job with a space in the path. |
| `scontrol_25_array_held.txt` | Layout of real output (Slurm 25.05, array task held after requeue), anonymized; second record synthetic (pending task range). |
| `sacct_disabled.txt` | Real message on a cluster without accounting. |
| `squeue.txt` | **Synthetic** for the format in `parse.SQUEUE_FORMAT` (assumption A2). Last line is deliberately malformed. |
| `sacct.txt` | **Synthetic** for `parse.SACCT_FIELDS` with `-P` (assumption A7). |
