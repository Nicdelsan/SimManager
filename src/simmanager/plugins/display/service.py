"""`display.manager`: runs library scripts on a run folder and keeps the images they make.

One execution per run at a time and at most `max_parallel_per_cluster` per cluster, so a
slow script never takes all the SSH channels the monitor needs.
"""

from __future__ import annotations

import asyncio
import contextlib
import difflib
import logging
import posixpath
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from simmanager.contracts import display as contract
from simmanager.contracts import timeseries as ts_contract
from simmanager.contracts import transport as transport_contract
from simmanager.contracts.display import (
    DisplayError,
    DisplayStore,
    ExecutionRecord,
    ExecutionStatus,
    ExecutionTrigger,
    FoundImage,
    ImageRecord,
    ImageSkip,
    ImageTooLargeError,
    RemoteEntry,
    ScheduleRecord,
    ScriptDraft,
    ScriptRecord,
    ScriptVersion,
    image_type,
)
from simmanager.contracts.scheduler import JobState
from simmanager.contracts.storage import RunRecord, Storage
from simmanager.contracts.timeseries import SolverState
from simmanager.contracts.transport import (
    ActionApproval,
    RemoteSession,
    Transport,
    TransportError,
)
from simmanager.kernel.services import ServiceView

from .command import (
    build_command,
    check_interpreter,
    describe_exit,
    new_marker,
    normalize_text,
    parse_output,
    safe_relpath,
)
from .config import DisplayConfig

SOURCE = "display"
EXECUTED_EVENT = "display.executed"
SCHEDULE_EVENT = "display.schedule_changed"
DURABLE_EVENT_KINDS = frozenset({EXECUTED_EVENT, SCHEDULE_EVENT})
HARD_MAX_IMAGE_BYTES = 1024 * 1024 * 1024
"""Even an explicit "download anyway" stops here: the bytes are held in memory."""
TAIL_CHARS = 4000
COMMAND_GRACE_S = 120
"""Extra transport timeout above `timeout_s` for the setup and the image listing."""
MAX_NAME = 120

Emit = Callable[[str, Any], Awaitable[None]]


def _now() -> datetime:
    return datetime.now(UTC)


class DisplayManager:
    """Implements `simmanager.contracts.display.DisplayService`."""

    def __init__(
        self,
        *,
        storage: Storage,
        store: DisplayStore,
        services: ServiceView,
        config: DisplayConfig,
        emit: Emit,
        log: logging.Logger,
    ) -> None:
        self._storage = storage
        self._store = store
        self._services = services
        self._cfg = config
        self._emit = emit
        self._log = log
        self._busy: set[int] = set()
        self._cluster_slots: dict[str, asyncio.Semaphore] = {}
        self._tasks: set[asyncio.Task[None]] = set()

    @property
    def max_image_bytes(self) -> int:
        return int(self._cfg.max_image_mb * 1024 * 1024)

    # ----------------------------------------------------------------- lifecycle
    async def close(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        for task in list(self._tasks):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        await self._store.interrupt_running()

    def _spawn(self, coro: Awaitable[None], name: str) -> None:
        task: asyncio.Task[None] = asyncio.ensure_future(coro)
        task.set_name(f"display:{name}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # ------------------------------------------------------------------ library
    async def save_script(self, draft: ScriptDraft) -> tuple[ScriptVersion, bool]:
        name = draft.name.strip()
        if not name or len(name) > MAX_NAME or any(c in name for c in "\0\r\n"):
            raise DisplayError(f"script name: 1 to {MAX_NAME} characters on one line")
        if not draft.body.strip():
            raise DisplayError("the script is empty")
        interpreter = " ".join(check_interpreter(draft.interpreter))
        clean = ScriptDraft(
            name=name,
            body=normalize_text(draft.body),
            setup=normalize_text(draft.setup),
            interpreter=interpreter,
            message=draft.message.strip()[:500],
        )
        return await self._store.save_script(clean)

    async def scripts(self) -> list[ScriptRecord]:
        return await self._store.scripts()

    async def versions(self, script_id: int) -> list[ScriptVersion]:
        return await self._store.versions(script_id)

    async def version(self, version_id: int) -> ScriptVersion | None:
        return await self._store.version(version_id)

    async def diff(self, old_version_id: int, new_version_id: int) -> str:
        old = await self._store.version(old_version_id)
        new = await self._store.version(new_version_id)
        if old is None or new is None:
            raise DisplayError("unknown script version")
        return "".join(
            difflib.unified_diff(
                _diff_text(old).splitlines(keepends=True),
                _diff_text(new).splitlines(keepends=True),
                fromfile=f"v{old.number}",
                tofile=f"v{new.number}",
            )
        )

    # --------------------------------------------------------------- executions
    def busy(self, run_id: int) -> bool:
        return run_id in self._busy

    async def start(
        self,
        run_id: int,
        script_id: int,
        *,
        trigger: ExecutionTrigger,
        approved_by: str,
    ) -> ExecutionRecord:
        run = await self._run(run_id)
        script = await self._store.script(script_id)
        if script is None:
            raise DisplayError("unknown script")
        if run_id in self._busy:
            raise DisplayError("a script is already running on this run")
        self._busy.add(run_id)
        try:
            execution = await self._store.start_execution(run_id, script.latest.id, trigger)
        except BaseException:
            self._busy.discard(run_id)
            raise
        approval = ActionApproval(
            action=contract.ACTION_RUN_SCRIPT,
            approved_by=approved_by,
            reason=f"script {script.name!r} v{script.latest.number} on run {run_id}",
        )
        self._spawn(
            self._execute(execution, run, script.latest, approval), f"run-{run_id}-{execution.id}"
        )
        await self._updated(execution.id, run_id, ExecutionStatus.RUNNING)
        return execution

    async def _execute(
        self,
        execution: ExecutionRecord,
        run: RunRecord,
        version: ScriptVersion,
        approval: ActionApproval,
    ) -> None:
        status, exit_status, out, err, error = ExecutionStatus.ERROR, None, "", "", ""
        images: list[ImageRecord] = []
        try:
            assert run.cluster_id is not None and run.work_dir is not None
            async with self._slot(run.cluster_id):
                session = await self._transport().session(run.cluster_id)
                marker = new_marker()
                result = await session.run_mutating(
                    build_command(
                        work_dir=run.work_dir,
                        setup=version.setup,
                        interpreter=version.interpreter,
                        timeout_s=self._cfg.timeout_s,
                        marker=marker,
                    ),
                    approval=approval,
                    timeout_s=self._cfg.timeout_s + COMMAND_GRACE_S,
                    input=version.body,
                )
                parsed = parse_output(result.stdout, marker)
                exit_status, out, err = result.exit_status, parsed.stdout, result.stderr
                error = describe_exit(exit_status, self._cfg.timeout_s)
                status = ExecutionStatus.OK if exit_status == 0 else ExecutionStatus.FAILED
                images = await self._copy_images(session, run.work_dir, execution.id, parsed.images)
        except asyncio.CancelledError:
            raise  # close() marks it interrupted
        except (TransportError, DisplayError) as exc:
            error = str(exc)
        except Exception as exc:  # a bug must not leave the run busy forever
            self._log.exception("display execution %s failed", execution.id)
            error = f"internal error: {exc}"
        finally:
            self._busy.discard(run.id)
        await self._store.finish_execution(
            execution.id,
            status=status,
            exit_status=exit_status,
            stdout_tail=out[-TAIL_CHARS:],
            stderr_tail=err[-TAIL_CHARS:],
            error=error,
        )
        await self._storage.append_event(
            EXECUTED_EVENT,
            {
                "execution_id": execution.id,
                "script": execution.script_name,
                "version": version.number,
                "trigger": str(execution.trigger),
                "status": str(status),
                "exit_status": exit_status,
                "images": len(images),
                "error": error,
            },
            source=SOURCE,
            cluster_id=run.cluster_id,
            run_id=run.id,
        )
        await self._updated(execution.id, run.id, status)

    async def _copy_images(
        self, session: RemoteSession, work_dir: str, execution_id: int, found: list[FoundImage]
    ) -> list[ImageRecord]:
        """Copy the newest images into storage; the others are listed as skipped."""
        out = []
        limit = self.max_image_bytes
        for index, image in enumerate(found):
            media_type = image_type(image.path) or "application/octet-stream"
            data: bytes | None = None
            skipped: ImageSkip | None = None
            detail = ""
            if index >= self._cfg.max_images_per_execution:
                skipped = ImageSkip.LIMIT
            elif image.size > limit:
                skipped = ImageSkip.TOO_LARGE
                detail = f"{image.size} bytes > display.max_image_mb ({self._cfg.max_image_mb})"
            else:
                try:
                    data = await _read_all(session, posixpath.join(work_dir, image.path), limit)
                except TransportError as exc:
                    skipped, detail = ImageSkip.READ_ERROR, str(exc)[:500]
            out.append(
                await self._store.add_image(execution_id, image, media_type, data, skipped, detail)
            )
        return out

    async def executions(self, run_id: int, *, limit: int = 50) -> list[ExecutionRecord]:
        return await self._store.executions(run_id, limit=limit)

    async def image_data(self, image_id: int) -> tuple[ImageRecord, bytes] | None:
        record = await self._store.image(image_id)
        data = await self._store.image_data(image_id)
        return (record, data) if record is not None and data is not None else None

    async def fetch_skipped(self, image_id: int) -> ImageRecord:
        record = await self._store.image(image_id)
        if record is None:
            raise DisplayError("unknown image")
        if record.sha256 is not None:
            return record
        execution = await self._store.execution(record.execution_id)
        assert execution is not None
        run = await self._run(execution.run_id)
        assert run.cluster_id is not None and run.work_dir is not None
        session = await self._transport().session(run.cluster_id)
        path = posixpath.join(run.work_dir, record.path)
        data = await _read_all(session, path, HARD_MAX_IMAGE_BYTES)
        return await self._store.attach_image_data(image_id, data)

    # ------------------------------------------------------------------ browser
    async def browse(self, run_id: int, path: str) -> list[RemoteEntry]:
        run = await self._run(run_id)
        rel = safe_relpath(path)
        assert run.cluster_id is not None and run.work_dir is not None
        session = await self._transport().session(run.cluster_id)
        listing = await session.listdir(posixpath.join(run.work_dir, rel) if rel else run.work_dir)
        entries = [
            RemoteEntry(
                name=posixpath.basename(s.path),
                path=posixpath.join(rel, posixpath.basename(s.path)),
                is_dir=s.is_dir,
                size=s.size,
                mtime=s.mtime,
                media_type=None if s.is_dir else image_type(s.path),
            )
            for s in listing
        ]
        return sorted(entries, key=lambda e: (not e.is_dir, e.name.lower()))

    async def read_image(self, run_id: int, path: str, *, force: bool) -> tuple[str, bytes]:
        run = await self._run(run_id)
        rel = safe_relpath(path)
        media_type = image_type(rel)
        if not rel or media_type is None:
            raise DisplayError("not an image (png, gif, jpg, webp)")
        assert run.cluster_id is not None and run.work_dir is not None
        session = await self._transport().session(run.cluster_id)
        full = posixpath.join(run.work_dir, rel)
        stat = await session.stat(full)
        if stat is None or stat.is_dir:
            raise DisplayError(f"{rel}: not found")
        limit = HARD_MAX_IMAGE_BYTES if force else self.max_image_bytes
        if stat.size > limit:
            raise ImageTooLargeError(rel, stat.size, limit)
        return media_type, await _read_all(session, full, limit)

    # ---------------------------------------------------------------- schedules
    async def set_schedule(
        self,
        run_id: int,
        script_id: int,
        *,
        interval_s: int,
        enabled: bool,
        approved_by: str,
    ) -> ScheduleRecord:
        run = await self._run(run_id)
        script = await self._store.script(script_id)
        if script is None:
            raise DisplayError("unknown script")
        if interval_s < self._cfg.min_interval_s:
            raise DisplayError(f"interval: at least {self._cfg.min_interval_s} s")
        record = await self._store.set_schedule(
            run_id, script_id, interval_s=interval_s, enabled=enabled
        )
        await self._storage.append_event(
            SCHEDULE_EVENT,
            {
                "script": script.name,
                "script_id": script_id,
                "interval_s": interval_s,
                "enabled": enabled,
                "approved_by": approved_by,
            },
            source=SOURCE,
            cluster_id=run.cluster_id,
            run_id=run_id,
        )
        return record

    async def schedules(self, run_id: int) -> list[ScheduleRecord]:
        return await self._store.schedules(run_id)

    async def schedule_loop(self) -> None:
        while True:
            try:
                await self.run_due_schedules()
            except asyncio.CancelledError:
                raise
            except Exception:
                self._log.exception("display schedule check failed")
            await asyncio.sleep(self._cfg.schedule_tick_s)

    async def run_due_schedules(self, now: datetime | None = None) -> list[ExecutionRecord]:
        """Start every enabled schedule that is due on a running run."""
        now = now or _now()
        started = []
        for sched in await self._store.schedules():
            if not sched.enabled or self.busy(sched.run_id):
                continue
            if sched.last_run_at and (now - sched.last_run_at).total_seconds() < sched.interval_s:
                continue
            if not await self._run_is_running(sched.run_id):
                continue
            await self._store.mark_scheduled_run(sched.run_id, sched.script_id, now)
            try:
                started.append(
                    await self.start(
                        sched.run_id,
                        sched.script_id,
                        trigger=ExecutionTrigger.AUTO,
                        approved_by=f"schedule approved {sched.approved_at.isoformat()}",
                    )
                )
            except DisplayError as exc:
                self._log.warning("display schedule on run %s: %s", sched.run_id, exc)
        return started

    async def _run_is_running(self, run_id: int) -> bool:
        job = await self._storage.job_for_run(run_id)
        if job is not None:
            return job.active and job.info.state == JobState.RUNNING
        ts = self._services.get(ts_contract.SERVICE_KEY)
        if ts is None:
            return False
        state = await ts.get_run_solver_state(run_id)
        return state is not None and state.solver_state == SolverState.RUNNING

    # ------------------------------------------------------------------ helpers
    async def _run(self, run_id: int) -> RunRecord:
        run = await self._storage.run(run_id)
        if run is None:
            raise DisplayError("unknown run")
        if not run.cluster_id or not run.work_dir:
            raise DisplayError("the run has no cluster folder")
        return run

    def _transport(self) -> Transport:
        transport = self._services.get(transport_contract.SERVICE_KEY)
        if transport is None:
            raise DisplayError("the SSH transport is not available")
        return transport  # type: ignore[no-any-return]

    def _slot(self, cluster_id: str) -> asyncio.Semaphore:
        if cluster_id not in self._cluster_slots:
            self._cluster_slots[cluster_id] = asyncio.Semaphore(self._cfg.max_parallel_per_cluster)
        return self._cluster_slots[cluster_id]

    async def _updated(self, execution_id: int, run_id: int, status: ExecutionStatus) -> None:
        await self._emit(
            contract.UPDATED_EVENT,
            {"run_id": run_id, "execution_id": execution_id, "status": str(status)},
        )


async def _read_all(session: RemoteSession, path: str, limit: int) -> bytes:
    data = await session.read_range(path, 0, limit + 1)
    if len(data) > limit:
        raise TransportError(f"{path}: larger than {limit} bytes")
    return data


def _diff_text(v: ScriptVersion) -> str:
    return f"# interpreter: {v.interpreter}\n# setup:\n{v.setup}# script:\n{v.body}"
