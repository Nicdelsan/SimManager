"""`timeseries` contract: file reading cursors, chunked time series, and solver run state."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol

SERVICE_KEY = "timeseries"
CONTRACT_VERSION = 1


class FileStatus(StrEnum):
    ACTIVE = "active"
    BACKFILLING = "backfilling"
    COMPLETE = "complete"
    TRUNCATED = "truncated"


class SolverState(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    STOPPED = "stopped"
    FAILED = "failed"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class FileCursor:
    """Durable incremental-reading position and parser state for one remote file."""

    run_id: int
    file_path: str
    parser_kind: str
    file_size: int = 0
    mtime: float = 0.0
    head_prefix: bytes = b""
    """First up to 256 bytes of the file, used to detect truncation or replacement."""
    forward_offset: int = 0
    """Live tail reading cursor: bytes up to this offset (except backfill gap) are ingested."""
    backfill_start: int = 0
    """Start byte offset of unread middle gap (equals backfill_end when backfill is complete)."""
    backfill_end: int = 0
    header_data: dict[str, Any] = field(default_factory=dict)
    parser_state: dict[str, Any] = field(default_factory=dict)
    status: FileStatus = FileStatus.ACTIVE
    updated_at: datetime | None = None

    @property
    def needs_backfill(self) -> bool:
        return self.backfill_start < self.backfill_end


@dataclass(frozen=True)
class SeriesKey:
    """Identity and metadata of a time series extracted from a solver file."""

    file_path: str
    name: str
    qualifier: str = ""
    unit: str = ""
    x_name: str = "Time"
    x_unit: str = ""


@dataclass(frozen=True)
class SeriesMeta:
    """Stored summary metadata for a time series."""

    id: int
    run_id: int
    file_path: str
    name: str
    qualifier: str
    unit: str
    x_name: str
    x_unit: str
    point_count: int
    last_x: float | None
    last_y: float | None
    min_y: float | None
    max_y: float | None


@dataclass(frozen=True)
class SeriesPoints:
    """Result of a range/downsampled time-series query."""

    series_id: int
    x: list[float]
    y: list[float]
    level: int
    total_points: int


@dataclass(frozen=True)
class RunSolverState:
    """Latest solver-derived status and progress for a run."""

    run_id: int
    solver: str
    work_dir: str
    case_dir: str | None = None
    output_dir: str | None = None
    solver_state: SolverState = SolverState.UNKNOWN
    progress: float | None = None
    current_time: float | None = None
    start_time: float | None = None
    end_time: float | None = None
    ncyc: int | None = None
    warning_count: int = 0
    error_message: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    updated_at: datetime | None = None


class TimeSeriesStore(Protocol):
    async def get_cursor(self, run_id: int, file_path: str) -> FileCursor | None: ...

    async def list_cursors(self, run_id: int) -> list[FileCursor]: ...

    async def upsert_cursor(self, cursor: FileCursor) -> None: ...

    async def reset_file_data(self, run_id: int, file_path: str) -> None:
        """Delete stored series and chunks for one file (used only when truncation is detected)."""
        ...

    async def append_samples(
        self,
        run_id: int,
        batch: Mapping[SeriesKey, tuple[Sequence[float], Sequence[float]]],
    ) -> list[SeriesMeta]:
        """Store raw float64 chunks and min/max downsampled levels for a batch of series."""
        ...

    async def list_series(self, run_id: int) -> list[SeriesMeta]: ...

    async def query_series(
        self,
        series_id: int,
        *,
        x_min: float | None = None,
        x_max: float | None = None,
        max_points: int = 2000,
    ) -> SeriesPoints: ...

    async def get_run_solver_state(self, run_id: int) -> RunSolverState | None: ...

    async def upsert_run_solver_state(self, state: RunSolverState) -> None: ...
