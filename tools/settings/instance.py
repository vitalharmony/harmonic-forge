"""Per-user instance settings for forge tooling (harmonic-forge#1505).

The file holds two keys: the operator's timezone and the lane hook overlay
declaration. Forge is a public repo, so the values live outside it, in a
per-user file this module only reads:

    $XDG_CONFIG_HOME/harmonic-forge/instance.toml   (an empty XDG_CONFIG_HOME counts as unset)
    ~/.config/harmonic-forge/instance.toml          (otherwise)

    timezone = "America/Los_Angeles"
    lane_hook_overlay = "operator"

`timezone()` reads the first. A missing file or key returns the default and
says so; an invalid value is an error, never a silent default. Every current
timestamp stays UTC: this is for local-time displays and schedules.

`lane_hook_overlay()` reads the second (harmonic-forge#1525): `"operator"`
merges `tools/lane/lane_hooks.operator.json` into the lane hook set, `"none"`
builds the core set only. It has no default: a missing file, a missing key or
any other value raises ValueError, so a lane launch refuses rather than guess.

`forge_root()` and `projects_root()` read two optional absolute paths
(harmonic-forge#1526), and `resolve_forge_root()` is the one precedence the
lane launcher and the hooks share: `HARMONIC_FORGE_ROOT`, then `forge_root`,
then the launcher's own checkout. `launch_view()` adds the set path and the
root as written, the one view a launch and the onboarding report share.

The one writer is the `set lane3_policy <path>` subcommand (harmonic-forge#1531),
which the Lane 3 install step runs instead of a blind shell append: it adds the
key as a top-level line at the head of the file, so a file with no final
newline or one ending in a `[table]` stays valid and the key stays top-level.
Every reader above only reads.
"""
from __future__ import annotations

import os
import sys
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


def config_path(environ: Mapping[str, str] | None = None) -> Path:
    """The per-user settings file. A relative XDG_CONFIG_HOME is ignored, as the
    XDG spec requires; a path inside any git checkout raises ValueError.

    `environ` (default: the process environment) is where XDG_CONFIG_HOME is
    read, so a fixture can name the file a launch under its environment reads."""
    xdg = (os.environ if environ is None else environ).get("XDG_CONFIG_HOME", "")
    base = Path(xdg) if xdg and Path(xdg).is_absolute() else Path.home() / ".config"
    path = base / "harmonic-forge" / FILE_NAME
    if _inside_a_checkout(path):
        raise ValueError(f"{path}: the instance settings file must live outside any git checkout")
    return path


def _read(environ: Mapping[str, str] | None = None) -> tuple[Path, dict | None]:
    """(path, parsed settings). The dict is None only when nothing is at the
    path (not even a broken symlink) and every existing ancestor is a
    directory. Anything else at the path, or an unreadable file, raises
    ValueError."""
    path = config_path(environ)
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


def _written_setting(key: str, environ: Mapping[str, str] | None = None) -> str | None:
    """An optional path key exactly as written: None when the file or the key
    is missing. The value must be a non-empty string that is absolute or
    starts with `~/`; anything else raises ValueError. Existence is not checked."""
    path, data = _read(environ)
    if data is None or key not in data:
        return None
    value = data[key]
    if (not isinstance(value, str) or not value
            or not (value.startswith("/") or value.startswith("~/"))):
        raise ValueError(f"{path}: `{key}` must be an absolute path, got {value!r}")
    return value


def _path_setting(key: str, environ: Mapping[str, str] | None = None) -> Path | None:
    """`_written_setting`, `~/` expanded."""
    value = _written_setting(key, environ)
    return None if value is None else Path(value).expanduser()


def forge_root(environ: Mapping[str, str] | None = None) -> Path | None:
    """The declared forge checkout, or None (harmonic-forge#1526)."""
    return _path_setting("forge_root", environ)


def projects_root(environ: Mapping[str, str] | None = None) -> Path | None:
    """The declared projects directory, or None. Read only (harmonic-forge#1526)."""
    return _path_setting("projects_root", environ)


LAUNCHER_CHECKOUT = "launcher checkout"
#: The lane hook set a launch passes, relative to the launcher checkout.
LANE_HOOK_SET = Path("tools") / "lane" / "lane_hooks.json"


def named_root(written: str, source: str, resolved: object = None) -> str:
    """How every forge-root refusal names the root: the value as written, its
    source, then the resolved form (sticky-wicket C1)."""
    tail = "" if resolved is None else f"; resolves to {str(resolved)!r}"
    return f"forge root {written!r} (from {source}{tail})"


def launch_view(launcher_checkout: Path, environ: Mapping[str, str] | None = None
                ) -> tuple[Path, Path, str, str]:
    """(set path, resolved forge root, its source, the root as written): the one
    view of what a lane launch runs (sticky-wicket B1). The launcher's `root`
    subcommand prints it and the onboarding readers call it, so neither renders
    a set or a root the other does not.

    The root is a non-empty `HARMONIC_FORGE_ROOT` (absolute, else ValueError),
    then `instance.toml` `forge_root`, then `launcher_checkout`, resolved
    strictly: a missing component, a loop, or a root with no `tools/hooks/`
    raises ValueError via `named_root` (a hook set built from it would run
    nothing, harmonic-forge#1526). The launcher compares the source label
    `LAUNCHER_CHECKOUT` as a literal, so it never changes alone."""
    env = os.environ if environ is None else environ
    from_env = env.get("HARMONIC_FORGE_ROOT", "")
    if from_env:
        if not Path(from_env).is_absolute():
            raise ValueError(f"HARMONIC_FORGE_ROOT must be an absolute path, got {from_env!r}")
        written, source = from_env, "HARMONIC_FORGE_ROOT"
    elif (declared := _written_setting("forge_root", env)) is not None:
        written, source = declared, f"{config_path(env)} forge_root"
    else:
        written, source = str(launcher_checkout), LAUNCHER_CHECKOUT
    if "\t" in written or "\n" in written:
        raise ValueError(f"{named_root(written, source)} contains a tab or newline")
    try:
        root = Path(written).expanduser().resolve(strict=True)
        set_path = Path(launcher_checkout).resolve(strict=True) / LANE_HOOK_SET
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"{named_root(written, source)} does not resolve: {exc}") from None
    named = named_root(written, source, root)
    if "\t" in str(root) or "\n" in str(root) or "\t" in str(set_path) or "\n" in str(set_path):
        raise ValueError(f"{named} contains a tab or newline")
    if not (root / "tools" / "hooks").is_dir():
        raise ValueError(f"{named} has no tools/hooks/ -- "
                         f"refusing to build a hook set that would run nothing")
    return set_path, root, source, written


def resolve_forge_root(launcher_checkout: Path,
                       environ: Mapping[str, str] | None = None) -> tuple[Path, str]:
    """(resolved forge root, its source): `launch_view` without the set."""
    _set, root, source, _written = launch_view(launcher_checkout, environ)
    return root, source


def lane3_policy(environ: Mapping[str, str] | None = None) -> Path | None:
    """The installed Lane 3 policy file (harmonic-forge#1531), or None when the
    key is absent (Lane 3 then launches without one, harmonic-forge#878). The
    value must be an absolute path, never `~/`; anything else raises ValueError.
    Ownership and immutability are the launcher's check, not this reader's."""
    path, data = _read(environ)
    if data is None or "lane3_policy" not in data:
        return None
    value = data["lane3_policy"]
    if not isinstance(value, str) or not value.startswith("/") or _has_control(value):
        raise ValueError(f"{path}: `lane3_policy` must be an absolute path, got {value!r}")
    return Path(value)


def _has_control(value: str) -> bool:
    """A control character (below 0x20, or 0x7f). `get` NUL-terminates each value,
    so an embedded NUL would truncate it to another path (sticky-wicket PATCH)."""
    return any(ord(c) < 0x20 or ord(c) == 0x7F for c in value)


def codex_hook_paths(environ: Mapping[str, str] | None = None) -> list[Path]:
    """Extra `.codex/hooks.json` files a Codex launch checks besides its own
    checkout's (harmonic-forge#1531): empty when absent. A non-list, or an entry
    that is not an absolute or `~/` string, raises ValueError."""
    path, data = _read(environ)
    if data is None or "codex_hook_paths" not in data:
        return []
    value = data["codex_hook_paths"]
    if (not isinstance(value, list)
            or not all(isinstance(v, str) and (v.startswith("/") or v.startswith("~/"))
                       and not _has_control(v) for v in value)):
        raise ValueError(f"{path}: `codex_hook_paths` must be a list of absolute paths, "
                         f"got {value!r}")
    return [Path(v).expanduser() for v in value]


def _toml_string(value: str) -> str:
    """A TOML basic string for a value with no control characters."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def set_lane3_policy(value: str, environ: Mapping[str, str] | None = None) -> tuple[Path, bool]:
    """Declare `lane3_policy` in the settings file (harmonic-forge#1531):
    (the file, whether it changed). The value must pass the `lane3_policy`
    reader's own rule. A missing file is created holding just the key. In an
    existing file the key goes in as the first line -- the head of a TOML
    document is always top level, so a file with no final newline or one
    ending in a `[table]` stays valid and the key is never nested -- and the
    result must parse to the old settings plus the key, or nothing is written.
    The same value already set is a no-op; a different one raises ValueError
    naming both, so an installed path is never silently replaced. The write is
    atomic (a temporary file beside the target, then a rename) and keeps the
    file's mode; a symlinked file is written through to its target."""
    if not value.startswith("/") or _has_control(value):
        raise ValueError(f"`lane3_policy` must be an absolute path, got {value!r}")
    path, data = _read(environ)
    line = f"lane3_policy = {_toml_string(value)}\n"
    if data is None:
        path.parent.mkdir(parents=True, exist_ok=True)
        old_text, mode, target = "", 0o644, path
    else:
        if "lane3_policy" in data:
            current = data["lane3_policy"]
            if current == value:
                return path, False
            raise ValueError(f"{path}: `lane3_policy` is already set to {current!r}; "
                             f"refusing to replace it with {value!r} -- edit the file by hand")
        target = path.resolve()
        old_text = target.read_text(encoding="utf-8")
        mode = target.stat().st_mode & 0o7777
    new_text = line + old_text
    if tomllib.loads(new_text) != {**(data or {}), "lane3_policy": value}:
        raise ValueError(f"{path}: adding `lane3_policy` would not leave it a top-level key; "
                         f"nothing written")
    tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(new_text)
        os.replace(tmp, target)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return path, True


def _main(argv: list[str]) -> int:
    """`instance.py get lane3_policy|codex_hook_paths`: each value NUL-terminated
    (nothing when absent), exit 0; an invalid value prints the reason, exit 1.
    `instance.py set lane3_policy <path>`: `set_lane3_policy`, exit 0 when the
    key now holds the path, 1 (file unchanged) on any refusal."""
    if len(argv) == 4 and argv[1:3] == ["set", "lane3_policy"]:
        try:
            path, changed = set_lane3_policy(argv[3])
        except (ValueError, OSError, tomllib.TOMLDecodeError) as exc:
            print(f"instance: {exc}", file=sys.stderr)
            return 1
        print(f"instance: {path}: lane3_policy {'set' if changed else 'already set'} to {argv[3]!r}",
              file=sys.stderr)
        return 0
    readers = {"lane3_policy": lambda: [p for p in [lane3_policy()] if p],
               "codex_hook_paths": codex_hook_paths}
    if len(argv) != 3 or argv[1] != "get" or argv[2] not in readers:
        print("usage: instance.py get " + "|".join(readers) + " | instance.py set lane3_policy <path>",
              file=sys.stderr)
        return 2
    try:
        values = readers[argv[2]]()
    except ValueError as exc:
        print(f"instance: {exc}", file=sys.stderr)
        return 1
    sys.stdout.write("".join(f"{v}\0" for v in values))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))
