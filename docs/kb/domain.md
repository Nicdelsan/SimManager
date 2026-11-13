# Domain knowledge

Facts verified on the user's real samples (2026-11-13) unless marked as assumption.
Paths and names below are anonymized.

## Glossary

| Term | Meaning |
|---|---|
| Project → Campaign → Case → Run | Data hierarchy. A **Case** is one planned simulation (Excel row); a **Run** is one execution (one scheduler job, or a manual registration). One case can have many runs (relaunch, restart). |
| Job | Scheduler-side fact (id, state, times). A run references at most one job. |
| Work dir | Case directory on the cluster; job `WorkDir`. Main key to match runs ↔ cases. |
| Monitor / series | A time series extracted from an output file or log. |

## Normalized job states

`queued, running, completing, completed, failed, cancelled, timeout, unknown`.
Keep the scheduler's original string in `raw_state`.

## SLURM

Commands (all read-only, explicit user — no `$USER`, no shell):

```
squeue -h -r -u <user> -o "%i|%j|%T|%M|%l|%D|%P|%V|%S|%R|%Z"  # -r: one line per array task
sacct -X -n -P -j <ids> -o JobID,JobName,State,ExitCode,Elapsed,Submit,Start,End,WorkDir,Partition,NNodes,Timelimit,User,NodeList
scontrol show job <id>
sinfo --version
```

`<user>` is the login name of the SSH session. `%R` is the node list for running jobs and
`(Reason)` for pending ones. Durations: `[D-]HH:MM:SS`, `MM:SS`, bare minutes,
`UNLIMITED`. Without accounting `sacct` prints `Slurm accounting storage is disabled`.

State normalization (raw string kept in `raw_state`; `CANCELLED by <uid>` → first word):
queued = PENDING, REQUEUED, REQUEUE_HOLD, REQUEUE_FED, RESV_DEL_HOLD, SPECIAL_EXIT;
running = RUNNING, CONFIGURING, SUSPENDED, STOPPED, SIGNALING, RESIZING, STAGE_OUT;
completing = COMPLETING; completed = COMPLETED; failed = FAILED, NODE_FAIL,
OUT_OF_MEMORY, BOOT_FAIL, DEADLINE, PREEMPTED, REVOKED; cancelled = CANCELLED;
timeout = TIMEOUT; anything else = unknown. A job that leaves the queue and is unknown
to both sacct and scontrol is recorded as `unknown`.

Array job ids: squeue/sacct show `<array>_<task>`; scontrol shows its own `JobId` plus
`ArrayJobId`/`ArrayTaskId` → normalized to `<array>_<task>` (`<array>_[<range>]` for
a pending range).

`scontrol show job` output (verified on Slurm 18.08 and 25.05): records separated by a blank
line, `Key=Value` tokens separated by spaces over indented lines. Useful keys: `JobId`,
`ArrayJobId`, `ArrayTaskId` (may be a range like `11-14%1` for pending arrays), `JobName`,
`UserId=name(uid)`, `JobState`, `Reason`, `ExitCode`, `RunTime`, `TimeLimit`, `SubmitTime`,
`StartTime` / `EndTime` (`Unknown` when not set), `Partition`, `NodeList`, `NumNodes`,
`NumCPUs`, `WorkDir`, `StdOut`, `StdErr`, `Command`, `Comment`, `Requeue`, `Restarts`.
Values may contain spaces (e.g. `Comment=... budget `): parse keys known to be free text
up to end of line. `StdOut` may contain unexpanded patterns like `%x_%n_<id>.Qtxt`
(18.08) — expand `%x` (job name), `%j` (job id), `%n` (node index, 0) when locating files.
Times are cluster-local without zone → per-cluster `timezone` setting.

`scontrol show job` without an id lists **all users'** jobs: always pass ids or filter by
user, and never put other users' records in fixtures.

## CONVERGE (4.1.2)

### Case directory layout

```
<case>/
  *.in, *.dat                 inputs (inputs.in, solver.in, boundary.in, ... surface.dat, therm.dat)
  *.echo                      echo of inputs written by CONVERGE
  logout.txt                  solver stdout (redirected in the batch script)
  <job>_<id>.out/.err|.Qtxt   SLURM stdout/stderr (name from #SBATCH --output)
  STOP                        user-created file to stop the run
  outputs_<name>/             one per run/restart (e.g. outputs_original, outputs_restart1)
    converge.log, converge.start, global_variables.txt, memory_usage.out, mpi_map.log
    output/  post*_+<time>.h5, map_*.h5           (3D results — never copied)
    restart*.rst                                    (never copied)
    stream0/ *.out, converge.log                    (time-series .out files per stream)
```

The output directory is announced in stdout: `creating pathname <dir>/outputs_X/output`.
Prefer that line (and `Writing post file <path>`) over hard-coded names.

### `inputs.in` (and other `.in`)

YAML (`version: 4`, `---`, `#` comments). Progress-relevant keys under `simulation_control`:
`crank_flag` (0 = seconds), `start_time`, `end_time`, `restart_flag`, `restart_number`.

### `.out` files

```
# CONVERGE 4.1.2/  Dec 17 2024       Run Date:<date>
# column        1                2                3   ...
#            Time         Pressure         Max_Pres   ...
#       (seconds)            (MPa)            (MPa)   ...
#                          Regions_1_to_3  ...          ← optional qualifier row(s)
#
    2.0173315e-02    9.5435770e-01    2.3806929e+00   ...
```

- Header: version row, `# column` index row, names row, units row `(...)`, zero or more
  qualifier rows (e.g. `area_avg_regions_flow.out`: `(none)` / `Regions_1_to_3`), then `#`.
- Names repeat across qualifiers → column identity = `(name, qualifier)`.
- Fixed-width, whitespace-separated, `e` notation; some columns are integers (`Cycle_Number`).
- Files are large (tens to hundreds of MB): incremental reading only.
- Region variants: `<base>_region<N>.out` (same header, region subset).

### Solver stdout (`logout.txt`)

- May contain ANSI color codes (cluster-dependent) → strip `\x1b\[[0-9;]*m`.
- Progress line: `[ 15.98%] ncyc   109946;  Time    2.639254072e-02 (sec); dt=    5.216794175e-08 (dt_grow)`.
- Normal end: `normal termination`, followed by timing summary `Total Simulation Run Time: <s> seconds`.
- User stop: `Found file STOP. Finishing job.`
- Recoveries: `recovering .... because transport equations did not converge ...` (frequent; candidate warning counter, not an error).
- Input files are echoed as `Found the file '<name>' at '<path>'` — gives the case dir.
- `converge.log` (in `outputs_*/` and `stream0/`) is a different, more verbose log
  (`Time for ncyc N = x seconds`, per-species residual iterations); not needed for progress.

## OpenFOAM (v2312, custom solvers)

### Solver log (`log.<solver>` redirected in the batch script)

- Header: `Exec`, `Date`, `Host`, `Case`, `nProcs`, `I/O : uncollated|collated`.
- Per time step: `Courant Number mean: … max: …` / `Courant Number max: …`, `deltaT = …`,
  `Time = …`, then solver lines
  `DILUPBiCGStab:  Solving for p, Initial residual = …, Final residual = …, No Iterations N`
  (repeated per corrector/PIMPLE iteration → keep the **first** initial residual per field
  per step, foamLog convention), `PIMPLE: iteration N`,
  `time step continuity errors : sum local = …, global = …, cumulative = …`,
  `ExecutionTime = … s  ClockTime = … s`.
- Custom solvers add their own lines (`max/min FaceAcCourant: …`, `rho min/max : …`) →
  parser must ignore unknown lines, optionally extract `name: a/b` pairs.
- End: `End`; errors: `FOAM FATAL ERROR`, `FOAM FATAL IO ERROR` (no real sample yet).
- Size: ~1 GB / 350k steps for one run.

### Case

`system/controlDict`: `application`, `startFrom`, `startTime`, `endTime`, `deltaT`,
`writeInterval` — progress = (Time − startTime)/(endTime − startTime). Values can be followed
by `// comments`. Parallel: `system/decomposeParDict` `numberOfSubdomains`. Sometimes collated.
`postProcessing/` not used yet by the user (will be; format to be sampled).

## Campaign Excel

Sheet `Cases`: header row with a case number column (`# Case`), parameter columns, a `State`
column (`Failed/Done/Running/Pending`), a `Diverged` flag, and a **reference folder** column
holding the case work-dir path → primary matching key with job `WorkDir`. Free-text notes
appear below the table (stop reading at the first row without a case id). Other sheets
(`Comparisons`) group case numbers. Folder names follow `NNN_<parentNNN>_<description>`.
