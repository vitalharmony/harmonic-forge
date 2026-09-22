#!/usr/bin/env python3
"""Atomic Lane 1 status posting capability for a private-repo incident."""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

from _sweep_tier import NO_TIER_MESSAGE, parse_write_tier

PLATFORM_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PLATFORM_ROOT / "tools" / "onboard"))
from manifest import ManifestError, require_onboarded_repo  # noqa: E402

# harmonic-forge#219: shared item-list cache, consolidating 4 previously
# independent duplicate `gh project item-list` fetches. Falls back to a
# direct, uncached fetch if ~/harmonic-forge isn't present -- this script
# has zero prior dependency on that repo existing.
sys.path.insert(0, str(PLATFORM_ROOT / "tools" / "gh"))
try:
    import item_list_cache as _item_list_cache
except ImportError:
    _item_list_cache = None

# harmonic-forge#691 (AC1'). The shared belt-candidate recorder -- this is
# the one place every `l1-post v1` marker gets written, so it is also the
# natural place to record that a belt candidate now exists. Same
# graceful-absence posture as `item_list_cache` above: a sibling checkout
# moving must never make this tool unable to post.
try:
    import belt_candidates as _belt_candidates
except ImportError:
    _belt_candidates = None

# a private-repo incident: R-0128 (harmonic-forge#496) fixed the READ side
# (check_lane3_ready.py refuses to execute without the exact
# `<PREFIX><N>` heading) but left the write side loose -- AE_HEADING/
# SWEEP_HEADING below only ever required the leading keyword, not the
# prefix+number, because the prefix "isn't otherwise threaded through"
# these functions. Reuse the same repo->prefix table batch_auth already
# derives from the manifest (never a second hand-maintained copy), with
# the identical fail-open-to-a-fixed-table posture batch_auth itself
# uses -- a missing/broken import here must not block a real post over
# a heading-strictness nicety.
sys.path.insert(0, str(PLATFORM_ROOT / "tools" / "hooks"))
try:
    from batch_auth import REPO_PREFIXES as _REPO_PREFIXES
except ImportError:
    _REPO_PREFIXES = {
        "vitalharmony/hrse": "H",
        "vitalharmony/harmonic-forge": "F",
        "vitalharmony/cymagraph-infra": "I",
        "vitalharmony/openclaw-projects": "O",
    }


def _repo_prefix(repo: str) -> str | None:
    return _REPO_PREFIXES.get(repo)

# harmonic-forge#687: a posted handoff discharges the owed-handoff record
# `gh_issue.py` wrote at filing time. Same optional shape as the cache above
# and for the same reason -- this script must keep working with no
# ~/harmonic-forge present, and an absent store is a missed discharge, never
# a failed post.
sys.path.insert(0, str(PLATFORM_ROOT / "tools" / "hooks"))
try:
    import handoff_owed as _handoff_owed
except ImportError:
    _handoff_owed = None

HANDOFF_HEADINGS = [
    "Issue", "Lane 3 Gate Variant", "Affected Files", "Root Cause / Entry Point",
    "Design Alternatives Considered", "Load-Bearing Assumptions",
    "Delegated Judgment Calls", "Pre-Flight Preconditions", "Implementation Spec",
    "Test Cases (for Lane 3)", "Read-Before-Edit Instruction", "Ambiguity Gate",
]
TC_ID = re.compile(r"\bTC[- ]?(\d+)\b", re.I)

# a private-repo incident (pre-close panel): a sweep entry must be a LINE, must name its own
# case, and must carry text. The deleted TC_STATUS pattern enforced all three
# incidentally, and only its `pass|fail|blocked` vocabulary was ever the defect
# -- dropping the whole pattern also dropped line-anchoring, identifier binding
# and non-emptiness. Measured on the panel's own reproductions: bare ordinals
# ("1.\n2.\n3."), entries naming a case that does not exist, entries about
# something else entirely, and the spec pasted back verbatim as its own sweep
# all validated. `TC_ID` is unanchored by design (it must find mentions in a
# spec's prose), so it can never carry this job; `LIST_ITEM` already anchors,
# which is why the list scheme was never this weak.
SWEEP_TC_ENTRY = re.compile(
    r"(?m)^[ \t]*(?:[-*][ \t]+|\d+\.[ \t]*)\**[ \t]*TC[- ]?(\d+)\b\**[ \t]*[-\u2014:.]?[ \t]*(.*)$",
    re.I,
)
SWEEP_LIST_ENTRY = re.compile(r"(?m)^[ \t]*\d+\.[ \t]*\**[ \t]*(.*)$")
# Only `pass`/`fail` are fabrications at sweep time. `ready` and `blocked` are
# both knowable before execution and stay legal.
SWEEP_FABRICATED_OUTCOME = re.compile(
    r"(?mi)^[ \t]*(?:[-*][ \t]+|\d+\.[ \t]*)\**[ \t]*TC[- ]?\d+\b\**[ \t]*[-\u2014:.]?[ \t]*(pass|passed|fail|failed)\b"
)

# a private-repo incident: Lane 3 specs number their cases as an ordinary markdown list at
# least as often as they use TC<n> -- 3 of the 5 most recent used no TC
# identifier at all. Requiring the identifier made the sweep cross-check
# unrunnable on those, and the workaround (posting via lane-comment) still
# produced an artifact Lane 3 accepts, so the validation silently did not
# happen. Fall back to list numbering, scoped to the spec's own test-case
# section so unrelated numbered lists elsewhere in the body are not counted.
# Matches both real formats: a spec's "### Test cases" and a sweep's
# "### Per-case readiness". Deliberately not a bare "cases?" -- that would
# also catch "### Edge cases" and count the wrong list.
CASE_HEADING = re.compile(
    r"(?im)^#{2,4}\s*.*\b(?:test\s+cases?|per-case|case\s+readiness)\b")
LIST_ITEM = re.compile(r"(?m)^\s*(\d+)\.\s")


#: harmonic-forge#472's lead block, as case-scanning sees it. Stripped before
#: any case identifier is read, because the two features collide by
#: construction the moment both are required:
#:
#:   **Next:** re-gate TC3 after the fix.
#:
#: On a sweep with no `### Per-case readiness` heading, `_case_section` falls
#: back to the WHOLE BODY, so that one line adds a phantom `TC3` and
#: `validate_sweep` rejects a correct sweep with "case IDs must exactly match
#: spec (expected ['1','2'], got ['1','2','3'])". Reproduced live before this
#: line existed; it is the concrete form of the issue's AC5.
#:
#: Line-scoped, and only these labels: a case entry is `1. TC1 — ...` or
#: `- TC1 ...`, so a `**Label:** ...` line can never be one, in a spec or a
#: sweep. A blanket "skip everything before the first heading" would have
#: deleted the whole body of a spec that uses bare TC<n> markers and no
#: heading at all — the shape a private-repo incident exists to keep working.
#: a private-repo incident added five more label names once LEAD_FIELDS grew past
#: sweep/ae. `validate_sweep` runs `case_ids()` over a fetched SPEC body too
#: (not just the sweep being validated), and a spec now carries its own
#: `**Cases:** ...` lead line -- unstripped, it risks nothing today (no
#: label here is a bare digit line `LIST_ITEM` or `TC_ID` could latch onto),
#: but the whole point of this set is "known lead labels, stripped before
#: case-scanning" rather than "labels observed to be safe so far".
LEAD_LINE = re.compile(
    r"(?im)^[ \t]*\**[ \t]*(?:Readiness|Blockers|Next|Authorized|Status|Change"
    r"|Scope|Target|Verified|Finding|Cases|Verdict)"
    r"[ \t]*\**[ \t]*:.*$")


def _case_section(body: str) -> tuple[str, bool]:
    """Return the case section, or the whole body when no heading exists.

    a private-repo incident: a spec organizing its cases under sub-headings ("### Group A"
    directly beneath a "## Test cases" heading) used to have its section
    cut to empty -- NEXT_HEADING matched the very next line, which was the
    first group sub-heading, not the section's real end. A sub-heading
    nested *under* the case heading is part of the case section; only a
    heading at the same level or shallower (### under ##, or ## itself)
    genuinely exits it. Deeper-still headings (#### under a ### case
    heading) are handled the same way, generalized rather than hardcoded
    to the two-level "## / ###" shape observed live.

    a private-repo incident: CASE_HEADING's "test case" vocabulary also matches a
    per-case heading phrased naturally as "### TC1 -- test case 1: ...".
    Taking the FIRST such match (harmonic-forge#401's spec) picked TC1's
    own heading as if it were the section label, truncating the section
    to TC1's body and silently dropping every other case. A heading that
    itself carries a case identifier (TC_ID) is a per-case heading, not
    the section label -- skip any match with one, and keep looking. If
    every CASE_HEADING match on a spec carries its own TC_ID, there is no
    real section heading at all (only naturally-worded per-case ones),
    and this correctly falls through to has_case_heading=False, the same
    whole-body TC<n> scan already used for a spec with bare TC<n> markers
    and no heading whatsoever.
    """
    body = LEAD_LINE.sub("", body)
    match = None
    for candidate in CASE_HEADING.finditer(body):
        if TC_ID.search(candidate.group(0)):
            continue
        match = candidate
        break
    if not match:
        return body, False
    level = len(re.match(r"#{1,4}", match.group(0)).group(0))
    next_heading = re.compile(rf"(?m)^#{{1,{level}}}\s")
    rest = body[match.end():]
    following = next_heading.search(rest)
    return (rest[:following.start()] if following else rest), True


def _classify_case_ids(body: str) -> tuple[set[str], bool]:
    """Case identifiers plus whether they came from TC<n> markers (vs. a plain
    numbered list) -- both branches return plain digit strings, so the caller
    cannot tell which scheme was used from the ids alone; validate_sweep needs
    that distinction to decide whether the strict "TCn: status" line format
    applies (a private-repo incident follow-up).

    a private-repo incident: TC<n> matching used to run over the whole comment before the
    section-scoped list-numbering fallback got a turn, so a single incidental
    "TC6" in a Basis/precondition paragraph -- outside the actual test-cases
    section -- pre-empted a spec that numbers its cases as a plain 1..N list.
    Scope the TC<n> search to the same section CASE_HEADING already isolates
    for the list path, and only fall back to a whole-body TC<n> search when no
    case-heading exists at all (a spec using bare TC<n> markers with no
    recognizable "### Test cases" heading should still validate).
    """
    section, has_case_heading = _case_section(body)
    if not has_case_heading:
        ids = set(TC_ID.findall(section))
        return ids, bool(ids)
    # a private-repo incident follow-up 2: a numbered-list case (e.g. "12. ... TC4 ...") can
    # cross-reference an earlier case by its number *inside* the section
    # itself, not just in prose outside it -- observed live on a private-repo incident's
    # spec. Prefer the section's own numbered-list structure whenever one
    # exists; only treat TC<n> as the case-numbering scheme when the section
    # has no plain "N. " list at all (the genuine "- TC1: a" shape).
    list_ids = set(LIST_ITEM.findall(section))
    if list_ids:
        return list_ids, False
    return set(TC_ID.findall(section)), bool(TC_ID.findall(section))


def case_ids(body: str) -> set[str]:
    """Case identifiers in a spec or sweep: TC<n> if present, else list numbers."""
    return _classify_case_ids(body)[0]
TEMPLATE_PLACEHOLDER = re.compile(
    r"\{(?:url|labels|quoted line or condition that is the root cause|"
    r"explicit step-by-step instruction for Lane 2 — no ambiguity)\}"
    r"|\{(?:standard \(|none \| )"
)
RESERVED_MARKER = "<!-- l1-post "


def run(*args: str, cwd: Path | None = None,
        env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=cwd, env=env, text=True, capture_output=True, check=False)


# The pre-post check must not inherit the ambient temp root. A transient
# ``.git`` there makes every fixture below it look as though it belongs to an
# unrelated repository and can cause correct SHAs to be refused.
CHECK_TMP_BASE = Path.home() / ".cache" / "l1-post-check"


def _private_check_tmp() -> tuple[Path, dict[str, str]]:
    """Return a fresh private temp directory and an environment using it."""
    CHECK_TMP_BASE.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="check-", dir=CHECK_TMP_BASE))
    env = dict(os.environ)
    for name in ("TMPDIR", "TMP", "TEMP"):
        env[name] = str(root)
    return root, env


def fail(message: str) -> None:
    raise SystemExit(f"[l1-post] {message}")


def resolve_repo(value: str | None, cwd: Path | None = None) -> str:
    """Resolve an explicit repo or the invoking checkout through projects.toml."""
    candidate = value
    if candidate is None:
        origin = run("git", "remote", "get-url", "origin", cwd=cwd or Path.cwd())
        if origin.returncode != 0 or not origin.stdout.strip():
            fail("--repo was omitted and the invoking checkout has no readable origin")
        candidate = origin.stdout.strip()
    try:
        project = require_onboarded_repo(candidate)
    except ManifestError as exc:
        fail(str(exc))
    assert project.repo is not None
    return project.repo


#: harmonic-forge#691 (AC1'/AC2'). The belt's candidate set for
#: `--queue-for` is worktree-derived plus explicit `--issues` -- neither
#: exists for a FRESH post on an issue with no worktree yet, which is
#: exactly what harmonic-forge#596/#618 (and #686's own removal of the
#: account-wide scan that used to cover this) are about. `l1_post.py` is
#: the one place every `l1-post v1` marker gets written, so it is also the
#: natural place to record that a candidate now exists -- a fact this tool
#: already knows, not something the belt has to go ask GitHub for.
#: `l2_post.py` and `post_lane_discussion.py` are the other two writers,
#: all three calling the same shared `belt_candidates.record_candidate`
#: (harmonic-forge) rather than each keeping their own copy of this logic.
def record_queue_candidate(repo: str, issue: int, kind: str) -> None:
    """Record `{repo, issue, kind, posted_by="l1"}` for the belt to pick up
    later -- `posted_by` is hardcoded, not derived from `LANE`, because
    this tool IS Lane 1's, unconditionally (unlike `post_lane_discussion.py`,
    which is shared across lanes and derives it from the environment).

    Best-effort: a write failure here must never fail the post itself (the
    comment is already on GitHub by the time this runs) -- the belt is a
    convenience the record feeds, not the other way around. NOT a silent
    no-op when the platform checkout is absent (`_belt_candidates is
    None`) -- harmonic-forge#691 preclose finding 4: `item_list_cache`'s
    own graceful-absence posture falls back to a still-functional
    uncached path, so its silence is honest. This one has no fallback --
    "the import failed" means "nothing was recorded" -- and the
    `[l1-post] success` message printed by the caller regardless made that
    indistinguishable from "recorded successfully" (AC1' violation). A
    stderr warning here is the whole fix: it never raises, so the post
    itself still succeeds, but the operator/log can now tell "recorded"
    from "silently recorded nothing"."""
    if _belt_candidates is None:
        print(
            "[l1-post] belt-candidate not recorded: "
            "could not import belt_candidates from ~/harmonic-forge/tools/gh "
            "(checkout absent, or harmonic-forge#691's belt_candidates.py "
            "has not landed on its main branch yet) -- "
            f"{repo}#{issue} kind={kind} will not appear in the belt's "
            "recorded-candidate source until this import succeeds",
            file=sys.stderr,
        )
        return
    _belt_candidates.record_candidate(repo, issue, kind, "l1")


def regular_body(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        fail("--file must name a regular, non-symlink file")
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        fail(f"cannot read body file: {exc}")


def reject_reserved_marker(body: str) -> None:
    if RESERVED_MARKER in body:
        fail("body may not contain the reserved l1-post attestation namespace")


def _describe_repo(cwd: Path | None) -> str:
    """Which repo `cwd` (or the actual process cwd, if None) is standing in
    -- harmonic-forge#568 AC1. `resolve_sha`'s only diagnostic used to be
    git's own `fatal: Needed a single revision`, which reads as "that SHA is
    wrong" and gives no way to tell it apart from "you are standing in a
    repo that does not contain this commit at all" -- the actual cause on
    every real occurrence measured for this issue (harmonic-forge#567's own
    handoff hit it while being handed off from HRSE2 to a forge-attesting
    invocation). One `git remote get-url origin` -- the same call
    `_source_repo_is_hrse` already makes for an unrelated reason below --
    says which repo this is, independent of and never a substitute for
    `--repo` (that flag names the GitHub issue's repo, not the repo the
    attested commit lives in; see `_source_repo_is_hrse`'s own docstring --
    AC2 protects that independence, and this function does not touch it)."""
    actual_cwd = cwd or Path.cwd()
    remote = run("git", "remote", "get-url", "origin", cwd=actual_cwd)
    if remote.returncode == 0 and remote.stdout.strip():
        return f"{remote.stdout.strip()} (cwd {actual_cwd})"
    return f"cwd {actual_cwd} (no git remote 'origin' found there)"


def resolve_sha(value: str, cwd: Path | None = None) -> str:
    result = run("git", "rev-parse", "--verify", f"{value}^{{commit}}", cwd=cwd)
    if result.returncode:
        fail(
            f"cannot resolve --sha {value!r} in {_describe_repo(cwd)}: "
            f"{result.stderr.strip()}"
        )
    return result.stdout.strip()


def heading_content(body: str, heading: str) -> str:
    match = re.search(rf"(?ms)^### {re.escape(heading)}\s*$\n(.*?)(?=^### |\Z)", body)
    return match.group(1).strip() if match else ""


def is_substantive(value: str) -> bool:
    # TEMPLATE_PLACEHOLDER's alternatives are literal phrases with no
    # whitespace tolerance -- harmonic-forge#382's preclose review found
    # the canonical template itself hard-wraps one of them across a line
    # break ("Lane 2 —\nno ambiguity"), which let a verbatim-copied
    # placeholder through undetected. Collapse whitespace runs before
    # matching so a line-wrapped placeholder is caught the same as an
    # unwrapped one; this only affects placeholder detection, never the
    # stored/posted body.
    normalized = re.sub(r"\s+", " ", value)
    if not value or TEMPLATE_PLACEHOLDER.search(normalized) or "[Issue #N" in value:
        return False
    # a private-repo incident: a value that is ENTIRELY a single {...} span, once
    # whitespace-collapsed, is unfilled template syntax regardless of its
    # exact wording -- narrower than "contains braces anywhere" (which real
    # content legitimately does: a code snippet, a dict literal, both
    # already asserted substantive above by TEMPLATE_PLACEHOLDER's own
    # test coverage). Matched only when the WHOLE value is the placeholder,
    # never a substring of it. Added when `validate_lead` started routing
    # its captured field text through this function and the handoff
    # template's own `{one line — what this handoff covers, ...}` lead
    # placeholder passed as "present" under the old presence-only check
    # (preclose-inspection finding on this issue).
    stripped = normalized.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        return False
    return True


#: a private-repo incident — the marker that says an Implementation Spec is deliberately
#: withheld, as a pattern over what Lane 1 authors ACTUALLY write rather than
#: one magic phrase.
#:
#: Measured across 333 handoffs on both repos since 2026-08-01: of the 229
#: whose Delegated Judgment Calls read as Plan-First, the idioms in use were
#: `withheld` (108), `this handoff triggers Plan-First` (40), `spec follows`
#: (23), `post the plan` (22) — and 145 matched none of them. Requiring the
#: single phrase the issue proposed would have refused 189 of 229 correct
#: historical handoffs. A validator that reads the language authors already
#: use is not the same thing as a magic token, which is what DJC2 asked for.
WITHHELD_MARKER = re.compile(
    r"\bwithheld\b"
    r"|\bomitted pending\b"
    r"|\bdeferred pending\b"
    r"|\bthis handoff triggers Plan-First\b"
    r"|\bspec follows\b"
    r"|\bpost the plan\b",
    re.I)


def validate_plan_first_spec(body: str, plan_first: bool | None) -> None:
    """A Plan-First handoff may not lead with steps (a private-repo incident, ADR-005).

    **The trigger is the DECLARED `--plan-first`, not `Delegated Judgment
    Calls != none`.** The issue's own scope note said to keep the latter; it
    was written before a private-repo incident and is superseded (Lane 1 confirmed). That
    inference is `plan_first_of`'s single guessing branch and a private-repo incident is the
    recorded case where it read backwards — on a handoff whose own sentence is
    *"this does not need Plan-First. Implement directly."* Building a hard
    REJECTION on it would make the unreliable path load-bearing for a failure
    rather than for a soft misclassification. `--plan-first` is declared by the
    author, refused when absent, and already validated one line away.

    **The rule is: the withheld marker must come before the first step.** Not
    "there are no steps", which is what the issue's text asks for and what the
    live corpus rules out. Two legitimate shapes contain a step list:

    - a **split handoff** — a private-repo incident's Implementation Spec opens *"Half 1
      only. Half 2's spec is withheld pending the Plan-First decision
      above."* Half 1's steps are correct and necessary.
    - a **withheld section that then enumerates what the spec will cover** —
      a private-repo incident's shape.

    Any rule strict enough to satisfy the issue's wording rejects both. This
    one is measurably weaker: an author can put the marker first and paste the
    spec below it. It is also the strongest rule that does not refuse correct
    work, and it still catches the founding incident — a private-repo incident's handoff
    pairs substantive Delegated Judgment Calls with a complete seven-step spec
    beginning `1. Read scripts/gate_scheduler_lease.py in full`, and no
    marker anywhere. Hand-verified on four real handoffs; rule C classified
    all four correctly and the naive step-count rule got two of them wrong.

    `None` — undetermined — does not reach here: `validate_plan_first_declared`
    refuses the post before this runs.
    """
    if not plan_first:
        return
    spec = heading_content(body, "Implementation Spec")
    marker = WITHHELD_MARKER.search(spec)
    first_step = LIST_ITEM.search(spec)
    # `<` not `<=` states the rule; the two are equivalent in practice and
    # mutation testing confirms it. `LIST_ITEM` anchors at a line's leading
    # whitespace and the marker matches a word, so the two can never begin at
    # the same offset. Kept strict because "before" is what the rule means.
    if marker and (first_step is None or marker.start() < first_step.start()):
        return
    if marker is None:
        fail(
            "--plan-first true, but the Implementation Spec carries no "
            "withheld marker (a private-repo incident / ADR-005). A Plan-First handoff "
            "defers its spec until Lane 2's plan is ratified — say so in the "
            "section rather than omitting it, because an absent section is "
            "indistinguishable from a forgotten one. Any of: 'withheld', "
            "'omitted pending', 'deferred pending', 'this handoff triggers "
            "Plan-First', 'spec follows', 'post the plan'."
        )
    fail(
        "--plan-first true, but the Implementation Spec's step list starts "
        "BEFORE its withheld marker (a private-repo incident / ADR-005). Co-delivering the "
        "spec with the plan-first instruction is the a private-repo incident failure ADR-005 "
        "records: it let the plan round be skipped 'even when explicitly, "
        "repeatedly stated'. Move the marker above the first numbered step."
    )


def validate_handoff(body: str, requires_preflight: bool) -> None:
    missing = [heading for heading in HANDOFF_HEADINGS if not heading_content(body, heading)]
    if missing:
        fail("handoff missing required headings: " + ", ".join(missing))
    for heading in HANDOFF_HEADINGS:
        if heading == "Implementation Spec":
            spec = heading_content(body, heading)
            # A genuine Plan-First deferral keeps the guidance paragraph and
            # drops (or replaces) the non-Plan-First branch's own placeholder
            # -- the guidance phrase alone is not enough: harmonic-forge#382's
            # preclose review found a template-derived skeleton carries BOTH
            # the guidance paragraph AND the still-unfilled "Otherwise" branch
            # placeholder by construction, which the phrase-only check let
            # straight through on every single generated handoff.
            if ("**If this handoff triggers Plan-First" in spec
                    and "{explicit step-by-step instruction for Lane 2" not in spec):
                continue
        if not is_substantive(heading_content(body, heading)):
            fail(f"handoff heading is still a template placeholder: {heading}")
    if requires_preflight and heading_content(body, "Pre-Flight Preconditions").lower() == "none":
        fail("live-mutating/cross-repo handoff requires explicit pre-flight preconditions")


#: harmonic-forge#472. Per-artifact lead blocks, NOT one universal
#: verdict/finding/next schema. That universal shape was proposed, red-teamed
#: and rejected on semantic grounds Lane 1 ratified: a gate-readiness sweep is
#: a PRE-execution artifact — `validate_sweep` below says in as many words
#: that neither `pass` nor `fail` is true of it — so it has no verdict and no
#: finding, it has a readiness assessment. An AE is an authorization; it
#: reports nothing at all. Forcing both into one template produces headings
#: that lie, which is a worse failure than the burial this issue fixes.
#:
#: a private-repo incident extends the table past the two shortest artifacts to every
#: kind, reversing the "not in the ratified table" scoping this comment used
#: to state for `ready-for-l3`/`rework`. That scoping was never wrong about
#: the universal-schema rejection above — it is still wrong to force PASS/
#: FAIL onto a sweep — but it was silent on the narrower question this issue
#: answers: a *kind-specific* lead requirement for the long artifacts too.
#: Measured 2026-09-08: the two enforced kinds are the shortest of eight, and
#: the operator's own words were "I only need to know what I need to know to
#: advance the issue" — a requirement the corpus shows unmet on `handoff`,
#: `rework`, and Lane 3's `spec`/`gate-result`, which is most of what a
#: reader actually opens.
#:
#: Fields are chosen per-kind, same discipline as sweep/ae above: `handoff`
#: reports scope, not a verdict it hasn't earned yet; `ready-for-l3` names
#: what it verified, because "ready" alone is the same unearned-verdict
#: shape a sweep already avoids; `rework` names the finding that makes more
#: work necessary; `spec`/`gate-result` (added by `post_lane_discussion.py`,
#: which imports and calls this same function rather than a second
#: implementation) name case count and verdict respectively.
LEAD_FIELDS = {
    "sweep": ("Readiness", "Blockers", "Next"),
    "ae": ("Authorized", "Next"),
    "handoff": ("Scope", "Next"),
    "ready-for-l3": ("Target", "Verified", "Next"),
    "rework": ("Finding", "Next"),
    "spec": ("Cases", "Next"),
    "gate-result": ("Verdict", "Finding", "Next"),
}

#: Where the lead has to be, and what the cap (below) measures. `<details>`
#: first, because that is the one thing a lead must not sit inside; then the
#: first sub-heading of ANY level, which is where evidence starts in every
#: real report surveyed. Bounded to `#{3,6}` when this covered only
#: sweep/ae, on the reasoning that the artifact's own heading is always `##`
#: and must not self-terminate. a private-repo incident's own preclose-inspection found
#: that reasoning doesn't generalize: `ready-for-l3`/`gate-result` bodies in
#: real use structure themselves with `##` sub-headings ("## Lane 1
#: review", "## Gate variant"), and excluding ALL `##` lines — not just the
#: artifact's own first one — let those bodies' entire content count as
#: "lead". See `lead_region()`: skip past the artifact's own opening line,
#: THEN match any heading level.
#:
#: **A `###`/`##` heading is a fold for this purpose by the issue's own
#: definition (AC3: "below the first `###` section or `<details>` block"),
#: not because GitHub visually collapses it** — it does not; preclose-
#: inspection correctly flagged that a heading placed right after the
#: required labels lets everything past it render fully while still
#: counting as "below the fold" here, and that gap is accepted rather than
#: closed: measured against the real corpus (6,385 comments), the
#: alternative — capping only what `<details>` truly hides — puts real
#: `handoff` bodies at up to 21,848 bytes before the cap would even start
#: counting, because this codebase's own convention almost never folds
#: evidence into `<details>` at all; the organizational heading IS the fold
#: in practice. A heading-based cap trades a narrow, deliberate-authorship
#: gaming vector for one that actually matches how every real artifact here
#: is written. Not revisited without new evidence, per the same discipline
#: `LEAD_CAP_BYTES` asks for below.
LEAD_STOP = re.compile(r"(?im)^\s*<details\b|^#{2,6}[ \t]")
LEAD_LABEL = r"(?im)^[ \t]*\**[ \t]*{label}[ \t]*\**[ \t]*:[ \t]*\**[ \t]*(?P<text>\S.*)$"

#: a private-repo incident AC3. A lead that merely EXISTS can still bury the reader in
#: 8,000 bytes of "summary" — extending LEAD_FIELDS alone guarantees a label
#: exists, not that it is short. **Calibrated against the full real corpus,
#: not a filing-time sample** — the original ~950-byte estimate this
#: constant shipped with was wrong by more than 2x, caught by
#: preclose-inspection replaying all 6,385 `l1-post`-footered comments on
#: `vitalharmony/hrse`. The true maximum `lead_region()` across every kind
#: this validates is 3,758 bytes (a 13-case sweep, issuecomment-5275967223)
#: — `sweep`/`ae` bodies routinely run past 2,000 bytes because they have
#: no `<details>` convention at all; their per-case list sits directly
#: under a `###`/bare heading, which is legitimate content a 13-case gate
#: genuinely needs to report. 4,096 clears that observed maximum with
#: headroom and is the smallest round number that does. Raise it in a
#: follow-up if a legitimate lead is ever refused for size — AC5's replay
#: (`test_l1_post_lead_real_corpus.py` and the corpus measurement in this
#: issue's own closing report) is exactly the check that would show it.
LEAD_CAP_BYTES = 4096


def lead_region(body: str) -> str:
    """The part of a report a reader sees before expanding anything, and
    what `LEAD_CAP_BYTES` is measured against.

    The artifact's own opening heading line is never itself a stop — skip
    past it before searching, rather than excluding an entire hash-count
    class from `LEAD_STOP` (which is what let a real `##`-substructured
    body count as all-lead; a private-repo incident preclose-inspection finding). Every
    kind this validates opens with its own heading line (`## Handoff:
    ...`, `# ready-for-l3 — ...`, etc.), so skipping to the first newline
    is always skipping exactly that line, regardless of its hash count.
    """
    first_newline = body.find("\n")
    search_from = first_newline + 1 if first_newline != -1 else len(body)
    stop = LEAD_STOP.search(body, search_from)
    return body[:stop.start()] if stop else body


def validate_lead(kind: str, body: str) -> None:
    """Refuse a post that buries its outcome under its evidence, or that
    pads the lead itself past the point of being one (a private-repo incident AC3).

    A validator rather than an emitter, deliberately and with the limit
    stated: `l1_post.py` does not compose these bodies, a lane does. AC4 asks
    for the requirement to be mechanical rather than "a convention a lane is
    asked to remember", and a refusal at post time is mechanical — a lane
    cannot land a burying report — even though it is not composition.
    """
    fields = LEAD_FIELDS.get(kind)
    if not fields:
        return
    region = lead_region(body)
    # a private-repo incident preclose-inspection: presence alone accepted an unfilled
    # template placeholder (`**Scope:** {one line — ...}`) as "present" --
    # this file's own `is_substantive()` already exists for exactly this
    # class of defect (`validate_handoff` uses it per-heading) and simply
    # wasn't being called on lead fields. Route the captured text through
    # it here, same as every other required field in this file.
    missing = []
    for label in fields:
        match = re.search(LEAD_LABEL.format(label=label), region)
        if not match or not is_substantive(match.group("text")):
            missing.append(label)
    if missing:
        fail(
            f"--kind {kind} must lead with its summary block before any "
            f"evidence (harmonic-forge#472): missing "
            + ", ".join(f"`**{label}:** ...`" for label in missing)
            + ". Put it directly under the artifact's heading, above the "
            "first `###` section or `<details>` block — a reader who never "
            "expands the evidence still has to know where this stands and "
            "what happens next."
        )
    region_size = len(region.encode())
    if region_size > LEAD_CAP_BYTES:
        fail(
            f"--kind {kind}'s lead region is {region_size} bytes, over the "
            f"{LEAD_CAP_BYTES}-byte cap (a private-repo incident). Move detail below the "
            "first `###` section or `<details>` block — the lead is what a "
            "reader sees before expanding anything."
        )


AE_HEADING = re.compile(r"(?im)^#{1,4}\s*AE\b")


#: a private-repo incident preclose finding: an unanchored `(?m)^...` search only proves the
#: canonical form appears SOMEWHERE at a line start -- including inside a
#: fenced code block quoting what the correct heading looks like, which is
#: exactly the shape the real a private-repo incident incident's own correction
#: comments used. `post_lane_discussion.py`'s `_executable()`/`_FENCE`
#: already solved this for its own marker scan (a private-repo incident); mirrored here
#: rather than re-deriving a second, weaker version.
_FENCE = re.compile(r"```.*?```", re.S)


def _heading_matches_exact(pattern: re.Pattern[str], body: str) -> bool:
    """True only when `pattern` matches the body's actual FIRST line, fenced
    blocks stripped first. `re.match`, not `.search` -- a match anywhere
    later in the stripped text is still not the heading, it is a heading
    mentioned in prose after the real (possibly malformed) one."""
    stripped = _FENCE.sub("", body).lstrip()
    return pattern.match(stripped) is not None


def _exact_heading_pattern(label: str, repo: str, issue: int) -> re.Pattern[str] | None:
    """a private-repo incident: the exact `## <label> — <PREFIX><N>` form
    `harmonic-forge/rules/testing-gate.md` and the `lane3-gate` skill make
    mechanically required on the READ side (Lane 3 refuses to execute
    without it) -- this builds the matching check for the WRITE side, so a
    malformed heading is refused before posting instead of only caught
    downstream. Returns None when `repo` has no known prefix (an
    unrecognized/new repo): callers fall back to the loose keyword-only
    check rather than refusing every post from a repo this table doesn't
    yet know, which would be a worse failure than a loose heading check.

    Anchored at the start of the (fence-stripped) body via
    `_heading_matches_exact`'s `re.match` -- NOT `(?m)^...` -- so a
    correctly-formed heading quoted later in the body (prose explaining
    what the heading should look like, a fenced example, a prior round's
    heading pasted as context) cannot satisfy this check in place of the
    comment's own actual, possibly-malformed, first line."""
    prefix = _repo_prefix(repo)
    if prefix is None:
        return None
    # Case-sensitive on `label`/"Gate-readiness sweep" and the em-dash --
    # these are the exact literals testing-gate.md's own canonical examples
    # use. Trailing content after `<PREFIX><N>` (e.g. "H1925 (respec, ...)")
    # is allowed; `\b` stops the number from silently absorbing a suffix
    # digit of an unrelated, larger issue number.
    return re.compile(rf"#{{1,4}}\s*{re.escape(label)}\s*—\s*{re.escape(prefix)}{issue}\b")


def validate_ae(body: str, repo: str, issue: int) -> None:
    """a private-repo incident: AE previously went out via plain `lane-comment`, with no
    reserved-marker footer and no structural check -- indistinguishable from
    any other discussion comment. `lane3-begin`'s readiness check (a private-repo incident)
    needs to find "the most recent AE" and "the most recent sweep" for an
    issue by scanning comment footers, the same way it already finds sweeps;
    AE needs the same footer to be findable at all."""
    if not AE_HEADING.search(body):
        fail("AE body must contain a heading starting with 'AE' (e.g. '## AE — H<N>')")
    exact = _exact_heading_pattern("AE", repo, issue)
    if exact is not None and not _heading_matches_exact(exact, body):
        fail(
            f"AE heading must be exactly '## AE — {_repo_prefix(repo)}{issue}' "
            "(trailing text after the number is fine) -- Lane 3's own "
            "readiness check refuses anything looser (a private-repo incident)"
        )
    if not is_substantive(body.strip()):
        fail("AE body is a template placeholder")


SWEEP_HEADING = re.compile(r"(?im)^#{1,4}\s*Gate-readiness sweep\b")


def validate_sweep(body: str, spec_body: str, repo: str, issue: int) -> None:
    """a private-repo incident: `harmonic-forge/rules/testing-gate.md` (a private-repo incident) makes the
    `## Gate-readiness sweep -- <PREFIX><N>` heading mechanically required --
    Lane 3 refuses to execute without it -- but this validator had no check
    for it at all, so a headerless sweep posted successfully and was only
    caught downstream by manual thread inspection. a private-repo incident tightened this
    from a loose keyword-only match to the exact prefix/issue-number form,
    now that `repo`/`issue` are threaded through (see `_exact_heading_pattern`)."""
    if not SWEEP_HEADING.search(body):
        fail(
            "sweep body must contain a heading starting with 'Gate-readiness "
            "sweep' (e.g. '## Gate-readiness sweep — H<N>', a private-repo incident)"
        )
    exact = _exact_heading_pattern("Gate-readiness sweep", repo, issue)
    if exact is not None and not _heading_matches_exact(exact, body):
        fail(
            f"sweep heading must be exactly '## Gate-readiness sweep — "
            f"{_repo_prefix(repo)}{issue}' (trailing text after the number is "
            "fine) -- Lane 3's own readiness check refuses anything looser "
            "(a private-repo incident)"
        )
    expected = case_ids(spec_body)
    actual = case_ids(body)
    if not expected:
        # a private-repo incident: the error used to assert "no identifiable test cases"
        # even when the parser had isolated a real section and simply found
        # nothing in it -- pointing the reader at the spec's contents when
        # the defect was in the parser's section isolation. Name what was
        # actually isolated so the two failure modes are distinguishable.
        section, has_heading = _case_section(spec_body)
        if has_heading:
            fail(
                "referenced Lane 3 spec's isolated test-case section has no "
                "identifiable cases (expected TC<n> identifiers or a "
                f"numbered list). Isolated section starts: {section.strip()[:200]!r}"
            )
        fail(
            "referenced Lane 3 spec has no identifiable test cases: expected "
            "either TC<n> identifiers or a numbered list under a '### Test "
            "cases' heading (see templates/hitl-test-review.md)"
        )
    if expected != actual:
        fail(f"sweep case IDs must exactly match spec (expected {sorted(expected, key=int)}, got {sorted(actual, key=int)})")
    # a private-repo incident: NO per-case *outcome* is required -- a gate-readiness sweep is a
    # PRE-execution artifact (`3-lane-protocol.md`), so no case has run and
    # neither `pass` nor `fail` is true. Requiring one could only be satisfied by
    # fabricating gate results inside the artifact that attests the gate is ready
    # to *start*; a private-repo incident and a private-repo incident were both parked on exactly that.
    #
    # But per-case *structure* is still required, and is enforced below rather
    # than inferred from the ID-set check above. `testing-gate.md` calls for "a
    # structured checklist ... one line per TC ... not a prose paragraph", and
    # the ID-set check cannot deliver that for the TC scheme: `TC_ID` is
    # unanchored, so N mentions in one sentence satisfy it. `LIST_ITEM` is
    # line-anchored, so the list scheme always did carry real entries -- the two
    # schemes are made consistent here, in the strong direction.
    spec_uses_tc_scheme = _classify_case_ids(spec_body)[1]
    sweep_section, _ = _case_section(body)

    fabricated = SWEEP_FABRICATED_OUTCOME.search(sweep_section)
    if fabricated:
        fail(
            "sweep states a test outcome (`pass`/`fail`), but a gate-readiness "
            "sweep is posted BEFORE the gate runs -- no case has an outcome yet. "
            "Describe readiness instead (`ready`, or `blocked` and why)."
        )
    # a private-repo incident: bitten three times -- check_lane3_ready.py already rejects a
    # sweep with no parseable [RWP] tier letter, but only after it is posted.
    # Reject it here too, before the GitHub write, using the same parser so
    # the two checks can never diverge.
    if parse_write_tier(body) is None:
        fail(f"sweep {NO_TIER_MESSAGE}")

    if spec_uses_tc_scheme:
        entries = SWEEP_TC_ENTRY.findall(sweep_section)
        named = {number for number, _ in entries}
        if named != expected:
            fail(
                "sweep needs one line per case, each naming its own TC -- found "
                f"entries for {sorted(named, key=int) or 'none'}, expected "
                f"{sorted(expected, key=int)}. Mentioning the identifiers in prose "
                "is not an entry (testing-gate.md: a structured checklist, not a "
                "prose paragraph)."
            )
        empty = [number for number, text in entries if not is_substantive(text.strip())]
        if empty:
            fail(f"sweep entries carry no text: TC{', TC'.join(sorted(set(empty), key=int))}")
    else:
        empty_list = [
            index for index, text in enumerate(SWEEP_LIST_ENTRY.findall(sweep_section), start=1)
            if not is_substantive(text.strip())
        ]
        if empty_list:
            fail(f"sweep entries carry no text: item(s) {empty_list}")


def resolve_project_board(repo: str, cwd: Path) -> tuple[str, str] | None:
    """Resolve the target repo's board from the canonical project manifest.

    `cwd` remains in the signature for compatibility with callers and tests,
    but board identity belongs to the target repo, not to whichever checkout
    launched the transport. Requiring the onboarded manifest entry also makes
    a missing checkout distinguishable from a repo that intentionally has no
    board: checkout location is irrelevant to this decision.
    """
    del cwd
    try:
        return require_onboarded_repo(repo).board
    except ManifestError as exc:
        fail(str(exc))


def resolve_board_tier(repo: str, owner: str, number: str, issue_number: int) -> str | None:
    # harmonic-forge#219: this read must see a board write that may have
    # happened moments earlier (the operator/Lane 2 just set Estimate), so it
    # never caches. That requirement is unchanged.
    #
    # a private-repo incident: it used to satisfy it by fetching the ENTIRE board (limit=1000,
    # ttl=0) and linearly scanning for one issue number -- hundreds of GraphQL
    # complexity points to read a single integer, on every `l1-post --kind
    # handoff`. Now a targeted per-issue query, which is still live (a targeted
    # live query is no less live than a live scan) but costs roughly one unit.
    # harmonic-forge#257: reads Tier, the model-routing signal. The shared
    # helper prefers the Tier field and falls back to deriving one from a legacy
    # numeric Estimate, so this is correct against a migrated or unmigrated
    # board, in either order.
    if _item_list_cache is not None and hasattr(_item_list_cache, "fetch_issue_tier"):
        try:
            return _item_list_cache.fetch_issue_tier(
                repo, issue_number, number, run=lambda cmd: run(*cmd),
            )
        except _item_list_cache.GhItemListError as exc:
            fail(f"cannot read Tier for {repo}#{issue_number} on board {owner}/{number}: {exc}")
    # Fallback for a checkout whose harmonic-forge sibling predates #802 (or is
    # absent entirely). Deliberately the old full-board scan: correctness first,
    # cost second -- a stale sibling must still gate correctly, just expensively.
    result = run("gh", "project", "item-list", number, "--owner", owner,
                 "--limit", "1000", "--format", "json")
    if result.returncode != 0:
        fail(f"cannot fetch project board {owner}/{number} to verify Estimate: " + result.stderr.strip())
    try:
        items = json.loads(result.stdout)["items"]
    except (json.JSONDecodeError, KeyError):
        fail(f"unexpected response shape from project board {owner}/{number}")
    for item in items:
        content = item.get("content") or {}
        if content.get("number") == issue_number:
            tier = item.get("tier")
            if isinstance(tier, str) and tier.strip():
                return tier.strip().lower()
            est = item.get("estimate")
            if not isinstance(est, (int, float)):
                return None
            # Same boundary as the retired THRESHOLD=8.
            return "deep" if est >= 8 else "standard" if est >= 5 else "fast"
    return None  # issue not on the board at all -- treat like an unset tier


def validate_plan_first_declared(value: str | None) -> None:
    """a private-repo incident: a handoff must DECLARE Plan-First; it may not be inferred.

    A hard refusal rather than a default, on `validate_tier_set`'s precedent
    directly above: an optional field silently falls back to the legacy
    body-parsing path forever, which is the drift this design exists to close.
    Every handoff author answering the question is the intended cost.

    The reason no default is honest: R-0244 has three triggers and only the
    first is in the handoff text. Trigger 2 asks whether the implementation's
    own operation mutates git state or live data; trigger 3 is an operator
    sentence in chat. Defaulting either way asserts an answer to questions the
    defaulting code cannot see, and the expensive direction -- a wrong
    "not Plan-First" -- lets Lane 2 implement without plan review, which is
    ADR-005 / a private-repo incident verbatim.
    """
    if value in ("true", "false"):
        return
    fail(
        "a handoff must declare --plan-first true|false (a private-repo incident). "
        "R-0244: Plan-First is required when Delegated Judgment Calls is "
        "anything other than 'none', OR the implementation's own operation "
        "mutates git state or live data, OR HITL said 'Plan-first #N'. The "
        "last two are not in the handoff body and cannot be recovered from "
        "it later -- which is why this is declared here rather than inferred "
        "downstream."
    )


def validate_tier_set(repo: str, issue: int) -> None:
    """a private-repo incident: a handoff must not post if the target issue's board
    Tier field is unset (harmonic-forge#257 — was Estimate) -- .claude/rules/planning.md already requires
    it in prose, but harmonic-forge#202's model-tier gate silently no-ops
    without it (a missing estimate reads identically to "below 8 points").
    Found live 2026-08-09: a private-repo incident and a private-repo incident both had handoffs posted
    with a stated point estimate that was never written to the board.
    No-ops entirely on a repo with no project board configured (e.g.
    vitalharmony/openclaw-projects today) -- this gate only applies where
    a board exists to enforce against."""
    board = resolve_project_board(repo, Path.cwd())
    if board is None:
        return
    owner, number = board
    tier = resolve_board_tier(repo, owner, number, issue)
    if tier is None:
        fail(
            f"{repo}#{issue} has no board Tier set on project {owner}#{number} -- "
            "required before a handoff can post (.claude/rules/planning.md; "
            "harmonic-forge#202's model-tier gate silently no-ops without it, "
            "and a missing Tier reads identically to 'does not escalate'). "
            "Set it with `gh_issue.py --tier fast|standard|deep`, or via the "
            "board UI. fast = 1-3 pts, standard = 5, deep = 8+ (deep is the "
            "only tier that requires the high-tier model)."
        )


def repo_milestone_titles(repo: str) -> list[str]:
    """Every open milestone title on `repo`, or [] if it uses none.

    harmonic-forge#283 NC1: a failed query fails LOUD. An auth or network
    error must never be indistinguishable from "this repo genuinely has no
    milestones" -- that would silently turn `validate_milestone_set()` into a
    no-op at exactly the moment the check matters, which is harmonic-forge#263's
    class of defect. Mirrors resolve_board_tier's own fail-on-fetch-error
    behaviour (`:283-284`)."""
    result = run("gh", "api", f"repos/{repo}/milestones",
                 "-X", "GET", "-f", "state=open", "--jq", ".[].title")
    if result.returncode != 0:
        fail(f"cannot read milestones for {repo}: {result.stderr.strip()}")
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def issue_milestone_title(repo: str, issue: int) -> str | None:
    result = run("gh", "api", f"repos/{repo}/issues/{issue}", "--jq", ".milestone.title // \"\"")
    if result.returncode != 0:
        fail(f"cannot read milestone for {repo}#{issue}: {result.stderr.strip()}")
    title = result.stdout.strip()
    return title or None


def validate_milestone_set(repo: str, issue: int) -> None:
    """harmonic-forge#283: a handoff must not post if the target issue has no
    milestone -- the field is release membership, and an unset one is
    indistinguishable from a deliberate "not in any release."

    Structurally mirrors validate_tier_set() above, including its graceful
    no-op: a repo with no milestones at all (harmonic-forge, openclaw-projects)
    is not gated, because there is nothing to enforce against. Requiredness is
    derived from live state rather than a hardcoded per-repo list.

    Lazy population, not retroactive: only an issue actively entering a lane
    needs this decided, same philosophy as Tier's own documented rule."""
    titles = repo_milestone_titles(repo)
    if not titles:
        return
    if issue_milestone_title(repo, issue) is not None:
        return
    # NC4: because requiredness is derived live, the first milestone created in
    # a previously milestone-less repo switches this gate on for every existing
    # unmilestoned issue at once, with no lazy-population grace period. The fix
    # command is named inline so that is a one-line unblock rather than a
    # surprise mid-handoff.
    fail(
        f"{repo}#{issue} has no milestone set -- required before a handoff can "
        f"post (harmonic-forge#283: the milestone is release membership, and "
        f"unset reads identically to 'deliberately in no release'). "
        f"Set it with: gh api repos/{repo}/issues/{issue} -X PATCH "
        f"-F milestone=<number>, or via the issue UI. "
        f"Open milestones on {repo}: {', '.join(titles)}. "
        f"Use `Later` for work that is real but not yet placed in a release."
    )


def issue_is_open(repo: str, issue: int) -> None:
    # harmonic-forge#220: REST migration off GraphQL-backed `gh issue view`.
    # REST's `.state` is lowercase ("open"/"closed"), unlike GraphQL's
    # ("OPEN"/"CLOSED") -- confirmed live, 2026-08-11.
    result = run("gh", "api", f"repos/{repo}/issues/{issue}", "--jq", ".state")
    if result.returncode or result.stdout.strip() != "open":
        fail(f"issue {repo}#{issue} is not open")


# a private-repo incident: files whose presence in two branches carries no information about
# merge risk.
#   - `transaction-log.md` is a GENERATED VIEW, regenerated from `git log` by
#     `scripts/transaction_log.py` on demand (`mise run transaction-log-regen`)
#     -- a private-repo incident. The exclusion still holds, but for a DIFFERENT reason than
#     it did under the old append-on-every-`mise run commit` model, and the
#     distinction is load-bearing: that model was safe because EVERY branch
#     touched the file harmlessly, so co-modification was pure noise. This one
#     is safe because ALMOST NO branch touches it, and the rare one that does
#     writes a deterministic derivation of git history rather than an authorial
#     edit -- so a difference between two branches reflects which commits each
#     branch's HEAD can see, never competing human changes. Conflicts are still
#     resolved by regenerating, not by merging either side.
#   - `frontend/package.json`'s version line is rewritten by
#     `scripts/bump_version.py` on the same flow, resolved by taking the higher
#     value.
# Excluded whole-file rather than line-scoped: safe specifically because
# `bump_version.py` writes only `frontend/package.json` (its PACKAGE_JSON
# constant) and never `frontend/package-lock.json`, so a REAL dependency
# conflict between two branches still co-signals in the lock file and still
# blocks. Never add `frontend/package-lock.json` here -- that is the residual
# guard that makes excluding `package.json` safe at all (a private-repo incident AC5).
_MECHANICAL_PATHS = frozenset({"transaction-log.md", "frontend/package.json"})


def changed_files(base: str, ref: str, cwd: Path | None = None) -> set[str]:
    result = run("git", "diff", "--name-only", base, ref, cwd=cwd)
    if result.returncode:
        fail(f"cannot inspect changed files for {ref!r}")
    return {line for line in result.stdout.splitlines() if line}


def _already_upstream(
    sibling_sha: str, merge_base: str, upstream: str, *, cwd: Path | None = None
) -> bool:
    """Whether a sibling branch's work is already contained in `upstream`,
    including when it landed as a SQUASH commit.

    a private-repo incident: the obvious check -- `git merge-base --is-ancestor` -- is wrong
    for this repo. A squash merge writes a brand-new single-parent commit with
    no ancestry link to the branch it came from, so `--is-ancestor` returns
    false for every branch merged under the standing PR flow. Measured against
    all five of the issue's incident branches: `--is-ancestor` misses every one
    (e.g. `fix/913-delete-activity-mixin`, merged as `3fed649`, whose parent is
    `f4d290e` -- a squash, not a merge).

    Instead, build a throwaway commit of the branch's own tree on its own
    merge-base and ask whether THAT patch is already upstream. `git cherry`
    prefixes `-` when an equivalent patch exists upstream and `+` when it does
    not. Per-commit `git cherry` on the branch itself is not sufficient: a
    multi-commit branch squashed into one has no individual commit whose
    patch-id matches, so it reports `+` for work that is fully merged.

    Fails safe: any git error yields empty stdout, so this returns False and
    the sibling is still compared -- a false block, never a false pass.
    """
    # Merge-commit / fast-forward shape. Kept as a fast path, not as the whole
    # mechanism: for a true ancestor, merge_base == sibling_sha, so the probe
    # below would be an EMPTY commit and `git cherry` reports `+` for it (an
    # empty patch matches nothing upstream). Without this branch the predicate
    # returns False for plainly-merged branches -- harmless in aggregate, since
    # an ancestor's `changed_files()` is empty and cannot overlap anything, but
    # it would make the predicate lie about what it measures and let the
    # merge-commit test pass vacuously.
    if run("git", "merge-base", "--is-ancestor", sibling_sha, upstream, cwd=cwd).returncode == 0:
        return True
    tree = run("git", "rev-parse", f"{sibling_sha}^{{tree}}", cwd=cwd).stdout.strip()
    if not tree:
        return False
    probe = run("git", "commit-tree", tree, "-p", merge_base, "-m", "probe", cwd=cwd).stdout.strip()
    if not probe:
        return False
    return run("git", "cherry", upstream, probe, cwd=cwd).stdout.strip().startswith("-")


def sibling_overlaps(
    target_files: set[str],
    siblings: Iterable[str],
    *,
    upstream: str = "origin/main",
    exclude: frozenset[str] = _MECHANICAL_PATHS,
    cwd: Path | None = None,
) -> list[tuple[str, set[str]]]:
    """(branch, overlapping_paths) for every sibling that genuinely conflicts.

    a private-repo incident: extracted out of `world_checks()` so the overlap comparison is
    testable at all. `world_checks()` calls live GitHub/network state before
    ever reaching this logic, so the comparison could not be exercised
    end-to-end; this function is free of GitHub and network calls (the git
    plumbing runs for real, against a temp repo, in its tests).

    Raises nothing -- the caller decides how to fail, which is what keeps this
    assertable without catching SystemExit.
    """
    conflicts: list[tuple[str, set[str]]] = []
    for sibling in siblings:
        if sibling == "main":
            continue
        sibling_sha = resolve_sha(sibling, cwd=cwd)
        sibling_base = run("git", "merge-base", upstream, sibling_sha, cwd=cwd)
        if sibling_base.returncode:
            continue
        merge_base = sibling_base.stdout.strip()
        if _already_upstream(sibling_sha, merge_base, upstream, cwd=cwd):
            continue
        overlap = (target_files & changed_files(merge_base, sibling_sha, cwd=cwd)) - exclude
        if overlap:
            conflicts.append((sibling, overlap))
    return conflicts


def active_worktree_branches() -> set[str]:
    result = run("git", "worktree", "list", "--porcelain")
    if result.returncode:
        fail("cannot inspect sibling worktrees")
    return {
        line.removeprefix("branch refs/heads/")
        for line in result.stdout.splitlines()
        if line.startswith("branch refs/heads/")
    }


def validate_comment_target(url: str, repo: str, issue: int) -> int:
    parsed = urlparse(url)
    expected_path = f"/{repo}/issues/{issue}"
    comment = re.search(r"(?:^|&)issuecomment-(\d+)(?:&|$)", parsed.fragment)
    if (
        parsed.scheme != "https"
        or parsed.netloc != "github.com"
        or parsed.path != expected_path
        or not comment
    ):
        fail(f"posted comment target {url!r} does not match requested {repo}#{issue}")
    return int(comment.group(1))


def world_checks(
    repo: str, issue: int, sha: str, branch: str, *, ack_overlap: str | None = None
) -> tuple[list[str], list[str]]:
    """Run non-cacheable readiness checks immediately before publication.

    Returns (checks, warnings) -- warnings is durable text the caller appends
    into the posted comment body itself (a private-repo incident: the override must be part
    of the record on the thread, not just a CLI argument that leaves no trace).
    """
    issue_is_open(repo, issue)
    remote = run("git", "ls-remote", "--heads", "origin", branch)
    if remote.returncode or not remote.stdout.split():
        fail(f"origin does not have branch {branch!r}")
    if remote.stdout.split()[0] != sha:
        fail(f"origin/{branch} no longer matches the attested SHA")
    target_base = run("git", "merge-base", "origin/main", sha)
    if target_base.returncode:
        fail("cannot calculate target merge-base")
    target_files = changed_files(target_base.stdout.strip(), sha)
    warnings: list[str] = []
    for sibling, overlap in sibling_overlaps(
        target_files, active_worktree_branches() - {branch, "main"}
    ):
        message = f"active sibling branch {sibling!r} overlaps target files: {', '.join(sorted(overlap))}"
        if ack_overlap is None:
            fail(message)
        warnings.append(f"- {message} -- acknowledged: {ack_overlap}")
    return ["issue-open", "origin-branch-sha-match", "active-worktree-overlap"], warnings


_REMOTE_URL = re.compile(
    r"^(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)"
    r"(?P<repo>[^/]+/[^/]+?)(?:\.git)?$"
)


def _cwd_repo_from_git(cwd: Path | None) -> str | None:
    """`owner/name` for the repo `cwd` sits in, from git alone -- no API call.

    harmonic-forge#220's REST-first policy applied to this function's own
    repo-resolution half. `gh repo view` is a GraphQL call, and GraphQL
    rate-limits by query POINT COST rather than call count: a session that ran
    a few expensive Projects V2 / `timelineItems` queries can find EVERY
    GraphQL call refused while REST keeps working normally (confirmed live,
    2026-09-22 -- `gh api rate_limit` reported `graphql: used 0, remaining
    5000` while raw `curl` to the GraphQL endpoint returned HTTP 200 with a
    `RATE_LIMIT` error body). That took `l1_post.py` down entirely: this check
    runs before `--ack-no-pr-required` is consulted, so no override reached
    it, and no AE/sweep/handoff could be posted at all while a Lane 3 sat
    blocked waiting for one.

    Git is the right source here anyway, not merely the available one. The
    worktree unreliability the docstring below records is specifically about
    `gh`'s own implicit repo *detection*; `git remote get-url origin` reads
    the worktree's shared config directly and has no such ambiguity
    (confirmed live from two separate worktrees of two different repos).
    Returns None rather than failing, so the caller can fall back.
    """
    remote = run("git", "remote", "get-url", "origin", cwd=cwd)
    if remote.returncode or not remote.stdout.strip():
        return None
    match = _REMOTE_URL.match(remote.stdout.strip())
    return match.group("repo") if match else None


def _open_prs_via_rest(cwd_repo: str, branch: str, cwd: Path | None) -> list[dict] | None:
    """Open PRs for `branch` against main, over REST. None = the call failed.

    `head` is `owner:branch`, and the owner is taken from `cwd_repo` rather
    than assumed: a same-repo branch (every case this protocol produces) has
    the repo's own owner, and passing it explicitly keeps the filter correct
    rather than relying on GitHub's default.
    """
    head = f"{cwd_repo.split('/')[0]}:{branch}"
    result = run("gh", "api",
                 f"repos/{cwd_repo}/pulls?head={head}&base=main&state=open",
                 cwd=cwd)
    if result.returncode:
        return None
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    # REST's `state` is lowercase where GraphQL's is upper; normalise to the
    # caller's existing GraphQL-shaped expectation rather than teaching the
    # caller two vocabularies.
    return [{"number": pr.get("number"), "state": str(pr.get("state", "")).upper()}
            for pr in payload] if isinstance(payload, list) else None


def require_open_pr(
    repo: str, branch: str, *, ack_no_pr_required: str | None = None, cwd: Path | None = None,
) -> tuple[list[str], list[str]]:
    """a private-repo incident: `ready-for-l3`/`ae` must not be postable for a branch with
    no open PR against `main`.

    Every consuming repo's CI workflow (confirmed live, `harmonic-forge` and
    `HRSE2` both) triggers only on `pull_request` or `push` to `main` -- a
    feature-branch push alone guarantees zero check runs on the attested SHA.
    Lane 3's own R-0354 then mechanically (and correctly) returns BLOCKED on
    that empty check-run list, bouncing back to Lane 1 to open the PR after
    the fact -- confirmed live three times in one session (a private-repo incident, a private-repo incident,
    a private-repo incident) before this closed the gap at its source.

    **`repo` (the GitHub issue's repo) is deliberately NOT what this queries
    against** (preclose finding). A cross-repo attestation (`_source_repo_is_hrse`'s
    own docstring, a private-repo incident) can name `--repo vitalharmony/harmonic-forge`
    for an issue whose attested branch/PR live in `vitalharmony/hrse` -- `gh pr
    list --repo <issue repo>` would then return `[]` for a PR that is genuinely
    open, elsewhere. The actual source-of-truth repo is always the one `cwd`
    sits in, never `--repo`.

    a private-repo incident: `gh pr list --head <branch> --base main` with NO `--repo`
    silently returned `[]` for a real, open, matching PR when run from a git
    *worktree* (confirmed live) -- `gh`'s own implicit repo detection for
    `pr list` is not as reliable as `gh repo view`'s, which correctly resolves
    the same worktree. So the repo is resolved explicitly via `gh repo view`
    (cwd-based, same source of truth as intended, just a reliable path to it)
    and passed to `pr list` as `--repo`, rather than leaving `gh` to infer it.

    Returns (checks, warnings), matching `world_checks`' shape: warnings is
    durable text the caller appends into the posted comment body itself
    (mirroring `--ack-overlap`'s a private-repo incident pattern) so an override leaves a
    trace on the thread, never just a CLI argument that leaves none.
    """
    # harmonic-forge#220, REST first, GraphQL only as a fallback. Both halves
    # of this check -- resolving the repo and listing its PRs -- used to be
    # GraphQL-backed (`gh repo view`, `gh pr list`), which made the whole
    # posting transport unusable whenever GraphQL alone was throttled. See
    # `_cwd_repo_from_git` for the incident.
    prs: list[dict] | None = None
    cwd_repo = _cwd_repo_from_git(cwd)
    if cwd_repo is not None:
        prs = _open_prs_via_rest(cwd_repo, branch, cwd)

    if prs is None:
        # Fallback: the original GraphQL path, unchanged. Reached when the
        # remote is not a recognised github.com URL, or REST itself failed --
        # never silently, so a genuinely broken check still fails loudly below.
        repo_view = run("gh", "repo", "view", "--json", "nameWithOwner",
                         "-q", ".nameWithOwner", cwd=cwd)
        if repo_view.returncode or not repo_view.stdout.strip():
            fail("cannot resolve the current repo (git remote unusable, and "
                 "'gh repo view' failed): "
                 + (repo_view.stderr.strip() or "empty output"))
        cwd_repo = repo_view.stdout.strip()
        result = run("gh", "pr", "list", "--repo", cwd_repo, "--head", branch,
                     "--base", "main", "--json", "number,state", cwd=cwd)
        if result.returncode:
            fail("cannot check for an open PR: " + (result.stderr.strip() or "gh pr list failed"))
        try:
            prs = json.loads(result.stdout)
        except json.JSONDecodeError:
            fail("gh pr list returned unparseable output")
    if any(pr.get("state") == "OPEN" for pr in prs):
        return ["pr-open"], []
    message = (
        f"no open PR exists for {branch!r} against main -- CI will never run "
        f"on this SHA until one does. Open one first, from the branch's own "
        f"repo checkout (not necessarily {repo!r}, the issue's repo -- see "
        f"this function's own docstring): gh pr create --head {branch} "
        f"--base main --title '...' --body-file <path>"
    )
    if ack_no_pr_required is None:
        fail(message)
    return ["pr-open (acknowledged override)"], [f"- {message} -- acknowledged: {ack_no_pr_required}"]


HRSE_DEPENDENCY_DIRS = ("frontend/node_modules", "backend/.venv")


def _source_repo_is_hrse(repo_root: Path) -> bool:
    """Whether repo_root's own git remote is vitalharmony/hrse -- independent
    of --repo, which names the GitHub issue's repo, not the repo the
    attested commit actually lives in. a private-repo incident wrongly conflated the
    two: a harmonic-forge issue whose handoff targets HRSE2 code (e.g.
    harmonic-forge#152, fixing HRSE2's scripts/gate_codex_tool.py) has
    --repo=vitalharmony/harmonic-forge while the sha/branch being verified
    are hrse's -- gating the symlink loop on --repo silently skipped it for
    exactly that case, breaking the frontend lint/test/build check with a
    misleading "eslint: command not found" instead of a clear signal."""
    remote = run("git", "remote", "get-url", "origin", cwd=repo_root)
    if remote.returncode != 0:
        return False
    normalized = remote.stdout.strip().rstrip("/").removesuffix(".git")
    # Boundary-aware: matches both HTTPS ("https://github.com/vitalharmony/hrse")
    # and SSH scp-like ("git@github.com:vitalharmony/hrse") remote forms,
    # requiring the GitHub host boundary immediately before "vitalharmony/hrse"
    # so a nested path can't false-positive via a matching suffix.
    return bool(re.search(r"github\.com[:/]vitalharmony/hrse$", normalized))


def refresh_main() -> str:
    """Fetch `origin/main` and return its SHA. Fails closed (a private-repo incident).

    **The race this closes.** `static_checks` below has always asserted that
    the attested SHA is based on current `origin/main` — but it read the LOCAL
    remote-tracking ref, which is only as fresh as whoever last fetched. So a
    merge landing between Lane 2's rebase and Lane 1's `ready-for-l3` was
    invisible: the check passed against a stale ref and authorized a gate on a
    base that was already behind. `l1_post.py` performed no fetch at all
    before this (verified: zero occurrences).

    Fetching immediately before the ancestry test is what makes the gap "no
    more than one atomic check" — the issue's own acceptance criterion. It
    cannot stop `main` moving; it makes a stale base *detected* rather than
    silently accepted, which is the half a tool can actually own.

    **Fails rather than degrading.** A fetch that cannot run leaves currency
    unestablished, and this function exists precisely to stop an
    unestablished base being treated as a verified one. That is the same
    posture every other check in this module takes.
    """
    fetched = run("git", "fetch", "origin", "main")
    if fetched.returncode:
        fail(
            "cannot fetch origin/main, so the attested SHA's base cannot be "
            "confirmed current: " + (fetched.stderr.strip() or "git fetch failed")
        )
    resolved = run("git", "rev-parse", "FETCH_HEAD")
    if resolved.returncode:
        fail("cannot resolve FETCH_HEAD after fetching origin/main")
    return resolved.stdout.strip()


def static_checks(sha: str, branch: str) -> list[str]:
    branch_sha = resolve_sha(branch)
    if branch_sha != sha:
        fail(f"branch {branch!r} no longer resolves to the attested SHA")
    # a private-repo incident: fetched HERE, immediately before the ancestry test, so the
    # two are one action. Any merge landing after this point is R-0209's
    # carry-forward case, not this check's.
    main_sha = refresh_main()
    if run("git", "merge-base", "--is-ancestor", main_sha, sha).returncode:
        fail(
            f"attested SHA is not based on current origin/main ({main_sha[:12]}) "
            f"-- main moved since {branch!r} was rebased. Ask Lane 2 to rebase "
            f"onto {main_sha[:12]} and post ready-for-l3 in the same action as "
            f"confirming it, so this cannot drift again."
        )
    repo_root_result = run("git", "rev-parse", "--show-toplevel")
    if repo_root_result.returncode:
        fail("cannot resolve the source worktree root")
    repo_root = Path(repo_root_result.stdout.strip())
    scratch = Path(tempfile.mkdtemp(prefix="hrse-l1-post-"))
    check_tmp: Path | None = None
    try:
        added = run("git", "worktree", "add", "--detach", str(scratch), sha)
        if added.returncode:
            fail("cannot create detached check worktree: " + added.stderr.strip())
        if resolve_sha("HEAD", cwd=scratch) != sha:
            fail("check worktree HEAD changed before validation")
        if _source_repo_is_hrse(repo_root):
            for dependency_dir in HRSE_DEPENDENCY_DIRS:
                source = repo_root / dependency_dir
                if not source.is_dir():
                    fail(f"source worktree dependency directory is missing: {dependency_dir}")
                (scratch / dependency_dir).symlink_to(source, target_is_directory=True)
        # a private-repo incident: `git worktree add --detach` never provisions `.claude/`
        # (an untracked, locally-linked directory in every repo this tool
        # runs against) -- a repo whose own `mise run check` self-verifies
        # that linkage (harmonic-forge does; HRSE2 as a consumer does not)
        # then fails unconditionally in this scratch worktree, regardless of
        # the actual code change being attested. Best-effort: some repos may
        # not declare this task at all, and a failure here must not become a
        # NEW way for this function to wrongly refuse a genuinely good SHA --
        # `mise run check` immediately below remains the actual gate either way.
        run("mise", "run", "hooks-install", cwd=scratch)
        # `hooks-install` deliberately refuses to touch a skill symlink that
        # already points somewhere outside its own platform skills dir
        # (correct for a real checkout, where that means a deliberate local
        # override) -- but in a freshly created scratch worktree there is no
        # such override, only a stale link inherited from wherever `git
        # worktree add` happened to leave it. Force-relink any skill symlink
        # still pointing outside `scratch` after the best-effort install above.
        skills_dir = scratch / ".claude" / "skills"
        if skills_dir.is_dir():
            for link in skills_dir.iterdir():
                if not link.is_symlink():
                    continue
                target = link.resolve()
                if scratch not in target.parents and target != scratch:
                    real = scratch / "skills" / link.name
                    if real.exists():
                        link.unlink()
                        link.symlink_to(real, target_is_directory=True)
        check_tmp, check_env = _private_check_tmp()
        checked = run("mise", "run", "check", cwd=scratch, env=check_env)
        if checked.returncode:
            fail("static verification failed:\n" + checked.stdout + checked.stderr)
        clean = run("git", "status", "--porcelain", cwd=scratch)
        if clean.returncode or clean.stdout.strip():
            fail("verification left the detached worktree dirty")
    finally:
        run("git", "worktree", "remove", "--force", str(scratch))
        shutil.rmtree(scratch, ignore_errors=True)
        if check_tmp is not None:
            shutil.rmtree(check_tmp, ignore_errors=True)
    return ["mise-check", "origin-main-ancestor", "branch-sha-match", "clean-worktree"]


def comment_body(repo: str, issue: int, body: str) -> tuple[str, int]:
    # harmonic-forge#220: REST migration off GraphQL-backed `gh issue
    # comment`. REST's `.html_url` on the created comment resource is
    # confirmed live to match the same `https://github.com/{repo}/issues/
    # {issue}#issuecomment-{id}` shape `validate_comment_target` already
    # parses -- no downstream change needed beyond the fetch itself.
    with tempfile.NamedTemporaryFile("w", suffix=".md", encoding="utf-8", delete=False) as handle:
        handle.write(body)
        filename = handle.name
    try:
        posted = run("gh", "api", f"repos/{repo}/issues/{issue}/comments",
                     "-F", f"body=@{filename}", "--jq", ".html_url")
    finally:
        Path(filename).unlink(missing_ok=True)
    if posted.returncode:
        fail("GitHub comment failed: " + posted.stderr.strip())
    url = posted.stdout.strip()
    comment_id = validate_comment_target(url, repo, issue)
    fetched = run("gh", "api", f"repos/{repo}/issues/comments/{comment_id}", "--jq", ".body")
    if fetched.returncode or fetched.stdout.rstrip("\n") != body.rstrip("\n"):
        fail("posted comment did not match the attested body")
    return url, comment_id


def post_kind(
    repo: str, issue: int, kind: str, body: str, sha: str, branch: str,
    *, ack_overlap: str | None = None, is_handoff_extra_checks: bool = False,
    plan_first: bool | None = None, ack_no_pr_required: str | None = None,
) -> tuple[str, int]:
    """Run world_checks, build the footer, post, and write the receipt for
    ONE already-validated claim. Shared by the single-kind path and
    harmonic-forge#381's `ae-and-sweep` (each of its two posts calls this
    once) so the two paths cannot silently diverge in what gets checked or
    recorded."""
    checks = static_checks(sha, branch) if kind == "ready-for-l3" else ["body-validation"]
    if is_handoff_extra_checks:
        checks.append("tier-set")
    world_check_names, overlap_warnings = world_checks(repo, issue, sha, branch, ack_overlap=ack_overlap)
    checks += world_check_names
    # a private-repo incident: only the two kinds Lane 3 actually gates against CI need a
    # PR to exist first -- `handoff`/`rework` precede any gate, and `sweep`
    # (standalone) always follows an `ae` on the same SHA that already
    # required one.
    pr_warnings: list[str] = []
    if kind in ("ready-for-l3", "ae"):
        pr_check_names, pr_warnings = require_open_pr(repo, branch, ack_no_pr_required=ack_no_pr_required)
        checks += pr_check_names
    # Two distinct override classes, two distinct headings -- a reader
    # auditing the thread for one kind of waiver must not find the other's
    # warning filed under a heading that names something that never
    # happened (preclose finding, a private-repo incident: a no-PR override rendered
    # under "Sibling-overlap override" asserted an overlap that was never
    # acknowledged, or even present).
    if overlap_warnings:
        body = body.rstrip("\n") + "\n\n### Sibling-overlap override (operator-acknowledged)\n" + "\n".join(overlap_warnings) + "\n"
    if pr_warnings:
        body = body.rstrip("\n") + "\n\n### No-open-PR override (operator-acknowledged)\n" + "\n".join(pr_warnings) + "\n"
    # a private-repo incident: hash the rstripped body, not the raw one -- `comment_body()`
    # below posts `body.rstrip("\n") + footer`, so hashing `body` unstripped
    # recorded a digest that didn't correspond to what was actually posted
    # whenever the source had a different trailing-newline count than
    # exactly one. A verifier re-fetching the comment, stripping the footer,
    # and re-hashing could never reconstruct the original digest correctly --
    # a false tamper-mismatch on a sweep/AE that was never edited. Verified
    # live against a real posted comment before this fix; see a private-repo incident.
    digest = hashlib.sha256(body.rstrip("\n").encode()).hexdigest()
    # a private-repo incident: a handoff DECLARES whether it is Plan-First rather than
    # leaving it to be inferred from prose downstream. `lane_state.py` used to
    # decide with a whole-body `re.compile(r"Plan-First")` search, which
    # matched a private-repo incident's handoff -- a handoff whose own sentence was "this
    # does not need Plan-First. Implement directly."
    #
    # The deeper reason a body search can never be right: R-0244 has THREE
    # triggers and only the first is in the text. Trigger 2 is a fact about
    # the OPERATION (does it mutate git state or live data), trigger 3 lives
    # in operator chat. Both are knowable to the author at post time and to
    # nobody afterwards, so the derivation has to happen here.
    plan_first_field = ""
    if kind == "handoff":
        plan_first_field = f" plan-first={'true' if plan_first else 'false'};"
    footer = (f"\n\n<!-- l1-post v1; kind={kind};{plan_first_field} sha={sha}; "
              f"body-sha256={digest}; checks={','.join(checks)} -->\n")
    url, comment_id = comment_body(repo, issue, body.rstrip("\n") + footer)
    write_receipt({"version": 1, "repo": repo, "issue": issue, "kind": kind,
                   **({"plan_first": bool(plan_first)} if kind == "handoff" else {}),
                   "sha": sha, "branch": branch, "body_sha256": digest, "checks": checks,
                   "created_at": datetime.now(UTC).isoformat(), "comment_id": comment_id, "url": url})
    if kind == "handoff":
        _discharge_handoff_owed(repo, issue)
    return url, comment_id


def _discharge_handoff_owed(repo: str, issue: int) -> None:
    """Clear harmonic-forge#687's owed-handoff record for this issue.

    AC2: a posted handoff discharges the obligation for that issue **and
    only that issue**, which is why this is keyed on repo and number rather
    than clearing the posting session's whole file -- two issues filed in
    one turn are two independent obligations (AC5).

    Placed after `comment_body()` and `write_receipt()` on purpose: the
    obligation is discharged by a handoff that actually posted, never by an
    attempt. Both of those raise on failure, so reaching this line means the
    comment is live on the issue.

    Silent and optional, following the `item_list_cache` import above: this
    script has no hard dependency on ~/harmonic-forge existing, and a
    missing obligation store must never turn a successful handoff post into
    a failure. The cost of a missed discharge is one stale record that
    prunes itself in 7 days; the cost of raising here would be a handoff
    that posted and then reported an error.
    """
    if _handoff_owed is None:
        return
    try:
        _handoff_owed.discharge(repo, int(issue))
    except Exception:
        pass


def write_receipt(record: dict) -> None:
    root = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "harmonic-forge/l1-post"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = root / f"{record['issue']}-{record['comment_id']}.json"
    temp = target.with_suffix(".tmp")
    temp.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    temp.chmod(0o600)
    temp.replace(target)


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate and atomically post a Lane 1 claim")
    parser.add_argument("--repo")
    parser.add_argument("--issue", type=int, required=True)
    # a private-repo incident: `rework` is a first-class kind, not a `discussion` a reader
    # has to interpret. Lane 1 asking Lane 2 for more work on a branch (a
    # rebase, a correction) previously had no kind of its own, so it posted as
    # `discussion` -- which `lane_state._Markers.newest()` excludes by design,
    # leaving the row still naming Lane 3. Inferring it from a discussion
    # instead was measured at 0/63 precision against this repo's own history:
    # every post-`l2.done` discussion on record is a closing note, a merge
    # confirmation, or a gate sign-off, not a request for work.
    #
    # No mandated heading -- that part of the original scoping holds. But it
    # now DOES carry a LEAD_FIELDS entry (a private-repo incident): see that dict's own
    # comment for why the "over-reach" reading this note used to give was
    # narrower than it looked.
    parser.add_argument("--kind", choices=("handoff", "ready-for-l3", "sweep", "ae", "ae-and-sweep", "rework"), required=True)
    parser.add_argument("--sha", required=True)
    parser.add_argument("--branch", required=True)
    parser.add_argument("--file", type=Path, help="Body file for every --kind except ae-and-sweep")
    parser.add_argument("--ae-file", type=Path, help="AE body file, --kind ae-and-sweep only")
    parser.add_argument("--sweep-file", type=Path, help="Sweep body file, --kind ae-and-sweep only")
    parser.add_argument("--spec-comment", type=int)
    parser.add_argument("--mutates-live", action="store_true")
    # a private-repo incident. Deliberately a required tri-state rather than a `store_true`
    # flag: `--plan-first` absent would be indistinguishable from
    # `--plan-first false`, and "the author did not answer" is exactly the
    # state this field exists to make impossible.
    parser.add_argument(
        "--plan-first", choices=("true", "false"), default=None,
        help="REQUIRED on --kind handoff (a private-repo incident). Declares Plan-First per "
             "R-0244's three triggers: Delegated Judgment Calls is non-'none'; "
             "the operation mutates git state or live data; or HITL said "
             "'Plan-first #N'. Only you can answer triggers 2 and 3 -- they "
             "are not in the handoff text and cannot be recovered downstream.",
    )
    parser.add_argument("--cross-repo", action="store_true")
    parser.add_argument(
        "--ack-overlap", default=None, metavar="REASON",
        help="Non-empty human justification to downgrade a genuine sibling-worktree "
             "overlap from a hard fail to a durable warning appended to the posted "
             "comment (a private-repo incident). Never a bare boolean escape hatch -- applies "
             "uniformly across every --kind; the flag itself already requires "
             "explicit human judgment to invoke, so no further per-kind gating.",
    )
    parser.add_argument(
        "--ack-no-pr-required", default=None, metavar="REASON",
        help="Non-empty human justification to downgrade the missing-open-PR "
             "refusal (a private-repo incident) from a hard fail to a durable warning "
             "appended to the posted comment -- for the rare case CI is "
             "deliberately not required before this gate. Never a bare "
             "boolean escape hatch.",
    )
    args = parser.parse_args()
    if args.ack_no_pr_required is not None and not args.ack_no_pr_required.strip():
        fail("--ack-no-pr-required requires a non-empty reason")
    if args.kind in ("sweep", "ae-and-sweep") and not args.spec_comment:
        fail("--spec-comment is required for sweep / ae-and-sweep")
    if args.kind not in ("sweep", "ae-and-sweep") and args.spec_comment:
        fail("--spec-comment is valid only for sweep / ae-and-sweep")
    if args.ack_overlap is not None and not args.ack_overlap.strip():
        fail("--ack-overlap requires a non-empty reason")
    if args.kind == "ae-and-sweep":
        if args.file or not (args.ae_file and args.sweep_file):
            fail("--kind ae-and-sweep requires --ae-file and --sweep-file, not --file")
    elif not args.file or args.ae_file or args.sweep_file:
        fail(f"--kind {args.kind} requires --file, not --ae-file/--sweep-file")

    # Resolve checkout identity only after the purely local argument-shape
    # guards above. Invalid invocations must fail without consulting git or
    # the onboarding manifest; valid invocations still resolve before any
    # repository-sensitive work.
    args.repo = resolve_repo(args.repo)
    repo = args.repo
    sha = resolve_sha(args.sha)

    if args.kind == "ae-and-sweep":
        # harmonic-forge#381: AE and the gate-readiness sweep as one atomic
        # action -- four recorded incidents (a private-repo incident, a private-repo incident, H306/H308,
        # H231/H1221) of AE posted with the sweep never landing in the same
        # turn. Both bodies are read and validated BEFORE either is posted
        # (AC3): a malformed sweep must never leave a bare AE standing from
        # a validation failure alone.
        ae_body = regular_body(args.ae_file)
        sweep_body = regular_body(args.sweep_file)
        reject_reserved_marker(ae_body)
        reject_reserved_marker(sweep_body)
        validate_ae(ae_body, repo, args.issue)
        # harmonic-forge#472 on both halves, before either is posted: the
        # atomic-pair guarantee above is exactly why a lead check on only one
        # of them would be worse than none — a sweep refused after the AE
        # landed leaves the bare AE this branch exists to prevent.
        validate_lead("ae", ae_body)
        validate_lead("sweep", sweep_body)
        spec = run("gh", "api", f"repos/{repo}/issues/comments/{args.spec_comment}", "--jq", ".body")
        if spec.returncode:
            fail("cannot fetch referenced Lane 3 spec")
        validate_sweep(sweep_body, spec.stdout, repo, args.issue)

        ae_url, _ = post_kind(repo, args.issue, "ae", ae_body, sha, args.branch,
                              ack_overlap=args.ack_overlap, ack_no_pr_required=args.ack_no_pr_required)
        print(f"[l1-post] AE posted {ae_url}")
        record_queue_candidate(repo, args.issue, "ae")
        try:
            sweep_url, _ = post_kind(repo, args.issue, "sweep", sweep_body, sha, args.branch, ack_overlap=args.ack_overlap)
        except BaseException:
            # AC2: both bodies already passed validation together, so a
            # failure here is a posting-time fault (network, GitHub API,
            # comment-target mismatch, an interrupt, a full /tmp -- anything,
            # not just fail()'s SystemExit), not a content defect. Rolling
            # back the AE would delete a real, valid attestation from a
            # permanent record purely to paper over a transient failure.
            # Fail loud and explicit instead: the AE stands, the sweep is
            # owed, and this is the one state check_lane3_ready.py cannot
            # see on its own (an AE with no sweep at all looks identical
            # to "sweep not started yet").
            #
            # NOT advised here: resubmitting AE. Preclose review
            # (harmonic-forge#381) found the read-back verification inside
            # comment_body() can independently fail AFTER a successful POST
            # -- so "the sweep failed to post" is not always true; sometimes
            # it posted and only the verification failed. Suggesting a
            # second AE in that case creates a real duplicate that
            # check_lane3_ready.py's strict sweep>ae ordering then blocks on
            # (the new AE postdates the genuine sweep already on the
            # thread). Check the thread before acting, don't guess.
            print(
                f"[l1-post] AE posted ({ae_url}) but the sweep raised an error -- "
                "check the issue thread FIRST: the sweep comment may have "
                "actually posted and only its own verification failed (this "
                "is not always a transport failure). If no sweep comment is "
                "present, post one with `--kind sweep --spec-comment "
                f"{args.spec_comment}`. Do not resubmit the AE without "
                "checking -- a duplicate AE postdating a real sweep blocks "
                "check_lane3_ready.py.",
                file=sys.stderr,
            )
            raise
        print(f"[l1-post] sweep posted {sweep_url}")
        #: The candidate this leaves recorded is "sweep" -- the LAST kind
        #: posted in this atomic pair, and the one both QUEUE_KINDS["l3"]
        #: and a human reading the thread would treat as the current state
        #: of this issue's Lane 3 request. The "ae" record two lines above
        #: is superseded by this one on the same issue's single candidate
        #: file (harmonic-forge#691 AC3') the instant this line runs.
        record_queue_candidate(repo, args.issue, "sweep")
        return

    body = regular_body(args.file)
    reject_reserved_marker(body)
    if args.kind == "handoff":
        validate_handoff(body, args.mutates_live or args.cross_repo)
        # Order matters: `validate_plan_first_declared` refuses a handoff with
        # no declaration at all, so the cross-field rule below can treat
        # `args.plan_first` as a real answer rather than a tri-state.
        validate_plan_first_declared(args.plan_first)
        validate_plan_first_spec(body, args.plan_first == "true")
        validate_tier_set(repo, args.issue)
        validate_milestone_set(repo, args.issue)
    validate_lead(args.kind, body)
    if args.kind == "sweep":
        spec = run("gh", "api", f"repos/{repo}/issues/comments/{args.spec_comment}", "--jq", ".body")
        if spec.returncode:
            fail("cannot fetch referenced Lane 3 spec")
        validate_sweep(body, spec.stdout, repo, args.issue)
    if args.kind == "ae":
        validate_ae(body, repo, args.issue)

    url, _ = post_kind(
        repo, args.issue, args.kind, body, sha, args.branch,
        ack_overlap=args.ack_overlap, is_handoff_extra_checks=(args.kind == "handoff"),
        plan_first=(args.plan_first == "true"), ack_no_pr_required=args.ack_no_pr_required,
    )
    print(f"[l1-post] posted and refetched {url}")
    record_queue_candidate(repo, args.issue, args.kind)


if __name__ == "__main__":
    main()
