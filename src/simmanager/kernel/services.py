"""Service registry: capabilities published under stable dotted keys.

Examples: `storage`, `transport`, `scheduler.slurm`, `solver.converge`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .errors import ServiceConflictError, ServiceNotFoundError


@dataclass(frozen=True)
class ServiceEntry:
    key: str
    instance: Any
    provider: str
    contract_version: int


class ServiceRegistry:
    def __init__(self) -> None:
        self._entries: dict[str, ServiceEntry] = {}

    def register(self, entry: ServiceEntry) -> None:
        current = self._entries.get(entry.key)
        if current is not None:
            raise ServiceConflictError(
                f"service {entry.key!r} is already provided by plugin {current.provider!r}"
            )
        self._entries[entry.key] = entry

    def unregister(self, key: str) -> None:
        self._entries.pop(key, None)

    def entry(self, key: str) -> ServiceEntry | None:
        return self._entries.get(key)

    def all_keys(self) -> list[str]:
        return sorted(self._entries)


class ServiceView:
    """Read-only view of the registry given to plugins through their context.

    Consumers depend on the contract (a Protocol in `simmanager.contracts`), so they annotate
    the result themselves: `storage: Storage = ctx.services.require("storage")`.
    """

    def __init__(self, registry: ServiceRegistry) -> None:
        self._registry = registry

    def get(self, key: str) -> Any | None:
        entry = self._registry.entry(key)
        return None if entry is None else entry.instance

    def require(self, key: str) -> Any:
        entry = self._registry.entry(key)
        if entry is None:
            raise ServiceNotFoundError(f"service {key!r} is not available")
        return entry.instance

    def all(self, prefix: str) -> dict[str, Any]:
        """Every service whose key is `prefix` or starts with `prefix.` (e.g. all schedulers)."""
        return {
            key: self._registry.entry(key).instance  # type: ignore[union-attr]
            for key in self._registry.all_keys()
            if key == prefix or key.startswith(prefix + ".")
        }

    def has(self, key: str) -> bool:
        return self._registry.entry(key) is not None
