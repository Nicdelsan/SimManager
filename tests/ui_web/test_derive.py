from __future__ import annotations

import pytest

from simmanager.contracts.timeseries import FileCursor
from simmanager.plugins.ui_web.derive import ETA_WINDOW_S, eta, read_fraction

T0 = 1.8e9


def test_eta_linear_progress() -> None:
    # 10 % per hour, sampled every 10 minutes, now at 50 %.
    x = [T0 + 600 * i for i in range(31)]
    y = [0.2 + 0.1 * (600 * i) / 3600 for i in range(31)]
    result = eta(x, y)
    assert result is not None
    assert result.rate_per_s == pytest.approx(0.1 / 3600)
    assert result.remaining_s == pytest.approx((1 - y[-1]) / (0.1 / 3600))
    assert result.at == pytest.approx(x[-1] + result.remaining_s)


def test_eta_uses_recent_window() -> None:
    # Fast for 5 h, then 4x slower for the last 2 h: ETA follows the recent rate.
    x, y = [], []
    for i in range(0, 5 * 3600 + 1, 600):
        x.append(T0 + i)
        y.append(0.1 * i / 3600)
    for _ in range(600, 2 * 3600 + 1, 600):
        x.append(x[-1] + 600)
        y.append(y[-1] + 0.025 * 600 / 3600)
    result = eta(x, y)
    assert result is not None
    assert result.rate_per_s == pytest.approx(0.025 / 3600)
    assert ETA_WINDOW_S == 3600


@pytest.mark.parametrize(
    ("x", "y"),
    [
        ([], []),
        ([T0], [0.5]),
        ([T0, T0 + 30], [0.1, 0.2]),  # span too short
        ([T0, T0 + 600], [0.3, 0.3]),  # stalled
        ([T0, T0 + 600], [0.9, 1.0]),  # done
        ([T0, T0 + 600], [0.4]),  # inconsistent
    ],
)
def test_eta_none(x: list[float], y: list[float]) -> None:
    assert eta(x, y) is None


def test_eta_sparse_points_older_than_window() -> None:
    # Progress changes rarely: the last two points are 2 h apart, still usable.
    assert eta([T0, T0 + 7200], [0.2, 0.4]) is not None


def test_read_fraction() -> None:
    base = FileCursor(run_id=1, file_path="/a", parser_kind="p")
    assert read_fraction(base) is None
    partial = FileCursor(
        run_id=1,
        file_path="/a",
        parser_kind="p",
        file_size=1000,
        backfill_start=100,
        backfill_end=700,
    )
    assert read_fraction(partial) == pytest.approx(0.4)
    done = FileCursor(
        run_id=1,
        file_path="/a",
        parser_kind="p",
        file_size=1000,
        backfill_start=700,
        backfill_end=700,
    )
    assert read_fraction(done) == 1.0
