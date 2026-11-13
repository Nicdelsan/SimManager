from __future__ import annotations

import math
import shutil
from pathlib import Path

import pytest

from simmanager.contracts.timeseries import SolverState
from simmanager.plugins.solver_converge import ConvergeSolver
from simmanager.plugins.solver_converge.parsers import (
    parse_inputs_in,
    parse_out_chunk,
    parse_out_header,
    parse_stdout_chunk,
)
from simmanager.plugins.storage_sqlite import SqliteStorage, SqliteTimeSeriesStore
from simmanager.readers import IncrementalReader, LocalRemoteSession, ReaderConfig

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "converge"
SAMPLES_CONVERGE = Path(__file__).resolve().parents[2] / "samples" / "converge"


def test_parse_inputs_in_multidoc() -> None:
    inp = parse_inputs_in((FIXTURES / "inputs.in").read_text(encoding="utf-8"))
    assert inp.crank_flag == 0
    assert inp.start_time == 0.02
    assert inp.end_time == 0.06
    assert inp.restart_flag == 1
    assert inp.restart_number == 1


def test_parse_out_header_with_and_without_trailing_hash() -> None:
    thermo_raw = (FIXTURES / "thermo.out").read_bytes()
    h1, off1 = parse_out_header(thermo_raw)
    assert [c["name"] for c in h1["columns"]] == ["Time", "Pressure", "Max_Pres", "Mean_Temp"]
    assert [c["unit"] for c in h1["columns"]] == ["seconds", "MPa", "MPa", "K"]
    assert thermo_raw[off1:].lstrip().startswith(b"2.0173315e-02")

    flow_raw = (FIXTURES / "area_avg_regions_flow.out").read_bytes()
    h2, off2 = parse_out_header(flow_raw)
    assert [(c["name"], c["qualifier"]) for c in h2["columns"]] == [
        ("Time", ""),
        ("Cycle_Number", ""),
        ("Flow_Rate", "Regions_1_to_3"),
        ("Flow_Rate", "bound_id_11"),
    ]
    assert flow_raw[off2:].lstrip().startswith(b"2.0173315e-02")

    parsed = parse_out_chunk(
        "area_avg_regions_flow.out",
        flow_raw[off2:].decode("utf-8"),
        header_data=h2,
        parser_state={},
    )
    assert len(parsed.series) == 3  # Cycle_Number + 2 Flow_Rate qualifiers


def test_parse_out_nan_detection() -> None:
    raw = (FIXTURES / "thermo.out").read_bytes()
    hdr, _off = parse_out_header(raw)
    text = "2.02e-02 nan 2.4 295.0\n"
    parsed = parse_out_chunk("thermo.out", text, header_data=hdr, parser_state={})
    assert any(kind == "solver.nan_detected" for kind, _ in parsed.events)
    pressure_key = next(k for k in parsed.series if k.name == "Pressure")
    assert math.isnan(parsed.series[pressure_key][1][0])


def test_parse_stdout_ansi_progress_stop_and_errors() -> None:
    text = (FIXTURES / "logout.txt").read_text(encoding="utf-8")
    parsed = parse_stdout_chunk("logout.txt", text, header_data={}, parser_state={})
    assert parsed.status_update is not None
    u = parsed.status_update
    assert u.case_dir == "/work/user1/case_a"
    assert u.output_dir == "/work/user1/case_a/outputs_restart1"
    assert u.solver_state is SolverState.COMPLETED
    assert u.progress == 1.0
    assert u.ncyc == 109946
    assert u.warning_delta == 1
    assert u.metadata["run_time_s"] == 120.5

    stop_parsed = parse_stdout_chunk(
        "logout.txt", "Found file STOP. Finishing job.\n", header_data={}, parser_state={}
    )
    assert stop_parsed.status_update is not None
    assert stop_parsed.status_update.solver_state is SolverState.STOPPED

    err_parsed = parse_stdout_chunk(
        "logout.txt", "Fatal Error: mesh inverted\n", header_data={}, parser_state={}
    )
    assert err_parsed.status_update is not None
    assert err_parsed.status_update.solver_state is SolverState.FAILED
    assert err_parsed.status_update.error_message == "mesh inverted"


async def test_converge_discover_and_ingest_via_local_session(tmp_path: Path) -> None:
    case = tmp_path / "case_a"
    out_dir = case / "outputs_restart1" / "stream0"
    out_dir.mkdir(parents=True)
    shutil.copy(FIXTURES / "inputs.in", case / "inputs.in")
    # Point creating pathname to relative/virtual path under session root
    logout_text = (
        (FIXTURES / "logout.txt")
        .read_text(encoding="utf-8")
        .replace("/work/user1/case_a", case.as_posix())
    )
    (case / "logout.txt").write_text(logout_text, encoding="utf-8")
    shutil.copy(FIXTURES / "thermo.out", out_dir / "thermo.out")
    shutil.copy(FIXTURES / "area_avg_regions_flow.out", out_dir / "area_avg_regions_flow.out")

    session = LocalRemoteSession(case)
    solver = ConvergeSolver()
    status, watched = await solver.discover(session, case.as_posix())
    assert status is not None
    assert status.start_time == 0.02
    assert status.end_time == 0.06
    assert {w.file_path for w in watched} == {
        "logout.txt",
        "outputs_restart1/stream0/thermo.out",
        "outputs_restart1/stream0/area_avg_regions_flow.out",
    }

    storage = await SqliteStorage.open(str(tmp_path / "test.db"))
    ts = SqliteTimeSeriesStore(storage.connection, storage.write_lock)
    reader = IncrementalReader()
    for w in watched:
        await reader.sync_file(session, ts, solver, 1, w)

    series = await ts.list_series(1)
    flow_series = [s for s in series if s.name == "Flow_Rate"]
    assert {s.qualifier for s in flow_series} == {"Regions_1_to_3", "bound_id_11"}
    await storage.close()


@pytest.mark.slow
@pytest.mark.skipif(not SAMPLES_CONVERGE.is_dir(), reason="local samples/converge not present")
async def test_converge_real_samples_tail_first_and_backfill(tmp_path: Path) -> None:
    session = LocalRemoteSession(SAMPLES_CONVERGE)
    solver = ConvergeSolver()
    status, watched = await solver.discover(session, session.root_posix)
    assert status is not None
    assert status.start_time == 0.02
    assert status.end_time == 0.06

    storage = await SqliteStorage.open(str(tmp_path / "samples_converge.db"))
    ts = SqliteTimeSeriesStore(storage.connection, storage.write_lock)
    reader = IncrementalReader(ReaderConfig(full_read_max_bytes=50 * 1024 * 1024))

    flow_w = next(w for w in watched if w.file_path == "area_avg_regions_flow.out")
    res = await reader.sync_file(session, ts, solver, 1, flow_w)
    assert res.needs_backfill
    assert res.points_ingested > 0

    prog = await reader.backfill_step(session, ts, solver, 1, flow_w, max_chunks=None)
    assert prog.done

    logout_w = next(w for w in watched if w.file_path == "logout.txt")
    res_log = await reader.sync_file(session, ts, solver, 1, logout_w)
    assert any(u.solver_state is SolverState.COMPLETED for u in res_log.status_updates)
    await storage.close()
