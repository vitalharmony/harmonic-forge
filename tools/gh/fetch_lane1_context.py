#!/usr/bin/env python3
"""Fetch only the issue body plus Lane-1-authored comments — never the full
comment thread — for Lane 3 spec derivation (harmonic-forge#253).

`3-lane-protocol.md`'s spec-derivation rule says Lane 3 derives its test
spec from the issue body, Lane 1's handoff, and Lane 1's Lane-3-addressed
comment — never Lane 2's completion comment. That rule held as prose and
still got violated twice on the same issue (vitalharmony/hrse#793): a
fresh Lane 3 session's own "read the full thread" starting step
necessarily exposes Lane 2's comment along with everything else, and by
the time a session notices, it has already read it. This script makes the
violation structurally unavailable instead of relying on the reading
session to self-censor what it just saw: it never returns Lane 2/Lane 3's
own direct posts (posted straight via `gh`, no `l1-post` marker) or a
Lane-2/3-authored discussion comment in the first place.

Qualifying comments are recognized by the `l1-post`/`lane-comment`
attestation footer every Lane 1 post already carries -- each consuming
project's own `scripts/l1_post.py` / `scripts/post_lane_discussion.py`
(e.g. HRSE2's, not a file in this directory) is what appends it:
  - `kind=handoff` / `kind=ready-for-l3` / `kind=sweep` -- inherently
    Lane-1-only kinds, no further check needed.
  - `kind=discussion` -- only when `posted-by=LANE1` or
    `posted-by=LANE-unset` (a session with no LANE set, e.g. a plain
    Claude Code session acting as Lane 1); a Lane 2/3 discussion comment
    carries `posted-by=LANE2`/`LANE3` and is excluded.
Anything without the footer marker at all -- including every Lane 2/Lane 3
completion or gate report, which the protocol has them post directly via
`gh`, never through `l1-post` -- is excluded.

Known limitation (harmonic-forge#269, partially closed): field extraction
(`kind=`, `posted-by=`) is scoped to the first matched `<!-- l1-post v1;
... -->` marker's own text, not the whole body, so prose elsewhere that
merely mentions another lane's footer fields (e.g. quoting `posted-by=LANE3`
by name while describing what a comment is -- ordinary Lane 1 writing, not
an adversarial act, and the incident that motivated this fix on hrse#848)
no longer misclassifies. What remains open: the *marker* match itself is
still the first occurrence in the body, so a comment that block-quotes
another comment's genuine, full `<!-- l1-post v1; ... -->` footer verbatim
before its own could still misclassify. Not observed in practice, and
`l1_post.py`'s own `reject_reserved_marker` already refuses to let a caller
forge the footer text into a body posted through the shared tooling -- so a
forged marker would have to be typed by hand outside that path.

Usage: python3 fetch_lane1_context.py --repo OWNER/REPO --issue N
"""

import argparse
import json
import re
import subprocess
import sys

_MARKER_RE = re.compile(r"<!--\s*l1-post\s+v1;.*?-->", re.DOTALL)
_KIND_RE = re.compile(r"kind=([\w-]+)")
_POSTED_BY_RE = re.compile(r"posted-by=([\w-]+)")
_LANE1_KINDS = {"handoff", "ready-for-l3", "sweep"}
_LANE1_POSTED_BY = {"LANE1", "LANE-unset"}


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True)


def is_lane1_comment(body: str) -> bool:
    # harmonic-forge#269: both field regexes must be scoped to the matched
    # marker's own text, not the whole body -- otherwise prose elsewhere in
    # a Lane 1 comment that quotes another lane's footer tag by name (e.g.
    # "the spec posted above (`posted-by=LANE3`, ...)") gets matched by
    # `.search()` instead of the genuine closing footer, misclassifying a
    # real Lane 1 comment as non-Lane-1. Live incident, hrse#848.
    marker_match = _MARKER_RE.search(body)
    if marker_match is None:
        return False
    marker = marker_match.group(0)
    kind_match = _KIND_RE.search(marker)
    if kind_match is None:
        return False
    kind = kind_match.group(1)
    if kind in _LANE1_KINDS:
        return True
    if kind == "discussion":
        posted_by_match = _POSTED_BY_RE.search(marker)
        return posted_by_match is not None and posted_by_match.group(1) in _LANE1_POSTED_BY
    return False


def fetch_issue_body(repo: str, issue: int) -> str:
    result = _run(["gh", "api", f"repos/{repo}/issues/{issue}", "--jq", ".body"])
    if result.returncode != 0:
        print(f"[FETCH-L1-CONTEXT] failed to fetch issue body:\n{result.stderr}", file=sys.stderr)
        sys.exit(1)
    return result.stdout.rstrip("\n")


def fetch_comments(repo: str, issue: int) -> list[dict]:
    result = _run(["gh", "api", f"repos/{repo}/issues/{issue}/comments", "--paginate"])
    if result.returncode != 0:
        print(f"[FETCH-L1-CONTEXT] failed to fetch comments:\n{result.stderr}", file=sys.stderr)
        sys.exit(1)
    # --paginate concatenates one JSON array per page back to back, not one
    # combined array -- reparse each `]​[` -adjacent chunk defensively by
    # loading the whole stdout as a sequence of arrays.
    text = result.stdout.strip()
    if not text:
        return []
    decoder = json.JSONDecoder()
    comments: list[dict] = []
    idx = 0
    while idx < len(text):
        while idx < len(text) and text[idx] in " \n\t":
            idx += 1
        if idx >= len(text):
            break
        obj, end = decoder.raw_decode(text, idx)
        comments.extend(obj)
        idx = end
    return comments


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Fetch only the issue body and Lane-1-authored comments -- never the full thread."
    )
    parser.add_argument("--repo", required=True, help="Target repo, e.g. vitalharmony/hrse")
    parser.add_argument("--issue", required=True, type=int, help="Issue number")
    args = parser.parse_args()

    body = fetch_issue_body(args.repo, args.issue)
    all_comments = fetch_comments(args.repo, args.issue)
    included = [c for c in all_comments if is_lane1_comment(c.get("body", ""))]
    excluded_count = len(all_comments) - len(included)

    print(f"## Issue body ({args.repo}#{args.issue})\n")
    print(body)
    print()
    for c in included:
        print(f"## Comment {c.get('html_url', '(no url)')}\n")
        print(c.get("body", ""))
        print()

    print(
        f"[FETCH-L1-CONTEXT] {len(included)} Lane-1 comment(s) included, "
        f"{excluded_count} comment(s) excluded (Lane 2/3 direct posts or non-Lane-1 discussion)",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
