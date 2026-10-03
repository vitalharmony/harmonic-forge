#!/usr/bin/env python3
"""Which Lane 3 env task, if any, a checkout declares (harmonic-forge#875).

The `lane3` launcher and `lane3-provision` call this before touching the gate
worktree's `backend/.env`. Three outcomes, and the launcher treats only the
second as "relink as today":

* exit 0, prints the task name -- the project declares `lane3_env_task`; the
  launcher runs it instead of relinking;
* exit 0, prints nothing -- the project is in the manifest and declares no
  such task; the launcher relinks exactly as before (R-0188);
* exit 2, prints the reason -- any lookup failure: the manifest is missing or
  unparseable, or no project's `path` resolves to this checkout. The launcher
  refuses. Collapsing a failure into "declared nothing" would silently bring
  back the relink this issue exists to stop.

Usage (either form):
    python3 tools/onboard/lane3_env_task.py <checkout>
    python3 -m lane3_env_task <checkout>      # with tools/onboard on sys.path

Never reads any project's env files -- only `projects.toml`
(`FORGE_PROJECTS_MANIFEST` overrides its location, as for every consumer).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from manifest import load, manifest_path  # noqa: E402
from manifest_protocol import ManifestError  # noqa: E402

LOOKUP_FAILED = 2


def lane3_env_task(checkout: Path) -> str | None:
    """The declared task for the project whose checkout is ``checkout``.

    Raises ManifestError when the manifest cannot be read or no project's
    checkout resolves to ``checkout``. Matching is on the fully resolved path
    on both sides (`Project.checkout` resolves the same way), so a symlinked
    checkout such as `~/harmonic-forge` matches.
    """
    wanted = Path(checkout).expanduser().resolve()
    for project in load():
        if project.checkout == wanted:
            return project.protocol.lane3_env_task if project.protocol else None
    raise ManifestError(f"no project in {manifest_path()} has checkout {wanted}")


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("usage: lane3_env_task.py <checkout>", file=sys.stderr)
        return LOOKUP_FAILED
    try:
        task = lane3_env_task(Path(args[0]))
    except ManifestError as exc:
        print(f"lane3 env lookup failed: {exc}", file=sys.stderr)
        return LOOKUP_FAILED
    except Exception as exc:  # noqa: BLE001 -- any failure is a lookup failure
        print(f"lane3 env lookup failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return LOOKUP_FAILED
    if task:
        print(task)
    return 0


if __name__ == "__main__":
    sys.exit(main())
