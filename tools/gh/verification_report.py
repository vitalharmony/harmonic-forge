#!/usr/bin/env python3
"""Lane 3 verification report: what each gate ran and what caught defects
(harmonic-forge#893).

Read-only, over the telemetry store. It answers two before/after questions for
the planned lean-verification change:
- Does Lane 3 mostly re-run Lane 2's suite (`existing` cases) rather than test
  the acceptance criteria (`ac`) or the live system (`live`)?
- Which class of case catches defects, and what does Lane 1's own re-run at
  `ready-for-l3` add (harmonic-forge#892's `ready-for-l3.attempt` events)?

Inputs are scalar event attrs only: a spec event's `tc_ac/tc_existing/tc_live`,
a gate event's `tc_count/fail_count/blocked_count/gate_ms` and, after pairing in
`extract_threads.pair_gates_with_specs`, its `paired` flag and per-class
`case_*/fail_*/blocked_*` counts.

Every gate is in exactly one bucket, and absence is never read as zero. The
bucket is the `measurement` the extractor stamped (harmonic-forge#893 reforge,
R4), never inferred here from which attrs are present:
- measured: per-case results, paired with its classified spec;
- unpaired: per-case results with no matching classified spec;
- no-map: posted through post_lane_discussion.py without a usable map;
- no-cases: posted with an explicitly empty map (a gate that ran no case);
- pre-893: posted by the tool before #893 recorded case maps;
- bypass-route: posted without the tool's footer at all;
- unstamped: a gate reading the current extractor took but did not stamp,
  which is a defect to look at, never history.
Plus missing input: an issue with no ready-for-l3 attempt events (#892).
Aggregates cover `measured` only. A measured gate whose map disagrees with its
own verdict is counted as low-confidence, never refused or dropped.

    verification_report.py [--since YYYY-MM-DD] [--until YYYY-MM-DD] [--store PATH]
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import statistics
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "telemetry"))
import emit  # noqa: E402

#: Every event type a gate comment extracts as, BLOCKED included: a BLOCKED
#: gate that vanished from the report would read as one fewer gate. "unknown"
#: is load-bearing: it is how a gate whose heading and lead verdict disagree
#: (CONFLICT) extracts, and removing it drops every such gate (#893 e1 #1).
GATE_TYPES = {"gate.pass", "gate.fail", "blocked.lane", "unknown"}
ATTEMPT_TYPE = "ready-for-l3.attempt"
CLASSES = ("ac", "existing", "live")
TOKEN_NOTE = ("Lane token cost is not measured: a lane session cannot read its own token "
              "usage (only subagent completion notices carry usage), so time is wall-clock.")


BUCKETS = ("measured", "unpaired", "no-map", "no-cases", "pre-893", "bypass-route", "unstamped")
#: The first extractor that stamps a measurement on every gate it recognises.
STAMPING_EXTRACTOR = 4


class StoreUnreadable(Exception):
    """The store's event directory is missing or cannot be listed."""


def load_events(store: Path, since: Optional[str], until: Optional[str],
                skipped: Optional[list[str]] = None) -> list[dict[str, Any]]:
    """Every event under `store/events`, inside [since, until] by date. A file
    or line that cannot be read is named in `skipped`, never silently dropped:
    a damaged store must not read as fewer gates. A missing or unlistable
    `events/` raises StoreUnreadable: it is not an empty window."""
    events_dir = store / "events"
    try:
        next(iter(events_dir.iterdir()), None)
    except OSError as exc:
        raise StoreUnreadable(f"no readable telemetry store at {events_dir} ({exc})") from exc
    out = []
    parts = []
    # os.walk names every directory it cannot list; Path.glob skips it
    # silently, which read an unreadable partition as an empty window.
    for here, _, files in os.walk(events_dir, onerror=lambda exc: skipped is not None
                                  and skipped.append(f"{exc.filename} (unreadable directory)")):
        parts += [Path(here) / name for name in files if name.endswith(".jsonl")]
    for part in sorted(parts):
        try:
            lines = part.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            if skipped is not None:
                skipped.append(str(part))
            continue
        for number, line in enumerate(lines, 1):
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except ValueError:
                if skipped is not None:
                    skipped.append(f"{part}:{number}")
                continue
            day = str(event.get("ts", ""))[:10]
            if (since and day < since) or (until and day > until):
                continue
            out.append(event)
    return out


#: The markers of an older (threads-3 and before) reading that IS a gate:
#: footer- or heading-era gate results and the token-era L3P/L3F verdicts. An
#: L2B or L3B is a lane reporting it could not run, never a gate.
PRE_893_GATE_MARKERS = {"gate-result", "L3P", "L3F"}


def is_gate_reading(event: dict[str, Any]) -> bool:
    """A gate the report counts: one the extractor stamped, or an unstamped
    reading whose marker says it is a gate. Event type alone never decides:
    L2B/L3B extract as blocked.lane too."""
    attrs = event.get("attrs") or {}
    if attrs.get("measurement") in BUCKETS:
        return True
    return "measurement" not in attrs and attrs.get("marker") in PRE_893_GATE_MARKERS


def bucket(gate: dict[str, Any]) -> str:
    """The stamped measurement. An unstamped gate reading is pre-893 only when
    an older extractor took it: from a stamping extractor it is history the
    stamping path missed, so it is `unstamped`, loudly, never pre-893 (#893
    e1 sticky-wicket #9)."""
    value = (gate.get("attrs") or {}).get("measurement")
    if value in BUCKETS:
        return value
    return "unstamped" if _version(gate.get("extractor_version"))[0] >= STAMPING_EXTRACTOR else "pre-893"


def _median(values: Iterable[float]) -> Optional[float]:
    values = list(values)
    return statistics.median(values) if values else None


def _fmt(value: Optional[float]) -> str:
    return "n/a" if value is None else f"{value:,.0f}"


def latest_readings(gates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One reading per gate comment: re-extraction after an EXTRACTOR_VERSION
    bump (or an edit) emits the same comment again, and counting both would
    double every gate. The highest extractor version wins, then the latest edit."""
    best: dict[tuple, dict[str, Any]] = {}
    for gate in gates:
        attrs = gate.get("attrs") or {}
        cid = attrs.get("comment_id")
        key = (str(gate.get("repo", "")).lower(), gate.get("issue"), cid) if cid else (id(gate),)
        rank = (_version(gate.get("extractor_version")), str(attrs.get("edited_at") or ""),
                str(attrs.get("extracted_at") or ""))
        held = best.get(key)
        if held is None or rank > held[0]:
            best[key] = (rank, gate)
    return [gate for _, gate in best.values()]


def _version(value: object) -> tuple[int, str]:
    """`threads-10` above `threads-9`: the numeric suffix, compared as a number
    (#893 preclose pass 2). A suffix that is not a number sorts below any that
    is, so it can never displace a real reading."""
    text = str(value or "")
    _, _, suffix = text.rpartition("-")
    return (int(suffix), text) if suffix.isdigit() else (-1, text)


def build(events: list[dict[str, Any]]) -> dict[str, Any]:
    # Every gate-typed reading, verdict attr or not (#893 preclose pass 2: the
    # `"verdict" in attrs` filter is what made a bypassing gate vanish).
    gates = latest_readings([e for e in events if e.get("source") == "gh-thread"
                             and e.get("event_type") in GATE_TYPES
                             and (e.get("attrs") or {}).get("comment_id") and is_gate_reading(e)])
    attempts = defaultdict(list)
    for e in events:
        if e.get("event_type") == ATTEMPT_TYPE:
            attempts[(str(e.get("repo", "")).lower(), e.get("issue"))].append(e.get("attrs") or {})
    issues: dict[tuple[str, Any], dict[str, Any]] = {}
    buckets = {**{name: 0 for name in BUCKETS}, "missing input": 0}
    for gate in gates:
        key = (str(gate.get("repo", "")).lower(), gate.get("issue"))
        row = issues.setdefault(key, {"gates": [], "attempts": attempts.get(key, [])})
        row["gates"].append(gate)
        buckets[bucket(gate)] += 1
    buckets["missing input"] = sum(1 for row in issues.values() if not row["attempts"])
    measured = [g["attrs"] for g in gates if bucket(g) == "measured"]
    aggregate = {
        "gates": len(measured),
        "median_gate_ms": _median(a["gate_ms"] for a in measured if a.get("gate_ms_known")),
        **{f"cases_{c}": sum(int(a.get(f"case_{c}") or 0) for a in measured) for c in CLASSES},
        **{f"fails_{c}": sum(int(a.get(f"fail_{c}") or 0) for a in measured) for c in CLASSES},
        "existing_dominant": sum(1 for a in measured if int(a.get("case_existing") or 0)
                                 > int(a.get("case_ac") or 0) + int(a.get("case_live") or 0)),
        "low_confidence": sum(1 for a in measured if a.get("map_agrees") is False),
    }
    return {"issues": issues, "buckets": buckets, "aggregate": aggregate}


def render(report: dict[str, Any], skipped: Optional[list[str]] = None) -> str:
    lines = ["| Issue | Gates | Measured | Cases ac/existing/live | Fails ac/existing/live | "
             "Median gate ms | ready-for-l3 attempts | Median local check ms | CI green, local red |",
             "|---|---|---|---|---|---|---|---|---|"]
    for (repo, issue), row in sorted(report["issues"].items(), key=lambda kv: (kv[0][0], kv[0][1] or 0)):
        measured = [g["attrs"] for g in row["gates"] if bucket(g) == "measured"]
        cases = "/".join(str(sum(int(a.get(f"case_{c}") or 0) for a in measured)) for c in CLASSES)
        fails = "/".join(str(sum(int(a.get(f"fail_{c}") or 0) for a in measured)) for c in CLASSES)
        gate_ms = _median(a["gate_ms"] for a in measured if a.get("gate_ms_known"))
        att = row["attempts"]
        if att:
            attempts = str(len(att))
            local = _fmt(_median(a["local_check_ms"] for a in att if isinstance(a.get("local_check_ms"), int)))
            red = str(sum(1 for a in att if a.get("ci_green_local_red")))
        else:
            attempts = local = red = "missing input"
        lines.append(f"| {repo}#{issue} | {len(row['gates'])} | {len(measured)} | "
                     f"{cases if measured else 'n/a'} | {fails if measured else 'n/a'} | "
                     f"{_fmt(gate_ms)} | {attempts} | {local} | {red} |")
    b, agg = report["buckets"], report["aggregate"]
    lines += ["", "Buckets (gates, except missing input, which counts issues):",
              *(f"- {name}: {b[name]}" for name in BUCKETS),
              f"- missing input: {b['missing input']}", ""]
    if agg["gates"]:
        share = agg["existing_dominant"] / agg["gates"]
        lines += [f"Aggregate over {agg['gates']} measured gate(s): median gate {_fmt(agg['median_gate_ms'])} ms; "
                  f"cases ac/existing/live {agg['cases_ac']}/{agg['cases_existing']}/{agg['cases_live']}; "
                  f"fails ac/existing/live {agg['fails_ac']}/{agg['fails_existing']}/{agg['fails_live']}; "
                  f"{agg['existing_dominant']} of {agg['gates']} ({share:.0%}) gates have more existing "
                  "cases than ac and live combined; "
                  f"{agg['low_confidence']} low-confidence (the map disagrees with the report's own "
                  "verdict)."]
    else:
        lines.append("Aggregate: no measured gates in this window; nothing is reported as zero.")
    lines.append(TOKEN_NOTE)
    if skipped:
        lines.insert(0, f"WARNING: {len(skipped)} store file(s) or line(s) could not be read; "
                        "every count below may be low.\n")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--since", type=_day, help="Only events on or after this date (YYYY-MM-DD).")
    parser.add_argument("--until", type=_day, help="Only events on or before this date (YYYY-MM-DD).")
    parser.add_argument("--store", type=Path, default=None, help="Telemetry store root.")
    args = parser.parse_args()
    store = args.store or emit.store_root()
    skipped: list[str] = []
    try:
        events = load_events(store, args.since, args.until, skipped)
    except StoreUnreadable as exc:
        # An unread store is not a window with no gates (#893 preclose pass 2).
        print(f"verification-report: {exc}; nothing was read, so nothing is reported.",
              file=sys.stderr)
        sys.exit(2)
    print(render(build(events), skipped))


def _day(value: str) -> str:
    """A real YYYY-MM-DD date, refused otherwise: an unpadded date compares as
    a string and would silently empty the window."""
    try:
        return datetime.date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not a YYYY-MM-DD date: {value!r}") from exc


if __name__ == "__main__":
    main()
