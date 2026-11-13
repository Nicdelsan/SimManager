"""Layered YAML configuration: later layers override earlier ones (deep merge of mappings)."""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from .errors import ConfigError


@dataclass(frozen=True)
class ConfigLayer:
    name: str  # "defaults", "user", "project"
    data: Mapping[str, Any]
    path: Path | None = None


@dataclass
class ComposedConfig:
    data: dict[str, Any]
    origins: dict[tuple[str, ...], str] = field(default_factory=dict)
    """Leaf key path -> name of the layer that set it."""
    layers: list[ConfigLayer] = field(default_factory=list)


def load_yaml_file(path: Path) -> dict[str, Any]:
    """Read a YAML mapping. A missing or empty file is an empty mapping."""
    if not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: invalid YAML: {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: top level must be a mapping, got {type(data).__name__}")
    return data


def deep_merge(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    """Merge mappings recursively. Non-mapping values (lists included) are replaced."""
    out: dict[str, Any] = dict(base)
    for key, value in override.items():
        current = out.get(key)
        if isinstance(current, Mapping) and isinstance(value, Mapping):
            out[key] = deep_merge(current, value)
        else:
            out[key] = value
    return out


def compose(layers: Sequence[ConfigLayer]) -> ComposedConfig:
    merged: dict[str, Any] = {}
    origins: dict[tuple[str, ...], str] = {}
    for layer in layers:
        merged = deep_merge(merged, layer.data)
        for path in _leaf_paths(layer.data):
            # drop origins of keys that this layer replaced with a non-mapping value
            for existing in [p for p in origins if p[: len(path)] == path and p != path]:
                del origins[existing]
            origins[path] = layer.name
    return ComposedConfig(data=merged, origins=origins, layers=list(layers))


def _leaf_paths(data: Mapping[str, Any], prefix: tuple[str, ...] = ()) -> Iterator[tuple[str, ...]]:
    for key, value in data.items():
        path = (*prefix, str(key))
        if isinstance(value, Mapping) and value:
            yield from _leaf_paths(value, path)
        else:
            yield path


def format_validation_error(exc: ValidationError, prefix: str = "") -> str:
    """One line per error, with the full dotted key path: `plugins.monitor.poll_interval_s: ...`."""
    lines = []
    for err in exc.errors():
        loc = ".".join(str(part) for part in err["loc"])
        where = f"{prefix}.{loc}" if prefix and loc else (prefix or loc or "<root>")
        lines.append(f"{where}: {err['msg']}")
    return "; ".join(lines)
