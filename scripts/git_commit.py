#!/usr/bin/env python3
"""Stage and commit, optionally push.

harmonic-forge's own commit task (harmonic-forge#54), scoped down from HRSE2's
version: no version-bump tier in the message hierarchy, because this repo
has no running artifact to stamp (see mise.toml's header comment for the
full reasoning). Two-tier hierarchy instead of three: explicit override,
else a default message.

It no longer writes a transaction log (harmonic-forge#883): the view is
rendered from git when it is read (`mise run transaction-log`, and the
SessionStart hook `tools/hooks/transaction_log_context.py`), so a commit has
nothing to append and a push has nothing to clear.
"""

import argparse
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent


def execute_git(cmd: list[str], check: bool = True) -> str:
    result = subprocess.run(
        cmd, cwd=PROJECT_ROOT, capture_output=True, text=True, check=False
    )
    if check and result.returncode:
        if result.stderr:
            print(result.stderr, file=sys.stderr, end="" if result.stderr.endswith("\n") else "\n")
        raise subprocess.CalledProcessError(
            result.returncode, cmd, output=result.stdout, stderr=result.stderr
        )
    return result.stdout.strip()


def main() -> int:
    parser = argparse.ArgumentParser(description="Stage and commit repo changes")
    parser.add_argument("--message", help="Override the auto-generated commit message")
    parser.add_argument("--push", action="store_true", help="Push commit to remote")
    args = parser.parse_args()

    commit_msg = args.message or "docs: Update harmonic-forge"

    status = execute_git(["git", "status", "--porcelain"], check=False)
    if not status:
        print("[GIT] Working tree clean. Nothing to commit.")
        # Real workflow: commit happens on a branch, then a fast-forward
        # merge to main (no new commit) precedes publishing. --push must
        # still run in that case — an empty tree here does NOT mean
        # "nothing to push," it means "no NEW commit to make before
        # pushing whatever's already merged." Only skip commit-only work.
        if not args.push:
            return 0
    else:
        print("[GIT] Changes detected. Staging...")
        execute_git(["git", "add", "."])
        execute_git(["git", "commit", "-m", commit_msg])
        print("[GIT] Successfully committed.")

    if args.push:
        print("[GIT] Pushing to remote...")
        execute_git(["git", "push", "-u", "origin", "HEAD"])
        print("[GIT] Push complete.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
