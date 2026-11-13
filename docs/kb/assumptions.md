# Assumptions register

Every `# ASSUMPTION:` in the code has a row here. When a sample confirms or refutes it,
update the status and the code.

| ID | Assumption | Where | How to verify | Status |
|---|---|---|---|---|
| A1 | Job ids are not reused on the same cluster (`UNIQUE(cluster_id, job_id)`). | M1 schema | Long-running cluster history | open |
| A2 | `squeue -h -r -o` with `%Z` (work dir) works on Slurm 18.08. | scheduler-slurm | `simman cluster test uni-a` (row "squeue format") | open |
| A3 | Without accounting, `scontrol show job <id>` still returns a finished job for ~MinJobAge (default 300 s), enough with ≤ 60 s polling of running jobs. | scheduler-slurm | `simman cluster test uni-a` (row "MinJobAge") | open |
| A4 | CONVERGE `.out` header layout (names / units / optional qualifier rows / `#`) is the same for all `.out` files. | M2 parser | More `.out` samples (species_*, dynamic, numerics, mass_balance) | partially verified (thermo, area_avg_regions_flow) |
| A5 | OpenFOAM fatal errors appear as `FOAM FATAL ERROR` / `FOAM FATAL IO ERROR` in the solver log. | M2 parser | A failing log sample | open |
| A6 | Cluster timestamps are local time of a single configurable zone per cluster. | scheduler-slurm | — (configurable) | accepted |
| A7 | `sacct -X -n -P -o JobID,JobName,State,ExitCode,Elapsed,Submit,Start,End,WorkDir,Partition,NNodes,Timelimit,User,NodeList` prints one `\|`-separated line per job, and `-j` without `-S` searches past days. | scheduler-slurm | Run on `hpc-national` after a job ends; compare with `tests/fixtures/slurm/sacct.txt` | open |
