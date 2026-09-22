#!/usr/bin/env python3
"""Append-only attribution log for HEAD moves in a lane worktree.

## Why this exists

`lane3-end` runs `rm -f "$git_dir/LANE3_ACTIVE"`. The marker was the only
artifact carrying the owner pid and the claim's timestamp, so once a gate
ended, post-hoc attribution was impossible: `git reflog` records the ref and
the time and nothing about the invoking process.

That is not theoretical. Triaging the 2026-09-09 incident produced three
attributions from the surviving evidence and two were wrong, and the third —
the correct one — was unconfirmable, because the marker that would have
settled it had been deleted. A gate-integrity incident nobody can attribute is
re-diagnosed from scratch every time it recurs. It already had been, twice, on
that one.

## Why it records raw `git`, not only `gate-checkout`

The issue's AC1 asks for an audit of "every HEAD move a lane worktree undergoes
through `gate-checkout`". That wording would not have caught the incident it
was written from. The disrupting checkout **never went through
`gate-checkout`** — it was a bare `git checkout --detach origin/main` run by a
Lane 1 session advancing worktrees, and no mise task, marker or lease sees a
sibling process calling `git` directly.

So the record is written from two places, and the second is the one that makes
AC1's own question ("which session moved HEAD at 11:41:01") answerable:

  * `gate-checkout` and `lane3_marker.py` — the sanctioned path, and the
    only place a REFUSAL can be recorded, since a refused checkout never
    reaches git at all.
  * `.githooks/post-checkout` — every HEAD move in the worktree, whoever
    caused it. Git hooks are the one layer that sees raw `git`.

## Where it lives, and why not beside the marker

`$(git rev-parse --git-common-dir)/lane3-audit.jsonl` — the COMMON dir, shared
by every worktree of the repo, not the per-worktree git dir. Two reasons: it
survives `lane3-end`, which only removes `LANE3_ACTIVE` from the per-worktree
dir; and cross-worktree attribution is the actual question — "who moved
`<project>-lane3`'s HEAD" is asked by someone sitting in the main checkout.

Untracked, unignored-by-nothing, and never read by the gate itself. This is
evidence, not control flow: nothing here may refuse anything, because a
logging failure must never be able to block a gate.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

AUDIT_FILENAME = "lane3-audit.jsonl"

#: Keep the log bounded without a cron job. Records carry the full ancestry
#: chain and measure ~600-900 bytes each (measured, not estimated), so this
#: bound is ~1.5 MB. The interesting window is always the recent one, and the
#: whole file is read and rewritten only when the bound is actually exceeded.
MAX_RECORDS = 2000


def _git(*args: str, cwd: Path | None = None) -> str | None:
    try:
        result = subprocess.run(
            ["git", *args], cwd=str(cwd) if cwd else None,
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def audit_path(cwd: Path | None = None) -> Path | None:
    common = _git("rev-parse", "--git-common-dir", cwd=cwd)
    if common is None:
        return None
    path = Path(common)
    if not path.is_absolute():
        root = _git("rev-parse", "--show-toplevel", cwd=cwd)
        if root is None:
            return None
        path = Path(root) / path
    return path / AUDIT_FILENAME


def _hook_names(cwd: Path | None = None) -> set[str]:
    """Filenames in `.githooks/`, which appear in `/proc` as their own comm.

    A git hook script runs as a process named after the script — `post-checkout`,
    not `bash` — so the plain shell/mise transparency list walks straight into
    it and stops. Read from the directory rather than hardcoded, so adding a
    hook does not silently break attribution.
    """
    root = _git("rev-parse", "--show-toplevel", cwd=cwd)
    if root is None:
        return set()
    hooks = Path(root) / ".githooks"
    if not hooks.is_dir():
        return set()
    return {p.name for p in hooks.iterdir() if p.is_file()}


def _marker_on_path() -> None:
    """The marker's protocol half is this module's sibling (harmonic-forge#721).

    It was the consuming repo's `scripts/check_lane3_marker.py` until #721 cut
    the seam; the temporary three-place search that bridged #720 and #721 is
    gone with it. Both callers below swallow an ImportError and answer empty,
    so a wrong path here would silently drop the owner from every audit record.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parent))


def ancestry_chain(cwd: Path | None = None) -> list[dict]:
    """`[{pid, comm}, ...]` from here to init.

    Recorded verbatim alongside the resolved owner because the resolution is a
    heuristic and the chain is a fact. Attribution should not depend on the
    walk having stopped in exactly the right place: the 2026-09-09 triage
    failed because the evidence was gone, not because it was hard to read.
    """
    try:
        _marker_on_path()
        from lane3_marker import ancestry, _stat  # noqa: PLC0415

        out = []
        for pid in ancestry():
            info = _stat(pid)
            out.append({"pid": pid, "comm": info[0] if info else None})
        return out
    except Exception:
        return []


def session_owner(cwd: Path | None = None) -> int | None:
    """The session identity `lane3_marker.py` stamps on the marker.

    Shares that module's transparency list rather than reimplementing it — two
    answers to "who am I" would drift, and the record exists to be compared
    against what the marker said. Extended here with the `.githooks/` script
    names, which only matter on this call path.
    """
    try:
        _marker_on_path()
        from lane3_marker import _TRANSPARENT, ancestry, _stat  # noqa: PLC0415

        skip = set(_TRANSPARENT) | _hook_names(cwd)
        for pid in ancestry():
            info = _stat(pid)
            if info is None:
                continue
            if info[0] not in skip:
                return pid
        return None
    except Exception:
        return None


def record(event: str, outcome: str, *, worktree: Path | None = None,
           prior_head: str | None = None, target: str | None = None,
           detail: str | None = None, path: Path | None = None) -> bool:
    """Append one record. Returns False on any failure, and never raises.

    Fail-open is not a compromise here, it is required: this is called from a
    git hook and from a gate guard, and an audit write that could fail either
    of those would be a logging system with the power to stop a gate.
    """
    try:
        cwd = worktree or Path.cwd()
        target_path = path or audit_path(cwd)
        if target_path is None:
            return False
        entry = {
            "at": datetime.now(timezone.utc).isoformat(),
            "event": event,
            "outcome": outcome,
            "worktree": str(cwd),
            "session_owner": session_owner(cwd),
            "ancestry": ancestry_chain(cwd),
            "pid": os.getpid(),
            "prior_head": prior_head,
            "target": target,
        }
        if detail:
            entry["detail"] = detail
        target_path.parent.mkdir(parents=True, exist_ok=True)
        # The log lives in the shared --git-common-dir, so EVERY worktree of
        # the repo appends to this one file. Append is atomic enough on its
        # own for short lines, but `_trim`'s read-modify-write is not: without
        # the lock, a trim in one worktree can rewrite a stale snapshot over a
        # record another worktree appended in between, silently discarding
        # exactly the refusal evidence AC2 exists to keep. One exclusive lock
        # spans both, so a trim can never straddle someone else's append.
        with target_path.open("a", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                handle.write(json.dumps(entry) + "\n")
                handle.flush()
                _trim(target_path)
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return True
    except Exception:
        return False


def _trim(path: Path) -> None:
    """Bound the log. Caller MUST hold the exclusive lock — see `record()`."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines(True)
        if len(lines) > MAX_RECORDS:
            path.write_text("".join(lines[-MAX_RECORDS:]), encoding="utf-8")
    except Exception:
        return


def read(path: Path | None = None, cwd: Path | None = None) -> list[dict]:
    target = path or audit_path(cwd)
    if target is None or not target.is_file():
        return []
    out = []
    for line in target.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Lane 3 HEAD-move audit log")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_rec = sub.add_parser("record", help="append one record")
    p_rec.add_argument("--event", required=True,
                       help="gate-checkout | post-checkout | marker-check")
    p_rec.add_argument("--outcome", required=True, help="allowed | refused | moved")
    p_rec.add_argument("--prior-head")
    p_rec.add_argument("--target")
    p_rec.add_argument("--detail")

    p_show = sub.add_parser("show", help="print the log, newest last")
    p_show.add_argument("-n", type=int, default=20)

    args = parser.parse_args(argv)
    if args.cmd == "record":
        ok = record(args.event, args.outcome, prior_head=args.prior_head,
                    target=args.target, detail=args.detail)
        return 0 if ok else 0  # never fail a caller on a logging problem
    for entry in read()[-args.n:]:
        print(json.dumps(entry))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
