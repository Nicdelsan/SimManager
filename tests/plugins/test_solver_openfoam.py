from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from simmanager.contracts.timeseries import SolverState
from simmanager.plugins.solver_openfoam import OpenFoamSolver
from simmanager.plugins.solver_openfoam.parsers import (
    find_log_tail_alignment,
    parse_control_dict,
    parse_decompose_par_dict,
    parse_log_chunk,
    parse_log_header,
)
from simmanager.plugins.storage_sqlite import SqliteStorage, SqliteTimeSeriesStore
from simmanager.readers import IncrementalReader, LocalRemoteSession, ReaderConfig

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "openfoam"
SAMPLES_OPENFOAM = Path(__file__).resolve().parents[2] / "samples" / "openfoam"


def test_parse_openfoam_dicts_with_comments() -> None:
    ctrl = parse_control_dict((FIXTURES / "system" / "controlDict").read_text(encoding="utf-8"))
    assert ctrl.application == "twoPhaseSolver"
    assert ctrl.start_from == "latestTime"
    assert ctrl.start_time == 0.0
    assert ctrl.end_time == 5e-4
    assert ctrl.delta_t == 5e-11
    assert ctrl.write_interval == 5e-5

    nprocs = parse_decompose_par_dict(
        (FIXTURES / "system" / "decomposeParDict").read_text(encoding="utf-8")
    )
    assert nprocs == 16


def test_parse_openfoam_log_foamlog_convention_and_anchored_time() -> None:
    raw = (FIXTURES / "log.out").read_bytes()
    header, header_end = parse_log_header(raw)
    assert header["Case"] == "/work/user1/case_a"
    assert header["nProcs"] == 16
    assert header["I/O"] == "uncollated"

    body = raw[header_end:].decode("utf-8")
    parsed = parse_log_chunk("log.out", body, header_data=header, parser_state={})

    by_name = {k.name: xy for k, xy in parsed.series.items()}
    # ExecutionTime must not be matched as Time =
    assert by_name["deltaT"] == ([6e-11, 1.32e-10], [6.0e-11, 7.2e-11])
    assert by_name["courant.max"] == ([6e-11, 1.32e-10], [0.0, 0.02])
    assert by_name["courant.mean"] == ([1.32e-10], [0.01])
    # First Initial residual per field per step (foamLog convention)
    assert by_name["residual.p"] == ([6e-11, 1.32e-10], [3.5e-06, 4.1e-06])
    assert by_name["residual.k"] == ([6e-11, 1.32e-10], [1.0, 0.002])
    assert by_name["execution_time"] == ([6e-11, 1.32e-10], [0.14, 0.21])
    # Last min/max per step for repeated T
    assert by_name["T.min"] == ([6e-11], [294.0])
    assert by_name["T.max"] == ([6e-11], [374.0])
    assert by_name["FaceAcCourant.max"] == ([6e-11], [2.35])
    assert by_name["rho.min"] == ([1.32e-10], [0.14])

    assert parsed.status_update is not None
    assert parsed.status_update.solver_state is SolverState.COMPLETED
    assert parsed.status_update.progress == 1.0


def test_parse_openfoam_fatal_error() -> None:
    text = (
        'Time = 0.001\nFOAM FATAL IO ERROR:\ncannot find file "constant/thermophysicalProperties"\n'
    )
    parsed = parse_log_chunk("log.out", text, header_data={}, parser_state={})
    assert parsed.status_update is not None
    assert parsed.status_update.solver_state is SolverState.FAILED
    assert "thermophysicalProperties" in (parsed.status_update.error_message or "")
    assert any(kind == "solver.error" for kind, _ in parsed.events)


def test_find_tail_alignment_starts_before_time_at_step_block_start() -> None:
    raw = (FIXTURES / "log.out").read_bytes()
    # Seek into the middle of step 1 (after Time = 6e-11)
    idx_inside_step1 = raw.index(b"DILUPBiCGStab:  Solving for p")
    rel = find_log_tail_alignment(raw[idx_inside_step1:])
    assert rel is not None
    aligned = raw[idx_inside_step1 + rel :]
    # Must align to the start of step 2 (Mean and max Characteristic Courant Numbers), NOT Time =
    assert aligned.startswith(b"Mean and max Characteristic Courant Numbers")


async def test_openfoam_discover_and_tail_backfill_seam(tmp_path: Path) -> None:
    case = tmp_path / "of_case"
    (case / "system").mkdir(parents=True)
    shutil.copy(FIXTURES / "system" / "controlDict", case / "system" / "controlDict")
    shutil.copy(FIXTURES / "system" / "decomposeParDict", case / "system" / "decomposeParDict")

    # Build a synthetic multi-step log large enough to trigger tail-first + backfill
    raw_head = (FIXTURES / "log.out").read_text(encoding="utf-8")
    preamble = raw_head.split("Starting time loop")[0] + "Starting time loop\n\n"
    blocks = []
    for i in range(1, 61):
        t = i * 1e-6
        blocks.append(
            f"Courant Number mean: {0.01 * i:.4f} max: {0.1 * i:.4f}\n"
            f"deltaT = 1e-06\n"
            f"Time = {t:.6e}\n"
            f"DILUPBiCGStab:  Solving for p, Initial residual = {1.0 / i:.6e}, "
            f"Final residual = 1e-12, No Iterations 1\n"
            f"DILUPBiCGStab:  Solving for p, Initial residual = 1e-12, "
            f"Final residual = 1e-12, No Iterations 0\n"
            f"ExecutionTime = {0.1 * i:.2f} s  ClockTime = {i} s\n\n"
        )
    (case / "log.twoPhaseSolver").write_text(preamble + "".join(blocks), encoding="utf-8")

    session = LocalRemoteSession(case)
    solver = OpenFoamSolver()
    status, watched = await solver.discover(session, case.as_posix())
    assert status is not None
    assert status.start_time == 0.0
    assert status.end_time == 5e-4
    assert status.metadata["numberOfSubdomains"] == 16
    assert [w.file_path for w in watched] == ["log.twoPhaseSolver"]

    storage = await SqliteStorage.open(str(tmp_path / "of.db"))
    ts = SqliteTimeSeriesStore(storage.connection, storage.write_lock)
    reader = IncrementalReader(
        ReaderConfig(
            chunk_bytes=600,
            full_read_max_bytes=1500,
            tail_preview_bytes=1200,
            header_probe_bytes=600,
        )
    )

    res = await reader.sync_file(session, ts, solver, 1, watched[0])
    assert res.needs_backfill

    prog = await reader.backfill_step(session, ts, solver, 1, watched[0], max_chunks=None)
    assert prog.done

    series = {s.name: s for s in await ts.list_series(1)}
    p_pts = await ts.query_series(series["residual.p"].id, max_points=5000)
    co_pts = await ts.query_series(series["courant.max"].id, max_points=5000)
    assert len(p_pts.x) == 60
    assert len(co_pts.x) == 60
    assert p_pts.x == co_pts.x  # no split or missing Courant/Time at the backfill seam!
    await storage.close()


@pytest.mark.slow
@pytest.mark.skipif(not SAMPLES_OPENFOAM.is_dir(), reason="local samples/openfoam not present")
async def test_openfoam_real_samples_tail_first_and_backfill_step(tmp_path: Path) -> None:
    session = LocalRemoteSession(SAMPLES_OPENFOAM)
    solver = OpenFoamSolver()
    status, watched = await solver.discover(session, session.root_posix)
    assert status is not None
    assert status.end_time == 5.0e-4
    assert status.metadata["numberOfSubdomains"] == 10
    log_w = next(w for w in watched if w.file_path == "log.out")

    storage = await SqliteStorage.open(str(tmp_path / "samples_of.db"))
    ts = SqliteTimeSeriesStore(storage.connection, storage.write_lock)
    reader = IncrementalReader(ReaderConfig(full_read_max_bytes=50 * 1024 * 1024))

    res = await reader.sync_file(session, ts, solver, 1, log_w)
    assert res.needs_backfill
    assert res.points_ingested > 0

    prog = await reader.backfill_step(session, ts, solver, 1, log_w, max_chunks=2)
    assert prog.bytes_read > 0
    assert prog.points_ingested > 0

    series = {s.name: s for s in await ts.list_series(1)}
    assert "residual.p" in series
    assert "courant.max" in series
    assert "deltaT" in series
    await storage.close()
