"""tools/dev_setup.py: term harvesting (synthetic data only) and environment guard."""

from __future__ import annotations

import importlib.util
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("dev_setup", ROOT / "tools" / "dev_setup.py")
assert _spec and _spec.loader
dev_setup = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dev_setup)


def test_harvest_ssh_config() -> None:
    text = """
Host alpha-cluster
    HostName login.alpha.example.org
    User jdoe
    ProxyJump gw@bastion.example.org:2222
Host *
    ForwardX11 yes
"""
    terms = dev_setup.clean(dev_setup.harvest_ssh_config(text))
    assert {"alpha-cluster", "alpha", "jdoe", "login.alpha.example.org", "bastion"} <= terms
    assert "login" not in terms and "*" not in terms and "example" in terms


def test_harvest_sample_text() -> None:
    text = (
        "[jdoe@login07 case_a]$ sacct\n"
        "JobId=1 UserId=otheruser(123) Account=proj_x42\n"
        "#SBATCH --mail-user=first.last@example.org\n"
        "WorkDir=/scratchfs/PROJ_ALPHA/case\n"
        "node9.cluster-beta.local\n"
    )
    terms = dev_setup.clean(dev_setup.harvest_sample_text(text))
    assert {"jdoe", "login07", "otheruser", "proj_x42", "first.last", "first", "last"} <= terms
    assert {"scratchfs", "proj_alpha", "alpha", "cluster-beta", "beta", "node9"} & terms
    assert "cluster" not in terms


def test_harvest_clusters_yaml() -> None:
    data = {
        "clusters": {
            "a": {
                "host": "hpc.alpha.example.org",
                "user": "jdoe",
                "work_roots": ["/wkdir/teamx/sub"],
            }
        }
    }
    terms = dev_setup.clean(dev_setup.harvest_clusters_yaml(data))
    assert {"hpc.alpha.example.org", "alpha", "jdoe", "wkdir", "teamx"} <= terms
    assert dev_setup.harvest_clusters_yaml(None) == set()


def test_clean_drops_generic_and_short() -> None:
    assert dev_setup.clean(["Home", "ab", "1.2.3", "OpenFOAM", "RealName"]) == {"realname"}


def _is_git_checkout() -> bool:
    result = subprocess.run(["git", "rev-parse", "--git-dir"], cwd=ROOT, capture_output=True)
    return result.returncode == 0


@pytest.mark.skipif(bool(os.environ.get("CI")), reason="CI has no private sources")
@pytest.mark.skipif(not _is_git_checkout(), reason="not a git checkout")
def test_dev_environment_is_set_up() -> None:
    """Fails on a fresh clone until `uv run python tools/dev_setup.py` has run."""
    assert dev_setup.hooks_enabled(), "run: uv run python tools/dev_setup.py"
    assert dev_setup.denylist_path().is_file(), "run: uv run python tools/dev_setup.py"
