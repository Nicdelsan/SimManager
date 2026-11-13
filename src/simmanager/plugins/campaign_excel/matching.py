"""Pure matching, path-root scoping, NNN_MMM derivation, and case-state evaluation."""

from __future__ import annotations

import posixpath
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from simmanager.contracts.campaign import (
    DEFAULT_NAMING_REGEX,
    CaseComputedState,
    CaseRecord,
    MatchKind,
    RunExecutionFacts,
)
from simmanager.contracts.scheduler import JobState
from simmanager.contracts.storage import JobRecord, RunRecord
from simmanager.contracts.timeseries import SeriesMeta, SolverState

_DIVERGE_RE = re.compile(r"\b(?:diverg\w*|nan|inf|floating point|cfl)\b", re.IGNORECASE)


def normalize_work_dir(raw: str | None) -> str | None:
    """Normalize a remote POSIX path for reliable comparisons."""
    if raw is None:
        return None
    cleaned = raw.strip().replace("\\", "/")
    if not cleaned:
        return None
    norm = posixpath.normpath(cleaned)
    if norm == ".":
        return None
    if len(norm) > 1:
        norm = norm.rstrip("/")
    return norm


def is_under_root(path: str | None, root: str | None) -> bool:
    """Return True when `path` is equal to or strictly inside `root` on a '/' boundary."""
    norm_path = normalize_work_dir(path)
    norm_root = normalize_work_dir(root)
    if norm_path is None or norm_root is None or norm_root == "/":
        return False
    return norm_path == norm_root or norm_path.startswith(norm_root + "/")


def infer_work_root(work_dirs: Sequence[str | None]) -> str | None:
    """Infer a campaign's common directory root from its cases' work_dirs."""
    normalized = sorted({w for raw in work_dirs if (w := normalize_work_dir(raw)) is not None})
    if not normalized:
        return None
    if len(normalized) == 1:
        parent = posixpath.dirname(normalized[0])
        return None if parent in ("", "/", ".") else parent
    try:
        common = posixpath.commonpath(normalized)
    except ValueError:
        return None
    if common in ("", "/", "."):
        return None
    return common


def compile_naming_regex(pattern: str | None) -> re.Pattern[str] | None:
    if not pattern:
        return None
    return re.compile(pattern)


def extract_naming_groups(
    text: str | None,
    pattern: re.Pattern[str] | None,
) -> tuple[str | None, str | None]:
    """Extract `(case_token, parent_token)` from `text` using named regex groups."""
    if not text or pattern is None:
        return None, None
    match = pattern.match(text.strip())
    if match is None:
        return None, None
    groups = match.groupdict()
    case_tok = groups.get("case")
    parent_tok = groups.get("parent")
    return (
        case_tok.strip() if isinstance(case_tok, str) and case_tok.strip() else None,
        parent_tok.strip() if isinstance(parent_tok, str) and parent_tok.strip() else None,
    )


def extract_parent_key(
    case_key: str,
    work_dir: str | None,
    naming_regex: str | None = DEFAULT_NAMING_REGEX,
) -> str | None:
    """Extract optional derivation parent token from `case_key` or `basename(work_dir)`."""
    pattern = compile_naming_regex(naming_regex)
    if pattern is None:
        return None
    for candidate in (case_key, posixpath.basename(work_dir) if work_dir else None):
        case_tok, parent_tok = extract_naming_groups(candidate, pattern)
        if parent_tok and not keys_equivalent(parent_tok, case_tok or case_key):
            return parent_tok
    return None


def keys_equivalent(a: str, b: str) -> bool:
    """Exact string equality, or numeric equality for non-negative integers (`003` == `3`)."""
    a_s = a.strip()
    b_s = b.strip()
    if a_s == b_s:
        return True
    return a_s.isdigit() and b_s.isdigit() and int(a_s) == int(b_s)


def match_run_to_cases(
    run: RunRecord,
    job: JobRecord | None,
    cases: Sequence[CaseRecord],
    *,
    work_root: str | None,
    naming_regex: str | None,
) -> tuple[MatchKind | None, str | None, list[CaseRecord]]:
    """Match one run against active cases of a campaign in strict priority order.

    1. `auto_work_dir`: exact normalized work_dir (or relative path suffix on '/' boundary).
    2. `auto_dir_name` (only when `run.work_dir` is under `work_root`): basename match.
    3. `auto_regex` (only when `run.work_dir` is under `work_root`): `naming_regex` match.
    """
    active_cases = [c for c in cases if c.active]
    run_wd = normalize_work_dir(run.work_dir or (job.info.work_dir if job else None))

    if run_wd is not None:
        exact = [
            c
            for c in active_cases
            if (cw := normalize_work_dir(c.work_dir)) is not None and cw == run_wd
        ]
        if exact:
            return MatchKind.AUTO_WORK_DIR, run_wd, exact

        suffix = [
            c
            for c in active_cases
            if (cw := normalize_work_dir(c.work_dir)) is not None
            and not cw.startswith("/")
            and run_wd.endswith("/" + cw)
        ]
        if suffix:
            return MatchKind.AUTO_WORK_DIR, run_wd, suffix

    if run_wd is None or not is_under_root(run_wd, work_root):
        return None, None, []

    run_base = posixpath.basename(run_wd)
    by_name = [
        c
        for c in active_cases
        if (
            (cw := normalize_work_dir(c.work_dir)) is not None
            and posixpath.basename(cw) == run_base
        )
        or c.case_key == run_base
    ]
    if by_name:
        return MatchKind.AUTO_DIR_NAME, run_base, by_name

    pattern = compile_naming_regex(naming_regex)
    if pattern is not None:
        for source_name in (run_base, job.info.name if job else None):
            case_tok, _ = extract_naming_groups(source_name, pattern)
            if case_tok is not None:
                by_regex = [c for c in active_cases if keys_equivalent(case_tok, c.case_key)]
                if by_regex:
                    return MatchKind.AUTO_REGEX, f"{source_name} -> {case_tok}", by_regex

    return None, None, []


def _is_run_active(facts: RunExecutionFacts) -> bool:
    if facts.job is not None:
        return facts.job.active or not facts.job.info.state.terminal
    return facts.solver_state is not None and facts.solver_state.solver_state is SolverState.RUNNING


def _run_sort_key(facts: RunExecutionFacts) -> tuple[datetime, int]:
    ts = (
        facts.job.info.submit_time
        if facts.job is not None and facts.job.info.submit_time is not None
        else facts.run.created_at
    )
    return (ts, facts.run.id)


def select_governing_run(facts_list: Sequence[RunExecutionFacts]) -> RunExecutionFacts | None:
    """Choose the governing run for a case: latest active run, else latest run overall."""
    if not facts_list:
        return None
    active = [f for f in facts_list if _is_run_active(f)]
    pool = active if active else list(facts_list)
    return max(pool, key=_run_sort_key)


def _mentions_divergence(message: str | None) -> bool:
    return bool(message and _DIVERGE_RE.search(message))


def compute_case_state(facts_list: Sequence[RunExecutionFacts]) -> CaseComputedState:
    """Derive a case's computed state from its linked runs, jobs, and solver states."""
    gov = select_governing_run(facts_list)
    if gov is None:
        return CaseComputedState.PLANNED

    job_state = gov.job.info.state if gov.job is not None else None
    solver_state = gov.solver_state.solver_state if gov.solver_state is not None else None
    err_msg = gov.solver_state.error_message if gov.solver_state is not None else None

    if job_state is JobState.CANCELLED:
        return CaseComputedState.CANCELLED

    if solver_state is SolverState.FAILED:
        # ASSUMPTION(A200): SolverState.FAILED (or a failed JobState) combined with
        # `solver.nan_detected` or divergence keywords in `error_message` indicates `diverged`.
        if gov.has_nan_detected or _mentions_divergence(err_msg):
            return CaseComputedState.DIVERGED
        return CaseComputedState.FAILED

    if job_state in {JobState.FAILED, JobState.TIMEOUT}:
        if gov.has_nan_detected or _mentions_divergence(err_msg):
            return CaseComputedState.DIVERGED
        return CaseComputedState.FAILED

    if job_state is JobState.QUEUED:
        return CaseComputedState.QUEUED

    if job_state in {JobState.RUNNING, JobState.COMPLETING} or (
        job_state is None and solver_state is SolverState.RUNNING
    ):
        return CaseComputedState.RUNNING

    if solver_state is SolverState.STOPPED:
        return CaseComputedState.STOPPED

    if solver_state is SolverState.COMPLETED or job_state is JobState.COMPLETED:
        return CaseComputedState.COMPLETED

    if job_state is JobState.UNKNOWN:
        if gov.has_nan_detected:
            return CaseComputedState.DIVERGED
        return CaseComputedState.UNKNOWN

    if gov.solver_state is None:
        return CaseComputedState.PLANNED

    return CaseComputedState.UNKNOWN


@dataclass(frozen=True)
class MonitorSelector:
    """Parsed `file:series[:qualifier]` (or bare `series`) export monitor selector."""

    raw: str
    file_filter: str | None
    series_name: str
    qualifier: str | None = None

    @property
    def column_label(self) -> str:
        return f"SM_Monitor_{self.raw}"


def parse_monitor_selector(spec: str) -> MonitorSelector:
    parts = spec.split(":", 2)
    if len(parts) == 1:
        return MonitorSelector(raw=spec, file_filter=None, series_name=parts[0].strip())
    if len(parts) == 2:
        return MonitorSelector(
            raw=spec,
            file_filter=parts[0].strip() or None,
            series_name=parts[1].strip(),
        )
    return MonitorSelector(
        raw=spec,
        file_filter=parts[0].strip() or None,
        series_name=parts[1].strip(),
        qualifier=parts[2].strip(),
    )


def select_series_value(series: Sequence[SeriesMeta], selector: MonitorSelector) -> float | None:
    """Return `last_y` of the first series matching `selector`, or None."""
    for s in series:
        if s.name != selector.series_name:
            continue
        if selector.file_filter is not None and not (
            s.file_path == selector.file_filter
            or posixpath.basename(s.file_path) == selector.file_filter
            or s.file_path.endswith("/" + selector.file_filter)
        ):
            continue
        if selector.qualifier is not None and s.qualifier != selector.qualifier:
            continue
        return s.last_y
    return None
