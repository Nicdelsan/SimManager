"""Plugin discovery: built-in plugins plus the `simmanager.plugins` entry point group.

An external plugin is a normal pip package declaring, in its pyproject.toml:

    [project.entry-points."simmanager.plugins"]
    my-plugin = "my_package.plugin:MyPlugin"      # a Plugin subclass, or a list of them
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from importlib.metadata import EntryPoint, entry_points

from .plugin import Plugin

ENTRY_POINT_GROUP = "simmanager.plugins"

log = logging.getLogger("simmanager.kernel.discovery")


@dataclass(frozen=True)
class DiscoveredPlugin:
    cls: type[Plugin]
    source: str  # "builtin" or "<distribution>:<entry point name>"

    @property
    def name(self) -> str:
        return self.cls.name


@dataclass
class DiscoveryResult:
    plugins: list[DiscoveredPlugin] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def classes(self) -> list[type[Plugin]]:
        return [p.cls for p in self.plugins]


def discover(
    builtins: Sequence[type[Plugin]] = (),
    *,
    group: str = ENTRY_POINT_GROUP,
    load_entry_points: Callable[[str], Iterable[EntryPoint]] | None = None,
) -> DiscoveryResult:
    """Collect plugins. A broken entry point is reported in `errors`, never raised."""
    result = DiscoveryResult()
    seen: dict[str, str] = {}

    def add(cls: type[Plugin], source: str) -> None:
        name = getattr(cls, "name", None)
        if not isinstance(name, str) or not name:
            result.errors.append(f"{source}: {cls!r} has no plugin name")
            return
        if name in seen:
            result.errors.append(
                f"{source}: plugin name {name!r} already provided by {seen[name]}; ignored"
            )
            return
        seen[name] = source
        result.plugins.append(DiscoveredPlugin(cls, source))

    for cls in builtins:
        add(cls, "builtin")

    eps = (load_entry_points or _default_entry_points)(group)
    for ep in eps:
        dist = ep.dist.name if ep.dist is not None else "?"
        source = f"{dist}:{ep.name}"
        try:
            obj = ep.load()
        except Exception as exc:
            log.exception("cannot load entry point %s", source)
            result.errors.append(f"{source}: import failed: {type(exc).__name__}: {exc}")
            continue
        candidates = obj if isinstance(obj, (list, tuple)) else [obj]
        for candidate in candidates:
            if isinstance(candidate, type) and issubclass(candidate, Plugin):
                add(candidate, source)
            else:
                result.errors.append(f"{source}: {candidate!r} is not a Plugin subclass")
    return result


def _default_entry_points(group: str) -> Iterable[EntryPoint]:
    return entry_points(group=group)
