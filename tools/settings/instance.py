"""Per-user instance settings for forge tooling (harmonic-forge#1505).

The file holds two keys: the operator's timezone and the lane hook overlay
declaration. Forge is a public repo, so the values live outside it, in a
per-user file this module only reads:

    $XDG_CONFIG_HOME/harmonic-forge/instance.toml   (an empty XDG_CONFIG_HOME counts as unset)
    ~/.config/harmonic-forge/instance.toml          (otherwise)

The file is refused inside a git checkout unless that checkout reports it
ignored and untracked (a HOME or ~/.config kept in git, harmonic-forge#1525).

    timezone = "America/Los_Angeles"
    lane_hook_overlay = "operator"

`timezone()` reads the first. A missing file or key returns the default and
says so; an invalid value is an error, never a silent default. Every current
timestamp stays UTC: this is for local-time displays and schedules.

`lane_hook_overlay()` reads the second (harmonic-forge#1525): `"operator"`
merges `tools/lane/lane_hooks.operator.json` into the lane hook set, `"none"`
builds the core set only. It has no default: a missing file, a missing key or
any other value raises ValueError, so a lane launch refuses rather than guess.
"""
from __future__ import annotations

import os
import subprocess
import tomllib
from collections.abc import Mapping
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_TIMEZONE = "America/Los_Angeles"
FILE_NAME = "instance.toml"
OVERLAY_CHOICES = ("operator", "none")


def _inside_a_checkout(path: Path) -> bool:
    """True when any resolved ancestor holds a `.git` entry (file or directory),
    so the main worktree and every linked worktree count, not just this one."""
    return any(os.path.lexists(parent / ".git") for parent in path.resolve().parents)


def _git_ignored_and_untracked(path: Path) -> bool:
    """True only when the git checkout holding `path` reports it ignored AND
    untracked, so no `git add -A` or commit there can carry it
    (harmonic-forge#1525). Every `GIT_*` variable is dropped, so the
    environment cannot point git at another repository, index or config;
    any git error, a missing git, or a timeout is False (refuse)."""
    resolved = path.resolve()
    anchor = next((p for p in resolved.parents if p.is_dir()), None)
    if anchor is None:
        return False
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    base = ["git", "-c", "core.fsmonitor=false", "-C", str(anchor)]
    try:
        ignored = subprocess.run([*base, "check-ignore", "-q", "--", str(resolved)],
                                 env=env, capture_output=True, timeout=10)
        tracked = subprocess.run([*base, "--literal-pathspecs", "ls-files", "-z", "--", str(resolved)],
                                 env=env, capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return False
    return ignored.returncode == 0 and tracked.returncode == 0 and tracked.stdout == b""


def config_path(environ: Mapping[str, str] | None = None) -> Path:
    """The per-user settings file. A relative XDG_CONFIG_HOME is ignored, as the
    XDG spec requires. A path inside a git checkout raises ValueError unless
    that checkout reports it ignored and untracked: #1505 keeps per-user
    values out of every repository, and an ignored, untracked file cannot be
    committed by accident, so a HOME or ~/.config kept in git (a dotfiles
    repo) still has a valid location (harmonic-forge#1525).

    `environ` (default: the process environment) is where XDG_CONFIG_HOME is
    read, so a fixture can name the file a launch under its environment reads."""
    xdg = (os.environ if environ is None else environ).get("XDG_CONFIG_HOME", "")
    base = Path(xdg) if xdg and Path(xdg).is_absolute() else Path.home() / ".config"
    path = base / "harmonic-forge" / FILE_NAME
    if _inside_a_checkout(path) and not _git_ignored_and_untracked(path):
        raise ValueError(f"{path}: the instance settings file must live outside any git "
                         f"checkout, or be ignored and untracked by it (add it to that "
                         f"checkout's .gitignore)")
    return path


def _read() -> tuple[Path, dict | None]:
    """(path, parsed settings). The dict is None only when nothing is at the
    path (not even a broken symlink) and every existing ancestor is a
    directory. Anything else at the path, or an unreadable file, raises
    ValueError."""
    path = config_path()
    if not os.path.lexists(path):
        for parent in path.parents:
            if os.path.lexists(parent):
                if not parent.is_dir():
                    raise ValueError(f"{parent}: an ancestor of the instance settings path is not a directory")
                break
        return path, None
    if not path.is_file():
        raise ValueError(f"{path}: the instance settings path exists but is not a file")
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"{path}: cannot read instance settings: {exc}") from exc
    return path, data


def timezone() -> tuple[ZoneInfo, bool]:
    """(zone, defaulted). `defaulted` is True only when nothing is at the path
    (not even a broken symlink) and every existing ancestor is a directory, or
    when the file has no `timezone` key. Anything else at the path, and an
    invalid zone, raise ValueError."""
    path, data = _read()
    if data is None:
        return ZoneInfo(DEFAULT_TIMEZONE), True
    name = data.get("timezone")
    if name is None:
        return ZoneInfo(DEFAULT_TIMEZONE), True
    if not isinstance(name, str) or not name.strip():
        raise ValueError(f"{path}: `timezone` must be an IANA zone name, got {name!r}")
    try:
        return ZoneInfo(name), False
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"{path}: `timezone` {name!r} is not a known IANA zone") from exc


def lane_hook_overlay() -> tuple[str, Path]:
    """(declared overlay, the file it was read from). One of OVERLAY_CHOICES;
    there is no default. A missing file or key, and any other value of any
    type, raise ValueError (harmonic-forge#1525)."""
    path, data = _read()
    if data is None or "lane_hook_overlay" not in data:
        raise ValueError(
            f"{path}: `lane_hook_overlay` is not declared -- add "
            f"`lane_hook_overlay = \"operator\"` (the operator's machine) or "
            f"`lane_hook_overlay = \"none\"` (core hooks only); the lane hook set "
            f"never guesses (harmonic-forge#1525)")
    value = data["lane_hook_overlay"]
    if not isinstance(value, str) or value not in OVERLAY_CHOICES:
        raise ValueError(f"{path}: `lane_hook_overlay` must be one of "
                         f"{', '.join(OVERLAY_CHOICES)}; got {value!r}")
    return value, path
