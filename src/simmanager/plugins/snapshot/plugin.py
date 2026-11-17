"""`snapshot` plugin: captures remote inputs, computes semantic/textual diffs, stores notes."""

from __future__ import annotations

import asyncio
import posixpath
from collections.abc import Mapping
from datetime import datetime
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field

from simmanager.contracts import campaign as campaign_contract
from simmanager.contracts import monitor as monitor_contract
from simmanager.contracts import snapshot as contract
from simmanager.contracts import storage as storage_contract
from simmanager.contracts import transport as transport_contract
from simmanager.contracts.campaign import CampaignStore
from simmanager.contracts.monitor import PollReport
from simmanager.contracts.scheduler import JobState
from simmanager.contracts.snapshot import (
    ContentStatus,
    DiffSummary,
    RunNoteRecord,
    SnapshotBlob,
    SnapshotDiff,
    SnapshotFileSpec,
    SnapshotRecord,
    SnapshotResult,
    SnapshotStatus,
    SnapshotStore,
    SnapshotTrigger,
)
from simmanager.contracts.storage import EventRecord, Storage
from simmanager.contracts.transport import RemoteSession, Transport, TransportError
from simmanager.kernel import Context, Plugin

from .collector import (
    CollectorConfig,
    PendingHash,
    check_late_files,
    compute_manifest_sha256,
    detect_solver_kind,
    discover_input_files,
    read_and_prepare_file,
    stream_sha256_remote,
)
from .diff import compute_snapshot_diff, summarize_diff


class SnapshotConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_file_bytes: int = Field(default=1_048_576, ge=1024)
    """Files larger than this cap are not copied as blobs (metadata + sha256 only)."""
    hash_large_files: bool = True
    """Compute SHA-256 in the background via SFTP for oversize files up to `hash_max_bytes`."""
    hash_max_bytes: int = Field(default=104_857_600, ge=1024)
    read_chunk_bytes: int = Field(default=512 * 1024, ge=4096)
    converge_globs: list[str] = Field(default_factory=lambda: ["*.in", "*.dat"])
    openfoam_roots: list[str] = Field(default_factory=lambda: ["system", "constant", "0", "0.orig"])
    openfoam_exclude_dirs: list[str] = Field(
        default_factory=lambda: ["polyMesh", "triSurface", "*Mesh*"]
    )
    auto_capture: bool = True


def _norm_dir(path: str | None) -> str | None:
    if not path:
        return None
    norm = posixpath.normpath(path.strip().replace("\\", "/"))
    return norm.rstrip("/") if len(norm) > 1 else norm


class SnapshotManager:
    """Implements `simmanager.contracts.snapshot.SnapshotService`."""

    def __init__(
        self,
        ctx: Context,
        storage: Storage,
        store: SnapshotStore,
        cfg: SnapshotConfig,
    ) -> None:
        self._ctx = ctx
        self._storage = storage
        self._store = store
        self._cfg = cfg
        self._collector_cfg = CollectorConfig(
            max_file_bytes=cfg.max_file_bytes,
            hash_large_files=cfg.hash_large_files,
            hash_max_bytes=cfg.hash_max_bytes,
            read_chunk_bytes=cfg.read_chunk_bytes,
            converge_globs=tuple(cfg.converge_globs),
            openfoam_roots=tuple(cfg.openfoam_roots),
            openfoam_exclude_dirs=tuple(cfg.openfoam_exclude_dirs),
        )
        self._run_locks: dict[int, asyncio.Lock] = {}
        self._hash_tasks: set[asyncio.Task[None]] = set()
        self._scheduled_discovered: set[int] = set()
        self._scheduled_running: set[int] = set()

    def _run_lock(self, run_id: int) -> asyncio.Lock:
        lock = self._run_locks.get(run_id)
        if lock is None:
            lock = asyncio.Lock()
            self._run_locks[run_id] = lock
        return lock

    async def capture_run(
        self,
        session: RemoteSession,
        run_id: int,
        work_dir: str,
        *,
        trigger: SnapshotTrigger = SnapshotTrigger.MANUAL,
        solver_kind: str | None = None,
        job_start_time: datetime | None = None,
        effective: bool = True,
        wait_for_hash: bool = False,
    ) -> SnapshotResult:
        async with self._run_lock(run_id):
            norm_work_dir = _norm_dir(work_dir) or work_dir
            if job_start_time is None:
                job = await self._storage.job_for_run(run_id)
                if job is not None:
                    job_start_time = job.info.start_time

            try:
                solver = await detect_solver_kind(session, norm_work_dir, hint=solver_kind)
                inputs = await discover_input_files(
                    session, norm_work_dir, solver, self._collector_cfg
                )
            except (OSError, TransportError) as exc:
                snap = await self._store.save_snapshot(
                    run_id=run_id,
                    trigger=trigger,
                    effective=False,
                    status=SnapshotStatus.FAILED,
                    late=False,
                    late_files=(),
                    solver=solver_kind,
                    work_dir=norm_work_dir,
                    manifest_sha256="",
                    error_message=str(exc),
                    files=(),
                    blobs={},
                )
                await self._storage.append_event(
                    contract.EVENT_SNAPSHOT_FAILED,
                    {
                        "snapshot_id": snap.id,
                        "work_dir": norm_work_dir,
                        "trigger": str(trigger),
                        "error": str(exc),
                    },
                    source=self._ctx.plugin_name,
                    cluster_id=session.cluster_id,
                    run_id=run_id,
                )
                return SnapshotResult(snapshot=snap)

            late, late_files = check_late_files(inputs, job_start_time)
            specs: list[SnapshotFileSpec] = []
            blobs: dict[str, tuple[bytes, bool, str]] = {}
            pending_hashes: list[PendingHash] = []
            unreadable_count = 0

            for item in inputs:
                reused_sha: str | None = None
                if item.stat.size > self._collector_cfg.max_file_bytes:
                    reused_sha = await self._store.find_reusable_sha256(
                        norm_work_dir, item.rel_path, item.stat.size, item.stat.mtime
                    )
                spec, blob_entry, pending = await read_and_prepare_file(
                    session, item, self._collector_cfg, reused_sha256=reused_sha
                )
                specs.append(spec)
                if spec.content_status is ContentStatus.UNREADABLE:
                    unreadable_count += 1
                if blob_entry is not None and spec.sha256 is not None:
                    blobs[spec.sha256] = blob_entry
                if pending is not None:
                    pending_hashes.append(pending)

            status = SnapshotStatus.PARTIAL if unreadable_count else SnapshotStatus.OK
            manifest_sha = compute_manifest_sha256(specs)
            err_msg = f"{unreadable_count} unreadable file(s)" if unreadable_count else None
            snap = await self._store.save_snapshot(
                run_id=run_id,
                trigger=trigger,
                effective=effective,
                status=status,
                late=late,
                late_files=late_files,
                solver=solver,
                work_dir=norm_work_dir,
                manifest_sha256=manifest_sha,
                error_message=err_msg,
                files=specs,
                blobs=blobs,
            )

            diff_summary = await self._compute_capture_summary(snap)
            await self._storage.append_event(
                contract.EVENT_SNAPSHOT_TAKEN,
                {
                    "snapshot_id": snap.id,
                    "trigger": str(trigger),
                    "effective": snap.effective,
                    "status": str(snap.status),
                    "late": snap.late,
                    "late_files": list(snap.late_files),
                    "solver": snap.solver,
                    "file_count": snap.file_count,
                    "total_bytes": snap.total_bytes,
                    "diff_summary": {
                        "base_snapshot_id": diff_summary.base_snapshot_id,
                        "base_run_id": diff_summary.base_run_id,
                        "base_reason": diff_summary.base_reason,
                        "added": diff_summary.added,
                        "removed": diff_summary.removed,
                        "modified": diff_summary.modified,
                        "oversize_changed": diff_summary.oversize_changed,
                        "semantic_changes": diff_summary.semantic_changes,
                    },
                },
                source=self._ctx.plugin_name,
                cluster_id=session.cluster_id,
                run_id=run_id,
            )

            if pending_hashes:
                if wait_for_hash:
                    await self._hash_pending_files(
                        session.cluster_id, snap.id, pending_hashes, session=session
                    )
                    reloaded = await self._store.get_snapshot(snap.id, include_files=True)
                    if reloaded is not None:
                        snap = reloaded
                else:
                    self._spawn_hash_task(session.cluster_id, snap.id, pending_hashes, session)

            return SnapshotResult(
                snapshot=snap,
                diff_summary=diff_summary,
                hashing_pending=0 if wait_for_hash else len(pending_hashes),
            )

    def _spawn_hash_task(
        self,
        cluster_id: str,
        snapshot_id: int,
        pending: list[PendingHash],
        session: RemoteSession | None,
    ) -> None:
        async def _runner() -> None:
            await self._hash_pending_files(cluster_id, snapshot_id, pending, session=session)

        task = self._ctx.task(_runner(), name=f"snapshot-hash-{snapshot_id}")
        self._hash_tasks.add(task)
        task.add_done_callback(self._hash_tasks.discard)

    async def _hash_pending_files(
        self,
        cluster_id: str,
        snapshot_id: int,
        pending: list[PendingHash],
        *,
        session: RemoteSession | None = None,
    ) -> None:
        active_session = session
        if active_session is None:
            transport: Transport | None = self._ctx.services.get(transport_contract.SERVICE_KEY)
            if transport is None:
                return
            try:
                active_session = await transport.session(cluster_id)
            except TransportError as exc:
                self._ctx.log.warning(
                    "oversize hashing skipped for snapshot %s: %s", snapshot_id, exc
                )
                return

        for item in pending:
            try:
                st = await active_session.stat(item.remote_path)
                if st is None or st.is_dir:
                    continue
                if st.size != item.size_bytes or abs(st.mtime - item.mtime) >= 1e-6:
                    continue
                sha = await stream_sha256_remote(
                    active_session,
                    item.remote_path,
                    item.size_bytes,
                    self._collector_cfg.read_chunk_bytes,
                )
                st_after = await active_session.stat(item.remote_path)
                if (
                    st_after is not None
                    and st_after.size == item.size_bytes
                    and abs(st_after.mtime - item.mtime) < 1e-6
                ):
                    await self._store.update_file_hash(snapshot_id, item.rel_path, sha)
            except asyncio.CancelledError:
                raise
            except (OSError, TransportError) as exc:
                self._ctx.log.warning(
                    "oversize hash failed for snapshot %s (%s): %s",
                    snapshot_id,
                    item.rel_path,
                    exc,
                )

    async def _compute_capture_summary(self, snap: SnapshotRecord) -> DiffSummary:
        base, reason = await self._resolve_base_snapshot(snap.run_id, exclude_snapshot_id=snap.id)
        if base is None:
            return DiffSummary(base_reason="none")
        diff = await self._diff_records(base, snap, base_reason=reason)
        return summarize_diff(diff)

    async def list_snapshots(self, run_id: int) -> list[SnapshotRecord]:
        return await self._store.list_snapshots(run_id, include_files=False)

    async def get_snapshot(
        self, snapshot_id: int, *, include_files: bool = True
    ) -> SnapshotRecord | None:
        return await self._store.get_snapshot(snapshot_id, include_files=include_files)

    async def get_file_text(self, snapshot_id: int, rel_path: str) -> str | None:
        snap = await self._store.get_snapshot(snapshot_id, include_files=True)
        if snap is None:
            return None
        for f in snap.files:
            if f.rel_path == rel_path and f.sha256 is not None:
                blob = await self._store.get_blob(f.sha256)
                if blob is None or not blob.is_text:
                    return None
                return blob.data.decode(blob.encoding, errors="replace")
        return None

    async def diff_runs(
        self,
        run_id: int,
        *,
        against_run_id: int | None = None,
        parent: bool = False,
    ) -> SnapshotDiff:
        target = await self._store.effective_snapshot(run_id, include_files=True)
        if target is None:
            raise LookupError(f"no snapshot found for run {run_id}")

        if against_run_id is not None:
            base = await self._store.effective_snapshot(against_run_id, include_files=True)
            if base is None:
                raise LookupError(f"no snapshot found for base run {against_run_id}")
            return await self._diff_records(base, target, base_reason="explicit")

        if parent:
            base = await self._resolve_parent_case_snapshot(run_id)
            if base is None:
                raise LookupError(f"no parent case snapshot found for run {run_id}")
            return await self._diff_records(base, target, base_reason="parent_case")

        base, reason = await self._resolve_base_snapshot(run_id)
        return await self._diff_records(base, target, base_reason=reason)

    async def diff_snapshots(
        self,
        base_snapshot_id: int | None,
        target_snapshot_id: int,
        *,
        base_reason: str = "explicit",
    ) -> SnapshotDiff:
        target = await self._store.get_snapshot(target_snapshot_id, include_files=True)
        if target is None:
            raise LookupError(f"snapshot {target_snapshot_id} not found")
        base: SnapshotRecord | None = None
        if base_snapshot_id is not None:
            base = await self._store.get_snapshot(base_snapshot_id, include_files=True)
            if base is None:
                raise LookupError(f"snapshot {base_snapshot_id} not found")
        return await self._diff_records(base, target, base_reason=base_reason)

    async def _diff_records(
        self,
        base: SnapshotRecord | None,
        target: SnapshotRecord,
        *,
        base_reason: str,
    ) -> SnapshotDiff:
        needed_shas: set[str] = set()
        if base is not None:
            needed_shas.update(
                f.sha256
                for f in base.files
                if f.sha256 is not None and f.content_status is ContentStatus.STORED
            )
        needed_shas.update(
            f.sha256
            for f in target.files
            if f.sha256 is not None and f.content_status is ContentStatus.STORED
        )
        blobs: dict[str, SnapshotBlob] = {}
        for sha in needed_shas:
            blob = await self._store.get_blob(sha)
            if blob is not None:
                blobs[sha] = blob
        return compute_snapshot_diff(base, target, blobs, base_reason=base_reason)

    async def _resolve_parent_case_snapshot(self, run_id: int) -> SnapshotRecord | None:
        campaign_store: CampaignStore | None = self._ctx.services.get(campaign_contract.SERVICE_KEY)
        if campaign_store is None:
            return None
        links = await campaign_store.links_for_run(run_id)
        for link in links:
            case = await campaign_store.case(link.campaign_id, link.case_id)
            if case is None or case.parent_case_id is None:
                continue
            camp_links = await campaign_store.links_for_campaign(link.campaign_id)
            parent_run_ids = sorted(
                {cl.run_id for cl in camp_links if cl.case_id == case.parent_case_id},
                reverse=True,
            )
            for pid in parent_run_ids:
                snap = await self._store.effective_snapshot(pid, include_files=True)
                if snap is not None:
                    return snap
        return None

    async def _resolve_base_snapshot(
        self, run_id: int, *, exclude_snapshot_id: int | None = None
    ) -> tuple[SnapshotRecord | None, str]:
        # 1. Previous run of the same campaign case
        campaign_store: CampaignStore | None = self._ctx.services.get(campaign_contract.SERVICE_KEY)
        if campaign_store is not None:
            links = await campaign_store.links_for_run(run_id)
            for link in links:
                camp_links = await campaign_store.links_for_campaign(link.campaign_id)
                peer_runs = sorted(
                    {
                        cl.run_id
                        for cl in camp_links
                        if cl.case_id == link.case_id and cl.run_id < run_id
                    },
                    reverse=True,
                )
                for peer_id in peer_runs:
                    snap = await self._store.effective_snapshot(peer_id, include_files=True)
                    if snap is not None and snap.id != exclude_snapshot_id:
                        return snap, "previous_case_run"

        # 2. Previous run in the same work_dir
        target_run = await self._storage.run(run_id)
        target_wdir = _norm_dir(target_run.work_dir) if target_run else None
        if target_run is not None and target_wdir is not None:
            all_runs = await self._storage.runs(cluster_id=target_run.cluster_id, limit=500)
            for prev in all_runs:
                if prev.id >= run_id:
                    continue
                if _norm_dir(prev.work_dir) != target_wdir:
                    continue
                snap = await self._store.effective_snapshot(prev.id, include_files=True)
                if snap is not None and snap.id != exclude_snapshot_id:
                    return snap, "previous_workdir_run"

        return None, "none"

    async def get_note(self, run_id: int) -> RunNoteRecord | None:
        return await self._store.get_note(run_id)

    async def set_note(self, run_id: int, note: str) -> RunNoteRecord:
        record = await self._store.set_note(run_id, note)
        run = await self._storage.run(run_id)
        await self._storage.append_event(
            contract.EVENT_RUN_NOTE_CHANGED,
            {
                "run_id": run_id,
                "length": len(note),
                "preview": note[:120],
            },
            source=self._ctx.plugin_name,
            cluster_id=run.cluster_id if run is not None else None,
            run_id=run_id,
        )
        return record

    # ------------------------------------------------------------------ hooks
    async def on_storage_event(self, payload: Any) -> None:
        if not self._cfg.auto_capture or not isinstance(payload, EventRecord):
            return
        if payload.run_id is None:
            return
        run_id = payload.run_id
        if payload.kind in ("job.discovered", "run.created"):
            if run_id in self._scheduled_discovered:
                return
            self._scheduled_discovered.add(run_id)
            existing = await self._store.list_snapshots(run_id, include_files=False)
            if existing:
                return
            self._spawn_auto_capture(
                run_id,
                cluster_id=payload.cluster_id,
                work_dir=payload.payload.get("work_dir"),
                trigger=SnapshotTrigger.DISCOVERED,
            )
            return

        if (
            payload.kind == "job.state_changed"
            and payload.payload.get("to") == str(JobState.RUNNING)
            and payload.payload.get("from") != str(JobState.RUNNING)
            and run_id not in self._scheduled_running
        ):
            self._scheduled_running.add(run_id)
            self._spawn_auto_capture(
                run_id,
                cluster_id=payload.cluster_id,
                work_dir=None,
                trigger=SnapshotTrigger.RUNNING,
            )

    async def on_monitor_polled(self, payload: Any) -> None:
        if not self._cfg.auto_capture:
            return
        if isinstance(payload, PollReport):
            if not payload.ok:
                return
            cluster_id = payload.cluster_id
        elif isinstance(payload, Mapping):
            if not payload.get("ok"):
                return
            cid = payload.get("cluster_id")
            if not isinstance(cid, str):
                return
            cluster_id = cid
        else:
            return

        jobs = await self._storage.jobs(cluster_id=cluster_id, active=True, limit=200)
        for job in jobs:
            if job.run_id is None or not job.info.work_dir:
                continue
            if job.run_id in self._scheduled_discovered:
                continue
            existing = await self._store.list_snapshots(job.run_id, include_files=False)
            if existing:
                self._scheduled_discovered.add(job.run_id)
                continue
            self._scheduled_discovered.add(job.run_id)
            self._spawn_auto_capture(
                job.run_id,
                cluster_id=cluster_id,
                work_dir=job.info.work_dir,
                trigger=SnapshotTrigger.DISCOVERED,
                job_start_time=job.info.start_time,
            )

    def _spawn_auto_capture(
        self,
        run_id: int,
        *,
        cluster_id: str | None,
        work_dir: str | None,
        trigger: SnapshotTrigger,
        job_start_time: datetime | None = None,
    ) -> None:
        async def _runner() -> None:
            try:
                transport: Transport | None = self._ctx.services.get(transport_contract.SERVICE_KEY)
                if transport is None:
                    return
                run = await self._storage.run(run_id)
                cid = cluster_id or (run.cluster_id if run else None)
                wdir = work_dir or (run.work_dir if run else None)
                job = await self._storage.job_for_run(run_id)
                if job is not None and not wdir:
                    wdir = job.info.work_dir
                if cid is None or not wdir:
                    return
                if cid not in transport.clusters():
                    return
                session = await transport.session(cid)
                start_t = job_start_time or (job.info.start_time if job else None)
                await self.capture_run(
                    session,
                    run_id,
                    wdir,
                    trigger=trigger,
                    job_start_time=start_t,
                    effective=True,
                )
            except asyncio.CancelledError:
                raise
            except TransportError as exc:
                self._ctx.log.warning("snapshot skipped for run %s: %s", run_id, exc)
            except Exception:
                self._ctx.log.exception("snapshot failed for run %s", run_id)

        self._ctx.task(_runner(), name=f"snapshot-capture-{run_id}-{trigger}")


class SnapshotPlugin(Plugin):
    name = "snapshot"
    provides = contract.MANAGER_SERVICE_KEY
    contract_version = contract.MANAGER_CONTRACT_VERSION
    inject: ClassVar[Mapping[str, int]] = {
        storage_contract.SERVICE_KEY: storage_contract.CONTRACT_VERSION,
        contract.SERVICE_KEY: contract.CONTRACT_VERSION,
    }
    config_schema = SnapshotConfig
    description = "Input snapshot capture, large-file hashing, semantic diffs, and run notes."

    def apply(self, ctx: Context) -> SnapshotManager:
        assert isinstance(ctx.config, SnapshotConfig)
        storage: Storage = ctx.services.require(storage_contract.SERVICE_KEY)
        store: SnapshotStore = ctx.services.require(contract.SERVICE_KEY)
        mgr = SnapshotManager(ctx, storage, store, ctx.config)
        ctx.on(storage_contract.EVENT_APPENDED, mgr.on_storage_event)
        ctx.on(monitor_contract.POLLED_EVENT, mgr.on_monitor_polled)
        return mgr
