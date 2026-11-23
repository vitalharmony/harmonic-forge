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


REPO_ROOT = Path(__file__).resolve().parents[2]


def config_path() -> Path:
    """The per-user settings file. A relative XDG_CONFIG_HOME is ignored, as the
    XDG spec requires; a path inside the repository raises ValueError."""
    xdg = os.environ.get("XDG_CONFIG_HOME", "")
    base = Path(xdg) if xdg and Path(xdg).is_absolute() else Path.home() / ".config"
    path = base / "harmonic-forge" / FILE_NAME
    if path.resolve().is_relative_to(REPO_ROOT):
        raise ValueError(f"{path}: the instance settings file must live outside the repository")
    return path


def timezone() -> tuple[ZoneInfo, bool]:
    """(zone, defaulted). `defaulted` is True when the file or its `timezone`
    key is missing. Anything at the path that is not a readable file, and an
    invalid zone, raise ValueError."""
    path = config_path()
    if not path.exists():
        return ZoneInfo(DEFAULT_TIMEZONE), True
    if not path.is_file():
        raise ValueError(f"{path}: the instance settings path exists but is not a file")
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
