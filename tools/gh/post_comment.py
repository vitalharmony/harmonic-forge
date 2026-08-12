#!/usr/bin/env python3
"""Post a GitHub issue comment via REST, then self-check by refetching.

Mechanizes ADR-004's GitHub Comment Formatting rule (write to a file, post,
refetch and confirm it rendered legibly) so it doesn't depend on a lane
remembering it at the moment of a routine action. This exact failure class
recurred three times on HRSE2 (#234, #235, #236) despite the rule being
written down twice.

Generalized from HRSE2's original `scripts/post_comment.py` (harmonic-forge#53)
after a hardcoded `REPO = "vitalharmony/hrse"` default caused a real mis-post:
this tool was invoked for an harmonic-forge issue out of HRSE2 habit and silently
posted to an unrelated HRSE2 issue instead. Fix: `--repo` is now required,
with no default, and a banner prints the resolved target before acting so a
misconfigured `--repo` is visible immediately, not discovered after the fact.

Posts via `gh api --method POST .../issues/{n}/comments` (pure REST) rather
than `gh issue comment`, which uses GraphQL internally (harmonic-forge#225) —
GraphQL has a much lower effective budget in practice, and a single heavy
verification pass elsewhere (e.g. an agent doing live sub-issue-linkage
checks) can exhaust it and block ordinary comment-posting for up to an hour
for a reason unrelated to posting itself. The self-check step below was
already pure REST and is unchanged.
"""

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path


def post_comment(repo: str, issue: int, body_file: Path, source_content: str) -> int:
    print(f"[POST-COMMENT] Posting to {repo}#{issue}")

    result = subprocess.run(
        [
            "gh", "api", "--method", "POST",
            f"repos/{repo}/issues/{issue}/comments",
            "-F", f"body=@{body_file}",
            "--jq", ".html_url",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"[POST-COMMENT] gh api POST failed:\n{result.stderr}", file=sys.stderr)
        return 1

    comment_url = result.stdout.strip()
    print(f"[POST-COMMENT] Posted: {comment_url}")

    # The URL ends with #issuecomment-<id> — fetch that exact comment back,
    # not just "the last one" (which could race with a concurrent poster).
    if "#issuecomment-" not in comment_url:
        print(f"[POST-COMMENT] Warning: could not parse comment id from '{comment_url}', skipping self-check", file=sys.stderr)
        return 1
    comment_id = comment_url.rsplit("#issuecomment-", 1)[1]

    fetch = subprocess.run(
        ["gh", "api", f"repos/{repo}/issues/comments/{comment_id}", "-q", ".body"],
        capture_output=True,
        text=True,
    )
    if fetch.returncode != 0:
        print(f"[POST-COMMENT] Self-check fetch failed:\n{fetch.stderr}", file=sys.stderr)
        return 1

    posted_content = fetch.stdout
    # gh api -q ".body" appends a trailing newline; the source may or may
    # not have one. Compare with trailing whitespace normalized only.
    if posted_content.rstrip("\n") != source_content.rstrip("\n"):
        print(
            "[POST-COMMENT] SELF-CHECK FAILED: posted comment does not match source content "
            "(likely stripped/mangled formatting). Compare manually before trusting this comment:\n"
            f"  {comment_url}",
            file=sys.stderr,
        )
        return 1

    print("[POST-COMMENT] Self-check passed: posted content matches source exactly.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Post a GitHub issue comment with a mandatory self-check")
    parser.add_argument("--repo", required=True, help="Target repo, e.g. vitalharmony/hrse (no default — must be explicit)")
    parser.add_argument("--issue", type=int, required=True, help="Issue number")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--file", type=Path, help="Path to a file containing the comment body")
    group.add_argument("--body", type=str, help="Comment body as a literal string (written to a tempfile internally)")
    args = parser.parse_args()

    tmp_path: Path | None = None
    if args.file:
        if not args.file.exists():
            print(f"[POST-COMMENT] File not found: {args.file}", file=sys.stderr)
            return 1
        body_file = args.file
        source_content = args.file.read_text()
    else:
        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False)
        tmp.write(args.body)
        tmp.close()
        tmp_path = Path(tmp.name)
        body_file = tmp_path
        source_content = args.body

    try:
        return post_comment(args.repo, args.issue, body_file, source_content)
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)


if __name__ == "__main__":
    sys.exit(main())
