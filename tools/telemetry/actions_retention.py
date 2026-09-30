#!/usr/bin/env python3
"""Raise each registry repo's Actions retention to its plan maximum (harmonic-forge#826).

From 2026-10-01 GitHub applies a repo's Actions retention to checks, workflow
runs and commit statuses, not only logs and artifacts, so a 90-day setting on
a repo that allows 400 deletes CI history it did not have to. This raises
every registry repo whose retention is below its maximum to that maximum.
Repos capped below 400 days are reported: `ci_history_export.py` archives
their history instead, because no setting can keep it.

Registry-driven through `tools/onboard/manifest.py`; every call runs under the
repo's own identity via `apply_project_identity`.

Usage:
    python3 tools/telemetry/actions_retention.py            # raise
    python3 tools/telemetry/actions_retention.py --dry-run  # report only
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Optional

HERE = Path(__file__).resolve().parent
for _p in (str(HERE), str(HERE.parent / "onboard")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

FULL_RETENTION_DAYS = 400

Gh = Callable[..., subprocess.CompletedProcess]


def _gh(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["gh", "api", *args], capture_output=True, text=True)


def plan(current: Optional[dict]) -> str:
    """`raise`, `ok`, `capped` (max < 400, export instead) or `unknown`."""
    if not current:
        return "unknown"
    days, maximum = current.get("days", 0), current.get("maximum_allowed_days", 0)
    if days < maximum:
        return "raise"
    return "ok" if days >= FULL_RETENTION_DAYS else "capped"


def apply(repo: str, gh: Gh, *, dry_run: bool) -> dict[str, Any]:
    endpoint = f"repos/{repo}/actions/permissions/artifact-and-log-retention"
    read = gh(endpoint)
    current = json.loads(read.stdout) if read.returncode == 0 else None
    action = plan(current)
    row: dict[str, Any] = {"repo": repo, "before": current, "action": action}
    if action == "raise" and not dry_run:
        write = gh("-X", "PUT", endpoint, "-F", f"days={current['maximum_allowed_days']}")
        after = gh(endpoint)
        row["after"] = json.loads(after.stdout) if after.returncode == 0 else None
        if write.returncode != 0:
            row["error"] = write.stderr.strip()[:200]
    return row


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--manifest", type=Path, default=None)
    args = parser.parse_args(argv)

    import manifest  # noqa: PLC0415
    from manifest_identity import apply_project_identity  # noqa: PLC0415

    rows = []
    for project in manifest.load(args.manifest):
        if not project.repo:
            continue
        try:
            apply_project_identity(project.repo)
            rows.append(apply(project.repo, _gh, dry_run=args.dry_run))
        except SystemExit as exc:
            rows.append({"repo": project.repo, "error": str(exc)[:200]})
    print(json.dumps(rows, indent=2))
    return 1 if any("error" in r for r in rows) else 0


if __name__ == "__main__":
    sys.exit(main())
