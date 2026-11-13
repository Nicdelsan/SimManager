"""Pure parsers for CONVERGE CFD inputs, stdout (`logout.txt`), and `.out` time-series files."""

from __future__ import annotations

import math
import posixpath
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import yaml

from simmanager.contracts.solver import ParsedChunk, SolverStatusUpdate
from simmanager.contracts.timeseries import SeriesKey, SolverState

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_FOUND_FILE_RE = re.compile(r"^Found the file '[^']+' at '([^']+)'")
_CREATING_PATH_RE = re.compile(r"^creating pathname (\S+)")
_WRITING_POST_RE = re.compile(r"^Writing post file (\S+)")
# ASSUMPTION(A100): Crank-angle runs put the time unit inside parentheses after Time (e.g. (CA)).
_PROGRESS_RE = re.compile(
    r"^\[\s*([0-9]+(?:\.[0-9]+)?)\s*%\]\s+"
    r"ncyc\s+(\d+);\s+"
    r"Time\s+([+-]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)\s*\(([^)]+)\);\s+"
    r"dt=\s*([+-]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)\s*\(([^)]+)\)"
)
_PROGRAM_USED_RE = re.compile(
    r"^(?:Program used|Total Simulation Run Time:)\s*([0-9]+(?:\.[0-9]+)?)\s+seconds"
)
# ASSUMPTION(A101): Unverified CONVERGE fatal error line prefixes (no failing sample yet).
_FATAL_ERR_RE = re.compile(r"^(?:Fatal Error|FATAL ERROR|ERROR):?\s*(.+)$")


@dataclass(frozen=True)
class ConvergeInputs:
    crank_flag: int | None = None
    start_time: float | None = None
    end_time: float | None = None
    restart_flag: int | None = None
    restart_number: int | None = None


def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def parse_inputs_in(text: str) -> ConvergeInputs:
    """Parse CONVERGE `inputs.in` (handles multi-document YAML starting with `version: 4`)."""
    sim_ctrl: Mapping[str, Any] | None = None
    for doc in yaml.safe_load_all(text):
        if isinstance(doc, Mapping) and isinstance(doc.get("simulation_control"), Mapping):
            sim_ctrl = doc["simulation_control"]
            break
    if sim_ctrl is None:
        return ConvergeInputs()

    return ConvergeInputs(
        crank_flag=_as_int(sim_ctrl.get("crank_flag")),
        start_time=_as_float(sim_ctrl.get("start_time")),
        end_time=_as_float(sim_ctrl.get("end_time")),
        restart_flag=_as_int(sim_ctrl.get("restart_flag")),
        restart_number=_as_int(sim_ctrl.get("restart_number")),
    )


def _as_int(val: Any) -> int | None:
    if isinstance(val, bool) or val is None:
        return None
    try:
        return int(val)
    except (TypeError, ValueError):
        return None


def _as_float(val: Any) -> float | None:
    if isinstance(val, bool) or val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _strip_parens(token: str) -> str:
    if token.startswith("(") and token.endswith(")"):
        return token[1:-1]
    return token


def _derive_output_dir(path: str) -> str:
    norm = posixpath.normpath(path)
    if posixpath.basename(norm) == "output":
        return posixpath.dirname(norm)
    return norm


def parse_out_header(raw: bytes) -> tuple[dict[str, Any], int]:
    """Parse a CONVERGE `.out` header up to the first non-`#` line.

    Handles both headers ending with `#` (`thermo.out`) and headers where qualifier row is
    immediately followed by numeric rows (`area_avg_regions_flow.out`).
    """
    lines = raw.splitlines(keepends=True)
    header_rows: list[list[str]] = []
    offset = 0
    for line_b in lines:
        line = line_b.decode("utf-8", errors="replace")
        stripped = line.strip()
        if not stripped:
            offset += len(line_b)
            continue
        if stripped.startswith("#"):
            offset += len(line_b)
            tokens = stripped.lstrip("#").split()
            if tokens:
                header_rows.append(tokens)
            continue
        break

    if not header_rows:
        return {}, offset

    # Find names row: right after `column 1 2 ...` row if present.
    col_idx = None
    for idx, row in enumerate(header_rows):
        if row[0].lower() == "column":
            col_idx = idx
            break

    start_idx = col_idx + 1 if col_idx is not None else 1
    if start_idx >= len(header_rows):
        return {}, offset

    names = header_rows[start_idx]
    units_raw = header_rows[start_idx + 1] if start_idx + 1 < len(header_rows) else []
    units = (
        [_strip_parens(u) for u in units_raw] if len(units_raw) == len(names) else [""] * len(names)
    )

    qual_rows = header_rows[start_idx + 2 :] if start_idx + 2 < len(header_rows) else []
    qualifiers: list[str] = []
    for i in range(len(names)):
        parts: list[str] = []
        for qrow in qual_rows:
            if i < len(qrow):
                q = qrow[i]
                if q != "(none)" and q != "none":
                    parts.append(q)
        qualifiers.append("/".join(parts))

    columns = [
        {
            "name": names[i],
            "unit": "" if units[i] in {"none", ""} else units[i],
            "qualifier": qualifiers[i],
        }
        for i in range(len(names))
    ]
    return {"columns": columns}, offset


def parse_out_chunk(
    file_path: str,
    text: str,
    *,
    header_data: Mapping[str, Any],
    parser_state: Mapping[str, Any],
) -> ParsedChunk:
    """Parse numeric rows of a CONVERGE `.out` file."""
    cols = header_data.get("columns")
    if not isinstance(cols, list) or len(cols) < 2:
        return ParsedChunk(parser_state=dict(parser_state))

    x_col = cols[0]
    x_name = str(x_col.get("name", "Time"))
    x_unit = str(x_col.get("unit", ""))

    col_keys: list[SeriesKey] = [
        SeriesKey(
            file_path=file_path,
            name=str(c["name"]),
            qualifier=str(c.get("qualifier", "")),
            unit=str(c.get("unit", "")),
            x_name=x_name,
            x_unit=x_unit,
        )
        for c in cols[1:]
    ]

    xs_all: list[float] = []
    ys_cols: list[list[float]] = [[] for _ in col_keys]
    nan_event: tuple[str, dict[str, Any]] | None = None
    expected_len = len(cols)

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) < expected_len:
            continue
        try:
            x_val = float(parts[0])
        except ValueError:
            continue
        row_ys: list[float] = []
        valid = True
        for i, tok in enumerate(parts[1:expected_len]):
            try:
                y_val = float(tok)
            except ValueError:
                valid = False
                break
            if not math.isfinite(y_val) and nan_event is None:
                nan_event = (
                    "solver.nan_detected",
                    {"file_path": file_path, "column": col_keys[i].name, "x": x_val},
                )
            row_ys.append(y_val)
        if not valid:
            continue
        xs_all.append(x_val)
        for i, y_val in enumerate(row_ys):
            ys_cols[i].append(y_val)

    series = (
        {key: (list(xs_all), ys) for key, ys in zip(col_keys, ys_cols, strict=True)}
        if xs_all
        else {}
    )
    events = [nan_event] if nan_event is not None else []
    return ParsedChunk(series=series, parser_state=dict(parser_state), events=events)


def parse_stdout_header(raw: bytes) -> tuple[dict[str, Any], int]:
    """Parse CONVERGE `logout.txt` preamble up to the first progress line."""
    lines = raw.splitlines(keepends=True)
    offset = 0
    case_dir: str | None = None
    output_dir: str | None = None

    for line_b in lines:
        line = strip_ansi(line_b.decode("utf-8", errors="replace")).strip()
        if _PROGRESS_RE.match(line):
            break
        offset += len(line_b)
        if case_dir is None:
            m_found = _FOUND_FILE_RE.match(line)
            if m_found:
                case_dir = posixpath.dirname(m_found.group(1))
        m_path = _CREATING_PATH_RE.match(line)
        if m_path:
            output_dir = _derive_output_dir(m_path.group(1))
        elif output_dir is None:
            m_post = _WRITING_POST_RE.match(line)
            if m_post:
                output_dir = posixpath.dirname(
                    posixpath.dirname(posixpath.normpath(m_post.group(1)))
                )

    header: dict[str, Any] = {}
    if case_dir:
        header["case_dir"] = case_dir
    if output_dir:
        header["output_dir"] = output_dir
    return header, offset


def parse_stdout_chunk(
    file_path: str,
    text: str,
    *,
    header_data: Mapping[str, Any],
    parser_state: Mapping[str, Any],
    is_backfill: bool = False,
) -> ParsedChunk:
    """Parse CONVERGE `logout.txt` incremental lines."""
    times: list[float] = []
    dts: list[float] = []
    pcts: list[float] = []
    time_unit = str(parser_state.get("time_unit", "sec"))

    case_dir: str | None = header_data.get("case_dir")
    output_dir: str | None = header_data.get("output_dir")
    solver_state: SolverState | None = None
    latest_pct: float | None = None
    latest_time: float | None = None
    latest_ncyc: int | None = None
    warning_delta = 0
    error_message: str | None = None
    run_time_s: float | None = None
    events: list[tuple[str, dict[str, Any]]] = []

    for raw_line in text.splitlines():
        line = strip_ansi(raw_line).strip()
        if not line:
            continue

        if case_dir is None:
            m_found = _FOUND_FILE_RE.match(line)
            if m_found:
                case_dir = posixpath.dirname(m_found.group(1))
                continue

        m_create = _CREATING_PATH_RE.match(line)
        if m_create:
            output_dir = _derive_output_dir(m_create.group(1))
            continue

        m_post = _WRITING_POST_RE.match(line)
        if m_post and output_dir is None:
            output_dir = posixpath.dirname(posixpath.dirname(posixpath.normpath(m_post.group(1))))
            continue

        m_prog = _PROGRESS_RE.match(line)
        if m_prog:
            pct = float(m_prog.group(1))
            ncyc = int(m_prog.group(2))
            t_val = float(m_prog.group(3))
            time_unit = m_prog.group(4).strip()
            dt_val = float(m_prog.group(5))

            times.append(t_val)
            dts.append(dt_val)
            pcts.append(pct)
            latest_pct = pct / 100.0
            latest_time = t_val
            latest_ncyc = ncyc
            if solver_state is None:
                solver_state = SolverState.RUNNING
            continue

        if line.startswith("recovering "):
            warning_delta += 1
            continue

        if line.startswith("normal termination"):
            solver_state = SolverState.COMPLETED
            latest_pct = 1.0
            events.append(("solver.finished", {"file_path": file_path, "state": "completed"}))
            continue

        m_used = _PROGRAM_USED_RE.match(line)
        if m_used:
            run_time_s = float(m_used.group(1))
            continue

        if line.startswith("Found file STOP."):
            solver_state = SolverState.STOPPED
            events.append(("solver.finished", {"file_path": file_path, "state": "stopped"}))
            continue

        m_err = _FATAL_ERR_RE.match(line)
        if m_err:
            solver_state = SolverState.FAILED
            error_message = m_err.group(1)
            events.append(("solver.error", {"file_path": file_path, "message": error_message}))

    series: dict[SeriesKey, tuple[list[float], list[float]]] = {}
    if times:
        series[SeriesKey(file_path, "dt", "", time_unit, "Time", time_unit)] = (times, dts)
        series[SeriesKey(file_path, "progress_pct", "", "%", "Time", time_unit)] = (
            list(times),
            pcts,
        )

    new_state = dict(parser_state)
    new_state["time_unit"] = time_unit

    if is_backfill:
        status_update = (
            SolverStatusUpdate(warning_delta=warning_delta) if warning_delta > 0 else None
        )
    else:
        meta = {"run_time_s": run_time_s} if run_time_s is not None else {}
        if (
            any(
                v is not None
                for v in (
                    solver_state,
                    latest_pct,
                    latest_time,
                    latest_ncyc,
                    error_message,
                    case_dir,
                    output_dir,
                )
            )
            or warning_delta > 0
            or meta
        ):
            status_update = SolverStatusUpdate(
                solver_state=solver_state,
                progress=latest_pct,
                current_time=latest_time,
                ncyc=latest_ncyc,
                warning_delta=warning_delta,
                error_message=error_message,
                case_dir=case_dir,
                output_dir=output_dir,
                metadata=meta,
            )
        else:
            status_update = None

    return ParsedChunk(
        series=series,
        parser_state=new_state,
        status_update=status_update,
        events=events,
    )
