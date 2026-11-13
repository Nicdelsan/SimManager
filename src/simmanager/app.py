"""Application bootstrap: compose configuration, discover plugins, start the kernel.

Kept outside the kernel so that the kernel stays generic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from . import paths
from .kernel import (
    ComposedConfig,
    ConfigError,
    ConfigLayer,
    DiscoveryResult,
    Kernel,
    Plugin,
    compose,
    discover,
    load_yaml_file,
)
from .kernel.config import format_validation_error
from .plugins import BUILTIN_PLUGINS, DISABLED_BY_DEFAULT


class CoreConfig(BaseModel):
    """Top-level keys of the composed configuration."""

    model_config = ConfigDict(extra="forbid")

    log_level: str = "INFO"
    plugins: dict[str, dict[str, Any]] = Field(default_factory=dict)
    """Per-plugin section, keyed by plugin name. `enabled: false` switches a plugin off."""


@dataclass
class AppSetup:
    config: ComposedConfig
    core: CoreConfig
    discovery: DiscoveryResult
    enabled: list[type[Plugin]]
    disabled: list[str]
    warnings: list[str] = field(default_factory=list)


def find_project_file(project: Path | None) -> Path | None:
    """`--project` may be a directory (containing simmanager.yaml) or the file itself.
    Without it, ./simmanager.yaml is used when present."""
    if project is None:
        candidate = Path.cwd() / paths.PROJECT_CONFIG_FILE
        return candidate if candidate.is_file() else None
    candidate = (
        project if project.suffix in {".yaml", ".yml"} else project / paths.PROJECT_CONFIG_FILE
    )
    if not candidate.is_file():
        raise ConfigError(f"project config not found: {candidate}")
    return candidate


def _schema_defaults(cls: type[Plugin]) -> dict[str, Any]:
    try:
        return cls.config_schema().model_dump(mode="json")
    except ValidationError:
        return {}  # schema has required fields: nothing to show as default


def build_layers(discovery: DiscoveryResult, project_file: Path | None) -> list[ConfigLayer]:
    plugin_defaults = {
        p.name: {"enabled": p.name not in DISABLED_BY_DEFAULT, **_schema_defaults(p.cls)}
        for p in discovery.plugins
    }
    user_file = paths.user_config_file()
    layers = [
        ConfigLayer("defaults", {"log_level": "INFO", "plugins": plugin_defaults}),
        ConfigLayer("user", load_yaml_file(user_file), user_file),
    ]
    if project_file is not None:
        layers.append(ConfigLayer("project", load_yaml_file(project_file), project_file))
    return layers


def prepare(project: Path | None = None) -> AppSetup:
    """Discover plugins and compose the configuration. Raises ConfigError on invalid config."""
    discovery = discover(BUILTIN_PLUGINS)
    composed = compose(build_layers(discovery, find_project_file(project)))
    try:
        core = CoreConfig.model_validate(composed.data)
    except ValidationError as exc:
        raise ConfigError(format_validation_error(exc)) from exc

    warnings = list(discovery.errors)
    known = {p.name for p in discovery.plugins}
    warnings += [
        f"config section plugins.{name} does not match any installed plugin"
        for name in core.plugins
        if name not in known
    ]

    enabled: list[type[Plugin]] = []
    disabled: list[str] = []
    for p in discovery.plugins:
        flag = core.plugins.get(p.name, {}).get("enabled", True)
        if not isinstance(flag, bool):
            raise ConfigError(f"plugins.{p.name}.enabled: must be true or false, got {flag!r}")
        if flag:
            enabled.append(p.cls)
        else:
            disabled.append(p.name)
    return AppSetup(composed, core, discovery, enabled, disabled, warnings)


async def start_kernel(setup: AppSetup) -> Kernel:
    kernel = Kernel(setup.core.plugins)
    await kernel.add(*setup.enabled)
    return kernel
