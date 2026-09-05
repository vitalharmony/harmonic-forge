#!/usr/bin/env python3
"""Receipt-backed Lane 2 status posting, with mandatory post/fetch/diff
self-check (harmonic-forge#371).

Composes an L2P/L2D/L2B status comment from the caller's own recorded
receipts (see receipt_runner.py) plus a clearly separated narrative
section -- never blends the two. The factual scaffold ("comment N exists
with this body hash", "the wrapped command exited 0/N") comes only from
receipts and from this script's own fresh REST snapshot; a "no new
comment exists" claim is refused unless it is backed by a snapshot taken
at call time (`snapshot`), never from memory or inference (AC3).

Posting goes through exactly one transport (`gh api ... -X POST`), and
this script refuses to report success until it has independently
re-fetched the comment it just posted and confirmed the id and a SHA-256
match on the body -- the same fetch-and-diff shape `l1_post.py`/
`post_comment.py` already use for Lane 1.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from receipt_runner import clear_lock, is_locked, lock_path, write_receipt  # noqa: E402


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _gh_api(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["gh", "api", *args], text=True, capture_output=True)


def load_receipts(paths: list[str]) -> list[dict]:
    return [json.loads(Path(raw).read_text()) for raw in paths]


def snapshot(repo: str, issue: int) -> dict:
    """Fetch every comment on `issue` right now and record the result as a
    receipt -- the only source AC3 permits for a 'no new comment' claim."""
    result = _gh_api("--paginate", f"repos/{repo}/issues/{issue}/comments")
    if result.returncode != 0:
        raise SystemExit(f"snapshot fetch failed: {result.stderr}")
    comments = json.loads(result.stdout or "[]")
    body = {
        "repo": repo,
        "issue": issue,
        "comment_ids": [comment["id"] for comment in comments],
        "comment_body_sha256": {str(comment["id"]): _sha(comment["body"]) for comment in comments},
        "raw_response_sha256": _sha(result.stdout),
    }
    path = write_receipt(issue, "rest-snapshot", body)
    print(json.dumps({"receipt": str(path), **body}))
    return body


#: harmonic-forge#472. The lead block, per artifact rather than one universal
#: verdict/finding/next schema — the correction the red team made and Lane 1
#: ratified. Forcing four artifacts into one template produces headings that
#: lie: a sweep is pre-execution and has no verdict, an AE is an authorization
#: and reports no finding. Lane 2's three artifacts genuinely do share a shape
#: (status / what changed or what blocks / what happens next), so they share
#: one here — and nothing beyond them does.
LEAD_LABELS = ("Status", "Change", "Next")

#: Required on `completion` and `blocked`, optional on `plan`.
#:
#: The asymmetry is deliberate and is the open question 2 on the issue,
#: answered here as the plan's stated lean rather than left to block the
#: work: a plan's "finding" IS the plan, and a mandatory one-line summary of
#: something the reader is about to read in full produces filler. A completion
#: and a blocker both report an outcome that a reader needs before deciding
#: whether to read further, which is the operator's actual complaint.
LEAD_REQUIRED_KINDS = ("completion", "blocked")


def lead_block(lead: dict[str, str]) -> str:
    """The visible three lines. Empty string when nothing was supplied."""
    lines = [f"**{label}:** {lead[label].strip()}"
             for label in LEAD_LABELS if lead.get(label, "").strip()]
    return "\n".join(lines) + "\n\n" if lines else ""


def compose_body(kind: str, receipts: list[dict], narrative: str,
                 lead: dict[str, str] | None = None) -> str:
    """Outcome first, evidence collapsed (harmonic-forge#472).

    Two changes from the shape this replaced, both structural rather than
    advisory — AC4 rejects "a convention a lane is asked to remember", and a
    lane physically cannot post through this function without them:

    1. The lead block sits above the narrative, so a reader who reads three
       lines knows the outcome and the next action (AC3).
    2. The receipts JSON moved BELOW the narrative and into a collapsed
       `<details>`. It was the first thing in the comment — evidence ahead of
       outcome, which is the defect this issue names. Nothing is deleted and
       nothing moves to a second comment (AC2).

    The `## L2P|L2D|L2B` heading stays at the top level, outside `<details>`:
    `lane_state.py` reads it, and hrse#1590 made position load-bearing.
    """
    label = {"plan": "L2P", "completion": "L2D", "blocked": "L2B"}[kind]
    fenced = json.dumps(receipts, indent=2, sort_keys=True)
    count = len(receipts)
    return (
        f"## {label} — receipt-backed status (harmonic-forge#371)\n\n"
        f"{lead_block(lead or {})}"
        f"### Narrative\n{narrative}\n\n"
        f"<details><summary>Verified receipts — {count}</summary>\n\n"
        f"```json\n{fenced}\n```\n\n"
        f"</details>\n"
    )


def validate_lead(kind: str, lead: dict[str, str]) -> None:
    """Refuse a completion or a blocked post that buries its outcome."""
    if kind not in LEAD_REQUIRED_KINDS:
        return
    missing = [label for label in LEAD_LABELS if not lead.get(label, "").strip()]
    if missing:
        raise SystemExit(
            f"--kind {kind} requires the lead block (harmonic-forge#472): "
            f"missing {', '.join('--' + label.lower() for label in missing)}. "
            "A reader who never expands the evidence still has to know the "
            "outcome and what happens next."
        )


def post(repo: str, issue: int, body: str) -> dict:
    result = _gh_api("--method", "POST", f"repos/{repo}/issues/{issue}/comments",
                      "-f", f"body={body}")
    if result.returncode != 0:
        raise SystemExit(f"post failed: {result.stderr}")
    posted = json.loads(result.stdout)
    comment_id = posted["id"]
    refetch = _gh_api(f"repos/{repo}/issues/comments/{comment_id}")
    if refetch.returncode != 0:
        raise SystemExit(
            f"post/fetch/diff self-check failed: could not refetch comment "
            f"{comment_id}: {refetch.stderr}"
        )
    refetched_body = json.loads(refetch.stdout).get("body", "")
    if _sha(refetched_body) != _sha(body):
        raise SystemExit(
            f"post/fetch/diff self-check failed: comment {comment_id} body hash "
            "mismatch -- what landed does not match what was sent. Refusing to "
            "report success."
        )
    return {"comment_id": comment_id, "body_sha256": _sha(body), "url": posted.get("html_url")}


def resolve_lock(repo: str, issue: int, resolution_comment: int) -> None:
    check = _gh_api(f"repos/{repo}/issues/comments/{resolution_comment}")
    if check.returncode != 0:
        raise SystemExit(
            f"--resolve-lock refused: comment {resolution_comment} could not be "
            "fetched -- a bare assertion that the lock is resolved is not accepted."
        )
    clear_lock(issue)
    print(f"lock cleared for issue {issue}, resolved by comment {resolution_comment}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)

    post_p = sub.add_parser("post", help="compose and post a receipt-backed status")
    post_p.add_argument("--kind", choices=("plan", "completion", "blocked"), required=True)
    post_p.add_argument("--repo", required=True)
    post_p.add_argument("--issue", type=int, required=True)
    post_p.add_argument("--receipts", nargs="*", default=[])
    post_p.add_argument("--narrative-file", type=Path, required=True)
    post_p.add_argument("--status", default="",
                        help="Lead block: where this issue now stands. Required "
                             "for --kind completion/blocked (harmonic-forge#472).")
    post_p.add_argument("--change", default="",
                        help="Lead block: what changed, or what blocks. Required "
                             "for --kind completion/blocked.")
    post_p.add_argument("--next", dest="next_action", default="",
                        help="Lead block: the literal next action. Required for "
                             "--kind completion/blocked.")

    snap_p = sub.add_parser("snapshot", help="fetch and record a fresh comment snapshot")
    snap_p.add_argument("--repo", required=True)
    snap_p.add_argument("--issue", type=int, required=True)

    lock_p = sub.add_parser("resolve-lock", help="clear an issue-scoped failure lock")
    lock_p.add_argument("--repo", required=True)
    lock_p.add_argument("--issue", type=int, required=True)
    lock_p.add_argument("--resolution-comment", type=int, required=True)

    args = parser.parse_args()

    if args.action == "snapshot":
        snapshot(args.repo, args.issue)
        return 0

    if args.action == "resolve-lock":
        resolve_lock(args.repo, args.issue, args.resolution_comment)
        return 0

    # args.action == "post"
    if is_locked(args.issue) and args.kind != "blocked":
        print(
            f"issue {args.issue} is locked ({lock_path(args.issue)}) by a failed "
            "underlying command -- run `l2_post.py resolve-lock` with a real, "
            "fetchable resolution comment first, or post --kind blocked instead.",
            file=sys.stderr,
        )
        return 2

    receipts = load_receipts(args.receipts)
    narrative = args.narrative_file.read_text()
    lead = {"Status": args.status, "Change": args.change, "Next": args.next_action}
    validate_lead(args.kind, lead)
    body = compose_body(args.kind, receipts, narrative, lead)
    result = post(args.repo, args.issue, body)
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
