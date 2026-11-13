"""Short-lived OpenSSH user certificates (e.g. issued by `step ssh certificate`).

A certificate `<key>-cert.pub` next to a private key is used automatically by the SSH
client. When it has expired the server would reject the key, so the transport checks the
validity first and asks the user to renew instead of failing (and retrying) blindly.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from cryptography.hazmat.primitives.serialization import SSHCertificate, load_ssh_public_identity

CERT_SUFFIX = "-cert.pub"
FOREVER = datetime.max.replace(tzinfo=UTC)
_YEAR_9999 = 253402300799  # OpenSSH "forever" certificates use 2**64-1


@dataclass(frozen=True)
class CertInfo:
    path: Path
    valid_after: datetime
    valid_before: datetime

    def expired(self, now: datetime | None = None) -> bool:
        return (now or datetime.now(UTC)) >= self.valid_before


def cert_path(key_file: str | Path) -> Path:
    return Path(f"{key_file}{CERT_SUFFIX}")


def read_cert(path: Path) -> CertInfo | None:
    """Validity of the certificate at `path`; None if absent or not a certificate."""
    try:
        identity = load_ssh_public_identity(path.read_bytes())
    except (OSError, ValueError):
        return None
    if not isinstance(identity, SSHCertificate):
        return None
    before = identity.valid_before
    return CertInfo(
        path=path,
        valid_after=datetime.fromtimestamp(identity.valid_after, UTC),
        valid_before=FOREVER if before >= _YEAR_9999 else datetime.fromtimestamp(before, UTC),
    )


def certs_for(key_files: list[str]) -> list[CertInfo]:
    """Certificates found next to the given private keys."""
    out = []
    for key in key_files:
        info = read_cert(cert_path(Path(key).expanduser()))
        if info is not None:
            out.append(info)
    return out


def files_fingerprint(key_files: list[str]) -> tuple[tuple[str, float], ...]:
    """mtimes of keys and certificates: changes when the user renews outside SimManager."""
    out = []
    for key in key_files:
        for p in (Path(key).expanduser(), cert_path(Path(key).expanduser())):
            try:
                out.append((str(p), p.stat().st_mtime))
            except OSError:
                out.append((str(p), -1.0))
    return tuple(out)
