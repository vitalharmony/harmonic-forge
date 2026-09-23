#!/usr/bin/env python3
"""Stage, append a transaction-log entry, and commit — in that order, one commit total.

harmonic-forge's own commit task (harmonic-forge#54), scoped down from HRSE2's
version: no version-bump tier in the message hierarchy, because this repo
has no running artifact to stamp (see mise.toml's header comment for the
full reasoning). Two-tier hierarchy instead of three: explicit override,
else a default message.

Calls directly into tools/transaction-log/ — the published, generic
implementation this repo itself ships — rather than reimplementing the
compute-before-commit, single-commit-total ordering hrse#242 established.
Per #54's acceptance criteria, importing the library (not re-deriving the
logic) is the one thing this task can't silently drift on.
"""

import argparse
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "tools" / "transaction-log"))
from transaction_log import append_log_entry, newest_entry_header  # noqa: E402
from diffstat_summary import diffstat_summary  # noqa: E402

LOG_PATH = PROJECT_ROOT / "transaction-log.md"


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

        # If a previous invocation appended the log entry but then failed to
        # commit (e.g. a hook rejection), re-running would append a second,
        # duplicate entry before the retried commit succeeds. Skip the append
        # when the newest entry already matches this exact commit message.
        if newest_entry_header(LOG_PATH) == f"## {commit_msg}":
            print("[TRANSACTION LOG] Entry for this commit already staged from a prior attempt — not duplicating")
        else:
            delta_summary = diffstat_summary(PROJECT_ROOT, cached=True)
            if append_log_entry(LOG_PATH, commit_msg, delta_summary):
                print("[TRANSACTION LOG] Appended entry for upcoming commit")

        # Stage the transaction-log edit too
        execute_git(["git", "add", "transaction-log.md"])

        execute_git(["git", "commit", "-m", commit_msg])
        print("[GIT] Successfully committed.")

    if args.push:
        print("[GIT] Pushing to remote...")
        execute_git(["git", "push", "-u", "origin", "HEAD"])
        print("[GIT] Push complete.")

        # Push is this repo's genuine "publish" event (no version bump
        # exists to hang rotation on — see mise.toml's [tasks.push] comment
        # for the full reasoning) — clear the log after a successful push,
        # but only when publishing main: pushing a feature branch isn't a
        # publish event, and .githooks/pre-commit blocks a commit made
        # directly on main, so the clear itself has to happen on a
        # throwaway branch and get fast-forward-merged back — the same
        # branch -> commit -> merge shape every other commit in this repo
        # already goes through, just automated here.
        current_branch = execute_git(["git", "rev-parse", "--abbrev-ref", "HEAD"], check=False)
        if current_branch != "main":
            print(f"[TRANSACTION LOG] On branch '{current_branch}', not main — skipping clear-on-push "
                  "(clearing only happens when main itself is published).")
            return 0

        clear_result = subprocess.run(
            [sys.executable, str(PROJECT_ROOT / "tools" / "transaction-log" / "transaction_log.py"),
             "--project-root", str(PROJECT_ROOT), "clear"],
            capture_output=True, text=True,
        ).stdout.strip()
        print(f"[TRANSACTION LOG] Clear on push: {clear_result}")

        if clear_result == "cleared":
            clear_branch = "chore/clear-transaction-log"
            execute_git(["git", "checkout", "-b", clear_branch])
            execute_git(["git", "add", "transaction-log.md"])
            execute_git(["git", "commit", "-m", "chore: clear transaction-log.md after push"])
            execute_git(["git", "checkout", "main"])
            execute_git(["git", "merge", clear_branch, "--ff-only"])
            execute_git(["git", "branch", "-d", clear_branch])
            execute_git(["git", "push", "-u", "origin", "HEAD"])

    return 0


if __name__ == "__main__":
    sys.exit(main())
