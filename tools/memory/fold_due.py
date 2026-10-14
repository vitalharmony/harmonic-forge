#!/usr/bin/env python3
"""Is a memory fold owed? (harmonic-forge#982)

Consolidation and promotion of the operator memory store have only ever run
when someone remembered to start them. This script decides, from local files
only (no network, no GitHub), whether Lane 1 owes a fold, measured against the
snapshot `--record` writes after each fold. Any one trigger is enough:

1. volume: 15 or more feedback memories absent from the last snapshot;
2. recurrence: the same non-empty `rule_ref` in two or more
   `protocol-failures.jsonl` records appended since the marker;
3. growth: `MEMORY.md` bytes, or this repo's always-loaded surface
   (`context_budget.surface`), up 10% or more since the snapshot. Absolute
   budgets are not used: HRSE2 is over its budget by design, and a fold
   cannot clear that;
4. fallback: seven days since the marker.

A missing or unreadable marker means "owed: no marker". It prints one line,
`memory-fold owed: <trigger>`, for the first trigger that fires, and nothing
otherwise. The check always exits 0: a check that tells Lane 1 about owed work must
never break the hook that runs it. `--record` exits 1 and writes nothing when
a measurement fails.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

import context_budget  # noqa: E402
import memory_lint  # noqa: E402

VOLUME_THRESHOLD = 15
RECURRENCE_THRESHOLD = 2
GROWTH_FRACTION = 0.10
FALLBACK_DAYS = 7


def state_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "harmonic-forge" / "memory_fold"


def marker_path() -> Path:
    return state_dir() / "marker.json"


def _failures_path(store: Path) -> Path:
    return store / "protocol-failures.jsonl"


def _failure_lines(store: Path) -> list[str]:
    try:
        return _failures_path(store).read_text(encoding="utf-8").splitlines()
    except OSError:
        return []


def _memory_md_bytes(store: Path) -> int | None:
    try:
        return len((store / "MEMORY.md").read_bytes())
    except OSError:
        return None


def _surface_bytes(repo: Path) -> int | None:
    try:
        return sum(size for _c, _p, size in context_budget.surface(repo))
    except Exception:  # noqa: BLE001 -- None: unmeasured, never a 0 baseline
        return None


def _feedback_names(store: Path) -> set[str]:
    """Feedback memories by filename or `metadata.type`. Volume counts files
    absent from the last snapshot, not dates: a `first_seen` is a day and the
    snapshot is a moment, so a date test misses files written after a fold on
    the fold's own day (preclose pass 1)."""
    names: set[str] = set()
    for f in memory_lint.memory_files(store):
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        fm = memory_lint._parse_frontmatter(text)
        if f.name.startswith("feedback_") or fm.get("metadata.type", "").strip() == "feedback":
            names.add(f.name)
    return names


def repo_key(repo: Path) -> tuple[str, Path]:
    """A repo's identity and its checkout root. Keyed on the git common dir, so
    every worktree and subdirectory of one repo shares a baseline."""
    try:
        common = subprocess.run(["git", "-C", str(repo), "rev-parse", "--path-format=absolute",
                                 "--git-common-dir"],
                                capture_output=True, text=True, timeout=5, check=True).stdout.strip()
        listing = subprocess.run(["git", "-C", str(repo), "worktree", "list", "--porcelain"],
                                 capture_output=True, text=True, timeout=5, check=True).stdout
        # The main worktree is listed first. Every checkout of the repo shares
        # one baseline, so it is always measured from that one (sticky-wicket):
        # a feature worktree's branch must never become main's baseline.
        main_tree = listing.splitlines()[0].split(" ", 1)[1]
        return str(Path(common).resolve()), Path(main_tree)
    except (OSError, subprocess.SubprocessError, IndexError):
        return str(repo.resolve()), repo.resolve()


#: Repos whose surface every --record re-measures, so a fold recorded from one
#: lane repo still baselines the others (preclose pass 1).
DEFAULT_REPOS = (Path.home() / "harmonic-forge", Path.home() / "Harmonic_Projects" / "HRSE2")


def _head_hash(lines: list[str], n: int) -> str:
    return hashlib.sha256("\n".join(lines[:n]).encode()).hexdigest()[:16]


class MeasurementError(RuntimeError):
    """A --record measurement failed; nothing is written (sticky-wicket: a
    failed measurement must never become a 0 baseline)."""


def snapshot(store: Path, repo: Path, now: datetime, known: dict | None = None) -> dict:
    lines = _failure_lines(store)
    surfaces: dict[str, dict] = {}
    roots = [Path(v["path"]) for v in (known or {}).values()
             if isinstance(v, dict) and isinstance(v.get("path"), str)]
    for root in [*roots, *DEFAULT_REPOS, repo]:
        if root.is_dir():
            key, top = repo_key(root)
            size = _surface_bytes(top)
            if size is None:
                raise MeasurementError(f"could not measure the always-loaded surface of {top}")
            surfaces[key] = {"path": str(top), "bytes": size}
    md = _memory_md_bytes(store)
    if md is None:
        raise MeasurementError(f"could not read {store / 'MEMORY.md'}")
    return {
        "ts": now.isoformat(),
        "date": now.date().isoformat(),
        "feedback_names": sorted(_feedback_names(store)),
        "failures_offset": len(lines),
        "failures_head": _head_hash(lines, len(lines)),
        "memory_md_bytes": md,
        "surface_bytes": surfaces,
    }


def read_marker(path: Path | None = None) -> dict | None:
    """The marker, or None when it is missing or any field is unusable: an
    untrusted marker reads as "no marker", never as quiet (preclose pass 1)."""
    try:
        data = json.loads((path or marker_path()).read_text(encoding="utf-8"))
        datetime.fromisoformat(str(data["ts"]))
        date.fromisoformat(str(data["date"]))
        ok = (isinstance(data["feedback_names"], list)
              and all(isinstance(n, str) for n in data["feedback_names"])
              and isinstance(data["failures_offset"], int) and data["failures_offset"] >= 0
              and isinstance(data["failures_head"], str)
              and isinstance(data["memory_md_bytes"], int)
              and isinstance(data["surface_bytes"], dict)
              and all(isinstance(v, dict) and isinstance(v.get("bytes"), int)
                      and isinstance(v.get("path"), str) for v in data["surface_bytes"].values()))
    except (OSError, ValueError, TypeError, KeyError):
        return None
    return data if ok else None


def record(store: Path, repo: Path, now: datetime, path: Path | None = None) -> Path:
    """Write the snapshot atomically (temp file plus rename)."""
    target = path or marker_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    old = read_marker(target)
    data = snapshot(store, repo, now, old["surface_bytes"] if old else None)
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=".marker-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle)
        os.replace(tmp, target)
    finally:
        Path(tmp).unlink(missing_ok=True)
    return target


def _grew(now_value: int, then_value: int) -> bool:
    """Up 10% or more, in integers (a float product can miss exactly 10%)."""
    return then_value > 0 and now_value * 10 >= then_value * 11


#: Trigger kinds, in report order. The hook suppresses by kind, not by text.
KINDS = ("no-marker", "volume", "recurrence", "growth", "fallback")


def owed(store: Path, repo: Path, now: datetime, marker: dict | None) -> list[tuple[str, str]]:
    """Every trigger that fires, as (kind, message), in order. Empty: none owed."""
    if marker is None:
        return [("no-marker", "no marker")]
    marker_ts = datetime.fromisoformat(str(marker["ts"]))
    if marker_ts.tzinfo is None:
        marker_ts = marker_ts.replace(tzinfo=timezone.utc)
    fired: list[tuple[str, str]] = []

    new = len(_feedback_names(store) - set(marker["feedback_names"]))
    if new >= VOLUME_THRESHOLD:
        fired.append(("volume", f"{new} new feedback memories since the last fold"))

    lines = _failure_lines(store)
    offset = marker["failures_offset"]
    if offset > len(lines) or _head_hash(lines, offset) != marker["failures_head"]:
        offset = 0  # the log was truncated or replaced: count all of it
    counts: dict[str, int] = {}
    for line in lines[offset:]:
        try:
            ref = json.loads(line).get("rule_ref")
        except (ValueError, AttributeError):
            continue
        if isinstance(ref, str) and ref.strip():
            counts[ref.strip()] = counts.get(ref.strip(), 0) + 1
    recurring = sorted(r for r, n in counts.items() if n >= RECURRENCE_THRESHOLD)
    if recurring:
        fired.append(("recurrence", f"{', '.join(recurring)} failed again since the last fold"))

    key, top = repo_key(repo)
    base = marker["surface_bytes"].get(key)
    if _grew(_memory_md_bytes(store) or 0, marker["memory_md_bytes"]):
        fired.append(("growth", "MEMORY.md grew 10% or more since the last fold"))
    elif base is None:
        fired.append(("growth", f"no surface baseline for {top}; run the fold from here"))
    elif _grew(_surface_bytes(top) or 0, base["bytes"]):
        fired.append(("growth", "the always-loaded surface grew 10% or more since the last fold"))

    if now - marker_ts >= timedelta(days=FALLBACK_DAYS):
        fired.append(("fallback", f"{FALLBACK_DAYS} days since last fold"))
    return fired


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--store", type=Path, help="Memory store (default: autoMemoryDirectory).")
    parser.add_argument("--repo", type=Path, default=Path.cwd(),
                        help="Repo whose always-loaded surface is measured (default: cwd).")
    parser.add_argument("--record", action="store_true",
                        help="Record the post-fold snapshot; run at the end of a fold.")
    parser.add_argument("--marker", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        repo = args.repo.resolve()
        store = args.store or memory_lint.resolve_store(repo_key(repo)[1])
        now = datetime.now(timezone.utc)
        if args.record:
            try:
                print(f"memory-fold recorded: {record(store, repo, now, args.marker)}")
            except MeasurementError as exc:
                print(f"fold_due: not recorded: {exc}", file=sys.stderr)
                return 1
            return 0
        fired = owed(store, repo, now, read_marker(args.marker))
        if fired:
            print(f"memory-fold owed: {fired[0][1]}")
            print(f"kinds: {','.join(kind for kind, _m in fired)}")
    except Exception as exc:  # noqa: BLE001 -- always exit 0
        print(f"fold_due: internal error ({exc!r})", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
