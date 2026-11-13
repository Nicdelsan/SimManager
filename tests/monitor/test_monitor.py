"""Monitor loop logic on a fake transport with the real SLURM scheduler and SQLite storage."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from pathlib import Path
from typing import Any, ClassVar

import pytest

from simmanager.contracts.clusters import ClusterProfile
from simmanager.contracts.scheduler import JobState
from simmanager.contracts.transport import (
    ClusterUnavailableError,
    CommandResult,
    ConnectionState,
    RemoteStat,
)
from simmanager.kernel import Context, Kernel, Plugin
from simmanager.plugins.monitor import JobMonitor, MonitorConfig, MonitorPlugin
from simmanager.plugins.scheduler_slurm import SlurmScheduler, SlurmSchedulerPlugin
from simmanager.plugins.scheduler_slurm.parse import sacct_argv, scontrol_argv, squeue_argv
from simmanager.plugins.storage_sqlite import SqliteStorage, SqliteStoragePlugin

C = "c1"
USER = "user1"
SQUEUE = tuple(squeue_argv(USER))


def line(job_id: str, state: str, work_dir: str = "/work/user1/a") -> str:
    return f"{job_id}|case|{state}|1:00|1-00:00:00|1|debug|2026-11-13T10:00:00|N/A|node1|{work_dir}"


class FakeSession:
    cluster_id = C
    user = USER

    def __init__(self, outputs: dict[tuple[str, ...], CommandResult]) -> None:
        self.outputs = outputs
        self.calls: list[tuple[str, ...]] = []

    async def run_readonly(self, argv: Sequence[str], *, timeout_s: float = 30) -> CommandResult:
        key = tuple(argv)
        self.calls.append(key)
        return self.outputs.get(key, CommandResult(key, 1, "", "unknown job"))

    async def run_mutating(self, *args: Any, **kwargs: Any) -> CommandResult:
        raise AssertionError("the monitor must never mutate")

    async def stat(self, path: str) -> RemoteStat | None:
        return None

    async def listdir(self, path: str) -> list[RemoteStat]:
        return []

    async def read_range(self, path: str, offset: int, max_bytes: int) -> bytes:
        return b""


class FakeTransport:
    def __init__(self) -> None:
        self.outputs: dict[tuple[str, ...], CommandResult] = {}
        self.session_obj = FakeSession(self.outputs)
        self.down = False

    def clusters(self) -> Mapping[str, ClusterProfile]:
        return {C: ClusterProfile(host="h", timezone="UTC")}

    async def session(self, cluster_id: str) -> FakeSession:
        if self.down:
            raise ClusterUnavailableError(cluster_id, ConnectionState.AWAITING_AUTH, "otp")
        return self.session_obj

    def state(self, cluster_id: str) -> ConnectionState:
        return ConnectionState.CONNECTED

    async def reset(self, cluster_id: str) -> None:
        return None

    def squeue(self, *lines: str, status: int = 0) -> None:
        text = "".join(f"{x}\n" for x in lines)
        self.outputs[SQUEUE] = CommandResult(SQUEUE, status, text, "boom" if status else "")

    def reply(self, argv: list[str], stdout: str, status: int = 0) -> None:
        self.outputs[tuple(argv)] = CommandResult(tuple(argv), status, stdout, "")


class Services:
    def __init__(self) -> None:
        self.scheduler = SlurmScheduler()

    def get(self, key: str) -> Any:
        return self.scheduler if key == "scheduler.slurm" else None


@pytest.fixture
async def storage(tmp_path: Path) -> AsyncIterator[SqliteStorage]:
    s = await SqliteStorage.open(str(tmp_path / "m.db"))
    yield s
    await s.close()


def make_monitor(storage: SqliteStorage, transport: FakeTransport, **cfg: Any) -> JobMonitor:
    async def emit(event: str, payload: Any) -> None:
        return None

    return JobMonitor(
        storage=storage,
        transport=transport,  # type: ignore[arg-type]
        services=Services(),  # type: ignore[arg-type]
        emit=emit,
        config=MonitorConfig(loop=False, **cfg),
    )


async def kinds(storage: SqliteStorage) -> list[str]:
    return [e.kind for e in await storage.events()]


SACCT_DONE = (
    "10|case|COMPLETED|0:0|01:00:00|2026-11-13T10:00:00|2026-11-13T10:00:00|"
    "2026-11-13T11:00:00|/work/user1/a|debug|1|1-00:00:00|user1|node1\n"
)
SCONTROL_FAILED = (
    "JobId=11 JobName=case\n"
    "   JobState=FAILED Reason=NonZeroExitCode Dependency=(null)\n"
    "   ExitCode=1:0\n"
    "   EndTime=2026-11-13T12:00:00\n"
)


async def test_discovery_state_change_and_end(storage: SqliteStorage) -> None:
    t = FakeTransport()
    m = make_monitor(storage, t)

    t.squeue(line("10", "PENDING"))
    r1 = await m.poll(C)
    assert r1.ok and r1.discovered == 1 and r1.next_poll_s == pytest.approx(180, rel=0.11)
    assert await kinds(storage) == ["job.discovered", "run.created"]

    t.squeue(line("10", "RUNNING"))
    r2 = await m.poll(C)
    assert r2.changed == 1 and r2.next_poll_s == pytest.approx(60, rel=0.11)

    t.squeue()  # the job left the queue: sacct says it completed
    t.reply(sacct_argv(["10"]), SACCT_DONE)
    r3 = await m.poll(C)
    assert r3.changed == 1 and r3.active_jobs == 0
    assert r3.next_poll_s == pytest.approx(300, rel=0.11)

    (job,) = await storage.jobs(cluster_id=C)
    assert job.info.state is JobState.COMPLETED and not job.active
    assert job.info.end_time is not None
    changes = [e.payload for e in await storage.events(kinds=["job.state_changed"])]
    assert [(c["from"], c["to"]) for c in changes] == [
        ("queued", "running"),
        ("running", "completed"),
    ]
    assert all(e.run_id == job.run_id for e in await storage.events())


async def test_without_accounting_uses_scontrol(storage: SqliteStorage) -> None:
    t = FakeTransport()
    m = make_monitor(storage, t)
    t.squeue(line("11", "RUNNING"), line("12", "RUNNING"))
    await m.poll(C)

    t.squeue()
    t.reply(sacct_argv(["11", "12"]), "Slurm accounting storage is disabled\n")
    t.reply(scontrol_argv("11"), SCONTROL_FAILED)
    await m.poll(C)  # job 12: neither sacct nor scontrol knows it any more

    states = {j.info.job_id: j.info.state for j in await storage.jobs(cluster_id=C)}
    assert states == {"11": JobState.FAILED, "12": JobState.UNKNOWN}

    t.squeue(line("13", "RUNNING"))
    await m.poll(C)
    t.squeue()
    t.session_obj.calls.clear()
    await m.poll(C)
    assert not any(c[0] == "sacct" for c in t.session_obj.calls)  # remembered: no accounting


async def test_restart_with_existing_database_creates_no_duplicates(
    storage: SqliteStorage,
) -> None:
    t = FakeTransport()
    t.squeue(line("20", "RUNNING"))
    await make_monitor(storage, t).poll(C)
    await make_monitor(storage, t).poll(C)  # a new process on the same database
    assert await kinds(storage) == ["job.discovered", "run.created"]
    assert len(await storage.jobs()) == 1


async def test_failures_and_unavailable_cluster(storage: SqliteStorage) -> None:
    t = FakeTransport()
    m = make_monitor(storage, t, failure_threshold=2)
    t.down = True
    r = await m.poll(C)
    assert not r.ok and "otp" in (r.error or "")
    assert r.next_poll_s == pytest.approx(300, rel=0.11)

    t.down = False
    t.squeue(status=1)  # a squeue error must never look like "no jobs"
    for _ in range(3):
        assert not (await m.poll(C)).ok
    assert (await kinds(storage)).count("cluster.degraded") == 1

    t.squeue()
    assert (await m.poll(C)).ok
    assert (await kinds(storage))[-1] == "cluster.recovered"


async def test_array_tasks_are_separate_runs(storage: SqliteStorage) -> None:
    t = FakeTransport()
    t.squeue(line("30_1", "RUNNING"), line("30_2", "PENDING"))
    await make_monitor(storage, t).poll(C)
    runs = {j.info.job_id: j.run_id for j in await storage.jobs()}
    assert set(runs) == {"30_1", "30_2"} and len(set(runs.values())) == 2


class FakeTransportPlugin(Plugin):
    name = "fake-transport"
    provides = "transport"
    instance: ClassVar[FakeTransport]

    def apply(self, ctx: Context) -> FakeTransport:
        return FakeTransportPlugin.instance


async def test_unloading_monitor_keeps_jobs(tmp_path: Path) -> None:
    FakeTransportPlugin.instance = FakeTransport()
    FakeTransportPlugin.instance.squeue(line("40", "RUNNING"))
    kernel = Kernel(
        {"storage-sqlite": {"path": str(tmp_path / "k.db")}, "monitor": {"loop": False}}
    )
    await kernel.add(SqliteStoragePlugin, FakeTransportPlugin, SlurmSchedulerPlugin, MonitorPlugin)
    await kernel.services.require("monitor").poll_all()
    await kernel.remove("monitor")
    assert len(await kernel.services.require("storage").jobs()) == 1
    await kernel.stop()
