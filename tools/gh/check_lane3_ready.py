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

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "onboard"))
from manifest_identity import apply_project_identity  # noqa: E402

from _handoff_footer import (  # noqa: E402
    UNREADABLE_HANDOFF, FooterState, attested_footer, newest_handoff_mutates_live,
)
from _sweep_tier import NO_TIER_MESSAGE, TIER_RANK, parse_write_tier  # noqa: E402

FOOTER_KIND = re.compile(r"<!--\s*l1-post\s+v1;\s*kind=(\w[\w-]*)", re.I)
#: harmonic-forge#791: a spec/handoff is recognized by its OWN heading too,
#: not only its `kind=` footer -- `require_green_ci`'s docstring records that
#: 74 of 98 real gate reports carry no matching kind footer, and the same
#: mis-kinding is reachable here (`mise run lane-comment` with no `--kind`
#: stamps `discussion` regardless of what the body says). Headings mirrored
#: from `post_lane_discussion.KIND_HEADING`, not re-derived.
ROUND_HEADING = re.compile(r"(?im)^#{1,4}[ \t]*(?:Lane 3 Test Spec|Handoff)\b")
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


def _own_kind(comment: dict) -> str | None:
    """The comment's own kind, from its own footer only (harmonic-forge#851
    REFORGE): an ATTESTED footer, or a legacy digest-less one (ABSENT). An
    UNREADABLE footer has no trustworthy kind, and a quoted marker never
    nominates the comment it is quoted in."""
    footer = attested_footer(comment.get("body", ""))
    if footer.state is FooterState.UNREADABLE or footer.kind is None:
        return None
    return footer.kind.lower()


def _attested_text(comment: dict) -> str:
    """The text a consumer may read content from: the attested prefix when
    the footer attests, else the whole (legacy) body."""
    footer = attested_footer(comment.get("body", ""))
    return footer.prefix if footer.state is FooterState.ATTESTED else comment.get("body", "")


def unreadable_after(comments: list[dict], kind: str, after: dict | None) -> dict | None:
    """The newest comment newer than `after` that claims `kind` but whose own
    footer does not attest -- edited, forged, or hand-posted with a quote. A
    caller refuses on it instead of falling back to an older, valid one
    (harmonic-forge#851 preclose pass 2: never skip an unreadable candidate)."""
    bad = [comment for comment in comments
           if (after is None or comment["id"] > after["id"])
           and (footer := attested_footer(comment.get("body", ""))).state is FooterState.UNREADABLE
           and kind in {k.lower() for k in footer.claimed_kinds}]
    return max(bad, key=lambda c: c["id"]) if bad else None


def _unreadable_message(kind: str, comment: dict) -> str:
    return (f"{kind} ({comment['html_url']}) body does not match its recorded body-sha256 -- it "
            f"may have been edited since posting, or it quotes another comment's footer; an "
            f"unverifiable {kind} is never read past to an older one. Re-post it.")


def latest_by_kind(comments: list[dict], kind: str) -> dict | None:
    matches = [comment for comment in comments if _own_kind(comment) == kind]
    if not matches:
        return None
    # `id`, not `created_at` -- GitHub's REST created_at has one-second
    # resolution, and harmonic-forge#381's atomic ae-and-sweep can post two
    # comments inside the same second. `id` is strictly monotonic.
    return max(matches, key=lambda c: c["id"])


def is_auto_ae(comment: dict) -> bool:
    """harmonic-forge#851: an AE `l1_post.py --auto-ae` posted. Read from the
    body's own trailing attestation footer only, so a marker quoted in the
    body never counts."""
    footer = attested_footer(comment.get("body", ""))
    return footer.state is FooterState.ATTESTED and "authorized-by=auto-ae" in (footer.marker or "")


def auto_ae_refusal(comments: list[dict], ae: dict, tier: str) -> str | None:
    """The consumer half of the fixed auto-AE carve-out (harmonic-forge#851).

    `l1_post.py --auto-ae` refuses these cases at the poster, but that is the
    tool Lane 1 itself invokes with Lane-1-authored inputs. This is the half
    that holds when the poster is bypassed: an auto-AE never authorizes a gate
    above Tier W, or on an issue whose newest handoff is live-mutating or
    predates the `mutates-live` field. A manual AE never reaches here."""
    manual = "the operator's manual AE is required (post it without --auto-ae)"
    # harmonic-forge#851 preclose pass 1 survivor 2: the same ceiling the poster
    # computes -- the sweep AND the newest spec -- so a spec revised to add a
    # Tier P case after the auto-AE cannot ride carry-forward past this guard.
    spec = latest_by_kind(comments, "spec")
    if (bad := unreadable_after(comments, "spec", spec)) is not None:
        return _unreadable_message("spec", bad)
    spec_tier = parse_write_tier(_attested_text(spec)) if spec else None
    if spec_tier is None:
        return (f"AE ({ae['html_url']}) was posted under the auto-AE toggle, but the newest "
                f"Lane 3 spec declares no write tier (or none was found); auto-AE never "
                f"reads silence as Tier R -- {manual}")
    ceiling = max((tier, spec_tier), key=TIER_RANK.__getitem__)
    if ceiling not in ("R", "W"):
        return (f"AE ({ae['html_url']}) was posted under the auto-AE toggle, but the write-tier "
                f"ceiling over the sweep and the newest spec is {ceiling}; an auto-AE "
                f"authorizes Tier R or W only -- {manual}")
    mutates_live = newest_handoff_mutates_live(c.get("body", "") for c in comments)
    if mutates_live is not False:
        why = ("does not match its body-sha256 (edited or quoted), so it cannot be trusted"
               if mutates_live == UNREADABLE_HANDOFF
               else "says mutates-live=true" if mutates_live is True
               else "carries no mutates-live field")
        return (f"AE ({ae['html_url']}) was posted under the auto-AE toggle, but the newest "
                f"handoff footer {why} -- {manual}")
    return None


def footer_sha(comment: dict) -> str | None:
    """The `sha=` from the comment's OWN footer (attested or legacy), never
    from a marker quoted in its body."""
    footer = attested_footer(comment.get("body", ""))
    if footer.state is FooterState.UNREADABLE or footer.marker is None:
        return None
    match = FOOTER_SHA.search(footer.marker)
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
    # harmonic-forge#851 REFORGE: the footer is the body's own only when its
    # digest matches the text before it. A digest that matches nothing is a
    # mismatch (False), never "nothing to verify"; only a genuinely
    # digest-less (legacy) body passes on absence.
    return attested_footer(comment.get("body", "")).state is not FooterState.UNREADABLE


def current_head_sha() -> str:
    result = run("git", "rev-parse", "HEAD")
    if result.returncode:
        fail("cannot resolve checked-out HEAD")
    return result.stdout.strip()


#: harmonic-forge#791. A Lane 1 `handoff` is new scope and a Lane 3 `spec` is
#: an unapproved test plan; either one landing BETWEEN an authority and the
#: `ready-for-l3` it would extend to means that `ready-for-l3` covers a round
#: the authority never approved -- exactly hrse#2101's round 2, where the
#: AE at `829f8e54` carried forward to `ready-for-l3` at `21e587db` straight
#: through an intervening handoff and spec for that same round.
#:
#: Scoped to the (authority, candidate) WINDOW, not the whole thread. A
#: preclose refuter reproduced why that scoping matters, live, against
#: hrse#2095's real thread: an unrelated spec for a SEPARATE piece of work
#: (the production-backfill spec, posted while an earlier fix's AE was still
#: carrying forward to its own PASS) is not between that AE and the
#: `ready-for-l3` it authorizes -- so a thread-wide "any newer spec/handoff
#: anywhere" check refused a gate that had no round problem at all. Window
#: scoping is naturally immune: it only looks at what actually sits between
#: the two comments in THIS chain.
ROUND_KINDS = ("handoff", "spec")

#: harmonic-forge#792 AC3. A Lane 1 `rework` can amend test cases after the
#: AE (operator-memory `feedback_rework_comments_invisible_to_lane3.md`), and
#: an AE carried past it would cover a scope nobody approved. Most reworks
#: don't: FAIL -> rework -> repush is the routine cycle `carry_forward`
#: exists for. Whether a rework changed test cases can't be read from its
#: prose, so it's declared: a rework is a round boundary UNLESS its body
#: states `**Test cases:** unchanged`. Fails closed. A rework that forgets the
#: line costs one extra AE; it can never carry an old AE over new cases.
TC_UNCHANGED = re.compile(r"(?im)^[ \t]*\**[ \t]*Test cases[ \t]*:?[ \t]*\**[ \t]*:?[ \t]*unchanged\.?[ \t]*$")


def same_sha(a: str | None, b: str | None) -> bool:
    """Prefix equality, 7+ hex (harmonic-forge#792 preclose finding): a gate
    report may state an abbreviated SHA while `l1_post`'s footer records it
    in full, and exact equality refused an approved PASS for that alone."""
    if not a or not b or min(len(a), len(b)) < 7:
        return False
    return a.startswith(b) or b.startswith(a)


def _is_round_artifact(comment: dict) -> bool:
    body = comment.get("body", "")
    kind_match = FOOTER_KIND.search(body)
    kind = kind_match.group(1).lower() if kind_match else None
    if kind in ROUND_KINDS:
        return True
    if kind == "rework":
        return not TC_UNCHANGED.search(body)
    return bool(ROUND_HEADING.search(body))


def _round_artifact_between(comments: list[dict], lo_id: int, hi_id: int) -> dict | None:
    """The oldest round artifact (`handoff`, `spec`, or a test-case-changing
    `rework`) with `lo_id < id < hi_id`, or None."""
    found = [c for c in comments if lo_id < c["id"] < hi_id and _is_round_artifact(c)]
    return min(found, key=lambda c: c["id"]) if found else None


def _round_artifact_after(comments: list[dict], authority: dict) -> dict | None:
    """The oldest test-case-changing `rework` posted after `authority`, or
    None (harmonic-forge#792 preclose finding). The carry window only covers a
    new SHA; a rework amending test cases with no new push reuses the
    authority's own SHA and was never examined. Cross-family finding: a new
    handoff or spec at the same SHA is the same hole, so any round artifact
    counts. hrse#2095's interleaved spec is unaffected -- that gate ran on the
    carry path, which stays window-scoped."""
    found = [c for c in comments if c["id"] > authority["id"] and _is_round_artifact(c)]
    return min(found, key=lambda c: c["id"]) if found else None


def _rework_message(authority: dict, rework: dict) -> str:
    return (f"a new round artifact ({rework['html_url']}) was posted after {authority['html_url']} "
            "-- a fresh AE is required (a rework that changes no test cases can say "
            "`**Test cases:** unchanged`)")


def carry_forward(comments: list[dict], authority: dict, head_sha: str) -> dict | None:
    """A Lane 1 `ready-for-l3` posted after the authorizing
    comment, naming the commit actually being gated, extends that
    authorization to a new SHA without requiring a fresh one for the
    routine fix-and-repush cycle -- UNLESS a handoff or spec sits between
    the authority and that `ready-for-l3` (harmonic-forge#791): that means
    new scope or a new, unapproved test plan landed before the carry, and
    the carry must not paper over it.

    Generalized from `ae`-only to any authorizing comment --
    parameter rename only, same body (it only ever read `authority["id"]`,
    nothing AE-specific) -- so the identical staleness protection applies
    when a tier-R sweep is the authority instead of an AE."""
    candidates = [
        comment for comment in comments
        if comment["id"] > authority["id"]
        and _own_kind(comment) == "ready-for-l3"
        and same_sha(footer_sha(comment), head_sha)
        and _round_artifact_between(comments, authority["id"], comment["id"]) is None
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda c: c["id"])


def resolve_gate_authority(comments: list[dict], head_sha: str) -> tuple[dict | None, str]:
    """Does an AE or tier-R sweep authorize executing `head_sha`? `(authority,
    message)` -- `authority` is None exactly when `message` is a refusal
    reason; otherwise `message` is a human-readable success line.

    The single decision `main()` and `post_lane_discussion.require_round_
    approval` both act on (harmonic-forge#791) -- the gate cannot start, and
    a PASS cannot be posted, on two different definitions of "approved". This
    mirrors `main`'s own AE-then-tier-R-fallback precedence exactly (an AE
    that exists but does not (even via carry-forward) cover `head_sha` is
    still a hard failure -- it does NOT fall through to the tier-R path,
    matching the pre-#791 behaviour this refactor must not change) and
    applies `carry_forward`'s round-window check identically to both the AE
    and the tier-R branch, so a tampered tier-R sweep is caught here exactly
    as `verify_body_sha256` already caught it inline in `main` before this
    was factored out.
    """
    sweep = latest_by_kind(comments, "sweep")
    if (bad := unreadable_after(comments, "sweep", sweep)) is not None:
        return None, _unreadable_message("sweep", bad)
    if sweep is None:
        return None, "no gate-readiness sweep"
    tier = parse_write_tier(_attested_text(sweep))
    if tier is None:
        return None, f"sweep ({sweep['html_url']}) {NO_TIER_MESSAGE}"

    ae = latest_by_kind(comments, "ae")
    if (bad := unreadable_after(comments, "ae", ae)) is not None:
        return None, _unreadable_message("AE", bad)
    if ae is not None:
        # harmonic-forge#851: before the SHA match and before carry_forward, so
        # a later ready-for-l3 can never carry a refused auto-AE onto a new SHA.
        if is_auto_ae(ae) and (refusal := auto_ae_refusal(comments, ae, tier)):
            return None, refusal
        if sweep["id"] <= ae["id"]:
            return None, f"no gate-readiness sweep posted after the most recent AE ({ae['html_url']})"
        ae_sha = footer_sha(ae)
        if ae_sha is None:
            return None, f"AE comment ({ae['html_url']}) has no parseable sha= marker"
        if same_sha(ae_sha, head_sha):
            if (rework := _round_artifact_after(comments, ae)) is not None:
                return None, _rework_message(ae, rework)
            return ae, f"authorized for {head_sha} by {ae['html_url']}"
        carry = carry_forward(comments, ae, head_sha)
        if carry is None:
            return None, (
                f"AE ({ae['html_url']}) authorizes sha={ae_sha}, but the target is "
                f"{head_sha} -- post a Lane 1 ready-for-l3 naming {head_sha}, or a "
                "fresh AE at this commit"
            )
        return carry, f"authorized for {head_sha} by {carry['html_url']}"

    if tier != "R":
        return None, f"no AE comment and its sweep declares tier {tier} -- an AE is required above tier R"

    if not verify_body_sha256(sweep):
        return None, (
            f"sweep ({sweep['html_url']}) body does not match its recorded body-sha256 -- it "
            "may have been edited since posting; a tier-R gate cannot start on an "
            "unverifiable authorization anchor"
        )
    sweep_sha = footer_sha(sweep)
    if sweep_sha is None:
        return None, f"sweep ({sweep['html_url']}) has no parseable sha= marker"
    if same_sha(sweep_sha, head_sha):
        if (rework := _round_artifact_after(comments, sweep)) is not None:
            return None, _rework_message(sweep, rework)
        return sweep, f"authorized for {head_sha} by {sweep['html_url']}"
    carry = carry_forward(comments, sweep, head_sha)
    if carry is None:
        return None, (
            f"sweep ({sweep['html_url']}) authorizes sha={sweep_sha}, but the target is "
            f"{head_sha} -- post a Lane 1 ready-for-l3 naming {head_sha}, or a fresh "
            "sweep at this commit"
        )
    return carry, f"authorized for {head_sha} by {carry['html_url']}"


def tier_w_availability() -> str | None:
    """The disposable-graph availability line, from the consuming repo's own
    gate-adapter manifest (`tier_w_message.text`), or None when it declares
    none (harmonic-forge#721).

    It names a container, ports and task names, so it is the repo's data, not
    platform code -- ADR-008 Decision 1 applied to a string. Printed on every
    successful readiness check, which is the last thing a Lane 3 session runs
    before a gate, so it reaches the session at the moment it decides what a
    spec needs rather than waiting to be looked up. A manifest that carried it
    for weeks with nobody finding it is the whole finding: being present
    somewhere is not discoverability.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "gate"))
    import adapter  # noqa: PLC0415

    entry = adapter.declared("tier_w_message")
    return entry.get("text") if entry else None


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
    apply_project_identity(repo)  # harmonic-forge#804
    if args.issue is not None:
        issue = args.issue
    else:
        issue = issue_for_branch(current_branch())
    comments = fetch_comments(repo, issue)
    head_sha = current_head_sha()

    authority, message = resolve_gate_authority(comments, head_sha)
    if authority is None:
        fail(f"{repo}#{issue}: {message} -- before starting a Lane 3 gate")

    sweep = latest_by_kind(comments, "sweep")
    tier = parse_write_tier(sweep.get("body", "")) if sweep else None
    print(f"[check-lane3-ready] {repo}#{issue}: sweep ({sweep['html_url']}), tier {tier} "
          f"-- ready, {message}")
    availability = tier_w_availability()
    if availability:
        print(availability)


if __name__ == "__main__":
    main()
