"""`transport` service over SSH (asyncssh)."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import ClassVar

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError

from simmanager import paths
from simmanager.contracts import auth as auth_contract
from simmanager.contracts import transport as contract
from simmanager.contracts.clusters import ClusterProfile, ClustersFile
from simmanager.kernel import ConfigError, Context, Plugin
from simmanager.kernel.config import format_validation_error

from .transport import SshSession, SshTransport


class TransportConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    clusters_file: str | None = None
    """Cluster profiles. Default: clusters.yaml in the user config dir."""


def load_clusters(path: Path) -> dict[str, ClusterProfile]:
    """Read clusters.yaml; a missing file means no clusters. Raises ConfigError if invalid."""
    if not path.is_file():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return ClustersFile.model_validate(data).clusters
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: invalid YAML: {exc}") from exc
    except ValidationError as exc:
        raise ConfigError(f"{path}:\n{format_validation_error(exc)}") from exc


class SshTransportPlugin(Plugin):
    name = "transport-ssh"
    provides = contract.SERVICE_KEY
    contract_version = contract.CONTRACT_VERSION
    inject: ClassVar[Mapping[str, int]] = {"storage": 1}
    config_schema = TransportConfig
    description = "SSH connections to the clusters in clusters.yaml (read-only command policy)."

    def apply(self, ctx: Context) -> SshTransport:
        assert isinstance(ctx.config, TransportConfig)
        path = Path(ctx.config.clusters_file) if ctx.config.clusters_file else None
        profiles = load_clusters(path or paths.clusters_file())
        transport = SshTransport(
            profiles,
            storage=ctx.services.require("storage"),
            emit=ctx.emit,
            prompter=lambda: ctx.services.get(auth_contract.SERVICE_KEY),
        )
        ctx.add_disposer(transport.close, "close SSH connections")
        ctx.log.info("%d cluster profile(s) loaded", len(profiles))
        return transport


__all__ = ["SshSession", "SshTransport", "SshTransportPlugin", "TransportConfig", "load_clusters"]
