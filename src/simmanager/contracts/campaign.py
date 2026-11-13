"""`campaign` and `campaign.manager` contracts: projects, Excel campaigns, cases, and runs."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from .storage import JobRecord, RunRecord
from .timeseries import RunSolverState

SERVICE_KEY = "campaign"
CONTRACT_VERSION = 1

MANAGER_SERVICE_KEY = "campaign.manager"
MANAGER_CONTRACT_VERSION = 1

DEFAULT_NAMING_REGEX = r"^(?P<case>\d+)(?:_(?P<parent>\d+))?(?:_(?P<desc>.+))?$"

EVENT_CAMPAIGN_IMPORTED = "campaign.imported"
EVENT_CASE_MATCHED = "case.matched"
EVENT_CASE_UNMATCHED = "case.unmatched"
EVENT_CASE_STATE_CHANGED = "case.state_changed"
EVENT_AMBIGUOUS_MATCH = "campaign.ambiguous_match"

DURABLE_EVENT_KINDS = frozenset(
    {
        EVENT_CAMPAIGN_IMPORTED,
        EVENT_CASE_MATCHED,
        EVENT_CASE_UNMATCHED,
        EVENT_CASE_STATE_CHANGED,
        EVENT_AMBIGUOUS_MATCH,
    }
)


class CaseComputedState(StrEnum):
    PLANNED = "planned"
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    STOPPED = "stopped"
    DIVERGED = "diverged"
    FAILED = "failed"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


class MatchKind(StrEnum):
    AUTO_WORK_DIR = "auto_work_dir"
    AUTO_DIR_NAME = "auto_dir_name"
    AUTO_REGEX = "auto_regex"
    MANUAL = "manual"


@dataclass(frozen=True)
class ProjectRecord:
    id: int
    name: str
    description: str
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class CampaignSpec:
    """Parameters used to create or update a campaign definition."""

    project_name: str
    name: str
    source_path: str | None = None
    sheet_name: str | None = None
    header_row: int = 1
    case_column: str = "# Case"
    work_dir_column: str | None = None
    manual_state_column: str | None = None
    work_root: str | None = None
    naming_regex: str | None = DEFAULT_NAMING_REGEX
    columns: tuple[str, ...] = ()
    export_monitors: tuple[str, ...] = ()


@dataclass(frozen=True)
class CampaignRecord:
    id: int
    project_id: int
    name: str
    source_path: str | None
    sheet_name: str | None
    header_row: int
    case_column: str
    work_dir_column: str | None
    manual_state_column: str | None
    work_root: str | None
    naming_regex: str | None
    columns: tuple[str, ...]
    export_monitors: tuple[str, ...]
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class CaseSpec:
    """One case row parsed from a campaign source."""

    case_key: str
    row_index: int | None = None
    parent_key_raw: str | None = None
    work_dir: str | None = None
    manual_state: str | None = None
    params: Mapping[str, str] = field(default_factory=dict)
    notes: str | None = None


@dataclass(frozen=True)
class CaseRecord:
    id: int
    campaign_id: int
    case_key: str
    row_index: int | None
    parent_case_id: int | None
    parent_key_raw: str | None
    work_dir: str | None
    manual_state: str | None
    computed_state: CaseComputedState
    params: dict[str, str]
    active: bool
    notes: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class CaseRunLink:
    id: int
    campaign_id: int
    case_id: int
    run_id: int
    match_kind: MatchKind
    match_detail: str | None
    locked: bool
    created_at: datetime


@dataclass(frozen=True)
class MatchAmbiguityRecord:
    campaign_id: int
    run_id: int
    candidate_case_ids: tuple[int, ...]
    resolved: bool
    updated_at: datetime


@dataclass(frozen=True)
class RunExecutionFacts:
    """Combined execution facts for one run used to evaluate case state and export."""

    run: RunRecord
    job: JobRecord | None = None
    solver_state: RunSolverState | None = None
    has_nan_detected: bool = False


@dataclass(frozen=True)
class CaseSummary:
    """Rich read model of a case with its linked runs, derivation, and latest execution."""

    case: CaseRecord
    links: tuple[CaseRunLink, ...]
    runs: tuple[RunExecutionFacts, ...]
    latest: RunExecutionFacts | None = None
    parent_case_key: str | None = None
    children_keys: tuple[str, ...] = ()


@dataclass(frozen=True)
class ExcelPreview:
    """Auto-detected structure of an Excel workbook presented before importing."""

    path: Path
    sheets: tuple[str, ...]
    selected_sheet: str
    header_row: int
    columns: tuple[str, ...]
    detected_case_column: str | None
    detected_work_dir_column: str | None
    detected_manual_state_column: str | None
    sample_rows: tuple[dict[str, str], ...]
    total_data_rows: int


@dataclass(frozen=True)
class ImportOptions:
    project_name: str = "default"
    campaign_name: str | None = None
    sheet_name: str | None = None
    header_row: int | None = None
    case_column: str | None = None
    work_dir_column: str | None = None
    manual_state_column: str | None = None
    work_root: str | None = None
    naming_regex: str | None = DEFAULT_NAMING_REGEX
    stop_on_blank_case: bool = True
    apply_renames: bool = False
    dry_run: bool = False


@dataclass(frozen=True)
class ImportReport:
    campaign_id: int | None
    project_name: str
    campaign_name: str
    added: tuple[str, ...] = ()
    updated: tuple[str, ...] = ()
    unchanged: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()
    renamed: tuple[tuple[str, str], ...] = ()
    """Pairs of (old_case_key, new_case_key)."""
    matched_runs: int = 0
    ambiguous_runs: int = 0
    dry_run: bool = False


@dataclass(frozen=True)
class ReconcileReport:
    campaigns_checked: int = 0
    matched: int = 0
    ambiguous: int = 0
    state_changes: int = 0


@dataclass(frozen=True)
class UnmatchedRunInfo:
    run: RunRecord
    job: JobRecord | None = None
    ambiguous_case_keys: tuple[str, ...] = ()


@dataclass(frozen=True)
class UnmatchedReport:
    campaign: CampaignRecord
    unlaunched_cases: tuple[CaseRecord, ...]
    unmatched_runs: tuple[UnmatchedRunInfo, ...]


@dataclass(frozen=True)
class ExportOptions:
    output_path: Path | None = None
    monitors: tuple[str, ...] | None = None
    include_inactive: bool = False


@dataclass(frozen=True)
class ExportReport:
    campaign_id: int
    campaign_name: str
    output_path: Path
    cases_exported: int
    used_template: bool
    columns_added: tuple[str, ...]


class CampaignStore(Protocol):
    """Durable SQLite persistence for projects, campaigns, cases, and run associations."""

    async def ensure_project(self, name: str, *, description: str = "") -> ProjectRecord: ...

    async def projects(self) -> list[ProjectRecord]: ...

    async def upsert_campaign(self, spec: CampaignSpec) -> CampaignRecord: ...

    async def campaign(
        self, name_or_id: str | int, *, project_name: str | None = None
    ) -> CampaignRecord | None: ...

    async def campaigns(self, *, project_name: str | None = None) -> list[CampaignRecord]: ...

    async def case(self, campaign_id: int, case_key_or_id: str | int) -> CaseRecord | None: ...

    async def cases(
        self, campaign_id: int, *, include_inactive: bool = False
    ) -> list[CaseRecord]: ...

    async def upsert_case(
        self,
        campaign_id: int,
        spec: CaseSpec,
        *,
        active: bool = True,
    ) -> tuple[CaseRecord, bool, bool]:
        """Insert or update a case by (campaign_id, case_key).
        Returns (record, created, changed)."""
        ...

    async def rename_case(
        self, campaign_id: int, old_case_key: str, new_case_key: str
    ) -> CaseRecord: ...

    async def set_cases_inactive(self, campaign_id: int, case_keys: Sequence[str]) -> None: ...

    async def set_case_parent(self, case_id: int, parent_case_id: int | None) -> None: ...

    async def update_case_computed_state(
        self, case_id: int, state: CaseComputedState
    ) -> CaseComputedState | None:
        """Update `computed_state` if changed. Returns previous state if changed, else None."""
        ...

    async def link_run(
        self,
        campaign_id: int,
        case_id: int,
        run_id: int,
        *,
        match_kind: MatchKind,
        match_detail: str | None = None,
        locked: bool = False,
    ) -> CaseRunLink: ...

    async def unlink_run(
        self, campaign_id: int, run_id: int, *, exclude: bool = True
    ) -> CaseRunLink | None: ...

    async def links_for_campaign(self, campaign_id: int) -> list[CaseRunLink]: ...

    async def links_for_run(self, run_id: int) -> list[CaseRunLink]: ...

    async def is_run_excluded(self, campaign_id: int, run_id: int) -> bool: ...

    async def exclusions_for_campaign(self, campaign_id: int) -> set[int]: ...

    async def record_ambiguity(
        self, campaign_id: int, run_id: int, candidate_case_ids: Sequence[int]
    ) -> bool:
        """Record or update an unresolved ambiguity.
        Returns True when newly created or when the candidate set changed."""
        ...

    async def resolve_ambiguity(self, campaign_id: int, run_id: int) -> None: ...

    async def ambiguities_for_campaign(
        self, campaign_id: int, *, unresolved_only: bool = True
    ) -> list[MatchAmbiguityRecord]: ...

    async def list_runs(
        self, *, cluster_id: str | None = None, limit: int = 1000
    ) -> list[RunRecord]: ...

    async def run_facts(self, run_ids: Sequence[int]) -> dict[int, RunExecutionFacts]: ...


class CampaignService(Protocol):
    """High-level Excel campaign import/export, matching, and state evaluation."""

    async def preview_xlsx(
        self,
        xlsx_path: Path,
        *,
        sheet_name: str | None = None,
        header_row: int | None = None,
    ) -> ExcelPreview: ...

    async def import_xlsx(
        self,
        xlsx_path: Path,
        options: ImportOptions | None = None,
    ) -> ImportReport: ...

    async def list_campaigns(self, *, project_name: str | None = None) -> list[CampaignRecord]: ...

    async def show_campaign(
        self,
        campaign: str | int,
        *,
        project_name: str | None = None,
        include_inactive: bool = False,
    ) -> tuple[CampaignRecord, list[CaseSummary]]: ...

    async def show_case(
        self,
        campaign: str | int,
        case_key: str,
        *,
        project_name: str | None = None,
    ) -> tuple[CampaignRecord, CaseSummary]: ...

    async def match_run(
        self,
        campaign: str | int,
        case_key: str,
        run_id: int,
        *,
        project_name: str | None = None,
    ) -> CaseRunLink: ...

    async def unmatch_run(
        self,
        campaign: str | int,
        run_id: int,
        *,
        project_name: str | None = None,
    ) -> CaseRunLink | None: ...

    async def unmatched(
        self,
        campaign: str | int,
        *,
        project_name: str | None = None,
    ) -> UnmatchedReport: ...

    async def reconcile_all(self, *, cluster_id: str | None = None) -> ReconcileReport: ...

    async def export_xlsx(
        self,
        campaign: str | int,
        options: ExportOptions | None = None,
        *,
        project_name: str | None = None,
    ) -> ExportReport: ...
