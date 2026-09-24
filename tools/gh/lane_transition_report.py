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


def _gh_as_run(cmd: list[str]) -> tuple[int, str]:
    """`gate_ci.ci_conclusion()`'s injectable `run` -- scoped to
    `gh-as vitalharmony` like every other GitHub read in this file (preclose
    finding: the two new #745 read paths were the only ones in this module
    still shelling bare `gh`, which silently reads under whatever account
    happens to be active in the caller's shell rather than the account this
    report is written against)."""
    try:
        result = subprocess.run(["gh-as", "vitalharmony", *cmd], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, str(exc)
    return result.returncode, result.stdout if result.returncode == 0 else result.stderr


def _raw_check_run(repo: str, sha: str, name: str = "verify") -> dict | None:
    """The latest check-run named `name` for `sha` in `repo`, or `None`.
    Queried separately from `gate_ci.ci_conclusion()` because the two answer
    different questions -- `ci_conclusion` owns the authoritative fail-closed
    VERDICT (green/red/pending/absent/unknown, harmonic-forge#504); this
    returns the raw object so `marker_overlap()` can read `started_at`/
    `completed_at` for the actual elapsed-time math AC2 needs, which
    `ci_conclusion`'s tri-state summary alone can't provide. `gh-as
    vitalharmony`-scoped, matching `api()` below (preclose finding)."""
    result = subprocess.run(
        ["gh-as", "vitalharmony", "gh", "api", "--paginate",
         f"repos/{repo}/commits/{sha}/check-runs?per_page=100", "--jq", ".check_runs[]"],
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


def _parse_utc(value: str | None) -> dt.datetime | None:
    """`None` on absence OR malformation -- never raises. Preclose finding:
    an unguarded `fromisoformat()` on a truncated/hand-edited/quoted
    timestamp took down the entire report, including the pre-#745 transition
    output that has nothing to do with this feature."""
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None  # naive -- can't safely subtract against aware timestamps below
    return parsed


def marker_overlap(comments: list[dict]) -> list[dict]:
    """One row per DISTINCT `(pr-repo, head-sha)` -- preclose finding: one
    row per COMMENT double- (or N-) counts a re-posted readiness marker or
    any later comment that happens to quote one (a Lane 3 gate report citing
    its provenance, a Lane 1 review pasting the marker verbatim), inflating
    both the AC3 sample-size floor and its median. Last occurrence wins.

    Each row always carries `ci_snapshot_state` (`None` for a pre-#745
    marker -- TC4's "unknown, not fabricated"). For every row with usable
    timing fields and a resolvable `(pr_repo, head_sha)`, re-queries CURRENT
    state via `gate_ci.ci_conclusion()` (never the marker's own possibly-
    stale snapshot) and fetches the raw check-run for its `started_at`/
    `completed_at` -- both `gh-as vitalharmony`-scoped. This runs regardless
    of whether the marker's own snapshot already said `green`/`red`:
    preclose finding -- restricting the re-query to `state == 'pending'`
    excluded every already-resolved-at-ready observation, which are exactly
    the zero-overlap cases AC3 needs to be able to conclude "retain the
    current flow" at all.

    `overlap_seconds` and `post_ready_remaining_ci_seconds` are both
    populated only once the check run has a `completed_at` -- an
    unresolved re-query (`api-error` or still `pending`) leaves both `None`
    and is reported as its own explicit bucket by `main()`, never merged
    into "unknown" (pre-#745, no timing at all) or silently dropped
    (preclose finding)."""
    by_key: dict[tuple[str, str], dict] = {}
    for c in comments:
        fields = _parse_marker_fields(c.get("body", ""))
        if fields is None:
            continue
        pr_repo = fields.get("pr-repo")
        head_sha = fields.get("head-sha")
        row = {
            "pr_repo": pr_repo,
            "head_sha": head_sha,
            "local_check_start": fields.get("local-check-start"),
            "local_check_end": fields.get("local-check-end"),
            "ci_snapshot_state": fields.get("ci-snapshot-state"),
            "ci_snapshot_at": fields.get("ci-snapshot-at"),
            "final_ci_state": None,
            "overlap_seconds": None,
            "post_ready_remaining_ci_seconds": None,
        }
        key = (pr_repo, head_sha) if pr_repo and head_sha else (id(c), 0)
        by_key[key] = row  # last occurrence wins -- see docstring

    rows = list(by_key.values())
    for row in rows:
        if not (row["pr_repo"] and row["head_sha"] and row["local_check_start"] and row["local_check_end"]):
            continue
        local_start = _parse_utc(row["local_check_start"])
        local_end = _parse_utc(row["local_check_end"])
        if local_start is None or local_end is None:
            continue
        final_state, _detail = gate_ci.ci_conclusion(
            row["pr_repo"], row["head_sha"], required={"verify"}, run=_gh_as_run,
        )
        row["final_ci_state"] = final_state
        run = _raw_check_run(row["pr_repo"], row["head_sha"])
        ci_started = _parse_utc(run.get("started_at")) if run else None
        ci_completed = _parse_utc(run.get("completed_at")) if run else None
        if ci_started is not None:
            overlap_end = min(local_end, ci_completed) if ci_completed is not None else local_end
            row["overlap_seconds"] = max(0.0, (overlap_end - max(local_start, ci_started)).total_seconds())
        if ci_completed is not None:
            row["post_ready_remaining_ci_seconds"] = max(0.0, (ci_completed - local_end).total_seconds())
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


def overlap_report_lines(overlap_rows: list[dict]) -> list[str]:
    """harmonic-forge#745 AC2/AC3's printable summary, pure and directly
    testable -- pulled out of `main()` so the bucketing/recommendation logic
    can be exercised without a network mock (preclose finding: "no test
    exercises `main()`'s recommendation branches at all").

    Four explicit buckets, not two, per the preclose review: a marker can be
    `unknown` (no timing at all -- pre-#745), `unresolved` (timing present,
    but the re-query never reached a terminal CI state -- API failure or
    still pending at report time), or `resolved` (timing present AND a
    terminal CI state, so overlap and remaining-time are both real numbers,
    possibly zero). Only `resolved` counts toward AC3's floor -- and,
    unlike the prior version, `resolved` includes zero-overlap observations
    (CI already green/red at ready time), because excluding them made
    "retain the current flow" an unreachable conclusion by construction."""
    unknown = [r for r in overlap_rows if r["ci_snapshot_state"] is None]
    has_timing = [r for r in overlap_rows if r["ci_snapshot_state"] is not None]
    resolved = [r for r in has_timing if r["post_ready_remaining_ci_seconds"] is not None]
    unresolved = [r for r in has_timing if r["post_ready_remaining_ci_seconds"] is None]
    lines = [
        f"readiness/CI overlap: markers={len(overlap_rows)} unknown-timing={len(unknown)} "
        f"unresolved={len(unresolved)} resolved={len(resolved)}",
    ]
    overlap_values = [r["overlap_seconds"] for r in resolved if r["overlap_seconds"] is not None]
    if resolved:
        remaining_values = [r["post_ready_remaining_ci_seconds"] for r in resolved]
        lines.append(f"post-ready remaining CI time: p50={pct(remaining_values,.5):.0f}s "
                      f"p95={pct(remaining_values,.95):.0f}s")
    if overlap_values:
        nonzero = sum(1 for v in overlap_values if v > 0)
        lines.append(f"overlap: p50={pct(overlap_values,.5):.0f}s p95={pct(overlap_values,.95):.0f}s "
                      f"({nonzero}/{len(overlap_values)} readiness posts actually overlapped CI)")
    if len(resolved) >= 20:
        nonzero_fraction = (sum(1 for v in overlap_values if v > 0) / len(overlap_values)) if overlap_values else 0.0
        if nonzero_fraction > 0:
            median_overlap = pct(overlap_values, .5)
            lines.append(
                f"recommendation (n={len(resolved)}): {nonzero_fraction:.0%} of readiness posts "
                f"overlapped CI (median overlap {median_overlap:.0f}s among those) -- a "
                "non-serializing reduction (e.g. Lane 3 beginning on independent evidence rather "
                "than this marker) is worth designing. Do not change the readiness contract from "
                "this report alone (AC3) -- file it as its own issue.")
        else:
            lines.append(f"recommendation (n={len(resolved)}): retain the current flow -- CI was not "
                          "observed overlapping readiness in this sample.")
    else:
        lines.append(f"recommendation: n={len(resolved)} resolved (plus {len(unresolved)} unresolved -- "
                      "re-query still pending or the CI read failed), below the 20-observation bar (AC3) "
                      "-- no recommendation yet.")
    return lines


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

    print()
    for line in overlap_report_lines(overlap_rows):
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
