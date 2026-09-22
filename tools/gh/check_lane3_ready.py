#!/usr/bin/env python3
"""Refuse to start a Lane 3 gate unless the target issue has a
durable AE comment followed by a gate-readiness sweep.

Before this, `lane3-begin` unlocked `gate-checkout`/`gate-restart`/`gate-e2e`
purely by touching a marker file -- it never checked GitHub for AE or a
sweep. The correctness of the AE-then-sweep ordering depended entirely on
the Lane 1 session remembering to do both in the same turn, which failed
twice in one session before this existed.

Determines which issue is in play from the most recent l1-post receipt for
the current branch (`~/.local/state/harmonic-forge/l1-post/*.json`), then
asks GitHub directly for the AE/sweep comments themselves -- the receipt is
only used to find the issue number, not trusted as evidence that AE/sweep
happened, since a receipt is local and could be stale or absent from a
later run on a different machine or a resumed clone.
"""

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

from _sweep_tier import NO_TIER_MESSAGE, parse_write_tier  # noqa: E402

FOOTER_KIND = re.compile(r"<!--\s*l1-post\s+v1;\s*kind=(\w[\w-]*)", re.I)
FOOTER_SHA = re.compile(r"<!--\s*l1-post\s+v1;.*?\bsha=([0-9a-f]{7,40})\b", re.I)
FOOTER_BODY_SHA = re.compile(r"<!--\s*l1-post\s+v1;.*?\bbody-sha256=([0-9a-f]{64})\b", re.I)
FOOTER_MARKER = re.compile(r"\n\n<!--\s*l1-post\s+v1;.*?-->\s*\Z", re.I | re.DOTALL)
RECEIPT_ROOT = Path.home() / ".local" / "state" / "harmonic-forge" / "l1-post"

def run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, text=True, capture_output=True, check=False)


def fail(message: str) -> None:
    print(f"[check-lane3-ready] {message}", file=sys.stderr)
    raise SystemExit(1)


def current_branch() -> str:
    result = run("git", "rev-parse", "--abbrev-ref", "HEAD")
    if result.returncode or result.stdout.strip() in ("", "HEAD"):
        fail("cannot resolve current branch (detached HEAD?) -- lane3-begin needs a named branch")
    return result.stdout.strip()


def current_repo() -> str:
    result = run("git", "remote", "get-url", "origin")
    if result.returncode:
        fail("cannot resolve origin remote")
    normalized = result.stdout.strip().rstrip("/").removesuffix(".git")
    match = re.search(r"github\.com[:/]([^/]+/[^/]+)$", normalized)
    if not match:
        fail(f"cannot parse a GitHub repo from origin remote {result.stdout.strip()!r}")
    return match.group(1)


def issue_for_branch(branch: str) -> int:
    if not RECEIPT_ROOT.is_dir():
        fail(
            f"no l1-post receipts found at {RECEIPT_ROOT} -- post a ready-for-l3 "
            f"claim for {branch!r} via `mise run l1-post --kind ready-for-l3` before "
            "starting a Lane 3 gate"
        )
    candidates = []
    for path in RECEIPT_ROOT.glob("*.json"):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if record.get("branch") == branch and isinstance(record.get("issue"), int):
            candidates.append(record)
    if not candidates:
        fail(
            f"no l1-post receipt names branch {branch!r} -- post a ready-for-l3 "
            "claim for this branch before starting a Lane 3 gate"
        )
    # Sort by the GitHub-assigned comment_id, not created_at's ISO string --
    # harmonic-forge#381's atomic ae-and-sweep can post two comments inside
    # the same wall-clock second, and comment_id is strictly monotonic where
    # created_at has only one-second resolution.
    candidates.sort(key=lambda r: r.get("comment_id", 0))
    return candidates[-1]["issue"]


def fetch_comments(repo: str, issue: int) -> list[dict]:
    result = run("gh", "api", f"repos/{repo}/issues/{issue}/comments", "--paginate")
    if result.returncode:
        fail(f"cannot fetch comments for {repo}#{issue}: {result.stderr.strip()}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        fail(f"unexpected response fetching comments for {repo}#{issue}")


def latest_by_kind(comments: list[dict], kind: str) -> dict | None:
    matches = []
    for comment in comments:
        match = FOOTER_KIND.search(comment.get("body", ""))
        if match and match.group(1).lower() == kind:
            matches.append(comment)
    if not matches:
        return None
    # `id`, not `created_at` -- GitHub's REST created_at has one-second
    # resolution, and harmonic-forge#381's atomic ae-and-sweep can post two
    # comments inside the same second. `id` is strictly monotonic.
    return max(matches, key=lambda c: c["id"])


def footer_sha(comment: dict) -> str | None:
    match = FOOTER_SHA.search(comment.get("body", ""))
    return match.group(1) if match else None


def verify_body_sha256(comment: dict) -> bool:
    """Confirm the fetched comment body hasn't been edited
    since it was posted -- comments are mutable, and this is the only check
    standing between an edited sweep and a forged tier-R authorization once
    the sweep itself is the authority. Strips the reserved attestation
    footer, matching exactly what `l1_post.py` hashed before appending it
    (the rstripped pre-footer body). Returns True if
    there is nothing to verify against (no body-sha256 marker) -- absence
    is a missing-marker problem the caller already checks for separately,
    not a mismatch."""
    recorded = FOOTER_BODY_SHA.search(comment.get("body", ""))
    if recorded is None:
        return True
    prefix = FOOTER_MARKER.sub("", comment.get("body", ""))
    digest = hashlib.sha256(prefix.rstrip("\n").encode()).hexdigest()
    return digest == recorded.group(1)


def current_head_sha() -> str:
    result = run("git", "rev-parse", "HEAD")
    if result.returncode:
        fail("cannot resolve checked-out HEAD")
    return result.stdout.strip()


def carry_forward(comments: list[dict], authority: dict, head_sha: str) -> dict | None:
    """A Lane 1 `ready-for-l3` posted after the authorizing
    comment, naming the commit actually being gated, extends that
    authorization to a new SHA without requiring a fresh one for the
    routine fix-and-repush cycle.

    Generalized from `ae`-only to any authorizing comment --
    parameter rename only, same body (it only ever read `authority["id"]`,
    nothing AE-specific) -- so the identical staleness protection applies
    when a tier-R sweep is the authority instead of an AE."""
    candidates = [
        comment for comment in comments
        if comment["id"] > authority["id"]
        and (match := FOOTER_KIND.search(comment.get("body", "")))
        and match.group(1).lower() == "ready-for-l3"
        and footer_sha(comment) == head_sha
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda c: c["id"])


#: TODO(harmonic-forge#721): adapter data. This message names one consuming
#: repo's disposable-graph container, ports and mise task; #721 sources it from
#: that repo's gate-adapter manifest instead of carrying it in platform code.
#:
#: Printed on every successful readiness check, which is the
#: last thing a Lane 3 session runs before a gate — so this reaches the
#: session at the moment it decides what a spec needs, rather than waiting to
#: be looked up.
#:
#: `mise.toml` carried the harness for three weeks and nobody found it, which
#: is the whole finding: being present somewhere is not discoverability. The
#: skill file now says Tier W is available too; this is the push half of that
#: pair, because a document is only read by someone who already suspects it
#: has the answer.
TIER_W_AVAILABILITY = (
    "[check-lane3-ready] Tier W is AVAILABLE: a gate that must "
    "mutate pre-existing state runs against a disposable restored copy, not "
    "production --\n"
    "    mise run gate-disposable-graph load | status | teardown\n"
    "    container hrse-graph-w, bolt 27687 / http 27474, ~13s for a real dump.\n"
    "  Point the gate's NEO4J_URI at 27687; the integration-test guard reads that shape "
    "as authorised. Never write to production instead."
)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--issue", type=int, default=None,
        help=(
            "Validate this issue number directly instead of deriving it from "
            "the current branch's l1-post receipt. Required when the worktree "
            "is on a stale/unrelated branch (a prior gate's) or is already "
            "detached (gate-checkout's normal fallback whenever "
            "Lane 2 still holds the target branch leaves no named branch for "
            "issue_for_branch() to resolve at all)."
        ),
    )
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])

    repo = current_repo()
    if args.issue is not None:
        issue = args.issue
    else:
        issue = issue_for_branch(current_branch())
    comments = fetch_comments(repo, issue)

    sweep = latest_by_kind(comments, "sweep")
    if sweep is None:
        fail(
            f"{repo}#{issue} has no gate-readiness sweep -- post one via "
            "`mise run l1-post --kind sweep --spec-comment <id>` before starting "
            "a Lane 3 gate"
        )

    tier = parse_write_tier(sweep.get("body", ""))
    if tier is None:
        fail(f"{repo}#{issue}'s sweep ({sweep['html_url']}) {NO_TIER_MESSAGE}")

    ae = latest_by_kind(comments, "ae")
    head_sha = current_head_sha()

    # An AE means HITL consent to execute, valid at any tier --
    # check for one first, exactly as before, untouched. Only
    # when none exists does tier R get its own fallback authorization path;
    # above tier R, no AE is still a hard failure.
    if ae is not None:
        if sweep["id"] <= ae["id"]:
            fail(
                f"{repo}#{issue} has no gate-readiness sweep posted after its most "
                f"recent AE ({ae['html_url']}) -- post one via "
                "`mise run l1-post --kind sweep --spec-comment <id>` before starting "
                "a Lane 3 gate"
            )
        ae_sha = footer_sha(ae)
        authority = ae
        if ae_sha is None:
            fail(f"{repo}#{issue}'s AE comment ({ae['html_url']}) has no parseable sha= marker")
        elif ae_sha != head_sha:
            carry = carry_forward(comments, ae, head_sha)
            if carry is None:
                fail(
                    f"{repo}#{issue}'s AE ({ae['html_url']}) authorizes sha={ae_sha}, but "
                    f"the checked-out HEAD is {head_sha} -- post a Lane 1 ready-for-l3 "
                    f"naming {head_sha} (`mise run l1-post --kind ready-for-l3`), or a "
                    "fresh AE at this commit, before starting a Lane 3 gate"
                )
            authority = carry
    elif tier == "R":
        if not verify_body_sha256(sweep):
            fail(
                f"{repo}#{issue}'s sweep ({sweep['html_url']}) body does not match its "
                "recorded body-sha256 -- it may have been edited since posting; a "
                "tier-R gate cannot start on an unverifiable authorization anchor"
            )
        sweep_sha = footer_sha(sweep)
        authority = sweep
        if sweep_sha is None:
            fail(f"{repo}#{issue}'s sweep ({sweep['html_url']}) has no parseable sha= marker")
        elif sweep_sha != head_sha:
            carry = carry_forward(comments, sweep, head_sha)
            if carry is None:
                fail(
                    f"{repo}#{issue}'s sweep ({sweep['html_url']}) authorizes "
                    f"sha={sweep_sha}, but the checked-out HEAD is {head_sha} -- post "
                    f"a Lane 1 ready-for-l3 naming {head_sha} (`mise run l1-post "
                    "--kind ready-for-l3`), or a fresh sweep at this commit, before "
                    "starting a Lane 3 gate"
                )
            authority = carry
    else:
        fail(
            f"{repo}#{issue} has no AE comment and its sweep declares tier {tier} -- "
            "an AE is required above tier R; post one via `mise run l1-post --kind ae` "
            "before starting a Lane 3 gate"
        )

    print(
        f"[check-lane3-ready] {repo}#{issue}: sweep ({sweep['html_url']}), tier {tier} "
        f"-- ready, authorized for {head_sha} by {authority['html_url']}"
    )
    print(TIER_W_AVAILABILITY)


if __name__ == "__main__":
    main()
