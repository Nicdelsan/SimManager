from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from simmanager.contracts.solver import ParsedChunk, SolverStatusUpdate, WatchedFile
from simmanager.contracts.timeseries import SeriesKey, TimeSeriesStore
from simmanager.contracts.transport import RemoteSession
from simmanager.plugins.storage_sqlite import SqliteStorage, SqliteTimeSeriesStore
from simmanager.readers import IncrementalReader, LocalRemoteSession, ReaderConfig


class _DummySolver:
    kind = "dummy"

    async def discover(
        self,
        session: RemoteSession,
        work_dir: str,
        *,
        stdout_path: str | None = None,
        known_output_dir: str | None = None,
    ) -> tuple[SolverStatusUpdate | None, list[WatchedFile]]:
        return None, []

    def parse_header(
        self, file_path: str, parser_kind: str, raw: bytes
    ) -> tuple[dict[str, Any], int]:
        lines = raw.splitlines(keepends=True)
        offset = 0
        cols: list[str] = []
        for line in lines:
            text = line.decode("utf-8", errors="replace").strip()
            if text.startswith("#"):
                offset += len(line)
                if text.startswith("# cols:"):
                    cols = text.removeprefix("# cols:").split()
            else:
                break
        return {"cols": cols}, offset

    def find_tail_alignment(self, parser_kind: str, chunk: bytes) -> int | None:
        # Align to the start of a "STEP " block if present, else next newline.
        if parser_kind == "step_block":
            nl = chunk.find(b"\n")
            if nl < 0:
                return None
            pos = nl + 1
            while pos < len(chunk):
                end = chunk.find(b"\n", pos)
                line = chunk[pos:] if end < 0 else chunk[pos : end + 1]
                if line.startswith(b"STEP "):
                    return pos
                if end < 0:
                    return None
                pos = end + 1
            return None
        idx = chunk.find(b"\n")
        return None if idx < 0 else idx + 1

    def parse_chunk(
        self,
        file_path: str,
        parser_kind: str,
        text: str,
        *,
        header_data: Mapping[str, Any],
        parser_state: Mapping[str, Any],
        is_backfill: bool = False,
    ) -> ParsedChunk:
        xs: list[float] = []
        ys: list[float] = []
        current_x: float | None = parser_state.get("current_x")
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("STEP "):
                current_x = float(line.split()[1])
                continue
            if line.startswith("VAL "):
                assert current_x is not None
                xs.append(current_x)
                ys.append(float(line.split()[1]))
                continue
            parts = line.split()
            xs.append(float(parts[0]))
            ys.append(float(parts[1]))

        key = SeriesKey(file_path, "v")
        series = {key: (xs, ys)} if xs else {}
        return ParsedChunk(series=series, parser_state={"current_x": current_x})


async def test_incremental_partial_line_and_short_growth(tmp_path: Path) -> None:
    case = tmp_path / "case"
    case.mkdir()
    data_file = case / "series.txt"
    data_file.write_bytes(b"# cols: x y\n1.0 10.0\n2.0 ")  # partial last line!

    storage = await SqliteStorage.open(str(tmp_path / "test.db"))
    ts: TimeSeriesStore = SqliteTimeSeriesStore(storage.connection, storage.write_lock)
    session = LocalRemoteSession(case)
    reader = IncrementalReader()
    watched = WatchedFile("series.txt", f"{session.root_posix}/series.txt", "plain")

    res1 = await reader.sync_file(session, ts, _DummySolver(), 1, watched)
    assert res1.points_ingested == 1
    assert not res1.truncated

    # Finish the partial line and grow past 256 bytes without triggering false truncation
    with data_file.open("ab") as fh:
        fh.write(b"20.0\n")
        for i in range(3, 40):
            fh.write(f"{float(i)} {float(i * 10)}\n".encode())

    res2 = await reader.sync_file(session, ts, _DummySolver(), 1, watched)
    assert not res2.truncated
    assert res2.points_ingested == 38

    s = (await ts.list_series(1))[0]
    pts = await ts.query_series(s.id)
    assert pts.x == [float(i) for i in range(1, 40)]
    assert pts.y == [float(i * 10) for i in range(1, 40)]
    await storage.close()


async def test_truncation_and_header_replacement_reset(tmp_path: Path) -> None:
    case = tmp_path / "case"
    case.mkdir()
    data_file = case / "series.txt"
    data_file.write_bytes(b"# cols: x y\n1.0 100.0\n2.0 200.0\n3.0 300.0\n")

    storage = await SqliteStorage.open(str(tmp_path / "test.db"))
    ts = SqliteTimeSeriesStore(storage.connection, storage.write_lock)
    session = LocalRemoteSession(case)
    reader = IncrementalReader()
    watched = WatchedFile("series.txt", f"{session.root_posix}/series.txt", "plain")

    await reader.sync_file(session, ts, _DummySolver(), 1, watched)

    # Case 1: shrink file -> truncated
    data_file.write_bytes(b"# cols: x y\n1.0 1.0\n")
    res_shrink = await reader.sync_file(session, ts, _DummySolver(), 1, watched)
    assert res_shrink.truncated
    assert res_shrink.points_ingested == 1
    s = (await ts.list_series(1))[0]
    assert (await ts.query_series(s.id)).y == [1.0]

    # Case 2: replace with a larger file whose first 256 bytes differ
    replacement = b"# cols: x z_replaced\n" + b"".join(
        f"{float(i)} {float(i * 2)}\n".encode() for i in range(1, 30)
    )
    data_file.write_bytes(replacement)
    res_rep = await reader.sync_file(session, ts, _DummySolver(), 1, watched)
    assert res_rep.truncated
    assert res_rep.points_ingested == 29
    s2 = (await ts.list_series(1))[0]
    assert (await ts.query_series(s2.id)).y == [float(i * 2) for i in range(1, 30)]
    await storage.close()


async def test_tail_first_and_background_backfill_with_multiline_blocks(tmp_path: Path) -> None:
    case = tmp_path / "case"
    case.mkdir()
    log_file = case / "log.txt"

    lines = [b"# header line 1\n", b"# cols: x v\n"]
    for i in range(1, 201):
        lines.append(f"STEP {i}\nVAL {i * 10}\n".encode())
    log_file.write_bytes(b"".join(lines))

    storage = await SqliteStorage.open(str(tmp_path / "test.db"))
    ts = SqliteTimeSeriesStore(storage.connection, storage.write_lock)
    session = LocalRemoteSession(case)
    cfg = ReaderConfig(
        chunk_bytes=256,
        full_read_max_bytes=500,
        tail_preview_bytes=600,
        header_probe_bytes=128,
    )
    reader = IncrementalReader(cfg)
    watched = WatchedFile("log.txt", f"{session.root_posix}/log.txt", "step_block")

    # Initial sync reads header + tail first, leaving middle gap for backfill
    res = await reader.sync_file(session, ts, _DummySolver(), 1, watched)
    assert res.needs_backfill
    assert res.points_ingested > 0
    s = (await ts.list_series(1))[0]
    pts_tail = await ts.query_series(s.id)
    assert pts_tail.x[-1] == 200.0
    assert pts_tail.x[0] > 1.0  # tail preview skipped older steps

    # Interleave a live append while backfill is pending!
    with log_file.open("ab") as fh:
        fh.write(b"STEP 201\nVAL 2010\n")

    res_live = await reader.sync_file(session, ts, _DummySolver(), 1, watched)
    assert res_live.points_ingested == 1
    assert res_live.needs_backfill

    # Finish backfill
    prog = await reader.backfill_step(session, ts, _DummySolver(), 1, watched, max_chunks=None)
    assert prog.done
    assert prog.remaining_bytes == 0

    s_all = (await ts.list_series(1))[0]
    pts_all = await ts.query_series(s_all.id, max_points=5000)
    assert pts_all.x == [float(i) for i in range(1, 202)]
    assert pts_all.y == [float(i * 10) for i in range(1, 202)]
    await storage.close()
