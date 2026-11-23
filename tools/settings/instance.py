"""Per-user instance settings for forge tooling (harmonic-forge#1505).

The one value today is the operator's timezone. Forge is a public repo, so the
value lives outside it, in a per-user file this module only reads:

    $XDG_CONFIG_HOME/harmonic-forge/instance.toml   (an empty XDG_CONFIG_HOME counts as unset)
    ~/.config/harmonic-forge/instance.toml          (otherwise)

    timezone = "America/Los_Angeles"

`timezone()` is the one accessor. A missing file or key returns the default and
says so; an invalid value is an error, never a silent default. Every current
timestamp stays UTC: this is for local-time displays and schedules.
"""
from __future__ import annotations

import os
import tomllib
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_TIMEZONE = "America/Los_Angeles"
FILE_NAME = "instance.toml"


def config_path() -> Path:
    """The per-user settings file, which is never inside the repository."""
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "harmonic-forge" / FILE_NAME


def timezone() -> tuple[ZoneInfo, bool]:
    """(zone, defaulted). `defaulted` is True when the file or its `timezone`
    key is missing. An unreadable file or an invalid zone raises ValueError."""
    path = config_path()
    if not path.is_file():
        return ZoneInfo(DEFAULT_TIMEZONE), True
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"{path}: cannot read instance settings: {exc}") from exc
    name = data.get("timezone")
    if name is None:
        return ZoneInfo(DEFAULT_TIMEZONE), True
    if not isinstance(name, str) or not name.strip():
        raise ValueError(f"{path}: `timezone` must be an IANA zone name, got {name!r}")
    try:
        return ZoneInfo(name), False
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"{path}: `timezone` {name!r} is not a known IANA zone") from exc
