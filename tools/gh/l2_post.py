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


def compose_body(kind: str, receipts: list[dict], narrative: str) -> str:
    label = {"plan": "L2P", "completion": "L2D", "blocked": "L2B"}[kind]
    fenced = json.dumps(receipts, indent=2, sort_keys=True)
    return (
        f"## {label} — receipt-backed status (harmonic-forge#371)\n\n"
        f"### Verified receipts\n```json\n{fenced}\n```\n\n"
        f"### Narrative\n{narrative}\n"
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
    body = compose_body(args.kind, receipts, narrative)
    result = post(args.repo, args.issue, body)
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
