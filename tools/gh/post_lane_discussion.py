#!/usr/bin/env python3
"""Post an attested lane comment through the same safe transport boundary,
tagging the posting session's LANE (harmonic-forge#193).

Originally Lane-1-exclusive (post_lane1_discussion.py); now available to
Lane 2/3 sessions too (post-#190/#191), so the marker records which LANE
actually posted rather than always implying Lane 1 authorship.

harmonic-forge#473 — this is also the emitter for Lane 3's two artifacts.

Every other gate-phase artifact (`ae`, `sweep`, `ready-for-l3`, the
L2S/L2D/L2B family -- L2P was retired at harmonic-forge#583) has an emitter that stamps a `kind=` footer. Lane 3's
Test Spec and Gate Results — the two comments that decide whether a gate
passed — went out through this script, which stamped `kind=discussion` on
everything. So the premise both a private-repo incident and harmonic-forge#472 measured
("no footer") was very slightly wrong in a way that made the fix smaller
than either expected: the footer was there and **misdeclared its kind**.
There was never a missing emitter, only a missing argument.

That is also why this is not a new `l3_post.py`. A second script would
duplicate this transport, this LANE tagging, and this reserved-marker
guard, and the two would drift — the failure a private-repo incident exists to fix, one
protocol concept with two implementations.
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

# harmonic-forge#504's check is PLATFORM work — every repo's Lane 3 posts
# through this path, and a copy per repo is what drifts. Imported from the
# forge checkout rather than vendored, the same way the rules are.
_FORGE_ROOT = Path(__file__).resolve().parents[2]
_FORGE = _FORGE_ROOT / "tools" / "gh"
if _FORGE.is_dir():
    sys.path.insert(0, str(_FORGE))

from gate_ci import check_gate_result, gated_sha, lane3_artifact, looks_like_a_gate_report, verdict_of

# harmonic-forge#791: the one definition of "this round is approved", shared
# with `lane3-begin` so the gate cannot start, and a PASS cannot be posted, on
# different rules.
import check_lane3_ready

# harmonic-forge#691 (AC1'). This is the THIRD marker-posting tool -- the
# one the pre-rescope design missed, despite it being the actual path
# Lane 2's own guard routes `plan` postings through (`reject_plan_as_
# discussion` below refuses a `plan` posted through any OTHER path). Same
# graceful-absence posture as `gate_ci` immediately above.
try:
    import belt_candidates
except ImportError:  # pragma: no cover - platform checkout absent
    belt_candidates = None

from l1_post import (
    comment_body, fail, regular_body, reject_reserved_marker, resolve_repo, run,
    apply_project_identity, validate_lead,
)

#: The kinds this script may stamp. `discussion` is the default and the
#: pre-harmonic-forge#473 behaviour, so every existing caller — the
#: `lane-comment` mise task, every lane session that invokes it — keeps
#: working unchanged and unrewritten.
KINDS = ("discussion", "plan", "spec", "gate-result")

#: The heading each artifact must carry, as the cross-check. Same posture
#: a private-repo incident established for the other kinds: the footer is the authority
#: because this script only stamps it after validating; the heading is the
#: cross-check, and a disagreement is refused here rather than reported
#: downstream. `l1_post.py`'s `AE_HEADING`/`SWEEP_HEADING` are the pattern.
#:
#: `Lane 3 Gate Results` deliberately does NOT require PASS/FAIL in the
#: heading: `lane_state.py` reads the verdict from it, but a BLOCKED gate
#: is a legitimate third outcome that names neither, and refusing to post
#: one would push Lane 3 back to the untyped `discussion` path — the exact
#: hole this closes.
KIND_HEADING = {
    # harmonic-forge#618. Either spelling: `## Plan — H<N>` is what Lane 2
    # actually writes, `## L2S` is what `l2_post.py` stamps. Both are plans;
    # requiring one would just move the bypass.
    "plan": re.compile(r"(?im)^#{1,4}[ \t]*(?:Plan\b|L2S\b)"),
    "spec": re.compile(r"(?im)^#{1,4}[ \t]*Lane 3 Test Spec\b"),
    "gate-result": re.compile(r"(?im)^#{1,4}[ \t]*Lane 3 Gate Results\b"),
}

#: Marker-shaped headings, mirrored from `lane_state.py`'s own scan rather
#: than invented here — if that module learns to read a new heading, this
#: guard has to learn it in the same commit or the guard is a fiction.
QUOTABLE_MARKERS = re.compile(
    r"(?m)^#{1,4}[ \t]*(?:L[123][PDSFB]\b"
    r"|Lane 3 (?:Test Spec|Gate Results)\b"
    r"|AE\b"
    r"|Gate-readiness sweep\b"
    r"|Handoff\b)")

_FENCE = re.compile(r"```.*?```", re.S)


def resolve_body_path(path: Path) -> Path:
    """Resolve a body path against the invoking repo, never the platform repo."""
    if path.is_file():
        return path
    caller_root = os.environ.get("LANE_TRANSPORT_CALLER_ROOT")
    if caller_root:
        repo_root = Path(caller_root)
    else:
        top = run("git", "rev-parse", "--show-toplevel", cwd=Path.cwd())
        repo_root = (Path(top.stdout.strip()) if top.returncode == 0 and top.stdout.strip()
                     else _FORGE_ROOT)
    candidate = repo_root / path
    if candidate.is_file():
        return candidate
    raise SystemExit(
        f"--file not found: {path}\n"
        f"  tried: {path.resolve()}\n"
        f"     and: {candidate}\n"
        "Pass an absolute path — the working directory is not stable "
        "between tool calls."
    )


def _executable(body: str) -> str:
    """The body as `lane_state.py` will read it: fenced blocks removed.

    a private-repo incident strips fences before every marker scan, so a marker inside a
    fence is already inert. Measuring that rather than assuming it is what
    made this guard's shape obvious — see the docstring on
    `reject_quoted_markers`.
    """
    return _FENCE.sub("", body)


def reject_quoted_markers(kind: str, body: str) -> None:
    """Refuse evidence that quotes a marker-shaped heading unfenced.

    **Measured against the merged a private-repo incident, not assumed.** Four shapes,
    same parser:

        quoted inside a FENCE                -> inert, no transition
        quoted inside <details>, unfenced    -> masked ONLY if the host
                                                comment carries a marker of
                                                the same class already
        quoted inside <details>, blockquoted -> same
        an `## L2B` quoted inside <details>  -> **forges a transition**

    So `<details>` does not hide anything: harmonic-forge#472 requires
    evidence to be retained verbatim inside a collapsed block, and a
    collapsed block is not a fence. The residual hazard is narrower than
    "any quoted marker forges a state" — it is a quoted marker of a class
    the host comment does not itself carry, which is exactly the case a
    gate report quoting a prior round's `L2B` or `L3S` hits.
    A quoted *footer* is already refused outright by
    `reject_reserved_marker`, so only headings need this.

    **Refuse rather than auto-fence.** Rewriting a lane's evidence to make
    it parse is the shape of fix that silently changes what a gate report
    says it observed; `l1_post.py` refuses malformed sweeps rather than
    repairing them, and this follows that. The author fences the quote,
    which is one edit and leaves the evidence verbatim inside the fence.
    """
    if kind == "discussion":
        # Ordinary discussion carries no marker of its own, so a quoted one
        # would forge a transition here too. But `lane-comment` is also how
        # every lane quotes a thread back at itself, and retrofitting a
        # refusal onto the general-purpose path is a scope this issue does
        # not have. Recorded as a known gap rather than half-closed.
        return
    stripped = _executable(body)
    own = KIND_HEADING[kind].search(stripped)
    extra = [m for m in QUOTABLE_MARKERS.finditer(stripped)
             if not (own and m.start() == own.start())]
    if extra:
        quoted = ", ".join(sorted({m.group(0).strip() for m in extra}))
        fail(
            f"--kind {kind} body quotes marker-shaped heading(s) outside a "
            f"fenced block: {quoted} (harmonic-forge#473). `lane_state.py` "
            "strips fences before every marker scan but does NOT strip "
            "`<details>`, so a collapsed evidence block still forges a state "
            "transition. Wrap the quoted heading in a ``` fence — the "
            "evidence stays verbatim and becomes inert."
        )


#: A Lane 2 plan headed `## Plan — H1234`. Anchored to the first line, because
#: a comment that merely DISCUSSES a plan legitimately says the word.
#: Anchored to the first line, because a comment that merely DISCUSSES a plan
#: legitimately says the word. Both spellings, per KIND_HEADING["plan"]:
#: `## Plan — H<N>` is what Lane 2 writes by hand, `## L2S` is what
#: `l2_post.py` stamps. harmonic-forge#618's preclose finding: keying on one
#: of them only moves the bypass rather than closing it.
_PLAN_HEADING_RE = re.compile(r"\A\s*#{1,4}\s*(?:Plan\b|L2S\b)", re.I)


def reject_plan_as_discussion(kind: str, body: str) -> None:
    """Refuse a Lane 2 plan posted as `kind=discussion` (harmonic-forge#618).

    Measured live 2026-09-10: four Plan-First plans -- a private-repo incident, #1662, #1663,
    #1771 -- went out through this script headed `## Plan — H####` and stamped
    `kind=discussion; posted-by=LANE2`. All four then sat unactioned until Lane
    2 asked Lane 1 why it kept ignoring them.

    Nothing was going to find them. `discussion` was deliberately removed from
    `QUEUE_KINDS["l2"]` on a measurement of 63 issues whose newest marker was a
    `discussion`, **none actionable** -- so a plan wearing that kind is
    invisible to every bounded queue, by design and for good reason. Lane 1's
    belt is worktrees-first and a Plan-First issue has no worktree until the
    plan is approved. Two correct mechanisms, and the plan fell between them
    because it was mislabelled at the source.

    This is the same defect harmonic-forge#473 fixed for Lane 3: this very
    script stamped `kind=discussion` on Test Specs and Gate Results too, and
    the fix was "there was never a missing emitter, only a missing argument."
    Same again, for Lane 2's plans.

    **Refuse rather than rewrite.** Silently restamping the kind would change
    what the thread records about who declared what -- the shape of fix
    `reject_quoted_markers` above already rejects for the same reason.
    """
    if kind != "discussion" or not _PLAN_HEADING_RE.match(body):
        return
    fail(
        "This looks like a Lane 2 plan (first line is a `## Plan` heading) but "
        "it is being posted as `kind=discussion`.\n\n"
        "  `discussion` is deliberately NOT queue-eligible -- 63 issues were "
        "measured whose newest marker was a discussion and none were "
        "actionable -- so a plan posted this way is invisible to Lane 1's "
        "inbound queue and to the belt. Four plans stalled exactly this way "
        "(harmonic-forge#618).\n\n"
        "  Post it with `--kind plan`, which stamps a queue-eligible marker:\n"
        "    mise run lane-comment --issue <N> --kind plan --file <path>\n\n"
        "  If this genuinely is discussion ABOUT a plan rather than the plan "
        "itself, change the heading -- the check anchors on the first line."
    )


def validate_kind(kind: str, body: str) -> None:
    heading = KIND_HEADING.get(kind)
    if heading and not heading.search(body):
        fail(
            f"--kind {kind} body must carry its heading — the footer is only "
            f"authority because this script refuses to stamp it without the "
            f"cross-check (e.g. '## Lane 3 Test Spec — H<N>')."
        )
    reject_quoted_markers(kind, body)
    reject_plan_as_discussion(kind, body)


#: harmonic-forge#893: each spec case's class, fixed at approval time, and each
#: gate case's verdict. Bounded so the footer stays one short physical line
#: (`eras._OWN_FOOTER` is line-anchored).
CASE_CLASSES = ("ac", "existing", "live")
CASE_RESULTS = ("pass", "fail", "blocked")
MAX_CASES = 64
MAX_CASE_ID = 12
#: The same freshness bound the Lane 3 write guard applies to this marker.
LANE3_MARKER_MAX_AGE_SECONDS = 12 * 60 * 60
_CASE_KEY = re.compile(r"^(?:TC[- ]?)?(\w+)$", re.I)


def sidecar(body_path: Path, name: str) -> Path | None:
    """`<body>.<name>.json` beside the body file (gate.md -> gate.results.json),
    when it exists. The documented way to pass a case map: it needs no flag,
    so it survives every repo's lane-comment wrapper."""
    candidate = Path(body_path).with_suffix(f".{name}.json")
    return candidate if candidate.is_file() else None


def warn(message: str) -> None:
    """harmonic-forge#893 reforge (R2): a case map is telemetry, and telemetry
    never refuses a post. A map that cannot be used is reported here and the
    post goes out with the field stamped `absent`."""
    print(f"[post-comment] WARNING: {message}; posting with the case map recorded as absent",
          file=sys.stderr)


def load_case_map(path: Path | None, allowed: tuple[str, ...], flag: str) -> dict[str, str] | None:
    """`{case id: value}` from `path`, or None when there is no usable map.

    The map is the record of what Lane 3 ran (harmonic-forge#893 reforge, R1):
    its keys ARE the case ids. The prose body is never parsed to check them;
    two passes showed no reader of free-form reports both finds every real case
    and nothing else. Only the map's own shape is checked. Whether it agrees with
    the prose is a data-quality question the verification report flags, never a
    reason to refuse a post (R2)."""
    if path is None:
        warn(f"no {flag} was given")
        return None
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        warn(f"{flag}: cannot read a JSON object from {path} ({exc})")
        return None
    if not isinstance(raw, dict):
        warn(f"{flag}: {path} must hold a JSON object of case id to value")
        return None
    if len(raw) > MAX_CASES:
        warn(f"{flag}: {len(raw)} cases; at most {MAX_CASES} are recorded")
        return None
    found: dict[str, str] = {}
    for key, value in raw.items():
        match = _CASE_KEY.match(str(key).strip())
        if not match or len(match.group(1)) > MAX_CASE_ID:
            warn(f"{flag}: {key!r} is not a case id")
            return None
        if match.group(1) in found:
            warn(f"{flag}: {key!r} names case {match.group(1)} a second time")
            return None
        value = str(value).strip().lower()
        if value not in allowed:
            warn(f"{flag}: case {match.group(1)} is {value!r}; allowed: {', '.join(allowed)}")
            return None
        found[match.group(1)] = value
    return found


def _case_order(key: str) -> tuple[int, str]:
    return (int(key), "") if key.isdigit() else (10 ** 9, key)


def case_field(name: str, mapping: dict[str, str] | None) -> str:
    """`name=1:pass,2:fail`; `name=` for an empty map (a BLOCKED gate that ran
    no case); `name=absent` when no usable map was given (R2)."""
    if mapping is None:
        return f"{name}=absent"
    return f"{name}=" + ",".join(f"{k}:{mapping[k]}" for k in sorted(mapping, key=_case_order))


def gate_ms(issue: int | None = None, now: float | None = None) -> str:
    """Milliseconds since `lane3-begin` wrote `<git-dir>/LANE3_ACTIVE` for THIS
    issue, or `unknown`. Derived, never self-reported, never written as zero.

    The marker must name the issue (`issue=<N>`, which lane3-begin writes with
    --issue): the 12-hour bound is the write guard's freshness bound, not a
    gate duration, so a marker left by an earlier gate in the same session
    would otherwise time the next gate from the first one's start (#893 e1
    sticky-wicket #7). A marker that names no issue, or another one, reads
    unknown."""
    # The caller's own directory: a lane-comment task may cd elsewhere (HRSE2's
    # runs from Lane 1's tools worktree), and mise records where it was invoked.
    cwd = os.environ.get("MISE_ORIGINAL_CWD") or None
    # A telemetry derivation is never in a post's raise path (#893 reforge,
    # R2): a pruned caller directory or a missing git reads as unknown.
    try:
        git_dir = subprocess.run(["git", "rev-parse", "--absolute-git-dir"], cwd=cwd,
                                 text=True, capture_output=True, check=False)
        if git_dir.returncode:
            return "unknown"
        marker = Path(git_dir.stdout.strip()) / "LANE3_ACTIVE"
        named = re.search(r"(?m)^issue=(\d+)\s*$", marker.read_text(encoding="utf-8", errors="replace"))
        if issue is None or not named or int(named.group(1)) != int(issue):
            return "unknown"
        started = marker.stat().st_mtime
    except Exception:  # noqa: BLE001 - telemetry never fails the post
        return "unknown"
    elapsed = (time.time() if now is None else now) - started
    if elapsed <= 0 or elapsed > LANE3_MARKER_MAX_AGE_SECONDS:
        return "unknown"
    return str(int(elapsed * 1000))


def footer(kind: str, body: str, posted_by: str, ack_no_pr_required: str | None = None,
           case_fields: str = "") -> str:
    """`kind=discussion` keeps its exact pre-harmonic-forge#473 footer.

    Byte-identical on that path on purpose: `lane_state.py`, a private-repo incident's
    handoff-heading fallback and every archived comment already match it,
    and a footer that gained a digest for discussion posts would make every
    one of them look like an attested artifact.
    """
    if kind == "discussion":
        # Byte-identical unless the body is a gate report, which carries its
        # case fields whatever its kind (#893 reforge, R3).
        extra = f"; {case_fields}" if case_fields else ""
        return f"\n\n<!-- l1-post v1; kind=discussion; posted-by={posted_by}{extra} -->\n"
    # Same digest contract as `l1_post.post_kind`: hash the RSTRIPPED body,
    # because that is what gets posted (a private-repo incident — hashing the raw body
    # recorded a digest a verifier could never reconstruct). This is what
    # lets `lane_state.py` report `validated=True` for these two artifacts
    # instead of the `False` they were pinned to by having no digest at all.
    digest = hashlib.sha256(body.rstrip("\n").encode()).hexdigest()
    override = (f"; ack-no-pr-required={ack_no_pr_required}"
                if ack_no_pr_required is not None else "")
    # harmonic-forge#893: the case fields go INSIDE the one-line footer, after
    # every existing key, so every footer reader keeps parsing it. They are not
    # inside the digest: the digest covers the body, as every verifier expects.
    extra = f"; {case_fields}" if case_fields else ""
    return (f"\n\n<!-- l1-post v1; kind={kind}; posted-by={posted_by}; "
            f"body-sha256={digest}{override}{extra} -->\n")


def require_green_ci(
    kind: str, repo: str, body: str, ack_no_pr_required: str | None = None,
) -> bool:
    """Returns whether the no-PR override was the reason this passed.

    `gate_ci.check_gate_result`'s `(ok, message)` return keeps its existing
    2-tuple shape (18 call sites in its own test suite unpack it that way);
    the override marker lives in `message` instead of widening that contract.
    """
    """A Lane 3 PASS may not outrun the PR's own CI (harmonic-forge#504).

    Fires for anything that IS a gate report, not only for what was stamped as
    one.

    2026-09-07: a gate returned PASS at 00:12 and CI failed on the same code at
    00:13, and `main` stayed red for three hours across two merges. The three
    failures read the operator's live `~/.claude/settings.json` — and Lane 3
    runs on the operator's machine, so the gate is structurally incapable of
    seeing that class of failure. `testing-gate.md` could say "also check CI";
    AC1 rejects that outright, because prose compliance degrades under context
    pressure and a long gate run IS that pressure.

    Only PASS is gated. FAIL and BLOCKED are reports of a problem and must
    always be publishable — a check that can silence a failure report is worse
    than none.

    Silent no-op when the platform checkout is absent: this must not make a
    repo unable to post a gate result because a sibling directory moved.
    """
    # Bound at MODULE level, not imported inside this function. A local import
    # shadows the module attribute, so the check could not be patched — which
    # meant the test asserting a red CI is refused passed against a function
    # that never called the checker at all.
    #
    # Keyed on the BODY, not on `kind`. `kind` is the author declaring what
    # they are posting; omitting `--kind gate-result` skipped the check
    # entirely while `lane_state.py` still scored the comment `gate.pass` from
    # its heading. 74 of 98 real `Lane 3 Gate Results` comments across both
    # repos carry no `kind=gate-result` footer, so that was the majority path,
    # not a corner. `kind` is kept as a second trigger for a gate report whose
    # heading is non-standard.
    # `kind` is NOT consulted. It is the author declaring what they are
    # posting, and omitting `--kind gate-result` skipped the check entirely
    # while `lane_state.py` still scored the comment `gate.pass` from its
    # heading — 74 of 98 real gate comments across both repos carry no
    # `kind=gate-result` footer, so that was the majority path, not a corner.
    # `check_gate_result` recognises a gate report by its heading, exactly as
    # `lane_state.py` does, and returns cleanly for anything that is not one;
    # asking it every time means the two can never disagree about what a gate
    # report IS.
    ok, message = check_gate_result(repo, body, ack_no_pr_required=ack_no_pr_required)
    if not ok:
        fail(message)
    return ok and message.startswith("[GATE] no-pr-override:")


def require_round_approval(repo: str, issue: int, body: str) -> None:
    """A PASS may not be posted for a round nobody approved (harmonic-forge#791).

    `lane3-begin` is the first line; this is the second, at the point a PASS
    becomes real -- via `check_lane3_ready.resolve_gate_authority`, the SAME
    function, not a second definition of "approved" (a preclose refuter found
    exactly this drift when the tier-R body-sha256 check lived only in one of
    two copies; there is now one copy).

    Scoped to genuine gate reports only (preclose finding): `verdict_of`'s
    regex reads a PASS-shaped verdict out of ANY body, including a Lane 1
    `ready-for-l3` whose lead block happens to contain "Result: PASS" prose --
    `looks_like_a_gate_report` is the same recognizer `check_gate_result`
    itself gates on, so a `ready-for-l3`/`rework`/`sweep` is never blocked by
    a check meant only for `## Lane 3 Gate Results`. FAIL and BLOCKED report a
    problem and must always be publishable, at any kind. Fails closed: a
    comments fetch that fails refuses the PASS (`fetch_comments` raises
    SystemExit); a PASS with no parseable Head-SHA also refuses -- there is
    nothing to check authorization against.
    """
    if verdict_of(body) != "PASS" or not looks_like_a_gate_report(body):
        return
    sha = gated_sha(body)
    if sha is None:
        fail("[GATE] REFUSED: a PASS must state the head SHA it gated -- cannot check round approval")
    comments = check_lane3_ready.fetch_comments(repo, issue)
    authority, message = check_lane3_ready.resolve_gate_authority(comments, sha)
    if authority is None:
        fail(f"[GATE] REFUSED: a PASS cannot be posted for an unapproved round -- {message}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Post an attested lane comment, tagging the posting session's LANE")
    parser.add_argument("--repo")
    parser.add_argument("--issue", type=int, required=True)
    parser.add_argument("--file", type=Path, required=True)
    parser.add_argument(
        "--kind", choices=KINDS, default="discussion",
        help="Artifact kind stamped into the footer (harmonic-forge#473). "
             "`spec` and `gate-result` are Lane 3's two artifacts and are "
             "validated against their heading and digested; `discussion` is "
             "the default and unchanged.")
    parser.add_argument(
        "--ack-no-pr-required", default=None, metavar="REASON",
        help="harmonic-forge#788: a `gate-result` PASS whose named SHA is "
             "the current tip of `main` (a data-migration action with no "
             "PR of its own, e.g. hrse#2095's live backfill) never gets a "
             "`verify` run -- that workflow is gated to pull_request events "
             "only. This override is consulted ONLY at that specific "
             "no-PR-possible point; a SHA that belongs to a real PR (open, "
             "pending, or red) is refused exactly as before, regardless of "
             "this flag.")
    parser.add_argument(
        "--tc-classes", type=Path, default=None, metavar="JSON",
        help="harmonic-forge#893: a JSON object mapping each case id in a Lane 3 spec to "
             "ac, existing or live. The map is the record; the body is not parsed for ids.")
    parser.add_argument(
        "--tc-results", type=Path, default=None, metavar="JSON",
        help="harmonic-forge#893: a JSON object mapping each case id a gate report ran to "
             "pass, fail or blocked. Without it a gate report posts with results=absent.")
    args = parser.parse_args()
    if args.ack_no_pr_required is not None and not args.ack_no_pr_required.strip():
        fail("--ack-no-pr-required requires a non-empty reason")
    # Preclose finding: the reason is written INSIDE the HTML-comment footer,
    # so `-->` would end the footer early, truncate its fields and leak the
    # rest into the rendered comment. Refused rather than rewritten, the same
    # posture `reject_quoted_markers` takes toward a lane's own text.
    if args.ack_no_pr_required is not None and (
            "-->" in args.ack_no_pr_required or "<!--" in args.ack_no_pr_required
            or "\n" in args.ack_no_pr_required):
        fail("--ack-no-pr-required reason may not contain '-->', '<!--' or a "
             "newline: it is recorded inside the comment's HTML footer")
    args.repo = resolve_repo(args.repo)
    apply_project_identity(args.repo)  # harmonic-forge#804
    # harmonic-forge#266: a relative --file resolves against the CALLER's cwd,
    # which is not stable — mise resets it, and an agent's shell is reset
    # between tool calls. That produced four "task failed" errors in one
    # session, each a generic mise failure that said nothing about the real
    # cause. Resolve against the repo root when the literal path is not there,
    # and fail with a message that names the file if neither resolves.
    path = resolve_body_path(args.file)
    body = regular_body(path)
    reject_reserved_marker(body)
    validate_kind(args.kind, body)
    # a private-repo incident AC2: same lead-block/cap requirement as l1_post.py's own
    # kinds, same function -- not a second implementation. `discussion` has
    # no LEAD_FIELDS entry, so this is a no-op on the pre-existing default
    # path; only `spec`/`gate-result` are newly checked.
    validate_lead(args.kind, body)
    # harmonic-forge#893 reforge (R2, R3): keyed on the BODY, never on --kind,
    # for the reason require_green_ci gives below -- 74 of 98 real gate reports
    # were posted as `discussion`. A gate report always carries its results
    # field and gate time; a spec carries its classes field. Neither ever
    # refuses the post: a missing or unusable map is stamped `absent`.
    case_fields = ""
    lane = os.environ.get("LANE")
    # The same poster identity the footer stamps, LANE-unset included (#893 e1
    # sticky-wicket #6): only Lane 3 posts an artifact.
    artifact = lane3_artifact(body, args.kind if args.kind in ("gate-result", "spec") else None,
                              f"LANE{lane}" if lane else "LANE-unset")
    # The map rides in a sidecar file next to the body, never only in a flag
    # (#893 e1 sticky-wicket #4): a consuming repo's lane-comment wrapper
    # validates argv strictly and rejected an unknown flag before this script
    # ran, so a FAIL could not be posted. The flags still win when given.
    args.tc_results = args.tc_results or sidecar(path, "results")
    args.tc_classes = args.tc_classes or sidecar(path, "classes")
    if artifact == "gate":
        case_fields = (f"{case_field('results', load_case_map(args.tc_results, CASE_RESULTS, '--tc-results'))}; "
                       f"gate-ms={gate_ms(args.issue)}")
    elif artifact == "spec":
        case_fields = case_field("classes", load_case_map(args.tc_classes, CASE_CLASSES, "--tc-classes"))
    elif args.tc_results is not None or args.tc_classes is not None:
        print("[post-comment] WARNING: --tc-results/--tc-classes given, but this post is not a "
              "Lane 3 spec or gate report (a Lane 1/2 relay never is); the map was not recorded",
              file=sys.stderr)
    override_used = require_green_ci(args.kind, args.repo, body, args.ack_no_pr_required)
    require_round_approval(args.repo, args.issue, body)
    lane = os.environ.get("LANE")
    posted_by = f"LANE{lane}" if lane else "LANE-unset"
    url, _ = comment_body(
        args.repo,
        args.issue,
        body.rstrip("\n") + footer(
            args.kind, body, posted_by,
            ack_no_pr_required=args.ack_no_pr_required if override_used else None,
            case_fields=case_fields,
        ),
    )
    print(f"[post-comment] posted and refetched {url}")
    #: harmonic-forge#691 (AC1'). `posted_by` here is derived from the SAME
    #: `LANE` env var the footer above already used -- `"l1"`/`"l2"`/`"l3"`
    #: is `belt_candidates`'s own convention (matching `QUEUE_POSTERS`'
    #: keys/values), not the footer's `"LANE1"`/`"LANE-unset"` spelling.
    #: An unset or unrecognized LANE records as `"unknown"`, which
    #: `QUEUE_POSTERS` never lists as an accepted poster for any lane --
    #: so it is simply never eligible, rather than being misattributed.
    if belt_candidates is not None:
        candidate_poster = f"l{lane}" if lane in ("1", "2", "3") else "unknown"
        belt_candidates.record_candidate(args.repo, args.issue, args.kind, candidate_poster)
    else:
        #: harmonic-forge#691 preclose finding 4. Same hole `l1_post.py`'s
        #: `record_queue_candidate` had: a bare `ImportError` fallback to
        #: `None` with no visible consequence made this indistinguishable
        #: from "recorded successfully" -- the `[post-comment] posted...`
        #: line above still prints either way. `item_list_cache`'s
        #: graceful-absence posture is not equivalent here: that one falls
        #: back to a still-functional uncached path, this one falls back
        #: to doing nothing. Never raises -- the comment itself must still
        #: succeed -- but the operator/log must be able to tell "recorded"
        #: from "silently recorded nothing".
        print(
            "[post-comment] belt-candidate not recorded: "
            "could not import belt_candidates from ~/harmonic-forge/tools/gh "
            "(checkout absent, or harmonic-forge#691's belt_candidates.py "
            "has not landed on its main branch yet) -- "
            f"{args.repo}#{args.issue} kind={args.kind} will not appear in "
            "the belt's recorded-candidate source until this import succeeds",
            file=sys.stderr,
        )


if __name__ == "__main__":
    main()
