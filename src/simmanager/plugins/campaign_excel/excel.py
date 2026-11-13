"""Pure `openpyxl` Excel preview, parsing, and enriched export."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import copy
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

import openpyxl
from openpyxl.utils import get_column_letter

from simmanager.contracts.campaign import (
    DEFAULT_NAMING_REGEX,
    CampaignRecord,
    CaseSpec,
    CaseSummary,
    ExcelPreview,
)

from .matching import extract_parent_key, infer_work_root, normalize_work_dir

_CASE_ALIASES = ("# case", "case", "case id", "case_id", "case key", "case_key", "#")
_WORKDIR_KEYWORDS = ("work_dir", "workdir", "folder", "path", "dir")
_STATE_ALIASES = ("state", "status", "manual state", "manual_state")


def _cell_to_str(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else str(value)
    if isinstance(value, datetime):
        return value.isoformat(sep=" ")
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def _select_sheet_name(sheetnames: Sequence[str], requested: str | None) -> str:
    if requested is not None:
        if requested not in sheetnames:
            raise ValueError(f"sheet {requested!r} not found (available: {', '.join(sheetnames)})")
        return requested
    for name in sheetnames:
        if name.strip().lower() == "cases":
            return name
    return sheetnames[0]


def _detect_header_row(rows: Sequence[tuple[Any, ...]]) -> int:
    for idx, row in enumerate(rows[:20], start=1):
        non_empty = sum(1 for cell in row if _cell_to_str(cell).strip() != "")
        if non_empty >= 2:
            return idx
    return 1


def _detect_column(headers: Sequence[str], kind: str) -> str | None:
    lowered = [(h, h.strip().lower()) for h in headers]
    if kind == "case":
        for orig, low in lowered:
            if low in _CASE_ALIASES:
                return orig
        for orig, low in lowered:
            if "case" in low:
                return orig
        return None
    if kind == "work_dir":
        for orig, low in lowered:
            if any(kw in low for kw in _WORKDIR_KEYWORDS):
                return orig
        return None
    if kind == "manual_state":
        for orig, low in lowered:
            if low in _STATE_ALIASES:
                return orig
        return None
    return None


@dataclass(frozen=True)
class ParsedWorkbook:
    sheet_name: str
    header_row: int
    case_column: str
    work_dir_column: str | None
    manual_state_column: str | None
    columns: tuple[str, ...]
    cases: tuple[CaseSpec, ...]
    inferred_work_root: str | None


def preview_workbook(
    path: Path,
    *,
    sheet_name: str | None = None,
    header_row: int | None = None,
    stop_on_blank_case: bool = True,
) -> ExcelPreview:
    """Inspect an `.xlsx` file and propose sheet, header row, and key columns."""
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        sheets = tuple(wb.sheetnames)
        if not sheets:
            raise ValueError(f"workbook {path} has no sheets")
        selected = _select_sheet_name(sheets, sheet_name)
        ws = wb[selected]
        all_rows = [tuple(row) for row in ws.iter_rows(values_only=True)]
    finally:
        wb.close()

    detected_header_row = header_row if header_row is not None else _detect_header_row(all_rows)
    if detected_header_row < 1 or detected_header_row > max(1, len(all_rows)):
        raise ValueError(f"header_row {detected_header_row} out of range (1..{len(all_rows)})")

    raw_headers = all_rows[detected_header_row - 1] if all_rows else ()
    raw_header_names = [_cell_to_str(c).strip() for c in raw_headers]
    provisional_cols = [
        name if name else f"col_{get_column_letter(idx)}"
        for idx, name in enumerate(raw_header_names, start=1)
    ]
    detected_case = _detect_column(provisional_cols, "case")
    detected_wd = _detect_column(provisional_cols, "work_dir")
    detected_state = _detect_column(provisional_cols, "manual_state")

    case_idx = provisional_cols.index(detected_case) if detected_case in provisional_cols else None
    data_rows = _extract_data_rows(
        all_rows[detected_header_row:],
        case_idx=case_idx,
        stop_on_blank_case=stop_on_blank_case,
    )

    col_indices = _active_column_indices(raw_header_names, data_rows)
    columns = tuple(
        raw_header_names[i] if raw_header_names[i] else f"col_{get_column_letter(i + 1)}"
        for i in col_indices
    )
    samples: list[dict[str, str]] = []
    for _, row_vals in data_rows[:5]:
        samples.append(
            {
                col_name: _cell_to_str(row_vals[i] if i < len(row_vals) else None)
                for i, col_name in zip(col_indices, columns, strict=True)
            }
        )

    return ExcelPreview(
        path=path,
        sheets=sheets,
        selected_sheet=selected,
        header_row=detected_header_row,
        columns=columns,
        detected_case_column=detected_case,
        detected_work_dir_column=detected_wd,
        detected_manual_state_column=detected_state,
        sample_rows=tuple(samples),
        total_data_rows=len(data_rows),
    )


def parse_workbook(
    path: Path,
    *,
    sheet_name: str | None = None,
    header_row: int | None = None,
    case_column: str | None = None,
    work_dir_column: str | None = None,
    manual_state_column: str | None = None,
    naming_regex: str | None = DEFAULT_NAMING_REGEX,
    stop_on_blank_case: bool = True,
) -> ParsedWorkbook:
    """Parse an `.xlsx` sheet into `CaseSpec` items without modifying the workbook."""
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        sheets = tuple(wb.sheetnames)
        if not sheets:
            raise ValueError(f"workbook {path} has no sheets")
        selected = _select_sheet_name(sheets, sheet_name)
        ws = wb[selected]
        all_rows = [tuple(row) for row in ws.iter_rows(values_only=True)]
    finally:
        wb.close()

    if not all_rows:
        raise ValueError(f"sheet {selected!r} in {path} is empty")

    hdr_row = header_row if header_row is not None else _detect_header_row(all_rows)
    if hdr_row < 1 or hdr_row > len(all_rows):
        raise ValueError(f"header_row {hdr_row} out of range (1..{len(all_rows)})")

    raw_headers = [_cell_to_str(c).strip() for c in all_rows[hdr_row - 1]]
    provisional_cols = [
        name if name else f"col_{get_column_letter(idx)}"
        for idx, name in enumerate(raw_headers, start=1)
    ]

    chosen_case = case_column or _detect_column(provisional_cols, "case")
    if not chosen_case or chosen_case not in provisional_cols:
        raise ValueError(f"case column {chosen_case!r} not found in headers {provisional_cols}")
    case_idx = provisional_cols.index(chosen_case)

    chosen_wd = (
        work_dir_column
        if work_dir_column is not None
        else _detect_column(provisional_cols, "work_dir")
    )
    if chosen_wd is not None and chosen_wd not in provisional_cols:
        raise ValueError(f"work_dir column {chosen_wd!r} not found in headers {provisional_cols}")
    wd_idx = provisional_cols.index(chosen_wd) if chosen_wd is not None else None

    chosen_state = (
        manual_state_column
        if manual_state_column is not None
        else _detect_column(provisional_cols, "manual_state")
    )
    if chosen_state is not None and chosen_state not in provisional_cols:
        raise ValueError(
            f"manual_state column {chosen_state!r} not found in headers {provisional_cols}"
        )
    state_idx = provisional_cols.index(chosen_state) if chosen_state is not None else None

    data_rows = _extract_data_rows(
        all_rows[hdr_row:],
        case_idx=case_idx,
        stop_on_blank_case=stop_on_blank_case,
        start_excel_row=hdr_row + 1,
    )
    col_indices = _active_column_indices(raw_headers, data_rows)
    columns = tuple(
        raw_headers[i] if raw_headers[i] else f"col_{get_column_letter(i + 1)}" for i in col_indices
    )

    specs: list[CaseSpec] = []
    seen_keys: set[str] = set()
    for excel_row_num, row_vals in data_rows:
        case_key = _cell_to_str(row_vals[case_idx] if case_idx < len(row_vals) else None).strip()
        if not case_key:
            continue
        if case_key in seen_keys:
            raise ValueError(f"duplicate case key {case_key!r} at row {excel_row_num}")
        seen_keys.add(case_key)

        raw_wd = (
            _cell_to_str(row_vals[wd_idx] if wd_idx < len(row_vals) else None)
            if wd_idx is not None
            else ""
        )
        work_dir = normalize_work_dir(raw_wd)

        raw_state = (
            _cell_to_str(row_vals[state_idx] if state_idx < len(row_vals) else None).strip()
            if state_idx is not None
            else ""
        )
        manual_state = raw_state or None

        params = {
            col_name: _cell_to_str(row_vals[i] if i < len(row_vals) else None)
            for i, col_name in zip(col_indices, columns, strict=True)
        }
        parent_raw = extract_parent_key(case_key, work_dir, naming_regex)
        specs.append(
            CaseSpec(
                case_key=case_key,
                row_index=excel_row_num,
                parent_key_raw=parent_raw,
                work_dir=work_dir,
                manual_state=manual_state,
                params=params,
            )
        )

    return ParsedWorkbook(
        sheet_name=selected,
        header_row=hdr_row,
        case_column=chosen_case,
        work_dir_column=chosen_wd,
        manual_state_column=chosen_state,
        columns=columns,
        cases=tuple(specs),
        inferred_work_root=infer_work_root([c.work_dir for c in specs]),
    )


def _extract_data_rows(
    rows: Sequence[tuple[Any, ...]],
    *,
    case_idx: int | None,
    stop_on_blank_case: bool,
    start_excel_row: int = 2,
) -> list[tuple[int, tuple[Any, ...]]]:
    extracted: list[tuple[int, tuple[Any, ...]]] = []
    for offset, row in enumerate(rows):
        excel_row = start_excel_row + offset
        if case_idx is not None:
            case_val = _cell_to_str(row[case_idx] if case_idx < len(row) else None).strip()
            if not case_val:
                if stop_on_blank_case:
                    break
                continue
        else:
            if all(_cell_to_str(c).strip() == "" for c in row):
                if stop_on_blank_case:
                    break
                continue
        extracted.append((excel_row, row))
    return extracted


def _active_column_indices(
    raw_headers: Sequence[str],
    data_rows: Sequence[tuple[int, tuple[Any, ...]]],
) -> list[int]:
    """Keep all columns with a header, plus headerless columns that carry data in imported rows."""
    max_width = max(
        [len(raw_headers), *(len(row) for _, row in data_rows)],
        default=0,
    )
    active: list[int] = []
    for col_idx in range(max_width):
        has_header = col_idx < len(raw_headers) and raw_headers[col_idx] != ""
        has_data = any(
            col_idx < len(row) and _cell_to_str(row[col_idx]).strip() != "" for _, row in data_rows
        )
        if has_header or has_data:
            active.append(col_idx)
    return active


def write_enriched_workbook(
    campaign: CampaignRecord,
    summaries: Sequence[CaseSummary],
    monitor_values: Mapping[int, Mapping[str, float | None]],
    monitor_labels: Sequence[str],
    output_path: Path,
) -> tuple[bool, tuple[str, ...]]:
    """Export campaign data + SimManager columns to `output_path` (never overwriting original).

    Returns `(used_template, added_column_names)`.
    """
    sm_headers = (
        "SM_ComputedState",
        "SM_JobID",
        "SM_Cluster",
        "SM_Progress",
        *(f"SM_Monitor_{label}" for label in monitor_labels),
    )
    source = Path(campaign.source_path) if campaign.source_path else None
    if source is not None and source.resolve() == output_path.resolve():
        raise ValueError(f"refusing to overwrite original campaign workbook: {source}")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    if source is not None and source.is_file():
        _export_from_template(
            source, campaign, summaries, monitor_values, monitor_labels, sm_headers, output_path
        )
        return True, sm_headers

    _export_fresh(campaign, summaries, monitor_values, monitor_labels, sm_headers, output_path)
    return False, sm_headers


def _summary_row_values(
    summary: CaseSummary,
    monitor_values: Mapping[int, Mapping[str, float | None]],
    monitor_labels: Sequence[str],
) -> list[Any]:
    latest = summary.latest
    job_id = latest.job.info.job_id if latest and latest.job else ""
    cluster = (
        latest.run.cluster_id
        if latest and latest.run.cluster_id
        else (latest.job.cluster_id if latest and latest.job else "")
    )
    progress: float | str = (
        round(latest.solver_state.progress * 100.0, 2)
        if latest and latest.solver_state and latest.solver_state.progress is not None
        else ""
    )
    case_monitors = monitor_values.get(summary.case.id, {})
    mon_cells = [
        val if (val := case_monitors.get(label)) is not None else "" for label in monitor_labels
    ]
    return [str(summary.case.computed_state), job_id, cluster, progress, *mon_cells]


def _export_from_template(
    source: Path,
    campaign: CampaignRecord,
    summaries: Sequence[CaseSummary],
    monitor_values: Mapping[int, Mapping[str, float | None]],
    monitor_labels: Sequence[str],
    sm_headers: tuple[str, ...],
    output_path: Path,
) -> None:
    wb = openpyxl.load_workbook(source)
    try:
        sheet_name = (
            campaign.sheet_name
            if campaign.sheet_name and campaign.sheet_name in wb.sheetnames
            else wb.sheetnames[0]
        )
        ws = wb[sheet_name]
        header_row = campaign.header_row

        # Place SM_* after ws.max_column so headerless columns (e.g. col_N) are preserved.
        start_col = ws.max_column + 1

        case_col_idx: int | None = None
        for c in range(1, start_col):
            if (
                _cell_to_str(ws.cell(row=header_row, column=c).value).strip()
                == campaign.case_column
            ):
                case_col_idx = c
                break
        template_header_cell = (
            ws.cell(row=header_row, column=case_col_idx) if case_col_idx is not None else None
        )

        for offset, name in enumerate(sm_headers):
            cell = ws.cell(row=header_row, column=start_col + offset, value=name)
            if template_header_cell is not None and template_header_cell.has_style:
                cell.font = copy(template_header_cell.font)
                cell.fill = copy(template_header_cell.fill)
                cell.alignment = copy(template_header_cell.alignment)

        by_row_index: dict[int, CaseSummary] = {}
        by_key: dict[str, CaseSummary] = {}
        for s in summaries:
            by_key[s.case.case_key] = s
            if s.case.row_index is not None:
                by_row_index[s.case.row_index] = s

        written_cases: set[int] = set()
        for row_num in range(header_row + 1, ws.max_row + 1):
            row_key = (
                _cell_to_str(ws.cell(row=row_num, column=case_col_idx).value).strip()
                if case_col_idx is not None
                else ""
            )
            matched: CaseSummary | None = None
            candidate = by_row_index.get(row_num)
            if candidate is not None and (not row_key or candidate.case.case_key == row_key):
                matched = candidate
            elif row_key and row_key in by_key:
                matched = by_key[row_key]
            if matched is None:
                continue
            written_cases.add(matched.case.id)
            vals = _summary_row_values(matched, monitor_values, monitor_labels)
            for offset, val in enumerate(vals):
                ws.cell(row=row_num, column=start_col + offset, value=val)

        # Append any active cases not matched to an existing template row (e.g. inactive/moved).
        next_row = ws.max_row + 1
        for s in summaries:
            if s.case.id in written_cases:
                continue
            vals = _summary_row_values(s, monitor_values, monitor_labels)
            if case_col_idx is not None:
                ws.cell(row=next_row, column=case_col_idx, value=s.case.case_key)
            for offset, val in enumerate(vals):
                ws.cell(row=next_row, column=start_col + offset, value=val)
            next_row += 1

        wb.save(output_path)
    finally:
        wb.close()


def _export_fresh(
    campaign: CampaignRecord,
    summaries: Sequence[CaseSummary],
    monitor_values: Mapping[int, Mapping[str, float | None]],
    monitor_labels: Sequence[str],
    sm_headers: tuple[str, ...],
    output_path: Path,
) -> None:
    wb = openpyxl.Workbook()
    try:
        ws = wb.active
        assert ws is not None
        ws.title = campaign.sheet_name or "Cases"
        orig_cols = campaign.columns
        ws.append([*orig_cols, *sm_headers])
        for s in summaries:
            orig_vals = [s.case.params.get(col, "") for col in orig_cols]
            sm_vals = _summary_row_values(s, monitor_values, monitor_labels)
            ws.append([*orig_vals, *sm_vals])
        wb.save(output_path)
    finally:
        wb.close()
