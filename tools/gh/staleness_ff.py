"""harmonic-forge#1075: fast-forward a clean, strictly-behind main checkout.

Called by `watch_lane_posts._check_git_staleness` once it has counted
`behind > 0`. Mirrors the launcher's rule (`tools/lane/_lane_refresh.sh`):
`merge --ff-only`, only on a clean `refs/heads/main` with no merge or rebase
in progress. On success the process re-executes itself, so the belt runs the
updated code rather than the pre-merge modules already imported. In every
other state it returns the reason to refuse, and writes nothing.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def _git(repo_root: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", repo_root, *args],
                          capture_output=True, text=True, timeout=15)


def _merge_ff(repo_root: str) -> tuple[int, str]:
    proc = _git(repo_root, "merge", "--ff-only", "origin/main")
    return proc.returncode, (proc.stderr or proc.stdout).strip()


def _in_progress(repo_root: str) -> str | None:
    for name, label in (("MERGE_HEAD", "merge"), ("rebase-merge", "rebase"),
                        ("rebase-apply", "rebase")):
        path = _git(repo_root, "rev-parse", "--git-path", name).stdout.strip()
        if path and (Path(repo_root) / path).exists():
            return f"a {label} is in progress"
    return None


def _reexec(repo_root: str, old: str) -> None:
    new = _git(repo_root, "rev-parse", "HEAD").stdout.strip()
    print(f"[watch_lane_posts] fast-forwarded ~/harmonic-forge "
          f"{old[:12]}..{new[:12]}", file=sys.stderr)
    os.execv(sys.executable, [sys.executable, *sys.argv])


def fast_forward_or_reason(repo_root: str, head: str) -> str:
    """Fast-forward and re-exec (never returns), or return why it refused.

    The read-only checks may raise; the caller fails open on those. From
    `merge --ff-only` on, the checkout may have changed, so an exception there
    is a refusal, never a fail-open start on pre-merge code."""
    # A rebase detaches HEAD, so this runs first to name it (AC4).
    reason = _in_progress(repo_root)
    if reason:
        return reason
    if _git(repo_root, "status", "--porcelain", "--untracked-files=no").stdout.strip():
        return "the tree is dirty"
    sym = _git(repo_root, "symbolic-ref", "-q", "HEAD")
    if sym.returncode != 0:
        return "HEAD is detached"
    if sym.stdout.strip() != "refs/heads/main":
        return f"on {sym.stdout.strip()}, not refs/heads/main"
    if _git(repo_root, "merge-base", "--is-ancestor", "HEAD", "origin/main").returncode != 0:
        return "HEAD has diverged from origin/main"
    try:
        code, msg = _merge_ff(repo_root)
    except (subprocess.SubprocessError, OSError) as exc:
        return f"merge --ff-only failed or timed out: {exc} (check .git/index.lock)"
    try:
        if code != 0:
            recount = _git(repo_root, "rev-list", "--count", "HEAD..origin/main")
            if recount.returncode != 0 or recount.stdout.strip() != "0":
                return f"merge --ff-only failed: {msg}"
        _reexec(repo_root, head)
    except (subprocess.SubprocessError, OSError) as exc:
        return f"fast-forwarded but re-exec failed: {exc}"
    return "re-exec returned"  # only reachable when os.execv is patched out
