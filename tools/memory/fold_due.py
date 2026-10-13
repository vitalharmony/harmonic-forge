#!/usr/bin/env python3
"""Is a memory fold owed? (harmonic-forge#982)

Consolidation and promotion of the operator memory store have only ever run
when someone remembered to start them. This script decides, from local files
only (no network, no GitHub), whether Lane 1 owes a fold, measured against the
snapshot `--record` writes after each fold. Any one trigger is enough:

1. volume: 15 or more feedback memories whose `first_seen` is strictly after
   the marker's date (one first seen on the fold's own day was in that fold);
2. recurrence: the same non-empty `rule_ref` in two or more
   `protocol-failures.jsonl` records appended since the marker;
3. growth: `MEMORY.md` bytes, or this repo's always-loaded surface
   (`context_budget.surface`), up 10% or more since the snapshot. Absolute
   budgets are not used: HRSE2 is over its budget by design, and a fold
   cannot clear that;
4. fallback: seven days since the marker.

A missing or unreadable marker means "owed: no marker". It prints one line,
`memory-fold owed: <trigger>`, for the first trigger that fires, and nothing
otherwise. It always exits 0: a check that tells Lane 1 about owed work must
never break the hook that runs it.
"""
from __future__ import annotations

import argparse
import json
import os
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


def _memory_md_bytes(store: Path) -> int:
    try:
        return len((store / "MEMORY.md").read_bytes())
    except OSError:
        return 0


def _surface_bytes(repo: Path) -> int:
    try:
        return sum(size for _c, _p, size in context_budget.surface(repo))
    except Exception:  # noqa: BLE001 -- a surface we cannot measure is not growth
        return 0


def _feedback_first_seen(store: Path) -> list[date]:
    dates: list[date] = []
    for f in memory_lint.memory_files(store):
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        fm = memory_lint._parse_frontmatter(text)
        if not (f.name.startswith("feedback_") or fm.get("metadata.type", "").strip() == "feedback"):
            continue
        raw = fm.get("first_seen", "").strip()
        try:
            dates.append(date.fromisoformat(raw))
        except ValueError:
            continue
    return dates


def snapshot(store: Path, repo: Path, now: datetime) -> dict:
    return {
        "ts": now.isoformat(),
        "date": now.date().isoformat(),
        "store_count": len(memory_lint.memory_files(store)),
        "failures_offset": len(_failure_lines(store)),
        "memory_md_bytes": _memory_md_bytes(store),
        "surface_bytes": {str(repo): _surface_bytes(repo)},
    }


def read_marker(path: Path | None = None) -> dict | None:
    try:
        data = json.loads((path or marker_path()).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    required = ("ts", "date", "failures_offset", "memory_md_bytes", "surface_bytes")
    if not isinstance(data, dict) or any(k not in data for k in required):
        return None
    return data


def record(store: Path, repo: Path, now: datetime, path: Path | None = None) -> Path:
    """Write the snapshot atomically (temp file plus rename)."""
    target = path or marker_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    data = snapshot(store, repo, now)
    old = read_marker(target)
    if old and isinstance(old.get("surface_bytes"), dict):
        data["surface_bytes"] = {**old["surface_bytes"], **data["surface_bytes"]}
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=".marker-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle)
        os.replace(tmp, target)
    finally:
        Path(tmp).unlink(missing_ok=True)
    return target


def _grew(now_value: int, then_value: int) -> bool:
    return then_value > 0 and now_value >= then_value * (1 + GROWTH_FRACTION)


def owed(store: Path, repo: Path, now: datetime, marker: dict | None) -> list[str]:
    """Every trigger that fires, in order. Empty means no fold is owed."""
    if marker is None:
        return ["no marker"]
    try:
        marker_date = date.fromisoformat(str(marker["date"]))
        marker_ts = datetime.fromisoformat(str(marker["ts"]))
        offset = int(marker["failures_offset"])
    except (KeyError, TypeError, ValueError):
        return ["no marker"]
    if marker_ts.tzinfo is None:
        marker_ts = marker_ts.replace(tzinfo=timezone.utc)
    fired: list[str] = []

    new = sum(1 for d in _feedback_first_seen(store) if d > marker_date)
    if new >= VOLUME_THRESHOLD:
        fired.append(f"{new} new feedback memories since the last fold")

    counts: dict[str, int] = {}
    for line in _failure_lines(store)[max(offset, 0):]:
        try:
            ref = json.loads(line).get("rule_ref")
        except (ValueError, AttributeError):
            continue
        if isinstance(ref, str) and ref.strip():
            counts[ref.strip()] = counts.get(ref.strip(), 0) + 1
    recurring = sorted(r for r, n in counts.items() if n >= RECURRENCE_THRESHOLD)
    if recurring:
        fired.append(f"{', '.join(recurring)} failed again since the last fold")

    then_surface = (marker.get("surface_bytes") or {}).get(str(repo), 0) \
        if isinstance(marker.get("surface_bytes"), dict) else 0
    if _grew(_memory_md_bytes(store), int(marker.get("memory_md_bytes") or 0)):
        fired.append("MEMORY.md grew 10% or more since the last fold")
    elif _grew(_surface_bytes(repo), int(then_surface or 0)):
        fired.append("the always-loaded surface grew 10% or more since the last fold")

    if now - marker_ts >= timedelta(days=FALLBACK_DAYS):
        fired.append(f"{FALLBACK_DAYS} days since last fold")
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
        store = args.store or memory_lint.resolve_store()
        repo = args.repo.resolve()
        now = datetime.now(timezone.utc)
        if args.record:
            print(f"memory-fold recorded: {record(store, repo, now, args.marker)}")
            return 0
        fired = owed(store, repo, now, read_marker(args.marker))
        if fired:
            print(f"memory-fold owed: {fired[0]}")
    except Exception as exc:  # noqa: BLE001 -- always exit 0
        print(f"fold_due: internal error ({exc!r})", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
