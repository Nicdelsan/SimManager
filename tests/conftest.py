from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolated_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Never touch the real user config/data directories from tests."""
    monkeypatch.setenv("SIMMANAGER_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("SIMMANAGER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.chdir(tmp_path)
    return tmp_path
