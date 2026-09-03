#!/usr/bin/env python3
"""Poll GitHub issue comments for a lane-post signal, one event per line
(harmonic-forge#442).

**Only Lane 1's posting tool (`l1_post.py` / `mise run l1-post`) stamps a
machine-readable marker** -- confirmed live against hrse#1530's real
comment history, 2026-09-03:

    <!-- l1-post v1; kind=handoff; posted-by=LANE-unset -->
    <!-- l1-post v1; kind=discussion; posted-by=LANE1 -->
    <!-- l1-post v1; kind=ready-for-l3; sha=...; body-sha256=...; checks=... -->

`l2_post.py`'s receipt-backed status comments and Lane 3's spec/gate
comments carry **no such marker** -- verified by grepping full comment
bodies, not assumed. They are plain markdown, distinguishable only by
their first heading line, which is NOT a fixed short code either (Lane 3's
real heading was `## Lane 3 Test Spec`, not `## L3S`). So detection here
is two different mechanisms depending on which lane posted:

- Lane 1 -> the `<!-- l1-post v1; kind=X -->` marker (reliable, exact).
- Lane 2 -> first line matches `^## L2[A-Z]` (`L2P`/`L2D`/`L2B` observed).
- Lane 3 -> first line matches `^## L3\\b` or `^## Lane 3\\b` (heuristic --
  no fixed vocabulary confirmed; widen this pattern if a real Lane 3
  heading is seen that doesn't match).

This is the correct signal for "did another lane just do something I need
to react to" regardless of mechanism -- not local git state. A lane
frequently works in a disposable per-issue worktree (`/tmp/hrse2-<N>-impl`)
and never pushes until its counterpart reviews, so watching a shared
worktree's `git log` can sit silent through a real completion (this
happened live on hrse#1530 -- the trigger for this script).

Designed to be pasted as a `Monitor` tool `command` verbatim, or run
standalone from a terminal. Prints ONE line per new comment whose detected
`lane` (`l1`/`l2`/`l3`) is in `--watch`; every other comment (plain chat,
a lane not being watched) is silent.

Usage
-----
    python3 watch_lane_posts.py --repo vitalharmony/hrse --issues 1530 \\
        --watch l2 --watch l3 --interval 30

Exits only on error or Ctrl-C; runs until stopped otherwise.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import subprocess
import sys
import time

_L1_MARKER_RE = re.compile(r"<!--\s*l1-post\s+v\d+;\s*kind=([\w-]+)")
_L2_HEADING_RE = re.compile(r"^##\s+L2[A-Z]\b")
_L3_HEADING_RE = re.compile(r"^##\s+(L3\b|Lane 3\b)")


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _fetch_comments(repo: str, issue: int, since: str) -> list[dict]:
    result = subprocess.run(
        ["gh", "api", f"repos/{repo}/issues/{issue}/comments?since={since}"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"[watch_lane_posts] gh api failed for #{issue}: "
              f"{result.stderr.strip()}", file=sys.stderr)
        return []
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return []


def _classify(body: str) -> tuple[str, str] | None:
    """Returns `(lane, detail)` -- `detail` is the `kind=` value for l1-post,
    or the matched heading text for l2/l3 -- or `None` if unclassifiable."""
    marker = _L1_MARKER_RE.search(body)
    if marker:
        return "l1", marker.group(1)
    headline = body.strip().split("\n", 1)[0]
    if _L2_HEADING_RE.match(headline):
        return "l2", headline
    if _L3_HEADING_RE.match(headline):
        return "l3", headline
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", required=True, help="owner/repo, e.g. vitalharmony/hrse")
    parser.add_argument("--issues", required=True, type=int, nargs="+",
                        help="issue numbers to poll")
    parser.add_argument("--watch", required=True, action="append",
                        choices=["l1", "l2", "l3"],
                        help="lane whose posts to surface -- l1, l2, and/or l3 (repeatable)")
    parser.add_argument("--interval", type=int, default=30, help="poll interval, seconds")
    args = parser.parse_args()

    watch = set(args.watch)
    since = _now()
    print(f"[watch_lane_posts] watching {args.repo} issues={args.issues} "
          f"for lanes={sorted(watch)} every {args.interval}s", file=sys.stderr)

    while True:
        time.sleep(args.interval)
        now = _now()
        for issue in args.issues:
            for comment in _fetch_comments(args.repo, issue, since):
                classified = _classify(comment.get("body", ""))
                if classified is None:
                    continue
                lane, detail = classified
                if lane not in watch:
                    continue
                print(f"#{issue} {lane} — {detail}")
                sys.stdout.flush()
        since = now


if __name__ == "__main__":
    raise SystemExit(main())
