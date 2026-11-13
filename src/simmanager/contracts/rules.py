"""`rules` and `rules.manager` contracts: multi-level detection rules, hysteresis, alerts."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol

SERVICE_KEY = "rules"
CONTRACT_VERSION = 1

MANAGER_SERVICE_KEY = "rules.manager"
MANAGER_CONTRACT_VERSION = 1

EVENT_ALERT_RAISED = "alert.raised"
EVENT_ALERT_CLEARED = "alert.cleared"
EVENT_ALERT_ACKNOWLEDGED = "alert.acknowledged"

DURABLE_EVENT_KINDS = frozenset(
    {
        EVENT_ALERT_RAISED,
        EVENT_ALERT_CLEARED,
        EVENT_ALERT_ACKNOWLEDGED,
    }
)


class RuleSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class RuleKind(StrEnum):
    NAN_INF = "nan_inf"
    RESIDUAL_HIGH = "residual_high"
    MONITOR_RANGE = "monitor_range"
    COURANT_HIGH = "courant_high"
    STALL = "stall"
    JOB_FAILED = "job_failed"
    SOLVER_ERROR = "solver_error"


class RuleScope(StrEnum):
    GLOBAL = "global"
    CAMPAIGN = "campaign"
    CASE = "case"


class AlertStatus(StrEnum):
    OPEN = "open"
    CLEARED = "cleared"


@dataclass(frozen=True)
class RuleSpec:
    """Definition/override of a detection rule at a given scope."""

    scope_type: RuleScope
    scope_id: int
    rule_key: str
    rule_kind: RuleKind
    enabled: bool = True
    severity: RuleSeverity = RuleSeverity.WARNING
    params: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RuleConfigRecord:
    """Stored rule configuration entry."""

    id: int
    scope_type: RuleScope
    scope_id: int
    rule_key: str
    rule_kind: RuleKind
    enabled: bool
    severity: RuleSeverity
    params: dict[str, Any]
    updated_at: datetime


@dataclass(frozen=True)
class EffectiveRule:
    """Resolved rule after merging global -> campaign -> case layers."""

    rule_key: str
    rule_kind: RuleKind
    enabled: bool
    severity: RuleSeverity
    params: dict[str, Any]
    origin_scope: RuleScope
    origin_id: int = 0


@dataclass(frozen=True)
class AlertRecord:
    """Durable alert instance with hysteresis and acknowledgment state."""

    id: int
    dedup_key: str
    rule_key: str
    rule_kind: RuleKind
    severity: RuleSeverity
    status: AlertStatus
    cluster_id: str | None
    run_id: int | None
    campaign_id: int | None
    case_id: int | None
    title: str
    message: str
    details: dict[str, Any]
    raised_at: datetime
    last_seen_at: datetime
    cleared_at: datetime | None = None
    acknowledged_at: datetime | None = None
    acknowledged_by: str | None = None


@dataclass(frozen=True)
class AlertRaise:
    """Payload to open (or refresh) an alert keyed by `dedup_key`."""

    dedup_key: str
    rule_key: str
    rule_kind: RuleKind
    severity: RuleSeverity
    title: str
    message: str
    cluster_id: str | None = None
    run_id: int | None = None
    campaign_id: int | None = None
    case_id: int | None = None
    details: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RuleRunState:
    """Incremental cursor and hysteresis state for `(run_id, rule_key, series_tag)`."""

    run_id: int
    rule_key: str
    series_tag: str = ""
    last_x: float | None = None
    consecutive_hits: int = 0
    consecutive_clear: int = 0
    warmup_remaining: int = 0
    last_observed_bytes: int | None = None
    last_growth_at: datetime | None = None
    growth_intervals_s: tuple[float, ...] = ()
    updated_at: datetime | None = None


class RuleStore(Protocol):
    """Durable SQLite persistence for rule configs, open/cleared alerts, and evaluation state."""

    async def upsert_rule_config(self, spec: RuleSpec) -> RuleConfigRecord: ...

    async def delete_rule_config(
        self, scope_type: RuleScope, scope_id: int, rule_key: str
    ) -> bool: ...

    async def list_rule_configs(
        self,
        *,
        scope_type: RuleScope | None = None,
        scope_id: int | None = None,
    ) -> list[RuleConfigRecord]: ...

    async def raise_alert(self, req: AlertRaise) -> tuple[AlertRecord, bool]:
        """Open a new alert if no open alert shares `dedup_key`, else refresh `last_seen_at`.

        Returns `(record, newly_opened)`.
        """
        ...

    async def clear_alert(self, dedup_key: str) -> AlertRecord | None:
        """Close the currently open alert matching `dedup_key`, or return None."""
        ...

    async def clear_alerts_for_run(
        self, run_id: int, *, rule_kinds: frozenset[RuleKind] | None = None
    ) -> list[AlertRecord]: ...

    async def acknowledge_alert(
        self, alert_id: int, *, by: str = "cli"
    ) -> AlertRecord | None: ...

    async def get_alert(self, alert_id: int) -> AlertRecord | None: ...

    async def list_alerts(
        self,
        *,
        open_only: bool = False,
        run_id: int | None = None,
        limit: int = 200,
    ) -> list[AlertRecord]: ...

    async def get_run_state(
        self, run_id: int, rule_key: str, series_tag: str = ""
    ) -> RuleRunState | None: ...

    async def list_run_states(self, run_id: int) -> list[RuleRunState]: ...

    async def upsert_run_state(self, state: RuleRunState) -> None: ...


class RuleEngine(Protocol):
    """Detection rule evaluator with hierarchical configuration and alert lifecycle."""

    async def effective_rules(
        self,
        *,
        run_id: int | None = None,
        campaign_id: int | None = None,
        case_id: int | None = None,
    ) -> list[EffectiveRule]: ...

    async def set_rule(self, spec: RuleSpec) -> RuleConfigRecord: ...

    async def unset_rule(
        self, scope_type: RuleScope, scope_id: int, rule_key: str
    ) -> bool: ...

    async def list_alerts(
        self,
        *,
        open_only: bool = False,
        run_id: int | None = None,
        limit: int = 200,
    ) -> list[AlertRecord]: ...

    async def acknowledge(self, alert_id: int, *, by: str = "cli") -> AlertRecord | None: ...

    async def evaluate_stall_now(self, *, now: datetime | None = None) -> list[AlertRecord]:
        """Evaluate adaptive stall conditions across currently running runs."""
        ...
