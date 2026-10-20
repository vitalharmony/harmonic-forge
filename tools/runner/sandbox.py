#!/usr/bin/env python3
"""The one boundary every run of the agent's code goes through (harmonic-forge#1098).

The te-runner's agent, and everything that executes the agent's code (`mise
run check`, the kill-check tests, the preclose refuters), runs under `bwrap`:

- the whole filesystem read-only;
- one writable directory (the agent's clone, or the scratch tree a test runs in);
- `~/.claude/state`, `~/.local/share/te-runner` and `~/.config/te-runner`
  (receipts, kill bundles, the runner's key and config) behind a private,
  throwaway tmpfs, so their real contents are neither visible nor writable;
- the runner's gh config re-exposed read-only;
- `~/.claude/projects` bound to a private transcripts directory, or a tmpfs.

Probes on this host (issue comments 6283139358, 6283189924) showed writes outside
the writable directory never reach the real paths, and G3's hook events still
fire. `bwrap` is required: without it nothing is launched (fail closed).

    sandbox.py here -- CMD...   # run CMD inside the boundary, writable = the cwd

`here` exists for `kill_check.py`, which materializes its own scratch tree and
runs each test with that tree as its cwd. It is invoked by absolute path from
the trusted checkout, never from the tree under test.
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

HOME = Path.home()
HIDDEN = (HOME / ".claude" / "state", HOME / ".local" / "share" / "te-runner",
          HOME / ".config" / "te-runner")
RUNNER_GH = HOME / ".config" / "te-runner" / "gh"
TRANSCRIPTS = HOME / ".claude" / "projects"
BWRAP = "bwrap"


class SandboxError(Exception):
    """The boundary cannot be put in place, so nothing is launched."""


def require_bwrap() -> str:
    # Tests only: a stand-in that records the argv and runs the command
    # unconfined. Honored only in a test process, never in a real run.
    stub = os.environ.get("TE_RUNNER_BWRAP")
    if stub and os.environ.get("HARMONIC_FORGE_TESTING") == "1":
        return stub
    found = shutil.which(BWRAP)
    if not found:
        raise SandboxError("bwrap is not installed; the agent's code never runs unconfined")
    return found


def boundary_argv(writable: Path, transcripts: Path | None = None) -> list[str]:
    """The `bwrap ... --` prefix. Mounts apply in order: each tmpfs hides a
    trusted directory, and the later read-only gh bind re-exposes only that."""
    bwrap = require_bwrap()
    writable = Path(writable).resolve()
    for mount_point in (*HIDDEN, TRANSCRIPTS):  # bwrap needs each mount point to exist
        mount_point.mkdir(parents=True, exist_ok=True)
    argv = [bwrap, "--die-with-parent", "--ro-bind", "/", "/", "--dev", "/dev",
            "--proc", "/proc", "--tmpfs", "/tmp", "--bind", str(writable), str(writable)]
    for hidden in HIDDEN:
        argv += ["--tmpfs", str(hidden)]
    if RUNNER_GH.is_dir():
        argv += ["--ro-bind", str(RUNNER_GH), str(RUNNER_GH)]
    if transcripts is not None:
        Path(transcripts).mkdir(parents=True, exist_ok=True)
        argv += ["--bind", str(Path(transcripts).resolve()), str(TRANSCRIPTS)]
    else:
        argv += ["--tmpfs", str(TRANSCRIPTS)]
    return argv + ["--chdir", str(writable), "--"]


def wrap(argv: list[str], writable: Path, transcripts: Path | None = None) -> list[str]:
    return boundary_argv(writable, transcripts) + list(argv)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) < 3 or args[:2] != ["here", "--"]:
        print("usage: sandbox.py here -- CMD...", file=sys.stderr)
        return 2
    try:
        command = wrap(args[2:], Path.cwd())
    except (SandboxError, OSError) as exc:
        print(f"sandbox: {exc}", file=sys.stderr)
        return 2
    os.execv(command[0], command)
    return 2  # not reached


if __name__ == "__main__":
    sys.exit(main())
