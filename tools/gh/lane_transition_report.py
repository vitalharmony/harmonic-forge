#!/usr/bin/env python3
"""Report bounded, elapsed lane-transition outliers from GitHub issue threads."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import subprocess
from collections import defaultdict

L1 = re.compile(r"<!-- l1-post v1; kind=([^; ]+)")
L2 = re.compile(r"(?im)^#{1,4}\s+L2D\b")
SPEC = re.compile(r"(?im)^#{1,4}\s+Lane 3 Test Spec\b")
GATE = re.compile(r"(?im)^#{1,4}\s+Lane 3 Gate Results\b")


def kind(body: str) -> str | None:
    marker = L1.search(body)
    if marker:
        return {"handoff": "handoff", "ready-for-l3": "ready", "ae": "ae",
                "rework": "rework"}.get(marker.group(1))
    if L2.search(body): return "complete"
    if SPEC.search(body): return "spec"
    if GATE.search(body): return "gate"
    return None


def transitions(issue: int, comments: list[dict]) -> list[tuple[int, str, float]]:
    events = [(dt.datetime.fromisoformat(c["created_at"].replace("Z", "+00:00")), kind(c["body"]))
              for c in comments]
    events = [(at, name) for at, name in events if name]
    rows = []
    for (start, left), (end, right) in zip(events, events[1:]):
        rows.append((issue, f"{left}->{right}", (end - start).total_seconds()))
    return rows


def api(path: str) -> object:
    result = subprocess.run(["gh-as", "vitalharmony", "gh", "api", path], text=True,
                            capture_output=True)
    if result.returncode:
        raise SystemExit(result.stderr.strip() or "GitHub API failed")
    return json.loads(result.stdout)


def pct(values: list[float], q: float) -> float:
    values = sorted(values)
    return values[round((len(values) - 1) * q)]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args()
    since = (dt.datetime.now(dt.UTC) - dt.timedelta(days=args.days)).isoformat().replace("+00:00", "Z")
    issues = api(f"repos/{args.repo}/issues?state=all&since={since}&per_page=100")
    rows = []
    sampled = 0
    for record in issues:
        if record.get("pull_request"):
            continue
        sampled += 1
        rows += transitions(record["number"], api(f"repos/{args.repo}/issues/{record['number']}/comments?per_page=100"))
    print(f"window={args.days}d issues={sampled} transitions={len(rows)}")
    print("durations are elapsed-only; blocked-on-operator is not derivable from current structured comments.")
    by_type = defaultdict(list)
    for _, name, seconds in rows: by_type[name].append(seconds)
    for name in sorted(by_type):
        values = by_type[name]
        print(f"{name} n={len(values)} p50={pct(values,.5):.0f}s p95={pct(values,.95):.0f}s")
    for issue, name, seconds in sorted(rows, key=lambda row: row[2], reverse=True)[:args.limit]:
        print(f"outlier issue=#{issue} transition={name} elapsed={seconds:.0f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
