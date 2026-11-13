"""`simman cluster | monitor | jobs` commands (no network)."""

from __future__ import annotations

import yaml
from typer.testing import CliRunner

from simmanager import paths
from simmanager.cli.main import app, fmt_duration, split_command
from simmanager.plugins.transport_ssh import load_clusters

runner = CliRunner()


def test_cluster_add_and_list() -> None:
    result = runner.invoke(
        app,
        [
            "cluster", "add", "uni-a", "--ssh-host", "uni-a-login", "--timezone", "Europe/Rome",
            "--key", "~/.ssh/id_test",
            "--renew-command", r'step ssh certificate "a b" C:\keys\cert --force',
        ],
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    profile = load_clusters(paths.clusters_file())["uni-a"]
    assert profile.ssh_config_host == "uni-a-login" and profile.timezone == "Europe/Rome"
    assert profile.cert_renew_command == ["step", "ssh", "certificate", "a b", r"C:\keys\cert",
                                          "--force"]  # fmt: skip
    raw = yaml.safe_load(paths.clusters_file().read_text(encoding="utf-8"))
    assert "poll" not in raw["clusters"]["uni-a"]  # defaults are not written out

    again = runner.invoke(app, ["cluster", "add", "uni-a", "--host", "x"])
    assert again.exit_code == 1 and "already exists" in again.output

    listing = runner.invoke(app, ["cluster", "list"])
    assert listing.exit_code == 0 and "uni-a" in listing.output


def test_cluster_add_rejects_bad_input() -> None:
    assert runner.invoke(app, ["cluster", "add", "x y", "--host", "h"]).exit_code == 2
    assert runner.invoke(app, ["cluster", "add", "c"]).exit_code == 2  # no host
    bad_zone = runner.invoke(app, ["cluster", "add", "c", "--host", "h", "--timezone", "Mars/X"])
    assert bad_zone.exit_code == 2 and "time zone" in bad_zone.output


def test_unknown_cluster_test() -> None:
    result = runner.invoke(app, ["cluster", "test", "nope"])
    assert result.exit_code == 1 and "unknown cluster" in result.output


def test_monitor_once_and_jobs_list_without_clusters() -> None:
    result = runner.invoke(app, ["monitor", "--once"])
    assert result.exit_code == 0, result.output
    jobs = runner.invoke(app, ["jobs", "list", "--all"])
    assert jobs.exit_code == 0 and "no jobs recorded yet" in jobs.output


def test_helpers() -> None:
    assert split_command("a 'b c' d") == ["a", "b c", "d"]
    assert fmt_duration(130627) == "1-12:17:07"
    assert fmt_duration(None) == ""
