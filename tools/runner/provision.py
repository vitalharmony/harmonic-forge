#!/usr/bin/env python3
"""Platform links for every te-runner clone (harmonic-forge#1127).

A `--shared --no-checkout` clone followed by a checkout runs no
`post-checkout` hook, so the clone has no `.claude/` platform links and
`platform_link_report` fails on an unchanged base. `provision_clone` does what
`.githooks/post-checkout` and `mise run hooks-install` do, with two
differences: a failed verify raises instead of being swallowed, and the clone's
own `sync_rules.py` (tree code, possibly the agent's) runs only inside the
oracle boundary. Only `git config`, which executes no tree code, runs
in-process.

It lives outside `te_run` so that `te_run` and the preflight's probe clone
share one implementation.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import sandbox  # noqa: E402


# sync_rules.py links a few files; a run past this is a hang, not a slow link.
LINK_TIMEOUT = 120


class ProvisionError(Exception):
    """The clone could not be linked to its own platform."""


def _sandboxed(argv: list[str], path: Path, objects: Path) -> subprocess.CompletedProcess:
    try:
        full = sandbox.wrap(argv, path, inputs=(objects,))
    except sandbox.SandboxError as exc:
        raise ProvisionError(str(exc)) from exc
    try:
        return subprocess.run(full, cwd=path, capture_output=True, text=True,
                              timeout=LINK_TIMEOUT)
    except subprocess.TimeoutExpired as exc:
        raise ProvisionError(f"{argv[1]} timed out after {LINK_TIMEOUT}s") from exc


def provision_clone(path: Path, objects: Path) -> None:
    """Set `core.hooksPath` and self-link the clone at `path`; `objects` is the
    source object store the `--shared` clone borrows from (a read-only input
    of the boundary). Raises `ProvisionError` unless the links verify."""
    path = Path(path)
    subprocess.run(["git", "-C", str(path), "config", "core.hooksPath", ".githooks"],
                   check=True, capture_output=True, text=True)
    if not (path / "sync_rules.py").is_file():
        raise ProvisionError(f"{path} has no sync_rules.py to link with")
    # Link mode exits 0 even on an absent manifest (#545); verify decides.
    _sandboxed(["python3", "sync_rules.py", "--project", "."], path, objects)
    verified = _sandboxed(["python3", "sync_rules.py", "--verify", "--project", "."],
                          path, objects)
    if verified.returncode:
        output = f"{verified.stdout}\n{verified.stderr}".strip()
        raise ProvisionError(f"sync_rules.py --verify exited {verified.returncode}: "
                             f"{output[-400:]}")
