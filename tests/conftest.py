from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolated_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Never touch the real user config/data directories from tests."""
    monkeypatch.setenv("SIMMANAGER_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("SIMMANAGER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture(autouse=True)
def block_real_desktop_notifications(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Ensure no test ever instantiates or sends through the real OS desktop_notifier."""
    import desktop_notifier
    import desktop_notifier.main

    real_send_attempts: list[dict[str, object]] = []

    class _BlockedDesktopNotifier:
        def __init__(self, *args: object, **kwargs: object) -> None:
            self.args = args
            self.kwargs = kwargs

        async def send(self, **kwargs: object) -> str:
            real_send_attempts.append(dict(kwargs))
            raise AssertionError(
                f"Real desktop_notifier.DesktopNotifier.send() called during test: {kwargs}"
            )

    monkeypatch.setattr(desktop_notifier, "DesktopNotifier", _BlockedDesktopNotifier)
    monkeypatch.setattr(desktop_notifier.main, "DesktopNotifier", _BlockedDesktopNotifier)
    yield
    assert not real_send_attempts, (
        "Real desktop notifications are forbidden in tests; "
        f"caught send attempts: {real_send_attempts}"
    )
