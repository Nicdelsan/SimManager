"""Structured logging: JSON lines in a rotating file, short human-readable lines on the console."""

from __future__ import annotations

import json
import logging
import logging.handlers
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Attributes every LogRecord has; anything else was passed through `extra=` and is kept.
_STANDARD_ATTRS = set(vars(logging.makeLogRecord({}))) | {"message", "asctime", "taskName"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in vars(record).items():
            if key not in _STANDARD_ATTRS and not key.startswith("_"):
                entry[key] = value
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str, ensure_ascii=False)


def setup_logging(
    log_dir: Path,
    *,
    level: str = "INFO",
    console_level: str = "WARNING",
    max_bytes: int = 5 * 1024 * 1024,
    backups: int = 5,
) -> Path:
    """Configure the `simmanager` logger. Returns the log file path. Safe to call twice."""
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "simmanager.log"

    logger = logging.getLogger("simmanager")
    logger.setLevel(level.upper())
    logger.propagate = False
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

    file_handler = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=max_bytes, backupCount=backups, encoding="utf-8"
    )
    file_handler.setFormatter(JsonFormatter())
    logger.addHandler(file_handler)

    console = logging.StreamHandler()
    console.setLevel(console_level.upper())
    console.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    logger.addHandler(console)
    return log_file
