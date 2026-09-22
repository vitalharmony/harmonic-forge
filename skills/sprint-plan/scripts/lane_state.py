"""Where an issue sits in the 3-lane cycle, and the literal next trigger (hrse#1584).

Every row used to render `[Todo | Tier: standard]`. `Todo`/`In Progress` is
the board `Status` field — a Kanban column a human moves — and it cannot
separate four completely different next actions:

    no handoff, not startable         nothing to do yet
    handoff posted, awaiting trigger  `Implement H<N>` / `Plan H<N>`
    Lane 2 done, awaiting the gate    the gate runs
    gate FAILed, back with Lane 2     `Fix H<N>`

**No new bookkeeping.** Every signal is already a posted comment carrying a
machine-readable marker — `l1_post.py`'s `<!-- l1-post v1; kind=... -->`
footer, and the lane result headings. Writing lane state back to the board's
`Status` was the rejected alternative: it is the one field a human maintains,
and machine-writing it is how `Priority` decayed into confident falsehood
(hrse#839).

**Two marker contracts, because live comments use both** (read from
hrse#1573/#1565/#1441/#1575, 2026-09-04). Lane 1's posts carry the footer;
Lane 2's and Lane 3's carry *none* and must be matched on their heading:

    ## Handoff: H1573 — ...                        <!-- ... kind=handoff -->
    ## L2P — receipt-backed status                 (no footer)
    ## L1 — decisions, hrse#1573                   <!-- ... kind=discussion -->
    ## L2D — receipt-backed status                 (no footer)
    ## Lane 3 Gate Results — hrse#1573 — FAIL      (no footer)

**Nothing here costs an extra request.** The comments ride the
`gh issue list --json ...,comments` call the summary already makes. Measured
2026-09-04 on 171 open `hrse` issues: 1.46s/720KB without the field, 4.35s/
1.26MB with it, no additional invocation. A full board scan is not available
— `gh project item-list --limit 5000` exhausted the GraphQL quota twice on
2026-09-04 (harmonic-forge#468) and `block_raw_board_scan` now denies it at
the tool-call level.
"""
from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import NamedTuple

#: `L` + lane digit + one letter, per `harmonic-forge/rules/lane-shorthand.md`
#: — the canonical table (`3-lane-protocol.md` R-0206 points there and keeps
#: no second copy, because two copies is how the vocabulary drifted).
#:
#: `L2P` is **retired in favour of `L2S`** (operator, 2026-08-16; "a legacy
#: `L2P` in an older issue thread reads as `L2S`"). The tooling was fixed at
#: harmonic-forge#583 -- `l2_post._HEADINGS["plan"]` emits `## L2S` -- so the
#: note that used to sit here, "the live tooling still emits the `## L2P`
#: heading on every thread surveyed above", has been false since then. What
#: remains is the HISTORICAL corpus: threads surveyed before #583 carry `L2P`
#: and always will, which is why both spellings still map to one state rather
#: than the parser tracking a rename (harmonic-forge#609).
_LANE_TOKEN = re.compile(r"^##\s+L(?P<lane>[123])(?P<code>[PDSFB])\b", re.M)

#: `L3P` = gate passed, `L3F` = gate failed — verified against the canonical
#: table, resolving this issue's one `asserted` load-bearing assumption. Note
#: `P` is deliberately overloaded across lanes: Lane 2's `P` is *plan posted*,
#: Lane 3's is *passed*, so the lane digit is load-bearing, not decoration.
#: `B` is not a failure verdict on any lane — a lane reporting BLOCKED is the
#: protocol working, and it routes to remediation, never back a lane.
_PLAN_POSTED = {("2", "P"), ("2", "S")}
_LANE2_DONE = {("2", "D")}
_GATE_PASSED = {("3", "P")}
_GATE_FAILED = {("3", "F")}
_SPEC_POSTED = {("3", "S")}

#: Lane 3's gate verdict, as actually posted — a heading, not an `L3P`/`L3F`
#: token. Non-greedy up to the first PASS/FAIL so a FAIL whose parenthetical
#: reads "(migration executed successfully; ...)" is still a FAIL.
_GATE_RESULT = re.compile(r"^##\s+Lane 3 Gate Results\b[^\n]*?\b(PASS|FAIL)\b", re.M)
#: The same heading with neither verdict in it: a gate comment this module
#: cannot score. Unrecognized, never silently skipped.
_GATE_HEADING = re.compile(r"^##\s+Lane 3 Gate Results\b", re.M)

#: hrse#1590: the footer is the AUTHORITY, not one of two equal signals.
#: `l1_post.py` stamps it on every artifact it posts and refuses to post
#: without validating the body first, so `kind=` is an attested claim about
#: what the comment IS. A heading is prose anyone can type into any comment —
#: the protocol already warns that an AE posted through ordinary discussion
#: "reads correctly to a human but is invisible to Lane 3's own spec/AE
#: fetch", i.e. heading-presence and authorization are KNOWN to diverge.
#:
#: Spelled to match `check_lane3_ready.py`'s `FOOTER_KIND` exactly, character
#: for character, rather than re-invented here. One protocol concept with two
#: implementations is the defect hrse#1589 exists to fix; writing a second
#: footer dialect in the same session would re-open it.
_FOOTER_KIND = re.compile(r"<!--\s*l1-post\s+v1;\s*kind=(?P<kind>\w[\w-]*)", re.I)
_FOOTER_BODY_SHA = re.compile(
    r"<!--\s*l1-post\s+v1;.*?\bbody-sha256=[0-9a-f]{64}\b", re.I | re.S)

#: The headings `l1_post.py` itself validates, mirrored from `AE_HEADING` and
#: `SWEEP_HEADING` there. They are the CROSS-CHECK (AC1), never the authority:
#: a footer whose mandated heading is missing yields `validated=False` on the
#: transition, so the disagreement is reported rather than silently resolved.
_AE_HEADING = re.compile(r"(?im)^#{1,4}\s*AE\b")
_SWEEP_HEADING = re.compile(r"(?im)^#{1,4}\s*Gate-readiness sweep\b")

#: Lane 3's Test Spec, heading-only by necessity. Operator decision on this
#: issue's plan question 1, option (a): the Test Spec and the Gate Results are
#: the two artifacts that decide whether a gate passed, and they are exactly
#: the two with NO emitter — `l1_post.py --kind` is
#: `handoff|ready-for-l3|sweep|ae|ae-and-sweep`, and `l2_post.py` covers only
#: L2P/L2D/L2B. So they are read from their headings and the timeline SAYS SO,
#: `provenance="heading:..."`, never presented as attested. That gap is real
#: and stays visible on every gate until the emitter exists.
_SPEC_HEADING = re.compile(r"^#{1,4}\s*Lane 3 Test Spec\b", re.M | re.I)

#: Footer detection alone is provably insufficient on live data: hrse#1546's
#: handoff — a full one, Affected Files and Ambiguity Gate and all — is
#: stamped `kind=discussion; posted-by=LANE-unset`. Reading only the footer
#: renders that issue as having no handoff at all, which is exactly the
#: confidently-wrong row this issue exists to remove. The heading is the
#: fallback, and a fenced block is stripped first so a handoff QUOTED inside
#: another comment is not mistaken for one (`milestone_summary_analysis.py`
#: strips fences before its `## Dependencies` search for the same reason).
_HANDOFF_HEADING = re.compile(r"^##\s+Handoff\b", re.M)
_FENCE = re.compile(r"```.*?```", re.S)

#: hrse#1589: Plan-First is DECLARED by `l1_post.py`, not inferred from prose.
#:
#: The predicate this replaces was `re.compile(r"Plan-First", re.I)` over the
#: whole body. It matched hrse#1546's handoff — whose own sentence, under
#: `Delegated Judgment Calls`, is *"this does not need Plan-First. Implement
#: directly."* Ordinary prose that names the mechanism in order to DECLINE it
#: was enough; no malformed handoff required.
#:
#: And no body regex could have been right, because R-0244 has three triggers
#: and only the first is in the text: trigger 2 is a fact about the operation
#: (does it mutate git state or live data), trigger 3 is a sentence in
#: operator chat. Both are knowable to the handoff's author and to nobody
#: downstream — which is why the derivation moved to post time.
_PLAN_FIRST_DECLARED = re.compile(r"plan-first=(true|false)\b", re.I)

#: The legacy fallback's section, for handoffs posted before the field
#: existed. `l1_post.py`'s own heading list is the vocabulary; this matches
#: the one section R-0244's trigger 1 keys on, and stops at the next heading
#: of any level.
#: `[ \t]*\n` on the heading line, not `\s*\n`: `\s*` swallows the blank line
#: after the heading, so the captured body then STARTS at the next heading and
#: a `(?=\n#{1,6})` terminator cannot see it — an empty section read as
#: substantive content, i.e. Plan-First, from a section that says nothing.
#: Terminating on `^#{1,6}` under `re.M` rather than `\n#{1,6}` closes the
#: same hole from the other side. `milestone_summary_analysis.py`'s
#: `_DEPENDENCIES_SECTION` carries a recorded incident of exactly this shape
#: (hrse#1523: a terminator that could not match `\n### ` pulled a
#: subheading's bullets in as real blockers); this is the mirror of it.
_DELEGATED_SECTION = re.compile(
    r"^#{1,6}[ \t]*Delegated Judgment Calls[ \t]*\n(?P<body>.*?)(?=^#{1,6}[ \t]|\Z)",
    re.M | re.S | re.I)

#: An explicit decline. hrse#1546 is the live fixture: a section with
#: substantive content that says Plan-First is NOT needed. A naive
#: "non-`none` means Plan-First" rule classifies it backwards, which is the
#: regression this issue names.
_DECLINES_PLAN_FIRST = re.compile(
    r"\b(?:does\s+not|doesn't|no)\s+(?:need|require)\s+plan-first"
    r"|\bnot\s+plan-first\b"
    r"|\bimplement\s+directly\b",
    re.I)

#: A declaration. Both spellings appear live.
_DECLARES_PLAN_FIRST = re.compile(
    r"\bthis\s+is\s+plan-first\b|\bmakes?\s+(?:this|the)\s+issue\s+plan-first\b"
    r"|\bplan-first\b\s*[.:]?\s*$",
    re.I | re.M)

#: "None" as the section's OPENING token, not as its entire content.
#:
#: The live idiom is "None." followed by a sentence saying why — hrse#1382's
#: is *"None. Alias, `extra="forbid"`, caller audit, round-trip test — all
#: specified."* An anchor on the whole content misses every one of those and
#: reads the explanation as a delegated call, flipping correctly-`Implement`
#: rows to `Plan`. Caught by AC7's row-by-row comparison against the
#: published hrse#1584 baseline, which is exactly what that comparison is
#: for: three rows moved that should not have, and the totals alone would
#: have shown a plausible-looking 5->3 / 6->8 shift.
_SECTION_IS_NONE = re.compile(r"^\s*(none|n/?a|—|-)\b", re.I)

BLOCKED_NO_TIER = "blocked: no Tier"
NO_HANDOFF = "no handoff"
PLAN_POSTED = "plan posted, awaiting L1"
SPEC_POSTED = "spec posted, awaiting L1"
AWAITING_GATE = "implemented, awaiting gate"

#: hrse#1609. Names Lane 2, because naming the wrong actor is the whole defect
#: — the row previously read "implemented, awaiting gate" while Lane 1 was
#: waiting on a rebase nobody had been told to do.
REWORK_REQUESTED = "L1 asked for rework, back to L2"
GATED = "gated"

#: hrse#1590, R-0208. AE and its sweep are ONE atomic action, same turn,
#: sweep strictly after. So an AE standing alone is not a tidy waypoint on
#: the way to a gate — it is a partial transition that did not complete, and
#: rendering it as "awaiting sweep" would describe the protocol being
#: half-executed as though it were a normal step. The sweep is Lane 1's to
#: post (`testing-gate.md` rule 3), and naming the owner is the entire reason
#: R-0128 exists: the step was being skipped silently.
AE_WITHOUT_SWEEP = "AE posted, L1 owes the sweep"

#: hrse#1590, R-0209. Spec + AE + sweep, or that same authorization carried
#: forward onto a new SHA by a `ready-for-l3` after a FAIL. The carry-forward
#: half matters more than the fresh half: FAIL -> fix -> `ready-for-l3` is the
#: most common cycle in the protocol, and a model demanding a fresh AE there
#: marks correctly-authorized work as blocked on the ordinary path.
EXECUTABLE = "authorized, gate executable"

#: **Stable machine keys** (hrse#1590 AC5). Five of `classify()`'s returns
#: interpolate the issue ref — `f"ready: Implement {ref}"` — so no consumer
#: could switch on state without matching prose, and every rename was a
#: breaking change. The key carries no issue number and no prose.
#:
#: Two namespaces, deliberately not merged. TIMELINE keys name an artifact
#: that was POSTED (`ae.posted`); STATE keys name a condition DERIVED from the
#: whole thread (`gate.executable`). `gate.pass`/`gate.fail` appear in both
#: because there the artifact and the state genuinely coincide.
KEY_HANDOFF = "handoff.posted"
KEY_PLAN_POSTED = "plan.posted"
KEY_SPEC_POSTED = "spec.posted"
KEY_L2_DONE = "l2.done"
KEY_AE = "ae.posted"
KEY_SWEEP = "sweep.posted"
KEY_DISCUSSION = "l1.discussion"
#: hrse#1609. Distinct from `KEY_GATE_FAIL` even though both route to the same
#: operator trigger: the two states are not the same thing, and a model that
#: cannot tell "the gate failed" from "Lane 1 wants a rebase" would report the
#: wrong reason on every row it got right. Like `gate.pass`/`gate.fail`, the
#: artifact and the state coincide here, so one key serves both namespaces.
KEY_REWORK = "l1.rework"
KEY_GATE_PASS = "gate.pass"
KEY_GATE_FAIL = "gate.fail"
KEY_BLOCKED_LANE = "blocked.lane"

KEY_BLOCKED_NO_TIER = "blocked.no-tier"
KEY_BLOCKED_DEP = "blocked.dependency"
KEY_NO_HANDOFF = "no-handoff"
KEY_READY_PLAN = "ready.plan"
KEY_READY_IMPLEMENT = "ready.implement"
KEY_AWAITING_GATE = "implemented.awaiting-gate"
KEY_AE_WITHOUT_SWEEP = "gate.ae-without-sweep"
KEY_EXECUTABLE = "gate.executable"
KEY_UNKNOWN = "unknown"

#: **The fail-loud default.** A row the parser cannot place says so rather
#: than defaulting into "ready" — the posture `pipeline_rank.py` takes with
#: UNCLASSIFIED instead of falling into tier 4. A wrongly-ready row is worse
#: than an unknown one: it invites a trigger that will not work.
UNKNOWN = "unknown"


class Transition(NamedTuple):
    """One posted marker, in thread order (hrse#1590 AC4).

    `classify()` returns current state; hrse#1194 needs *first-pass pass
    rate, FAIL rounds per issue, BLOCK-vs-FAIL split, median lead time*.
    None of those is a function of a current-state string — every one is a
    function of a transition list. Its rescope comment told it to consume
    `classify()`; that instruction is wrong as written, and this is the
    primitive that makes those measures computable at all.

    It also makes the derivation auditable: a state traceable to a comment id
    is one a human can check, which is what hrse#1194 exists to measure.

    - `key` — a TIMELINE key above. Stable, no issue number, no prose.
    - `comment_id` — GitHub's node id, e.g. `IC_kwDOSDFJYM8AAAABSs4KIg`. A
      **string**: measured live against `gh issue list --json comments`, not
      the integer this issue's plan sketched. `""` when the caller passed
      bodies without ids (the existing fixture shape).
    - `at` — the comment's `createdAt`, ISO-8601. `""` when absent.
    - `provenance` — `footer:<kind>` | `heading:<artifact>` | `token:L<N><C>`.
      Which signal produced this, so a heading-derived state is never read as
      an attested one.
    - `validated` — the footer carried a `body-sha256` AND the heading
      `l1_post.py` mandates for that kind is present. False for every
      heading- and token-derived transition by construction, and false for a
      footer whose mandated heading is missing — the disagreement AC1 asks to
      be reported rather than silently resolved.
    """

    key: str
    comment_id: str
    at: str
    provenance: str
    validated: bool


def plan_first_of(body: str) -> bool | None:
    """Is this handoff Plan-First? `None` means undetermined (hrse#1589).

    Authority order, and the order is the design:

    1. **The declared footer field.** `l1_post.py` stamps
       `plan-first=true|false` on every handoff and refuses to post one
       without it, so this is a fact the author asserted across all three
       R-0244 triggers — not a guess reconstructed from prose.
    2. **Legacy fallback**, for handoffs posted before the field existed:
       the `Delegated Judgment Calls` section, with THREE outcomes rather
       than two. A section that declines Plan-First in prose is not the same
       as an empty one, and it is not the same as a delegated call — reading
       "substantive content" as "Plan-First" is precisely the backwards
       classification hrse#1546 exhibits.
    3. **Anything else is `None`.** No section, an unreadable one, or one
       that both declines and delegates.

    `None` never becomes a verb. `classify()` returns `unknown`, because a
    wrongly-`ready` row invites a trigger that will not work — and on this
    field specifically, a wrong `Implement` lets Lane 2 skip plan review,
    which is ADR-005's founding incident. Defaulting to `Plan` instead was
    considered and rejected for the same reason in the other direction: it is
    still a confident `ready:` emitted from a read that failed.
    """
    declared = _PLAN_FIRST_DECLARED.search(body)
    if declared:
        return declared.group(1).lower() == "true"

    section = _DELEGATED_SECTION.search(body)
    if section is None:
        return None
    content = section.group("body").strip()
    if not content:
        return None
    if _SECTION_IS_NONE.match(content):
        return False

    declines = bool(_DECLINES_PLAN_FIRST.search(content))
    declares = bool(_DECLARES_PLAN_FIRST.search(content))
    if declines and declares:
        # Says both. Reading either half as the answer would be a coin flip
        # wearing a derivation's clothes.
        return None
    if declines:
        return False
    if declares:
        return True
    # Substantive content that neither declares nor declines: R-0244 trigger 1
    # is satisfied by the section being other than "none", so this is
    # Plan-First. This is the only branch that infers, and it infers from the
    # structural rule rather than from wording.
    return True


#: Which footer kind must also carry which heading, for the cross-check.
#: A kind absent from this map has no mandated heading, so the cross-check
#: passes vacuously rather than failing an artifact that was never required
#: to have one.
#: harmonic-forge#473 added `spec` and `gate-result`, so these two are no
#: longer heading-only. `_GATE_HEADING` rather than `_GATE_RESULT` for the
#: cross-check: a BLOCKED gate names neither PASS nor FAIL and is still a
#: real gate result, and `post_lane_discussion.py` accepts it for the same
#: reason.
_KIND_HEADING = {"handoff": _HANDOFF_HEADING, "ae": _AE_HEADING,
                 "sweep": _SWEEP_HEADING, "spec": _SPEC_HEADING,
                 "gate-result": _GATE_HEADING}

#: Footer kind -> timeline key. `ready-for-l3` and an `L2D` heading are the
#: same transition posted by different lanes on different threads, so they
#: map to one key rather than two.
#: harmonic-forge#473: `spec` is a straight footer-authority mapping, but
#: `gate-result` deliberately is NOT here. A gate result's key depends on
#: its VERDICT — `gate.pass` or `gate.fail` — which the footer does not
#: carry and cannot, since the verdict lives in the heading prose. Mapping
#: it to one key would have to pick a verdict, and picking is exactly what
#: this module refuses to do on an unreadable artifact. `_scan_comment`
#: handles it below: the footer's presence upgrades the existing
#: heading-derived transition's provenance and validation rather than
#: producing a second, verdict-less one.
_KIND_KEY = {"handoff": KEY_HANDOFF, "discussion": KEY_DISCUSSION,
             "ready-for-l3": KEY_L2_DONE, "ae": KEY_AE, "sweep": KEY_SWEEP,
             "spec": KEY_SPEC_POSTED, "rework": KEY_REWORK}

_TOKEN_KEY = {("2", "P"): KEY_PLAN_POSTED, ("2", "S"): KEY_PLAN_POSTED,
              ("2", "D"): KEY_L2_DONE, ("3", "P"): KEY_GATE_PASS,
              ("3", "F"): KEY_GATE_FAIL, ("3", "S"): KEY_SPEC_POSTED}


def _validated(kind: str, body: str) -> bool:
    if not _FOOTER_BODY_SHA.search(body):
        return False
    heading = _KIND_HEADING.get(kind)
    return heading is None or bool(heading.search(body))


def _scan_comment(comment: dict) -> list[Transition]:
    """Every marker in ONE comment, footer-first. A comment can legitimately
    carry more than one — `_scan`'s original shape already allowed a handoff
    footer and a gate heading to be seen in the same body — so this returns a
    list rather than the first hit."""
    raw = comment.get("body") or ""
    #: **Strip fences before ANY marker scan, not just the handoff heading.**
    #: hrse#1590's own L2P comment quotes `## Lane 3 Test Spec — hrse#1573`
    #: and `## Lane 3 Gate Results — hrse#1573 — FAIL` inside a fenced block,
    #: as EVIDENCE of what live footers look like. Scanning the raw body read
    #: both as real transitions and put a `gate.fail` on a thread that has
    #: never been gated — found by running this module against live threads,
    #: not by any fixture. hrse#1584 already stripped fences for the handoff
    #: heading for exactly this reason; the fix is applying that everywhere a
    #: marker is read, footers included. A footer inside a fence is quoted
    #: evidence too, and `l1_post.py` never emits one there.
    body = _FENCE.sub("", raw)
    ident = str(comment.get("id") or "")
    at = str(comment.get("createdAt") or comment.get("created_at") or "")

    def made(key: str, provenance: str, validated: bool = False) -> Transition:
        return Transition(key, ident, at, provenance, validated)

    out: list[Transition] = []
    footer = _FOOTER_KIND.search(body)
    kind = footer.group("kind").lower() if footer else ""
    key = _KIND_KEY.get(kind)
    if key is not None:
        out.append(made(key, f"footer:{kind}", _validated(kind, body)))
    # hrse#1546's handoff is stamped `kind=discussion` — a full handoff,
    # Affected Files and Ambiguity Gate and all. Footer-only detection renders
    # that issue as having no handoff at all, which is the confidently-wrong
    # row hrse#1584 exists to remove. The heading is the fallback, and a
    # fenced block is stripped first so a handoff QUOTED inside another
    # comment is not mistaken for one.
    if key != KEY_HANDOFF and _HANDOFF_HEADING.search(body):
        out.append(made(KEY_HANDOFF, "heading:handoff"))
    # harmonic-forge#473: `kind=spec` is now emitted, so the heading is the
    # FALLBACK here rather than the only signal — same shape as the handoff
    # above. Without the guard a spec carrying both would produce two
    # `spec.posted` transitions for one comment, which would double-count in
    # exactly the metric hrse#1194 is being built to compute.
    if key != KEY_SPEC_POSTED and _SPEC_HEADING.search(body):
        out.append(made(KEY_SPEC_POSTED, "heading:spec"))
    gate = _GATE_RESULT.search(body)
    # harmonic-forge#473: a `kind=gate-result` footer UPGRADES this
    # transition rather than adding one. The verdict is only in the heading —
    # a footer cannot carry PASS/FAIL — so the footer's contribution is
    # provenance and attestation, not a second, verdict-less transition. This
    # is the one artifact where footer-as-authority and
    # heading-as-cross-check are the same object.
    attested = kind == "gate-result"
    provenance = "footer:gate-result" if attested else "heading:gate-result"
    validated = _validated(kind, body) if attested else False
    if gate:
        out.append(made(KEY_GATE_PASS if gate.group(1) == "PASS" else KEY_GATE_FAIL,
                        provenance, validated))
    elif _GATE_HEADING.search(body):
        # A gate comment naming no verdict. BLOCKED is a legitimate third
        # outcome and this module still cannot score it, so it stays
        # `unknown` — attested or not. An attested unreadable artifact is
        # still unreadable, and marking it `validated` would say the
        # opposite.
        out.append(made(KEY_UNKNOWN, provenance))
    token = _LANE_TOKEN.search(body)
    if token:
        pair = (token.group("lane"), token.group("code"))
        spelling = f"token:L{pair[0]}{pair[1]}"
        if pair in _TOKEN_KEY:
            out.append(made(_TOKEN_KEY[pair], spelling))
        elif pair[1] == "B":
            out.append(made(KEY_BLOCKED_LANE, spelling))
        else:
            out.append(made(KEY_UNKNOWN, spelling))
    return out


def parse_timeline(comments: list[dict]) -> list[Transition]:
    """Every marker across an issue's comments, in thread order (AC4).

    Public in this issue rather than internal-then-public in hrse#1194
    (Lane 1 decision on this issue's plan question 2): hrse#1194 sits on the
    critical path directly behind it, and the two-step buys a second gate
    round and no design.

    Deliberately NOT deduplicated to the newest of each class. `_Markers`
    does that fold for `classify()`; keeping every round here is what makes
    "FAIL rounds per issue" a count rather than a boolean.
    """
    return [transition for comment in comments
            for transition in _scan_comment(comment)]


class _Markers:
    """Newest index of each marker class, folded from the timeline.

    Indices rather than a first-match-wins scan: deciding "plan posted,
    awaiting L1" versus "Lane 1 answered, Lane 2 owes the implementation"
    requires comparing *two* markers' positions, which a scan that returns
    on the first hit cannot do.
    """

    def __init__(self, comments: list[dict]) -> None:
        self.handoff = self.plan = self.done = self.discussion = None
        self.gate_pass = self.gate_fail = self.blocked = self.spec = None
        self.ae = self.sweep = self.rework = None
        #: A marker-shaped comment whose marker this module cannot interpret
        #: — an `L1D`, a gate-results heading carrying neither verdict. THIS
        #: is what `unknown` is for. Chatter with no marker at all is not
        #: unclassifiable; it is an issue with no handoff, and saying
        #: `unknown` there buries the real unknowns in noise (10 of them on
        #: the live 2.9 board before this distinction existed).
        self.unrecognized = False
        self.blocked_lane = ""
        #: `None` means UNDETERMINED, distinct from False. The tri-state is
        #: the whole point: `True`/`False` are answers, `None` is the absence
        #: of one, and only the last may not produce a `ready:` row.
        self.plan_first: bool | None = None
        self.timeline: list[Transition] = []
        for index, comment in enumerate(comments):
            body = _FENCE.sub("", comment.get("body") or "")
            found = _scan_comment(comment)
            self.timeline.extend(found)
            # The `elif` hrse#1584 wrote, restated for a scan that now emits
            # every marker in a comment rather than the first: hrse#1546's
            # handoff carries a `kind=discussion` footer, and counting it as
            # both would let it satisfy the "Lane 1 answered the plan" rule
            # against itself. Dropped BEFORE the fold, never nulled after —
            # nulling would also erase a real discussion from an earlier
            # comment, which is a different bug wearing the same fix.
            #
            # Mutation testing says this guard is currently UNREACHABLE:
            # `newest()` ranks the handoff below every other marker, so a dual
            # comment newer than the plan is itself the newest marker and the
            # plan branch is never entered. It is kept, and labelled, because
            # it states a rule the ranking happens to imply rather than one it
            # guarantees — reordering `newest()` would make it load-bearing
            # again, silently. Removing it to chase a mutation score would be
            # removing the only thing that survives that edit.
            keys = {transition.key for transition in found}
            for transition in found:
                if transition.key == KEY_DISCUSSION and KEY_HANDOFF in keys:
                    continue
                self._fold(index, transition, body)

    def _fold(self, index: int, transition: Transition, body: str) -> None:
        key = transition.key
        if key == KEY_HANDOFF:
            self.handoff = index
            self.plan_first = plan_first_of(body)
        elif key == KEY_DISCUSSION:
            self.discussion = index
        elif key == KEY_L2_DONE:
            self.done = index
        elif key == KEY_AE:
            self.ae = index
        elif key == KEY_SWEEP:
            self.sweep = index
        elif key == KEY_REWORK:
            self.rework = index
        elif key == KEY_SPEC_POSTED:
            self.spec = index
        elif key == KEY_PLAN_POSTED:
            self.plan = index
        elif key == KEY_GATE_PASS:
            self.gate_pass = index
        elif key == KEY_GATE_FAIL:
            self.gate_fail = index
        elif key == KEY_BLOCKED_LANE:
            self.blocked = index
            self.blocked_lane = transition.provenance[len("token:L"):][:1]
        else:
            self.unrecognized = True

    def authorized(self) -> bool:
        """R-0208's pair, in the order the rule mandates: an AE, and a sweep
        strictly after it. A sweep with no AE beneath it is not authorization
        — it is a contradictory sequence, and `classify()` treats it as one."""
        # `>` rather than `>=` is a statement of the rule, not a live
        # discriminator: `_FOOTER_KIND` reads one kind per comment, so an AE
        # and a sweep can never share an index. Mutation testing confirms the
        # two spellings are equivalent here; the strict form is kept because
        # R-0208 says *strictly after*.
        return (self.ae is not None and self.sweep is not None
                and self.sweep > self.ae)

    def newest(self) -> tuple[str, int] | None:
        """The strongest-and-latest marker. `discussion` is deliberately
        excluded: on its own it is Lane 1 talking, not a lane transition —
        it only ever *modifies* the meaning of the plan marker below it."""
        candidates = [
            # hrse#1609, FIRST and therefore winning any index tie: a rework
            # request is the newest thing that changes whose turn it is, and
            # it can legitimately follow ANY prior state — the two live cases
            # sit after `gate_pass` (hrse#1578) and after `done` (hrse#1606).
            # Ranking it here is what lets one branch cover both instead of
            # patching each state separately and missing the third shape.
            ("rework", self.rework),
            ("gate_fail", self.gate_fail), ("gate_pass", self.gate_pass),
            ("blocked", self.blocked), ("sweep", self.sweep), ("ae", self.ae),
            ("done", self.done), ("spec", self.spec), ("plan", self.plan),
            ("handoff", self.handoff),
        ]
        found = [(name, index) for name, index in candidates if index is not None]
        if not found:
            return None
        return max(found, key=lambda pair: pair[1])


#: harmonic-forge#134. How long a thread may sit before its age is worth
#: printing. **24 hours, measured rather than picked.** Across 3,421 real
#: gaps between consecutive transitions (533 issues, since 2026-08-01):
#:
#:     p50   0.07 h      p90   1.03 h      p99   91.33 h
#:     p75   0.24 h      p95   5.81 h      max  528.45 h
#:
#: The distribution is bimodal with a wide empty region — a thread that is
#: moving moves in minutes, and one that has stopped has stopped for days.
#: 24h is over 4x the p95 of normal gaps and sits inside that empty region,
#: so it does not clip working rhythm. 97.6% of real gaps fall under it.
#:
#: The exact number barely matters: between 8h and 72h the output changed by
#: a single row. What changes it 7x is `STALE_STATES` below.
STALE_AFTER_HOURS = 24.0

#: **Only states that name an owner can go stale.** This is the whole reason
#: the feature is usable: unscoped, the flag produced 51 rows of which 43
#: were `no-handoff` — the untriaged backlog, which is a queue rather than a
#: stalled thread. `blocked.*` is legitimately parked, `gate.pass` is done,
#: and `unknown` has no owner to name. Scoped, the same threshold produced 7
#: rows, each genuinely waiting 4-20 days on a trigger.
#:
#: `ready.plan`/`ready.implement` are in: they wait on an operator trigger,
#: and an issue ready for 11 days that nobody triggered is precisely the
#: founding incident's shape — an issue nobody owns moving forward.
STALE_STATES = frozenset({
    KEY_PLAN_POSTED, KEY_SPEC_POSTED, KEY_AWAITING_GATE,
    KEY_AE_WITHOUT_SWEEP, KEY_EXECUTABLE, KEY_GATE_FAIL,
    KEY_READY_PLAN, KEY_READY_IMPLEMENT,
})


def hours_since(at: str, now: datetime | None = None) -> float | None:
    """Hours since an ISO-8601 stamp. `None` when there is no stamp.

    `None` rather than `0.0`, and the caller must render the difference:
    hrse#1590 documents `Transition.at` as `""` when a comment carries no
    timestamp, and an age of zero on an untimestamped thread reads as
    "just moved" — the opposite of what is known. Measured: 0 of 3,421
    transitions in the window had an empty `at`, so this path is unexercised
    by live data and exists to be correct rather than to fire.
    """
    if not at:
        return None
    moment = datetime.fromisoformat(at.replace("Z", "+00:00"))
    return ((now or datetime.now(UTC)) - moment).total_seconds() / 3600.0


def stale_label(state: LaneState, now: datetime | None = None) -> str | None:
    """`"stale 8d"` / `"stale 30h"` / `"age unknown"`, or `None`.

    Deliberately contains no `|` and no `]`: it renders inside the summary
    row's bracketed field, which `board_dashboard_renderer.py` splits on
    exactly those characters.
    """
    if state.key not in STALE_STATES:
        return None
    age = hours_since(state.at, now)
    if age is None:
        return "age unknown"
    if age < STALE_AFTER_HOURS:
        return None
    return f"stale {age / 24:.0f}d" if age >= 48 else f"stale {age:.0f}h"


class LaneState(NamedTuple):
    """`classify()`'s return: the display string, the literal next trigger,
    and the stable key (hrse#1590 AC5).

    `state` stays byte-identical to what hrse#1584 returned for every state
    that already existed — the summary row and `board_dashboard_renderer.py`
    both read that prose, and renaming it into machine-safe slugs was the
    rejected alternative. The key is ADDITIVE, which is what removes the
    "never rename" constraint rather than encoding it.
    """

    state: str
    trigger: str | None
    key: str
    #: The newest transition's timestamp, `""` when unknown
    #: (harmonic-forge#134). Defaulted so every existing construction site
    #: keeps working — `classify()` fills it on the paths that have a
    #: timeline, and the early-return paths above it (no Tier, unmet
    #: dependency, no comments) are all states that cannot go stale anyway.
    at: str = ""


def classify(record: dict, unmet: list[tuple[str, str]]) -> LaneState:
    """`(state, literal_next_trigger_or_None, stable_key)` for one issue.

    A fold over `parse_timeline()` rather than the only view of it (AC4).

    Precedence is strongest-blocker-first, then latest-marker-wins:

    1. **No board Tier** beats everything — `l1_post.validate_tier_set`
       refuses the handoff, so the issue cannot move regardless of what else
       is true, and that condition was previously invisible until a post was
       rejected.
    2. **An unmet dependency**, computed by the caller from the summary's
       existing `deps` and open-key set. Never a second detection mechanism.
    3. **The latest lane marker wins.** An issue carrying both a handoff and
       an `L2D` is at `L2D`; one whose last gate comment is FAIL is back with
       Lane 2 even though an earlier PASS exists.
    """
    if not (record.get("fields") or {}).get("Tier"):
        return LaneState(BLOCKED_NO_TIER, None, KEY_BLOCKED_NO_TIER)
    if unmet:
        return LaneState(f"blocked on {unmet[0][0]}", None, KEY_BLOCKED_DEP)

    comments = record.get("comments") or []
    if not comments:
        return LaneState(NO_HANDOFF, None, KEY_NO_HANDOFF)

    markers = _Markers(comments)
    # harmonic-forge#134: the newest transition's timestamp, taken from the
    # timeline `_Markers` already built on its way to folding — no second
    # parse and no refactor, which is what closed that issue's Ambiguity
    # Gate before it needed to fire. The NEWEST transition of any kind, not
    # the newest state-determining one: a thread with a comment on it has
    # moved, and "how long has it been their turn" should reset when it does.
    at = next((t.at for t in reversed(markers.timeline) if t.at), "")
    newest = markers.newest()
    if newest is None:
        return LaneState(UNKNOWN, None, KEY_UNKNOWN, at) if markers.unrecognized \
            else LaneState(NO_HANDOFF, None, KEY_NO_HANDOFF, at)

    ref = record.get("key", "")
    name, index = newest
    if name == "rework":
        # hrse#1609. Same operator trigger as a gate FAIL, deliberately
        # (Lane 1 decision on this issue's plan): what Lane 2 must do is
        # identical — re-read the issue and do the work on the branch — so a
        # second token would be vocabulary to remember for no behavioural
        # difference. The KEY stays distinct, which is where precision is
        # actually needed; the state string above says which of the two it is.
        return LaneState(f"{REWORK_REQUESTED} → Fix {ref}", f"Fix {ref}",
                         KEY_REWORK, at)
    if name == "gate_fail":
        return LaneState(f"FAIL, back to L2 → Fix {ref}", f"Fix {ref}", KEY_GATE_FAIL, at)
    if name == "gate_pass":
        return LaneState(GATED, None, KEY_GATE_PASS, at)
    if name == "blocked":
        return LaneState(f"blocked: L{markers.blocked_lane}B, see thread", None,
                         KEY_BLOCKED_LANE, at)
    if name == "sweep":
        # R-0208 again, from the other side: a sweep with no AE beneath it is
        # not an authorization that skipped a step — it is a sequence this
        # module cannot place, and AC8's fail-loud posture applies.
        return LaneState(EXECUTABLE, None, KEY_EXECUTABLE, at) if markers.authorized() \
            else LaneState(UNKNOWN, None, KEY_UNKNOWN, at)
    if name == "ae":
        return LaneState(AE_WITHOUT_SWEEP, None, KEY_AE_WITHOUT_SWEEP, at)
    if name == "done":
        # R-0209, mirrored from `check_lane3_ready.carry_forward()` rather
        # than written a second time: a `ready-for-l3` posted AFTER the
        # authorizing comment extends that authorization to a new SHA. This
        # is the FAIL -> fix -> re-gate cycle, which is the most common one
        # in the protocol; a model demanding a fresh AE here would mark
        # correctly-authorized work as blocked on the ordinary path.
        # No `index > markers.sweep` guard here, deliberately: `newest()`
        # already ranks `sweep` above `done` on a tie, so reaching this branch
        # means the completion post is strictly newer than the sweep. A second
        # comparison would be unreachable code that reads like a safeguard —
        # found by mutation testing, where removing it killed nothing.
        if markers.authorized():
            return LaneState(EXECUTABLE, None, KEY_EXECUTABLE, at)
        return LaneState(AWAITING_GATE, None, KEY_AWAITING_GATE, at)
    if name == "spec":
        return LaneState(SPEC_POSTED, None, KEY_SPEC_POSTED, at)
    if name == "plan":
        # Operator decision, hrse#1584: a Lane 1 `kind=discussion` comment
        # NEWER than the plan means Lane 1 has answered and Lane 2 owes the
        # implementation. Without this the row renders "awaiting L1" for the
        # whole ratified-plan-to-L2D window — naming the wrong actor and
        # withholding the one trigger that is actually next. That window is
        # every Plan-First thread in this repo.
        if markers.discussion is not None and markers.discussion > index:
            return LaneState(f"ready: Implement {ref}", f"Implement {ref}",
                             KEY_READY_IMPLEMENT, at)
        return LaneState(PLAN_POSTED, None, KEY_PLAN_POSTED, at)
    if markers.plan_first is None:
        # hrse#1589 AC6, closed by construction rather than by a safer guess:
        # an undetermined read does not reach a `ready:` state at all, so no
        # input path can produce `ready: Implement` from one.
        return LaneState(UNKNOWN, None, KEY_UNKNOWN, at)
    verb = "Plan" if markers.plan_first else "Implement"
    key = KEY_READY_PLAN if markers.plan_first else KEY_READY_IMPLEMENT
    return LaneState(f"ready: {verb} {ref}", f"{verb} {ref}", key, at)
