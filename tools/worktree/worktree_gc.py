#!/usr/bin/env python3
"""Remove implementation worktrees whose issue has closed (harmonic-forge#1004).

Nothing removed a per-issue `<stem>-<N>[a-z]?-impl` worktree once its issue
closed, in any repo: HRSE2's `worktree-gc` knows only `hrse2-*`, and
`hygiene --prune-worktrees` is opt-in and never sees a folder with no `.git`.
This sweeps every such directory under the implementation roots
(`worktree_issue._IMPL_ROOTS`) and prints one line per candidate.

A registered worktree is removed only when, in this order:
  1. its issue resolves (`issue_for_worktree`, or the directory's number when
     a single-digit branch carries it) and the directory number agrees;
  2. `git status --porcelain` is empty;
  3. every ignored file is regenerable (`regenerable()`);
  4. the issue reads `closed` under the repo's own gh slot;
  5. no live process has its cwd inside it (checked last, right before the
     removal, so a session opened during the gh call is still seen).
Removal is `git worktree remove` WITHOUT `--force`, then `git worktree prune`.
The local branch is never touched.

A leftover folder (no `.git`) is deleted only when every path in it is
regenerable and its newest mtime is over an hour old.

Report-only by default; `--execute` removes. Anything undecidable is
`skipped: <reason>` and the sweep continues.

    worktree_gc.py [--execute]
"""
from __future__ import annotations

import argparse
import filecmp
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

TOOLS = Path(__file__).resolve().parents[1]
for sub in ("hooks", "worktree", "onboard"):
    sys.path.insert(0, str(TOOLS / sub))

import worktree_issue  # noqa: E402
from check_worktree_busy import ancestor_pids, busy_pids  # noqa: E402
from manifest import ManifestError, load  # noqa: E402
from manifest_identity import slot_env  # noqa: E402

CACHE_DIRS = frozenset({"node_modules", "__pycache__", ".vite", ".pytest_cache", ".buildcache",
                        "logs", "dist", "test-results"})
LEFTOVER_MIN_AGE_SECONDS = 3600
GH_TIMEOUT_SECONDS = 30


def _git(path: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True, check=False)


def regenerable(root: Path, rel: str, main: Path | None) -> bool:
    """The issue's one definition, shared by registered worktrees and leftovers.
    Never follows a symlink: each prefix is tested with `islink`."""
    parts = Path(rel).parts
    for i in range(1, len(parts) + 1):
        if os.path.islink(root.joinpath(*parts[:i])):
            return True
    for i in range(1, len(parts)):
        if parts[i - 1] in CACHE_DIRS or (root.joinpath(*parts[:i]) / "CACHEDIR.TAG").is_file():
            return True
    if parts and parts[-1] == ".mise.local.toml":
        return True
    if main is None:
        return False
    if Path(rel) == Path("frontend/.env") and (main / "frontend" / ".env").exists():
        return True
    mine, theirs = root / rel, main / rel
    if theirs.is_symlink() or not theirs.is_file() or not mine.is_file():
        return False
    return (mine.stat().st_size == theirs.stat().st_size
            and filecmp.cmp(mine, theirs, shallow=False))


def _number(path: Path) -> int | None:
    match = worktree_issue._IMPL_DIR_RE.search(path.name)
    return int(match.group("number")) if match else None


def resolve(path: Path) -> tuple[str, int] | str:
    """`(repo, issue)`, or the skip reason."""
    number = _number(path)
    found = worktree_issue.issue_for_worktree(str(path))
    if found is None:
        branch = (_git(path, "rev-parse", "--abbrev-ref", "HEAD").stdout or "").strip()
        repo = worktree_issue._origin_repo(str(path))
        if number is None or not repo or not re.search(rf"(?:^|/){number}(?=[-/]|$)", branch):
            return "unresolved"
        return repo, number
    repo, issue = found
    if number is not None and number != issue:
        return f"dir {number} != branch {issue}"
    return repo, issue


def issue_state(repo: str, issue: int) -> str:
    """`open`/`closed`, or raises with the reason (a 404 included)."""
    env = slot_env(repo)
    result = subprocess.run(["gh", "api", f"repos/{repo}/issues/{issue}", "--jq", ".state"],
                            capture_output=True, text=True, env=env, timeout=GH_TIMEOUT_SECONDS,
                            check=False)
    state = result.stdout.strip()
    if result.returncode != 0 or state not in ("open", "closed"):
        raise RuntimeError(f"gh {repo}#{issue}: {(result.stderr or state or 'no state').strip()[:120]}")
    return state


def _summary(paths: list[str]) -> str:
    return ", ".join(paths[:5]) + (f" (+{len(paths) - 5})" if len(paths) > 5 else "")


def _busy(path: Path) -> list[tuple[int, str]]:
    own = os.getpid()
    return busy_pids(path, exclude={own} | ancestor_pids(own), caller_lane=None)


def sweep_registered(path: Path, execute: bool) -> str:
    resolved = resolve(path)
    if isinstance(resolved, str):
        return f"skipped: {resolved}"
    repo, issue = resolved
    status = _git(path, "status", "--porcelain")
    if status.returncode != 0:
        return f"skipped: git status: {status.stderr.strip()[:120]}"
    if status.stdout.strip():
        return f"reported: uncommitted ({len(status.stdout.splitlines())} paths)"
    common = _git(path, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if common.returncode != 0:
        return f"skipped: git common dir: {common.stderr.strip()[:120]}"
    main = Path(common.stdout.strip()).parent
    ignored = _git(path, "ls-files", "--others", "--ignored", "--exclude-standard")
    if ignored.returncode != 0:
        return f"skipped: git ls-files: {ignored.stderr.strip()[:120]}"
    keep = [rel for rel in ignored.stdout.splitlines() if not regenerable(path, rel, main)]
    if keep:
        return f"reported: non-regenerable: {_summary(keep)}"
    try:
        state = issue_state(repo, issue)
    except (ManifestError, SystemExit, RuntimeError, subprocess.TimeoutExpired, OSError) as exc:
        return f"skipped: {repo}#{issue}: {exc}"
    if state != "closed":
        return f"kept: {repo}#{issue} open"
    busy = _busy(path)
    if busy:
        return f"kept: busy pid {busy[0][0]} ({busy[0][1][:60]})"
    if not execute:
        return f"would remove ({repo}#{issue} closed)"
    removed = _git(main, "worktree", "remove", str(path))
    if removed.returncode != 0:
        return f"reported: git refused: {removed.stderr.strip()[:160]}"
    _git(main, "worktree", "prune")
    return f"removed ({repo}#{issue} closed)"


def main_checkout_for_stem(stem: str) -> Path | None:
    """The manifest checkout whose directory name, lowercased, is the stem."""
    for project in load():
        checkout = project.checkout
        if checkout is not None and checkout.name.lower() == stem.lower():
            return checkout
    return None


def sweep_leftover(path: Path, execute: bool, now: float | None = None) -> str:
    stem = worktree_issue._IMPL_DIR_RE.sub("", path.name)
    try:
        main = main_checkout_for_stem(stem)
    except ManifestError:
        main = None
    newest = path.lstat().st_mtime
    keep: list[str] = []
    for dirpath, dirnames, filenames in os.walk(path, followlinks=False):
        here = Path(dirpath)
        newest = max(newest, here.lstat().st_mtime)
        for name in dirnames + filenames:
            entry = here / name
            newest = max(newest, entry.lstat().st_mtime)
            if name in filenames or entry.is_symlink():
                rel = str(entry.relative_to(path))
                if not regenerable(path, rel, main):
                    keep.append(rel)
    if (now if now is not None else time.time()) - newest <= LEFTOVER_MIN_AGE_SECONDS:
        return "kept: younger than 1h"
    if keep:
        return f"reported: non-regenerable: {_summary(sorted(keep))}"
    if not execute:
        return "would delete (leftover, all regenerable)"
    shutil.rmtree(path)
    return "deleted (leftover, all regenerable)"


def candidates(roots: list[Path]) -> list[Path]:
    found = []
    for root in roots:
        if not root.is_dir():
            continue
        for entry in sorted(root.iterdir()):
            if entry.is_dir() and not entry.is_symlink() and worktree_issue._IMPL_DIR_RE.search(entry.name):
                found.append(entry)
    return found


def sweep(roots: list[Path], execute: bool) -> list[str]:
    lines = []
    for path in candidates(roots):
        try:
            if (path / ".git").exists():
                result = sweep_registered(path, execute)
            else:
                result = sweep_leftover(path, execute)
        except Exception as exc:  # noqa: BLE001 -- AC2: one bad candidate never ends the sweep
            result = f"skipped: {type(exc).__name__}: {exc}"
        lines.append(f"{path}: {result}")
    return lines


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--execute", action="store_true", help="Remove; the default only reports.")
    parser.add_argument("--root", action="append", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    roots = args.root or [Path(root) for root in worktree_issue._IMPL_ROOTS]
    print(f"worktree-gc {time.strftime('%Y-%m-%dT%H:%M:%S%z')} {'execute' if args.execute else 'report-only'}")
    for line in sweep(roots, args.execute):
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
