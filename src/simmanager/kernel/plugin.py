"""Plugin base class and lifecycle states."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from enum import StrEnum
from typing import TYPE_CHECKING, Any, ClassVar

from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from .context import Context


class PluginState(StrEnum):
    PENDING = "pending"  # waiting for required services
    LOADING = "loading"  # apply() running
    ACTIVE = "active"
    UNLOADING = "unloading"  # effects being disposed
    DISPOSED = "disposed"
    FAILED = "failed"  # apply() raised, a task crashed, or config/contract invalid


class NoConfig(BaseModel):
    """Default config schema: no options."""

    model_config = ConfigDict(extra="forbid")


class Plugin(ABC):
    """Base class for every plugin.

    A fresh instance is created at each activation, so instance attributes never leak
    between activations. Durable state (jobs, runs, samples) belongs in storage, never here.
    """

    name: ClassVar[str]
    """Unique plugin name, kebab-case (`scheduler-slurm`). Also its config section key."""

    provides: ClassVar[str | None] = None
    """Service key published by this plugin, if any (`scheduler.slurm`)."""

    contract_version: ClassVar[int] = 1
    """Major version of the contract implemented by the provided service."""

    inject: ClassVar[Mapping[str, int]] = {}
    """Required services: key -> required contract major version."""

    config_schema: ClassVar[type[BaseModel]] = NoConfig

    description: ClassVar[str] = ""

    @abstractmethod
    def apply(self, ctx: Context) -> Any:
        """Set up the plugin (sync or async).

        If `provides` is set, return the service instance: the kernel registers it and
        withdraws it automatically on deactivation.
        """
