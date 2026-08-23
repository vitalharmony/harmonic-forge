#!/usr/bin/env python3
"""Issue-scoped command receipts for Lane 2 (harmonic-forge#371).

Records one immutable JSON receipt per consequential command a Lane 2
session runs -- argv, exit code, SHA-256 digests of stdout/stderr, bounded
previews, a UTC timestamp -- so `l2_post.py` can compose a status report
from verified facts instead of free-form assertion. A non-zero exit
additionally writes a LOCKED.json marker under the same issue scope;
`l2_post.py` refuses ordinary status composition while it is present (see
its `--resolve-lock` path). A `--kind blocked` status is not gated by
this -- see l2_post.py's own lock check, which distinguishes "the
underlying command failed" from "Lane 2 correctly reported it is
blocked."

Receipts live under `<git-dir>/lane2-receipts/<issue>/` (`git rev-parse
--git-path`) -- inside `.git/`, so never staged and never committed.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import subprocess
import sys
from pathlib import Path

PREVIEW_BYTES = 2000


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def receipt_dir(issue: int) -> Path:
    result = subprocess.run(
        ["git", "rev-parse", "--git-path", f"lane2-receipts/{issue}"],
        capture_output=True, text=True, check=True,
    )
    path = Path(result.stdout.strip())
    path.mkdir(parents=True, exist_ok=True)
    return path


def lock_path(issue: int) -> Path:
    return receipt_dir(issue) / "LOCKED.json"


def is_locked(issue: int) -> bool:
    return lock_path(issue).exists()


def write_receipt(issue: int, kind: str, body: dict) -> Path:
    directory = receipt_dir(issue)
    timestamp = _now()
    payload = {"kind": kind, "timestamp": timestamp, **body}
    path = directory / f"{timestamp.replace(':', '-')}-{kind}.json"
    path.write_text(json.dumps(payload, indent=2, sort_keys=True))
    return path


def write_lock(issue: int, receipt_path: Path, exit_code: int) -> Path:
    path = lock_path(issue)
    path.write_text(json.dumps({
        "receipt": receipt_path.name,
        "exit_code": exit_code,
        "timestamp": _now(),
    }, indent=2, sort_keys=True))
    return path


def clear_lock(issue: int) -> None:
    lock_path(issue).unlink(missing_ok=True)


def run_command(issue: int, argv: list[str]) -> int:
    """Run argv, record a receipt, lock the issue on non-zero exit."""
    result = subprocess.run(argv, capture_output=True, text=True)
    receipt_path = write_receipt(issue, "command", {
        "argv": argv,
        "exit_code": result.returncode,
        "stdout_sha256": _digest(result.stdout.encode()),
        "stderr_sha256": _digest(result.stderr.encode()),
        "stdout_preview": result.stdout[:PREVIEW_BYTES],
        "stderr_preview": result.stderr[:PREVIEW_BYTES],
    })
    if result.returncode != 0:
        write_lock(issue, receipt_path, result.returncode)
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    print(f"[receipt] {receipt_path}", file=sys.stderr)
    return result.returncode


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run a command under a Lane 2 issue scope and record its receipt (harmonic-forge#371)."
    )
    parser.add_argument("--issue", type=int, required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER,
                         help="the command to run, after a literal '--'")
    args = parser.parse_args()
    if not args.command or args.command[0] != "--":
        parser.error("pass the command to run after a literal '--', e.g. --issue 371 -- pytest -q")
    return run_command(args.issue, args.command[1:])


if __name__ == "__main__":
    raise SystemExit(main())
