"""SQLite implementation of `simmanager.contracts.rules.RuleStore`."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, ClassVar

import aiosqlite

from simmanager.contracts import rules as contract
from simmanager.contracts import storage as storage_contract
from simmanager.contracts.rules import (
    AlertRaise,
    AlertRecord,
    AlertStatus,
    RuleConfigRecord,
    RuleKind,
    RuleRunState,
    RuleScope,
    RuleSeverity,
    RuleSpec,
)
from simmanager.kernel import Context, Plugin

from .plugin import SqliteStorage


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


class SqliteRuleStore:
    """Implements `simmanager.contracts.rules.RuleStore` on the shared SQLite db."""

    def __init__(self, db: aiosqlite.Connection, write_lock: asyncio.Lock) -> None:
        self._db = db
        self._write_lock = write_lock

    async def upsert_rule_config(self, spec: RuleSpec) -> RuleConfigRecord:
        now = _now_iso()
        params_json = json.dumps(dict(spec.params), ensure_ascii=False, sort_keys=True)
        async with self._write_lock:
            await self._db.execute(
                """
                INSERT INTO rule_configs (
                    scope_type, scope_id, rule_key, rule_kind, enabled, severity,
                    params_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(scope_type, scope_id, rule_key) DO UPDATE SET
                    rule_kind = excluded.rule_kind,
                    enabled = excluded.enabled,
                    severity = excluded.severity,
                    params_json = excluded.params_json,
                    updated_at = excluded.updated_at
                """,
                (
                    str(spec.scope_type),
                    spec.scope_id,
                    spec.rule_key,
                    str(spec.rule_kind),
                    int(spec.enabled),
                    str(spec.severity),
                    params_json,
                    now,
                ),
            )
            await self._db.commit()
            async with self._db.execute(
                """
                SELECT * FROM rule_configs
                WHERE scope_type = ? AND scope_id = ? AND rule_key = ?
                """,
                (str(spec.scope_type), spec.scope_id, spec.rule_key),
            ) as cur:
                row = await cur.fetchone()
        assert row is not None
        return _rule_config_from_row(row)

    async def delete_rule_config(self, scope_type: RuleScope, scope_id: int, rule_key: str) -> bool:
        async with self._write_lock:
            cur = await self._db.execute(
                """
                DELETE FROM rule_configs
                WHERE scope_type = ? AND scope_id = ? AND rule_key = ?
                """,
                (str(scope_type), scope_id, rule_key),
            )
            await self._db.commit()
            return cur.rowcount > 0

    async def list_rule_configs(
        self,
        *,
        scope_type: RuleScope | None = None,
        scope_id: int | None = None,
    ) -> list[RuleConfigRecord]:
        sql = "SELECT * FROM rule_configs WHERE 1 = 1"
        args: list[Any] = []
        if scope_type is not None:
            sql += " AND scope_type = ?"
            args.append(str(scope_type))
        if scope_id is not None:
            sql += " AND scope_id = ?"
            args.append(scope_id)
        sql += " ORDER BY scope_type ASC, scope_id ASC, rule_key ASC"
        async with self._db.execute(sql, args) as cur:
            return [_rule_config_from_row(row) async for row in cur]

    async def raise_alert(self, req: AlertRaise) -> tuple[AlertRecord, bool]:
        now = _now_iso()
        details_json = json.dumps(dict(req.details), ensure_ascii=False, default=str)
        async with self._write_lock:
            async with self._db.execute(
                "SELECT * FROM alerts WHERE dedup_key = ? AND status = 'open' LIMIT 1",
                (req.dedup_key,),
            ) as cur:
                existing = await cur.fetchone()
            if existing is not None:
                alert_id = int(existing["id"])
                await self._db.execute(
                    """
                    UPDATE alerts
                    SET severity = ?, title = ?, message = ?, details_json = ?, last_seen_at = ?
                    WHERE id = ?
                    """,
                    (str(req.severity), req.title, req.message, details_json, now, alert_id),
                )
                await self._db.commit()
                row = await self._alert_row(alert_id)
                assert row is not None
                return _alert_from_row(row), False

            cur = await self._db.execute(
                """
                INSERT INTO alerts (
                    dedup_key, rule_key, rule_kind, severity, status,
                    cluster_id, run_id, campaign_id, case_id,
                    title, message, details_json, raised_at, last_seen_at
                ) VALUES (?, ?, ?, ?, 'open', ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    req.dedup_key,
                    req.rule_key,
                    str(req.rule_kind),
                    str(req.severity),
                    req.cluster_id,
                    req.run_id,
                    req.campaign_id,
                    req.case_id,
                    req.title,
                    req.message,
                    details_json,
                    now,
                    now,
                ),
            )
            await self._db.commit()
            assert cur.lastrowid is not None
            row = await self._alert_row(int(cur.lastrowid))
        assert row is not None
        return _alert_from_row(row), True

    async def clear_alert(self, dedup_key: str) -> AlertRecord | None:
        now = _now_iso()
        async with self._write_lock:
            async with self._db.execute(
                "SELECT id FROM alerts WHERE dedup_key = ? AND status = 'open' LIMIT 1",
                (dedup_key,),
            ) as cur:
                existing = await cur.fetchone()
            if existing is None:
                return None
            alert_id = int(existing["id"])
            await self._db.execute(
                """
                UPDATE alerts
                SET status = 'cleared', cleared_at = ?, last_seen_at = ?
                WHERE id = ?
                """,
                (now, now, alert_id),
            )
            await self._db.commit()
            row = await self._alert_row(alert_id)
        return _alert_from_row(row) if row is not None else None

    async def clear_alerts_for_run(
        self, run_id: int, *, rule_kinds: frozenset[RuleKind] | None = None
    ) -> list[AlertRecord]:
        now = _now_iso()
        async with self._write_lock:
            sql = "SELECT id FROM alerts WHERE run_id = ? AND status = 'open'"
            args: list[Any] = [run_id]
            if rule_kinds:
                sql += f" AND rule_kind IN ({','.join('?' * len(rule_kinds))})"
                args.extend(str(k) for k in rule_kinds)
            async with self._db.execute(sql, args) as cur:
                ids = [int(r["id"]) async for r in cur]
            if not ids:
                return []
            placeholders = ",".join("?" * len(ids))
            await self._db.execute(
                f"UPDATE alerts SET status = 'cleared', cleared_at = ?, last_seen_at = ?"
                f" WHERE id IN ({placeholders})",
                [now, now, *ids],
            )
            await self._db.commit()
            async with self._db.execute(
                f"SELECT * FROM alerts WHERE id IN ({placeholders}) ORDER BY id ASC", ids
            ) as cur:
                return [_alert_from_row(row) async for row in cur]

    async def acknowledge_alert(self, alert_id: int, *, by: str = "cli") -> AlertRecord | None:
        now = _now_iso()
        async with self._write_lock:
            cur = await self._db.execute(
                """
                UPDATE alerts
                SET acknowledged_at = COALESCE(acknowledged_at, ?),
                    acknowledged_by = COALESCE(acknowledged_by, ?)
                WHERE id = ?
                """,
                (now, by, alert_id),
            )
            await self._db.commit()
            if cur.rowcount == 0:
                return None
            row = await self._alert_row(alert_id)
        return _alert_from_row(row) if row is not None else None

    async def get_alert(self, alert_id: int) -> AlertRecord | None:
        row = await self._alert_row(alert_id)
        return _alert_from_row(row) if row is not None else None

    async def list_alerts(
        self,
        *,
        open_only: bool = False,
        run_id: int | None = None,
        limit: int = 200,
    ) -> list[AlertRecord]:
        sql = "SELECT * FROM alerts WHERE 1 = 1"
        args: list[Any] = []
        if open_only:
            sql += " AND status = 'open'"
        if run_id is not None:
            sql += " AND run_id = ?"
            args.append(run_id)
        sql += " ORDER BY raised_at DESC, id DESC LIMIT ?"
        args.append(limit)
        async with self._db.execute(sql, args) as cur:
            return [_alert_from_row(row) async for row in cur]

    async def get_run_state(
        self, run_id: int, rule_key: str, series_tag: str = ""
    ) -> RuleRunState | None:
        async with self._db.execute(
            """
            SELECT * FROM rule_run_state
            WHERE run_id = ? AND rule_key = ? AND series_tag = ?
            """,
            (run_id, rule_key, series_tag),
        ) as cur:
            row = await cur.fetchone()
        return _run_state_from_row(row) if row is not None else None

    async def list_run_states(self, run_id: int) -> list[RuleRunState]:
        async with self._db.execute(
            "SELECT * FROM rule_run_state WHERE run_id = ? ORDER BY rule_key, series_tag",
            (run_id,),
        ) as cur:
            return [_run_state_from_row(row) async for row in cur]

    async def upsert_run_state(self, state: RuleRunState) -> None:
        now = _now_iso()
        intervals_json = json.dumps(list(state.growth_intervals_s))
        last_growth = state.last_growth_at.isoformat() if state.last_growth_at else None
        async with self._write_lock:
            await self._db.execute(
                """
                INSERT INTO rule_run_state (
                    run_id, rule_key, series_tag, last_x, consecutive_hits,
                    consecutive_clear, warmup_remaining, last_observed_bytes,
                    last_growth_at, growth_intervals_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id, rule_key, series_tag) DO UPDATE SET
                    last_x = excluded.last_x,
                    consecutive_hits = excluded.consecutive_hits,
                    consecutive_clear = excluded.consecutive_clear,
                    warmup_remaining = excluded.warmup_remaining,
                    last_observed_bytes = excluded.last_observed_bytes,
                    last_growth_at = excluded.last_growth_at,
                    growth_intervals_json = excluded.growth_intervals_json,
                    updated_at = excluded.updated_at
                """,
                (
                    state.run_id,
                    state.rule_key,
                    state.series_tag,
                    state.last_x,
                    state.consecutive_hits,
                    state.consecutive_clear,
                    state.warmup_remaining,
                    state.last_observed_bytes,
                    last_growth,
                    intervals_json,
                    now,
                ),
            )
            await self._db.commit()

    async def _alert_row(self, alert_id: int) -> aiosqlite.Row | None:
        async with self._db.execute("SELECT * FROM alerts WHERE id = ?", (alert_id,)) as cur:
            return await cur.fetchone()


def _rule_config_from_row(row: aiosqlite.Row) -> RuleConfigRecord:
    raw_params: Any = json.loads(row["params_json"] or "{}")
    params = dict(raw_params) if isinstance(raw_params, dict) else {}
    return RuleConfigRecord(
        id=int(row["id"]),
        scope_type=RuleScope(row["scope_type"]),
        scope_id=int(row["scope_id"]),
        rule_key=str(row["rule_key"]),
        rule_kind=RuleKind(row["rule_kind"]),
        enabled=bool(row["enabled"]),
        severity=RuleSeverity(row["severity"]),
        params=params,
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )


def _alert_from_row(row: aiosqlite.Row) -> AlertRecord:
    raw_details: Any = json.loads(row["details_json"] or "{}")
    details = dict(raw_details) if isinstance(raw_details, dict) else {}
    return AlertRecord(
        id=int(row["id"]),
        dedup_key=str(row["dedup_key"]),
        rule_key=str(row["rule_key"]),
        rule_kind=RuleKind(row["rule_kind"]),
        severity=RuleSeverity(row["severity"]),
        status=AlertStatus(row["status"]),
        cluster_id=row["cluster_id"],
        run_id=row["run_id"],
        campaign_id=row["campaign_id"],
        case_id=row["case_id"],
        title=str(row["title"]),
        message=str(row["message"]),
        details=details,
        raised_at=datetime.fromisoformat(row["raised_at"]),
        last_seen_at=datetime.fromisoformat(row["last_seen_at"]),
        cleared_at=datetime.fromisoformat(row["cleared_at"]) if row["cleared_at"] else None,
        acknowledged_at=(
            datetime.fromisoformat(row["acknowledged_at"]) if row["acknowledged_at"] else None
        ),
        acknowledged_by=row["acknowledged_by"],
    )


def _run_state_from_row(row: aiosqlite.Row) -> RuleRunState:
    raw_intervals: Any = json.loads(row["growth_intervals_json"] or "[]")
    intervals = tuple(float(x) for x in raw_intervals) if isinstance(raw_intervals, list) else ()
    return RuleRunState(
        run_id=int(row["run_id"]),
        rule_key=str(row["rule_key"]),
        series_tag=str(row["series_tag"]),
        last_x=float(row["last_x"]) if row["last_x"] is not None else None,
        consecutive_hits=int(row["consecutive_hits"]),
        consecutive_clear=int(row["consecutive_clear"]),
        warmup_remaining=int(row["warmup_remaining"]),
        last_observed_bytes=(
            int(row["last_observed_bytes"]) if row["last_observed_bytes"] is not None else None
        ),
        last_growth_at=(
            datetime.fromisoformat(row["last_growth_at"]) if row["last_growth_at"] else None
        ),
        growth_intervals_s=intervals,
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )


class SqliteRulePlugin(Plugin):
    name = "rules-sqlite"
    provides = contract.SERVICE_KEY
    contract_version = contract.CONTRACT_VERSION
    inject: ClassVar[Mapping[str, int]] = {
        storage_contract.SERVICE_KEY: storage_contract.CONTRACT_VERSION
    }
    description = "Durable rule configs, alerts, and incremental run state on SQLite."

    def apply(self, ctx: Context) -> SqliteRuleStore:
        storage = ctx.services.require(storage_contract.SERVICE_KEY)
        if not isinstance(storage, SqliteStorage):
            raise TypeError(
                f"{self.name} requires storage-sqlite's SqliteStorage, got {type(storage).__name__}"
            )
        return SqliteRuleStore(storage.connection, storage.write_lock)
