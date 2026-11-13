"""Pure parsers for SLURM command output. Formats: docs/kb/domain.md#slurm."""

from __future__ import annotations

import logging
import re
from datetime import datetime, tzinfo

from simmanager.contracts.scheduler import JobInfo, JobState

log = logging.getLogger("simmanager.plugin.scheduler-slurm")

# ASSUMPTION(A2): `%Z` (work dir) and `-r` (one line per array task) work on Slurm 18.08.
SQUEUE_FORMAT = "%i|%j|%T|%M|%l|%D|%P|%V|%S|%R|%Z"
SQUEUE_FIELDS = 11

# ASSUMPTION(A7): `sacct -P` prints these fields in this order, `|`-separated, one line per
# allocation with -X, and -j without -S searches the whole history (Slurm >= 2x).
SACCT_FIELDS = (
    "JobID,JobName,State,ExitCode,Elapsed,Submit,Start,End,WorkDir,Partition,NNodes,"
    "Timelimit,User,NodeList"
)
SACCT_DISABLED = "accounting storage is disabled"

_STATE_MAP: dict[str, JobState] = {
    **dict.fromkeys(
        ["PENDING", "REQUEUED", "REQUEUE_HOLD", "REQUEUE_FED", "RESV_DEL_HOLD", "SPECIAL_EXIT"],
        JobState.QUEUED,
    ),
    **dict.fromkeys(
        [
            "RUNNING",
            "CONFIGURING",
            "SUSPENDED",
            "STOPPED",
            "SIGNALING",
            "RESIZING",
            "STAGE_OUT",
        ],
        JobState.RUNNING,
    ),
    "COMPLETING": JobState.COMPLETING,
    "COMPLETED": JobState.COMPLETED,
    **dict.fromkeys(
        ["FAILED", "NODE_FAIL", "OUT_OF_MEMORY", "BOOT_FAIL", "DEADLINE", "PREEMPTED", "REVOKED"],
        JobState.FAILED,
    ),
    "CANCELLED": JobState.CANCELLED,
    "TIMEOUT": JobState.TIMEOUT,
}

_SHORT_STATES = {"PD": "PENDING", "R": "RUNNING", "CG": "COMPLETING", "CD": "COMPLETED"}


def normalize_state(raw: str) -> JobState:
    """`CANCELLED by 123`, `CANCELLED+`, `PD` ... -> JobState."""
    word = raw.strip().split(" ", 1)[0].rstrip("+").upper()
    word = _SHORT_STATES.get(word, word)
    return _STATE_MAP.get(word, JobState.UNKNOWN)


_NONE_VALUES = {"", "n/a", "none", "unknown", "(null)", "not_set", "invalid"}


def _opt(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return None if value.lower() in _NONE_VALUES else value


def parse_duration(value: str | None) -> int | None:
    """`[D-]HH:MM:SS`, `MM:SS`, `MM` -> seconds. `UNLIMITED`/`N/A` -> None."""
    value = _opt(value)
    if value is None or not value[0].isdigit():
        return None
    days = 0
    if "-" in value:
        d, value = value.split("-", 1)
        days = int(d)
    parts = [float(p) for p in value.split(":")]
    if len(parts) == 1 and days:
        parts = [parts[0], 0, 0]  # "D-HH"
    elif len(parts) == 2 and days:
        parts = [parts[0], parts[1], 0]  # "D-HH:MM"
    seconds = 0.0
    for part in parts:
        seconds = seconds * 60 + part
    if len(parts) == 1:
        seconds *= 60  # a bare number is minutes
    return int(days * 86400 + seconds)


def parse_time(value: str | None, tz: tzinfo | None) -> datetime | None:
    """`2026-11-12T03:05:26` in cluster-local time -> aware datetime."""
    value = _opt(value)
    if value is None:
        return None
    try:
        naive = datetime.fromisoformat(value)
    except ValueError:
        return None
    # ASSUMPTION(A6): one configurable zone per cluster; None = this machine's local zone.
    return naive.replace(tzinfo=tz) if tz is not None else naive.astimezone()


def _int(value: str | None) -> int | None:
    value = _opt(value)
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


# ------------------------------------------------------------------------- squeue
def squeue_argv(user: str) -> list[str]:
    return ["squeue", "-h", "-r", "-u", user, "-o", SQUEUE_FORMAT]


def parse_squeue(text: str, tz: tzinfo | None) -> list[JobInfo]:
    jobs = []
    for line in text.splitlines():
        if not line.strip():
            continue
        fields = line.split("|")
        if len(fields) != SQUEUE_FIELDS:
            log.warning("squeue: skipping line with %d fields: %r", len(fields), line[:200])
            continue
        job_id, name, raw, elapsed, limit, nodes, part, submit, start, rnl, work_dir = fields
        state = normalize_state(raw)
        pending = state is JobState.QUEUED
        reason_or_nodes = rnl.strip()
        if reason_or_nodes.startswith("(") and reason_or_nodes.endswith(")"):
            reason_or_nodes = reason_or_nodes[1:-1]
        jobs.append(
            JobInfo(
                job_id=job_id.strip(),
                name=name.strip(),
                state=state,
                raw_state=raw.strip(),
                work_dir=_opt(work_dir),
                partition=_opt(part),
                nodes=_int(nodes),
                node_list=None if pending else _opt(reason_or_nodes),
                reason=_opt(reason_or_nodes) if pending else None,
                elapsed_s=parse_duration(elapsed),
                time_limit_s=parse_duration(limit),
                submit_time=parse_time(submit, tz),
                start_time=None if pending else parse_time(start, tz),
            )
        )
    return jobs


# -------------------------------------------------------------------------- sacct
def sacct_argv(job_ids: list[str]) -> list[str]:
    return ["sacct", "-X", "-n", "-P", "-j", ",".join(job_ids), "-o", SACCT_FIELDS]


def parse_sacct(text: str, tz: tzinfo | None) -> list[JobInfo]:
    n_fields = SACCT_FIELDS.count(",") + 1
    jobs = []
    for line in text.splitlines():
        if not line.strip():
            continue
        fields = line.split("|")
        if len(fields) != n_fields:
            log.warning("sacct: skipping line with %d fields: %r", len(fields), line[:200])
            continue
        (job_id, name, raw, exit_code, elapsed, submit, start, end, work_dir, part, nodes,
         limit, user, node_list) = fields  # fmt: skip
        jobs.append(
            JobInfo(
                job_id=job_id.strip(),
                name=name.strip(),
                state=normalize_state(raw),
                raw_state=raw.strip(),
                user=_opt(user),
                work_dir=_opt(work_dir),
                partition=_opt(part),
                nodes=_int(nodes),
                node_list=_opt(node_list),
                exit_code=_opt(exit_code),
                elapsed_s=parse_duration(elapsed),
                time_limit_s=parse_duration(limit),
                submit_time=parse_time(submit, tz),
                start_time=parse_time(start, tz),
                end_time=parse_time(end, tz),
            )
        )
    return jobs


# ----------------------------------------------------------------------- scontrol
# Values of these keys may contain spaces: they run to the end of their line.
_FREE_TEXT_KEYS = {"Comment", "Command", "WorkDir", "StdErr", "StdIn", "StdOut"}
_KEY = re.compile(r"(?:^|(?<=\s))([A-Za-z][A-Za-z0-9_:/]*)=")


def scontrol_argv(job_id: str) -> list[str]:
    return ["scontrol", "show", "job", job_id]


def parse_scontrol_records(text: str) -> list[dict[str, str]]:
    """`scontrol show job` output -> one {key: value} dict per job record."""
    records: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            current = None
            continue
        if line.startswith("JobId="):
            current = {}
            records.append(current)
        if current is None:
            continue
        current.update(_split_keys(line))
    return records


def _split_keys(line: str) -> dict[str, str]:
    out: dict[str, str] = {}
    matches = list(_KEY.finditer(line))
    for i, m in enumerate(matches):
        key = m.group(1)
        if key in _FREE_TEXT_KEYS:
            out[key] = line[m.end() :].rstrip()
            break
        end = matches[i + 1].start() if i + 1 < len(matches) else len(line)
        out[key] = line[m.end() : end].strip()
    return out


_STDOUT_PATTERN = re.compile(r"%(\d*)([%jxuAanN])")


def expand_stdout(pattern: str | None, rec: dict[str, str]) -> str | None:
    """Expand the filename patterns SLURM leaves unexpanded in `StdOut`/`StdErr`."""
    pattern = _opt(pattern)
    if pattern is None or "%" not in pattern:
        return pattern
    user = (rec.get("UserId") or "").split("(", 1)[0]
    values = {
        "%": "%",
        "j": rec.get("JobId", ""),
        "x": rec.get("JobName", ""),
        "u": user,
        "A": rec.get("ArrayJobId", rec.get("JobId", "")),
        "a": rec.get("ArrayTaskId", "4294967294"),
        "n": "0",
        "N": (rec.get("BatchHost") or "").split(".", 1)[0],
    }

    def sub(m: re.Match[str]) -> str:
        return values[m.group(2)].zfill(int(m.group(1) or 0))

    return _STDOUT_PATTERN.sub(sub, pattern)


def _scontrol_job_id(rec: dict[str, str]) -> str:
    array_job, task = rec.get("ArrayJobId"), rec.get("ArrayTaskId")
    if array_job and task:
        return f"{array_job}_{task}" if task.isdigit() else f"{array_job}_[{task}]"
    return rec.get("JobId", "")


def parse_scontrol(text: str, tz: tzinfo | None) -> list[JobInfo]:
    jobs = []
    for rec in parse_scontrol_records(text):
        raw = rec.get("JobState", "")
        jobs.append(
            JobInfo(
                job_id=_scontrol_job_id(rec),
                name=rec.get("JobName", ""),
                state=normalize_state(raw),
                raw_state=raw,
                user=_opt((rec.get("UserId") or "").split("(", 1)[0]),
                work_dir=_opt(rec.get("WorkDir")),
                partition=_opt(rec.get("Partition")),
                nodes=_int(rec.get("NumNodes")),
                node_list=_opt(rec.get("NodeList")),
                reason=_opt(rec.get("Reason")),
                exit_code=_opt(rec.get("ExitCode")),
                elapsed_s=parse_duration(rec.get("RunTime")),
                time_limit_s=parse_duration(rec.get("TimeLimit")),
                submit_time=parse_time(rec.get("SubmitTime"), tz),
                start_time=parse_time(rec.get("StartTime"), tz),
                end_time=_end_time(rec, tz),
                stdout_path=expand_stdout(rec.get("StdOut"), rec),
            )
        )
    return jobs


def _end_time(rec: dict[str, str], tz: tzinfo | None) -> datetime | None:
    # For running jobs EndTime is the time-limit deadline, not an end: keep it only when ended.
    if not normalize_state(rec.get("JobState", "")).terminal:
        return None
    return parse_time(rec.get("EndTime"), tz)
