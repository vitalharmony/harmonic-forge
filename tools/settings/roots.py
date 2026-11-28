#!/usr/bin/env python3
"""harmonic-forge#1529: the projects root, from instance.toml `projects_root`.

One total helper: every function answers instead of raising, because its
callers are PreToolUse hooks, where an uncaught exception exits 1 and lets the
tool call through (a crashed deny is an allow). An unset or invalid value is
None, and each caller fails closed on None in its own way (the handoff names
how).

CLI: `roots.py projects` prints the root as written (`~` expanded, symlinks
not resolved, so a symlinked HOME does not change a launch tuple), or nothing;
exit 0 either way.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
# -I drops the script's directory from sys.path; name it explicitly.
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

UNSET_TEXT = "the configured projects root (instance.toml projects_root)"


def projects_root_or_none(resolved: bool = False) -> Path | None:
    """The declared projects root, or None (unset, relative, unreadable)."""
    try:
        import instance  # noqa: PLC0415
        root = instance.projects_root()
        if root is None:
            return None
        root = Path(os.path.expanduser(str(root)))
        if not root.is_absolute():
            return None
        return root.resolve() if resolved else root
    except Exception:  # noqa: BLE001 - total: a hook must never crash here
        return None


def under_projects(*parts: str, resolved: bool = False) -> Path | None:
    """`<projects root>/<parts>`, or None. `resolved=True` resolves the JOINED
    path, as main's `(home/.../testplan).resolve()` did, so a symlinked
    `<root>/testplan` names its target; total, so a resolve failure is None."""
    root = projects_root_or_none()
    if root is None:
        return None
    path = root.joinpath(*parts)
    if not resolved:
        return path
    try:
        return path.resolve()
    except (OSError, RuntimeError, ValueError):
        # ValueError: a root holding a NUL (a TOML `\u0000` escape) makes
        # resolve() raise it; uncaught, it crashes a fence hook open.
        return None


def describe(own_file: str | None = None) -> str:
    """One line naming the forge root and the projects root, for messages.
    Never raises: any failure is fixed text."""
    try:
        projects = projects_root_or_none()
        part = f"projects root {projects}" if projects else f"{UNSET_TEXT} (unset)"
        try:
            import instance  # noqa: PLC0415
            checkout = Path(own_file or __file__).resolve().parents[2]
            forge = instance.resolve_forge_root(checkout)[0]
            return f"forge root {forge}, {part}"
        except Exception:  # noqa: BLE001
            return part
    except Exception:  # noqa: BLE001
        return UNSET_TEXT


def main(argv: list[str]) -> int:
    if argv[1:] == ["projects"]:
        root = projects_root_or_none()
        if root is not None:
            print(root)
        return 0
    print("usage: roots.py projects", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
