from __future__ import annotations

import pytest

from simmanager.kernel import Kernel, ServiceNotFoundError

from .helpers import make_plugin


async def test_all_by_prefix_and_optional_get() -> None:
    journal: list[str] = []
    kernel = Kernel()
    await kernel.add(
        make_plugin("slurm", journal, provides="scheduler.slurm"),
        make_plugin("pbs", journal, provides="scheduler.pbs"),
        make_plugin("sched-ui", journal, provides="schedulerui"),
    )
    assert sorted(kernel.services.all("scheduler")) == ["scheduler.pbs", "scheduler.slurm"]
    assert kernel.services.get("scheduler.lsf") is None
    with pytest.raises(ServiceNotFoundError):
        kernel.services.require("scheduler.lsf")
    await kernel.stop()
    assert kernel.services.all("scheduler") == {}
