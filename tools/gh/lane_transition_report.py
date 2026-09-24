#!/usr/bin/env python3
"""Report bounded, elapsed lane-transition outliers from GitHub issue threads.

harmonic-forge#745 AC2/AC3: also reports readiness/CI overlap, parsed from
the `lane-pr-link` marker's additive timing fields (harmonic-forge#745 AC1).
A marker with no timing fields (pre-#745, or a repo that never adopted the
addition) is counted as unknown, never fabricated (TC4) -- see
`marker_overlap()`.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import subprocess
from collections import defaultdict

import gate_ci

L1 = re.compile(r"<!-- l1-post v1; kind=([^; ]+)")
L2 = re.compile(r"(?im)^#{1,4}\s+L2D\b")
SPEC = re.compile(r"(?im)^#{1,4}\s+Lane 3 Test Spec\b")
GATE = re.compile(r"(?im)^#{1,4}\s+Lane 3 Gate Results\b")
MARKER = re.compile(r"<!-- lane-pr-link v1;(?P<fields>.*?) -->", re.DOTALL)


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


def _parse_marker_fields(body: str) -> dict[str, str] | None:
    """`None` if no `lane-pr-link` marker is present. A marker missing some
    (or all) of harmonic-forge#745's timing fields still parses -- the
    missing keys are simply absent from the returned dict, never guessed."""
    m = MARKER.search(body)
    if not m:
        return None
    fields: dict[str, str] = {}
    for part in m.group("fields").split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        key, value = part.split("=", 1)
        fields[key.strip()] = value.strip()
    return fields


def _raw_check_run(repo: str, sha: str, name: str = "verify") -> dict | None:
    """The latest check-run named `name` for `sha` in `repo`, or `None`.
    Queried separately from `gate_ci.ci_conclusion()` because the two answer
    different questions -- `ci_conclusion` owns the authoritative fail-closed
    VERDICT (green/red/pending/absent/unknown, harmonic-forge#504); this
    returns the raw object so `marker_overlap()` can read `completed_at` for
    the actual elapsed-time math AC2 needs, which `ci_conclusion`'s tri-state
    summary alone can't provide."""
    result = subprocess.run(
        ["gh", "api", "--paginate", f"repos/{repo}/commits/{sha}/check-runs?per_page=100",
         "--jq", ".check_runs[]"],
        text=True, capture_output=True,
    )
    if result.returncode:
        return None
    runs = []
    for line in (result.stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            runs.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    named = [r for r in runs if r.get("name") == name]
    if not named:
        return None
    named.sort(key=lambda r: r.get("started_at") or "")
    return named[-1]


def marker_overlap(comments: list[dict]) -> list[dict]:
    """One row per `lane-pr-link` marker found in `comments`. Each row
    always carries `ci_snapshot_state` (`None` for a pre-#745 marker --
    TC4's "unknown, not fabricated"). For a marker whose snapshot was
    `pending`, re-queries the PR's marker `pr-repo`/`head-sha` (never the
    issue repo -- the two can differ, per the handoff's own Pre-Flight
    Preconditions) for the CURRENT state, once, at report-generation time --
    by construction well after the original snapshot, so CI has usually
    settled by then. This is the "re-query final state" the handoff
    requires; `post_kind()` itself never waits or re-queries."""
    rows = []
    for c in comments:
        fields = _parse_marker_fields(c.get("body", ""))
        if fields is None:
            continue
        row = {
            "pr_repo": fields.get("pr-repo"),
            "head_sha": fields.get("head-sha"),
            "local_check_start": fields.get("local-check-start"),
            "local_check_end": fields.get("local-check-end"),
            "ci_snapshot_state": fields.get("ci-snapshot-state"),
            "ci_snapshot_at": fields.get("ci-snapshot-at"),
            "final_ci_state": None,
            "post_ready_remaining_ci_seconds": None,
        }
        if row["pr_repo"] and row["head_sha"] and row["ci_snapshot_state"] == "pending":
            final_state, _detail = gate_ci.ci_conclusion(row["pr_repo"], row["head_sha"], required={"verify"})
            row["final_ci_state"] = final_state
            if final_state in ("green", "red"):
                run = _raw_check_run(row["pr_repo"], row["head_sha"])
                if run and run.get("completed_at") and row["ci_snapshot_at"]:
                    completed = dt.datetime.fromisoformat(run["completed_at"].replace("Z", "+00:00"))
                    snapshot = dt.datetime.fromisoformat(row["ci_snapshot_at"])
                    row["post_ready_remaining_ci_seconds"] = (completed - snapshot).total_seconds()
        rows.append(row)
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
    overlap_rows: list[dict] = []
    sampled = 0
    for record in issues:
        if record.get("pull_request"):
            continue
        sampled += 1
        comments = api(f"repos/{args.repo}/issues/{record['number']}/comments?per_page=100")
        rows += transitions(record["number"], comments)
        overlap_rows += marker_overlap(comments)
    print(f"window={args.days}d issues={sampled} transitions={len(rows)}")
    print("durations are elapsed-only; blocked-on-operator is not derivable from current structured comments.")
    by_type = defaultdict(list)
    for _, name, seconds in rows: by_type[name].append(seconds)
    for name in sorted(by_type):
        values = by_type[name]
        print(f"{name} n={len(values)} p50={pct(values,.5):.0f}s p95={pct(values,.95):.0f}s")
    for issue, name, seconds in sorted(rows, key=lambda row: row[2], reverse=True)[:args.limit]:
        print(f"outlier issue=#{issue} transition={name} elapsed={seconds:.0f}s")

    # harmonic-forge#745 AC2/AC3.
    unknown = [r for r in overlap_rows if r["ci_snapshot_state"] is None]
    resolved = [r for r in overlap_rows if r["post_ready_remaining_ci_seconds"] is not None]
    print(f"\nreadiness/CI overlap: markers={len(overlap_rows)} "
          f"unknown-timing={len(unknown)} resolved={len(resolved)}")
    if resolved:
        values = [r["post_ready_remaining_ci_seconds"] for r in resolved]
        print(f"post-ready remaining CI time: p50={pct(values,.5):.0f}s p95={pct(values,.95):.0f}s")
    if len(resolved) >= 20:
        median = pct([r["post_ready_remaining_ci_seconds"] for r in resolved], .5)
        if median > 0:
            print(f"recommendation (n={len(resolved)}): CI regularly finishes {median:.0f}s after "
                  "readiness -- a non-serializing reduction (e.g. Lane 3 beginning on independent "
                  "evidence rather than this marker) is worth designing. Do not change the "
                  "readiness contract from this report alone (AC3) -- file it as its own issue.")
        else:
            print(f"recommendation (n={len(resolved)}): retain the current flow -- CI is not "
                  "meaningfully outrunning readiness at this sample size.")
    else:
        print(f"recommendation: n={len(resolved)}, below the 20-observation bar (AC3) -- "
              "no recommendation yet.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
