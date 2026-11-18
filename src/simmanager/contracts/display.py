"""`display` and `display.manager` contracts: user Python scripts run on the cluster to make
images (slices, time series, GIFs), with a versioned script library and image history.

Running a script changes the cluster (it executes user code on the login node), so every
execution goes through `RemoteSession.run_mutating` with an `ActionApproval` and is written to
the event log (ADR 0008). Images are copied into local storage, so the history survives the
remote files being deleted.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol

SERVICE_KEY = "display"
CONTRACT_VERSION = 1

MANAGER_SERVICE_KEY = "display.manager"
MANAGER_CONTRACT_VERSION = 1

UPDATED_EVENT = "display.updated"
"""Bus event: {run_id, execution_id, status} when an execution starts or ends."""

ACTION_RUN_SCRIPT = "display.run_script"
ACTION_SCHEDULE = "display.schedule"

IMAGE_TYPES: dict[str, str] = {
    ".png": "image/png",
    ".gif": "image/gif",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}
"""Image files shown by the display, by lower-case extension. No SVG: it can carry scripts."""


def image_type(path: str) -> str | None:
    """Media type of an image path, None when it is not a displayable image."""
    name = path.rsplit("/", 1)[-1]
    dot = name.rfind(".")
    return IMAGE_TYPES.get(name[dot:].lower()) if dot > 0 else None


class ExecutionStatus(StrEnum):
    RUNNING = "running"
    OK = "ok"
    FAILED = "failed"
    """The script ran and exited with a non-zero status."""
    ERROR = "error"
    """It could not run (cluster unreachable, timeout, ...)."""
    INTERRUPTED = "interrupted"
    """SimManager stopped while it was running."""


class ExecutionTrigger(StrEnum):
    MANUAL = "manual"
    AUTO = "auto"


class ImageSkip(StrEnum):
    TOO_LARGE = "too_large"
    READ_ERROR = "read_error"
    LIMIT = "limit"
    """More new images than `max_images_per_execution`: listed, not copied."""


class DisplayError(Exception):
    """A display request cannot be served (unknown script/run, run already busy, ...)."""


class ImageTooLargeError(DisplayError):
    def __init__(self, path: str, size: int, limit: int) -> None:
        super().__init__(f"{path}: {size} bytes exceeds the {limit} bytes limit")
        self.path = path
        self.size = size
        self.limit = limit


# ------------------------------------------------------------------------- records
@dataclass(frozen=True)
class ScriptVersion:
    id: int
    script_id: int
    number: int
    """1, 2, 3, ... per script."""
    sha256: str
    body: str
    setup: str
    """Shell lines run before the interpreter (e.g. `module load python`)."""
    interpreter: str
    message: str
    created_at: datetime


@dataclass(frozen=True)
class ScriptRecord:
    id: int
    name: str
    created_at: datetime
    latest: ScriptVersion


@dataclass(frozen=True)
class ScriptDraft:
    name: str
    body: str
    setup: str = ""
    interpreter: str = "python3"
    message: str = ""


@dataclass(frozen=True)
class FoundImage:
    """An image the script wrote, as listed on the cluster after it ran."""

    path: str
    """Relative to the run folder."""
    size: int
    mtime: float


@dataclass(frozen=True)
class ImageRecord:
    id: int
    execution_id: int
    path: str
    """Relative to the run folder."""
    size: int
    mtime: float
    media_type: str
    sha256: str | None
    """None when the bytes were not copied (see `skipped`)."""
    skipped: ImageSkip | None
    detail: str = ""


@dataclass(frozen=True)
class ExecutionRecord:
    id: int
    run_id: int
    script_id: int
    script_name: str
    version_id: int
    version_number: int
    trigger: ExecutionTrigger
    status: ExecutionStatus
    started_at: datetime
    finished_at: datetime | None
    exit_status: int | None
    stdout_tail: str
    stderr_tail: str
    error: str
    images: tuple[ImageRecord, ...] = ()


@dataclass(frozen=True)
class ScheduleRecord:
    run_id: int
    script_id: int
    interval_s: int
    enabled: bool
    approved_at: datetime
    last_run_at: datetime | None


@dataclass(frozen=True)
class RemoteEntry:
    name: str
    path: str
    """Relative to the run folder ("" = the folder itself)."""
    is_dir: bool
    size: int
    mtime: float
    media_type: str | None


# --------------------------------------------------------------------------- store
class DisplayStore(Protocol):
    async def save_script(self, draft: ScriptDraft) -> tuple[ScriptVersion, bool]:
        """New version when the content changed (created=True), else the latest one."""
        ...

    async def scripts(self) -> list[ScriptRecord]: ...

    async def script(self, script_id: int) -> ScriptRecord | None: ...

    async def versions(self, script_id: int) -> list[ScriptVersion]:
        """Newest first."""
        ...

    async def version(self, version_id: int) -> ScriptVersion | None: ...

    async def start_execution(
        self, run_id: int, version_id: int, trigger: ExecutionTrigger
    ) -> ExecutionRecord: ...

    async def finish_execution(
        self,
        execution_id: int,
        *,
        status: ExecutionStatus,
        exit_status: int | None,
        stdout_tail: str,
        stderr_tail: str,
        error: str,
    ) -> None: ...

    async def interrupt_running(self) -> int:
        """Mark executions left `running` by a previous process as interrupted."""
        ...

    async def add_image(
        self,
        execution_id: int,
        found: FoundImage,
        media_type: str,
        data: bytes | None,
        skipped: ImageSkip | None,
        detail: str = "",
    ) -> ImageRecord: ...

    async def attach_image_data(self, image_id: int, data: bytes) -> ImageRecord: ...

    async def executions(self, run_id: int, *, limit: int = 50) -> list[ExecutionRecord]:
        """Newest first, with their images."""
        ...

    async def execution(self, execution_id: int) -> ExecutionRecord | None: ...

    async def image(self, image_id: int) -> ImageRecord | None: ...

    async def image_data(self, image_id: int) -> bytes | None: ...

    async def set_schedule(
        self, run_id: int, script_id: int, *, interval_s: int, enabled: bool
    ) -> ScheduleRecord: ...

    async def schedules(self, run_id: int | None = None) -> list[ScheduleRecord]: ...

    async def mark_scheduled_run(self, run_id: int, script_id: int, at: datetime) -> None: ...


# ------------------------------------------------------------------------- service
class DisplayService(Protocol):
    @property
    def max_image_bytes(self) -> int: ...

    async def save_script(self, draft: ScriptDraft) -> tuple[ScriptVersion, bool]: ...

    async def scripts(self) -> list[ScriptRecord]: ...

    async def versions(self, script_id: int) -> list[ScriptVersion]: ...

    async def version(self, version_id: int) -> ScriptVersion | None: ...

    async def diff(self, old_version_id: int, new_version_id: int) -> str:
        """Unified diff (setup, interpreter and body) between two versions."""
        ...

    async def start(
        self,
        run_id: int,
        script_id: int,
        *,
        trigger: ExecutionTrigger,
        approved_by: str,
    ) -> ExecutionRecord:
        """Start the latest version of a script on the run folder in the background.

        Raises DisplayError when the run/script is unknown or the run is already busy."""
        ...

    def busy(self, run_id: int) -> bool: ...

    async def executions(self, run_id: int, *, limit: int = 50) -> list[ExecutionRecord]: ...

    async def image_data(self, image_id: int) -> tuple[ImageRecord, bytes] | None: ...

    async def fetch_skipped(self, image_id: int) -> ImageRecord:
        """Copy an image skipped as too large, ignoring the size limit (explicit request)."""
        ...

    async def browse(self, run_id: int, path: str) -> list[RemoteEntry]:
        """List a folder below the run folder (read-only)."""
        ...

    async def read_image(self, run_id: int, path: str, *, force: bool) -> tuple[str, bytes]:
        """(media type, bytes) of an image below the run folder. ImageTooLargeError above
        the limit unless `force`."""
        ...

    async def set_schedule(
        self,
        run_id: int,
        script_id: int,
        *,
        interval_s: int,
        enabled: bool,
        approved_by: str,
    ) -> ScheduleRecord: ...

    async def schedules(self, run_id: int) -> list[ScheduleRecord]: ...
