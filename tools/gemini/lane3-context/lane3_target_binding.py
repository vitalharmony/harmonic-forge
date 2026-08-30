#!/usr/bin/env python3
"""Trusted-provider and immutable-Git binding for Gemini Lane 3 (F326)."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

MAX_FILE_BYTES = 200_000


@dataclass(frozen=True)
class AttestedTarget:
    repository: str
    sha: str
    worktree: Path


def _run(*args: str, cwd: Path | None = None) -> str:
    result = subprocess.run(args, cwd=cwd, text=True, errors="replace", capture_output=True)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or f"{' '.join(args[:2])} failed")
    return result.stdout


def filtered_context(provider: Path, repo: str, issue: str, worktree: Path) -> str:
    return _run(
        "python3", str(provider), "--repo", repo, "--issue", issue,
        cwd=worktree,
    )


def named_comment(
    provider: Path, repo: str, issue: str, comment_id: int, worktree: Path,
) -> str:
    return _run(
        "python3", str(provider), "--repo", repo, "--issue", issue,
        "--comment-id", str(comment_id), cwd=worktree,
    )


def resolve_target(provider: Path, repo: str, issue: str, worktree: Path) -> AttestedTarget:
    raw = _run(
        "python3", str(provider), "--repo", repo, "--issue", issue,
        "--target-metadata", cwd=worktree,
    )
    try:
        metadata = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError("filtered-context provider returned invalid target metadata") from exc
    if not isinstance(metadata, dict) or set(metadata) != {"repository", "sha"}:
        raise RuntimeError("filtered-context provider returned widened target metadata")
    if metadata["repository"] != repo:
        raise RuntimeError("filtered-context provider returned the wrong canonical repository")
    sha = metadata["sha"]
    if not isinstance(sha, str) or len(sha) != 40 or any(c not in "0123456789abcdef" for c in sha):
        raise RuntimeError("filtered-context provider returned an invalid immutable SHA")
    _run("git", "cat-file", "-e", f"{sha}^{{commit}}", cwd=worktree)
    return AttestedTarget(repo, sha, worktree)


def _relative_path(value: object) -> str:
    if not isinstance(value, str) or not value or "\\" in value or any(ord(c) < 32 for c in value):
        raise RuntimeError("path must be a non-empty relative Git path without traversal")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or str(path) != value or ":" in value:
        raise RuntimeError("path must be a non-empty relative Git path without traversal")
    return value


def read_file(target: AttestedTarget, value: object) -> str:
    path = _relative_path(value)
    spec = f"{target.sha}:{path}"
    if _run("git", "cat-file", "-t", spec, cwd=target.worktree).strip() != "blob":
        raise RuntimeError("file is unavailable at the attested target")
    size = int(_run("git", "cat-file", "-s", spec, cwd=target.worktree).strip())
    if size > MAX_FILE_BYTES:
        raise RuntimeError("file exceeds the bounded read limit")
    return _run("git", "show", spec, cwd=target.worktree)


def _relative_pattern(value: object) -> str:
    if not isinstance(value, str) or not value or "\\" in value or any(ord(c) < 32 for c in value):
        raise RuntimeError("pattern must be relative and traversal-free")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or ":" in value:
        raise RuntimeError("pattern must be relative and traversal-free")
    return value


def list_files(target: AttestedTarget, pattern: object) -> list[str]:
    glob = _relative_pattern(pattern)
    names = _run(
        "git", "ls-tree", "-r", "--name-only", target.sha, cwd=target.worktree,
    ).splitlines()
    return [
        name for name in names
        if PurePosixPath(name).match(glob)
        or (glob.startswith("**/") and PurePosixPath(name).match(glob[3:]))
    ][:200]


def search_text(
    target: AttestedTarget, query: object, pattern: object = "**/*",
) -> str:
    if not isinstance(query, str) or not query or len(query) > 500:
        raise RuntimeError("query must be non-empty text of at most 500 characters")
    rows: list[str] = []
    for path in list_files(target, pattern):
        spec = f"{target.sha}:{path}"
        if _run("git", "cat-file", "-t", spec, cwd=target.worktree).strip() != "blob":
            continue
        size = int(_run("git", "cat-file", "-s", spec, cwd=target.worktree).strip())
        if size > MAX_FILE_BYTES:
            continue
        for line_no, line in enumerate(_run("git", "show", spec, cwd=target.worktree).splitlines(), 1):
            if query in line:
                rows.append(f"{path}:{line_no}:{line}")
                if len(rows) >= 200:
                    return "\n".join(rows)
    return "\n".join(rows)


def diff_from_main(target: AttestedTarget) -> str:
    return _run(
        "git", "diff", f"origin/main...{target.sha}", cwd=target.worktree,
    )
