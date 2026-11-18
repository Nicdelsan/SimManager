"""Display endpoints: library, run now, history images, browser, schedule, live update."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from simmanager.plugins.display import DisplayPlugin
from simmanager.plugins.storage_sqlite import SqliteDisplayPlugin, SqliteStoragePlugin
from tests.display_fakes import PNG, WORK, C, FakeTransport, ScriptSession, transport_plugin

from .webclient import Web, recv_type, running_web, ws_connect


@pytest.fixture
async def setup(tmp_path: Path) -> AsyncIterator[tuple[Web, ScriptSession, int]]:
    root = tmp_path / "case_a"
    root.mkdir()
    session = ScriptSession(root)
    config = {
        "storage-sqlite": {"path": str(tmp_path / "sim.db")},
        "display": {"max_image_mb": 0.001},
    }
    plugins = (
        SqliteStoragePlugin,
        SqliteDisplayPlugin,
        transport_plugin(FakeTransport(session)),
        DisplayPlugin,
    )
    async with running_web(*plugins, config=config) as web:
        run = await web.kernel.services.require("storage").create_run(work_dir=WORK, cluster_id=C)
        yield web, session, run.id


async def _get(web: Web, path: str) -> Any:
    async with web.client() as c:
        r = await c.get(path)
    assert r.status_code == 200, r.text
    return r.json()


async def test_save_run_and_show_the_new_image(setup: tuple[Web, ScriptSession, int]) -> None:
    web, session, run_id = setup
    saved = await web.post_json(
        "/api/display/scripts",
        {"name": "slice", "body": "print(1)\n", "setup": "module load python", "message": "v1"},
    )
    assert saved.status_code == 200, saved.text
    version = saved.json()["version"]
    assert saved.json()["created"] and version["number"] == 1
    again = await web.post_json("/api/display/scripts", {"name": "slice", "body": "print(2)\n"})
    v2 = again.json()["version"]
    assert v2["number"] == 2
    diff = await _get(web, f"/api/display/diff?old={version['id']}&new={v2['id']}")
    assert "+print(2)" in diff["diff"]
    [script] = await _get(web, "/api/display/scripts")
    assert script["latest"]["number"] == 2
    assert len(await _get(web, f"/api/display/scripts/{script['id']}/versions")) == 2

    session.produce = {"post/p.png": PNG}
    async with ws_connect(web) as ws:
        r = await web.post_json(f"/api/runs/{run_id}/display/run", {"script_id": script["id"]})
        assert r.status_code == 202, r.text
        done = await recv_type(ws, "display.updated")
        while done["status"] == "running":
            done = await recv_type(ws, "display.updated")
    assert done["status"] == "ok"

    panel = await _get(web, f"/api/runs/{run_id}/display")
    [execution] = panel["executions"]
    assert execution["version_number"] == 2 and execution["trigger"] == "manual"
    [image] = execution["images"]
    async with web.client() as c:
        img = await c.get(f"/api/display/images/{image['id']}")
    assert img.status_code == 200 and img.content == PNG
    assert img.headers["content-type"] == "image/png"
    assert "default-src 'self'" in img.headers["content-security-policy"]


async def test_browser_and_size_limit(setup: tuple[Web, ScriptSession, int]) -> None:
    web, session, run_id = setup
    (session.root / "post").mkdir()
    (session.root / "post" / "big.gif").write_bytes(b"G" * 5000)
    entries = await _get(web, f"/api/runs/{run_id}/display/files?path=post")
    assert [e["path"] for e in entries] == ["post/big.gif"]
    async with web.client() as c:
        big = await c.get(f"/api/runs/{run_id}/display/file", params={"path": "post/big.gif"})
        assert big.status_code == 413 and big.json()["detail"]["size"] == 5000
        forced = await c.get(
            f"/api/runs/{run_id}/display/file", params={"path": "post/big.gif", "force": "true"}
        )
        assert forced.status_code == 200 and len(forced.content) == 5000
        escape = await c.get(f"/api/runs/{run_id}/display/files", params={"path": "../.."})
        assert escape.status_code == 422
        missing = await c.get("/api/runs/999/display")
        assert missing.status_code == 404


async def test_schedule_and_refusals(setup: tuple[Web, ScriptSession, int]) -> None:
    web, _session, run_id = setup
    saved = await web.post_json("/api/display/scripts", {"name": "ts", "body": "x=1"})
    script_id = saved.json()["version"]["script_id"]
    r = await web.post_json(
        f"/api/runs/{run_id}/display/schedule",
        {"script_id": script_id, "interval_s": 600, "enabled": True},
    )
    assert r.status_code == 200, r.text
    panel = await _get(web, f"/api/runs/{run_id}/display")
    assert panel["schedules"][0]["interval_s"] == 600
    too_fast = await web.post_json(
        f"/api/runs/{run_id}/display/schedule",
        {"script_id": script_id, "interval_s": 5, "enabled": True},
    )
    assert too_fast.status_code == 422
    bad = await web.post_json(
        "/api/display/scripts", {"name": "x", "body": "1", "interpreter": "python3 && rm -rf ~"}
    )
    assert bad.status_code == 422
    async with web.client() as c:  # no Origin: refused like every state change
        r2 = await c.post(f"/api/runs/{run_id}/display/run", json={"script_id": script_id})
        assert r2.status_code == 403
