"""SLURM output parsers on anonymized fixtures."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from simmanager.contracts.scheduler import JobState
from simmanager.plugins.scheduler_slurm.parse import (
    expand_stdout,
    normalize_state,
    parse_duration,
    parse_sacct,
    parse_scontrol,
    parse_scontrol_records,
    parse_squeue,
    parse_time,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "slurm"
ROME = ZoneInfo("Europe/Rome")


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("raw", "state"),
    [
        ("RUNNING", JobState.RUNNING),
        ("PENDING", JobState.QUEUED),
        ("PD", JobState.QUEUED),
        ("REQUEUE_HOLD", JobState.QUEUED),
        ("CONFIGURING", JobState.RUNNING),
        ("COMPLETING", JobState.COMPLETING),
        ("COMPLETED", JobState.COMPLETED),
        ("CANCELLED by 1001", JobState.CANCELLED),
        ("CANCELLED+", JobState.CANCELLED),
        ("TIMEOUT", JobState.TIMEOUT),
        ("OUT_OF_MEMORY", JobState.FAILED),
        ("NODE_FAIL", JobState.FAILED),
        ("whatever", JobState.UNKNOWN),
    ],
)
def test_normalize_state(raw: str, state: JobState) -> None:
    assert normalize_state(raw) is state


@pytest.mark.parametrize(
    ("text", "seconds"),
    [
        ("1-12:17:07", 130627),
        ("02:47:34", 10054),
        ("15:02", 902),
        ("30", 1800),
        ("365-00:00:00", 365 * 86400),
        ("2-06", 2 * 86400 + 6 * 3600),
        ("UNLIMITED", None),
        ("N/A", None),
        ("", None),
    ],
)
def test_parse_duration(text: str, seconds: int | None) -> None:
    assert parse_duration(text) == seconds


def test_parse_time_uses_cluster_zone() -> None:
    t = parse_time("2026-07-01T12:00:00", ROME)
    assert t is not None and t.utcoffset() == timedelta(hours=2)
    assert parse_time("Unknown", ROME) is None
    local = parse_time("2026-07-01T12:00:00", None)
    assert local is not None and local.tzinfo is not None


def test_squeue() -> None:
    jobs = {j.job_id: j for j in parse_squeue(fixture("squeue.txt"), ROME)}
    assert set(jobs) == {"18105", "18120", "58444781_35", "18121"}  # malformed line skipped

    running = jobs["18105"]
    assert running.state is JobState.RUNNING
    assert running.node_list == "node02" and running.reason is None
    assert running.elapsed_s == 130627 and running.time_limit_s == 365 * 86400
    assert running.start_time == datetime(2026, 11, 12, 3, 5, 26, tzinfo=ROME)
    assert running.work_dir == "/work/user1/spray/case_a"

    queued = jobs["18120"]
    assert queued.state is JobState.QUEUED and queued.reason == "Resources"
    assert queued.node_list is None and queued.start_time is None
    assert queued.work_dir == "/work/user1/spray/case b" and queued.nodes == 2

    held = jobs["58444781_35"]
    assert held.reason == "job_requeued_in_held_state" and held.time_limit_s is None
    assert jobs["18121"].state is JobState.COMPLETING


def test_scontrol_records_keep_free_text() -> None:
    rec = parse_scontrol_records(fixture("scontrol_25_array_held.txt"))[0]
    assert rec["Comment"].startswith("BUDGET 01:53:47 prolog controller: insufficient")
    assert rec["ReqTRES"] == "cpu=8,mem=120G,node=1,billing=8,gres/gpu=1"
    assert rec["AllocNode:Sid"] == "login01:12345"
    assert rec["NodeList"] == ""


def test_scontrol_18() -> None:
    running, done = parse_scontrol(fixture("scontrol_18_running.txt"), ROME)
    assert running.job_id == "18105" and running.user == "user1"
    assert running.state is JobState.RUNNING
    assert running.end_time is None  # EndTime of a running job is only the deadline
    assert running.elapsed_s == 130627
    assert running.stdout_path == "/work/user1/spray/case_a/spray_case_a_0_18105.Qtxt"

    assert done.state is JobState.COMPLETED and done.exit_code == "0:0"
    assert done.work_dir == "/work/user1/test/case b"
    assert done.end_time == datetime(2026, 11, 13, 15, 22, 33, tzinfo=ROME)


def test_scontrol_25_arrays() -> None:
    task, task_range = parse_scontrol(fixture("scontrol_25_array_held.txt"), ROME)
    assert task.job_id == "58444781_35"
    assert task.state is JobState.QUEUED and task.reason == "job_requeued_in_held_state"
    assert task.start_time is None and task.node_list is None
    assert task.stdout_path == "/scratch/user2/sweep/logs/58444781_35.out"
    assert task_range.job_id == "58444781_[40-44%2]"


def test_sacct() -> None:
    jobs = {j.job_id: j for j in parse_sacct(fixture("sacct.txt"), UTC)}
    assert jobs["18115"].state is JobState.COMPLETED
    assert jobs["18115"].work_dir == "/work/user1/test/case_b"
    assert jobs["18116"].state is JobState.FAILED and jobs["18116"].exit_code == "1:0"
    assert jobs["18117"].state is JobState.CANCELLED
    assert jobs["18117"].raw_state == "CANCELLED by 1001"
    assert jobs["18117"].time_limit_s is None
    assert jobs["18118"].state is JobState.TIMEOUT
    assert jobs["18119"].state is JobState.FAILED and jobs["18119"].raw_state == "OUT_OF_MEMORY"
    assert jobs["18200_3"].state is JobState.FAILED
    assert jobs["18118"].end_time == datetime(2026, 11, 13, 12, 0, tzinfo=UTC)


def test_expand_stdout() -> None:
    rec = {"JobId": "7", "JobName": "nm", "UserId": "u(1)", "ArrayJobId": "5", "ArrayTaskId": "2"}
    assert expand_stdout("/d/%x_%j_%u_%A_%a_%3a_%%.o", rec) == "/d/nm_7_u_5_2_002_%.o"
    assert expand_stdout("/plain/path", rec) == "/plain/path"
    assert expand_stdout("(null)", rec) is None
