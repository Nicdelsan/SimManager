"""`solver.<kind>` contract (e.g. `solver.converge`, `solver.openfoam`): CFD log/output parsers.

A solver plugin is stateless with respect to connections: it inspects remote case directories
via `RemoteSession` and parses text chunks via pure functions.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from .timeseries import SeriesKey, SolverState
from .transport import RemoteSession

SERVICE_PREFIX = "solver."
CONTRACT_VERSION = 1


@dataclass(frozen=True)
class WatchedFile:
    """A remote solver file to follow incrementally."""

    file_path: str
    """Normalized path relative to `work_dir` (e.g. `outputs_restart1/stream0/thermo.out`)."""
    remote_path: str
    """Absolute remote POSIX path passed to `RemoteSession`."""
    parser_kind: str
    """Solver-specific parser identifier (e.g. `converge.out`, `openfoam.log`)."""


@dataclass(frozen=True)
class SolverStatusUpdate:
    """Incremental status/progress update extracted from solver inputs or logs."""

    solver_state: SolverState | None = None
    progress: float | None = None
    current_time: float | None = None
    start_time: float | None = None
    end_time: float | None = None
    ncyc: int | None = None
    warning_delta: int = 0
    error_message: str | None = None
    case_dir: str | None = None
    output_dir: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ParsedChunk:
    """Result of parsing one text chunk from a solver file."""

    series: dict[SeriesKey, tuple[list[float], list[float]]] = field(default_factory=dict)
    parser_state: dict[str, Any] = field(default_factory=dict)
    header_data: dict[str, Any] | None = None
    status_update: SolverStatusUpdate | None = None
    events: list[tuple[str, dict[str, Any]]] = field(default_factory=list)


class Solver(Protocol):
    kind: str

    async def discover(
        self,
        session: RemoteSession,
        work_dir: str,
        *,
        stdout_path: str | None = None,
        known_output_dir: str | None = None,
    ) -> tuple[SolverStatusUpdate | None, list[WatchedFile]]:
        """Inspect case inputs/directories and return initial status + files to watch."""
        ...

    def parse_header(
        self, file_path: str, parser_kind: str, raw: bytes
    ) -> tuple[dict[str, Any], int]:
        """Parse file header/preamble from initial bytes.

        Returns `(header_data, header_end_offset)` where `header_end_offset` is the byte offset
        after the header/preamble (aligned to a line boundary).
        """
        ...

    def find_tail_alignment(self, parser_kind: str, chunk: bytes) -> int | None:
        """Find byte offset within `chunk` where a complete record/step begins, or None."""
        ...

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
        """Pure incremental parser over complete newline-delimited lines."""
        ...
