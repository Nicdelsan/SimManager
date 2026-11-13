"""Bootstrap and CLI, end to end with the real built-in plugins."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from simmanager import paths
from simmanager.app import prepare, start_kernel
from simmanager.cli.main import app
from simmanager.kernel import ConfigError, PluginState

runner = CliRunner()


def _write_user_config(text: str) -> None:
    paths.config_dir().mkdir(parents=True, exist_ok=True)
    paths.user_config_file().write_text(text, encoding="utf-8")


def test_defaults_enable_storage_disable_examples() -> None:
    setup = prepare()
    assert {"storage-sqlite", "timeseries-sqlite"} <= {c.name for c in setup.enabled}
    assert set(setup.disabled) == {
        "example-clock",
        "example-ticker",
        "ui-web",
        "prompter-web",
        "terminal",
    }
    assert setup.config.origins[("plugins", "storage-sqlite", "enabled")] == "defaults"


def test_user_and_project_layers(tmp_path: Path) -> None:
    _write_user_config("plugins:\n  example-clock: {enabled: true, interval_s: 3}\n")
    project = tmp_path / "proj"
    project.mkdir()
    (project / "simmanager.yaml").write_text(
        "plugins:\n  example-clock: {interval_s: 9}\nlog_level: DEBUG\n", encoding="utf-8"
    )
    setup = prepare(project)
    clock = setup.core.plugins["example-clock"]
    assert clock == {"enabled": True, "interval_s": 9}
    assert setup.config.origins[("plugins", "example-clock", "enabled")] == "user"
    assert setup.config.origins[("plugins", "example-clock", "interval_s")] == "project"
    assert setup.core.log_level == "DEBUG"


def test_unknown_top_level_key_is_an_error() -> None:
    _write_user_config("pluginz: {}\n")
    with pytest.raises(ConfigError, match="pluginz"):
        prepare()


def test_missing_explicit_project_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        prepare(tmp_path / "nope")


def test_unknown_plugin_section_warns() -> None:
    _write_user_config("plugins:\n  not-installed: {}\n")
    assert any("not-installed" in w for w in prepare().warnings)


async def test_examples_run_end_to_end() -> None:
    _write_user_config(
        "plugins:\n"
        "  example-clock: {enabled: true, interval_s: 0.01}\n"
        "  example-ticker: {enabled: true}\n"
    )
    kernel = await start_kernel(prepare())
    states = {i.name: i.state for i in kernel.info()}
    assert set(states.values()) == {PluginState.ACTIVE}
    assert {
        "storage-sqlite",
        "timeseries-sqlite",
        "solver-ingest",
        "monitor",
        "example-clock",
        "example-ticker",
    } <= set(states)
    storage = kernel.services.require("storage")
    import asyncio

    for _ in range(100):
        if await storage.events(kinds=["example.tick"]):
            break
        await asyncio.sleep(0.01)
    assert await storage.events(kinds=["example.tick"])
    await kernel.remove("example-clock")
    assert kernel.state("example-ticker") is PluginState.PENDING
    await kernel.stop()


def test_cli_plugins_and_config() -> None:
    result = runner.invoke(app, ["plugins"])
    assert result.exit_code == 0, result.output
    assert "storage-sqlite" in result.output and "active" in result.output
    assert "disabled" in result.output

    result = runner.invoke(app, ["config", "show", "--origin"])
    assert result.exit_code == 0, result.output
    assert "plugins.storage-sqlite.enabled" in result.output

    result = runner.invoke(app, ["events"])
    assert result.exit_code == 0, result.output


def test_cli_reports_config_errors() -> None:
    _write_user_config("log_level: [oops\n")
    result = runner.invoke(app, ["plugins"])
    assert result.exit_code == 2
    assert "configuration error" in result.output


@pytest.mark.parametrize(
    ("prompter", "web", "expected"),
    [
        (None, False, {"prompter-cli"}),
        (None, True, {"ui-web", "prompter-web", "terminal"}),
        ("web", False, {"prompter-cli"}),  # no web server outside `serve`
        ("cli", True, {"ui-web", "prompter-cli", "terminal"}),
    ],
)
def test_web_plugins_and_prompter_selection(
    prompter: str | None, web: bool, expected: set[str]
) -> None:
    if prompter:
        _write_user_config(f"prompter: {prompter}\n")
    setup = prepare(web=web)
    names = {c.name for c in setup.enabled}
    assert names & {"ui-web", "prompter-web", "prompter-cli", "terminal"} == expected
