"""Which issue a lane worktree is building (harmonic-forge#843 reforge).

The downshift reminder's one resolver for its two "deep issue in hand" probes.
It owns its git reads rather than reusing the belt's `watch_lane_posts`
resolver, whose `_run_git` neither checks the exit code nor sets a timeout, so a
git failure there read as "this worktree builds nothing" (the original false
"switch down").

**Every git call goes through `git_lines`**: timed, `None` for a directory that
is not in a git tree at all (a true negative), and an exception for a nonzero
exit inside one, so an undecidable answer suppresses the reminder (AC4).

**The branch is authoritative** for repo and issue. The prefix grammar is built
from `manifest.prefix_repos()` at call time, never copied as a literal
character class (F605, `tools/onboard/manifest.py:259-268`): a copied class is
how onboarding a repo once broke the belt. The repo is the prefix's, else the
worktree's own `origin` remote. **Detached HEAD only** falls back to the issue
number in the `<stem>-<N>-impl` directory name, with the repo from `origin`.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

HOOKS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(HOOKS_DIR))
import model_tier_gate  # noqa: E402

GIT_TIMEOUT_SECONDS = 1.5
# `<stem>-<N>-impl`, or `<stem>-<N><letter>-impl` for a second worktree on the
# same issue (e.g. a reforge); the branch still decides repo and issue.
_IMPL_DIR_RE = re.compile(r"-(?P<number>\d+)[a-z]?-impl$")
_REMOTE_REPO_RE = re.compile(r"github\.com[:/](?P<repo>[\w.-]+/[\w.-]+?)(?:\.git)?$")
_IMPL_ROOTS = (
    str(Path.home() / "Harmonic_Projects" / ".worktrees"),
    "/tmp",
)


def in_git_tree(directory: str) -> bool:
    """A `.git` entry at or above `directory` (no git call): what tells "not a
    repository" (a true negative) apart from a git failure."""
    path = Path(directory).resolve()
    return any((candidate / ".git").exists() for candidate in (path, *path.parents))


def git_lines(directory: str, *args: str) -> list[str] | None:
    """stdout lines of a timed git call; None outside any git tree. A nonzero
    exit inside a tree raises (AC4): it is undecidable, never "nothing"."""
    if not in_git_tree(directory):
        return None
    result = model_tier_gate.timed_run(["git", "-C", directory, *args],
                                       timeout=GIT_TIMEOUT_SECONDS)
    if result.returncode != 0:
        raise RuntimeError(f"git {args[0]} failed inside a git tree")
    return result.stdout.splitlines()


def _manifest():
    onboard = str(HOOKS_DIR.parent / "onboard")
    if onboard not in sys.path:
        sys.path.insert(0, onboard)
    import manifest  # noqa: PLC0415 -- may raise; callers fail quiet

    return manifest


def prefix_repos() -> dict[str, str]:
    return _manifest().prefix_repos()


def repo_accounts() -> dict[str, str | None]:
    """`{owner/name lowercased: account}` from the manifest, for partitioning
    a batched read by the credentials that can see each repo."""
    return {p.repo.lower(): p.account for p in _manifest().load() if p.repo}


def _branch_issue_re(prefixes: dict[str, str]) -> re.Pattern:
    letters = "".join(sorted({c for k in prefixes for c in (k.lower(), k.upper())}))
    prefix = f"(?P<prefix>[{letters}])?" if letters else "(?P<prefix>)"
    return re.compile(r"(?:^|/)" + prefix + r"(?P<num>\d{2,6})(?=[-/]|$)")


def _origin_repo(path: str) -> str | None:
    lines = git_lines(path, "remote", "get-url", "origin")
    match = _REMOTE_REPO_RE.search(lines[0].strip()) if lines else None
    return match.group("repo").lower() if match else None


def is_impl_worktree(path: str) -> bool:
    """AC2's filter only: a `<stem>-<N>-impl` directory directly under one of
    the implementation-worktree roots. It never decides repo or issue."""
    parent, name = str(Path(path).parent), Path(path).name
    roots = set(_IMPL_ROOTS) | {str(Path(root).resolve()) for root in _IMPL_ROOTS}
    return parent in roots and bool(_IMPL_DIR_RE.search(name))


def issue_for_worktree(path: str) -> tuple[str, int] | None:
    """`(repo, issue)` the worktree at `path` is building, or None.

    Raises on a git failure inside the worktree (callers fail quiet)."""
    if not in_git_tree(path):
        return None
    head = git_lines(path, "rev-parse", "--abbrev-ref", "HEAD") or [""]
    branch = head[0].strip()
    if branch and branch != "HEAD":
        prefixes = prefix_repos()
        match = _branch_issue_re(prefixes).search(branch)
        if not match:
            return None
        prefix = (match.group("prefix") or "").lower()
        repo = prefixes[prefix].lower() if prefix else _origin_repo(path)
        return (repo, int(match.group("num"))) if repo else None
    numbered = _IMPL_DIR_RE.search(Path(path).name)
    repo = _origin_repo(path) if numbered else None
    return (repo, int(numbered.group("number"))) if repo else None
