# Assumptions register

Every `# ASSUMPTION:` in the code has a row here. When a sample confirms or refutes it,
update the status and the code.

| ID | Assumption | Where | How to verify | Status |
|---|---|---|---|---|
| A1 | Job ids are not reused on the same cluster (`UNIQUE(cluster_id, job_id)`). | M1 schema | Long-running cluster history | open |
| A2 | `squeue -o` with `%Z` (work dir) works on Slurm 18.08. | scheduler-slurm | Run the format on `uni-a` | open |
| A3 | Without accounting, `scontrol show job <id>` still returns a finished job for ~MinJobAge (default 300 s), enough with ≤ 60 s polling of running jobs. | scheduler-slurm | `scontrol show config \| grep MinJobAge` on `uni-a` | open |
| A4 | CONVERGE `.out` header layout (names / units / optional qualifier rows / `#`) is the same for all `.out` files. | M2 parser | More `.out` samples (species_*, dynamic, numerics, mass_balance) | partially verified (thermo, area_avg_regions_flow) |
| A5 | OpenFOAM fatal errors appear as `FOAM FATAL ERROR` / `FOAM FATAL IO ERROR` in the solver log. | M2 parser | A failing log sample | open |
| A6 | Cluster timestamps are local time of a single configurable zone per cluster. | scheduler-slurm | — (configurable) | accepted |
| A100 | Crank-angle CONVERGE runs print the unit inside parentheses in `logout.txt` (e.g. `(CA)` / `(deg)` instead of `(sec)`). | solver-converge | A crank-angle `logout.txt` sample | open |
| A101 | CONVERGE fatal errors in `logout.txt` start with `Fatal Error`, `FATAL ERROR`, or `ERROR:`. | solver-converge | A failing CONVERGE `logout.txt` sample | open |
