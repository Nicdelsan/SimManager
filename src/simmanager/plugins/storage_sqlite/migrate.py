"""Schema migrations: numbered `.sql` files in `migrations/`, applied once each, in order."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib import resources

import aiosqlite

log = logging.getLogger("simmanager.plugin.storage-sqlite")

_NAME = re.compile(r"^(\d{4})_[\w-]+\.sql$")


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str


def bundled_migrations() -> list[Migration]:
    folder = resources.files(__package__).joinpath("migrations")
    found = []
    for item in folder.iterdir():
        match = _NAME.match(item.name)
        if match:
            found.append(Migration(int(match.group(1)), item.name, item.read_text("utf-8")))
    found.sort(key=lambda m: m.version)
    versions = [m.version for m in found]
    if len(set(versions)) != len(versions):
        raise RuntimeError(f"duplicate migration versions: {versions}")
    return found


async def apply_migrations(db: aiosqlite.Connection, migrations: list[Migration]) -> list[int]:
    """Apply pending migrations, each in its own transaction. Returns applied versions."""
    await db.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        " version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)"
    )
    await db.commit()
    async with db.execute("SELECT version FROM schema_migrations") as cur:
        done = {row[0] async for row in cur}

    applied = []
    for m in migrations:
        if m.version in done:
            continue
        now = datetime.now(UTC).isoformat()
        script = (
            "BEGIN;\n"
            f"{m.sql}\n"
            "INSERT INTO schema_migrations (version, name, applied_at) "
            f"VALUES ({m.version}, '{m.name}', '{now}');\n"
            "COMMIT;"
        )
        try:
            await db.executescript(script)
        except Exception:
            if db.in_transaction:
                await db.rollback()
            raise
        log.info("applied migration %s", m.name)
        applied.append(m.version)
    return applied
