"""Cluster profiles: how to reach a cluster and how to poll it.

Profiles live in `clusters.yaml` in the user config dir (never in the repository). They are
data shared by the `transport`, `scheduler` and `monitor` services and by the CLI.
"""

from __future__ import annotations

from datetime import tzinfo
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator

AuthMethod = Literal["publickey", "keyboard-interactive", "password"]
DEFAULT_AUTH: tuple[AuthMethod, ...] = ("publickey", "keyboard-interactive", "password")


class PollConfig(BaseModel):
    """Adaptive polling interval of the monitor (seconds)."""

    model_config = ConfigDict(extra="forbid")

    running_s: float = Field(default=60, gt=0)
    """Used while at least one job is running."""
    queued_s: float = Field(default=180, gt=0)
    """Used while jobs are only queued."""
    idle_s: float = Field(default=300, gt=0)
    """Used when no job is known (and while the cluster is unreachable)."""
    jitter: float = Field(default=0.1, ge=0, lt=1)
    """Random +/- fraction applied to every interval."""


class ClusterProfile(BaseModel):
    """One entry under `clusters:` in clusters.yaml. Unset fields come from ~/.ssh/config."""

    model_config = ConfigDict(extra="forbid")

    ssh_config_host: str | None = None
    """Host alias in the OpenSSH config; HostName/User/Port/IdentityFile/ProxyJump inherited."""
    ssh_config_file: str | None = None
    """OpenSSH config file to read. Default: ~/.ssh/config when it exists."""
    host: str | None = None
    port: int | None = None
    user: str | None = None
    key_files: list[str] = Field(default_factory=list)
    """Private keys. When set, only these are offered (no SSH agent). A certificate
    `<key>-cert.pub` next to a key is used automatically. Bastions: use ProxyJump in the
    SSH config."""
    known_hosts: str | None = None
    """known_hosts file. Default: ~/.ssh/known_hosts. Host keys are always verified."""
    auth: list[AuthMethod] = Field(default_factory=lambda: list(DEFAULT_AUTH))
    """Allowed SSH authentication methods, in order of preference."""
    cert_renew_command: list[str] | None = None
    """Local command that renews an expired SSH certificate (e.g. a `step ssh certificate`
    call). Run only after the user confirms it."""

    scheduler: Literal["slurm"] = "slurm"
    timezone: str | None = None
    """IANA zone of the cluster's timestamps (e.g. Europe/Rome). Default: this machine's."""
    poll: PollConfig = Field(default_factory=PollConfig)
    work_roots: list[str] = Field(default_factory=list)

    max_channels: int = Field(default=6, ge=1, le=64)
    """Concurrent command channels of the monitor on the single connection. Together with
    the SFTP channel and `max_terminals` it should stay within sshd `MaxSessions`
    (usually 10)."""
    max_terminals: int = Field(default=3, ge=0, le=16)
    """Interactive terminals (M4) per cluster, on their own channels: never the monitor's."""
    terminal_tmux: bool = False
    """Open new terminals inside a new tmux session on the cluster (when tmux exists)."""
    keepalive_s: float = Field(default=30, gt=0)
    connect_timeout_s: float = Field(default=20, gt=0)
    compression: bool = True

    @field_validator("timezone")
    @classmethod
    def _known_zone(cls, value: str | None) -> str | None:
        if value is not None:
            try:
                ZoneInfo(value)
            except (ZoneInfoNotFoundError, ValueError) as exc:
                raise ValueError(f"unknown time zone {value!r}") from exc
        return value

    def channel_budget(self) -> int:
        """SSH channels this profile may open at once: commands + SFTP + terminals."""
        return self.max_channels + 1 + self.max_terminals

    def tz(self) -> tzinfo | None:
        """Zone for naive cluster timestamps; None means this machine's local zone."""
        return ZoneInfo(self.timezone) if self.timezone else None

    def target(self) -> str:
        """Human-readable destination, for listings and messages."""
        host = self.host or self.ssh_config_host or "?"
        user = f"{self.user}@" if self.user else ""
        port = f":{self.port}" if self.port else ""
        return f"{user}{host}{port}"


class ClustersFile(BaseModel):
    """Root of clusters.yaml."""

    model_config = ConfigDict(extra="forbid")

    clusters: dict[str, ClusterProfile] = Field(default_factory=dict)
