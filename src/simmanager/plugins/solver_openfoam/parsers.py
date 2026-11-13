"""Pure parsers for OpenFOAM dictionaries (`controlDict`, `decomposeParDict`) and solver logs."""

from __future__ import annotations

import contextlib
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from simmanager.contracts.solver import ParsedChunk, SolverStatusUpdate
from simmanager.contracts.timeseries import SeriesKey, SolverState

_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
_LINE_COMMENT_RE = re.compile(r"//[^\n]*")
_KV_RE = re.compile(r"^[ \t]*([A-Za-z0-9_]+)[ \t]+([^;{}\n]+?)[ \t]*;", re.MULTILINE)

_NUM = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?|nan|inf|-inf"

_HEADER_KV_RE = re.compile(r"^(Exec|Date|Host|Case|nProcs|I/O)\s*:\s*(.+)$")
_TIME_RE = re.compile(rf"^Time\s*=\s*({_NUM})\s*$")
_DELTAT_RE = re.compile(rf"^deltaT\s*=\s*({_NUM})\s*$")
_COURANT_RE = re.compile(rf"^Courant Number\s+(?:mean:\s*({_NUM})\s+)?max:\s*({_NUM})")
_CHAR_COURANT_RE = re.compile(
    rf"^Mean and max Characteristic Courant Numbers\s*=\s*({_NUM})\s+({_NUM})"
)
_SOLVING_RE = re.compile(
    rf"^[^:]+:\s*Solving for\s+([^,]+),\s*"
    rf"Initial residual\s*=\s*({_NUM}),\s*"
    rf"Final residual\s*=\s*({_NUM}),\s*"
    r"No Iterations\s+(\d+)"
)
_CONT_ERR_RE = re.compile(
    rf"^time step continuity errors\s*:\s*sum local\s*=\s*({_NUM}),\s*"
    rf"global\s*=\s*({_NUM}),\s*cumulative\s*=\s*({_NUM})"
)
_EXEC_TIME_RE = re.compile(rf"^ExecutionTime\s*=\s*({_NUM})\s*s\s+ClockTime\s*=\s*({_NUM})\s*s")
_MAX_MIN_RE = re.compile(rf"^max/min\s+([A-Za-z0-9_.]+)\s*:\s*({_NUM})\s*/\s*({_NUM})")
_MIN_MAX_RE = re.compile(
    rf"^(?:min/max\s+([A-Za-z0-9_.]+)|([A-Za-z0-9_.]+(?:\s+[A-Za-z0-9_.]+)?)\s+min/max)"
    rf"\s*[:=]\s*({_NUM})\s*/\s*({_NUM})"
)
_MAX_FUNC_RE = re.compile(rf"^max\(([^)]+)\)\s*:\s*({_NUM})")
# ASSUMPTION(A5): Unverified OpenFOAM fatal error header lines (no failing sample yet).
_FATAL_RE = re.compile(r"^FOAM FATAL(?: IO)? ERROR:\s*(.*)$")
_PRE_STEP_START_RE = re.compile(
    r"^(?:Mean and max Characteristic Courant Numbers\s*="
    r"|Courant Number\s+(?:mean:|max:)|deltaT\s*=|Time\s*=)"
)


@dataclass(frozen=True)
class ControlDictInfo:
    application: str | None = None
    start_from: str | None = None
    start_time: float | None = None
    end_time: float | None = None
    delta_t: float | None = None
    write_interval: float | None = None


def strip_foam_comments(text: str) -> str:
    text = _BLOCK_COMMENT_RE.sub("", text)
    return _LINE_COMMENT_RE.sub("", text)


def parse_foam_top_kv(text: str) -> dict[str, str]:
    """Extract top-level `key value;` pairs from an OpenFOAM dictionary (ignoring subdicts)."""
    cleaned = strip_foam_comments(text)
    # Remove braced blocks so nested keys (e.g. FoamFile { version 2.0; }) don't shadow top-level.
    out_chars: list[str] = []
    depth = 0
    for ch in cleaned:
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth = max(0, depth - 1)
        elif depth == 0:
            out_chars.append(ch)
    top_only = "".join(out_chars)
    return {m.group(1): m.group(2).strip() for m in _KV_RE.finditer(top_only)}


def parse_control_dict(text: str) -> ControlDictInfo:
    kv = parse_foam_top_kv(text)
    return ControlDictInfo(
        application=kv.get("application"),
        start_from=kv.get("startFrom"),
        start_time=_as_float(kv.get("startTime")),
        end_time=_as_float(kv.get("endTime")),
        delta_t=_as_float(kv.get("deltaT")),
        write_interval=_as_float(kv.get("writeInterval")),
    )


def parse_decompose_par_dict(text: str) -> int | None:
    kv = parse_foam_top_kv(text)
    val = kv.get("numberOfSubdomains")
    if val is None:
        return None
    try:
        return int(val)
    except ValueError:
        return None


def _as_float(val: str | None) -> float | None:
    if val is None:
        return None
    try:
        return float(val)
    except ValueError:
        return None


def parse_log_header(raw: bytes) -> tuple[dict[str, Any], int]:
    """Parse OpenFOAM log preamble up to the start of the first timestep block."""
    lines = raw.splitlines(keepends=True)
    header: dict[str, Any] = {}
    offset = 0
    saw_starting_loop = False

    for line_b in lines:
        line = line_b.decode("utf-8", errors="replace").strip()
        if line == "Starting time loop":
            offset += len(line_b)
            saw_starting_loop = True
            continue
        if saw_starting_loop and line:
            break
        if (
            _PRE_STEP_START_RE.match(line)
            and (line.startswith("Time =") or "Case" in header or "Exec" in header)
            and (line.startswith("Time =") or line.startswith("deltaT ="))
        ):
            break
        offset += len(line_b)
        m_kv = _HEADER_KV_RE.match(line)
        if m_kv:
            k, v = m_kv.group(1), m_kv.group(2).strip()
            if k == "nProcs":
                with contextlib.suppress(ValueError):
                    header["nProcs"] = int(v)
            else:
                header[k] = v

    return header, offset


def find_log_tail_alignment(chunk: bytes) -> int | None:
    """Align tail-first OpenFOAM reading to the start of a complete timestep block.

    Prefers the line immediately following `ExecutionTime = ...` (which marks the end of the
    previous step, so the next non-empty line is `Courant Number` / `Characteristic Courant` /
    `deltaT =` / `Time =` of the next step). Falls back to the first `Courant Number` /
    `Mean and max Characteristic Courant Numbers` line if no `ExecutionTime` appears.
    """
    nl = chunk.find(b"\n")
    if nl < 0:
        return None
    pos = nl + 1
    waiting_next_step = False
    fallback_pos: int | None = None

    while pos < len(chunk):
        end = chunk.find(b"\n", pos)
        raw_line = chunk[pos:] if end < 0 else chunk[pos : end + 1]
        text = raw_line.decode("utf-8", errors="replace").strip()

        if waiting_next_step:
            if text:
                return pos
        elif text.startswith("ExecutionTime"):
            if _EXEC_TIME_RE.match(text):
                waiting_next_step = True
        elif fallback_pos is None and (
            text.startswith("Mean and max Characteristic Courant Numbers")
            or text.startswith("Courant Number ")
        ):
            fallback_pos = pos

        if end < 0:
            break
        pos = end + 1

    return fallback_pos


def parse_log_chunk(
    file_path: str,
    text: str,
    *,
    header_data: Mapping[str, Any],
    parser_state: Mapping[str, Any],
    is_backfill: bool = False,
) -> ParsedChunk:
    """Pure incremental parser for OpenFOAM solver logs (`foamLog` residual convention)."""
    current_time: float | None = parser_state.get("current_time")
    seen_fields: set[str] = set(parser_state.get("seen_fields", []))
    pending_pre: dict[str, float] = dict(parser_state.get("pending_pre", {}))
    step_minmax: dict[str, tuple[float, float]] = {
        str(k): (float(v[0]), float(v[1]))
        for k, v in dict(parser_state.get("step_minmax", {})).items()
    }
    in_fatal: bool = bool(parser_state.get("in_fatal", False))
    fatal_lines: list[str] = list(parser_state.get("fatal_lines", []))

    buckets: dict[tuple[str, str], tuple[list[float], list[float]]] = {}

    def _add(name: str, unit: str, x: float, y: float) -> None:
        entry = buckets.get((name, unit))
        if entry is None:
            entry = ([], [])
            buckets[(name, unit)] = entry
        entry[0].append(x)
        entry[1].append(y)

    def _flush_minmax() -> None:
        if current_time is not None and step_minmax:
            for field_name, (lo, hi) in step_minmax.items():
                _add(f"{field_name}.min", "", current_time, lo)
                _add(f"{field_name}.max", "", current_time, hi)
        step_minmax.clear()

    solver_state: SolverState | None = None
    error_message: str | None = None
    case_dir: str | None = header_data.get("Case")
    events: list[tuple[str, dict[str, Any]]] = []
    nan_emitted = False

    def _check_nan(name: str, x: float, y: float) -> None:
        nonlocal nan_emitted
        if not nan_emitted and not math.isfinite(y):
            nan_emitted = True
            events.append(("solver.nan_detected", {"file_path": file_path, "column": name, "x": x}))

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        if in_fatal:
            if len(fatal_lines) < 3:
                fatal_lines.append(line)
                error_message = " ".join(fatal_lines)
                if len(fatal_lines) == 1:
                    events.append(
                        ("solver.error", {"file_path": file_path, "message": error_message})
                    )
            if len(fatal_lines) >= 2:
                in_fatal = False
            continue

        m_fatal = _FATAL_RE.match(line)
        if m_fatal:
            solver_state = SolverState.FAILED
            in_fatal = True
            fatal_lines = [m_fatal.group(1).strip()] if m_fatal.group(1).strip() else []
            error_message = fatal_lines[0] if fatal_lines else "FOAM FATAL ERROR"
            if fatal_lines:
                events.append(("solver.error", {"file_path": file_path, "message": error_message}))
            continue

        if line == "End":
            _flush_minmax()
            solver_state = SolverState.COMPLETED
            events.append(("solver.finished", {"file_path": file_path, "state": "completed"}))
            continue

        m_hdr = _HEADER_KV_RE.match(line)
        if m_hdr and current_time is None:
            if m_hdr.group(1) == "Case":
                case_dir = m_hdr.group(2).strip()
            continue

        m_co = _COURANT_RE.match(line)
        if m_co:
            if m_co.group(1) is not None:
                pending_pre["courant.mean"] = float(m_co.group(1))
            pending_pre["courant.max"] = float(m_co.group(2))
            continue

        m_cco = _CHAR_COURANT_RE.match(line)
        if m_cco:
            pending_pre["char_courant.mean"] = float(m_cco.group(1))
            pending_pre["char_courant.max"] = float(m_cco.group(2))
            continue

        m_dt = _DELTAT_RE.match(line)
        if m_dt:
            pending_pre["deltaT"] = float(m_dt.group(1))
            continue

        m_time = _TIME_RE.match(line)
        if m_time:
            _flush_minmax()
            current_time = float(m_time.group(1))
            seen_fields.clear()
            for pname, pval in pending_pre.items():
                unit = "s" if pname == "deltaT" else ""
                _add(pname, unit, current_time, pval)
                _check_nan(pname, current_time, pval)
            pending_pre.clear()
            if solver_state is None:
                solver_state = SolverState.RUNNING
            continue

        if current_time is None:
            continue

        m_solve = _SOLVING_RE.match(line)
        if m_solve:
            fld = m_solve.group(1).strip()
            if fld not in seen_fields:
                seen_fields.add(fld)
                init_res = float(m_solve.group(2))
                _add(f"residual.{fld}", "", current_time, init_res)
                _check_nan(f"residual.{fld}", current_time, init_res)
            continue

        m_cont = _CONT_ERR_RE.match(line)
        if m_cont:
            for sname, raw_v in zip(
                ("continuity.local", "continuity.global", "continuity.cumulative"),
                m_cont.groups(),
                strict=True,
            ):
                v = float(raw_v)
                _add(sname, "", current_time, v)
                _check_nan(sname, current_time, v)
            continue

        m_exec = _EXEC_TIME_RE.match(line)
        if m_exec:
            _flush_minmax()
            ex_t = float(m_exec.group(1))
            clk_t = float(m_exec.group(2))
            _add("execution_time", "s", current_time, ex_t)
            _add("clock_time", "s", current_time, clk_t)
            continue

        m_maxmin = _MAX_MIN_RE.match(line)
        if m_maxmin:
            fname = m_maxmin.group(1)
            step_minmax[fname] = (float(m_maxmin.group(3)), float(m_maxmin.group(2)))
            continue

        m_minmax = _MIN_MAX_RE.match(line)
        if m_minmax:
            fname = (m_minmax.group(1) or m_minmax.group(2)).strip().replace(" ", "_")
            step_minmax[fname] = (float(m_minmax.group(3)), float(m_minmax.group(4)))
            continue

        m_maxf = _MAX_FUNC_RE.match(line)
        if m_maxf:
            fname = f"max({m_maxf.group(1)})"
            val = float(m_maxf.group(2))
            _add(fname, "", current_time, val)
            continue

    series: dict[SeriesKey, tuple[list[float], list[float]]] = {
        SeriesKey(file_path, name, "", unit, "Time", "s"): xy
        for (name, unit), xy in buckets.items()
    }

    new_state: dict[str, Any] = {
        "current_time": current_time,
        "seen_fields": sorted(seen_fields),
        "pending_pre": pending_pre,
        "step_minmax": {k: [v[0], v[1]] for k, v in step_minmax.items()},
        "in_fatal": in_fatal,
        "fatal_lines": fatal_lines,
    }

    if is_backfill:
        status_update = None
    else:
        progress: float | None = 1.0 if solver_state is SolverState.COMPLETED else None
        if solver_state is not None or current_time is not None or error_message is not None:
            status_update = SolverStatusUpdate(
                solver_state=solver_state,
                progress=progress,
                current_time=current_time,
                error_message=error_message,
                case_dir=case_dir,
            )
        else:
            status_update = None

    return ParsedChunk(
        series=series,
        parser_state=new_state,
        status_update=status_update,
        events=events,
    )
