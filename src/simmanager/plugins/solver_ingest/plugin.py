"""`solver-ingest` plugin: incremental solver ingestion, backfill tasks and M1 event hooks."""

from __future__ import annotations

import asyncio
import posixpath
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field

from simmanager.contracts import ingest as contract
from simmanager.contracts import solver as solver_contract
from simmanager.contracts import storage as storage_contract
from simmanager.contracts import timeseries as ts_contract
from simmanager.contracts import transport as transport_contract
from simmanager.contracts.ingest import SyncRunResult
from simmanager.contracts.solver import Solver, SolverStatusUpdate, WatchedFile
from simmanager.contracts.storage import Storage
from simmanager.contracts.timeseries import RunSolverState, SolverState, TimeSeriesStore
from simmanager.contracts.transport import RemoteSession, Transport, TransportError
from simmanager.kernel import Context, Plugin
from simmanager.readers import IncrementalReader, ReaderConfig

_TERMINAL_JOB_STATES = frozenset({"completed", "failed", "cancelled", "timeout", "unknown"})


class IngestConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    chunk_mb: int = Field(default=4, ge=1, le=32)
    full_read_max_mb: int = Field(default=50, ge=1)
    tail_preview_mb: int = Field(default=20, ge=1)
    poll_interval_s: float = Field(default=30.0, gt=0)
    auto_backfill: bool = True


@dataclass
class _TrackedRun:
    run_id: int
    cluster_id: str
    work_dir: str
    solver_kind: str | None = None
    stdout_path: str | None = None
    terminal: bool = False


class SolverIngestor:
    """Implements `simmanager.contracts.ingest.IngestService`."""

    def __init__(
        self,
        ctx: Context,
        storage: Storage,
        timeseries: TimeSeriesStore,
        cfg: IngestConfig,
    ) -> None:
        self._ctx = ctx
        self._storage = storage
        self._ts = timeseries
        self._cfg = cfg
        self._reader = IncrementalReader(
            ReaderConfig(
                chunk_bytes=cfg.chunk_mb * 1024 * 1024,
                full_read_max_bytes=cfg.full_read_max_mb * 1024 * 1024,
                tail_preview_bytes=cfg.tail_preview_mb * 1024 * 1024,
            )
        )
        self._tracked: dict[int, _TrackedRun] = {}
        self._backfill_tasks: dict[tuple[int, str], asyncio.Task[None]] = {}
        self._run_locks: dict[int, asyncio.Lock] = {}

    def _run_lock(self, run_id: int) -> asyncio.Lock:
        lock = self._run_locks.get(run_id)
        if lock is None:
            lock = asyncio.Lock()
            self._run_locks[run_id] = lock
        return lock

    def _solvers(self) -> dict[str, Solver]:
        raw: dict[str, Any] = self._ctx.services.all("solver")
        return {k.removeprefix(solver_contract.SERVICE_PREFIX): v for k, v in raw.items()}

    async def detect_solver(
        self, session: RemoteSession, work_dir: str, solver_hint: str | None = None
    ) -> Solver | None:
        solvers = self._solvers()
        if solver_hint and solver_hint in solvers:
            return solvers[solver_hint]

        wdir = posixpath.normpath(work_dir).rstrip("/")
        if "converge" in solvers and (
            await session.stat(f"{wdir}/inputs.in") is not None
            or await session.stat(f"{wdir}/logout.txt") is not None
        ):
            return solvers["converge"]

        if "openfoam" in solvers and (
            await session.stat(f"{wdir}/system/controlDict") is not None
            or await session.stat(f"{wdir}/log.out") is not None
        ):
            return solvers["openfoam"]

        for solver in solvers.values():
            _, watched = await solver.discover(session, work_dir)
            if watched:
                return solver
        return None

    async def sync_run(
        self,
        session: RemoteSession,
        run_id: int,
        work_dir: str,
        *,
        solver_kind: str | None = None,
        stdout_path: str | None = None,
        run_backfill_immediately: bool = False,
    ) -> SyncRunResult:
        async with self._run_lock(run_id):
            solver = await self.detect_solver(session, work_dir, solver_hint=solver_kind)
            if solver is None:
                return SyncRunResult(run_id=run_id)

            existing = await self._ts.get_run_solver_state(run_id)
            init_status, watched = await solver.discover(
                session,
                work_dir,
                stdout_path=stdout_path,
                known_output_dir=existing.output_dir if existing else None,
            )

            updates: list[SolverStatusUpdate] = []
            if init_status is not None:
                updates.append(init_status)

            bytes_read = 0
            points_ingested = 0
            backfill_pending = 0
            emitted_events: list[tuple[str, dict[str, Any]]] = []

            for w in watched:
                res = await self._reader.sync_file(session, self._ts, solver, run_id, w)
                bytes_read += res.bytes_read
                points_ingested += res.points_ingested
                updates.extend(res.status_updates)
                emitted_events.extend(res.events)

                if res.needs_backfill:
                    if run_backfill_immediately:
                        prog = await self._reader.backfill_step(
                            session, self._ts, solver, run_id, w, max_chunks=None
                        )
                        bytes_read += prog.bytes_read
                        points_ingested += prog.points_ingested
                        emitted_events.extend(prog.events)
                        if prog.warning_delta:
                            updates.append(SolverStatusUpdate(warning_delta=prog.warning_delta))
                    else:
                        backfill_pending += 1
                        if self._cfg.auto_backfill:
                            self._schedule_backfill(
                                session.cluster_id, run_id, solver, w, session=session
                            )

            merged = _merge_run_state(run_id, solver.kind, work_dir, existing, updates)
            await self._ts.upsert_run_solver_state(merged)

            for kind, payload in emitted_events:
                await self._record_event(
                    kind, payload, cluster_id=session.cluster_id, run_id=run_id
                )

            if points_ingested > 0:
                await self._record_event(
                    "series.updated",
                    {
                        "solver": solver.kind,
                        "points_ingested": points_ingested,
                        "files": [w.file_path for w in watched],
                    },
                    cluster_id=session.cluster_id,
                    run_id=run_id,
                )

            if merged.progress is not None and (
                existing is None or merged.progress != existing.progress
            ):
                await self._record_event(
                    "run.progress",
                    {
                        "solver": solver.kind,
                        "solver_state": str(merged.solver_state),
                        "progress": merged.progress,
                        "current_time": merged.current_time,
                    },
                    cluster_id=session.cluster_id,
                    run_id=run_id,
                )

            return SyncRunResult(
                run_id=run_id,
                solver=solver.kind,
                files_synced=len(watched),
                bytes_read=bytes_read,
                points_ingested=points_ingested,
                backfill_pending=backfill_pending,
                solver_state=merged.solver_state,
                progress=merged.progress,
                watched_files=[w.file_path for w in watched],
            )

    def _schedule_backfill(
        self,
        cluster_id: str,
        run_id: int,
        solver: Solver,
        watched: WatchedFile,
        *,
        session: RemoteSession | None = None,
    ) -> None:
        key = (run_id, watched.file_path)
        existing_task = self._backfill_tasks.get(key)
        if existing_task is not None and not existing_task.done():
            return

        async def _runner() -> None:
            try:
                while not self._ctx.closed:
                    active_session = session
                    if active_session is None:
                        transport: Transport | None = self._ctx.services.get(
                            transport_contract.SERVICE_KEY
                        )
                        if transport is None:
                            return
                        active_session = await transport.session(cluster_id)

                    prog = await self._reader.backfill_step(
                        active_session, self._ts, solver, run_id, watched, max_chunks=1
                    )
                    if prog.warning_delta:
                        cur = await self._ts.get_run_solver_state(run_id)
                        if cur is not None:
                            await self._ts.upsert_run_solver_state(
                                _merge_run_state(
                                    run_id,
                                    cur.solver,
                                    cur.work_dir,
                                    cur,
                                    [SolverStatusUpdate(warning_delta=prog.warning_delta)],
                                )
                            )
                    for kind, payload in prog.events:
                        await self._record_event(
                            kind, payload, cluster_id=cluster_id, run_id=run_id
                        )
                    await self._record_event(
                        "backfill.progress",
                        {
                            "file_path": prog.file_path,
                            "bytes_read": prog.bytes_read,
                            "remaining_bytes": prog.remaining_bytes,
                            "done": prog.done,
                        },
                        cluster_id=cluster_id,
                        run_id=run_id,
                    )
                    if prog.done:
                        return
                    await asyncio.sleep(0.05)
            except asyncio.CancelledError:
                raise
            except TransportError as exc:
                self._ctx.log.warning(
                    "backfill paused for run %s (%s): %s", run_id, watched.file_path, exc
                )
            except Exception:
                self._ctx.log.exception(
                    "backfill failed for run %s (%s)", run_id, watched.file_path
                )
            finally:
                self._backfill_tasks.pop(key, None)

        self._backfill_tasks[key] = self._ctx.task(
            _runner(), name=f"backfill-{run_id}-{watched.file_path}"
        )

    async def _record_event(
        self,
        kind: str,
        payload: Mapping[str, Any],
        *,
        cluster_id: str | None,
        run_id: int | None,
    ) -> None:
        if kind != "backfill.progress":
            await self._storage.append_event(
                kind, payload, source=self._ctx.plugin_name, cluster_id=cluster_id, run_id=run_id
            )
        await self._ctx.emit(kind, {"run_id": run_id, "cluster_id": cluster_id, **payload})

    # ------------------------------------------------------------- M1 event hooks
    async def on_run_created(self, payload: Any) -> None:
        if not isinstance(payload, Mapping):
            return
        run_id = payload.get("run_id")
        cluster_id = payload.get("cluster_id")
        work_dir = payload.get("work_dir")
        if (
            not isinstance(run_id, int)
            or not isinstance(cluster_id, str)
            or not isinstance(work_dir, str)
        ):
            return
        solver_kind = payload.get("solver")
        stdout_path = payload.get("stdout_path")
        self._tracked[run_id] = _TrackedRun(
            run_id=run_id,
            cluster_id=cluster_id,
            work_dir=work_dir,
            solver_kind=solver_kind if isinstance(solver_kind, str) else None,
            stdout_path=stdout_path if isinstance(stdout_path, str) else None,
        )
        self._spawn_sync(self._tracked[run_id])

    async def on_job_state_changed(self, payload: Any) -> None:
        if not isinstance(payload, Mapping):
            return
        run_id = payload.get("run_id")
        if not isinstance(run_id, int):
            return
        tracked = self._tracked.get(run_id)
        if tracked is None:
            cluster_id = payload.get("cluster_id")
            work_dir = payload.get("work_dir")
            if isinstance(cluster_id, str) and isinstance(work_dir, str):
                tracked = _TrackedRun(
                    run_id=run_id,
                    cluster_id=cluster_id,
                    work_dir=work_dir,
                    solver_kind=payload.get("solver")
                    if isinstance(payload.get("solver"), str)
                    else None,
                    stdout_path=payload.get("stdout_path")
                    if isinstance(payload.get("stdout_path"), str)
                    else None,
                )
                self._tracked[run_id] = tracked
            else:
                return

        to_state = str(payload.get("to") or payload.get("state") or "").lower()
        if to_state in _TERMINAL_JOB_STATES:
            tracked.terminal = True
        self._spawn_sync(tracked)

    def _spawn_sync(self, tracked: _TrackedRun) -> None:
        async def _do_sync() -> None:
            try:
                await self._sync_tracked(tracked)
            except asyncio.CancelledError:
                raise
            except TransportError as exc:
                self._ctx.log.warning("sync skipped for run %s: %s", tracked.run_id, exc)
            except Exception:
                self._ctx.log.exception("sync failed for run %s", tracked.run_id)

        self._ctx.task(_do_sync(), name=f"sync-run-{tracked.run_id}")

    async def _sync_tracked(self, tracked: _TrackedRun) -> None:
        transport: Transport | None = self._ctx.services.get(transport_contract.SERVICE_KEY)
        if transport is None:
            return
        session = await transport.session(tracked.cluster_id)
        await self.sync_run(
            session,
            tracked.run_id,
            tracked.work_dir,
            solver_kind=tracked.solver_kind,
            stdout_path=tracked.stdout_path,
        )
        if tracked.terminal:
            self._tracked.pop(tracked.run_id, None)

    async def poll_loop(self) -> None:
        try:
            while not self._ctx.closed:
                await asyncio.sleep(self._cfg.poll_interval_s)
                for tracked in list(self._tracked.values()):
                    try:
                        await self._sync_tracked(tracked)
                    except asyncio.CancelledError:
                        raise
                    except TransportError as exc:
                        self._ctx.log.warning("poll skipped for run %s: %s", tracked.run_id, exc)
                    except Exception:
                        self._ctx.log.exception("poll failed for run %s", tracked.run_id)
        except asyncio.CancelledError:
            return


def _merge_run_state(
    run_id: int,
    solver_kind: str,
    work_dir: str,
    existing: RunSolverState | None,
    updates: list[SolverStatusUpdate],
) -> RunSolverState:
    case_dir = existing.case_dir if existing else None
    output_dir = existing.output_dir if existing else None
    solver_state = existing.solver_state if existing else SolverState.UNKNOWN
    progress = existing.progress if existing else None
    current_time = existing.current_time if existing else None
    start_time = existing.start_time if existing else None
    end_time = existing.end_time if existing else None
    ncyc = existing.ncyc if existing else None
    warning_count = existing.warning_count if existing else 0
    error_message = existing.error_message if existing else None
    metadata = dict(existing.metadata) if existing else {}

    for u in updates:
        if u.case_dir is not None:
            case_dir = u.case_dir
        if u.output_dir is not None:
            output_dir = u.output_dir
        if u.solver_state is not None:
            solver_state = u.solver_state
        if u.current_time is not None:
            current_time = u.current_time
        if u.start_time is not None:
            start_time = u.start_time
        if u.end_time is not None:
            end_time = u.end_time
        if u.ncyc is not None:
            ncyc = u.ncyc
        if u.progress is not None:
            progress = u.progress
        warning_count += u.warning_delta
        if u.error_message is not None:
            error_message = u.error_message
        if u.metadata:
            metadata.update(u.metadata)

    # Compute progress from times if solver didn't provide an explicit percentage
    if (
        progress is None
        and current_time is not None
        and start_time is not None
        and end_time is not None
        and end_time > start_time
    ):
        raw_p = (current_time - start_time) / (end_time - start_time)
        progress = min(1.0, max(0.0, raw_p))
    if solver_state is SolverState.COMPLETED:
        progress = 1.0

    return RunSolverState(
        run_id=run_id,
        solver=solver_kind,
        work_dir=work_dir,
        case_dir=case_dir,
        output_dir=output_dir,
        solver_state=solver_state,
        progress=progress,
        current_time=current_time,
        start_time=start_time,
        end_time=end_time,
        ncyc=ncyc,
        warning_count=warning_count,
        error_message=error_message,
        metadata=metadata,
    )


class SolverIngestPlugin(Plugin):
    name = "solver-ingest"
    provides = contract.SERVICE_KEY
    contract_version = contract.CONTRACT_VERSION
    inject: ClassVar[Mapping[str, int]] = {
        storage_contract.SERVICE_KEY: storage_contract.CONTRACT_VERSION,
        ts_contract.SERVICE_KEY: ts_contract.CONTRACT_VERSION,
    }
    config_schema = IngestConfig
    description = "Incremental solver log/output ingestion and background backfill."

    def apply(self, ctx: Context) -> SolverIngestor:
        assert isinstance(ctx.config, IngestConfig)
        storage: Storage = ctx.services.require(storage_contract.SERVICE_KEY)
        ts: TimeSeriesStore = ctx.services.require(ts_contract.SERVICE_KEY)
        ingestor = SolverIngestor(ctx, storage, ts, ctx.config)
        ctx.on("run.created", ingestor.on_run_created)
        ctx.on("job.state_changed", ingestor.on_job_state_changed)
        ctx.task(ingestor.poll_loop(), name="poll-loop")
        return ingestor
