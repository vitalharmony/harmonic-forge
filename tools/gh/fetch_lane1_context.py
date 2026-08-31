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
  - `kind=handoff` / `kind=ready-for-l3` / `kind=sweep` / `kind=ae` --
    inherently Lane-1-only kinds, no further check needed. (`ae` added
    hrse#929/harmonic-forge -- was missing here until a live Lane 3 Codex
    session on hrse#327 found its own AE comment invisible to this filter.)
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

The default output remains the filtered context used before Lane 3 writes its
test spec.  After AE, ``--target-metadata`` returns only the validated,
immutable target identity; it never returns the AE prose.  ``--comment-id``
uses the same trusted transport for a single explicitly named comment.

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
_SHA_RE = re.compile(r"(?:^|;\s*)sha=([0-9a-f]{40})(?=;|\s|-->)")
_LANE1_KINDS = {"handoff", "ready-for-l3", "sweep", "ae"}
_LANE1_POSTED_BY = {"LANE1", "LANE-unset"}
_CANONICAL_REPOS = {"vitalharmony/hrse", "vitalharmony/harmonic-forge"}


class NoAttestedTarget(ValueError):
    """No current AE exists yet; pre-AE context reads remain permitted."""


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
        # The shared posters do not currently add posted-by to these
        # Lane-1-only kinds.  If a hand-authored marker does carry an explicit
        # lane identity, however, it must not be allowed to forge one of them.
        posted_by_match = _POSTED_BY_RE.search(marker)
        return posted_by_match is None or posted_by_match.group(1) in _LANE1_POSTED_BY
    if kind == "discussion":
        posted_by_match = _POSTED_BY_RE.search(marker)
        return posted_by_match is not None and posted_by_match.group(1) in _LANE1_POSTED_BY
    return False


def _marker_kind(body: str) -> str | None:
    marker_match = _MARKER_RE.search(body)
    if marker_match is None:
        return None
    kind_match = _KIND_RE.search(marker_match.group(0))
    return kind_match.group(1) if kind_match else None


def attested_target(repo: str, comments: list[dict]) -> dict[str, str]:
    """Return the current single-marker Lane 1 AE target, never its body.

    An issue may contain historical AEs from superseded gate rounds.  A later
    eligible Lane 1 lifecycle comment invalidates the prior authorization;
    only the atomic sweep that immediately follows an AE is part of the same
    authorization event.  A later AE starts a new event.  The selected AE
    itself must pass the established Lane 1 filter, be structurally
    unambiguous, and carry a full commit SHA.
    """
    if repo not in _CANONICAL_REPOS:
        raise ValueError("repository is not a canonical supported target")
    lane1_comments = [c for c in comments if is_lane1_comment(c.get("body", ""))]
    ae_indexes = [
        index for index, comment in enumerate(lane1_comments)
        if _marker_kind(comment.get("body", "")) == "ae"
    ]
    if not ae_indexes:
        raise NoAttestedTarget("no valid Lane 1 AE exists for this issue")
    ae_index = ae_indexes[-1]
    later_kinds = [
        _marker_kind(comment.get("body", ""))
        for comment in lane1_comments[ae_index + 1:]
    ]
    if any(kind != "sweep" for kind in later_kinds):
        raise NoAttestedTarget("the newest Lane 1 AE belongs to a superseded gate round")
    body = lane1_comments[ae_index].get("body", "")
    markers = _MARKER_RE.findall(body)
    if len(markers) != 1:
        raise ValueError("current AE must contain exactly one Lane 1 attestation marker")
    sha_match = _SHA_RE.search(markers[0])
    if sha_match is None:
        raise ValueError("current AE does not attest one immutable 40-character SHA")
    return {"repository": repo, "sha": sha_match.group(1)}


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


def fetch_named_comment(repo: str, issue: int, comment_id: int) -> str:
    result = _run(["gh", "api", f"repos/{repo}/issues/comments/{comment_id}"])
    if result.returncode != 0:
        print(f"[FETCH-L1-CONTEXT] failed to fetch named comment:\n{result.stderr}", file=sys.stderr)
        sys.exit(1)
    comment = json.loads(result.stdout)
    expected_issue = f"https://api.github.com/repos/{repo}/issues/{issue}"
    if comment.get("issue_url") != expected_issue:
        raise ValueError("named comment does not belong to the requested issue")
    body = comment.get("body")
    if not isinstance(body, str):
        raise ValueError("named comment has no readable body")
    return body


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Fetch only the issue body and Lane-1-authored comments -- never the full thread."
    )
    parser.add_argument("--repo", required=True, help="Target repo, e.g. vitalharmony/hrse")
    parser.add_argument("--issue", required=True, type=int, help="Issue number")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--target-metadata", action="store_true", help="Emit validated AE target JSON only")
    mode.add_argument("--comment-id", type=int, help="Emit one named comment after issue-membership validation")
    args = parser.parse_args()

    if args.repo not in _CANONICAL_REPOS:
        parser.error("--repo must name a canonical supported repository")
    if args.comment_id is not None:
        if args.comment_id < 1:
            parser.error("--comment-id must be a positive integer")
        print(fetch_named_comment(args.repo, args.issue, args.comment_id))
        return 0

    body = fetch_issue_body(args.repo, args.issue)
    all_comments = fetch_comments(args.repo, args.issue)
    if args.target_metadata:
        try:
            print(json.dumps(attested_target(args.repo, all_comments), sort_keys=True))
        except NoAttestedTarget as exc:
            print(f"[FETCH-L1-CONTEXT] target metadata unavailable: {exc}", file=sys.stderr)
            return 3
        except ValueError as exc:
            print(f"[FETCH-L1-CONTEXT] invalid target metadata: {exc}", file=sys.stderr)
            return 1
        return 0

    # AE prose is authorization data, not spec-derivation context. The trusted
    # provider consumes it for metadata but never returns it to the adapter.
    included = [
        c for c in all_comments
        if is_lane1_comment(c.get("body", "")) and _marker_kind(c.get("body", "")) != "ae"
    ]
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
