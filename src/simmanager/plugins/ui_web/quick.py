"""Quick actions of a run's terminal: commands *inserted* into the terminal, never run.

The user presses Enter. Paths are absolute and shell-quoted, so the command is right
wherever the shell currently is. The only action that executes something ("cancel job")
is not here: it goes through `run_mutating` with an explicit confirmation (`actions.py`).
"""

from __future__ import annotations

import posixpath
import shlex
from dataclasses import dataclass

MAX_INPUT_FILES = 60
STDOUT_PARSERS = frozenset({"converge.stdout", "openfoam.log"})
OPENFOAM_INPUTS = (
    "system/controlDict",
    "system/fvSchemes",
    "system/fvSolution",
    "system/decomposeParDict",
)


@dataclass(frozen=True)
class QuickAction:
    group: str
    label: str
    command: str


def _q(path: str) -> str:
    return shlex.quote(path)


def quick_actions(
    *,
    work_dir: str | None,
    job_id: str | None,
    logs: list[str],
    inputs: list[str],
) -> list[QuickAction]:
    """`logs` and `inputs` are absolute remote paths."""
    out: list[QuickAction] = []
    if work_dir:
        out += [
            QuickAction("Folder", "cd to the run folder", f"cd {_q(work_dir)}"),
            QuickAction("Folder", "newest files", f"ls -lt {_q(work_dir)} | head -n 25"),
        ]
    for log in logs:
        name = posixpath.basename(log)
        out += [
            QuickAction("Log", f"tail -n 100 {name}", f"tail -n 100 {_q(log)}"),
            QuickAction("Log", f"tail -f {name}", f"tail -f {_q(log)}"),
            QuickAction("Log", f"less {name}", f"less +G {_q(log)}"),
        ]
    if job_id:
        j = _q(job_id)
        out += [
            QuickAction("Job", f"squeue -j {job_id}", f"squeue -j {j}"),
            QuickAction(
                "Job",
                f"sacct -j {job_id}",
                f"sacct -j {j} --format=JobID,JobName,State,Elapsed,ExitCode,NodeList",
            ),
            QuickAction("Job", f"scontrol show job {job_id}", f"scontrol show job {j}"),
        ]
    for path in inputs[:MAX_INPUT_FILES]:
        rel = posixpath.relpath(path, work_dir) if work_dir else path
        out.append(QuickAction("Edit input", f"vi {rel}", f"vi {_q(path)}"))
    return out


def input_candidates(solver: str | None, case_dir: str, names: list[str]) -> list[str]:
    """Input files worth a "vi" entry, from a listing of the case folder (and `system/`)."""
    if solver == "openfoam":
        return [posixpath.join(case_dir, n) for n in OPENFOAM_INPUTS if n in names]
    return sorted(posixpath.join(case_dir, n) for n in names if n.endswith(".in"))
