# ADR 0008 — Display: user scripts that run on the cluster and make images

- Status: accepted (2026-11-18). Amends [ADR 0004](0004-read-only-ssh-and-auth-states.md):
  read-only remains the rule for monitoring, not for SimManager as a whole.

## Context

The user wants to see, inside the run detail, images and GIFs (slices, time series) made
by their own Python scripts, run directly on the cluster where the data is. Until now
SimManager only observed (ADR 0004): the monitor runs allowlisted read-only commands and
the only mutating action of the UI was "Cancel job". On 2026-11-18 the user decided to
move SimManager from read-only to read **and** write/execute for this feature, with:
scripts run on the login node; the script writes its images below the run folder; the
newest image is shown, the others can be browsed; scripts live in a versioned library;
image history kept with dates; both "run now" and periodic auto-refresh.

## Decision

1. **Monitoring stays read-only.** `run_readonly` and its allowlist do not change; the
   monitor, ingest, snapshots and rules never execute user code.
2. **Scripts run only through `run_mutating`** with an `ActionApproval`
   (`display.run_script`), so every execution is in the event log (`action.executed` /
   `action.result`) plus `display.executed` with the run id. Pressing **Run** in the UI is
   the approval; enabling **auto-refresh** is a standing approval for one script on one
   run (`display.schedule_changed` records who enabled it and when; each automatic
   execution says `approved_by: schedule approved <time>`). Auto-refresh only fires while
   the job is `RUNNING` and only while `simman serve` is up (`display` is web-only).
3. **The script travels on stdin, never in the command line.** The command is a login
   `bash -lc` that writes stdin to a `mktemp` file (removed by a trap), runs the setup
   lines (e.g. `module load`), `cd`s to the run folder, runs the interpreter under GNU
   `timeout`, then lists with `find -newer` the images (png/gif/jpg/webp) written meanwhile
   below the run folder (skipping `processor*` and hidden folders). The script body is not
   in the event log.
4. **Library versioning is append-only.** A script is a name plus immutable versions
   (number, SHA-256 of interpreter+setup+body, note, date). Saving unchanged content
   creates nothing; an execution always references the exact version that ran; the UI
   shows versions and unified diffs.
5. **Images are copied into SQLite** (content-addressed `display_image_blobs`), so the
   history survives the remote files. Newest first, at most `max_images_per_execution`
   (20) per execution and `max_image_mb` (20 MB) each; larger or extra ones are listed and
   can be copied on explicit request ("Download anyway", hard cap 1 GiB).
6. **Browsing is read-only SFTP** below the run folder only (paths normalized, `..`
   refused). SVG is not shown (it can carry scripts).
7. **Limits protect the shared login node and the SSH channels**: one execution per run,
   `max_parallel_per_cluster` (1) per cluster, `timeout_s` (300 s), auto-refresh interval
   ≥ `min_interval_s` (60 s).

## Consequences

- AGENTS.md rule 2 now reads "read-only monitoring, explicit user-approved actions".
- The SSH transport gained `run_mutating(..., input=...)` (additive, contract v1).
- Image history grows the database; there is no automatic pruning yet.
- Assumption A402: login nodes have bash, GNU coreutils and GNU find.
- Heavy scripts on login nodes may break cluster usage policies: the user is responsible
  for what a script does; the timeout and per-cluster limit only bound the damage.
