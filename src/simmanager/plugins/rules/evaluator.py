"""Pure detection-rule merging, incremental series evaluation, and adaptive stall math."""

from __future__ import annotations

import math
import posixpath
import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from simmanager.contracts.rules import (
    EffectiveRule,
    RuleConfigRecord,
    RuleKind,
    RuleScope,
    RuleSeverity,
)
from simmanager.contracts.timeseries import SeriesMeta


@dataclass(frozen=True)
class PointEvalResult:
    """Result of evaluating new points on one series without full-series rescans."""

    last_x: float | None
    consecutive_hits: int
    consecutive_clear: int
    warmup_remaining: int
    should_raise: bool
    should_clear: bool
    last_trigger_value: float | None = None
    last_trigger_x: float | None = None


def merge_effective_rules(
    defaults: Sequence[EffectiveRule],
    db_configs: Sequence[RuleConfigRecord],
    *,
    campaign_id: int | None = None,
    case_id: int | None = None,
) -> list[EffectiveRule]:
    """Merge global YAML defaults with DB overrides (`global` -> `campaign` -> `case`)."""
    merged: dict[str, EffectiveRule] = {r.rule_key: r for r in defaults}

    def _apply_layer(scope: RuleScope, scope_id: int) -> None:
        for cfg in db_configs:
            if cfg.scope_type is not scope or cfg.scope_id != scope_id:
                continue
            prev = merged.get(cfg.rule_key)
            params = {**(prev.params if prev else {}), **cfg.params}
            merged[cfg.rule_key] = EffectiveRule(
                rule_key=cfg.rule_key,
                rule_kind=cfg.rule_kind,
                enabled=cfg.enabled,
                severity=cfg.severity,
                params=params,
                origin_scope=scope,
                origin_id=scope_id,
            )

    _apply_layer(RuleScope.GLOBAL, 0)
    if campaign_id is not None:
        _apply_layer(RuleScope.CAMPAIGN, campaign_id)
    if case_id is not None:
        _apply_layer(RuleScope.CASE, case_id)

    return sorted(merged.values(), key=lambda r: r.rule_key)


def match_series_for_rule(
    rule: EffectiveRule, all_series: Sequence[SeriesMeta]
) -> list[SeriesMeta]:
    """Select time series relevant to `rule`."""
    if rule.rule_kind is RuleKind.COURANT_HIGH:
        target = str(rule.params.get("series", "courant.max"))
        return [s for s in all_series if s.name == target]

    if rule.rule_kind is RuleKind.RESIDUAL_HIGH:
        prefix = str(rule.params.get("series_prefix", "residual."))
        return [s for s in all_series if s.name.startswith(prefix)]

    if rule.rule_kind is RuleKind.MONITOR_RANGE:
        selector = str(rule.params.get("selector", "")).strip()
        if not selector:
            return []
        file_filter, series_name, qualifier = _parse_selector(selector)
        matched: list[SeriesMeta] = []
        for s in all_series:
            if s.name != series_name:
                continue
            if file_filter is not None and not (
                s.file_path == file_filter
                or posixpath.basename(s.file_path) == file_filter
                or s.file_path.endswith("/" + file_filter)
            ):
                continue
            if qualifier is not None and s.qualifier != qualifier:
                continue
            matched.append(s)
        return matched

    return []


def _parse_selector(spec: str) -> tuple[str | None, str, str | None]:
    parts = spec.split(":", 2)
    if len(parts) == 1:
        return None, parts[0].strip(), None
    if len(parts) == 2:
        return parts[0].strip() or None, parts[1].strip(), None
    return parts[0].strip() or None, parts[1].strip(), parts[2].strip()


def _is_violation(rule_kind: RuleKind, y: float, params: Mapping[str, Any]) -> bool:
    if rule_kind in (RuleKind.COURANT_HIGH, RuleKind.RESIDUAL_HIGH):
        threshold = float(params.get("threshold", 1.0))
        return y > threshold
    if rule_kind is RuleKind.MONITOR_RANGE:
        lo = params.get("min")
        hi = params.get("max")
        if lo is not None and y < float(lo):
            return True
        return bool(hi is not None and y > float(hi))
    return False


def evaluate_points(
    rule: EffectiveRule,
    xs: Sequence[float],
    ys: Sequence[float],
    *,
    last_x: float | None,
    consecutive_hits: int,
    consecutive_clear: int,
    warmup_remaining: int,
) -> PointEvalResult:
    """Evaluate strictly new `(x, y)` points using hysteresis and warmup/restart skipping."""
    needed_hits = max(1, int(rule.params.get("consecutive", 1)))
    needed_clear = max(1, int(rule.params.get("clear_consecutive", 1)))
    warmup_steps = max(0, int(rule.params.get("warmup_steps", 0)))

    cur_last_x = last_x
    hits = consecutive_hits
    clears = consecutive_clear
    warmup = warmup_remaining
    should_raise = False
    should_clear = False
    trigger_val: float | None = None
    trigger_x: float | None = None

    for x, y in zip(xs, ys, strict=True):
        if cur_last_x is not None and x < cur_last_x:
            # Restart / non-monotonic jump: restart warmup period
            warmup = warmup_steps
            hits = 0
            clears = 0
        elif cur_last_x is not None and x == cur_last_x:
            continue

        cur_last_x = x
        if not math.isfinite(y):
            continue

        if warmup > 0:
            warmup -= 1
            continue

        if _is_violation(rule.rule_kind, y, rule.params):
            hits += 1
            clears = 0
            if hits >= needed_hits:
                should_raise = True
                should_clear = False
                trigger_val = y
                trigger_x = x
        else:
            clears += 1
            hits = 0
            if clears >= needed_clear:
                should_clear = True

    return PointEvalResult(
        last_x=cur_last_x,
        consecutive_hits=hits,
        consecutive_clear=clears,
        warmup_remaining=warmup,
        should_raise=should_raise,
        should_clear=should_clear,
        last_trigger_value=trigger_val,
        last_trigger_x=trigger_x,
    )


def adaptive_stall_threshold_s(
    intervals_s: Sequence[float],
    *,
    min_minutes: float = 30.0,
    multiplier: float = 3.0,
) -> float:
    """Compute `max(min_minutes * 60, multiplier * median(intervals_s))`."""
    floor_s = max(0.0, min_minutes) * 60.0
    if not intervals_s:
        return floor_s
    med = statistics.median(intervals_s)
    return max(floor_s, max(0.0, multiplier) * med)


def format_default_severity(val: str) -> RuleSeverity:
    return RuleSeverity(val)
