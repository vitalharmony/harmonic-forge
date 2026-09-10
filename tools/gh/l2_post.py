#!/usr/bin/env python3
"""Receipt-backed Lane 2 status posting, with mandatory post/fetch/diff
self-check (harmonic-forge#371).

Composes an L2P/L2D/L2B status comment from the caller's own recorded
receipts (see receipt_runner.py) plus a clearly separated narrative
section -- never blends the two. The factual scaffold ("comment N exists
with this body hash", "the wrapped command exited 0/N") comes only from
receipts and from this script's own fresh REST snapshot; a "no new
comment exists" claim is refused unless it is backed by a snapshot taken
at call time (`snapshot`), never from memory or inference (AC3).

Posting goes through exactly one transport (`gh api ... -X POST`), and
this script refuses to report success until it has independently
re-fetched the comment it just posted and confirmed the id and a SHA-256
match on the body -- the same fetch-and-diff shape `l1_post.py`/
`post_comment.py` already use for Lane 1.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from receipt_runner import clear_lock, is_locked, lock_path, strip_ansi, write_receipt  # noqa: E402


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


#: JSON's textual escape for a C0 control code -- a backslash, the letter
#: u, then four hex digits -- is the shape json.dumps(..., ensure_ascii=True)
#: (the default, used by compose_body for the receipts block) emits for an
#: embedded control byte: a raw ESC (0x1b) is written out as that six-
#: character textual escape, not as a real control byte. harmonic-forge#571
#: preclose finding: comparing only raw ESC bytes between sent and landed
#: missed this shape entirely, because a receipts-derived body never
#: contains a raw control byte in the first place -- it contains this
#: textual escape of one instead. Diagnosis-only: this collapses either
#: representation to nothing so the AC3 check recognizes both, and is never
#: applied to what is actually posted.
_JSON_CONTROL_ESCAPE_RE = re.compile(r"\\u(00[01][0-9a-fA-F])")


def _normalize_for_diagnosis(text: str) -> str:
    """Collapse every representation of a control-character escape this
    module has seen in the wild -- a raw control byte, and the JSON-textual
    6-character escape of one -- to the SAME representation (a real byte)
    before stripping, so the AC3 self-check comparison recognizes a
    transit-mangled escape regardless of which shape survived on which
    side.

    Decoding first, rather than just deleting the textual escape marker, is
    load-bearing: the textual form only replaces the control byte itself,
    leaving any parameter/final bytes that followed it (the `[33m` of a
    `ESC[33m` CSI sequence) as plain text. Deleting just the marker and
    leaving those bytes in place does not agree with what `strip_ansi`
    removes on the raw-byte side, which takes the whole CSI sequence --
    comparing the two would misreport a genuine transit-mangled-escape match
    as a real content difference.
    """
    decoded = _JSON_CONTROL_ESCAPE_RE.sub(lambda m: chr(int(m.group(1), 16)), text)
    return strip_ansi(decoded)


def _gh_api(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["gh", "api", *args], text=True, capture_output=True)


def load_receipts(paths: list[str]) -> list[dict]:
    return [json.loads(Path(raw).read_text()) for raw in paths]


def snapshot(repo: str, issue: int) -> dict:
    """Fetch every comment on `issue` right now and record the result as a
    receipt -- the only source AC3 permits for a 'no new comment' claim."""
    result = _gh_api("--paginate", f"repos/{repo}/issues/{issue}/comments")
    if result.returncode != 0:
        raise SystemExit(f"snapshot fetch failed: {result.stderr}")
    comments = json.loads(result.stdout or "[]")
    body = {
        "repo": repo,
        "issue": issue,
        "comment_ids": [comment["id"] for comment in comments],
        "comment_body_sha256": {str(comment["id"]): _sha(comment["body"]) for comment in comments},
        "raw_response_sha256": _sha(result.stdout),
    }
    path = write_receipt(issue, "rest-snapshot", body)
    print(json.dumps({"receipt": str(path), **body}))
    return body


#: harmonic-forge#472. The lead block, per artifact rather than one universal
#: verdict/finding/next schema — the correction the red team made and Lane 1
#: ratified. Forcing four artifacts into one template produces headings that
#: lie: a sweep is pre-execution and has no verdict, an AE is an authorization
#: and reports no finding. Lane 2's three artifacts genuinely do share a shape
#: (status / what changed or what blocks / what happens next), so they share
#: one here — and nothing beyond them does.
LEAD_LABELS = ("Status", "Change", "Next")

#: Required on `completion` and `blocked`, optional on `plan`.
#:
#: The asymmetry is deliberate and is the open question 2 on the issue,
#: answered here as the plan's stated lean rather than left to block the
#: work: a plan's "finding" IS the plan, and a mandatory one-line summary of
#: something the reader is about to read in full produces filler. A completion
#: and a blocker both report an outcome that a reader needs before deciding
#: whether to read further, which is the operator's actual complaint.
LEAD_REQUIRED_KINDS = ("completion", "blocked")

#: `kind=finding` (harmonic-forge#571 AC4) -- Lane 2's sanctioned way to post
#: a durable, attributed defect note without a mismatched kind and without
#: the operator as courier. It is deliberately excluded from
#: `LEAD_REQUIRED_KINDS`: a finding is a report, not a status transition
#: (AC5), so it carries no "what happens next" the way a completion or a
#: blocker does.
#:
#: The heading below (`## L2 Finding`, with a space) is chosen specifically
#: so it does NOT match `^##\s+L2[A-Z]\b` -- the pattern `watch_lane_posts.py`
#: and HRSE2's `lane_state.py` both use to read `L2P`/`L2D`/`L2B` as status
#: transitions. A finding must never be read as one (AC5): `lane_state.py`'s
#: `_LANE_TOKEN` regex is `^##\s+L(?P<lane>[123])(?P<code>[PDSFB])\b`, and
#: `L2F` would satisfy it (F is in the allowed code set) -- so `L2F` was
#: rejected as a heading precisely because it looks safe and is not.
_HEADINGS = {
    "plan": "## L2P — receipt-backed status (harmonic-forge#371)",
    "completion": "## L2D — receipt-backed status (harmonic-forge#371)",
    "blocked": "## L2B — receipt-backed status (harmonic-forge#371)",
    "finding": "## L2 Finding — receipt-backed finding (harmonic-forge#571)",
}


def lead_block(lead: dict[str, str]) -> str:
    """The visible three lines. Empty string when nothing was supplied."""
    lines = [f"**{label}:** {lead[label].strip()}"
             for label in LEAD_LABELS if lead.get(label, "").strip()]
    return "\n".join(lines) + "\n\n" if lines else ""


#: harmonic-forge#580 preclose finding: `watch_lane_posts._classify` searches
#: the WHOLE comment body for this literal marker syntax before it ever looks
#: at the heading line -- it has to, since a real Lane 1 marker can trail
#: after arbitrary prose. `l1_post.py` already refuses to let a caller forge
#: this text into a body it composes (`reject_reserved_marker`,
#: `fetch_lane1_context.py:43`); `l2_post.py` had no equivalent, and a
#: `--kind finding` narrative is the single most likely body in the system to
#: quote this exact syntax verbatim -- a finding routinely pastes the failing
#: command's own output, and this module's own source narrates the very
#: marker text `discover_queue` reads. Reproduced live: a finding whose
#: narrative merely discusses `<!-- l1-post v1; kind=handoff -->` is
#: classified `('l1', 'handoff')` by `_classify`, which both drops the
#: issue's real queue membership (AC1) and manufactures a false Lane 2
#: `handoff` queue hit via `_search_candidates`'s literal-substring search.
_RESERVED_MARKER_RE = re.compile(r"<!--\s*l1-post\s+v\d+;")


def reject_reserved_marker(body: str) -> None:
    """Refuse to compose a body containing literal reserved marker syntax --
    see `_RESERVED_MARKER_RE` above for why this must be structural, not
    advisory, and checked against the FULLY ASSEMBLED body (narrative, lead
    fields, and the receipts JSON block can each carry it) rather than any
    one field in isolation."""
    if _RESERVED_MARKER_RE.search(body):
        raise SystemExit(
            "refusing to post: the composed body contains literal "
            "'<!-- l1-post v1;' marker syntax. watch_lane_posts.py's "
            "classifier treats this text as a real lane marker anywhere it "
            "appears in a comment body, not only in a trailing footer -- "
            "quoting or discussing the marker's own syntax verbatim (e.g. "
            "pasting a failing command's output that names it) silently "
            "impersonates a Lane 1 post. Break the string across a code "
            "span (e.g. `<!--` + ` l1-post`) or paraphrase instead."
        )


def compose_body(kind: str, receipts: list[dict], narrative: str,
                 lead: dict[str, str] | None = None) -> str:
    """Outcome first, evidence collapsed (harmonic-forge#472).

    Two changes from the shape this replaced, both structural rather than
    advisory — AC4 rejects "a convention a lane is asked to remember", and a
    lane physically cannot post through this function without them:

    1. The lead block sits above the narrative, so a reader who reads three
       lines knows the outcome and the next action (AC3).
    2. The receipts JSON moved BELOW the narrative and into a collapsed
       `<details>`. It was the first thing in the comment — evidence ahead of
       outcome, which is the defect this issue names. Nothing is deleted and
       nothing moves to a second comment (AC2).

    The `## L2P|L2D|L2B` heading stays at the top level, outside `<details>`:
    `lane_state.py` reads it, and hrse#1590 made position load-bearing.
    `kind=finding`'s `## L2 Finding` heading is deliberately shaped to NOT be
    read the same way -- see `_HEADINGS` (harmonic-forge#571 AC5).

    `narrative` is ANSI-stripped here too, not only by `main()` before the
    call (harmonic-forge#571 preclose finding) -- it is free text embedded
    unescaped, unlike the receipts JSON, so it is the one part of the body a
    caller could still leak colour through if only the call site stripped it.

    A caller physically cannot compose a body carrying literal reserved
    marker syntax through this function either (harmonic-forge#580 preclose
    finding) -- `reject_reserved_marker` runs on the fully assembled body
    before it is returned, so the guard applies uniformly to narrative, lead
    fields, and the receipts JSON alike.
    """
    narrative = strip_ansi(narrative)
    fenced = json.dumps(receipts, indent=2, sort_keys=True)
    count = len(receipts)
    body = (
        f"{_HEADINGS[kind]}\n\n"
        f"{lead_block(lead or {})}"
        f"### Narrative\n{narrative}\n\n"
        f"<details><summary>Verified receipts — {count}</summary>\n\n"
        f"```json\n{fenced}\n```\n\n"
        f"</details>\n"
    )
    reject_reserved_marker(body)
    return body


def validate_lead(kind: str, lead: dict[str, str]) -> None:
    """Refuse a completion or a blocked post that buries its outcome."""
    if kind not in LEAD_REQUIRED_KINDS:
        return
    missing = [label for label in LEAD_LABELS if not lead.get(label, "").strip()]
    if missing:
        raise SystemExit(
            f"--kind {kind} requires the lead block (harmonic-forge#472): "
            f"missing {', '.join('--' + label.lower() for label in missing)}. "
            "A reader who never expands the evidence still has to know the "
            "outcome and what happens next."
        )


def post(repo: str, issue: int, body: str) -> dict:
    result = _gh_api("--method", "POST", f"repos/{repo}/issues/{issue}/comments",
                      "-f", f"body={body}")
    if result.returncode != 0:
        raise SystemExit(f"post failed: {result.stderr}")
    posted = json.loads(result.stdout)
    comment_id = posted["id"]
    refetch = _gh_api(f"repos/{repo}/issues/comments/{comment_id}")
    if refetch.returncode != 0:
        raise SystemExit(
            f"post/fetch/diff self-check failed: could not refetch comment "
            f"{comment_id}: {refetch.stderr}"
        )
    refetched_body = json.loads(refetch.stdout).get("body", "")
    if _sha(refetched_body) != _sha(body):
        # harmonic-forge#571 AC3. A hash mismatch has two distinct causes, and
        # conflating them sent Lane 2 down a wrong hypothesis (GitHub strips
        # control characters) that took real diagnosis time to disprove. If
        # the bodies agree once escape artifacts are normalized on both
        # sides, the divergence is a transit-mangled escape, not a real
        # content difference -- and receipt_runner.py's AC1 fix (stripping at
        # capture) should already prevent it, so seeing this means an escape
        # reached this body some other way and is worth investigating rather
        # than retried blindly.
        #
        # `_normalize_for_diagnosis`, not bare `strip_ansi`, because the two
        # sides of a receipts-derived mismatch do not carry the same
        # representation of the escape: `compose_body`'s
        # `json.dumps(ensure_ascii=True)` turns an embedded control byte
        # into its 6-character textual form on the SENT side (there is no
        # raw ESC byte to strip there), while transit-mangling can produce a
        # real control byte on the LANDED side. Comparing only raw bytes
        # (bare `strip_ansi`) never sees these as equal, which is precisely
        # the gap a preclose review found live.
        if _normalize_for_diagnosis(refetched_body) == _normalize_for_diagnosis(body):
            raise SystemExit(
                f"post/fetch/diff self-check failed: comment {comment_id} body "
                "differs ONLY in ANSI escape sequences (a transit-mangled "
                "escape) -- not a real content difference, and NOT GitHub "
                "stripping characters (that hypothesis is wrong, see "
                "harmonic-forge#571). receipt_runner.py strips ANSI from "
                "previews at capture; an escape reaching this body some other "
                "way is a defect worth investigating, not something to retry "
                "blindly."
            )
        raise SystemExit(
            f"post/fetch/diff self-check failed: comment {comment_id} body hash "
            "mismatch -- what landed does not match what was sent, and it is "
            "not an ANSI-escape artifact. Refusing to report success."
        )
    return {"comment_id": comment_id, "body_sha256": _sha(body), "url": posted.get("html_url")}


#: Kinds a standing lock does not block. `blocked` always could.
#:
#: `finding` joined it in harmonic-forge#571 AC4 on the reasoning that it's
#: a report, not the ordinary status composition the lock exists to gate --
#: but that conflated "this kind doesn't require a lead" (why `finding` is
#: absent from `LEAD_REQUIRED_KINDS`) with "this kind should also bypass
#: the lock," which are not the same property. A locked issue's `finding`
#: post can still carry a caller-supplied `--status`/`--next` lead that
#: reads exactly like a gate outcome, with nothing in the lock/lead/lane-
#: state chain positioned to catch it (harmonic-forge#580 AC2, live
#: reproduction in the issue body). Removed here: a finding is a defect
#: report and never asserts completion, so requiring the lock be resolved
#: first costs nothing a finding needs -- `blocked` remains the sanctioned
#: way to report while locked, unchanged (AC5).
LOCK_EXEMPT_KINDS = ("blocked",)


def lock_blocks(kind: str, issue: int) -> bool:
    """Whether posting `kind` for `issue` must be refused because of a
    standing failure lock. Factored out of `main` so the exemption is
    directly testable rather than only reachable through argv."""
    return is_locked(issue) and kind not in LOCK_EXEMPT_KINDS


def resolve_lock(repo: str, issue: int, resolution_comment: int) -> None:
    check = _gh_api(f"repos/{repo}/issues/comments/{resolution_comment}")
    if check.returncode != 0:
        raise SystemExit(
            f"--resolve-lock refused: comment {resolution_comment} could not be "
            "fetched -- a bare assertion that the lock is resolved is not accepted."
        )
    clear_lock(issue)
    print(f"lock cleared for issue {issue}, resolved by comment {resolution_comment}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)

    post_p = sub.add_parser("post", help="compose and post a receipt-backed status")
    post_p.add_argument("--kind", choices=("plan", "completion", "blocked", "finding"),
                        required=True,
                        help="'finding' (harmonic-forge#571) posts a durable, attributed "
                             "defect note -- a report, not a status transition; it never "
                             "reads as L2P/L2D/L2B to lane_state.py or the belt.")
    post_p.add_argument("--repo", required=True)
    post_p.add_argument("--issue", type=int, required=True)
    post_p.add_argument("--receipts", nargs="*", default=[])
    post_p.add_argument("--narrative-file", type=Path, required=True)
    post_p.add_argument("--status", default="",
                        help="Lead block: where this issue now stands. Required "
                             "for --kind completion/blocked (harmonic-forge#472).")
    post_p.add_argument("--change", default="",
                        help="Lead block: what changed, or what blocks. Required "
                             "for --kind completion/blocked.")
    post_p.add_argument("--next", dest="next_action", default="",
                        help="Lead block: the literal next action. Required for "
                             "--kind completion/blocked.")

    snap_p = sub.add_parser("snapshot", help="fetch and record a fresh comment snapshot")
    snap_p.add_argument("--repo", required=True)
    snap_p.add_argument("--issue", type=int, required=True)

    lock_p = sub.add_parser("resolve-lock", help="clear an issue-scoped failure lock")
    lock_p.add_argument("--repo", required=True)
    lock_p.add_argument("--issue", type=int, required=True)
    lock_p.add_argument("--resolution-comment", type=int, required=True)

    args = parser.parse_args()

    if args.action == "snapshot":
        snapshot(args.repo, args.issue)
        return 0

    if args.action == "resolve-lock":
        resolve_lock(args.repo, args.issue, args.resolution_comment)
        return 0

    # args.action == "post"
    if lock_blocks(args.kind, args.issue):
        # harmonic-forge#580 preclose finding: this string used to
        # hand-enumerate LOCK_EXEMPT_KINDS ("post --kind blocked or --kind
        # finding instead") and drifted the moment `finding` was removed
        # from that tuple (AC2) -- the refusal named an escape hatch that
        # no longer existed, on the one path whose entire job is to tell a
        # blocked Lane 2 session what to do instead. Built from the tuple
        # directly so it cannot drift again.
        alternatives = " or ".join(f"--kind {kind}" for kind in LOCK_EXEMPT_KINDS)
        print(
            f"issue {args.issue} is locked ({lock_path(args.issue)}) by a failed "
            "underlying command -- run `l2_post.py resolve-lock` with a real, "
            f"fetchable resolution comment first, or post {alternatives} instead.",
            file=sys.stderr,
        )
        return 2

    receipts = load_receipts(args.receipts)
    # harmonic-forge#571 preclose finding: the narrative is free text a
    # caller writes directly and is embedded into the body unescaped (not
    # through json.dumps) -- the single most likely place to carry colour
    # under a `--kind finding` post, since a finding's whole point is often
    # to paste the failing command's own output. `compose_body` strips it;
    # read raw here and let that be the one place responsible for it.
    narrative = args.narrative_file.read_text()
    lead = {"Status": args.status, "Change": args.change, "Next": args.next_action}
    validate_lead(args.kind, lead)
    body = compose_body(args.kind, receipts, narrative, lead)
    result = post(args.repo, args.issue, body)
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
