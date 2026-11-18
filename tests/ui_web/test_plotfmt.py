"""Chart number formatting (static/js/plotfmt.js): CFD-sized values must not read "0".

Runs the ES module with Node.js; skipped when Node is not installed."""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from simmanager.plugins.ui_web.app import STATIC_DIR

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="Node.js not installed")

CASES = [
    # (function, input, expected)
    ("fmtValue", 1.234567e-5, "1.23457e-5"),
    ("fmtValue", 2.5e-4, "2.5e-4"),
    ("fmtValue", 0.0125, "0.0125"),
    ("fmtValue", 0, "0"),
    ("fmtValue", 1013.25, "1013.25"),
    ("fmtValue", 123456.789, "123457"),
    ("fmtValue", 2.0e7, "2e+7"),
    ("fmtValue", -3.5e-6, "-3.5e-6"),
    ("fmtValue", None, "—"),
    ("fmtTick", 1e-5, "1e-5"),
    ("fmtTick", 0.30000000000000004, "0.3"),
    ("fmtTick", 0.002, "0.002"),
    ("fmtTick", 1500, "1500"),
    ("fmtTick", 1.5e6, "1.5e+6"),
    ("fmtTick", None, ""),
]


def _run_node(cases: list[tuple[str, float | None, str]]) -> list[str]:
    module = (STATIC_DIR / "js" / "plotfmt.js").as_uri()
    calls = json.dumps([[fn, arg] for fn, arg, _ in cases])
    script = (
        f"import * as f from {json.dumps(module)};"
        f"console.log(JSON.stringify({calls}.map(([fn, a]) => f[fn](a))));"
    )
    assert NODE is not None
    out = subprocess.run(
        [NODE, "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
        timeout=30,
    )
    result: list[str] = json.loads(out.stdout)
    return result


def test_small_and_large_values_keep_their_significant_digits() -> None:
    got = _run_node(CASES)
    for (fn, arg, expected), actual in zip(CASES, got, strict=True):
        assert actual == expected, (fn, arg)
