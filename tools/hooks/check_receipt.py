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
import tempfile
from pathlib import Path

STORE = Path.home() / ".cache" / "harmonic-forge" / "check-receipts"
#: harmonic-forge#1134: `l1_post` sets this on its own scratch `check`, so only
#: that run's receipt is reusable; a lane worktree's check is another environment.
SOURCE_ENV = "HF_CHECK_RECEIPT_SOURCE"
L1_POST_SOURCE = "l1-post-scratch"
#: The forge HEAD `l1_post` read before its check started (empty when the forge
#: tree was dirty): the receipt names that, never the HEAD at record time.
FORGE_SHA_ENV = "HF_CHECK_RECEIPT_FORGE_SHA"
#: harmonic-forge#1566 (amended, operator D25/D26): the forge identity is the
#: checkout this file lives in -- the recorder and its reader (l1_post) are both
#: forge scripts, so that checkout is the forge code that ran. When
#: HARMONIC_FORGE_ROOT or instance.toml `forge_root` declares a different real
#: checkout, the identity is None ("forge root mismatch"): a wrapper may then
#: have run forge from the other one, and a mismatch must miss a reuse, never
#: allow a false one. A forge-repo check recorded from its scratch checkout
#: therefore records no identity and is re-run (D26).
FORGE_ROOT = Path(__file__).resolve().parents[2]


def declared_forge_root() -> Path | None:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import hook_forge_root  # noqa: PLC0415
    return hook_forge_root.resolve_declared()


def _root_mismatch() -> str | None:
    declared = declared_forge_root()
    if declared is None:
        return None
    try:
        same = Path(declared).resolve() == Path(FORGE_ROOT).resolve()
    except OSError:
        same = False
    return None if same else f"forge root mismatch: declared {declared}, running {FORGE_ROOT}"
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


def check_full_path(slug: str, sha: str, forge: str) -> Path:
    """harmonic-forge#1565: keyed by forge SHA too, so no later run replaces it."""
    return STORE / slug / f"{sha}.check-full.{forge}"


def forge_sha() -> str | None:
    """The HEAD of the forge checkout a check ran against: a consumer repo's
    check runs forge scripts, so a forge change can change its result. None when
    it cannot be read, the forge tree is dirty (HEAD does not name the code), or
    a declared forge root is a different checkout (harmonic-forge#1566)."""
    # harmonic-forge#1538: untracked files are not code a check ran, so they do
    # not void the identity; a modified or staged tracked file still does.
    if _root_mismatch():
        return None
    try:
        if _git(*_FORGE_STATUS, cwd=str(FORGE_ROOT)):
            return None
        return _git("rev-parse", "HEAD", cwd=str(FORGE_ROOT))
    except (OSError, subprocess.CalledProcessError):
        return None


_FORGE_STATUS = ("status", "--porcelain", "--untracked-files=no")


def forge_unusable_reason() -> str | None:
    """Why no receipt written now could be reused (harmonic-forge#1538), or None
    when the forge identity is readable: `forge tree dirty: <path>` or
    `forge HEAD unreadable`."""
    if forge_sha():
        return None
    if (mismatch := _root_mismatch()) is not None:
        return mismatch
    try:
        lines = _git(*_FORGE_STATUS, cwd=str(FORGE_ROOT)).splitlines()
    except (OSError, subprocess.CalledProcessError):
        lines = []
    return f"forge tree dirty: {lines[0].split(maxsplit=1)[-1]}" if lines else "forge HEAD unreadable"


def _recorded_forge_sha() -> str | None:
    """The value read before the check (`FORGE_SHA_ENV`) if forge is still there
    now, else HEAD now (the slot's #1134 fallback; a sidecar never takes it). An
    edit made and reverted inside one run (ABA) is not seen (#1134 Out of scope)."""
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


#: harmonic-forge#1343: the machine-local subset of `check`, recorded so pre-push
#: needs only what CI cannot run. It never replaces another source's receipt; a
#: full `check` replaces it (the upgrade path).
LOCAL_CHECK_SOURCE = "local-check"
#: harmonic-forge#1586: a full `check-full` run (vitalharmony/hrse#2889), full-replace, own
#: label. harmonic-forge#1565: also a sidecar only it writes (`check_full_path`), for ci_outage.
CHECK_FULL_SOURCE = "check-full"
SOURCES = ("check", LOCAL_CHECK_SOURCE, CHECK_FULL_SOURCE)
#: harmonic-forge#894 C1: a `local-check` run inside l1_post's scratch worktree.
#: It keeps local-check's no-replace rule, so it never displaces a full
#: `l1-post-scratch` receipt.
L1_POST_LOCAL_SOURCE = "l1-post-scratch-local"
#: The sources that never replace an existing receipt for the same SHA.
NO_REPLACE_SOURCES = (LOCAL_CHECK_SOURCE, L1_POST_LOCAL_SOURCE)


def record(repo_root: str = ".", head: str | None = None, source: str = "check") -> int:
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
        # harmonic-forge#1343 preclose pass 1: the override relabels only a full
        # check; a local-check run never becomes a source l1_post reuses.
        if os.environ.get(SOURCE_ENV) == L1_POST_SOURCE:
            if source == "check":
                source = L1_POST_SOURCE
            elif source == LOCAL_CHECK_SOURCE:  # harmonic-forge#894 C1
                source = L1_POST_LOCAL_SOURCE
        existing = lookup(slug, sha)
        if source in NO_REPLACE_SOURCES and existing is not None:
            print(f"check-receipt: kept the existing {existing.get('source')} receipt")
            return 0
        forge = _recorded_forge_sha()
        wanted = os.environ.get(FORGE_SHA_ENV)
        if wanted and forge != wanted:
            # harmonic-forge#1566 (D26): say why this receipt will not be reused,
            # e.g. a forge-repo check recorded from its scratch checkout.
            why = forge_unusable_reason() or f"running forge is at {forge_sha() or 'unknown'}"
            print(f"check-receipt: forge identity {(forge or 'none')[:12]} differs from "
                  f"l1_post's {wanted[:12]} ({why}); this receipt will not be reused",
                  file=sys.stderr)
        data = json.dumps({"repo": slug, "sha": sha, "forge_sha": forge, "source": source}) + "\n"
        path.write_text(data, encoding="utf-8")
        if source == CHECK_FULL_SOURCE:
            _write_check_full(slug, sha, forge, data)
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"check-receipt: WARNING no receipt recorded ({exc})", file=sys.stderr)
        return 0
    print(f"check-receipt: recorded {slug} {sha[:12]}")
    return 0


def _write_check_full(slug: str, sha: str, forge: str | None, data: str) -> None:
    """harmonic-forge#1565: atomic; only for the non-empty forge HEAD captured at check start (P5)."""
    why = ((None if forge else forge_unusable_reason() or "forge moved") if os.environ.get(FORGE_SHA_ENV)
           else f"{FORGE_SHA_ENV} (the forge HEAD at check start) is unset")
    if why:
        print(f"check-receipt: {why}; no check-full sidecar recorded", file=sys.stderr)
        return
    target = check_full_path(slug, sha, forge)
    handle = tempfile.NamedTemporaryFile("w", dir=target.parent, suffix=".tmp", delete=False, encoding="utf-8")
    try:
        with handle: handle.write(data)  # noqa: E701
        os.replace(handle.name, target)
    except BaseException:
        Path(handle.name).unlink(missing_ok=True)
        raise


def check_full(slug: str, sha: str, forge: str | None) -> dict | None:
    """The (sha, forge) sidecar or None: one path read, both forge values non-empty."""
    try:
        data = json.loads(check_full_path(slug, sha, forge).read_text(encoding="utf-8")) if forge else None
    except (OSError, ValueError):
        return None
    good = isinstance(data, dict) and data.get("source") == CHECK_FULL_SOURCE and data.get("sha") == sha
    return data if good and data.get("forge_sha") and data["forge_sha"] == forge else None


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
        source = argv[argv.index("--source") + 1] if "--source" in argv else "check"
        if source not in SOURCES:
            print(f"check-receipt: --source must be one of {', '.join(SOURCES)}", file=sys.stderr)
            return 2
        return record(root, head, source)
    if len(argv) >= 2 and argv[1] == "verify":
        remote_url = argv[3] if len(argv) > 3 else ""
        try:
            return verify(remote_url, sys.stdin.read().splitlines())
        except Exception as exc:  # fail open: only verify's own refusal blocks a push
            print(f"pre-push: WARNING the gate could not decide ({exc!r}); pushing unchecked",
                  file=sys.stderr)
            return 0
    print("usage: check_receipt.py record [--repo-root DIR] [--head SHA] [--source check|local-check|check-full]"
          " | verify <remote> <url>", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
