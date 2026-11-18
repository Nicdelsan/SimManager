"""Display: script library versions, execution on a run folder, image history, browsing,
auto-refresh schedules (ADR 0008)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from tests.display_fakes import PNG, WORK, C, FakeTransport, ScriptSession, transport_plugin

from simmanager.contracts.display import (
    DisplayError,
    ExecutionRecord,
    ExecutionStatus,
    ExecutionTrigger,
    ImageSkip,
    ImageTooLargeError,
    ScriptDraft,
    image_type,
)
from simmanager.contracts.scheduler import JobInfo, JobState
from simmanager.kernel import Kernel
from simmanager.plugins.display import DisplayManager, DisplayPlugin
from simmanager.plugins.display.command import (
    build_command,
    parse_output,
    safe_relpath,
)
from simmanager.plugins.storage_sqlite import (
    SqliteDisplayPlugin,
    SqliteStorage,
    SqliteStoragePlugin,
)


@dataclass
class Env:
    kernel: Kernel
    storage: SqliteStorage
    display: DisplayManager
    session: ScriptSession
    run_id: int


@pytest.fixture
async def env(tmp_path: Path) -> AsyncIterator[Env]:
    root = tmp_path / "case_a"
    root.mkdir()
    session = ScriptSession(root)
    kernel = Kernel(
        {
            "storage-sqlite": {"path": str(tmp_path / "sim.db")},
            "display": {"max_image_mb": 0.001, "max_images_per_execution": 2},
        }
    )
    await kernel.add(
        SqliteStoragePlugin,
        SqliteDisplayPlugin,
        transport_plugin(FakeTransport(session)),
        DisplayPlugin,
    )
    storage = kernel.services.require("storage")
    upsert = await storage.upsert_job(
        C,
        JobInfo(
            job_id="101",
            name="case_a",
            state=JobState.RUNNING,
            raw_state="RUNNING",
            work_dir=WORK,
        ),
        active=True,
    )
    assert upsert.record.run_id is not None
    display = kernel.services.require("display.manager")
    try:
        yield Env(kernel, storage, display, session, upsert.record.run_id)
    finally:
        await kernel.stop()


async def wait_done(env: Env, execution_id: int) -> ExecutionRecord:
    for _ in range(200):
        record = next(e for e in await env.display.executions(env.run_id) if e.id == execution_id)
        if record.status != ExecutionStatus.RUNNING:
            return record
        await asyncio.sleep(0.01)
    raise AssertionError("execution did not finish")


async def save(env: Env, body: str = "print('hi')\n", setup: str = "") -> int:
    version, _ = await env.display.save_script(ScriptDraft(name="slice", body=body, setup=setup))
    return version.script_id


async def start(env: Env, script_id: int) -> ExecutionRecord:
    return await env.display.start(
        env.run_id, script_id, trigger=ExecutionTrigger.MANUAL, approved_by="web"
    )


# ------------------------------------------------------------------- command
def test_command_keeps_the_script_out_of_the_command_line() -> None:
    argv = build_command(
        work_dir="/work/user 1/case's",
        setup="module load python\r\n",
        interpreter="python3 -u",
        timeout_s=300,
        marker="__M__",
    )
    assert argv[:2] == ["bash", "-lc"]
    text = argv[2]
    assert "cd -- '/work/user 1/case'\"'\"'s' || exit 98" in text
    assert "module load python\n" in text and "\r" not in text
    assert 'timeout -k 10 300 python3 -u "$tmp"' in text
    assert "echo __M__" in text and '-newer "$tmp"' in text
    with pytest.raises(DisplayError):
        build_command(
            work_dir="/w", setup="", interpreter="python3; rm -rf ~", timeout_s=1, marker="__M__"
        )


def test_parse_output_splits_script_output_and_images() -> None:
    out = parse_output(
        "line\n__M__\n10.0\t5\tpost/a.png\n20.5\t7\tb.GIF\nbad line\n3\t1\tnotes.txt\n", "__M__"
    )
    assert out.stdout == "line\n" and out.listed
    assert [(i.path, i.size, i.mtime) for i in out.images] == [
        ("b.GIF", 7, 20.5),
        ("post/a.png", 5, 10.0),
    ]
    assert not parse_output("no marker\n", "__M__").listed


def test_paths_cannot_leave_the_run_folder() -> None:
    assert safe_relpath("") == "" and safe_relpath("./post/../post/x.png") == "post/x.png"
    for bad in ("/etc/passwd", "..", "../other", "post/../../x", "a\nb"):
        with pytest.raises(DisplayError):
            safe_relpath(bad)
    assert image_type("a/b.JPEG") == "image/jpeg"
    assert image_type("plot.svg") is None and image_type(".png") is None


# ------------------------------------------------------------------- library
async def test_script_versions_only_when_content_changes(env: Env) -> None:
    v1, created1 = await env.display.save_script(
        ScriptDraft(name="slice", body="print(1)\r\n", message="first")
    )
    v1b, created1b = await env.display.save_script(ScriptDraft(name="slice", body="print(1)"))
    v2, created2 = await env.display.save_script(
        ScriptDraft(name="slice", body="print(2)\n", setup="module load py")
    )
    assert (created1, created1b, created2) == (True, False, True)
    assert v1b.id == v1.id and v1.body == "print(1)\n"
    assert (v1.number, v2.number) == (1, 2)
    assert [v.number for v in await env.display.versions(v1.script_id)] == [2, 1]
    diff = await env.display.diff(v1.id, v2.id)
    assert "-print(1)" in diff and "+print(2)" in diff and "+module load py" in diff
    with pytest.raises(DisplayError):
        await env.display.save_script(ScriptDraft(name="x", body="  "))


# ----------------------------------------------------------------- execution
async def test_run_copies_new_images_and_records_everything(env: Env) -> None:
    script_id = await save(env, setup="module load python")
    env.session.produce = {"post/slice.png": PNG}
    started = await start(env, script_id)
    assert started.status == ExecutionStatus.RUNNING
    done = await wait_done(env, started.id)

    assert done.status == ExecutionStatus.OK and done.error == ""
    assert done.stdout_tail == "hello from the script\n"
    [call] = env.session.calls
    assert call.input == "print('hi')\n"  # body on stdin...
    assert "print('hi')" not in call.argv[2]  # ...never in the logged command line
    assert call.approval.action == "display.run_script" and call.approval.approved_by == "web"
    [image] = done.images
    assert (image.path, image.media_type, image.skipped) == ("post/slice.png", "image/png", None)
    stored = await env.display.image_data(image.id)
    assert stored is not None and stored[1] == PNG
    events = [e for e in await env.storage.events() if e.kind == "display.executed"]
    assert events[0].run_id == env.run_id and events[0].payload["images"] == 1
    assert not env.display.busy(env.run_id)


async def test_large_and_extra_images_are_listed_not_copied(env: Env) -> None:
    script_id = await save(env)
    big = b"G" * 5000  # > max_image_mb (0.001 MB ~ 1048 bytes)
    env.session.produce = {"a.png": PNG, "b.png": PNG, "c.gif": big}
    done = await wait_done(env, (await start(env, script_id)).id)
    by_path = {i.path: i for i in done.images}
    assert by_path["c.gif"].skipped == ImageSkip.TOO_LARGE
    assert sum(i.skipped == ImageSkip.LIMIT for i in done.images) == 1  # 2 per execution
    fetched = await env.display.fetch_skipped(by_path["c.gif"].id)  # "download anyway"
    assert fetched.skipped is None
    stored = await env.display.image_data(fetched.id)
    assert stored is not None and stored[1] == big


async def test_failed_script_and_busy_run(env: Env) -> None:
    script_id = await save(env)
    env.session.exit_status = 124
    env.session.gate = asyncio.Event()
    started = await start(env, script_id)
    with pytest.raises(DisplayError, match="already running"):
        await start(env, script_id)
    env.session.gate.set()
    done = await wait_done(env, started.id)
    assert done.status == ExecutionStatus.FAILED and "timed out" in done.error


async def test_stop_marks_running_executions_interrupted(tmp_path: Path) -> None:
    root = tmp_path / "case_a"
    root.mkdir()
    session = ScriptSession(root)
    session.gate = asyncio.Event()  # never set: the script "hangs"
    db = str(tmp_path / "sim.db")
    kernel = Kernel({"storage-sqlite": {"path": db}})
    await kernel.add(
        SqliteStoragePlugin,
        SqliteDisplayPlugin,
        transport_plugin(FakeTransport(session)),
        DisplayPlugin,
    )
    run = await kernel.services.require("storage").create_run(work_dir=WORK, cluster_id=C)
    display = kernel.services.require("display.manager")
    v, _ = await display.save_script(ScriptDraft(name="s", body="x=1"))
    started = await display.start(
        run.id, v.script_id, trigger=ExecutionTrigger.MANUAL, approved_by="web"
    )
    await asyncio.sleep(0.05)
    await kernel.stop()

    kernel = Kernel({"storage-sqlite": {"path": db}})
    await kernel.add(SqliteStoragePlugin, SqliteDisplayPlugin)
    try:
        record = await kernel.services.require("display").execution(started.id)
        assert record.status == ExecutionStatus.INTERRUPTED
    finally:
        await kernel.stop()


# ------------------------------------------------------------------- browser
async def test_browse_and_read_images_inside_the_run_folder(env: Env) -> None:
    (env.session.root / "post").mkdir()
    (env.session.root / "post" / "t.png").write_bytes(PNG)
    (env.session.root / "post" / "big.gif").write_bytes(b"G" * 5000)
    (env.session.root / "log.txt").write_text("x")

    top = await env.display.browse(env.run_id, "")
    assert [(e.name, e.is_dir) for e in top] == [("post", True), ("log.txt", False)]
    inner = await env.display.browse(env.run_id, "post")
    assert {e.path: e.media_type for e in inner} == {
        "post/big.gif": "image/gif",
        "post/t.png": "image/png",
    }
    assert await env.display.read_image(env.run_id, "post/t.png", force=False) == (
        "image/png",
        PNG,
    )
    with pytest.raises(ImageTooLargeError):
        await env.display.read_image(env.run_id, "post/big.gif", force=False)
    forced = await env.display.read_image(env.run_id, "post/big.gif", force=True)
    assert forced[1][:1] == b"G"
    for bad in ("../x.png", "log.txt", "post/missing.png"):
        with pytest.raises(DisplayError):
            await env.display.read_image(env.run_id, bad, force=False)


# ----------------------------------------------------------------- schedules
async def test_schedules_run_when_due_on_running_jobs_only(env: Env) -> None:
    script_id = await save(env)
    with pytest.raises(DisplayError, match="at least"):
        await env.display.set_schedule(
            env.run_id, script_id, interval_s=5, enabled=True, approved_by="web"
        )
    await env.display.set_schedule(
        env.run_id, script_id, interval_s=600, enabled=True, approved_by="web"
    )
    assert any(e.kind == "display.schedule_changed" for e in await env.storage.events())

    t0 = datetime.now(UTC)
    [first] = await env.display.run_due_schedules(t0)
    assert first.trigger == ExecutionTrigger.AUTO
    await wait_done(env, first.id)
    assert env.session.calls[-1].approval.approved_by.startswith("schedule approved")
    assert await env.display.run_due_schedules(t0 + timedelta(seconds=60)) == []  # not due
    [second] = await env.display.run_due_schedules(t0 + timedelta(seconds=601))
    await wait_done(env, second.id)

    await env.storage.upsert_job(  # job finished: no more auto-refresh
        C,
        JobInfo(
            job_id="101",
            name="case_a",
            state=JobState.COMPLETED,
            raw_state="COMPLETED",
            work_dir=WORK,
        ),
        active=False,
    )
    assert await env.display.run_due_schedules(t0 + timedelta(seconds=1300)) == []
