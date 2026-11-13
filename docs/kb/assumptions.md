# Assumptions register

Every `# ASSUMPTION:` in the code has a row here. When a sample confirms or refutes it,
update the status and the code.

| ID | Assumption | Where | How to verify | Status |
|---|---|---|---|---|
| A1 | Job ids are not reused on the same cluster (`UNIQUE(cluster_id, job_id)`). | M1 schema | Long-running cluster history | open |
| A2 | `squeue -h -r -o "%i\|%j\|%T\|%M\|%l\|%D\|%P\|%V\|%S\|%R\|%Z"` output lines parse as in `tests/fixtures/slurm/squeue.txt`. | scheduler-slurm | Run `simman monitor --once` while a job runs; check `simman jobs list` and the log for `squeue: skipping line` | partially verified 2026-11-13: command accepted on Slurm 18.08, 22.05, 25.05 (no job lines seen yet) |
| A3 | Without accounting, `scontrol show job <id>` still returns a finished job for ~MinJobAge, longer than the poll interval of running/queued jobs (60/180 s + 10 %). | scheduler-slurm | `simman cluster test` | verified 2026-11-13: MinJobAge 300 s on `uni-a` and `uni-b` (the clusters without accounting); 30 s on `hpc-national`, which has accounting |
| A4 | CONVERGE `.out` header layout (names / units / optional qualifier rows / `#`) is the same for all `.out` files. | M2 parser | More `.out` samples (species_*, dynamic, numerics, mass_balance) | partially verified (thermo, area_avg_regions_flow) |
| A5 | OpenFOAM fatal errors appear as `FOAM FATAL ERROR` / `FOAM FATAL IO ERROR` in the solver log. | M2 parser | A failing log sample | open |
| A6 | Cluster timestamps are local time of a single configurable zone per cluster. | scheduler-slurm | — (configurable) | accepted |
| A7 | `sacct -X -n -P -o JobID,JobName,State,ExitCode,Elapsed,Submit,Start,End,WorkDir,Partition,NNodes,Timelimit,User,NodeList` prints one `\|`-separated line per job, and `-j` without `-S` searches past days. | scheduler-slurm | Run on `hpc-national` after a job ends; compare with `tests/fixtures/slurm/sacct.txt` | open |
| A100 | Crank-angle CONVERGE runs print the unit inside parentheses in `logout.txt` (e.g. `(CA)` / `(deg)` instead of `(sec)`). | solver-converge | A crank-angle `logout.txt` sample | open |
| A101 | CONVERGE fatal errors in `logout.txt` start with `Fatal Error`, `FATAL ERROR`, or `ERROR:`. | solver-converge | A failing CONVERGE `logout.txt` sample | open |
| A200 | A failed `SolverState` or failed scheduler job combined with `solver.nan_detected` or divergence keywords in `error_message` indicates `diverged` rather than generic `failed`. | campaign-excel | A diverging CONVERGE/OpenFOAM run sample | open |
| A300 | Remote SFTP `mtime` and cluster scheduler `StartTime` share NTP sync within 1 second (`mtime > start_time + 1s` flags a file as modified after launch). | snapshot | Test on a running job on all clusters | open |
