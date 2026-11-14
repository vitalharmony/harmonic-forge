#!/usr/bin/env python3
"""After a landed `gh pr merge`, name every lane worktree on that repo now behind
the new `main` (harmonic-forge#1362).

The session that merges is the only party that knows `main` moved, and nothing
told it which in-flight branches the merge left stale. A queued ready-for-l3
citing one of their SHAs is then refused by `l1_post.py`
("attested SHA is not based on current origin/main"), several rounds later.

`batch_consume.py` calls `warn(command, cwd)` last, after its BATCH bookkeeping
and the prune, so a slow scan can never cost either. Read-only: git object
reads in each worktree, and two kinds of GET.

- **Which worktrees.** Every directory under the root (`STALE_WORKTREE_ROOT`,
  default `~/Harmonic_Projects/.worktrees`) holding `.git`, grouped by
  `git rev-parse --git-common-dir` so each repo's origin is read once.
- **Behind.** The new head's commit object is absent, or is not an ancestor of
  HEAD.
- **In flight.** `repos/R/commits/<HEAD>/pulls`: 422 "No commit found" is an
  unpushed head (named "no PR (unpushed)"), an empty list "no PR", an open PR
  "PR #n open". Every PR closed means finished work, merged or not, and is
  skipped. Any other answer is "PR state unknown" and is named.
- **Own deadline.** `STALE_SCAN_BUDGET_S` from the scan's start, never
  `batch_auth._hook_seconds_left()` (the 7 s PreToolUse budget, already spent in
  part). Every subprocess gets the time left. When it runs out, the scan stops
  and the message says how many worktrees were not checked.

Any error is one stderr line and no stdout: the hook never fails.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import batch_auth  # noqa: E402
from hook_identity import slot_env  # noqa: E402

ROOT_ENV = "STALE_WORKTREE_ROOT"
DEFAULT_ROOT = Path.home() / "Harmonic_Projects" / ".worktrees"
STALE_SCAN_BUDGET_S = 15.0


def _left(deadline: float) -> float:
    return max(0.0, deadline - time.monotonic())


def _git(path: str, args: list[str], deadline: float, runner=subprocess.run):
    left = _left(deadline)
    if left <= 0:
        return None
    try:
        return runner(["git", "-C", path, *args], capture_output=True, text=True,
                      check=False, timeout=left)
    except (OSError, subprocess.SubprocessError):
        return None


def _gh(args: list[str], repo: str, deadline: float, runner=subprocess.run):
    left = _left(deadline)
    if left <= 0:
        return None
    try:
        return runner(["gh", *args], capture_output=True, text=True, check=False,
                      timeout=left, env=slot_env(repo))
    except (OSError, subprocess.SubprocessError):
        return None


def origin_repo(path: str | None, deadline: float, runner=subprocess.run) -> str | None:
    """`owner/repo` of the checkout at `path`, read under this scan's deadline.

    Not `batch_auth.cwd_repo`: that draws on the 7 s PreToolUse budget, which
    `consume()` may already have spent (preclose pass 1, survivor 4)."""
    if not path:
        return None
    url = _git(path, ["remote", "get-url", "origin"], deadline, runner)
    if url is None or url.returncode != 0:
        return None
    match = batch_auth._ORIGIN_SLUG.search((url.stdout or "").strip())
    return f"{match.group(1)}/{match.group(2)}" if match else None


def landed(repo: str, number: int, deadline: float, runner=subprocess.run) -> bool:
    """Did PR `number` merge? The same REST read as `batch_auth.action_landed`,
    but bounded by this scan's deadline, not that function's fixed 30 s timeout
    (preclose pass 1, survivor 2)."""
    done = _gh(["api", f"repos/{repo}/pulls/{number}", "--jq", ".merged"], repo, deadline,
               runner)
    return done is not None and done.returncode == 0 and (done.stdout or "").strip() == "true"


def merged_repos(command: str, cwd: str | None, deadline: float,
                 runner=subprocess.run) -> list[str]:
    """Repos a landed merge in `command` moved, de-duplicated, in order."""
    try:
        segments = batch_auth.command_segments(command)
    except ValueError:
        return []
    repos: list[str] = []
    for raw in segments:
        info = batch_auth.classify_pr_merge(batch_auth.strip_invocation_prefix(raw))
        if info is None:
            continue
        repo, number = info
        if repo is None and number:
            repo = origin_repo(cwd, deadline, runner)
        if not repo or not number or repo in repos:
            continue
        if landed(repo, number, deadline, runner):
            repos.append(repo)
    return repos


def main_head(repo: str, deadline: float, runner=subprocess.run) -> str | None:
    done = _gh(["api", f"repos/{repo}/branches/main", "--jq", ".commit.sha"], repo,
               deadline, runner)
    if done is None or done.returncode != 0:
        return None
    sha = (done.stdout or "").strip()
    return sha if len(sha) == 40 else None


def behind(path: str, main_sha: str, deadline: float, runner=subprocess.run) -> bool | None:
    """True when HEAD lacks `main_sha`; None when it cannot be told in time."""
    present = _git(path, ["cat-file", "-e", f"{main_sha}^{{commit}}"], deadline, runner)
    if present is None:
        return None
    if present.returncode != 0:
        return True
    ancestor = _git(path, ["merge-base", "--is-ancestor", main_sha, "HEAD"], deadline, runner)
    if ancestor is None:
        return None
    return ancestor.returncode != 0


def in_flight(repo: str, head: str, deadline: float, runner=subprocess.run) -> str | None:
    """The label to name a behind worktree by, or None when its work is finished."""
    done = _gh(["api", f"repos/{repo}/commits/{head}/pulls"], repo, deadline, runner)
    if done is None:
        return "PR state unknown"
    if done.returncode != 0:
        detail = (done.stdout or "") + (done.stderr or "")
        if "422" in detail or "No commit found" in detail:
            return "no PR (unpushed)"
        return "PR state unknown"
    try:
        pulls = json.loads(done.stdout or "[]")
    except ValueError:
        return "PR state unknown"
    if not pulls:
        return "no PR"
    open_ = [p for p in pulls if p.get("state") == "open"]
    if open_:
        return f"PR #{open_[0].get('number')} open"
    return None


def stale_worktrees(repo: str, main_sha: str, root: Path, deadline: float,
                    runner=subprocess.run) -> tuple[list[dict], int, int]:
    """`(found, timed_out, errors)`: the behind, in-flight worktrees on `repo`;
    how many were not reached before the deadline; and how many could not be
    read for any other reason (a git error, no or an unknown origin).

    The two counts are kept apart (preclose pass 1, survivor 1): only
    `timed_out` is a fact about this merge. An unreadable or origin-less
    directory is not evidence that anything on `repo` is behind, so it never
    makes the message speak; `warn` reports it on stderr."""
    found: list[dict] = []
    timed_out = errors = 0

    def failed() -> None:
        nonlocal timed_out, errors
        if _left(deadline) <= 0:
            timed_out += 1
        else:
            errors += 1

    try:
        candidates = sorted(p for p in root.iterdir() if (p / ".git").exists())
    except OSError:
        return [], 0, 1
    origins: dict[str, str | None] = {}
    for path in candidates:
        if _left(deadline) <= 0:
            timed_out += 1
            continue
        common = _git(str(path), ["rev-parse", "--git-common-dir"], deadline, runner)
        if common is None or common.returncode != 0:
            failed()
            continue
        key = str((path / common.stdout.strip()).resolve())
        if key not in origins:
            origins[key] = origin_repo(str(path), deadline, runner)
        if origins[key] is None:
            failed()
            continue
        if origins[key].lower() != repo.lower():
            continue
        state = behind(str(path), main_sha, deadline, runner)
        if state is None:
            failed()
            continue
        if not state:
            continue
        head = _git(str(path), ["rev-parse", "HEAD"], deadline, runner)
        branch = _git(str(path), ["rev-parse", "--abbrev-ref", "HEAD"], deadline, runner)
        if head is None or head.returncode != 0:
            failed()
            continue
        sha = head.stdout.strip()
        label = in_flight(repo, sha, deadline, runner)
        if label is None:
            continue
        found.append({"dir": path.name, "branch": (branch.stdout.strip() if branch else "?"),
                      "head": sha[:9], "label": label})
    return found, timed_out, errors


def message(repo: str, main_sha: str, found: list[dict], timed_out: int) -> str | None:
    if not found and timed_out == 0:
        return None
    lines = [f"`main` of {repo} moved to {main_sha[:9]}. Lane worktrees now behind it:"]
    lines += [f"- {f['dir']} ({f['branch']} @ {f['head']}): {f['label']}" for f in found]
    if timed_out:
        lines.append(f"{timed_out} worktree(s) were not checked before the time limit.")
    lines.append("Any queued post that cites one of these SHAs will be refused. Dry-run every "
                 "queued post before posting any of them, and post a rebase rework where one "
                 "is owed.")
    return "\n".join(lines)


def warn(command: str, cwd: str | None, root: Path | None = None,
         runner=subprocess.run) -> str | None:
    """The `additionalContext` text for this command, or None."""
    if "merge" not in command:
        return None
    deadline = time.monotonic() + STALE_SCAN_BUDGET_S
    root = root or Path(os.environ.get(ROOT_ENV) or DEFAULT_ROOT)
    parts = []
    for repo in merged_repos(command, cwd, deadline, runner):
        sha = main_head(repo, deadline, runner)
        if sha is None:
            continue
        found, timed_out, errors = stale_worktrees(repo, sha, root, deadline, runner)
        if errors:
            print(f"[stale-worktrees] {errors} worktree(s) under {root} could not be read "
                  "(git error or no known origin); not reported", file=sys.stderr)
        text = message(repo, sha, found, timed_out)
        if text:
            parts.append(text)
    return "\n\n".join(parts) or None
