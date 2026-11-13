"""`snapshot` and `snapshot.manager` contracts: content-addressed input snapshots, diffs, notes."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from .transport import RemoteSession

SERVICE_KEY = "snapshot"
CONTRACT_VERSION = 1

MANAGER_SERVICE_KEY = "snapshot.manager"
MANAGER_CONTRACT_VERSION = 1

EVENT_SNAPSHOT_TAKEN = "snapshot.taken"
EVENT_SNAPSHOT_FAILED = "snapshot.failed"
EVENT_RUN_NOTE_CHANGED = "run.note_changed"

DURABLE_EVENT_KINDS = frozenset(
    {
        EVENT_SNAPSHOT_TAKEN,
        EVENT_SNAPSHOT_FAILED,
        EVENT_RUN_NOTE_CHANGED,
    }
)


class SnapshotTrigger(StrEnum):
    DISCOVERED = "discovered"
    RUNNING = "running"
    MANUAL = "manual"


class SnapshotStatus(StrEnum):
    OK = "ok"
    PARTIAL = "partial"
    FAILED = "failed"


class ContentStatus(StrEnum):
    STORED = "stored"
    OVERSIZE_HASHED = "oversize_hashed"
    OVERSIZE_UNHASHED = "oversize_unhashed"
    UNREADABLE = "unreadable"


class DiffChangeKind(StrEnum):
    ADDED = "added"
    REMOVED = "removed"
    MODIFIED = "modified"
    OVERSIZE_CHANGED = "oversize_changed"
    OVERSIZE_UNCHANGED = "oversize_unchanged"
    UNREADABLE = "unreadable"


@dataclass(frozen=True)
class SnapshotFileSpec:
    """File entry produced during snapshot capture before persistence."""

    rel_path: str
    size_bytes: int
    mtime: float
    sha256: str | None = None
    content_status: ContentStatus = ContentStatus.STORED
    is_text: bool = True
    encoding: str = "utf-8"


@dataclass(frozen=True)
class SnapshotFileRecord:
    """Stored manifest entry for one file inside a snapshot."""

    id: int
    snapshot_id: int
    rel_path: str
    size_bytes: int
    mtime: float
    sha256: str | None
    content_status: ContentStatus


@dataclass(frozen=True)
class SnapshotBlob:
    """Decompressed content-addressed blob retrieved from storage."""

    sha256: str
    size_bytes: int
    is_text: bool
    encoding: str
    data: bytes


@dataclass(frozen=True)
class SnapshotRecord:
    """Durable input snapshot metadata and optional file manifest."""

    id: int
    run_id: int
    trigger: SnapshotTrigger
    effective: bool
    status: SnapshotStatus
    late: bool
    late_files: tuple[str, ...]
    solver: str | None
    work_dir: str
    manifest_sha256: str
    file_count: int
    total_bytes: int
    error_message: str | None
    captured_at: datetime
    files: tuple[SnapshotFileRecord, ...] = ()


@dataclass(frozen=True)
class RunNoteRecord:
    """Mutable free-text annotation attached to a run."""

    run_id: int
    note: str
    updated_at: datetime


@dataclass(frozen=True)
class SemanticChange:
    """One semantic parameter difference (CONVERGE YAML or OpenFOAM dictionary)."""

    path: str
    old_value: str | None
    new_value: str | None
    doc_index: int | None = None


@dataclass(frozen=True)
class FileDiff:
    """Difference for a single relative path between two snapshots."""

    rel_path: str
    change_kind: DiffChangeKind
    base_size: int | None = None
    target_size: int | None = None
    base_sha256: str | None = None
    target_sha256: str | None = None
    semantic_changes: tuple[SemanticChange, ...] = ()
    unified_diff: str | None = None
    note: str | None = None


@dataclass(frozen=True)
class SnapshotDiff:
    """Complete comparison between a base snapshot and a target snapshot."""

    base_snapshot: SnapshotRecord | None
    target_snapshot: SnapshotRecord
    base_reason: str
    files: tuple[FileDiff, ...] = ()
    target_late: bool = False
    target_late_files: tuple[str, ...] = ()
    base_late: bool = False

    @property
    def has_changes(self) -> bool:
        return any(f.change_kind is not DiffChangeKind.OVERSIZE_UNCHANGED for f in self.files)


@dataclass(frozen=True)
class DiffSummary:
    """Compact summary embedded in `snapshot.taken` durable event payloads."""

    base_snapshot_id: int | None = None
    base_run_id: int | None = None
    base_reason: str = "none"
    added: int = 0
    removed: int = 0
    modified: int = 0
    oversize_changed: int = 0
    semantic_changes: int = 0


@dataclass(frozen=True)
class SnapshotResult:
    """Outcome of a capture operation."""

    snapshot: SnapshotRecord
    diff_summary: DiffSummary = field(default_factory=DiffSummary)
    hashing_pending: int = 0


class SnapshotStore(Protocol):
    """Durable SQLite persistence for content-addressed snapshots and run notes."""

    async def save_snapshot(
        self,
        *,
        run_id: int,
        trigger: SnapshotTrigger,
        effective: bool,
        status: SnapshotStatus,
        late: bool,
        late_files: Sequence[str],
        solver: str | None,
        work_dir: str,
        manifest_sha256: str,
        error_message: str | None,
        files: Sequence[SnapshotFileSpec],
        blobs: Mapping[str, tuple[bytes, bool, str]],
    ) -> SnapshotRecord:
        """Persist a snapshot, deduplicating blobs by sha256.

        When `effective` is True, any prior effective snapshot of the same `run_id` is marked
        non-effective in the same transaction.
        """
        ...

    async def get_snapshot(
        self, snapshot_id: int, *, include_files: bool = True
    ) -> SnapshotRecord | None: ...

    async def list_snapshots(
        self, run_id: int, *, include_files: bool = False
    ) -> list[SnapshotRecord]:
        """Snapshots for `run_id`, oldest first."""
        ...

    async def effective_snapshot(
        self, run_id: int, *, include_files: bool = True
    ) -> SnapshotRecord | None:
        """Effective snapshot for `run_id` (falling back to latest if none marked effective)."""
        ...

    async def find_reusable_sha256(
        self, work_dir: str, rel_path: str, size_bytes: int, mtime: float
    ) -> str | None:
        """Return an already-known sha256 matching `(work_dir, rel_path, size_bytes, mtime)`."""
        ...

    async def update_file_hash(
        self,
        snapshot_id: int,
        rel_path: str,
        sha256: str,
        *,
        content_status: ContentStatus = ContentStatus.OVERSIZE_HASHED,
    ) -> bool: ...

    async def get_blob(self, sha256: str) -> SnapshotBlob | None: ...

    async def get_note(self, run_id: int) -> RunNoteRecord | None: ...

    async def set_note(self, run_id: int, note: str) -> RunNoteRecord: ...


class SnapshotService(Protocol):
    """High-level input snapshot capture, semantic/textual diffing, and run notes."""

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
    ) -> SnapshotResult: ...

    async def list_snapshots(self, run_id: int) -> list[SnapshotRecord]: ...

    async def get_snapshot(
        self, snapshot_id: int, *, include_files: bool = True
    ) -> SnapshotRecord | None: ...

    async def get_file_text(self, snapshot_id: int, rel_path: str) -> str | None: ...

    async def diff_runs(
        self,
        run_id: int,
        *,
        against_run_id: int | None = None,
        parent: bool = False,
    ) -> SnapshotDiff: ...

    async def diff_snapshots(
        self,
        base_snapshot_id: int | None,
        target_snapshot_id: int,
        *,
        base_reason: str = "explicit",
    ) -> SnapshotDiff: ...

    async def get_note(self, run_id: int) -> RunNoteRecord | None: ...

    async def set_note(self, run_id: int, note: str) -> RunNoteRecord: ...
