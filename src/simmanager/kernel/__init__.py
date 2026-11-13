"""Minimal plugin kernel (concepts from Cordis: context, services, reactive DI, effects, events)."""

from .config import ComposedConfig, ConfigLayer, compose, deep_merge, load_yaml_file
from .context import Context
from .discovery import ENTRY_POINT_GROUP, DiscoveredPlugin, DiscoveryResult, discover
from .effects import Disposer
from .errors import (
    Blocked,
    ConfigError,
    ContextClosedError,
    ContractVersionError,
    KernelError,
    ServiceConflictError,
    ServiceNotFoundError,
)
from .events import EventBus
from .kernel import PLUGIN_STATE_EVENT, Kernel, PluginInfo
from .plugin import NoConfig, Plugin, PluginState
from .services import ServiceView

__all__ = [
    "ENTRY_POINT_GROUP",
    "PLUGIN_STATE_EVENT",
    "Blocked",
    "ComposedConfig",
    "ConfigError",
    "ConfigLayer",
    "Context",
    "ContextClosedError",
    "ContractVersionError",
    "DiscoveredPlugin",
    "DiscoveryResult",
    "Disposer",
    "EventBus",
    "Kernel",
    "KernelError",
    "NoConfig",
    "Plugin",
    "PluginInfo",
    "PluginState",
    "ServiceConflictError",
    "ServiceNotFoundError",
    "ServiceView",
    "compose",
    "deep_merge",
    "discover",
    "load_yaml_file",
]
