"""Resolve a relative file argument against the caller's directory
(harmonic-forge#1117 AC2).

A mise task runs with its cwd at the project root, and HRSE2's posting tasks
then `cd` into Lane 1's tools worktree (harmonic-forge#762), so a relative
`--file body.md` would be read from the wrong directory. mise exports the
directory the caller ran it from as `MISE_ORIGINAL_CWD`. Outside mise the
process cwd already is the caller's, so a path is left as given and each
script's own fallbacks (e.g. `post_lane_discussion.resolve_body_path`) apply
unchanged. The wrappers used to rewrite paths themselves
(`_abs()`), which meant one copy per consuming repo; the scripts own it now.
"""
from __future__ import annotations

import os
from pathlib import Path


def caller_path(path):
    """`path` made absolute against `MISE_ORIGINAL_CWD`, in the type it
    arrived in. Unchanged outside mise, and for None, "-" (stdin) and an
    absolute path."""
    base = os.environ.get("MISE_ORIGINAL_CWD")
    if not base or path is None or str(path) in ("", "-") or Path(path).is_absolute():
        return path
    resolved = Path(base) / path
    return resolved if isinstance(path, Path) else str(resolved)


def resolve_args(args, *names: str) -> None:
    """Resolve each named attribute of an argparse namespace in place; a list
    value (nargs) is resolved element by element."""
    for name in names:
        value = getattr(args, name, None)
        if isinstance(value, list):
            setattr(args, name, [caller_path(v) for v in value])
        else:
            setattr(args, name, caller_path(value))
