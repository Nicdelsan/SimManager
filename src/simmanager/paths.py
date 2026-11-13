"""User directories for configuration, data and logs.

Windows: config in %APPDATA%\\SimManager, data in %LOCALAPPDATA%\\SimManager.
Linux:   config in ~/.config/simmanager, data in ~/.local/share/simmanager (XDG respected).
`SIMMANAGER_CONFIG_DIR` / `SIMMANAGER_DATA_DIR` override both (tests, portable installs).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME_WINDOWS = "SimManager"
APP_NAME_POSIX = "simmanager"

USER_CONFIG_FILE = "config.yaml"
CLUSTERS_FILE = "clusters.yaml"
PROJECT_CONFIG_FILE = "simmanager.yaml"


def _env_dir(var: str) -> Path | None:
    value = os.environ.get(var)
    return Path(value).expanduser() if value else None


def config_dir() -> Path:
    override = _env_dir("SIMMANAGER_CONFIG_DIR")
    if override:
        return override
    if sys.platform == "win32":
        base = _env_dir("APPDATA") or Path.home() / "AppData" / "Roaming"
        return base / APP_NAME_WINDOWS
    base = _env_dir("XDG_CONFIG_HOME") or Path.home() / ".config"
    return base / APP_NAME_POSIX


def data_dir() -> Path:
    override = _env_dir("SIMMANAGER_DATA_DIR")
    if override:
        return override
    if sys.platform == "win32":
        base = _env_dir("LOCALAPPDATA") or Path.home() / "AppData" / "Local"
        return base / APP_NAME_WINDOWS
    base = _env_dir("XDG_DATA_HOME") or Path.home() / ".local" / "share"
    return base / APP_NAME_POSIX


def log_dir() -> Path:
    return data_dir() / "logs"


def user_config_file() -> Path:
    return config_dir() / USER_CONFIG_FILE


def clusters_file() -> Path:
    return config_dir() / CLUSTERS_FILE


def default_database_file() -> Path:
    return data_dir() / "simmanager.db"
