#!/usr/bin/env python3
"""Read the belt tick log — is the silence real, and what is it costing?

harmonic-forge#518 AC17c shipped the stub; harmonic-forge#519 is this: the
aggregation, drift detection, and the small number of alarms worth raising.

**Two commands, deliberately.** The default invocation reads only the local
JSONL and makes no API call whatsoever (AC1). `--audit` is a separate, explicit
invocation that fetches real comment bodies to replay would-match against
did-emit (AC5). They are split rather than flagged apart on one path because
AC1 and AC5 are in direct conflict — a replay needs bodies the log does not
store — and because AC16 requires that nothing on a schedule spends quota.
**`--audit` is never wired into a cron, a `/loop`, or a Monitor.**

    python3 tools/gh/belt_report.py --log ~/Harmonic_Projects/testplan/ticks.jsonl
    python3 tools/gh/belt_report.py --log <path> --audit --repo vitalharmony/hrse

WHAT THIS REPORT WILL NOT DO
------------------------------
It will not print zeros against an empty file and call that clean. An empty log
means the belt was never armed, which is the most severe state this tool can
observe, and it is reported as "no data" rather than as a healthy report with
no findings. That distinction is the whole reason the log exists: the
`LANE3_ACTIVE` marker was write-only and read as fine.

Nor does it claim to measure posted-to-action latency. It measures
**detection-to-action** and says so in its own output — see `latency_report`.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

#: `<repo>#<number>` — the documented element format (`REF_FORMAT` in
#: `belt_mechanics.py`). Validated rather than assumed: an entry that does not
#: match is reported as its own finding, never counted as zero.
_REF = re.compile(r"^(?P<repo>[A-Za-z0-9._-]+)#(?P<number>\d+)$")

#: Ticks a repo must be polled before "never produced an event" is worth
#: raising. **Unvalidated**: chosen because three is the smallest number that
#: is not obviously noise, not because any data supports it. The log had zero
#: records when this shipped. Labelled as a guess everywhere it appears.
DEFAULT_WINDOW = 3


def load(path: Path) -> tuple[list[dict], list[str]]:
    """`(records, malformed_lines)`. A line that will not parse is counted,
    not skipped silently — a log the reader cannot read is a finding."""
    if not path.exists():
        return [], []
    records, bad = [], []
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            bad.append(f"line {n}")
    return records, bad


def refs(rec: dict, key: str) -> list[dict]:
    """Entries under `key`, normalised. Tolerates the pre-#519 bare-string
    shape so an older log still reads, with `posted_at` absent rather than
    invented."""
    out = []
    for item in rec.get(key, []) or []:
        if isinstance(item, dict):
            out.append({"id": str(item.get("id", "")), "posted_at": item.get("posted_at")})
        else:
            out.append({"id": str(item), "posted_at": None})
    return out


def _parse_ts(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


class Findings:
    """AC6: every finding carries what to do about it, and a clean run prints
    two lines rather than nothing. A report that says nothing when nothing is
    wrong is indistinguishable from a report that did not run."""

    def __init__(self) -> None:
        self.items: list[tuple[str, str]] = []

    def add(self, what: str, do: str) -> None:
        self.items.append((what, do))

    def render(self) -> None:
        print("\nFINDINGS")
        if not self.items:
            print("  none — every repo polled produced at least one event, no")
            print("  malformed entries, no GraphQL from a scheduled path.")
            return
        for what, do in self.items:
            print(f"  ** {what}")
            print(f"     -> {do}")


def validate_refs(records: list[dict], f: Findings) -> dict[str, Counter]:
    """Per-repo counts, validating every ref against `REF_FORMAT`.

    An unparseable ref is a finding of its own. Dropping it would silently
    understate a repo's event count, which reads identically to a quiet repo —
    the failure mode this whole report exists to distinguish."""
    per_repo: dict[str, Counter] = {"matched": Counter(), "emitted": Counter(),
                                    "owed_found": Counter()}
    malformed: Counter = Counter()
    for rec in records:
        for key in per_repo:
            for entry in refs(rec, key):
                m = _REF.match(entry["id"])
                if m:
                    per_repo[key][m.group("repo")] += 1
                else:
                    malformed[f"{key}:{entry['id']!r}"] += 1
    if malformed:
        f.add(
            f"{sum(malformed.values())} log entr(ies) do not match the "
            f"documented ref format '<repo>#<number>': {list(malformed)[:5]}",
            "Fix the writer to log `f'{repo}#{number}'` (belt_mechanics.REF_FORMAT). "
            "Until then these are excluded from per-repo counts and those counts "
            "are understated.",
        )
    return per_repo


def latency_report(records: list[dict], f: Findings) -> None:
    """AC3 — detection-to-action per lane, and the oldest still outstanding.

    **This is detection-to-action, not posted-to-action**, and the output says
    so. The two differ by the polling cadence plus any interval the belt was
    not armed at all, and the second term is unbounded and invisible from
    inside the log: a belt that is down writes no record, so its own downtime
    cannot appear in its own telemetry. Where a marker's `posted_at` is
    recorded, the posted-to-detected gap IS derivable and is reported
    alongside — that is what F519 added the field for.
    """
    first_seen: dict[str, tuple[str, datetime, Optional[datetime]]] = {}
    acted: dict[str, datetime] = {}

    for rec in records:
        tick = _parse_ts(rec.get("ts"))
        if tick is None:
            continue
        lane = str(rec.get("lane", "?"))
        for entry in refs(rec, "matched"):
            rid = entry["id"]
            if rid and rid not in first_seen:
                first_seen[rid] = (lane, tick, _parse_ts(entry.get("posted_at")))
        for action in rec.get("actions_taken", []) or []:
            for rid in list(first_seen):
                if rid in str(action) and rid not in acted:
                    acted[rid] = tick

    print("\nDETECTION-TO-ACTION")
    print("  Measured from the first tick that matched a marker to the tick whose")
    print("  actions_taken references it. This is NOT posted-to-action: it cannot")
    print("  see time the belt was not running, because a belt that is down writes")
    print("  no record. Where posted_at is present, posted-to-detected is shown.")

    if not first_seen:
        print("  no markers matched in this log — nothing to measure.")
        return

    by_lane: dict[str, list[float]] = defaultdict(list)
    posted_gap: list[float] = []
    for rid, (lane, seen, posted) in first_seen.items():
        if rid in acted:
            by_lane[lane].append((acted[rid] - seen).total_seconds())
        if posted is not None:
            posted_gap.append((seen - posted).total_seconds())

    for lane in sorted(by_lane):
        deltas = by_lane[lane]
        print(f"  lane {lane}: n={len(deltas)}  "
              f"median={sorted(deltas)[len(deltas) // 2]:.0f}s  max={max(deltas):.0f}s")
    if posted_gap:
        print(f"  posted->detected (from posted_at, n={len(posted_gap)}): "
              f"median={sorted(posted_gap)[len(posted_gap) // 2]:.0f}s  "
              f"max={max(posted_gap):.0f}s")
    else:
        f.add(
            "no matched entry carries a posted_at, so only detection-to-action "
            "is derivable and belt downtime is invisible",
            "Have the belt call TickLog.record_match(ref, posted_at=<comment created_at>) "
            "rather than appending a bare ref.",
        )

    outstanding = {r: v for r, v in first_seen.items() if r not in acted}
    if outstanding:
        oldest = min(outstanding.items(), key=lambda kv: kv[1][1])
        f.add(
            f"{len(outstanding)} matched marker(s) never reached an action; "
            f"oldest is {oldest[0]} first seen {oldest[1][1].isoformat()} "
            f"by lane {oldest[1][0]}",
            f"Read {oldest[0]} and either act on it or record why it needed none. "
            "A marker matched and never acted on is the failure this telemetry exists to surface.",
        )


def report(records: list[dict], malformed_lines: list[str], last: int,
           window: int, path: Path) -> int:
    f = Findings()

    # The distinction that matters most, made first and made loudly.
    if not path.exists():
        print(f"NO DATA — no tick log exists at {path}.")
        print("The belt has never been armed with the F518 skill, or it is writing")
        print("elsewhere. This is not a clean report; it is the absence of one.")
        print("  -> Arm the belt (`/belt-and-suspenders`) and re-run after some ticks.")
        return 0
    if not records:
        print(f"NO DATA — the tick log at {path} exists but holds zero records.")
        print("A log file with no ticks means the belt is not running, not that it")
        print("is running quietly. Reporting zeros here would be the write-only")
        print("marker defect wearing a different hat.")
        print("  -> Check the belt is armed and that its log path matches this one.")
        return 0

    if malformed_lines:
        f.add(f"{len(malformed_lines)} unparseable log line(s): {malformed_lines[:5]}",
              "Inspect the writer; a log the reader cannot parse under-reports silently.")

    rest = sum(r.get("calls_rest", 0) for r in records)
    graphql = sum(r.get("calls_graphql", 0) for r in records)
    print(f"ticks: {len(records)}   "
          f"first {records[0].get('ts')}   last {records[-1].get('ts')}")
    print(f"  triggers: {dict(Counter(r.get('trigger', '?') for r in records))}")
    print(f"  lock:     {dict(Counter(r.get('lock', '?') for r in records))}")
    print(f"  calls:    rest={rest}  graphql={graphql}")

    # AC4 — name the tick, so the defect is findable rather than merely counted.
    offenders = [r.get("ts") for r in records if r.get("calls_graphql", 0)]
    if offenders:
        f.add(f"{graphql} GraphQL call(s) from a scheduled path, in tick(s) "
              f"{offenders[:5]}{' ...' if len(offenders) > 5 else ''}",
              "R-0019/AC16: scheduled paths use REST. Find the call in that tick's "
              "code path and convert it; the 5000/hr complexity quota is shared.")

    per_repo = validate_refs(records, f)

    polled: dict[tuple[str, str], int] = defaultdict(int)
    errored: dict[tuple[str, str], int] = defaultdict(int)
    for rec in records:
        for row in rec.get("repos_polled", []) or []:
            key = (row.get("account", "?"), row.get("repo", "?"))
            polled[key] += 1
            if not row.get("ok", True):
                errored[key] += 1

    if polled:
        print(f"\n  per repo (silence flagged after {window} poll(s) — "
              f"UNVALIDATED DEFAULT, not calibrated against real data):")
        for (account, repo), n in sorted(polled.items()):
            events = per_repo["emitted"].get(repo, 0) + per_repo["matched"].get(repo, 0)
            errs = errored.get((account, repo), 0)
            flag = f"   [{errs} failed call(s)]" if errs else ""
            print(f"    {account}/{repo:<24} polled={n:<5} events={events}{flag}")
            if events == 0 and n >= window:
                f.add(f"{account}/{repo} polled {n} time(s), produced zero events",
                      "Either the repo is genuinely quiet or the marker filter does not "
                      "match its posts. Post a marked comment there and confirm the next "
                      "tick sees it — silence is not evidence of quiet.")
            if errs:
                f.add(f"{account}/{repo} had {errs} failed poll(s)",
                      "Check `gh-as <account>` is initialised for that account; a 404 from "
                      "wrong credentials is indistinguishable from an empty result (R-0014/R-0015).")

    latency_report(records, f)

    owed = [e["id"] for rec in records for e in refs(rec, "owed_found")]
    if owed:
        print(f"\n  own-output predicate found {len(owed)} unanswered: "
              f"{sorted(set(owed))[:10]}")

    if last:
        print(f"\n  last {last} tick(s):")
        for rec in records[-last:]:
            print(f"    {rec.get('ts')}  {str(rec.get('trigger')):<8} "
                  f"matched={len(refs(rec, 'matched'))} "
                  f"emitted={len(refs(rec, 'emitted'))} "
                  f"lock={rec.get('lock')}")

    f.render()
    return 0


def audit(records: list[dict], repo: str, window: int) -> int:
    """AC5 — replay would-match against did-emit over a real window.

    **This makes API calls.** It is a separate command for that reason (AC1
    forbids them on the default path, AC16 forbids them on any scheduled one),
    and it must never be attached to a cron, a `/loop`, or a Monitor. REST
    only, per R-0019.
    """
    print(f"AUDIT — fetching the last {window} comment(s) on {repo}.")
    print("This command makes API calls. It is never run on a schedule.\n")

    emitted = {e["id"] for rec in records for e in refs(rec, "emitted")}
    matched = {e["id"] for rec in records for e in refs(rec, "matched")}

    try:
        # `sort=created&direction=desc` is load-bearing, not tidiness. The
        # endpoint's default is ascending, so a bare `per_page=N` returns the
        # repo's OLDEST N comments — which on any long-lived repo predate the
        # marker convention entirely and report "markers in window: 0". That
        # is the exact zeros-as-confidence failure this whole issue exists to
        # catch, and this audit shipped it once before being run live.
        out = subprocess.run(
            ["gh", "api",
             f"repos/{repo}/issues/comments?per_page={window}"
             f"&sort=created&direction=desc",
             "--jq", '.[] | {id: .id, url: .html_url, body: .body}'],
            capture_output=True, text=True, check=True, timeout=120,
        ).stdout
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        print(f"audit could not fetch comments: {exc}", file=sys.stderr)
        return 1

    would_match, missed = 0, []
    for line in out.splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        body = row.get("body") or ""
        if "<!-- l1-post" not in body:
            continue
        would_match += 1
        ref = _ref_from_url(row.get("url", ""))
        if ref and ref not in emitted and ref not in matched:
            missed.append((ref, row.get("url")))

    print(f"  markers in window: {would_match}")
    print(f"  present in the log: {would_match - len(missed)}")
    if missed:
        print(f"\n  ** {len(missed)} marker(s) the belt would match but never logged:")
        for ref, url in missed[:20]:
            print(f"     {ref}  {url}")
        print("     -> The belt was down, or its filter does not match these. "
              "Re-check the filter against one of these bodies verbatim.")
    else:
        print("\n  none missed in this window.")
        print("  Note: a clean window is not proof the belt never missed anything —")
        print("  it is proof it missed nothing in these comments.")
    return 0


def _ref_from_url(url: str) -> str:
    m = re.search(r"github\.com/[^/]+/([^/]+)/issues/(\d+)", url or "")
    return f"{m.group(1)}#{m.group(2)}" if m else ""


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--log", required=True, type=Path,
                        help="path to the JSONL tick log")
    parser.add_argument("--last", type=int, default=10, help="show the last N ticks")
    parser.add_argument("--window", type=int, default=DEFAULT_WINDOW,
                        help=f"polls before a zero-event repo is flagged "
                             f"(default {DEFAULT_WINDOW}, an UNVALIDATED guess); "
                             f"with --audit, comments to fetch")
    parser.add_argument("--audit", action="store_true",
                        help="AC5 replay. MAKES API CALLS. Never run on a schedule.")
    parser.add_argument("--repo", help="OWNER/REPO, required with --audit")
    args = parser.parse_args()

    records, malformed = load(args.log)
    if args.audit:
        if not args.repo:
            parser.error("--audit requires --repo OWNER/REPO")
        return audit(records, args.repo, args.window if args.window > 10 else 100)
    return report(records, malformed, args.last, args.window, args.log)


if __name__ == "__main__":
    raise SystemExit(main())
