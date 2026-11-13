"""Pure derivations for the views: ETA from the progress series, backfill completion."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from simmanager.contracts.timeseries import FileCursor

ETA_WINDOW_S = 3600.0
"""Rate is measured over the last hour of wall time (whole series if shorter)."""
MIN_SPAN_S = 60.0
"""Below this wall-time span the rate is too noisy to show an ETA."""


@dataclass(frozen=True)
class Eta:
    rate_per_s: float
    """Progress fraction per second of wall time."""
    remaining_s: float
    at: float
    """Expected end, seconds since epoch (UTC)."""


def eta(wall_time: Sequence[float], progress: Sequence[float]) -> Eta | None:
    """Linear extrapolation of progress (0..1) against wall time.

    Uses the oldest point still inside `ETA_WINDOW_S` before the last one, so a run that
    slows down (or speeds up) is reflected within an hour. None when progress does not
    advance, is already complete, or the observed span is too short.
    """
    if len(wall_time) != len(progress) or len(wall_time) < 2:
        return None
    x_last, y_last = wall_time[-1], progress[-1]
    if y_last >= 1.0:
        return None
    start = 0
    for i, x in enumerate(wall_time):
        if x >= x_last - ETA_WINDOW_S:
            start = i
            break
    if start == len(wall_time) - 1:
        start = max(0, start - 1)
    span = x_last - wall_time[start]
    advance = y_last - progress[start]
    if span < MIN_SPAN_S or advance <= 0:
        return None
    rate = advance / span
    remaining = (1.0 - y_last) / rate
    return Eta(rate_per_s=rate, remaining_s=remaining, at=x_last + remaining)


def read_fraction(cursor: FileCursor) -> float | None:
    """Fraction of the file already ingested (1.0 once backfill is complete)."""
    if cursor.file_size <= 0:
        return None
    gap = max(0, cursor.backfill_end - cursor.backfill_start)
    return max(0.0, min(1.0, 1.0 - gap / cursor.file_size))
