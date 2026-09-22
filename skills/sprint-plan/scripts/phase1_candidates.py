"""Six candidate evidence-scoping rules for hrse#917 phase 1.

Each candidate answers one question per mention: does a "closed"/"merged"
token count as evidence that THIS mention is stale-safe? They differ only in
how far they let evidence travel to reach a mention — from "anywhere in the
document" (C3, the negative control) down to "grammatically attached to this
exact reference" (C5/C6). None of this touches `stale_closed_mentions()` —
production behaviour is unchanged; these run only under `--phase1-report`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

from phase1_mentions import (
    H2Region,
    Mention,
    context,
    h2_regions,
    paragraph_span,
    sentence_span,
    unit_span,
)

_CLOSED_WORD = re.compile(r"\bclosed\b|\bmerged\b", re.IGNORECASE)
# How close a status token must sit to a mention, same sentence, to count as
# grammatically attached rather than merely co-located (C5/C6). Tuned against
# the live doc's two shapes: "#NNN — closed …" (~3 chars) and the #860
# false-clean trap ("With those merged, … except #860", 52 chars measured
# live) — 20 comfortably separates them without excluding the em-dash shape.
_ADJACENCY_WINDOW = 20


@dataclass(frozen=True)
class CandidateResult:
    outcome: str  # "FLAG" | "CLEAR"
    reason_code: str
    evidence_token: str | None
    evidence_direction: str | None
    evidence_span: tuple[int, int] | None


def _evidence_in_span(text: str, start: int, end: int) -> re.Match | None:
    return _CLOSED_WORD.search(text, start, end)


def _clear(token_match: re.Match, direction: str, reason: str) -> CandidateResult:
    return CandidateResult("CLEAR", reason, token_match.group(0), direction, token_match.span())


def _flag(reason: str) -> CandidateResult:
    return CandidateResult("FLAG", reason, None, None, None)


def c1_sentence(mention: Mention, text: str, region: H2Region) -> CandidateResult:
    s, e = sentence_span(mention.start, text, region)
    m = _evidence_in_span(text, s, e)
    return _clear(m, "same-sentence", "sentence-evidence") if m else _flag("no-sentence-evidence")


def c2_paragraph(mention: Mention, text: str, region: H2Region) -> CandidateResult:
    s, e = paragraph_span(mention.start, text, region)
    m = _evidence_in_span(text, s, e)
    return _clear(m, "same-paragraph", "paragraph-evidence") if m else _flag("no-paragraph-evidence")


def c4_structural_unit(mention: Mention, text: str, region: H2Region) -> CandidateResult:
    s, e = unit_span(mention.start, text, region)
    m = _evidence_in_span(text, s, e)
    return _clear(m, "same-unit", "unit-evidence") if m else _flag("no-unit-evidence")


def _other_hrse_number_between(text: str, a: int, b: int, issue: int) -> bool:
    """True if a *different* bare `#NNN` sits between offsets a and b —
    a competing subject that would make evidence binding ambiguous."""
    for m in re.finditer(r"#(\d+)", text[a:b]):
        if int(m.group(1)) != issue:
            return True
    return False


def _c5_result(mention: Mention, text: str, region: H2Region) -> CandidateResult:
    s, e = sentence_span(mention.start, text, region)

    # Rule A: evidence adjacent to (not merely co-located with) the mention,
    # within its own sentence, with nothing else competing for the subject.
    window_start = max(s, mention.start - _ADJACENCY_WINDOW)
    window_end = min(e, mention.end + _ADJACENCY_WINDOW)
    for m in _CLOSED_WORD.finditer(text, window_start, window_end):
        if m.start() >= mention.end:
            if not _other_hrse_number_between(text, mention.end, m.start(), mention.issue):
                return _clear(m, "same-sentence-adjacent", "subject-bound-adjacent")
        elif m.end() <= mention.start:
            if not _other_hrse_number_between(text, m.end(), mention.start, mention.issue):
                return _clear(m, "same-sentence-adjacent", "subject-bound-adjacent")

    # Rule B: forward status-continuation — the very next sentence in the
    # same region opens with the predicate, and nothing else in this
    # mention's own sentence competes for it.
    if not _other_hrse_number_between(text, mention.end, e, mention.issue):
        next_m = re.match(r"[^.!?]*[.!?]+(?:\s+|$)|[^.!?]+$", text[e:region.end])
        if next_m:
            s2, e2 = e + next_m.start(), e + next_m.end()
            lead = text[s2:e2].lstrip(" \t\n*_>\"'")
            token = _CLOSED_WORD.match(lead)
            if token:
                abs_start = s2 + (len(text[s2:e2]) - len(lead))
                span = (abs_start, abs_start + (token.end() - token.start()))
                return CandidateResult(
                    "CLEAR", "subject-bound-forward-continuation",
                    text[span[0]:span[1]], "forward-continuation", span,
                )

    return _flag("no-bound-evidence")


def c5_subject_bound(mention: Mention, text: str, region: H2Region) -> CandidateResult:
    return _c5_result(mention, text, region)


def c6_role_aware(mention: Mention, text: str, region: H2Region) -> CandidateResult:
    if mention.role == "citation":
        return CandidateResult("CLEAR", "citation-role", None, None, None)
    return _c5_result(mention, text, region)


def c3_per_issue_document_wide(
    mentions: list[Mention], text: str
) -> dict[str, CandidateResult]:
    """Negative control: evidence anywhere for an issue clears every
    occurrence of it, ignoring H2 boundaries entirely — deliberately the
    same unbound-evidence bug as today's code, at document scope."""
    whole_doc = H2Region("(whole document)", 0, len(text))
    has_evidence: dict[tuple[str, int], CandidateResult] = {}
    for mention in mentions:
        if mention.is_board_link:
            continue
        result = c1_sentence(mention, text, whole_doc)
        key = (mention.repo, mention.issue)
        if result.outcome == "CLEAR" and key not in has_evidence:
            has_evidence[key] = result

    out: dict[str, CandidateResult] = {}
    for mention in mentions:
        if mention.is_board_link:
            continue
        key = (mention.repo, mention.issue)
        if key in has_evidence:
            e = has_evidence[key]
            out[mention.mention_id] = CandidateResult(
                "CLEAR", "document-wide-aggregate-evidence",
                e.evidence_token, "document-wide", e.evidence_span,
            )
        else:
            out[mention.mention_id] = _flag("no-document-wide-evidence")
    return out


# Per-mention candidates (C1, C2, C4, C5, C6) share one calling shape;
# C3 is per-document because it aggregates across all occurrences first.
PER_MENTION_CANDIDATES: dict[str, Callable[[Mention, str, H2Region], CandidateResult]] = {
    "C1": c1_sentence,
    "C2": c2_paragraph,
    "C4": c4_structural_unit,
    "C5": c5_subject_bound,
    "C6": c6_role_aware,
}

CANDIDATE_IDS = ["C1", "C2", "C3", "C4", "C5", "C6"]


def evaluate_all(mentions: list[Mention], text: str) -> dict[str, dict[str, CandidateResult]]:
    """{candidate_id: {mention_id: CandidateResult}} for every non-board-link
    mention. Board-link mentions are handled separately by the report layer
    (they are EXCLUDE rows for every candidate, not a clearing decision)."""
    regions = h2_regions(text)
    results: dict[str, dict[str, CandidateResult]] = {cid: {} for cid in CANDIDATE_IDS}
    for mention in mentions:
        if mention.is_board_link:
            continue
        region = next(r for r in regions if r.start <= mention.start < r.end)
        for cid, fn in PER_MENTION_CANDIDATES.items():
            results[cid][mention.mention_id] = fn(mention, text, region)
    results["C3"] = c3_per_issue_document_wide(mentions, text)
    return results
