#!/usr/bin/env python3
"""Read the belt tick log (harmonic-forge#518 AC17c).

A write-only telemetry file is the `LANE3_ACTIVE` marker defect repeated — this
repo already shipped a marker that was write-only and had to be cleared by hand,
which is why `lane3-end` exists. So the log ships with a read path in the same
issue, however trivial.

**This is deliberately not the analysis layer.** Aggregation, drift detection and
alarms are harmonic-forge#519, and they should be designed against a week of real
records rather than against an empty file. This answers only what can be answered
without judgment: what ran, what it cost, and which repos have never produced an
event.

Reads local JSONL. Makes no network call — telemetry that costs quota reproduces
the defect it exists to catch.

    python3 tools/gh/belt_report.py --log ~/Harmonic_Projects/testplan/ticks.jsonl
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path


def load(path: Path) -> list[dict]:
    if not path.exists():
        # Named, not silently empty. "No log at <path>" and "a log of zero
        # ticks" are different states, and the second is the one that means
        # the belt is dead.
        print(f"no tick log at {path}", file=sys.stderr)
        return []
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records


def report(records: list[dict], last: int) -> int:
    if not records:
        print("0 ticks recorded.")
        return 0

    rest = sum(r.get("calls_rest", 0) for r in records)
    graphql = sum(r.get("calls_graphql", 0) for r in records)
    triggers = Counter(r.get("trigger", "?") for r in records)
    locks = Counter(r.get("lock", "?") for r in records)

    print(f"ticks: {len(records)}   "
          f"first {records[0].get('ts')}   last {records[-1].get('ts')}")
    print(f"  triggers: {dict(triggers)}")
    print(f"  lock:     {dict(locks)}")
    print(f"  calls:    rest={rest}  graphql={graphql}")
    if graphql:
        # AC16. Not a statistic — a defect, and the report says so rather than
        # printing a number a reader has to know how to interpret.
        print(f"  ** {graphql} GraphQL call(s) from a scheduled path — "
              f"that is a defect, not a statistic (R-0019, AC16).")

    polled: dict[tuple[str, str], int] = defaultdict(int)
    errored: dict[tuple[str, str], int] = defaultdict(int)
    for rec in records:
        for row in rec.get("repos_polled", []):
            key = (row.get("account", "?"), row.get("repo", "?"))
            polled[key] += 1
            if not row.get("ok", True):
                errored[key] += 1

    emitted_by_repo: Counter = Counter()
    for rec in records:
        for item in rec.get("emitted", []):
            emitted_by_repo[str(item).split("#")[0]] += 1

    if polled:
        print("\n  per repo:")
        for (account, repo), n in sorted(polled.items()):
            events = emitted_by_repo.get(repo, 0)
            errs = errored.get((account, repo), 0)
            # 17b: a repo polled many times with zero events is a suspicious
            # row, not a clean one — it cannot be distinguished from a filter
            # that does not match its posts without saying so out loud.
            flag = ""
            if events == 0 and n >= 3:
                flag = "   <- polled, never produced an event: quiet or broken?"
            if errs:
                flag += f"   [{errs} failed call(s)]"
            print(f"    {account}/{repo:<24} polled={n:<5} events={events}{flag}")

    owed = [item for rec in records for item in rec.get("owed_found", [])]
    if owed:
        print(f"\n  own-output predicate found {len(owed)} unanswered: "
              f"{sorted(set(owed))[:10]}")

    if last:
        print(f"\n  last {last} tick(s):")
        for rec in records[-last:]:
            print(f"    {rec.get('ts')}  {rec.get('trigger'):<8} "
                  f"matched={len(rec.get('matched', []))} "
                  f"emitted={len(rec.get('emitted', []))} "
                  f"lock={rec.get('lock')}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--log", required=True, type=Path, help="path to the JSONL tick log")
    parser.add_argument("--last", type=int, default=10, help="show the last N ticks")
    args = parser.parse_args()
    return report(load(args.log), args.last)


if __name__ == "__main__":
    raise SystemExit(main())
