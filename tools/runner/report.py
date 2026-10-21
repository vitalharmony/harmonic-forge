#!/usr/bin/env python3
"""The te-runner's report: one card per green PR (harmonic-forge#1056, G9, ruling 7).

    report.py render --queue FILE --out DIR   # writes DIR/<run-id>.md, prints its path
        [--invocation-id ID --exit-status N]  # the unit's: a run that never started
                                              # gets a one-line report of its own
    report.py notify --report FILE            # one ntfy line: counts and the report path

`render` reads the queue file and nothing else: each item's harness-written
keys (harmonic-forge#1055) and the reducer's `verdict` (harmonic-forge#1051).
The only free text it carries is the reducer's own `detail`, already capped at
200 characters. It reads no model output, pastes no gate, check or refuter
output, and names no lane status token.

The layout, in order:
1. a header with counts;
2. one line per item;
3. a review card per green PR;
4. a numbered set-aside list, each entry naming the decision the operator owes
   and a recommendation;
5. a fenced block holding the BATCH line and one close line per green PR.

That block is inert data: a BATCH line in a file mints no grant (R-0117), and
BATCH grants no close (harmonic-forge#612/#911). A card's CI state is what the
harness read at run time, labelled as not authoritative: CI is re-read live
when BATCH is typed.

`notify` sends one line with `curl` to `https://ntfy.sh/<topic>`. The topic is
read from `NTFY_TOPIC_FILE` or `~/.local/share/te-runner/ntfy-topic` and is
never printed or logged. It posts nothing to GitHub.
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "lane"))
import queue_state  # noqa: E402

DEFAULT_TOPIC_FILE = Path.home() / ".local" / "share" / "te-runner" / "ntfy-topic"
#: Lane status tokens never appear in a report (AC4); a detail carrying one is masked.
STATUS_TOKEN = re.compile(r"\bL[1-3][BDSPF]\b")
MAX_ITEM_LINES = 12
DETAIL_LIMIT = 200
CI_LABEL = {"green": "green", "pending": "pending — approve workflows", "red": "red"}
#: What the operator decides for each set-aside reason, and Lane 1's recommendation.
DECISIONS = {
    "ineligible": ("whether to fix the eligibility gap or drop the issue from the pilot",
                   "drop it from the queue until Lane 1 fixes the gap named above"),
    "deny-path": ("whether the change may touch a denied path at all",
                  "route it through Lane 1 interactively, never the runner"),
    "tool-refusal": ("whether the refused tool calls hide missing work",
                     "Lane 1 reads the run log before any re-queue"),
    "budget": ("whether to raise the per-item budget",
               "keep the budget and re-scope the issue smaller"),
    "self-flag": ("what the agent said it could not do honestly",
                  "route it to Lane 1; do not re-queue unchanged"),
    "check-fail": ("why `mise run check` or CI failed",
                   "route it to Lane 1; do not re-queue unchanged"),
    "kill-check-fail": ("whether Lane 1's oracle or the agent's change is wrong",
                        "Lane 1 reviews the kill-check results before any re-run"),
    "preclose-survivor": ("what the preclose panel found",
                          "read the preclose summary; Lane 1 fixes or reforges"),
    "unstamped": ("which run produced this item (it carries no run id)",
                  "read it in the queue file before acting; it is never proposed for merge"),
    "blocked": ("why the run could not proceed (the queue file's note)",
                "fix the named cause before re-queuing; never retried automatically"),
}


def issue_key(item: dict) -> str:
    return str(item.get("id", "?"))


def clean(text: object, limit: int = DETAIL_LIMIT) -> str:
    """One line, capped, with any lane status token masked."""
    line = " ".join(str(text or "").split())[:limit]
    return STATUS_TOKEN.sub("[status]", line)


def classify(item: dict) -> str:
    if item.get("status") == "blocked":
        return "blocked"
    if item.get("status") != "done":
        return "not run"
    if not item.get("run_id"):
        return "unstamped"  # never green, never in the BATCH block
    state = (item.get("verdict") or {}).get("state")
    return "green" if state == "green" else "set aside"


def reason_code(item: dict, kind: str) -> str:
    if kind in ("blocked", "unstamped"):
        return kind
    if kind == "not run":
        return "—"
    return (item.get("verdict") or {}).get("reason") or ("green" if kind == "green" else "?")


def item_line(item: dict) -> str:
    kind = classify(item)
    where = item.get("pr") or item.get("branch") or "—"
    return f"- {issue_key(item)} · {kind} · {reason_code(item, kind)} · {where}"


def card(item: dict) -> list[str]:
    facts = item.get("facts") or {}
    kills = "; ".join(f"{clean(k.get('ac'), 40)} base {k.get('base_rc')} / head {k.get('head_rc')}"
                      for k in facts.get("kill") or []) or "none recorded"
    preclose = facts.get("preclose") or {}
    survivors = preclose.get("survivors")
    live = facts.get("live") or {}
    ci = CI_LABEL.get(live.get("ci"), "unknown")
    titles = "; ".join(clean(t, 80) for t in item.get("ac_titles") or []) or "none recorded"
    return [
        f"### {issue_key(item)} — {item.get('pr') or 'no PR recorded'}",
        f"- Acceptance criteria: {clean(titles, 400)}",
        f"- Diff: {clean(item.get('diff_stat'), 120) or 'none recorded'}",
        f"- Kill checks: {clean(kills, 400)}. {check_line(facts)}",
        f"- Preclose survivors: {'none' if survivors == 0 else clean(survivors, 20)}",
        f"- Provenance: {clean(preclose.get('provenance'), 160) or 'none recorded'}",
        f"- CI: {ci}, as of {clean(item.get('live_read_at'), 30) or 'unknown'}, "
        "not authoritative: re-read live at BATCH time",
        f"- Preclose summary: {item.get('preclose_summary') or 'not recorded'}",
        "- [ ] scope",
        "- [ ] containment",
        "- [ ] honesty",
    ]


def check_line(facts: dict) -> str:
    """The base-relative check fact (harmonic-forge#1112 AC4): what failed on
    the head only, and what already failed on the base."""
    def names(key: str) -> str:
        found = facts.get(key)
        return clean("; ".join(map(str, found)), 400) if found else "none"
    return (f"Check: new failures {names('check_new_failures')}; "
            f"base failures {names('check_base_failures')}")


def all_reasons(item: dict) -> str:
    """Every clause that failed, in precedence order (harmonic-forge#1112 AC8)."""
    found = (item.get("verdict") or {}).get("reasons") or []
    return "; ".join(f"{clean(r.get('reason'), 40)}: {clean(r.get('detail'))}"
                     for r in found if isinstance(r, dict)) or "none recorded"


def set_aside_entry(number: int, item: dict) -> str:
    kind = classify(item)
    reason = reason_code(item, kind)
    decision, recommendation = DECISIONS.get(reason, ("what went wrong", "route it to Lane 1"))
    detail = clean((item.get("verdict") or {}).get("detail")) if kind not in ("blocked", "unstamped") else \
        "see the item's note in the queue file"
    extra = ""
    if kind not in ("blocked", "unstamped"):
        extra = (f" All reasons: {all_reasons(item)}. "
                 f"{check_line(item.get('facts') or {})}.")
    return (f"{number}. {issue_key(item)} ({reason}): {detail}.{extra} Decision: {decision}. "
            f"Recommendation: {recommendation}.")


def run_items(state: dict) -> list[dict]:
    """This run's items only: those `te_run` stamped with the queue's current
    `run_id`, plus any still pending. An earlier run's green item is never
    proposed again (preclose pass 1)."""
    run_id = state.get("run_id")
    # An item with no run id at all is shown too (fail closed: unknown
    # provenance is surfaced, never dropped), but only as set aside.
    return [item for item in state["items"]
            if item.get("status") == "pending" or not item.get("run_id")
            or (run_id and item.get("run_id") == run_id)]


def render_text(state: dict, run_id: str) -> str:
    items = run_items(state)
    kinds = [classify(item) for item in items]
    green = [item for item, kind in zip(items, kinds) if kind == "green"]
    aside = [item for item, kind in zip(items, kinds)
             if kind in ("set aside", "blocked", "unstamped")]
    counts = {k: kinds.count(k) for k in ("green", "set aside", "blocked", "not run")}
    counts["set aside"] += kinds.count("unstamped")
    lines = [f"# te-runner report {run_id}", "",
             f"{len(items)} items: {counts['green']} green, {counts['set aside']} set aside, "
             f"{counts['blocked']} blocked, {counts['not run']} not run.", "", "## Items", ""]
    lines += [item_line(item) for item in items] or ["No items in the queue."]
    lines += ["", "## Green", ""]
    if green:
        for item in green:
            lines += card(item) + [""]
    else:
        lines += ["No green PRs this run.", ""]
    lines += ["## Set aside", ""]
    lines += [set_aside_entry(n, item) for n, item in enumerate(aside, 1)] or \
        ["Nothing set aside this run."]
    lines += ["", "## Merge", "",
              "Inert data. Type BATCH only after every card above is checked.", "", "```"]
    if green:
        lines.append("BATCH " + ",".join(issue_key(item) for item in green))
        lines += [f"close {issue_key(item)}" for item in green]
    else:
        lines.append("(no green PRs: nothing to merge)")
    lines += ["```", ""]
    return "\n".join(lines)


def render(queue: Path, out: Path, now: dt.datetime | None = None,
           invocation: str | None = None, exit_status: str | None = None) -> Path:
    state = queue_state.load(queue)
    # Under systemd the unit passes its $INVOCATION_ID: the report belongs to
    # the invocation that just ended. A run that exited before `_run_queue`
    # saved its own invocation id (an empty budget, a held queue lock) never
    # started: its report says so in one line and is never the last run's
    # report (harmonic-forge#1112 sticky-wicket patch 2).
    if invocation and state.get("invocation_id") != invocation:
        run_id = "unstarted-" + (now or dt.datetime.now(dt.timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
        out.mkdir(parents=True, exist_ok=True)
        path = out / f"{run_id}.md"
        path.write_text(f"# te-runner report {run_id}\n\nrun ended before it started: exit "
                        f"{exit_status or 'unknown'}\n", encoding="utf-8")
        return path
    # The queue's own run id names the report; a queue no run has worked yet
    # gets a render-time id and lists only pending items.
    run_id = state.get("run_id") or \
        "unrun-" + (now or dt.datetime.now(dt.timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{run_id}.md"
    path.write_text(render_text(state, run_id), encoding="utf-8")
    return path


def notify(report: Path, curl: str = "curl") -> int:
    topic_file = Path(os.environ.get("NTFY_TOPIC_FILE") or DEFAULT_TOPIC_FILE)
    try:
        topic = topic_file.read_text(encoding="utf-8").strip()
    except OSError:
        print(f"report: no ntfy topic file at {topic_file}", file=sys.stderr)
        return 2
    if not topic:
        print(f"report: the ntfy topic file {topic_file} is empty", file=sys.stderr)
        return 2
    lines = report.read_text(encoding="utf-8").splitlines()
    counts = lines[2] if len(lines) > 2 else "report written"
    message = f"te-runner: {counts} {report}"
    # The topic is in the URL only, never echoed: curl's own stderr is not
    # relayed, since an error line could carry the URL.
    result = subprocess.run([curl, "-fsS", "-m", "20", "-H", "Title: te-runner report",
                             "-d", message, f"https://ntfy.sh/{topic}"],
                            capture_output=True, text=True)
    if result.returncode:
        print(f"report: ntfy notify failed (curl exit {result.returncode})", file=sys.stderr)
        return 1
    print("report: ntfy notified")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    rendered = sub.add_parser("render")
    rendered.add_argument("--queue", required=True)
    rendered.add_argument("--out", required=True)
    rendered.add_argument("--invocation-id", default="",
                          help="the unit's $INVOCATION_ID (patch 2); empty skips the check")
    rendered.add_argument("--exit-status", default="", help="the unit's $EXIT_STATUS")
    notified = sub.add_parser("notify")
    notified.add_argument("--report", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "render":
            print(render(Path(args.queue), Path(args.out), invocation=args.invocation_id,
                         exit_status=args.exit_status))
            return 0
        return notify(Path(args.report))
    except (ValueError, OSError) as exc:
        print(f"report: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
