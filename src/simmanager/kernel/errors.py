"""Exceptions raised by the kernel."""

from __future__ import annotations


class KernelError(Exception):
    """Base class for all kernel errors."""


class ConfigError(KernelError):
    """Invalid or unreadable configuration."""


class ServiceConflictError(KernelError):
    """Two plugins tried to provide the same service key."""


class ContractVersionError(KernelError):
    """A consumer requires a contract version that the provider does not offer."""


class ServiceNotFoundError(KernelError):
    """A required service is not registered."""


class ContextClosedError(KernelError):
    """A plugin used its context after being deactivated."""


class Blocked(Exception):  # noqa: N818 - reads naturally as `raise Blocked("reason")`
    """Raised by a waterfall handler to stop the chain and reject the payload."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason
