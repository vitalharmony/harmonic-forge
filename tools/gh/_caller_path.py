"""Resolve a relative file argument against the caller's directory
(harmonic-forge#1117 AC2).

A mise task runs with its cwd at the project root, and HRSE2's posting tasks
then `cd` into Lane 1's tools worktree (harmonic-forge#762), so a relative
`--file body.md` would be read from the wrong directory. The wrapper passes
the caller's directory on its exec line as `HF_CALLER_CWD`, for this one
process only: `resolve_args` reads it and removes it from the environment
before the script starts any child. `MISE_ORIGINAL_CWD` is never read, because
every process inside any mise task inherits it, so a script run by another
task (e.g. `l2-run` from a subdirectory) would resolve against the wrong
directory (preclose survivor). Without the variable the process cwd already is
the caller's, so a path is left as given and each script's own fallbacks
(e.g. `post_lane_discussion.resolve_body_path`) apply unchanged.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

CALLER_CWD = "HF_CALLER_CWD"


def _resolve(path, base):
    if not base or path is None or str(path) in ("", "-") or Path(path).is_absolute():
        return path
    resolved = Path(base) / path
    return resolved if isinstance(path, Path) else str(resolved)


def caller_path(path):
    """`path` made absolute against `HF_CALLER_CWD`, in the type it arrived
    in. Unchanged without the variable, and for None, "-" (stdin) and an
    absolute path."""
    return _resolve(path, os.environ.get(CALLER_CWD))


def resolve_args(args, *names: str) -> None:
    """Resolve each named attribute of an argparse namespace in place; a list
    value (nargs) is resolved element by element. Consumes `HF_CALLER_CWD`, so
    no child process inherits it."""
    base = os.environ.pop(CALLER_CWD, None)
    for name in names:
        value = getattr(args, name, None)
        if isinstance(value, list):
            setattr(args, name, [_resolve(v, base) for v in value])
        else:
            setattr(args, name, _resolve(value, base))


def refuse_repo_override(parser, task: str, argv: list[str] | None = None) -> None:
    """Under `mise run <task>`, whose wrapper pins `--repo "$GH_REPO"`, refuse a
    second `--repo` from the caller: argparse keeps the last value, so it would
    silently retarget the write (harmonic-forge#1117 preclose survivor). Keyed
    on `MISE_TASK_NAME`, like the `--body` refusal (harmonic-forge#266)."""
    if os.environ.get("MISE_TASK_NAME") != task:
        return
    argv = sys.argv[1:] if argv is None else argv
    seen = [a for a in argv if a.startswith("--re") and "--repo".startswith(a.split("=", 1)[0])]
    if len(seen) > 1:
        parser.error(f"--repo is fixed by `mise run {task}` ($GH_REPO); a second --repo "
                     "would retarget the write silently (harmonic-forge#1117)")
