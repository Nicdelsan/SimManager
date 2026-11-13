"""SQLite implementation of `simmanager.contracts.campaign.CampaignStore`."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, ClassVar

import aiosqlite

from simmanager.contracts import campaign as contract
from simmanager.contracts import storage as storage_contract
from simmanager.contracts.campaign import (
    CampaignRecord,
    CampaignSpec,
    CaseComputedState,
    CaseRecord,
    CaseRunLink,
    CaseSpec,
    MatchAmbiguityRecord,
    MatchKind,
    ProjectRecord,
    RunExecutionFacts,
)
from simmanager.contracts.storage import JobRecord, RunRecord
from simmanager.contracts.timeseries import RunSolverState
from simmanager.kernel import Context, Plugin

from .plugin import SqliteStorage, _job_record
from .timeseries import _run_state_from_row


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _placeholders(n: int) -> str:
    return ",".join("?" * n)


class SqliteCampaignStore:
    """Implements `simmanager.contracts.campaign.CampaignStore` on the shared SQLite db."""

    def __init__(self, db: aiosqlite.Connection, write_lock: asyncio.Lock) -> None:
        self._db = db
        self._write_lock = write_lock

    async def ensure_project(self, name: str, *, description: str = "") -> ProjectRecord:
        now = _now_iso()
        async with self._write_lock:
            async with self._db.execute("SELECT * FROM projects WHERE name = ?", (name,)) as cur:
                row = await cur.fetchone()
            if row is not None:
                return _project_from_row(row)
            cur = await self._db.execute(
                "INSERT INTO projects (name, description, created_at, updated_at)"
                " VALUES (?, ?, ?, ?)",
                (name, description, now, now),
            )
            await self._db.commit()
            assert cur.lastrowid is not None
            async with self._db.execute(
                "SELECT * FROM projects WHERE id = ?", (cur.lastrowid,)
            ) as rcur:
                created = await rcur.fetchone()
            assert created is not None
            return _project_from_row(created)

    async def projects(self) -> list[ProjectRecord]:
        async with self._db.execute("SELECT * FROM projects ORDER BY name ASC, id ASC") as cur:
            return [_project_from_row(row) async for row in cur]

    async def upsert_campaign(self, spec: CampaignSpec) -> CampaignRecord:
        project = await self.ensure_project(spec.project_name)
        now = _now_iso()
        cols_json = json.dumps(list(spec.columns), ensure_ascii=False)
        monitors_json = json.dumps(list(spec.export_monitors), ensure_ascii=False)
        async with self._write_lock:
            async with self._db.execute(
                "SELECT id, export_monitors_json FROM campaigns WHERE project_id = ? AND name = ?",
                (project.id, spec.name),
            ) as cur:
                existing = await cur.fetchone()
            if existing is None:
                cur = await self._db.execute(
                    """
                    INSERT INTO campaigns (
                        project_id, name, source_path, sheet_name, header_row,
                        case_column, work_dir_column, manual_state_column,
                        work_root, naming_regex, columns_json, export_monitors_json,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        project.id,
                        spec.name,
                        spec.source_path,
                        spec.sheet_name,
                        spec.header_row,
                        spec.case_column,
                        spec.work_dir_column,
                        spec.manual_state_column,
                        spec.work_root,
                        spec.naming_regex,
                        cols_json,
                        monitors_json,
                        now,
                        now,
                    ),
                )
                campaign_id = int(cur.lastrowid or 0)
            else:
                campaign_id = int(existing["id"])
                final_monitors = (
                    monitors_json if spec.export_monitors else str(existing["export_monitors_json"])
                )
                await self._db.execute(
                    """
                    UPDATE campaigns
                    SET source_path = ?, sheet_name = ?, header_row = ?,
                        case_column = ?, work_dir_column = ?, manual_state_column = ?,
                        work_root = ?, naming_regex = ?, columns_json = ?,
                        export_monitors_json = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        spec.source_path,
                        spec.sheet_name,
                        spec.header_row,
                        spec.case_column,
                        spec.work_dir_column,
                        spec.manual_state_column,
                        spec.work_root,
                        spec.naming_regex,
                        cols_json,
                        final_monitors,
                        now,
                        campaign_id,
                    ),
                )
            await self._db.commit()
            async with self._db.execute(
                "SELECT * FROM campaigns WHERE id = ?", (campaign_id,)
            ) as rcur:
                row = await rcur.fetchone()
            assert row is not None
            return _campaign_from_row(row)

    async def campaign(
        self, name_or_id: str | int, *, project_name: str | None = None
    ) -> CampaignRecord | None:
        if isinstance(name_or_id, int):
            async with self._db.execute(
                "SELECT * FROM campaigns WHERE id = ?", (name_or_id,)
            ) as cur:
                row = await cur.fetchone()
            return None if row is None else _campaign_from_row(row)

        if project_name is not None:
            sql = (
                "SELECT c.* FROM campaigns c"
                " JOIN projects p ON p.id = c.project_id"
                " WHERE c.name = ? AND p.name = ?"
            )
            args: tuple[Any, ...] = (name_or_id, project_name)
        else:
            sql = "SELECT * FROM campaigns WHERE name = ?"
            args = (name_or_id,)

        async with self._db.execute(sql, args) as cur:
            rows = list(await cur.fetchall())
        if len(rows) > 1:
            raise ValueError(f"multiple campaigns named {name_or_id!r}; specify project_name")
        if rows:
            return _campaign_from_row(rows[0])
        if name_or_id.isdigit() and project_name is None:
            async with self._db.execute(
                "SELECT * FROM campaigns WHERE id = ?", (int(name_or_id),)
            ) as cur:
                row = await cur.fetchone()
            return None if row is None else _campaign_from_row(row)
        return None

    async def campaigns(self, *, project_name: str | None = None) -> list[CampaignRecord]:
        if project_name is not None:
            sql = (
                "SELECT c.* FROM campaigns c"
                " JOIN projects p ON p.id = c.project_id"
                " WHERE p.name = ?"
                " ORDER BY c.id ASC"
            )
            args: tuple[Any, ...] = (project_name,)
        else:
            sql = "SELECT * FROM campaigns ORDER BY project_id ASC, id ASC"
            args = ()
        async with self._db.execute(sql, args) as cur:
            return [_campaign_from_row(row) async for row in cur]

    async def case(self, campaign_id: int, case_key_or_id: str | int) -> CaseRecord | None:
        if isinstance(case_key_or_id, int):
            async with self._db.execute(
                "SELECT * FROM cases WHERE campaign_id = ? AND id = ?",
                (campaign_id, case_key_or_id),
            ) as cur:
                row = await cur.fetchone()
            return None if row is None else _case_from_row(row)

        async with self._db.execute(
            "SELECT * FROM cases WHERE campaign_id = ? AND case_key = ?",
            (campaign_id, case_key_or_id),
        ) as cur:
            row = await cur.fetchone()
        return None if row is None else _case_from_row(row)

    async def cases(self, campaign_id: int, *, include_inactive: bool = False) -> list[CaseRecord]:
        sql = "SELECT * FROM cases WHERE campaign_id = ?"
        if not include_inactive:
            sql += " AND active = 1"
        sql += " ORDER BY COALESCE(row_index, 2147483647) ASC, id ASC"
        async with self._db.execute(sql, (campaign_id,)) as cur:
            return [_case_from_row(row) async for row in cur]

    async def upsert_case(
        self,
        campaign_id: int,
        spec: CaseSpec,
        *,
        active: bool = True,
    ) -> tuple[CaseRecord, bool, bool]:
        now = _now_iso()
        params_dict = dict(spec.params)
        params_json = json.dumps(params_dict, ensure_ascii=False)
        async with self._write_lock:
            async with self._db.execute(
                "SELECT * FROM cases WHERE campaign_id = ? AND case_key = ?",
                (campaign_id, spec.case_key),
            ) as cur:
                existing_row = await cur.fetchone()

            if existing_row is None:
                cur = await self._db.execute(
                    """
                    INSERT INTO cases (
                        campaign_id, case_key, row_index, parent_key_raw,
                        work_dir, manual_state, computed_state, params_json,
                        active, notes, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        campaign_id,
                        spec.case_key,
                        spec.row_index,
                        spec.parent_key_raw,
                        spec.work_dir,
                        spec.manual_state,
                        str(CaseComputedState.PLANNED),
                        params_json,
                        int(active),
                        spec.notes,
                        now,
                        now,
                    ),
                )
                await self._db.commit()
                async with self._db.execute(
                    "SELECT * FROM cases WHERE id = ?", (cur.lastrowid,)
                ) as rcur:
                    row = await rcur.fetchone()
                assert row is not None
                return _case_from_row(row), True, True

            existing = _case_from_row(existing_row)
            changed = (
                existing.row_index != spec.row_index
                or existing.parent_key_raw != spec.parent_key_raw
                or existing.work_dir != spec.work_dir
                or existing.manual_state != spec.manual_state
                or existing.params != params_dict
                or existing.active != active
                or (spec.notes is not None and existing.notes != spec.notes)
            )
            if not changed:
                return existing, False, False

            notes = spec.notes if spec.notes is not None else existing.notes
            await self._db.execute(
                """
                UPDATE cases
                SET row_index = ?, parent_key_raw = ?, work_dir = ?,
                    manual_state = ?, params_json = ?, active = ?,
                    notes = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    spec.row_index,
                    spec.parent_key_raw,
                    spec.work_dir,
                    spec.manual_state,
                    params_json,
                    int(active),
                    notes,
                    now,
                    existing.id,
                ),
            )
            await self._db.commit()
            async with self._db.execute("SELECT * FROM cases WHERE id = ?", (existing.id,)) as rcur:
                row = await rcur.fetchone()
            assert row is not None
            return _case_from_row(row), False, True

    async def rename_case(
        self, campaign_id: int, old_case_key: str, new_case_key: str
    ) -> CaseRecord:
        now = _now_iso()
        async with self._write_lock:
            async with self._db.execute(
                "SELECT id FROM cases WHERE campaign_id = ? AND case_key = ?",
                (campaign_id, old_case_key),
            ) as cur:
                old_row = await cur.fetchone()
            if old_row is None:
                raise KeyError(f"case {old_case_key!r} not found in campaign {campaign_id}")
            async with self._db.execute(
                "SELECT id FROM cases WHERE campaign_id = ? AND case_key = ?",
                (campaign_id, new_case_key),
            ) as cur:
                conflict = await cur.fetchone()
            if conflict is not None:
                raise ValueError(f"case {new_case_key!r} already exists in campaign {campaign_id}")
            await self._db.execute(
                "UPDATE cases SET case_key = ?, active = 1, updated_at = ? WHERE id = ?",
                (new_case_key, now, old_row["id"]),
            )
            await self._db.commit()
            async with self._db.execute(
                "SELECT * FROM cases WHERE id = ?", (old_row["id"],)
            ) as rcur:
                row = await rcur.fetchone()
            assert row is not None
            return _case_from_row(row)

    async def set_cases_inactive(self, campaign_id: int, case_keys: Sequence[str]) -> None:
        if not case_keys:
            return
        now = _now_iso()
        async with self._write_lock:
            await self._db.execute(
                f"UPDATE cases SET active = 0, updated_at = ?"
                f" WHERE campaign_id = ? AND case_key IN ({_placeholders(len(case_keys))})",
                (now, campaign_id, *case_keys),
            )
            await self._db.commit()

    async def set_case_parent(self, case_id: int, parent_case_id: int | None) -> None:
        now = _now_iso()
        async with self._write_lock:
            await self._db.execute(
                "UPDATE cases SET parent_case_id = ?, updated_at = ? WHERE id = ?",
                (parent_case_id, now, case_id),
            )
            await self._db.commit()

    async def update_case_computed_state(
        self, case_id: int, state: CaseComputedState
    ) -> CaseComputedState | None:
        now = _now_iso()
        async with self._write_lock:
            async with self._db.execute(
                "SELECT computed_state FROM cases WHERE id = ?", (case_id,)
            ) as cur:
                row = await cur.fetchone()
            if row is None:
                raise KeyError(case_id)
            prev = CaseComputedState(row["computed_state"])
            if prev is state:
                return None
            await self._db.execute(
                "UPDATE cases SET computed_state = ?, updated_at = ? WHERE id = ?",
                (str(state), now, case_id),
            )
            await self._db.commit()
            return prev

    async def link_run(
        self,
        campaign_id: int,
        case_id: int,
        run_id: int,
        *,
        match_kind: MatchKind,
        match_detail: str | None = None,
        locked: bool = False,
    ) -> CaseRunLink:
        now = _now_iso()
        is_locked = locked or (match_kind is MatchKind.MANUAL)
        async with self._write_lock:
            async with self._db.execute(
                "SELECT * FROM case_runs WHERE campaign_id = ? AND run_id = ?",
                (campaign_id, run_id),
            ) as cur:
                existing = await cur.fetchone()

            if existing is not None:
                if bool(existing["locked"]) and not is_locked:
                    return _link_from_row(existing)
                await self._db.execute(
                    """
                    UPDATE case_runs
                    SET case_id = ?, match_kind = ?, match_detail = ?, locked = ?
                    WHERE id = ?
                    """,
                    (case_id, str(match_kind), match_detail, int(is_locked), existing["id"]),
                )
                link_id = int(existing["id"])
            else:
                cur = await self._db.execute(
                    """
                    INSERT INTO case_runs (
                        campaign_id, case_id, run_id, match_kind, match_detail, locked, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        campaign_id,
                        case_id,
                        run_id,
                        str(match_kind),
                        match_detail,
                        int(is_locked),
                        now,
                    ),
                )
                link_id = int(cur.lastrowid or 0)

            if is_locked:
                await self._db.execute(
                    "DELETE FROM case_run_exclusions WHERE campaign_id = ? AND run_id = ?",
                    (campaign_id, run_id),
                )
            await self._db.execute(
                "UPDATE case_match_ambiguities SET resolved = 1, updated_at = ?"
                " WHERE campaign_id = ? AND run_id = ?",
                (now, campaign_id, run_id),
            )
            await self._db.commit()
            async with self._db.execute("SELECT * FROM case_runs WHERE id = ?", (link_id,)) as rcur:
                row = await rcur.fetchone()
            assert row is not None
            return _link_from_row(row)

    async def unlink_run(
        self, campaign_id: int, run_id: int, *, exclude: bool = True
    ) -> CaseRunLink | None:
        now = _now_iso()
        async with self._write_lock:
            async with self._db.execute(
                "SELECT * FROM case_runs WHERE campaign_id = ? AND run_id = ?",
                (campaign_id, run_id),
            ) as cur:
                existing = await cur.fetchone()
            if existing is not None:
                await self._db.execute("DELETE FROM case_runs WHERE id = ?", (existing["id"],))
            if exclude:
                await self._db.execute(
                    """
                    INSERT INTO case_run_exclusions (campaign_id, run_id, created_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT (campaign_id, run_id) DO UPDATE SET created_at = excluded.created_at
                    """,
                    (campaign_id, run_id, now),
                )
            await self._db.commit()
            return None if existing is None else _link_from_row(existing)

    async def links_for_campaign(self, campaign_id: int) -> list[CaseRunLink]:
        async with self._db.execute(
            "SELECT * FROM case_runs WHERE campaign_id = ? ORDER BY id ASC", (campaign_id,)
        ) as cur:
            return [_link_from_row(row) async for row in cur]

    async def links_for_run(self, run_id: int) -> list[CaseRunLink]:
        async with self._db.execute(
            "SELECT * FROM case_runs WHERE run_id = ? ORDER BY id ASC", (run_id,)
        ) as cur:
            return [_link_from_row(row) async for row in cur]

    async def is_run_excluded(self, campaign_id: int, run_id: int) -> bool:
        async with self._db.execute(
            "SELECT 1 FROM case_run_exclusions WHERE campaign_id = ? AND run_id = ?",
            (campaign_id, run_id),
        ) as cur:
            return (await cur.fetchone()) is not None

    async def exclusions_for_campaign(self, campaign_id: int) -> set[int]:
        async with self._db.execute(
            "SELECT run_id FROM case_run_exclusions WHERE campaign_id = ?", (campaign_id,)
        ) as cur:
            return {int(row["run_id"]) async for row in cur}

    async def record_ambiguity(
        self, campaign_id: int, run_id: int, candidate_case_ids: Sequence[int]
    ) -> bool:
        now = _now_iso()
        normalized = sorted({int(cid) for cid in candidate_case_ids})
        cand_json = json.dumps(normalized)
        async with self._write_lock:
            async with self._db.execute(
                "SELECT candidate_ids_json, resolved FROM case_match_ambiguities"
                " WHERE campaign_id = ? AND run_id = ?",
                (campaign_id, run_id),
            ) as cur:
                existing = await cur.fetchone()
            if (
                existing is not None
                and not bool(existing["resolved"])
                and str(existing["candidate_ids_json"]) == cand_json
            ):
                return False
            await self._db.execute(
                """
                INSERT INTO case_match_ambiguities (
                    campaign_id, run_id, candidate_ids_json, resolved, updated_at
                ) VALUES (?, ?, ?, 0, ?)
                ON CONFLICT (campaign_id, run_id) DO UPDATE SET
                    candidate_ids_json = excluded.candidate_ids_json,
                    resolved = 0,
                    updated_at = excluded.updated_at
                """,
                (campaign_id, run_id, cand_json, now),
            )
            await self._db.commit()
            return True

    async def resolve_ambiguity(self, campaign_id: int, run_id: int) -> None:
        now = _now_iso()
        async with self._write_lock:
            await self._db.execute(
                "UPDATE case_match_ambiguities SET resolved = 1, updated_at = ?"
                " WHERE campaign_id = ? AND run_id = ?",
                (now, campaign_id, run_id),
            )
            await self._db.commit()

    async def ambiguities_for_campaign(
        self, campaign_id: int, *, unresolved_only: bool = True
    ) -> list[MatchAmbiguityRecord]:
        sql = "SELECT * FROM case_match_ambiguities WHERE campaign_id = ?"
        if unresolved_only:
            sql += " AND resolved = 0"
        sql += " ORDER BY run_id ASC"
        async with self._db.execute(sql, (campaign_id,)) as cur:
            return [_ambiguity_from_row(row) async for row in cur]

    async def list_runs(
        self, *, cluster_id: str | None = None, limit: int = 1000
    ) -> list[RunRecord]:
        sql = "SELECT * FROM runs"
        args: list[Any] = []
        if cluster_id is not None:
            sql += " WHERE cluster_id = ?"
            args.append(cluster_id)
        sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
        args.append(limit)
        async with self._db.execute(sql, args) as cur:
            return [_run_from_row(row) async for row in cur]

    async def run_facts(self, run_ids: Sequence[int]) -> dict[int, RunExecutionFacts]:
        unique_ids = sorted({int(rid) for rid in run_ids})
        if not unique_ids:
            return {}
        ph = _placeholders(len(unique_ids))

        runs_by_id: dict[int, RunRecord] = {}
        async with self._db.execute(f"SELECT * FROM runs WHERE id IN ({ph})", unique_ids) as cur:
            async for row in cur:
                rec = _run_from_row(row)
                runs_by_id[rec.id] = rec

        jobs_by_run: dict[int, JobRecord] = {}
        async with self._db.execute(
            f"SELECT j.*, r.id AS run_id FROM runs r"
            f" JOIN jobs j ON j.id = r.job_pk WHERE r.id IN ({ph})",
            unique_ids,
        ) as cur:
            async for row in cur:
                job = _job_record(row)
                if job.run_id is not None:
                    jobs_by_run[job.run_id] = job

        solver_by_run: dict[int, RunSolverState] = {}
        async with self._db.execute(
            f"SELECT * FROM run_solver_state WHERE run_id IN ({ph})", unique_ids
        ) as cur:
            async for row in cur:
                st = _run_state_from_row(row)
                solver_by_run[st.run_id] = st

        nan_runs: set[int] = set()
        async with self._db.execute(
            f"SELECT DISTINCT run_id FROM events"
            f" WHERE kind = 'solver.nan_detected' AND run_id IN ({ph})",
            unique_ids,
        ) as cur:
            async for row in cur:
                if row["run_id"] is not None:
                    nan_runs.add(int(row["run_id"]))

        return {
            rid: RunExecutionFacts(
                run=run,
                job=jobs_by_run.get(rid),
                solver_state=solver_by_run.get(rid),
                has_nan_detected=rid in nan_runs,
            )
            for rid, run in runs_by_id.items()
        }


def _project_from_row(row: aiosqlite.Row) -> ProjectRecord:
    return ProjectRecord(
        id=int(row["id"]),
        name=str(row["name"]),
        description=str(row["description"]),
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )


def _campaign_from_row(row: aiosqlite.Row) -> CampaignRecord:
    return CampaignRecord(
        id=int(row["id"]),
        project_id=int(row["project_id"]),
        name=str(row["name"]),
        source_path=row["source_path"],
        sheet_name=row["sheet_name"],
        header_row=int(row["header_row"]),
        case_column=str(row["case_column"]),
        work_dir_column=row["work_dir_column"],
        manual_state_column=row["manual_state_column"],
        work_root=row["work_root"],
        naming_regex=row["naming_regex"],
        columns=tuple(json.loads(row["columns_json"])),
        export_monitors=tuple(json.loads(row["export_monitors_json"])),
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )


def _case_from_row(row: aiosqlite.Row) -> CaseRecord:
    return CaseRecord(
        id=int(row["id"]),
        campaign_id=int(row["campaign_id"]),
        case_key=str(row["case_key"]),
        row_index=row["row_index"],
        parent_case_id=row["parent_case_id"],
        parent_key_raw=row["parent_key_raw"],
        work_dir=row["work_dir"],
        manual_state=row["manual_state"],
        computed_state=CaseComputedState(row["computed_state"]),
        params=json.loads(row["params_json"]),
        active=bool(row["active"]),
        notes=row["notes"],
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )


def _link_from_row(row: aiosqlite.Row) -> CaseRunLink:
    return CaseRunLink(
        id=int(row["id"]),
        campaign_id=int(row["campaign_id"]),
        case_id=int(row["case_id"]),
        run_id=int(row["run_id"]),
        match_kind=MatchKind(row["match_kind"]),
        match_detail=row["match_detail"],
        locked=bool(row["locked"]),
        created_at=datetime.fromisoformat(row["created_at"]),
    )


def _ambiguity_from_row(row: aiosqlite.Row) -> MatchAmbiguityRecord:
    return MatchAmbiguityRecord(
        campaign_id=int(row["campaign_id"]),
        run_id=int(row["run_id"]),
        candidate_case_ids=tuple(int(x) for x in json.loads(row["candidate_ids_json"])),
        resolved=bool(row["resolved"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )


def _run_from_row(row: aiosqlite.Row) -> RunRecord:
    return RunRecord(
        id=int(row["id"]),
        job_pk=row["job_pk"],
        origin=str(row["origin"]),
        cluster_id=row["cluster_id"],
        work_dir=row["work_dir"],
        created_at=datetime.fromisoformat(row["created_at"]),
    )


class SqliteCampaignPlugin(Plugin):
    name = "campaign-sqlite"
    provides = contract.SERVICE_KEY
    contract_version = contract.CONTRACT_VERSION
    inject: ClassVar[Mapping[str, int]] = {
        storage_contract.SERVICE_KEY: storage_contract.CONTRACT_VERSION
    }
    description = "Durable projects, campaigns, cases, and run links on the SQLite connection."

    def apply(self, ctx: Context) -> SqliteCampaignStore:
        storage = ctx.services.require(storage_contract.SERVICE_KEY)
        if not isinstance(storage, SqliteStorage):
            raise TypeError(
                f"{self.name} requires storage-sqlite's SqliteStorage, got {type(storage).__name__}"
            )
        return SqliteCampaignStore(storage.connection, storage.write_lock)
