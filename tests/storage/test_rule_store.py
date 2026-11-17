from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from simmanager.contracts.rules import (
    AlertRaise,
    AlertStatus,
    RuleKind,
    RuleRunState,
    RuleScope,
    RuleSeverity,
    RuleSpec,
)
from simmanager.kernel import Kernel, PluginState
from simmanager.plugins.storage_sqlite import (
    SqliteRulePlugin,
    SqliteRuleStore,
    SqliteStorage,
    SqliteStoragePlugin,
)


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    return str(tmp_path / "db" / "rules.db")


async def test_rule_store_configs_alerts_dedup_and_state(db_path: str) -> None:
    kernel = Kernel({"storage-sqlite": {"path": db_path}})
    await kernel.add(SqliteStoragePlugin, SqliteRulePlugin)
    assert kernel.state("rules-sqlite") is PluginState.ACTIVE

    storage: SqliteStorage = kernel.services.require("storage")
    store: SqliteRuleStore = kernel.services.require("rules")
    run = await storage.create_run(work_dir="/work/user1/case_a", cluster_id="cluster-a")

    cfg = await store.upsert_rule_config(
        RuleSpec(
            scope_type=RuleScope.GLOBAL,
            scope_id=0,
            rule_key="courant_high",
            rule_kind=RuleKind.COURANT_HIGH,
            enabled=True,
            severity=RuleSeverity.WARNING,
            params={"threshold": 1.0, "consecutive": 3},
        )
    )
    assert cfg.params["threshold"] == 1.0

    req = AlertRaise(
        dedup_key=f"run:{run.id}:courant_high",
        rule_key="courant_high",
        rule_kind=RuleKind.COURANT_HIGH,
        severity=RuleSeverity.WARNING,
        title="Courant above 1.0",
        message="courant.max = 1.45",
        cluster_id="cluster-a",
        run_id=run.id,
        details={"value": 1.45},
    )
    alert1, created1 = await store.raise_alert(req)
    assert created1 is True
    assert alert1.status is AlertStatus.OPEN

    # Second raise with same dedup_key updates last_seen without duplicating
    alert2, created2 = await store.raise_alert(req)
    assert created2 is False
    assert alert2.id == alert1.id

    acked = await store.acknowledge_alert(alert1.id, by="user1")
    assert acked is not None
    assert acked.acknowledged_by == "user1"
    assert acked.status is AlertStatus.OPEN

    # Cleared alert allows a future incident with the same dedup_key to open a new row
    cleared = await store.clear_alert(req.dedup_key)
    assert cleared is not None
    assert cleared.status is AlertStatus.CLEARED
    assert cleared.cleared_at is not None

    alert3, created3 = await store.raise_alert(req)
    assert created3 is True
    assert alert3.id != alert1.id

    open_alerts = await store.list_alerts(open_only=True)
    assert [a.id for a in open_alerts] == [alert3.id]

    now = datetime.now(UTC)
    await store.upsert_run_state(
        RuleRunState(
            run_id=run.id,
            rule_key="stall",
            series_tag="",
            last_x=None,
            last_observed_bytes=1024,
            last_growth_at=now,
            growth_intervals_s=(60.0, 90.0, 120.0),
        )
    )
    loaded_state = await store.get_run_state(run.id, "stall")
    assert loaded_state is not None
    assert loaded_state.growth_intervals_s == (60.0, 90.0, 120.0)

    deleted = await store.delete_rule_config(RuleScope.GLOBAL, 0, "courant_high")
    assert deleted is True

    await kernel.stop()
