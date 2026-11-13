""" "Cancel job": nothing runs without prepare + confirm; then run_mutating and its events."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from simmanager.contracts.scheduler import JobInfo, JobState
from simmanager.plugins.scheduler_slurm import SlurmSchedulerPlugin
from simmanager.plugins.storage_sqlite import SqliteStoragePlugin
from simmanager.plugins.transport_ssh import SshTransportPlugin
from simmanager.plugins.ui_web.actions import PendingActions

from ..transport.sshserver import SshTestServer
from .test_terminal import C, _clusters_file, _config, ssh  # noqa: F401 (fixture)
from .webclient import Web, running_web

PREPARE = "/api/actions/job-cancel/prepare"


@pytest.fixture
async def web(tmp_path: Path, ssh: SshTestServer) -> AsyncIterator[Web]:  # noqa: F811
    config = _config(tmp_path, _clusters_file(tmp_path, ssh))
    plugins = (SqliteStoragePlugin, SshTransportPlugin, SlurmSchedulerPlugin)
    async with running_web(*plugins, config=config) as w:
        storage = w.kernel.services.require("storage")
        for job_id, active in (("77", True), ("78", False)):
            await storage.upsert_job(
                C,
                JobInfo(
                    job_id=job_id,
                    name=f"case-{job_id}",
                    state=JobState.RUNNING if active else JobState.COMPLETED,
                    raw_state="RUNNING" if active else "COMPLETED",
                    work_dir="/work/case1",
                ),
                active=active,
            )
        yield w


async def _kinds(web: Web) -> list[str]:
    return [e.kind for e in await web.kernel.services.require("storage").events()]


async def test_cancel_needs_prepare_and_confirm(web: Web, ssh: SshTestServer) -> None:  # noqa: F811
    ssh.commands["scancel 77"] = ("", "", 0)
    assert (await web.post_json("/api/actions/forged/confirm")).status_code == 404

    prepared = await web.post_json(PREPARE, {"cluster_id": C, "job_id": "77"})
    assert prepared.status_code == 200, prepared.text
    info = prepared.json()
    assert (info["job_id"], info["name"], info["cluster_id"]) == ("77", "case-77", C)
    assert "action.executed" not in await _kinds(web)  # preparing changes nothing
    assert not any(c.startswith("scancel") for c in ssh.executed)

    done = await web.post_json(f"/api/actions/{info['action_id']}/confirm")
    assert done.status_code == 200, done.text
    assert done.json()["exit_status"] == 0
    assert ssh.executed.count("scancel 77") == 1
    events = {e.kind: e for e in await web.kernel.services.require("storage").events()}
    assert events["action.executed"].payload["argv"] == ["scancel", "77"]
    assert events["action.executed"].payload["approved_by"] == "web"
    assert events["action.result"].payload["exit_status"] == 0

    again = await web.post_json(f"/api/actions/{info['action_id']}/confirm")
    assert again.status_code == 404  # single use
    assert ssh.executed.count("scancel 77") == 1


async def test_cancel_refuses_unknown_inactive_and_forged_requests(web: Web) -> None:
    for body, status in (
        ({"cluster_id": C, "job_id": "78"}, 404),  # finished: not active
        ({"cluster_id": C, "job_id": "999"}, 404),
        ({"cluster_id": "nope", "job_id": "77"}, 404),
        ({"cluster_id": C, "job_id": "77; rm -rf ~"}, 422),
        ({"cluster_id": C}, 422),
    ):
        r = await web.post_json(PREPARE, body)
        assert r.status_code == status, (body, r.text)
    async with web.client() as c:  # no Origin: refused like every state change
        r = await c.post(PREPARE, json={"cluster_id": C, "job_id": "77"})
        assert r.status_code == 403
    assert "action.executed" not in await _kinds(web)


def test_pending_actions_expire_and_are_single_use() -> None:
    now = [0.0]
    pending = PendingActions(ttl_s=60, clock=lambda: now[0])
    a, b = pending.add("c", "1", "x"), pending.add("c", "2", "y")
    assert pending.take(a) is not None and pending.take(a) is None
    now[0] = 61
    assert pending.take(b) is None
