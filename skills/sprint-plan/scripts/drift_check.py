#!/usr/bin/env python3
"""Detect drift between docs/PRIORITIES.md and live GitHub issue state
(hrse + harmonic-forge), plus stranded work on branches (hrse#789). Three
independent checks:

1. Open issues not referenced anywhere in the doc — range-aware (expands
   "#226-232", "hrse#191-196", etc. instead of only matching literal
   single-number mentions).
2. Issues referenced in the doc whose surrounding bullet/list-item text
   never mentions "closed"/"merged", but the issue is actually CLOSED on
   GitHub — the doc claiming or implying open/pending status for
   something that's done. This is the direction that's cheap and
   unambiguous to check mechanically: every correctly-updated entry in
   this doc already says "closed"/"merged" explicitly (its own written
   convention), so a closed issue's block missing that word is a real
   signal, not a false positive from prose variety.

   Deliberately one-directional — the inverse (an open issue described in
   past/closed tense) is not checked here; it's harder to detect
   reliably from text alone and was not the failure mode of the 2026-07-30
   incident this check was added for (harmonic-forge#135).
3. Stranded work (hrse#789) — real commits sitting on a branch with
   nothing reconciling recorded state (the issue) against real condition
   (the branch). Two directions, both cheap git/REST, deliberately no
   per-issue comment-history scanning (~150 API calls/run for marginal
   recall over the branch-name convention):
   - a branch (`<type>/<number>-<slug>`) ahead of `origin/main` whose
     issue number is still OPEN — gated PASS and forgotten, or a PR that
     never merged;
   - the inverse, more dangerous direction: a branch ahead of `origin/main`
     whose issue is CLOSED — work marked done that never actually landed.
   Relies entirely on the branch-naming convention; a branch that doesn't
   name its issue number is invisible to this check, which is an accepted
   imprecision (comment-history scanning costs two orders of magnitude
   more API calls for the residual recall) — see hrse#789.

Cheap, mechanical, no subagent — this is the drift-detection step of the
sprint-plan skill's two-tier escalation. A non-empty result means something
needs a human/agent decision about where it slots in; it does NOT mean the
sequencing itself is wrong.
"""

import re
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

# hrse#917 phase 2: stale_closed_mentions() imports its scoping rule from the
# phase1_* siblings. Running this file as a script puts this directory on
# sys.path implicitly, but importing it as a module (tests, and any caller
# that does so) does not -- make it explicit rather than depend on how the
# module happened to be loaded.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import _home  # noqa: E402

# harmonic-forge#708: these scripts are platform code, so the repos and the
# docs come from the home checkout's .claude/sprint-plan.config.json, not
# from where this file happens to sit. Import stays possible without a
# config (tests, other repos); main() re-resolves and fails loudly instead.
try:
    REPOS = _home.repo_names()
    _DOCS = _home.docs_dir()
    _DATA = _home.data_dir()
except _home.config_loader.ConfigError:
    REPOS, _DOCS, _DATA = [], Path("docs"), Path(".claude") / "sprint-plan"
DOC_PATH = _DOCS / "PRIORITIES.md"

# hrse#974. The honesty check reads both live docs, and deliberately NOT the
# third.
#
# PRIORITIES-cuts.md records work deliberately NOT being done. When a cut
# issue closes, that entry does not merely age -- it argues against doing
# something already done. It is the one artifact a board structurally cannot
# hold, and it went unchecked: harmonic-forge#246 sat there as a live
# deferral after closing, while drift_check reported clean throughout.
#
# PRIORITIES-archive.md is EXCLUDED, and that asymmetry is the point rather
# than an oversight. Its own header calls it a "frozen historical record"
# where "every entry describes work that is closed, merged, or resolved" and
# "nothing here is actionable". It carries ~184 issue references, essentially
# all closed by design. Scanning it would report ~184 findings on day one and
# turn this into output you have to ignore -- which drift_check's own history
# (hrse#789) records as worse than no check at all.
CHECKED_DOCS = (DOC_PATH, _DOCS / "PRIORITIES-cuts.md")
UNCHECKED_DOCS = (_DOCS / "PRIORITIES-archive.md",)

# <type>/<number>-<slug>, e.g. "fix/903-hide-cancelled-activities",
# "tooling/892-ontology-doc-split" — the convention every branch in this
# repo has used all session. A branch not shaped like this is invisible to
# the STRANDED check by design (hrse#789's accepted imprecision).
_BRANCH_ISSUE_RE = re.compile(r"^[a-z][a-z0-9]*/(\d+)-")

# A block starts at a top-level numbered ("12.", "12a.") or "- " bullet and
# runs until the next such bullet or a "## " heading — matches this doc's
# only two list conventions (NOW/NEXT numbered items, LATER "- " bullets).
_BLOCK_START = re.compile(r"^(?:\d+[a-z]?\.\s|-\s)", re.MULTILINE)
_ISSUE_REF = re.compile(r"#(\d+)(?:[-–](\d+))?")
_CLOSED_WORD = re.compile(r"\bclosed\b|\bmerged\b", re.IGNORECASE)

# This doc's own convention (verified against every entry): a harmonic-forge
# issue is always written with an explicit "harmonic-forge#NN" or "forge#NN"
# prefix; a bare "#NN" always means hrse (the doc's own repo). Splitting on
# this avoids false positives from cross-repo number collisions — #46 in
# hrse and #46 in harmonic-forge are unrelated issues, bare "#46" means hrse.
_FORGE_PREFIX = re.compile(r"(?:harmonic-forge|forge)#(\d+)(?:/#(\d+))*", re.IGNORECASE)


# hrse#789: referenced_issues() lived here and existed only to serve the removed
# "every open issue must appear in the doc" check. Deleted rather than left in
# place — the whole point of that removal is that the doc is not an index of
# GitHub, and a helper that computes "everything the doc indexes" invites the
# check being reinstated by someone who finds it and assumes it is needed.


def _blocks(doc_path: Path) -> list[str]:
    text = doc_path.read_text()
    starts = [m.start() for m in _BLOCK_START.finditer(text)]
    starts.append(len(text))
    return [text[a:b] for a, b in zip(starts, starts[1:])]


def _repo_scoped_numbers(block: str) -> dict[str, set[int]]:
    """Split a block's issue mentions by repo, using the explicit
    harmonic-forge#/forge# prefix convention — bare #NN is always hrse."""
    forge_nums: set[int] = set()
    forge_spans: list[tuple[int, int]] = []
    for m in re.finditer(r"(?:harmonic-forge|forge)#(\d+(?:[-–/]#?\d+)*)", block, re.IGNORECASE):
        forge_spans.append(m.span())
        for part in re.split(r"[-–/]#?", m.group(1)):
            forge_nums.add(int(part))

    def _in_forge_span(pos: int) -> bool:
        return any(a <= pos < b for a, b in forge_spans)

    hrse_nums: set[int] = set()
    for m in _ISSUE_REF.finditer(block):
        if _in_forge_span(m.start()):
            continue
        start = int(m.group(1))
        end = int(m.group(2)) if m.group(2) else start
        hrse_nums.update(range(start, end + 1))

    return {"vitalharmony/hrse": hrse_nums, "vitalharmony/harmonic-forge": forge_nums}


class StaleMention(NamedTuple):
    """One flagged mention. Per-mention, not per-issue -- see below."""
    issue: int
    repo: str
    line: int
    context: str


# hrse#974 triage. Three ways these docs record a resolution that mention-level
# scoping reads straight past. All three are structural -- a marker the document
# already uses -- rather than per-entry allow-listing.

# 1. A section whose HEADING declares its own tense. "## Settled 2026-08-13/14"
#    and "## Tooling backlog consolidation - 40 issues closed" are records of
#    what happened; an issue named there is closed *because* that is what the
#    section is for. Measured: 9 of PRIORITIES.md's mentions sit under
#    `## Settled`, and cuts.md's closing tally is retrospective by construction.
#    Fixing it at section scope covers both documents at once.
_RETROSPECTIVE_HEADING = re.compile(
    r"\b(settled|closed|archived|resolved|consolidation)\b", re.IGNORECASE)

# 2. "promoted to" says the item LEFT this document -- the same kind of
#    departure as closed/merged, which are already handled.
_DEPARTED = re.compile(r"\bpromoted to\b", re.IGNORECASE)

# 3. `~~strikethrough~~` is these docs' own "no longer applies" marker.
_STRUCK = re.compile(r"~~")


def _doc_marks_resolved(mention, text: str, region, subject_scoped: bool = False) -> bool:
    """Does the document already record this mention as resolved?

    Distinct from C6's evidence binding, which asks whether a closure token is
    grammatically attached to this mention. These are markers the doc applies
    to a whole entry or a whole section, which no per-mention adjacency rule
    can see.
    """
    if _RETROSPECTIVE_HEADING.search(getattr(region, "title", "") or ""):
        return True
    # The mention's own bullet: from its enclosing "- " to the next one.
    line_start = text.rfind("\n", 0, mention.start) + 1
    bullet_start = text.rfind("\n- ", 0, mention.start)
    bullet_start = bullet_start + 1 if bullet_start != -1 else line_start
    nxt = text.find("\n- ", mention.start)
    bullet = text[bullet_start:nxt if nxt != -1 else len(text)]
    if _STRUCK.search(bullet) or _DEPARTED.search(bullet):
        return True
    # In cuts.md each entry opens with a bolded title span that states the
    # subject and its disposition: "**hrse#803 - cost-aware Projects v2
    # enforcement, closed 2026-08-13 without being built.**". C6's 20-char
    # adjacency window cannot reach that far, so the doc's own verdict is
    # missed.
    #
    # Scoped to the BOLD SPAN, not the whole bullet. The bullet body routinely
    # cites other things' closures -- "merged harmonic-forge PR#122" sits in
    # an entry whose own subject is still genuinely stale -- and clearing on
    # those produced four false negatives when measured. False-clean is the
    # direction this issue family exists to prevent, so the narrower span wins.
    if subject_scoped:
        # hrse#917 phase 4: derive the title span from the BULLET START, not
        # from the mention. The previous form used rfind("**", 0, mention.start)
        # which, for a mention in the body, lands on the title's CLOSING
        # delimiter -- so the "bold span" searched for closure evidence was by
        # construction the plain prose BETWEEN two bold runs. Measured live on
        # the hrse#852 entry: a 644-character span starting at "** Surfaced by
        # Marc...". Harmless only because subject scoping kept body mentions
        # from reaching here; correcting it means that stays true by intent
        # rather than by luck.
        title = _bold_title_span(bullet)
        if title is None:
            return False
        t_start, t_end = bullet_start + title[0], bullet_start + title[1]
        if not (t_start <= mention.start < t_end):
            return False  # a body mention is never cleared by the title
        return bool(_CLOSED_WORD.search(text[t_start:t_end]))
    return False


def _cut_bullet_spans(text: str) -> list[tuple[int, int]]:
    """Span of each top-level `- ` bullet in PRIORITIES-cuts.md.

    hrse#974. cuts.md has a different shape from PRIORITIES.md and needs a
    different extraction. Everything after an entry's title is supporting
    prose, which routinely cites other issues ("found during forge#220's
    review", "pre-existing since #210").

    Scanning every mention here flags those citations: measured live, 107
    mentions across 79 issues, dominated by references that assert nothing
    about the cut. Scoping to the subject drops it to 25, all of which are
    entries whose own subject has since closed -- which is exactly what this
    check is for. The alternative was a check whose output you ignore, which
    drift_check's own history (hrse#789) rates worse than no check.
    """
    spans: list[tuple[int, int]] = []
    offset = 0
    for line in text.split("\n"):
        if line.startswith("- "):
            spans.append((offset, offset))
        offset += len(line) + 1
    bounds = [a for a, _ in spans] + [len(text)]
    return list(zip(bounds, bounds[1:]))


def _bold_title_span(bullet: str) -> tuple[int, int] | None:
    """Offsets of the first `**...**` run in a cuts.md bullet, or None.

    hrse#917 phase 4. Every cuts entry opens with a bolded title stating the
    subject and its disposition. That span is ALREADY what `_doc_marks_resolved`
    searches for closure evidence -- subject selection now reads the same span,
    so subject and evidence finally agree on where an entry's subject lives.

    A bullet whose title carries no issue reference has no ISSUE subject: its
    subject is a decision ("Follow-up model -- commitments vs. waits.
    Deliberately NOT filed as work") or a category label ("Duplicate (1)").
    Measured live: 10 of 67 bullets, and 0 bullets lack a bold title entirely.
    Promoting the first body citation to subject in those cases is what
    produced the standing false positive on hrse#852.
    """
    opening = bullet.find("**")
    if opening == -1:
        return None
    closing = bullet.find("**", opening + 2)
    if closing == -1:
        return None
    return opening, closing + 2


BASELINE_PATH = _DATA / "drift_baseline.toml"


class BaselineError(RuntimeError):
    """The baseline itself is wrong. Distinct from drift, and worse: a stale
    suppression hides real drift, so it fails the run rather than degrading."""


def load_baseline(path: Path = BASELINE_PATH) -> list[dict]:
    """Reviewed citation-role suppressions.

    hrse#917. The role layer clears mentions that are STRUCTURALLY citations
    (backticked, parenthetical, quoted, retrospective section). What remains is
    prose that reads as a claim but asserts no status -- "in as many days after
    #903", "record it on hrse#832". Deterministic classification cannot reach
    those without semantic understanding, and pattern-matching the current
    wording would break the moment the prose changes.

    So they are declared, with a reason, and reviewed. Same shape as
    check_token_contrast's [[baseline]] (hrse#964), including the property that
    makes a baseline safe rather than a dumping ground: **it self-expires.**
    See validate_baseline.
    """
    if not path.exists():
        return []
    try:
        import tomllib
    except ModuleNotFoundError:  # pragma: no cover - py<3.11
        return []
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise BaselineError(f"{path.name}: unparseable — {exc}") from exc
    entries = data.get("citation", [])
    for i, e in enumerate(entries):
        missing = [k for k in ("doc", "repo", "issue", "quote", "why", "reviewed")
                   if not e.get(k)]
        if missing:
            raise BaselineError(
                f"{path.name}: entry {i + 1} is missing {', '.join(missing)}. "
                "Every field is required -- an unexplained suppression is the "
                "thing this file exists to prevent.")
    return entries


def apply_baseline(doc_name: str, rows: list[StaleMention],
                   entries: list[dict]) -> tuple[list[StaleMention], list[dict]]:
    """Split rows into (still-reported, suppressed-by-a-matching-entry)."""
    mine = [e for e in entries if e["doc"] == doc_name]
    kept: list[StaleMention] = []
    used: list[dict] = []
    for row in rows:
        hit = next(
            (e for e in mine
             if e["repo"] == row.repo and int(e["issue"]) == row.issue
             and e["quote"] in row.context),
            None)
        if hit is None:
            kept.append(row)
        elif hit not in used:
            used.append(hit)
    return kept, used


def validate_baseline(entries: list[dict], matched: list[dict],
                      open_by_repo: dict[str, set[int]]) -> list[str]:
    """Every way a baseline entry can be WRONG. This is the whole point.

    An entry that no longer corresponds to anything is not harmless: it is a
    standing permission to ignore a mention that may since have become a real
    claim. hrse#964's ratchet breaks the build in BOTH directions for the same
    reason -- a suppression that outlives its subject is how a baseline rots
    into a blanket bypass.
    """
    problems: list[str] = []
    for e in entries:
        label = f"{e['doc']} {e['repo'].split('/')[-1]}#{e['issue']}"
        if e not in matched:
            problems.append(
                f"{label}: matches no flagged mention. Either the prose changed "
                f"(quote {e['quote']!r} no longer present) or the flag is gone "
                "— delete the entry.")
        if int(e["issue"]) in open_by_repo.get(e["repo"], set()):
            problems.append(
                f"{label}: that issue is OPEN. A stale-CLOSED suppression "
                "cannot apply to it — delete the entry.")
    return problems


def stale_closed_mentions(doc_path: Path, closed: dict[str, set[int]]) -> list[StaleMention]:
    """Mentions that describe a closed issue as live. One row per mention.

    hrse#917 phase 2. Implements rule **C6**, selected in phase 1 by
    adjudicating 113 mentions against the live doc.

    The rule this replaced cleared an entire block whenever *any*
    `closed`/`merged` token appeared in it, so evidence was never bound to a
    subject. Live failure 2026-08-16: the check reported "No stale-closed
    mentions found" while the doc described closed `harmonic-forge#250` as
    pending work, because its bullet said "closed 2026-08-14" about
    `hrse#802`.

    **Per-mention, not per-issue** -- the issue's own phase-1 adjudication:
    *"flagging an issue when any of its mentions lacks evidence makes output
    unactionable ... This holds whichever rule wins."* Returning one row per
    issue forces an aggregation rule, and every candidate for that rule is
    wrong:

    - flag-if-any-mention-flags reports an issue beside prose that plainly
      discloses its closure;
    - clear-if-any-mention-clears is C3, the control phase 1 disqualified.
      Measured live: it suppressed `#858` (described as not-yet-merged at
      :255-257) and three quarters of a stale sequencing paragraph
      (`#855`/`#856`/`#852` at :455-464) -- all closed, all real drift. It
      fails toward false-clean, the direction this check exists to prevent.

    Reporting `PRIORITIES.md:<line>` per mention retires that question
    instead of answering it, and is directly actionable in a way a bare
    issue number is not.

    **Citations abstain; they do not clear.** `c6_role_aware` returns CLEAR
    for a citation-role mention, which per-mention is correct -- a citation
    ("the #849 -> #859 chain") asserts no live status, so it cannot be stale.
    But CLEAR there means "asserts nothing", not "carries closure evidence",
    and the two must never be conflated. Here a citation is simply skipped.

    Coverage (criterion 5): every character is scanned. `extract_mentions`
    also expands `#794-#797` ranges and excludes `[hrse #1](.../projects/1)`
    board links. The old `_blocks()` slicing left the first ~9.5 KB --
    including the entire "In flight" section -- permanently unscanned.

    **Known residual**, recorded rather than papered over: retrospective and
    standing-fact narrative in `Settled` / `Dependencies that bite` is
    frequently classified `claim` by `phase1_mentions.classify_role`, so it
    flags. That is a role-layer gap, not an aggregation one, and closing it
    with an aggregation rule is exactly the mistake above.

    Falls back to the pre-hrse#917 block rule only if phase 1's modules
    cannot be imported, so a broken sibling degrades this check rather than
    taking down the sweep.
    """
    try:
        from phase1_candidates import c6_role_aware
        from phase1_mentions import extract_mentions, h2_regions
    except ImportError:  # pragma: no cover - degraded path
        return [
            StaleMention(n, repo, 0, "(degraded: phase1 modules unavailable)")
            for n, repo in _stale_closed_mentions_block_scoped(doc_path, closed)
        ]

    text = doc_path.read_text(encoding="utf-8")
    regions = h2_regions(text)
    rows: list[StaleMention] = []

    candidates = [m for m in extract_mentions(text)]
    subject_scoped = doc_path.name == "PRIORITIES-cuts.md"
    if subject_scoped:
        # One candidate per cut entry: the first reference in its BOLD TITLE.
        # An entry whose title names no issue contributes nothing -- see
        # _bold_title_span. Previously this took the first mention anywhere in
        # the bullet, which promoted body citations to subject (hrse#917 ph4).
        subjects = []
        for start, end in _cut_bullet_spans(text):
            title = _bold_title_span(text[start:end])
            if title is None:
                continue
            t_start, t_end = start + title[0], start + title[1]
            inside = [m for m in candidates if t_start <= m.start < t_end]
            if inside:
                subjects.append(min(inside, key=lambda m: m.start))
        candidates = subjects

    for mention in candidates:
        if mention.is_board_link:
            continue
        if mention.role == "citation":
            continue  # abstains: asserts no status, so it cannot be stale
        if mention.issue not in closed.get(mention.repo, set()):
            continue
        region = next(
            (r for r in regions if r.start <= mention.start < r.end),
            regions[-1] if regions else None,
        )
        if region is None:
            continue
        if c6_role_aware(mention, text, region).outcome != "FLAG":
            continue
        if _conjoined_subject_evidence(mention, text, region):
            continue
        if _doc_marks_resolved(mention, text, region,
                               subject_scoped=subject_scoped):
            continue
        snippet = " ".join(
            text[max(0, mention.start - 60):mention.end + 60].split()
        )
        rows.append(StaleMention(mention.issue, mention.repo, mention.line, snippet))
    return rows


# Between a mention and its status token: only other issue references and the
# punctuation and conjunctions that join a list. Any real word breaks it.
_CONJOINED = re.compile(
    r"""^(?:
          \s | [,/·&*_`~()\[\]-]
        | \#\d+
        | (?:hrse|harmonic-forge|forge)\#\d+
        | \b(?:and|both|all|were|are|also|now)\b
    )*$""",
    re.IGNORECASE | re.VERBOSE,
)


def _conjoined_subject_evidence(mention, text, region) -> bool:
    """Does a status token in this sentence apply to a list this mention is in?

    An evidence-binding refinement, NOT an aggregation rule -- it decides
    whether *this* mention has evidence, which is C6's own layer.

    C6 refuses to bind evidence across an intervening issue number, so that a
    neighbouring `merged` cannot be stolen by an unrelated mention. That is
    what makes the `#860` trap work and it must not be weakened. But it also
    refuses the *conjoined subject*, which this doc uses constantly:

        "hrse#792 and hrse#793 both merged and closed 2026-08-13"
        "(#847 and #848 both closed 2026-08-13)"

    There the intervening number shares the predicate rather than competing
    for it. The distinction is structural, not semantic: everything between
    the mention and the token must be other issue references and joining
    tokens. Any real word breaks it -- which is precisely why the `#860`
    trap still flags, since "complete except **#860**" puts prose between.
    """
    from phase1_mentions import sentence_span

    start, end = sentence_span(mention.start, text, region)
    for match in _CLOSED_WORD.finditer(text, start, end):
        if match.start() >= mention.end:
            between = text[mention.end:match.start()]
        elif match.end() <= mention.start:
            between = text[match.end():mention.start]
        else:
            continue
        if _CONJOINED.match(between):
            return True
    return False


def _stale_closed_mentions_block_scoped(
    doc_path: Path, closed: dict[str, set[int]]
) -> list[tuple[int, str]]:
    """The pre-hrse#917 rule, retained only as an import-failure fallback.

    Known-defective: evidence is not bound to its subject, and the first
    ~9.5 KB of the doc is never scanned. Kept so a missing phase1 module
    degrades the check instead of taking down `mise run sprint-plan`.
    """
    flagged: set[tuple[int, str]] = set()
    for block in _blocks(doc_path):
        if _CLOSED_WORD.search(block):
            continue
        scoped = _repo_scoped_numbers(block)
        for repo, nums in scoped.items():
            for n in nums & closed.get(repo, set()):
                flagged.add((n, repo))
    return sorted(flagged)


ROADMAP_PATH = _DOCS / "ROADMAP.md"

# harmonic-forge#283: milestones that deliberately describe no release, so
# they are exempt from needing a ROADMAP section. `Later` is the sentinel for
# "real work, not yet placed in a numbered release" (it carries 33 issues);
# `Platform` is cross-project tooling that never ships in a CymaGraph release.
# Required, not optional: ROADMAP's headings are exactly 2.7/2.8/2.9/3.0/
# "Beyond 3.0"/"Keeping this honest", so without this both would FAIL on a
# clean repo.
NON_RELEASE_MILESTONES = frozenset({"Later", "Platform"})

_ROADMAP_HEADING = re.compile(r"^##\s+(.+)$", re.MULTILINE)

# A release section's first token is a version ("2.7", "3.0"), never prose.
_RELEASE_TOKEN = re.compile(r"\d+\.\d+")


def roadmap_section_tokens(doc_path: Path) -> set[str]:
    """First whitespace token of every `## ` heading in ROADMAP.md.

    harmonic-forge#283 NC5: split on whitespace, NOT on the em dash. All four
    current headings use U+2014, so an em-dash split works today -- and would
    silently stop matching the day someone types a plain hyphen, producing a
    false FAIL on a doc that is actually correct."""
    if not doc_path.exists():
        return set()
    return {
        match.group(1).split()[0]
        for match in _ROADMAP_HEADING.finditer(doc_path.read_text(encoding="utf-8"))
        if match.group(1).split()
    }


def milestone_titles(repo: str) -> list[str]:
    result = subprocess.run(
        ["gh", "api", f"repos/{repo}/milestones", "-X", "GET", "-f", "state=all",
         "--jq", ".[].title"],
        capture_output=True, text=True, check=False,
    )
    if result.returncode:
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def milestone_roadmap_drift(repo: str, doc_path: Path) -> tuple[list[str], list[str]]:
    """(missing_sections, unmilestoned_sections) — deliberately asymmetric.

    **milestone with no section FAILS.** A release the team actually opened
    that the doc never explains is doc dishonesty, and ROADMAP.md itself
    claims to explain every release.

    **section with no milestone is REPORT-ONLY.** 2.8/2.9/3.0 have sections
    and no milestones today *by design* — they are future. Failing there would
    invert the source of truth and make GitHub chase the doc.

    This is not the completeness check hrse#789 removed. That one required
    every open *issue* (~200) to be referenced in the doc, satisfiable only by
    re-growing the doc into an index of GitHub — the dual-source-of-truth
    requirement itself. This runs over ~6 milestones, asks whether an
    already-opened release is *explained*, and puts the completeness-shaped
    direction on the report-only side precisely so it can never become that."""
    sections = roadmap_section_tokens(doc_path)
    missing_sections = [
        title for title in milestone_titles(repo)
        if title not in NON_RELEASE_MILESTONES and title.split()[0] not in sections
    ]
    titles = set(milestone_titles(repo))
    # Only version-shaped headings ("2.8", "3.0") are release sections. Prose
    # sections ("Keeping this honest", "Which repos carry milestones") are not
    # releases and must never be reported as unmilestoned. This replaced a
    # hardcoded {"Keeping", "Beyond"} skip-list, which silently produced a
    # false report line for every prose section added after it was written.
    unmilestoned = [
        token for token in sorted(sections)
        if token not in titles and _RELEASE_TOKEN.fullmatch(token)
    ]
    return sorted(missing_sections), unmilestoned


def _issue_numbers(repo: str, state: str) -> set[int]:
    # harmonic-forge#220/#223: REST migration off GraphQL-backed `gh issue
    # list`, coordinated with #223's own --limit bug (gh issue list's
    # implicit --limit 30 default silently capped this at 30 results on
    # repos with hundreds of issues -- confirmed live, 185 real open
    # issues on hrse alone). `--paginate` covers the full result set with
    # no cap. The REST /issues endpoint also returns PRs (confirmed live,
    # unlike `gh issue list`) -- filtered out via `.pull_request == null`.
    result = subprocess.run(
        ["gh", "api", f"repos/{repo}/issues", "-X", "GET", "-f", f"state={state}",
         "-f", "per_page=100", "--paginate", "--jq", ".[] | select(.pull_request == null) | .number"],
        capture_output=True, text=True, check=True,
    )
    return {int(n) for n in result.stdout.split()}


def open_issues(repo: str) -> set[int]:
    return _issue_numbers(repo, "open")


def closed_issues(repo: str) -> set[int]:
    return _issue_numbers(repo, "closed")


def branches_ahead_of_main() -> dict[int, tuple[str, int]]:
    """{issue_number: (branch_name, commits_ahead)} for every remote branch
    on `origin` matching the <type>/<number>-<slug> convention that carries
    at least one commit `origin/main` doesn't have.

    hrse#789: one `git ls-remote` for every branch, then one
    `git rev-list --count` per name-matching candidate — cheap, git-only,
    no GitHub API call. Scoped to this checkout's own `origin` remote
    (vitalharmony/hrse); harmonic-forge branches are not visible from here
    and are out of scope for this check (the acceptance criteria's own
    worked examples — hrse#48/#632/#713/#744 — are all hrse-side)."""
    ls = subprocess.run(
        ["git", "ls-remote", "--heads", "origin"],
        capture_output=True, text=True, check=True,
    )
    subprocess.run(["git", "fetch", "-q", "origin", "main"], check=True)

    candidates: dict[int, str] = {}
    for line in ls.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) != 2:
            continue
        branch = parts[1].removeprefix("refs/heads/")
        match = _BRANCH_ISSUE_RE.match(branch)
        if not match:
            continue
        candidates[int(match.group(1))] = branch

    # harmonic-forge#433: the candidate refs must actually be present locally.
    # `git ls-remote` lists remote heads without fetching them, and the fetch
    # above takes `main` only -- so `origin/<branch>` existed only if some
    # earlier command happened to fetch it. The old `rev-list` silently
    # `continue`d when the ref was missing, which reads as "not ahead" and is
    # the same silent-degradation Ambiguity Gate 4 forbids. Fetch them.
    if candidates:
        subprocess.run(
            ["git", "fetch", "-q", "origin",
             *(f"+refs/heads/{b}:refs/remotes/origin/{b}" for b in candidates.values())],
            capture_output=True, text=True,
        )

    ahead: dict[int, tuple[str, int]] = {}
    for number, branch in candidates.items():
        count = subprocess.run(
            ["git", "rev-list", "--count", f"origin/main..origin/{branch}"],
            capture_output=True, text=True,
        )
        if count.returncode != 0:
            # The ref is genuinely unavailable even after the fetch. Report it
            # rather than dropping it -- never silently degrade.
            ahead[number] = (branch, -1)
            continue
        n = int(count.stdout.strip() or 0)
        if n <= 0:
            continue
        if _content_is_already_on_main(branch):
            continue
        ahead[number] = (branch, n)
    return ahead


def _content_is_already_on_main(branch: str) -> bool:
    """True when `branch`'s commits are present in `origin/main` by PATCH-ID.

    harmonic-forge#433. Squash-merge rewrites the SHA, so `rev-list` counts
    already-shipped commits as absent. On 2026-09-02 that reported 14 CLOSED
    issues as "closed on GitHub but the work is not on main" — every one had
    a merged PR. `git cherry` compares by patch-id and recognises the
    equivalent change under a different SHA.

    **Two guards, both load-bearing.**

    `git cherry` alone is not sufficient: an N->1 squash produces no matching
    patch-id for any individual commit, so a multi-commit branch still reports
    `+`. Those need the merged-PR signal, applied by the caller.

    `git cherry` alone is not SAFE either, which is the direction that loses
    work: **it ignores merge commits entirely.** Reproduced in a scratch repo
    — a branch whose only commit ahead was a merge carrying resolution-only
    content reported zero unmatched patches while that content was absent from
    main. So "content is present" additionally requires that no merge commit
    be ahead. If one is, cherry's silence is not evidence and we fall through
    to reporting, which over-reports in the rare evil-merge case and never
    under-reports.
    """
    cherry = subprocess.run(
        ["git", "cherry", "origin/main", f"origin/{branch}"],
        capture_output=True, text=True,
    )
    if cherry.returncode != 0:
        return False
    unmatched = [ln for ln in cherry.stdout.splitlines() if ln.startswith("+")]
    if unmatched:
        return False

    merges = subprocess.run(
        ["git", "rev-list", "--count", "--merges", f"origin/main..origin/{branch}"],
        capture_output=True, text=True,
    )
    if merges.returncode != 0:
        return False
    return int(merges.stdout.strip() or 0) == 0


def stranded_work(open_by_repo: dict[str, set[int]], closed_by_repo: dict[str, set[int]]) -> tuple[list[tuple[int, str, int]], list[tuple[int, str, int]]]:
    """Two lists of (issue_number, branch_name, commits_ahead):
    (stranded_open, stranded_closed_dangerous) — see module docstring for
    what each direction means. hrse repo only, per branches_ahead_of_main's
    scope note."""
    ahead = branches_ahead_of_main()
    hrse_open = open_by_repo.get("vitalharmony/hrse", set())
    hrse_closed = closed_by_repo.get("vitalharmony/hrse", set())

    # harmonic-forge#433: the second signal. `git cherry` suppresses a 1:1
    # squash on its own, but an N->1 squash produces no matching patch-id for
    # any individual commit -- 4 of the 14 false positives were that shape.
    # A merged PR on the branch settles it.
    merged_heads = merged_pr_head_refs("vitalharmony/hrse")

    def _suppressed(branch: str) -> bool:
        """A merged PR settles this branch only if nothing is left beyond it.

        The first implementation dropped on the name alone, which let a
        merged PR override the patch-id signal entirely: a branch with two
        unmatched patches AND an earlier merged PR reported nothing at all.
        A merged PR explains the commits that PR contained — not whatever was
        pushed afterwards.
        """
        if branch not in merged_heads:
            return False
        return not unmatched_beyond(branch, merged_heads[branch])

    def _rows(numbers: set[int]) -> list[tuple[int, str, int]]:
        return sorted(
            (n, branch, count)
            for n, (branch, count) in ahead.items()
            if n in numbers and not _suppressed(branch)
        )

    return _rows(hrse_open), _rows(hrse_closed)


def merged_pr_head_refs(repo: str) -> dict[str, str]:
    """`{branch name: head SHA}` for every merged PR (harmonic-forge#433).

    Deliberately the same **REST** shape this file already uses at
    `stale_open_prs` -- `gh api repos/{repo}/pulls` widened to `state=all` --
    and NOT `gh pr list`, which is GraphQL-backed. This file was migrated off
    GraphQL-backed `gh issue list` in harmonic-forge#220/#223, and
    `repo_hygiene.py`'s design note 3 forbids GraphQL for this tooling class
    citing hrse#814.

    The join is exact: a PR's `head.ref` IS the branch name, so no
    issue-number inference is involved.

    **The head SHA is carried, not just the name.** A merged PR settles an
    N->1 squash only for the commits that PR contained — work pushed to the
    same branch *after* the merge is genuinely absent from main and must
    still report. Without the SHA there is no way to tell the two apart, and
    dropping on the name alone silently discards real unmatched patches. Same
    asymmetry as the merge-commit guard: over-report rather than lose work.
    """
    result = subprocess.run(
        ["gh", "api", f"repos/{repo}/pulls", "-X", "GET", "-f", "state=all",
         "-f", "per_page=100", "--paginate",
         "--jq", '.[] | select(.merged_at != null) | [.head.ref, .head.sha] | @tsv'],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        # Never fail open into "nothing was merged" -- that would resurrect
        # every squash-merged branch as a false positive, which is the whole
        # defect. An empty set is indistinguishable from a real answer, so
        # raise instead.
        raise RuntimeError(
            f"could not list merged PRs for {repo}: {result.stderr.strip()}")
    merged: dict[str, str] = {}
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        ref, _, sha = line.partition("\t")
        # A branch can carry several merged PRs; the LAST one wins, so
        # "beyond the merge" is measured from the most recent merge.
        merged[ref.strip()] = sha.strip()
    return merged


def unmatched_beyond(branch: str, merged_sha: str) -> bool:
    """True when `branch` carries content absent from main *after*
    `merged_sha` (harmonic-forge#433).

    `git cherry <upstream> <head> <limit>` restricts the comparison to
    commits reachable from `head` but not `limit`, so this asks exactly the
    right question: setting aside everything the merged PR contained, is
    anything left whose patch is not in main?

    Fails toward reporting. If the SHA is not present locally the answer is
    unknowable, and an unknowable answer must not silently clear a branch.
    """
    if not merged_sha:
        return True
    known = subprocess.run(["git", "cat-file", "-e", f"{merged_sha}^{{commit}}"],
                           capture_output=True, text=True)
    if known.returncode != 0:
        return True
    cherry = subprocess.run(
        ["git", "cherry", "origin/main", f"origin/{branch}", merged_sha],
        capture_output=True, text=True,
    )
    if cherry.returncode != 0:
        return True
    return any(ln.startswith("+") for ln in cherry.stdout.splitlines())


def stale_open_prs(repo: str, stale_days: int = 3) -> list[tuple[int, str, int]]:
    """(pr_number, title, days_since_update) for open PRs untouched beyond
    `stale_days`. One REST call per repo (hrse#789 acceptance criterion 4:
    a handful of calls added to a full run, not hundreds)."""
    from datetime import datetime, timezone

    result = subprocess.run(
        ["gh", "api", f"repos/{repo}/pulls", "-X", "GET", "-f", "state=open",
         "-f", "per_page=100", "--paginate",
         "--jq", ".[] | [.number, .title, .updated_at] | @tsv"],
        capture_output=True, text=True, check=True,
    )
    now = datetime.now(timezone.utc)
    stale: list[tuple[int, str, int]] = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        number_s, title, updated_s = line.split("\t", 2)
        updated = datetime.fromisoformat(updated_s.replace("Z", "+00:00"))
        days = (now - updated).days
        if days >= stale_days:
            stale.append((int(number_s), title, days))
    return sorted(stale, key=lambda t: -t[2])


def main() -> int:
    # hrse#789: this script used to also report "open issues NOT referenced in
    # PRIORITIES.md" — a completeness check asserting the doc must mirror every
    # open issue. That check is REMOVED, deliberately.
    #
    # Its premise *was* the dual-source-of-truth requirement (2026-08-12): the
    # doc no longer mirrors GitHub, it carries the reasoning GitHub's fields
    # structurally cannot hold — current thesis, what is in flight, explicit
    # cuts, dependencies. GitHub owns tracking. After the hrse#808 Phase 1
    # archive split the check reported 56 "drifted" issues and would only climb,
    # and the ONLY way to satisfy it was to re-grow the doc — precisely what was
    # just cut. It had become a check whose output you had to ignore, which is
    # worse than no check: ignoring it also masks the real signal below.
    #
    # What remains here is doc HONESTY, which got more valuable rather than
    # less: the doc is now purely narrative, so narrative that describes shipped
    # work as pending actively misleads the agents that read it to orient.
    # Three live instances on 2026-08-12 alone (#807 read as pending after it
    # merged, #809 said PARKED after it became next, #803's parking existed only
    # in the doc).
    #
    # The inverse check — "issues the BOARD claims are NOW/NEXT that the doc
    # never explains" — is not covered here. board_drift_check.py, which used
    # to own it, is deleted (hrse#839: it read the board's Priority field,
    # which was retired the same day). That signal is recorded as genuinely
    # lost in hrse#839's own record, not silently dropped without a trace —
    # if issues start being worked without a recorded rationale again, that
    # is the check to rebuild, against Status rather than Priority.
    any_drift = False
    closed_by_repo: dict[str, set[int]] = {}
    open_by_repo: dict[str, set[int]] = {}
    for repo in REPOS:
        closed_by_repo[repo] = closed_issues(repo)
        open_by_repo[repo] = open_issues(repo)

    # Reported per document, not pooled: a reader has to be able to tell
    # which doc is lying, and the two carry different kinds of claim.
    total_stale = 0
    try:
        baseline = load_baseline()
    except BaselineError as exc:
        print(f"BASELINE ERROR: {exc}", file=sys.stderr)
        return 2
    matched: list[dict] = []
    suppressed = 0
    for doc in CHECKED_DOCS:
        if not doc.exists():
            print(f"WARN: {doc.name} not found — skipped", file=sys.stderr)
            continue
        stale = stale_closed_mentions(doc, closed_by_repo)
        before = len(stale)
        stale, used = apply_baseline(doc.name, stale, baseline)
        matched.extend(used)
        suppressed += before - len(stale)
        if not stale:
            continue
        total_stale += len(stale)
        any_drift = True
        print(f"STALE [{doc.name}]: {len(stale)} mention(s) describing a "
              f"closed issue as live:")
        for row in stale:
            short = row.repo.split("/")[-1]
            print(f"  {doc.name}:{row.line} — {short}#{row.issue}")
            print(f"      {row.context}")
        if doc.name == "PRIORITIES-cuts.md":
            print("  A cut whose issue has closed does not merely go stale — "
                  "it argues against doing work that is already done.")
        print()
    if total_stale == 0:
        print("No stale-closed mentions found.")
    if suppressed:
        print(f"({suppressed} mention(s) suppressed by drift_baseline.toml — "
              "reviewed citations, each with a recorded reason.)")

    # A stale suppression hides real drift, so it fails the run outright rather
    # than degrading quietly. Exit 2 distinguishes "the baseline is wrong" from
    # "the doc is wrong" -- same split as check_token_contrast (hrse#964).
    baseline_problems = validate_baseline(baseline, matched, open_by_repo)
    if baseline_problems:
        print("\nBASELINE IS STALE — every entry must still correspond to a "
              "live suppression:", file=sys.stderr)
        for problem in baseline_problems:
            print(f"  {problem}", file=sys.stderr)
        return 2

    # harmonic-forge#283: milestones own release membership; ROADMAP.md owns
    # what each release means. Asymmetric on purpose — see
    # milestone_roadmap_drift's docstring.
    missing_sections, unmilestoned_sections = milestone_roadmap_drift(
        "vitalharmony/hrse", ROADMAP_PATH
    )
    if missing_sections:
        any_drift = True
        print(f"\nMILESTONE WITHOUT A ROADMAP SECTION: {len(missing_sections)} — "
              "a release nobody defined:")
        for title in missing_sections:
            print(f"  {title} — add a `## {title} — ...` section to docs/ROADMAP.md")
    else:
        print("Every release milestone has a ROADMAP.md section.")
    if unmilestoned_sections:
        # Report-only, never a failure: future releases legitimately have a
        # section and no milestone yet.
        print(f"\n(report-only) ROADMAP section with no milestone yet: "
              f"{', '.join(unmilestoned_sections)} — expected for future releases.")

    stranded_open, stranded_closed = stranded_work(open_by_repo, closed_by_repo)
    if stranded_open:
        any_drift = True
        print(f"\nSTRANDED: {len(stranded_open)} open issue(s) have real commits on a branch, "
              "not referenced as merged/closed:")
        for n, branch, count in stranded_open:
            ahead_text = ("commit count unavailable" if count < 0
                          else f"{count} commit(s) ahead of origin/main")
            print(f"  #{n} — {branch} ({ahead_text})")
    if stranded_closed:
        any_drift = True
        print(f"\nSTRANDED (dangerous direction): {len(stranded_closed)} CLOSED issue(s) have "
              "real commits on a branch that never merged:")
        for n, branch, count in stranded_closed:
            # harmonic-forge#433: "the work is not on main" asserted data loss
            # on work that had shipped. What is actually established is a
            # SHA-level absence, after patch-id and merged-PR checks have both
            # failed to account for it.
            ahead_text = ("commit count unavailable" if count < 0
                          else f"{count} commit(s) ahead of origin/main")
            print(f"  #{n} — {branch} ({ahead_text}) — "
                  "commits not present on main by SHA, no merged PR, and not "
                  "matched by patch-id")
    if not stranded_open and not stranded_closed:
        print("No stranded branches found.")

    for repo in REPOS:
        stale_prs = stale_open_prs(repo)
        if stale_prs:
            any_drift = True
            print(f"\nSTALE PR(S) on {repo}, no activity in 3+ days:")
            for number, title, days in stale_prs:
                print(f"  #{number} ({days}d) — {title}")

    return 1 if any_drift else 0


if __name__ == "__main__":
    # hrse#917 phase 1 (see phase1_report.py): dispatches before main() is
    # ever called, since main()/branches_ahead_of_main() mutate git refs.
    if "--phase1-report" in sys.argv:
        from phase1_report import run_phase1_report
        sys.exit(run_phase1_report())
    sys.exit(main())
