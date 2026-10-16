#!/usr/bin/env python3
"""Lane 1 rebases a clean Lane 2 branch itself (harmonic-forge#1018).

When one branch merges, every other open branch falls behind `origin/main`
and `l1_post.py` refuses its `ready-for-l3`:

    if run("git", "merge-base", "--is-ancestor", main_sha, sha).returncode:

(`tools/gh/l1_post.py`, `static_checks`). That check is kept. What this adds
is the cheap answer to it: a mechanical rebase no longer costs a `--kind
rework` and a Lane 2 round trip. A conflict still goes to Lane 2.

Usage: lane1_rebase.py <repo-dir> <issue> <branch>

Exit codes:
  0  `refs/heads/<branch>` (local) and `origin/<branch>` both end at the new
     SHA, printed as `new=<sha>`. Post `ready-for-l3` on it (R-0338).
  1  any other failure (fetch, a lease mismatch, a busy stale prep worktree).
  2  the rebase conflicts; aborted, nothing pushed, conflicting paths listed.
  3  the local branch cannot be moved (dirty impl worktree, missing ref, or
     local commits origin does not carry); both SHAs printed.
  4  a precondition failed (newest lane marker is neither `l2.done` nor a
     Lane 3 PASS on the branch head, a Lane 3 gate is open on the old SHA, or
     the issue is `tooling-exception`).
  5  the pre-push hook refused the push. Never retried with `--no-verify`.

Safe to re-run: a stale prep worktree is pruned at start and the prep
worktree is removed in a `finally`; if `origin/<branch>` already contains
`origin/main` (a crash after the push), it skips straight to the local-ref
step. That holds on the Lane 3 PASS path too (harmonic-forge#1034): before
the push a receipt `{issue, passed_sha, rebased_shas}` is written in the git
dir, and after a crash the PASS (which names the pre-rebase SHA) is accepted
only when that receipt names it and `origin/<branch>` is exactly one of the
receipt's recorded rebased SHAs; the local branch is left to the cherry check
in the local-ref step (operator ruling amending 6219927415). The receipt is
deleted once the local ref moves. The crash state is recorded, never
inferred from history (sticky-wicket reforge, F1034).
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import subprocess
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_GH = _HERE.parent / "gh"
_BUSY = _HERE.parent / "worktree" / "check_worktree_busy.py"

DEFAULT_WORKTREES = Path.home() / "Harmonic_Projects" / ".worktrees"


class Exit(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def git(cwd: str | Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(["git", "-C", str(cwd), *args],
                          capture_output=True, text=True)
    if check and proc.returncode:
        raise Exit(1, f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc


def rev(cwd: str | Path, ref: str) -> str | None:
    proc = git(cwd, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", check=False)
    return proc.stdout.strip() or None


# ---------------------------------------------------------------- preconditions

def repo_slug(repo_dir: str) -> str:
    url = git(repo_dir, "remote", "get-url", "origin").stdout.strip()
    match = re.search(r"[:/]([^/:]+/[^/]+?)(?:\.git)?/?$", url)
    if not match:
        raise Exit(1, f"cannot derive owner/repo from origin url {url!r}")
    return match.group(1)


def _watch():
    sys.path.insert(0, str(_GH))
    import watch_lane_posts  # noqa: PLC0415 -- lazy: the network reader
    return watch_lane_posts


def _gate_ci():
    sys.path.insert(0, str(_GH))
    import gate_ci  # noqa: PLC0415 -- lazy, beside watch_lane_posts
    return gate_ci


def newest_lane_marker(repo_dir: str, issue: int) -> str | None:
    """`lane.detail` of the issue's newest classified comment, e.g.
    `l2.done`, read with the belt's own classifier so the two cannot drift.
    A Lane 3 gate report (`_classify` == `('l3', 'gate-result')`: footer
    `kind=gate-result` + `posted-by=LANE3`) whose verdict is PASS is
    `l3.pass:<gated sha>` (harmonic-forge#1034), read with gate_ci's own
    `verdict_of`/`gated_sha`."""
    watch = _watch()
    gate_ci = _gate_ci()
    comments = watch._fetch_all_comments(repo_slug(repo_dir), issue)
    if comments is None:
        raise Exit(4, f"cannot read the comments of #{issue}")
    newest = None
    for comment in comments:
        body = comment.get("body") or ""
        found = watch._classify(body)
        if found:
            lane, detail = found
            # harmonic-forge#1034 (reforge r2, operator ruling): a gate
            # report is recognized by ALLOWLIST only -- the belt's own
            # classifier reading a `kind=gate-result` footer stamped
            # `posted-by=LANE3`. No heading fallback, no kind denylist: a
            # quoted heading in any other comment is never a verdict.
            if found == ("l3", "gate-result"):
                if gate_ci.verdict_of(body) == "PASS":
                    newest = f"l3.pass:{gate_ci.gated_sha(body) or ''}"
                else:
                    newest = f"l3.{detail}"
                continue
            # sticky-wicket PATCH (F1018 pass 2): Lane 1's own posts after the
            # L2D (ready-for-l3, AE, sweep, discussion) do not change the
            # branch's state -- an AE on the old SHA carries forward (R-0209,
            # R-0390). Only a Lane 1 rework sends the branch back to Lane 2.
            if lane == "l1" and detail != "rework":
                continue
            # preclose F1018-1: l2_post stamps `kind=completion`, and a legacy
            # heading classifies as `## L2D ...`; both are the belt's own
            # completion test, so the two cannot drift.
            if watch._is_l2_completion(lane, detail) or (
                    lane == "l2" and detail.lstrip("# ").startswith("L2D")):
                detail = "done"
            newest = f"{lane}.{detail}"
    return newest


def issue_labels(repo_dir: str, issue: int) -> set[str]:
    state, labels, _ = _watch()._read_issue_meta(repo_slug(repo_dir), issue)
    if labels is None:
        raise Exit(4, f"cannot read the labels of #{issue}")
    return labels


def worktrees(repo_dir: str) -> list[dict[str, str]]:
    out = git(repo_dir, "worktree", "list", "--porcelain").stdout
    entries, entry = [], {}
    for line in out.splitlines():
        if not line.strip():
            if entry:
                entries.append(entry)
            entry = {}
            continue
        key, _, value = line.partition(" ")
        entry[key] = value
    if entry:
        entries.append(entry)
    return entries


def lane3_open_on(repo_dir: str, sha: str) -> str | None:
    """A worktree whose HEAD is `sha` and which carries `lane3-begin`'s
    `LANE3_ACTIVE` marker, or None."""
    for entry in worktrees(repo_dir):
        path = entry.get("worktree")
        if entry.get("HEAD") != sha or not path or not Path(path).is_dir():
            continue
        git_dir = git(path, "rev-parse", "--absolute-git-dir", check=False).stdout.strip()
        if git_dir and (Path(git_dir) / "LANE3_ACTIVE").exists():
            return path
    return None


def _same_sha(a: str | None, b: str | None) -> bool:
    sys.path.insert(0, str(_GH))
    import check_lane3_ready  # noqa: PLC0415
    return check_lane3_ready.same_sha(a, b)


def receipt_path(repo_dir: str, issue: int) -> Path:
    common = git(repo_dir, "rev-parse", "--git-common-dir").stdout.strip()
    base = Path(common) if Path(common).is_absolute() else Path(repo_dir) / common
    return base / f"lane1-rebase-{issue}.json"


def read_receipt(repo_dir: str, issue: int) -> dict | None:
    try:
        data = json.loads(receipt_path(repo_dir, issue).read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def write_receipt(repo_dir: str, issue: int, passed: str, old: str, rebased: str) -> None:
    """Record `{issue, passed_sha, rebased_shas}` before a push, `passed`
    being the Lane 3 PASS Head-SHA (never guessed). The rebased SHAs carry
    forward only when the prior receipt names this same PASS and `old` is
    one it says was pushed (a re-rebase after a crash and a main move);
    otherwise the receipt is fresh (sticky-wicket PATCH, F1034)."""
    prior = read_receipt(repo_dir, issue)
    if (prior and prior.get("issue") == issue
            and _same_sha(passed, prior.get("passed_sha"))
            and old in prior.get("rebased_shas", [])):
        shas = prior["rebased_shas"] + [rebased]
    else:
        shas = [rebased]
    receipt_path(repo_dir, issue).write_text(json.dumps(
        {"issue": issue, "passed_sha": passed, "rebased_shas": shas}))


def drop_receipt(repo_dir: str, issue: int) -> None:
    receipt_path(repo_dir, issue).unlink(missing_ok=True)


def _pass_covers(repo_dir: str, issue: int, old: str, passed: str) -> bool:
    """A PASS covers the branch when it names the head, or (operator ruling
    on #1034) the crash-after-push state -- recorded, never inferred
    (sticky-wicket reforge): this tool's own receipt names this PASS and
    origin is exactly a SHA the receipt says was pushed. The local branch
    is not consulted (operator ruling amending 6219927415): unpushed local
    work is guarded by move_local_ref's cherry check. Fails closed (exit 4)
    otherwise."""
    if _same_sha(passed, old):
        return True
    receipt = read_receipt(repo_dir, issue)
    if not receipt or receipt.get("issue") != issue:
        return False
    return (_same_sha(passed, receipt.get("passed_sha"))
            and old in receipt.get("rebased_shas", []))


def check_preconditions(repo_dir: str, issue: int, branch: str, old: str) -> str | None:
    """Return the Lane 3 PASS Head-SHA, or None on the l2.done path."""
    # preclose F1018-2: the checks below read #issue, so the branch pushed
    # must be #issue's own (`feat|fix/<issue>-...`), never another issue's.
    if not re.match(rf"(?:feat|fix)/{issue}-", branch):
        raise Exit(4, f"{branch} is not #{issue}'s branch (expected feat/{issue}-... or fix/{issue}-...)")
    marker = newest_lane_marker(repo_dir, issue)
    passed = None
    if marker and marker.startswith("l3.pass:"):
        passed = marker.split(":", 1)[1]
        if not _pass_covers(repo_dir, issue, old, passed):
            raise Exit(4, f"the Lane 3 PASS on #{issue} names {passed[:12] or 'no SHA'}, "
                          f"not the branch head {old[:12]}")
    elif marker != "l2.done":
        raise Exit(4, f"newest lane marker on #{issue} is {marker!r}, "
                      "neither 'l2.done' nor a Lane 3 PASS on the branch head")
    if "tooling-exception" in issue_labels(repo_dir, issue):
        raise Exit(4, f"#{issue} is tooling-exception; Lane 2 branches only")
    gate = lane3_open_on(repo_dir, old)
    if gate:
        raise Exit(4, f"a Lane 3 gate is open on {old[:12]} in {gate}")
    return passed


# ---------------------------------------------------------------- git steps

def fetch(repo_dir: str, branch: str) -> None:
    git(repo_dir, "fetch", "origin",
        "+refs/heads/main:refs/remotes/origin/main",
        f"+refs/heads/{branch}:refs/remotes/origin/{branch}")


def prep_path(repo_dir: str, issue: int) -> Path:
    root = Path(os.environ.get("LANE1_REBASE_WORKTREES") or DEFAULT_WORKTREES)
    return root / f"{Path(repo_dir).resolve().name}-{issue}-prep"


def remove_prep(repo_dir: str, prep: Path, force: bool = False) -> None:
    # Not forced in the `finally`: an aborted rebase leaves a clean tree, so a
    # removal that fails there means the abort did not happen, and the left
    # prep worktree is the visible signal. The stale-prune at start forces.
    if prep.exists():
        git(repo_dir, "worktree", "remove", *(["--force"] if force else []),
            str(prep), check=False)
    git(repo_dir, "worktree", "prune", check=False)


def prune_stale_prep(repo_dir: str, prep: Path) -> None:
    if prep.exists() and _BUSY.exists():
        busy = subprocess.run([sys.executable, str(_BUSY), str(prep)],
                              capture_output=True, text=True)
        if busy.returncode == 1:
            raise Exit(1, f"stale prep worktree {prep} is busy:\n{busy.stdout}{busy.stderr}")
    remove_prep(repo_dir, prep, force=True)


def push(prep: Path, branch: str, old: str) -> None:
    proc = git(prep, "push", f"--force-with-lease={branch}:{old}", "origin",
               f"HEAD:refs/heads/{branch}", check=False)
    if proc.returncode == 0:
        return
    err = proc.stderr.strip()
    if "[rejected]" in err or "stale info" in err:
        raise Exit(1, f"push refused (lease: origin/{branch} is no longer {old[:12]}):\n{err}")
    hook = git(prep, "rev-parse", "--git-path", "hooks/pre-push", check=False).stdout.strip()
    hook_path = Path(hook) if Path(hook).is_absolute() else prep / hook
    if hook and os.access(hook_path, os.X_OK):
        raise Exit(5, f"pre-push hook refused the push (never --no-verify):\n{err}")
    raise Exit(1, f"push failed:\n{err}")


def rebase_and_push(repo_dir: str, issue: int, branch: str, old: str,
                    passed: str | None = None) -> str:
    prep = prep_path(repo_dir, issue)
    prune_stale_prep(repo_dir, prep)
    prep.parent.mkdir(parents=True, exist_ok=True)
    git(repo_dir, "worktree", "add", "--detach", str(prep), old)
    try:
        proc = git(prep, "rebase", "origin/main", check=False)
        if proc.returncode:
            paths = git(prep, "diff", "--name-only", "--diff-filter=U",
                        check=False).stdout.split()
            git(prep, "rebase", "--abort", check=False)
            listed = "\n".join(f"  {p}" for p in paths) or "  (none reported)"
            raise Exit(2, f"rebase of {branch} onto origin/main conflicts:\n{listed}")
        new = rev(prep, "HEAD")
        # sticky-wicket reforge F1034: record the crash state before it can
        # exist, so a re-run never has to infer it from history.
        # No receipt on the l2.done path: there is no PASS to carry.
        if passed:
            write_receipt(repo_dir, issue, passed, old, new)
        push(prep, branch, old)
        return new
    finally:
        remove_prep(repo_dir, prep)


def move_local_ref(repo_dir: str, branch: str, new: str) -> None:
    local = rev(repo_dir, f"refs/heads/{branch}")
    if local == new:
        return
    if local is None:
        raise Exit(3, f"local {branch} does not exist; origin is at {new}")
    # Every local commit origin lacks must be patch-equivalent to one in
    # `new`; otherwise moving the ref would drop unpushed Lane 2 work.
    cherry = git(repo_dir, "cherry", new, local).stdout.splitlines()
    if any(line.startswith("+") for line in cherry):
        raise Exit(3, f"local {branch} is at {local}; origin is at {new} "
                      "(local carries commits origin does not)")
    holder = next((e["worktree"] for e in worktrees(repo_dir)
                   if e.get("branch") == f"refs/heads/{branch}"), None)
    if holder:
        if git(holder, "status", "--porcelain").stdout.strip():
            raise Exit(3, f"local {branch} is at {local}; origin is at {new} "
                          f"(impl worktree {holder} is dirty)")
        git(holder, "reset", "--keep", new)
    else:
        git(repo_dir, "update-ref", f"refs/heads/{branch}", new, local)
    if rev(repo_dir, f"refs/heads/{branch}") != new:
        raise Exit(3, f"local {branch} is at {rev(repo_dir, f'refs/heads/{branch}')}; "
                      f"origin is at {new}")


def run(repo_dir: str, issue: int, branch: str) -> str:
    # preclose F1018-3: one run per issue at a time. The stale-prep busy check
    # ignores same-lane processes, so a concurrent Lane 1 run would look stale.
    lock_path = prep_path(repo_dir, issue).with_suffix(".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Exit(1, f"another lane1-rebase run holds {lock_path}; nothing done") from None
        return _run_locked(repo_dir, issue, branch)


def _run_locked(repo_dir: str, issue: int, branch: str) -> str:
    fetch(repo_dir, branch)
    old = rev(repo_dir, f"refs/remotes/origin/{branch}")
    main = rev(repo_dir, "refs/remotes/origin/main")
    if not old or not main:
        raise Exit(1, f"origin/{branch} or origin/main did not resolve after fetch")
    passed = check_preconditions(repo_dir, issue, branch, old)
    if git(repo_dir, "merge-base", "--is-ancestor", main, old, check=False).returncode == 0:
        new = old  # already on current main (incl. a re-run after a crash)
    else:
        new = rebase_and_push(repo_dir, issue, branch, old, passed)
    move_local_ref(repo_dir, branch, new)
    drop_receipt(repo_dir, issue)
    return new


def main(argv: list[str]) -> int:
    if len(argv) != 3 or not argv[1].isdigit():
        print("usage: lane1-rebase <repo-dir> <issue> <branch>", file=sys.stderr)
        return 1
    try:
        new = run(argv[0], int(argv[1]), argv[2])
    except Exit as exc:
        print(f"[lane1-rebase] exit {exc.code}: {exc.message}", file=sys.stderr)
        return exc.code
    print(f"new={new}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
