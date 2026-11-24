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


def _inside_a_checkout(path: Path) -> bool:
    """True when any resolved ancestor holds a `.git` entry (file or directory),
    so the main worktree and every linked worktree count, not just this one."""
    return any(os.path.lexists(parent / ".git") for parent in path.resolve().parents)


def config_path() -> Path:
    """The per-user settings file. A relative XDG_CONFIG_HOME is ignored, as the
    XDG spec requires; a path inside any git checkout raises ValueError."""
    xdg = os.environ.get("XDG_CONFIG_HOME", "")
    base = Path(xdg) if xdg and Path(xdg).is_absolute() else Path.home() / ".config"
    path = base / "harmonic-forge" / FILE_NAME
    if _inside_a_checkout(path):
        raise ValueError(f"{path}: the instance settings file must live outside any git checkout")
    return path


def timezone() -> tuple[ZoneInfo, bool]:
    """(zone, defaulted). `defaulted` is True only when nothing is at the path
    (not even a broken symlink) and every existing ancestor is a directory, or
    when the file has no `timezone` key. Anything else at the path, and an
    invalid zone, raise ValueError."""
    path = config_path()
    if not os.path.lexists(path):
        for parent in path.parents:
            if os.path.lexists(parent):
                if not parent.is_dir():
                    raise ValueError(f"{parent}: an ancestor of the instance settings path is not a directory")
                break
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
