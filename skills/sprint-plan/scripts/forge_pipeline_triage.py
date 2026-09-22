#!/usr/bin/env python3
"""hrse#1476 — periodic pipeline-relevance triage across harmonic-forge's
open issues, cross-referenced against hrse's live pipeline.

harmonic-forge carries no release milestones by design (CLAUDE.md — its
work is pulled by multiple ventures, belongs to no single release). That
left a real gap: no periodic, automated way to know which of its ~80 open
issues matter to the live job/opp search pipeline right now versus which
are safely deferrable.

**Deterministic by construction, not an LLM judgment call.** AC1 asks for a
classification "deterministically reproduced... from live Theme/Venture
fields + cross-repo reference scan" — so bucketing is driven entirely by
two mechanical signals:

1. Whether an open hrse issue's body explicitly references the forge issue
   (`forge#N`, `harmonic-forge#N`, or a bare `#N` inside a sentence already
   naming harmonic-forge), and what language surrounds that reference.
2. The referencing hrse issue's own board `Theme`/`Venture` — never used to
   invent a relationship the text doesn't state.

**Bucket assignment:**

- **A — blocks/harms the pipeline now:** the forge issue is referenced by an
  open hrse issue using block-language ("blocks", "blocked on", "harms",
  "breaks", "breaking").
- **B — foundation for a feature needed soon:** the forge issue is
  referenced by an open hrse issue at all, without block-language (e.g.
  "depends on", "needed for", "once ... lands", or any other mention).
- **C — neither / can wait:** no open hrse issue references the forge issue.

This is a narrower, more defensible signal than the 2026-09-01 manual
proof-of-concept's semantic read (which used a subagent's judgment to name
specific pipeline dependencies) — deliberately: a script that hallucinated
"this forge issue harms the pipeline" would be worse than one that says
"no explicit hrse reference found, therefore Bucket C" and lets a human
override it. The bucket assignment is auditable — every entry carries the
hrse issue and sentence that produced it.

**Report-only.** This module has no write method. It never mutates an
issue, label, or board field, on either repo — the acceptance criterion is
explicit about this, and hrse#1119's cleanup-must-not-delete lesson
generalizes: a periodic triage tool must be even more conservative than a
gate script, since nothing reviews its output before the next run.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

#: Where harmonic-forge is checked out varies by caller: a developer's own
#: machine has it at ~/harmonic-forge, but the repo-hygiene.yml CI runner
#: checks it out at $GITHUB_WORKSPACE/harmonic-forge — not $HOME — and a
#: hardcoded Path.home() lookup silently always misses there, always falling
#: through to the (untested, more failure-prone) direct `gh project
#: item-list` path (hrse#1476's own preclose-inspection finding). The caller
#: can point HARMONIC_FORGE_TOOLS_GH at the right place explicitly; falls
#: back to the ~/harmonic-forge convention for local/interactive use.
_forge_tools_gh = os.environ.get("HARMONIC_FORGE_TOOLS_GH") or str(
    Path.home() / "harmonic-forge" / "tools" / "gh"
)
sys.path.insert(0, _forge_tools_gh)
try:
    import item_list_cache as _item_list_cache
except ImportError:
    _item_list_cache = None

from pipeline_rank import (  # noqa: E402
    EPIC_LABEL, GROUP_NOT_READY, Ranking, check_ratchet, inherit_epic_group,
    rank,
)
from milestone_summary_analysis import dependencies  # noqa: E402
from milestone_summary_source import (  # noqa: E402
    MILESTONE_REPOS, fetch_issues, known_milestones, milestone_counts,
    read_anchor, resolve_milestone,
)

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _home  # noqa: E402

FORGE_REPO = "vitalharmony/harmonic-forge"
HRSE_REPO = "vitalharmony/hrse"
FORGE_BOARD_OWNER = "vitalharmony"
FORGE_BOARD_NUMBER = "3"

#: hrse#800/#966/forge#430's own lesson, generalized here: `gh issue list`
#: silently truncates at 30 without an explicit --limit, and `gh project
#: item-list` truncates at whatever --limit is passed with no signal that
#: more rows exist. Every REST/board fetch in this module either paginates
#: properly or checks `len(results) < limit` and fails loudly if that bound
#: is hit, rather than trusting a fixed number forever.
REST_PER_PAGE = 100
ISSUE_SCAN_LIMIT = 2000

#: AC5. `item_list_cache` treats `ttl<=0` as "always fetch", so the previous
#: `ttl=0` never read or wrote the cache at all.
#:
#: What this actually saves, stated plainly rather than overclaimed: the cache
#: is on disk under `tempfile.gettempdir()`, so a real TTL saves one full
#: board pagination on repeated **local** runs and **zero** in CI, whose
#: runner starts with a fresh tempdir every time. This one board read is the
#: whole of AC5 — the per-issue `updated_at` keying considered alongside it
#: reduces no API calls, because obtaining `updated_at` already requires the
#: fetch, and it must not be counted toward this criterion.
BOARD_TTL_SECONDS = 1800

#: Ordered so BLOCK_PHRASES is checked first — "blocked on" must win over a
#: later, weaker "depends on" if a body happens to contain both.
_BLOCK_PHRASES = (
    "blocks", "blocked on", "blocking", "harms", "breaks", "breaking",
)
#: **Explicit cross-repo form only** (`forge#N` / `harmonic-forge#N`) —
#: deliberately not a bare `#N` fallback. This repo's own convention,
#: consistent across every issue body and commit message this session
#: touched, is bare `#N` for a same-repo reference and an explicit prefix
#: for cross-repo. A bare-`#N` match would be ambiguous the moment an hrse
#: issue number happens to collide with a forge one, and it produced real
#: false positives in testing (this issue's own body, hrse#1476, matched
#: itself via its "reference, not still-current" disclaimer paragraph
#: naming forge#96/#97/#305/#172 in prose without the prefix repeated on
#: every mention).
_REFERENCE_RE = re.compile(r"(?:harmonic-forge|forge)#(\d+)", re.IGNORECASE)

#: The reverse direction — a forge body citing an hrse issue. hrse#1476's
#: rescope promised this scan and did not ship it. Same explicit-prefix
#: discipline as above: a bare `#N` inside a forge body means a *forge*
#: issue, so only the qualified form counts.
_HRSE_REFERENCE_RE = re.compile(r"hrse#(\d+)", re.IGNORECASE)


class TriageError(Exception):
    """Raised on any fetch failure. Never fail-open — a triage that quietly
    reports zero forge issues on an API error is indistinguishable from a
    real "nothing to flag" result, which is worse than a loud crash."""


def _run(*args: str) -> str:
    result = subprocess.run(args, capture_output=True, text=True)
    if result.returncode != 0:
        raise TriageError(f"{' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def _rest(path: str) -> list[dict]:
    """Paginated REST GET. `--paginate` matters — gh's implicit per-page
    default silently truncates at 30 (hrse#800's board-truncation class of
    bug, same lesson). `raw_decode`-based concatenation-splitting, ported
    verbatim from `repo_hygiene.py`'s own `_rest()` rather than
    re-implemented, since a regex-based splitter would be fooled by an
    issue body that happens to contain the same `]...[` boundary text."""
    out = _run("gh", "api", path, "--paginate", "-H", "Accept: application/vnd.github+json")
    items: list[dict] = []
    decoder = json.JSONDecoder()
    idx = 0
    text = out.strip()
    while idx < len(text):
        obj, end = decoder.raw_decode(text, idx)
        items.extend(obj if isinstance(obj, list) else [obj])
        idx = end
        while idx < len(text) and text[idx] in " \n\r\t":
            idx += 1
    return items


def fetch_open_issues(repo: str) -> list[dict]:
    """Every open issue (not PR) in `repo`, `{number, title, body, labels}`.

    **`labels` is load-bearing and was missing until hrse#1631.** `classify()`
    ranks forge rows with `_labels(source)` where `source` is one of these
    dicts, so dropping the key here made every forge issue rank against an
    EMPTY label set — silently, since `_labels()` reads
    `issue.get("labels") or []` and an absent key is indistinguishable from an
    unlabelled issue.

    The live symptom that found it: `harmonic-forge#97` carries
    `pipeline-clock`, the sanctioned Group-2 lever that `pipeline_rank.py`'s
    own docstring says exists *because* the `CLOCK` lexicon misses this exact
    issue. Called directly, `rank()` put it in Group 2; the summary rendered
    no Group 2 section at all and placed F97 in UNCLASSIFIED. Same issue,
    same ranker, two answers — the only difference was this projection.

    It is not F97-specific: any forge issue whose group turns on a label
    (`pipeline-clock`, `not-ready`, `epic`) was mis-ranked, which is the
    aggregate signature behind recent summaries' high UNCLASSIFIED counts.

    The REST payload already carries `labels` in the `[{name}]` shape
    `_labels()` documents, so this passes it through rather than re-fetching.
    """
    combined = _rest(f"repos/{repo}/issues?state=open&per_page={REST_PER_PAGE}")
    if len(combined) >= ISSUE_SCAN_LIMIT:
        raise TriageError(
            f"{repo} returned >= {ISSUE_SCAN_LIMIT} open issues — raise "
            "ISSUE_SCAN_LIMIT, this bound was meant to be well above any "
            "real count and has been reached"
        )
    return [
        {"number": i["number"], "title": i["title"], "body": i.get("body") or "",
         "labels": i.get("labels") or []}
        for i in combined
        if i.get("pull_request") is None
    ]


def fetch_forge_theme_venture(cache_dir: str | None = None) -> dict[int, dict]:
    """`{issue_number: {theme, venture, title}}` for every open harmonic-forge
    issue on its board, via the shared `item_list_cache` module (already
    paginated correctly, already truncation-checked — see its own
    `BOARD_ITEM_SCAN_LIMIT` comment for the hrse#800 incident this avoids).
    Falls back to a direct `gh project item-list` call if the shared module
    isn't importable, so this script has no hard dependency on
    ~/harmonic-forge existing at a specific path.
    """
    if _item_list_cache is not None:
        items = _item_list_cache.fetch_item_list(
            FORGE_BOARD_NUMBER, owner=FORGE_BOARD_OWNER, ttl=BOARD_TTL_SECONDS,
            **({"cache_dir": cache_dir} if cache_dir is not None else {}),
        )
    else:
        raw = _run(
            "gh", "project", "item-list", FORGE_BOARD_NUMBER,
            "--owner", FORGE_BOARD_OWNER, "--limit", "5000", "--format", "json",
        )
        payload = json.loads(raw)
        items = payload["items"]
        if len(items) >= 5000:
            raise TriageError(
                "forge board returned >= 5000 items — raise the limit, this "
                "was meant to be well above any real board size"
            )

    result: dict[int, dict] = {}
    for item in items:
        content = item.get("content") or {}
        if content.get("repository") != FORGE_REPO:
            continue
        number = content.get("number")
        if number is None:
            continue
        result[number] = {
            "theme": item.get("theme"),
            "venture": item.get("venture"),
            "title": content.get("title", ""),
        }
    return result


@dataclass
class TriageEntry:
    """One ranked issue, from either repo.

    `bucket` stays on forge rows deliberately: the A/B/C cross-reference
    signal shipped in hrse#1476 and is real evidence about a forge issue
    ("an hrse issue says this blocks it"). What changed is that it is no
    longer a *ranking* input — the pipeline `group` is. Dropping the field
    would be a scope expansion, not a correction. hrse rows carry
    `bucket=None`.

    hrse#1522: `group`/`group_why` are the pipeline-relevance rank
    (`1|2|3|4|LATER|UNCLASSIFIED`, `pipeline_rank.rank()`'s output) —
    renamed from `tier`/`tier_why` because that word already means the
    board's `fast|standard|deep` model-routing field everywhere else in
    this codebase, and rendering both under one label made them
    indistinguishable on the page. `board_tier` is that other field,
    plumbed here for the first time from the same board read
    `milestone_summary.py` already performs (`board_fields()`) — no
    second fetch.
    """
    repo: str            # "hrse" | "forge"
    issue: int
    title: str
    group: str
    group_why: str
    board_tier: str | None = None
    theme: str | None = None
    venture: str | None = None
    milestone: str | None = None
    bucket: str | None = None            # forge rows only
    #: `[{repo, issue, sentence}]` — uniform in both directions, so a
    #: consumer never has to know which scan produced a row.
    referenced_by: list[dict] = field(default_factory=list)
    #: hrse#1523: `pipeline_rank.rank()`'s advisory not-ready SHAPE
    #: (`READINESS_HINTS` key), carried through unchanged. Independent of
    #: `group` -- a hinted row that never got the `not-ready` label ranks
    #: exactly as it otherwise would.
    hint: str | None = None


def _sentence_containing(body: str, span: tuple[int, int]) -> str:
    """The sentence around a match, for an auditable citation — not the
    whole issue body, which would make every entry unreadable.

    hrse issue bodies are predominantly bullet-formatted (no periods), so a
    period-only boundary search never fires there and used to fall back to a
    raw ±120-char window that could span into an unrelated neighboring
    bullet, letting its language (e.g. "blocks") wrongly promote a plain
    dependency into Bucket A (hrse#1476's own preclose-inspection finding,
    live-reproduced). A newline is at least as strong a sentence boundary as
    a period here, so both are searched and whichever is closer wins.
    """
    start, end = span

    left_dot = body.rfind(".", 0, start)
    left_nl = body.rfind("\n", 0, start)
    left = max(left_dot, left_nl)
    left = left + 1 if left != -1 else max(0, start - 120)

    right_dot = body.find(".", end)
    right_nl = body.find("\n", end)
    candidates = [c for c in (right_dot, right_nl) if c != -1]
    right = min(candidates) + 1 if candidates else min(len(body), end + 120)

    return " ".join(body[left:right].split())


def _scan_references(
    sources: list[dict], targets: set[int], pattern: re.Pattern, source_repo: str,
) -> tuple[dict[int, list[dict]], set[int]]:
    """Bodies in `sources` citing an issue in `targets`. Both directions use
    this — the reverse (forge citing `hrse#N`) is what hrse#1476's rescope
    promised and did not ship."""
    references: dict[int, list[dict]] = {n: [] for n in targets}
    block_hits: set[int] = set()
    for issue in sources:
        body = issue.get("body") or ""
        for match in pattern.finditer(body):
            number = int(match.group(1))
            if number not in targets:
                continue
            sentence = _sentence_containing(body, match.span())
            references[number].append(
                {"repo": source_repo, "issue": issue["number"], "sentence": sentence}
            )
            if any(phrase in sentence.lower() for phrase in _BLOCK_PHRASES):
                block_hits.add(number)
    return references, block_hits


def _labels(issue: dict) -> set[str]:
    """`gh issue list --json labels` gives `[{name}]`; the REST shape matches."""
    return {label["name"] for label in issue.get("labels") or []}


def _open_blockers(deps: list[tuple[str, str]], open_hrse: set[int],
                   open_forge: set[int]) -> list[str]:
    """`deps` targets are `milestone_summary_analysis.dependencies()`'s own
    format (`{repo-shortname-or-explicit-prefix}#{number}`). Only `hrse` and
    `harmonic-forge`/`forge` prefixes resolve here -- there is no open-state
    set for any other repo (e.g. `cymagraph-infra`) available in this run,
    and an unresolvable target must never demote by default (that would be
    inventing a blocker, the same failure class this module already rejects
    for the A/B/C bucket's own citation scan).

    hrse#1523 preclose finding: `linked` provenance only -- a `blocked
    on`/`depends on` declaration or a `## Dependencies` heading's bulleted
    ref is the issue stating its own blocker. `INFERRED_DEP`'s phrase
    guesses (`requires`, `gated on`, up to 80 characters of arbitrary prose
    before the ref) are exactly the "phrase matcher promoted to
    authoritative" failure this issue's own `READINESS_HINTS` design
    rejects for prose readiness signals -- an `inferred` edge must not
    authoritatively demote real work on a coincidental phrase match."""
    blockers = []
    for target, provenance in deps:
        if provenance != "linked":
            continue
        prefix, _, number = target.partition("#")
        if not number.isdigit():
            continue
        num, prefix = int(number), prefix.lower()
        if prefix == "hrse" and num in open_hrse:
            blockers.append(target)
        elif prefix in ("harmonic-forge", "forge") and num in open_forge:
            blockers.append(target)
    return blockers


def _demote_if_not_ready(ranking: Ranking, labels: set[str],
                         deps: list[tuple[str, str]], open_hrse: set[int],
                         open_forge: set[int]) -> Ranking:
    """hrse#1523: an issue `rank()` placed in group 1-4 may still be
    unpickable -- blocked on a still-open dependency, or an epic with no
    code of its own (AC1/AC4). Both need cross-issue open/closed state
    `pipeline_rank.rank()` does not have, so this demotion pass lives here,
    after ranking, rather than inside `rank()` itself.

    hrse#1523 preclose finding: restricted to `ranking.group` already being
    "1"-"4" (matching `GROUP_NOT_READY`'s own documented invariant, which
    the first version of this function did not enforce). An epic or
    dependency-blocked issue that ALREADY ranks LATER/UNCLASSIFIED/NOT_READY
    is left exactly as it is -- demoting it anyway would have moved it out
    of `check_ratchet`'s UNCLASSIFIED count with no real classification
    improvement, silently loosening that fail-if-worse guard.
    """
    if ranking.group not in ("1", "2", "3", "4"):
        return ranking
    if EPIC_LABEL in labels:
        # hrse#1523 preclose finding: `inherit_epic_group` leaves `ranking`
        # unchanged (returns `own`) when the epic has no rankable children,
        # or when its own rank is already at least as urgent as theirs --
        # in both cases `ranking.group`/`.why` describe the epic's OWN
        # body, not its children, and the message must say so rather than
        # claiming "children would rank X" for a rank that came from the
        # epic's own text. `inherit_epic_group` writes "inherited from open
        # children" into `.why` only on the genuine-inheritance path, which
        # is the one stable signal available here for which case happened.
        if "inherited from open children" in ranking.why:
            cause = f"children would rank {ranking.group}: {ranking.why}"
        else:
            cause = f"its own rank would be {ranking.group}: {ranking.why}"
        return Ranking(GROUP_NOT_READY, f"epic — no code of its own ({cause})",
                       ranking.stages, ranking.hint)
    blockers = _open_blockers(deps, open_hrse, open_forge)
    if blockers:
        return Ranking(
            GROUP_NOT_READY, f"blocked on open {', '.join(sorted(blockers))}",
            ranking.stages, ranking.hint)
    return ranking


def classify(
    forge_open: set[int],
    forge_meta: dict[int, dict],
    hrse_issues: list[dict],
    forge_issues: list[dict] | None = None,
    milestone: str | None = None,
    hrse_reference_sources: list[dict] | None = None,
    board_fields: dict[tuple[str, int], dict[str, str]] | None = None,
) -> list[TriageEntry]:
    """Rank both repos' issues, with `repo` as a per-row attribute.

    hrse rows are the addition: hrse#1476 fetched hrse issues only to regex
    their bodies for `forge#N`, so they were reference *sources* and were
    never classified. hrse#1477 needs them grouped alongside forge's.

    **The milestone filter is hrse-only.** harmonic-forge carries no release
    milestones by design (CLAUDE.md — its work is pulled by several ventures
    and belongs to no single release), so there is nothing to filter it by.

    **Epic children outside the current milestone are invisible** to the
    inheritance rule: only issues inside the filtered set are available to
    inherit from, so an epic whose children sit in a later milestone
    inherits nothing and keeps its own ranking.

    `hrse_reference_sources` is separate from `hrse_issues` on purpose, and
    conflating them is a live regression this signature exists to prevent:
    the milestone filter governs which hrse issues are *classified*, but the
    forge A/B/C bucket asks "does ANY open hrse issue cite this forge issue",
    across the whole repo. Filtering the scan to the current milestone
    dropped every citation (6 -> 0 measured) and collapsed every forge row to
    bucket C, because 178 of 182 open hrse bodies were no longer being read.
    Defaults to `hrse_issues` so a caller that genuinely has only one set
    behaves as before.

    hrse#1522: `board_fields` is the `{(repo, number): {field: value}}` map
    `milestone_summary.board_fields()` already builds from one GraphQL read
    of both boards. Passed in, never re-fetched here -- this module has no
    board-write authority and no reason to duplicate a read its only real
    caller (`milestone_summary.py`) already performs. `None` (the default)
    leaves every `board_tier` unset, which is the correct behaviour for a
    caller -- `test_forge_pipeline_triage.py`'s existing fixtures -- that
    never had board data to begin with.
    """
    forge_issues = forge_issues or []
    board_fields = board_fields or {}
    reference_sources = (
        hrse_issues if hrse_reference_sources is None else hrse_reference_sources)
    hrse_open = {i["number"] for i in hrse_issues}
    # hrse#1523 AC1/AC6: the OPEN-STATE set for dependency-blocker demotion
    # must be every open hrse issue, not just this milestone's -- a
    # blocker can sit outside the current milestone (hrse#192, #199's own
    # blocker, is the live case). `reference_sources` is already that set
    # when the real caller (`run_triage()`) supplies it; reusing it here is
    # what keeps this a zero-extra-fetch signal (AC6).
    all_hrse_open = {i["number"] for i in reference_sources}

    references, block_hits = _scan_references(
        reference_sources, forge_open, _REFERENCE_RE, "hrse")
    reverse_refs, _ = _scan_references(
        forge_issues, hrse_open, _HRSE_REFERENCE_RE, "forge")

    entries: list[TriageEntry] = []

    rankings: dict[int, Ranking] = {}
    for issue in hrse_issues:
        rankings[issue["number"]] = rank(
            issue["title"], issue.get("body") or "", _labels(issue))
    # Epics inherit the most urgent tier among their open children. Applied
    # after every own-ranking exists, since a child's tier is the input.
    child_groups: dict[int, list[str]] = {n: [] for n in hrse_open}
    for issue in hrse_issues:
        for match in re.finditer(r"(?:part of|parent)\s+#(\d+)", issue.get("body") or "", re.I):
            parent = int(match.group(1))
            if parent in child_groups:
                child_groups[parent].append(rankings[issue["number"]].group)
    for issue in hrse_issues:
        number = issue["number"]
        ranking = rankings[number]
        labels = _labels(issue)
        if EPIC_LABEL in labels:
            # `inherit_epic_group` still runs first, unchanged -- its result
            # (what an epic WOULD rank via its children) is what
            # `_demote_if_not_ready` cites in the demotion reason below.
            ranking = inherit_epic_group(ranking, child_groups.get(number, []))
        ranking = _demote_if_not_ready(
            ranking, labels, dependencies(HRSE_REPO, issue), all_hrse_open, forge_open)
        hrse_board = board_fields.get((HRSE_REPO, number), {})
        entries.append(TriageEntry(
            repo="hrse", issue=number, title=issue["title"],
            group=ranking.group, group_why=ranking.why,
            board_tier=hrse_board.get("Tier"),
            theme=hrse_board.get("Theme"), venture=hrse_board.get("Venture"),
            milestone=(issue.get("milestone") or {}).get("title") or milestone,
            referenced_by=reverse_refs.get(number, []),
            hint=ranking.hint,
        ))

    forge_bodies = {i["number"]: i for i in forge_issues}
    for number in sorted(forge_open):
        meta = forge_meta.get(number, {"theme": None, "venture": None, "title": ""})
        refs = references.get(number, [])
        source = forge_bodies.get(number, {})
        labels = _labels(source)
        ranking = rank(meta["title"] or source.get("title", ""),
                       source.get("body") or "", labels)
        deps = dependencies(FORGE_REPO, {"number": number, "body": source.get("body")})
        ranking = _demote_if_not_ready(ranking, labels, deps, all_hrse_open, forge_open)
        forge_board = board_fields.get((FORGE_REPO, number), {})
        entries.append(TriageEntry(
            repo="forge", issue=number, title=meta["title"] or source.get("title", ""),
            group=ranking.group, group_why=ranking.why,
            board_tier=forge_board.get("Tier"),
            theme=meta["theme"], venture=meta["venture"],
            bucket="A" if number in block_hits else ("B" if refs else "C"),
            referenced_by=refs,
            hint=ranking.hint,
        ))
    return entries


def run_triage(cache_dir: str | None = None,
               milestone: str | None = None,
               board_fields: dict[tuple[str, int], dict[str, str]] | None = None,
               ) -> list[TriageEntry]:
    forge_issues = fetch_open_issues(FORGE_REPO)
    forge_open = {i["number"] for i in forge_issues}
    forge_meta = fetch_forge_theme_venture(cache_dir)

    # NC3: reuse `milestone_summary_source`. It fails CLOSED on every
    # ambiguous case (SystemExit), orders `2.10` above `2.8` via
    # `re.findall(r"\d+")` rather than `float()`, and — the part the triage's
    # own `fetch_open_issues` cannot do — returns `labels`, which the ranking
    # rule needs for steps 1/2/5/6. It also passes the milestone TITLE to
    # `gh issue list --milestone`, so the REST number trap (milestone 2.9 is
    # number 3; `-f milestone=2.9` silently returns the 2.8 set) never arises.
    # scripts/ -> sprint-plan/ -> skills/ -> .claude/ -> repo root, so [4].
    # hrse#1477: the caller injects its already-resolved milestone so ONE
    # resolution feeds both the page header and this section. They diverged
    # live before this existed — the summary resolved `2.8` and the triage
    # `2.9` — and the cause was not the priorities path (both read the same
    # file and anchor) but the REPO SET: `milestone_summary.py` counts
    # `MILESTONE_REPOS` (hrse + cymagraph-infra) while this counted hrse
    # alone, and all seven open 2.8 items are cymagraph-infra's. The page
    # would have rendered a 2.8 header above a section listing 2.9 issues.
    if milestone is None:
        priorities = _home.docs_dir() / "PRIORITIES.md"  # harmonic-forge#708
        # `MILESTONE_REPOS`, not hrse alone: CLAUDE.md states hrse AND
        # cymagraph-infra both carry CymaGraph's release milestones, so
        # resolving the current release from one of them is a defect on its
        # own terms. This deliberately changes behaviour shipped in 2.8.
        milestone, _correction = resolve_milestone(
            read_anchor(priorities),
            milestone_counts(MILESTONE_REPOS),
            known_milestones(MILESTONE_REPOS),
        )
    hrse_issues = fetch_issues(HRSE_REPO, milestone)
    # Every open hrse issue, unfiltered — the bucket signal is repo-wide.
    reference_sources = fetch_open_issues(HRSE_REPO)
    return classify(forge_open, forge_meta, hrse_issues, forge_issues, milestone,
                    hrse_reference_sources=reference_sources,
                    board_fields=board_fields)


_GROUP_HEADINGS = {
    "1": "Group 1 — adds capability to a pipeline stage",
    "2": "Group 2 — an external clock is running",
    "3": "Group 3 — integrity of the objects the pipeline runs on",
    "4": "Group 4 — no pipeline leverage this cycle",
    # hrse#1523: relevant but not pickable -- blocked on an open dependency,
    # the `not-ready` label, or an epic with no code of its own. An empty
    # heading here (nothing demoted) is a valid, useful result, not a
    # failure -- see `_demote_if_not_ready`.
    "NOT_READY": "NOT READY — relevant, but blocked, not-ready, or an epic",
    "LATER": "LATER — explicitly deferred",
    "UNCLASSIFIED": "UNCLASSIFIED — names no code surface the map recognizes",
}


def render(entries: list[TriageEntry]) -> str:
    """Grouped by pipeline `group`, with `repo` as a per-row attribute
    (hrse#1477's AC1).

    Group 1's implemented claim is the weaker "adds capability to a pipeline
    stage", not its real definition ("something he hit while actually using
    the product"), which is not derivable from issue text. The heading says
    so rather than implying the stronger reading.
    """
    lines = []
    for key, heading in _GROUP_HEADINGS.items():
        group = [e for e in entries if e.group == key]
        if not group:
            continue
        lines.append(f"{heading} ({len(group)})")
        for e in sorted(group, key=lambda x: (x.repo, x.issue)):
            meta = ""
            if e.theme or e.venture:
                meta = f" [{e.theme or '(no Theme)'}/{e.venture or '(no Venture)'}]"
            bucket = f" bucket {e.bucket}" if e.bucket else ""
            lines.append(f"  {e.repo}#{e.issue}{meta}{bucket} {e.title}")
            lines.append(f"      why: {e.group_why}")
            if e.hint:
                # hrse#1523: advisory only -- present even on a row that
                # kept its group, since the hint never demotes on its own.
                lines.append(f"      hint: {e.hint}")
            for ref in e.referenced_by[:2]:
                lines.append(
                    f"    <- {ref['repo']}#{ref['issue']}: {ref['sentence'][:140]}")
        lines.append("")

    ok, message = check_ratchet(
        [Ranking(e.group, e.group_why) for e in entries if e.repo == "hrse"])
    lines.append(message if ok else f"!! {message}")
    return "\n".join(lines)


#: hrse#1604 retired `SUMMARY_ROWS_PER_GROUP = 5`.
#:
#: hrse#1477 introduced it as "a navigation aid, not a full dump", with the
#: heading carrying the TRUE count so the elision was visible rather than
#: silent. Visible was not enough: a group of 11 rendered `(5 of 11)` and the
#: other six were simply unreachable from that section, which is what hrse#1604
#: reported. Removing the cap rather than making the section scrollable keeps
#: ONE code path and ONE row count, so the terminal and web renderings agree by
#: construction instead of by discipline.


def render_summary_section(entries: list[TriageEntry], milestone: str) -> list[str]:
    """The sprint summary's pipeline-relevance section (hrse#1477).

    **A separate emitter from `render()`, deliberately.** `render()` is
    `main()`'s terminal renderer and `test_forge_pipeline_triage.py` pins its
    shape; this one must satisfy `board_dashboard_renderer.py`'s line grammar,
    which parses markdown into HTML and renders anything unmatched as
    `class="unparsed"` — red monospace on the live page. Two of `render()`'s
    four line kinds do exactly that: the `<-` citation and the `!!` ratchet
    both start with characters the grammar rejects. So the citations and the
    `why:` lines are dropped here (the navigation-aid argument above), and the
    ratchet leads with `**`, which renders as a paragraph.

    **The milestone is in the heading on purpose.** It is passed in rather
    than re-resolved so the section can never silently disagree with the page
    header — they diverged live before this was wired: the summary resolved
    `2.8` while the triage resolved `2.9`, because the two used different repo
    sets. Carrying it in the text makes any future divergence visible instead
    of silent.

    **Only forge rows carry a `[Theme/Venture]` segment.** `classify()`
    constructs hrse entries with `theme=None, venture=None` (45/45 measured
    live), so the segment is conditional exactly as `render()` does it.
    Fabricating a placeholder would put invented data on the page.
    """
    lines: list[str] = []
    for key, heading in _GROUP_HEADINGS.items():
        group = sorted((e for e in entries if e.group == key),
                       key=lambda x: (x.repo, x.issue))
        if not group:
            continue
        # hrse#1604: every row, and a plain count. The `(N of M)` form is gone
        # because nothing can produce it any more — a conditional whose second
        # branch is unreachable is a claim the code cannot make good on.
        lines += ["", f"## Pipeline relevance — {milestone} — {heading} "
                      f"({len(group)})", ""]
        for entry in group:
            # hrse#1522: `Ref | Tier | Theme | Title`, a fourth row shape
            # `board_dashboard_renderer.py` parses on its own regex — the
            # previous em-dash line (`- repo#issue — [Theme/Venture] title`)
            # collapsed to the two-column held-back grammar, silently
            # swallowing the bracket text into the title cell. `Tier` here
            # is the BOARD field (fast|standard|deep|unset); `Theme` is
            # populated for both repos now (hrse#1522 plumbing).
            tier = entry.board_tier or "unset"
            theme = entry.theme or "unset"
            title = entry.title
            # hrse#1523 preclose finding: AC5's cause and AC3's hint must
            # reach THIS page, not only `render()`'s terminal output --
            # appended to the title cell (safe under the grammar: the row
            # regex's `title` group is `.*$`, so trailing plain text never
            # changes which table a row lands in). A NOT_READY row shows
            # its demotion cause; any other row shows its advisory hint,
            # if one matched.
            if key == "NOT_READY":
                title = f"{title} — {entry.group_why}"
            elif entry.hint:
                title = f"{title} — hint: {entry.hint}"
            lines.append(f"- {entry.repo}#{entry.issue} [{tier} | {theme}] {title}")

    hrse_rankings = [Ranking(e.group, e.group_why) for e in entries
                     if e.repo == "hrse"]
    _ok, message = check_ratchet(hrse_rankings)
    lines += ["", f"**Ratchet:** {message}"]
    return lines


def render_summary_failure(exc: BaseException) -> list[str]:
    """The section's failure line (hrse#1477).

    Never crash the summary and never omit the section silently: on the
    6-hour unattended timer a silent omission is indistinguishable from
    "nothing changed", and a crash stops the dashboard updating at all.

    **Raw exception text is not safe to pass through.** `gh_json`'s message
    interpolates raw multi-line `result.stderr`, and every one of these was
    verified to render as `unparsed`: a line starting `- gh: could not
    authenticate` (the row regex needs an em dash), a bare JSON body, and
    anything leading with `!!` or `<-`. So the text is collapsed to one line
    and the whole thing leads with `**`, which the grammar renders as a
    paragraph.
    """
    detail = " ".join(str(exc).split()) or exc.__class__.__name__
    return ["", f"**Triage unavailable:** {detail}"]


def main() -> int:
    try:
        entries = run_triage()
    except TriageError as exc:
        print(f"forge_pipeline_triage: {exc}", file=sys.stderr)
        return 1
    print(render(entries))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
