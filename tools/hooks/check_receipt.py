#!/usr/bin/env python3
"""Pre-push gate for Tooling Exception branches (harmonic-forge#975).

`record` runs as the last step of a passing `mise run check` and writes a
receipt for HEAD's commit. `verify` runs from git's `pre-push` hook and refuses
to push a branch whose issue carries `tooling-exception` unless a receipt exists
for the pushed commit (operator ruling C: Lane 1's own implementation branches
only; lane branches are already gated by `l1-post`'s check).

Receipts live in a per-user store keyed by repo and commit SHA, so a receipt
written in any worktree vouches for exactly that commit. `record` refuses a dirty
tree (untracked files included) and a HEAD that moved since `check` started
(`--head`, captured at its start), so a receipt never covers untested work.
`record` never fails a passing `check`: anything it cannot decide skips the
receipt with a warning.

The issue is read from the remote ref being updated (`HEAD:fix/975-x` is
gated like `fix/975-x`), and its repo is the push remote's. Known gap
(operator ruling, F975): a branch in one repo named for another repo's issue
is looked up in the wrong repo; branch names carry no repo.

This is a mistake-detector, not a boundary: if the label lookup fails (no
network, or `gh` refuses), the hook warns and lets the push through. The one
bypass is `git push --no-verify`, which the operator authorizes.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

STORE = Path.home() / ".cache" / "harmonic-forge" / "check-receipts"
#: harmonic-forge#1134: `l1_post` sets this on its own scratch `check`, so only
#: that run's receipt is reusable; a lane worktree's check is another environment.
SOURCE_ENV = "HF_CHECK_RECEIPT_SOURCE"
L1_POST_SOURCE = "l1-post-scratch"
#: The forge HEAD `l1_post` read before its check started (empty when the forge
#: tree was dirty): the receipt names that, never the HEAD at record time.
FORGE_SHA_ENV = "HF_CHECK_RECEIPT_FORGE_SHA"
FORGE_ROOT = Path.home() / "harmonic-forge"
BRANCH_ISSUE = re.compile(r"^[a-z]+/(\d+)-")
ZERO = "0" * 40
_SLUG = re.compile(r"github\.com(?::\d+)?[:/]+([^/]+)/([^/]+?)(?:\.git)?/?$", re.IGNORECASE)


def repo_slug(url: str) -> str | None:
    """`owner__repo` for an https, ssh or scp-form GitHub URL; None otherwise."""
    match = _SLUG.search(url.strip())
    # GitHub owner and repo names are case-insensitive.
    return f"{match.group(1)}__{match.group(2)}".lower() if match else None


def _git(*args: str, cwd: str | None = None) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True,
                          capture_output=True, text=True).stdout.strip()


def receipt_path(slug: str, sha: str) -> Path:
    return STORE / slug / sha


def forge_sha() -> str | None:
    """The `~/harmonic-forge` HEAD a check ran against: a consumer repo's check
    runs forge scripts, so a forge change can change its result. None when it
    cannot be read or the forge tree is dirty: then HEAD does not name the code."""
    try:
        if _git("status", "--porcelain", cwd=str(FORGE_ROOT)):
            return None
        return _git("rev-parse", "HEAD", cwd=str(FORGE_ROOT))
    except (OSError, subprocess.CalledProcessError):
        return None


def _recorded_forge_sha() -> str | None:
    """The forge identity to write: the value read before the check when
    `l1_post` passed one, and only if forge is still there now; else HEAD now.
    It brackets the check, so a forge edit made and reverted inside one run (ABA)
    is not seen; a fresh check has the same exposure (#1134 Out of scope)."""
    if FORGE_SHA_ENV not in os.environ:
        return forge_sha()
    before = os.environ[FORGE_SHA_ENV] or None
    return before if before and forge_sha() == before else None


def lookup(slug: str, sha: str) -> dict | None:
    """The receipt's JSON, or None when it is missing, unparseable or an empty
    receipt written before harmonic-forge#1134."""
    try:
        data = json.loads(receipt_path(slug, sha).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def record(repo_root: str = ".", head: str | None = None) -> int:
    try:
        if _git("status", "--porcelain", cwd=repo_root):
            print("check-receipt: tree is not clean; no receipt recorded", file=sys.stderr)
            return 0
        sha = _git("rev-parse", "HEAD", cwd=repo_root)
        if head is not None and sha != head:
            print("check-receipt: HEAD moved during check; no receipt recorded", file=sys.stderr)
            return 0
        slug = repo_slug(_git("remote", "get-url", "origin", cwd=repo_root))
        if slug is None:
            print("check-receipt: origin is not a GitHub URL; no receipt recorded", file=sys.stderr)
            return 0
        path = receipt_path(slug, sha)
        path.parent.mkdir(parents=True, exist_ok=True)
        source = L1_POST_SOURCE if os.environ.get(SOURCE_ENV) == L1_POST_SOURCE else "check"
        path.write_text(json.dumps({"repo": slug, "sha": sha, "forge_sha": _recorded_forge_sha(),
                                    "source": source}) + "\n", encoding="utf-8")
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"check-receipt: WARNING no receipt recorded ({exc})", file=sys.stderr)
        return 0
    print(f"check-receipt: recorded {slug} {sha[:12]}")
    return 0


def labels(slug: str, issue: str) -> list[str] | None:
    """The issue's labels, or None if they cannot be read."""
    owner, repo = slug.split("__", 1)
    try:
        out = subprocess.run(["gh", "api", f"repos/{owner}/{repo}/issues/{issue}",
                              "--jq", "[.labels[].name]"],
                             capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    try:
        return json.loads(out.stdout)
    except ValueError:
        return None


def push_refusal(slug: str, branch: str, sha: str,
                 cache: dict[str, list[str] | None] | None = None) -> str | None:
    """Why pushing `sha` to `branch` of `slug` is refused, or None to allow.

    harmonic-forge#1135 AC1: the one answer pre-push and the PreToolUse hook
    (`block_unchecked_push.py`) share. A TE branch with no check receipt for
    `sha` is refused; anything else, including an unreadable label, allows.
    """
    cache = {} if cache is None else cache
    match = BRANCH_ISSUE.match(branch)
    if not match:
        return None
    issue = match.group(1)
    if issue not in cache:
        cache[issue] = labels(slug, issue)
    found = cache[issue]
    if found is None:
        print(f"pre-push: WARNING could not read the labels of issue {issue}; "
              f"{branch} is pushed unchecked (mistake-detector, fails open)", file=sys.stderr)
        return None
    if "tooling-exception" not in found:
        return None
    if receipt_path(slug, sha).exists():
        return None
    return (f"{branch}@{sha[:12]} is a Tooling Exception branch with no "
            "passing 'mise run check' receipt; run it, then push")


def _refuses(slug: str, line: str, cache: dict[str, list[str] | None]) -> bool:
    """True when this pre-push line is a TE branch with no receipt for its SHA."""
    parts = line.split()
    if len(parts) != 4:
        return False
    _local_ref, local_sha, remote_ref, _remote_sha = parts
    if local_sha == ZERO:
        return False  # a branch deletion
    reason = push_refusal(slug, remote_ref.removeprefix("refs/heads/"), local_sha, cache)
    if reason is None:
        return False
    print(f"pre-push: {reason}", file=sys.stderr)
    return True


def verify(remote_url: str, lines: list[str]) -> int:
    slug = repo_slug(remote_url)
    if slug is None:
        return 0
    cache: dict[str, list[str] | None] = {}
    refused = False
    for line in lines:
        try:
            refused = _refuses(slug, line, cache) or refused
        except Exception as exc:  # fail open per ref, so one ref never erases another's refusal
            print(f"pre-push: WARNING could not check {line.split()[2:3]} ({exc!r}); "
                  "pushed unchecked (mistake-detector, fails open)", file=sys.stderr)
    return 1 if refused else 0


def main(argv: list[str]) -> int:
    if len(argv) >= 2 and argv[1] == "record":
        root = argv[argv.index("--repo-root") + 1] if "--repo-root" in argv else "."
        head = argv[argv.index("--head") + 1] if "--head" in argv else None
        return record(root, head)
    if len(argv) >= 2 and argv[1] == "verify":
        remote_url = argv[3] if len(argv) > 3 else ""
        try:
            return verify(remote_url, sys.stdin.read().splitlines())
        except Exception as exc:  # fail open: only verify's own refusal blocks a push
            print(f"pre-push: WARNING the gate could not decide ({exc!r}); pushing unchecked",
                  file=sys.stderr)
            return 0
    print("usage: check_receipt.py record [--repo-root DIR] [--head SHA] | verify <remote> <url>", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
