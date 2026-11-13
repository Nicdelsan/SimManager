from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path

from simmanager.contracts.notifier import NotificationMessage, NotificationSeverity
from simmanager.contracts.storage import EventRecord, Storage
from simmanager.kernel import Kernel, Plugin
from simmanager.plugins.notifier import (
    DesktopNotifierProvider,
    NotificationRouter,
    NotifierConfig,
    NotifierRouterPlugin,
    in_quiet_hours,
)
from simmanager.plugins.storage_sqlite import SqliteStoragePlugin


class _FakeProvider:
    def __init__(self, *, is_available: bool = True, fail: bool = False) -> None:
        self.sent: list[NotificationMessage] = []
        self._available = is_available
        self._fail = fail

    @property
    def kind(self) -> str:
        return "fake"

    def available(self) -> bool:
        return self._available

    async def send(self, message: NotificationMessage) -> bool:
        if self._fail:
            raise RuntimeError("boom")
        self.sent.append(message)
        return True


def _make_provider_plugin(provider: _FakeProvider, *, name: str = "fake-notifier") -> type[Plugin]:
    class _P(Plugin):
        provides = "notifier.fake"
        contract_version = 1

        def apply(self, ctx: object) -> _FakeProvider:
            return provider

    _P.name = name
    return _P


async def test_notifier_routing_immediate_first_then_batching_and_no_secrets(
    tmp_path: Path,
) -> None:
    provider = _FakeProvider()
    kernel = Kernel(
        {
            "storage-sqlite": {"path": str(tmp_path / "sim.db")},
            "notifier": {"batch_window_s": 0.15, "max_per_minute": 10},
        }
    )
    await kernel.add(SqliteStoragePlugin, _make_provider_plugin(provider), NotifierRouterPlugin)
    storage: Storage = kernel.services.require("storage")
    router: NotificationRouter = kernel.services.require("notifier.router")
    assert router.providers() == ["fake"]

    # Info alert below warning threshold -> ignored
    await storage.append_event(
        "alert.raised",
        {"alert_id": 1, "severity": "info", "title": "Info only", "message": "ignore me"},
        source="rules",
        cluster_id="c1",
        run_id=1,
    )
    assert provider.sent == []

    # 1st warning alert -> delivered IMMEDIATELY
    await storage.append_event(
        "alert.raised",
        {"alert_id": 2, "severity": "warning", "title": "Courant high", "message": "Co = 1.4"},
        source="rules",
        cluster_id="c1",
        run_id=1,
    )
    assert len(provider.sent) == 1
    assert provider.sent[0].title == "Courant high"

    # Subsequent alerts/events inside batch_window_s -> coalesced into one batch!
    await storage.append_event(
        "job.state_changed",
        {"job_id": "900", "from": "running", "to": "completed", "exit_code": "0:0"},
        source="monitor",
        cluster_id="c1",
        run_id=2,
    )
    await storage.append_event(
        "cluster.auth_required",
        {"prompt": "OTP", "password": "SUPER_SECRET_VALUE", "token": "XYZ_SECRET"},
        source="transport",
        cluster_id="c1",
    )
    # Still only the 1st immediate notification delivered so far
    assert len(provider.sent) == 1

    await asyncio.sleep(0.25)
    assert len(provider.sent) == 2
    batch = provider.sent[1]
    assert "2 notifications" in batch.title
    assert "Job 900 completed" in batch.body
    assert "Authentication required: c1" in batch.body
    assert "SUPER_SECRET_VALUE" not in batch.body
    assert "XYZ_SECRET" not in batch.body

    test_res = await router.send_test("Hello", "Body")
    assert test_res == {"fake": True}
    assert len(provider.sent) == 3

    await kernel.stop()


def test_quiet_hours_overnight_and_same_day() -> None:
    assert in_quiet_hours(datetime(2026, 11, 13, 23, 30), "22:00-07:00") is True
    assert in_quiet_hours(datetime(2026, 11, 13, 6, 59), "22:00-07:00") is True
    assert in_quiet_hours(datetime(2026, 11, 13, 12, 0), "22:00-07:00") is False
    assert in_quiet_hours(datetime(2026, 11, 13, 13, 15), "13:00-14:00") is True
    assert in_quiet_hours(datetime(2026, 11, 13, 15, 0), "13:00-14:00") is False


async def test_notifier_quiet_hours_and_rate_limit(tmp_path: Path) -> None:
    provider = _FakeProvider()
    kernel = Kernel({"storage-sqlite": {"path": str(tmp_path / "sim.db")}})
    await kernel.add(SqliteStoragePlugin, _make_provider_plugin(provider))

    # Direct router test with controlled clocks
    now_t = [100.0]
    local_dt = [datetime(2026, 11, 13, 23, 0)]

    class _DummyCtx:
        log = kernel.log
        services = kernel.services

        def task(self, coro: object, *, name: str = "") -> asyncio.Task[object]:
            return asyncio.create_task(coro)  # type: ignore[arg-type]

    cfg = NotifierConfig(
        batch_window_s=0.0,
        max_per_minute=2,
        quiet_hours="22:00-07:00",
        quiet_bypass_critical=True,
    )
    router = NotificationRouter(
        _DummyCtx(),  # type: ignore[arg-type]
        cfg,
        mono_clock=lambda: now_t[0],
        local_clock=lambda: local_dt[0],
    )

    # Warning suppressed in quiet hours; critical bypasses
    assert (
        await router.notify(NotificationMessage("w", "msg", severity=NotificationSeverity.WARNING))
        is False
    )
    assert (
        await router.notify(
            NotificationMessage("c1", "msg", severity=NotificationSeverity.CRITICAL)
        )
        is True
    )

    # Leave quiet hours; rate limit allows 1 more (2 total in 60s), blocks 3rd
    local_dt[0] = datetime(2026, 11, 13, 10, 0)
    assert (
        await router.notify(NotificationMessage("w2", "msg", NotificationSeverity.WARNING)) is True
    )
    assert (
        await router.notify(NotificationMessage("w3", "msg", NotificationSeverity.WARNING)) is False
    )

    # Advance > 60s -> allowed again
    now_t[0] = 165.0
    assert (
        await router.notify(NotificationMessage("w4", "msg", NotificationSeverity.WARNING)) is True
    )
    assert [m.title for m in provider.sent] == ["c1", "w2", "w4"]

    await router.close()
    await kernel.stop()


async def test_desktop_provider_fallback_when_unavailable() -> None:
    prov = DesktopNotifierProvider()

    class _BrokenClient:
        async def send(self, **kwargs: object) -> str:
            raise OSError("no notification daemon")

    prov._client = _BrokenClient()
    assert await prov.send(NotificationMessage("Title", "Body")) is False

    # Also verify auth_required formatting never leaks payload secrets
    from simmanager.plugins.notifier import format_event_notification

    evt = EventRecord(
        id=1,
        ts=datetime(2026, 11, 13, 12, 0),
        kind="cluster.auth_required",
        source="transport",
        cluster_id="uni-a",
        run_id=None,
        payload={"secret": "do_not_leak", "otp": "123456"},
    )
    msg = format_event_notification(evt, NotifierConfig())
    assert msg is not None
    assert "123456" not in msg.title and "123456" not in msg.body
    assert "do_not_leak" not in msg.title and "do_not_leak" not in msg.body
