"""Span-preserving issue-mention extraction for hrse#917 phase 1.

Extracts every issue mention (and every board-link exclusion) from a
PRIORITIES.md-shaped document exactly once, independent of coverage mode or
candidate scoping rule — `phase1_candidates.py` decides per-mention outcomes
against the structural spans this module computes; `phase1_report.py` decides
which mentions are in-scope for a given coverage mode. Keeping extraction,
evidence-scoping, and coverage separate is what lets `mention_id` stay stable
across every (candidate, coverage) combination the report emits.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

HRSE_REPO = "vitalharmony/hrse"
FORGE_REPO = "vitalharmony/harmonic-forge"

_H2 = re.compile(r"^##\s+(.*)$", re.MULTILINE)
_BLOCK_START = re.compile(r"^(?:\d+[a-z]?\.\s|-\s)", re.MULTILINE)
_BOARD_LINK = re.compile(r"\[(hrse|forge)\s*#(\d+)\]\([^)]+\)", re.IGNORECASE)
_FORGE_REF = re.compile(
    r"(?:harmonic-forge|forge)#(\d+(?:[-–/]#?\d+)*)", re.IGNORECASE
)
# The doc's real range form is en-dash *plus* a repeated `#` (`#794–#797`),
# not just a bare second number (`#794-797`) — both are accepted.
_ISSUE_REF = re.compile(r"#(\d+)(?:[-–]#?(\d+))?")
# Sentence-ish spans: greedy run up to a terminator, including trailing
# whitespace, so spans tile the whole text with no gaps.
_SENTENCE = re.compile(r"[^.!?]*[.!?]+(?:\s+|$)|[^.!?]+$")
_PARAGRAPH_BREAK = re.compile(r"\n[ \t]*\n")

# Citation-role heuristics (used by C6, see phase1_candidates.py). Kept here
# because they operate on the same raw text/spans this module already parses.
_CITATION_CLUSTER = re.compile(r"(?:#\d+\s*(?:→|/|,)\s*){2,}#\d+")
_CITATION_CUES = re.compile(
    r"\bbuilt to eliminate\b|\bin a single day\b|\bsame (?:shape|divergence class)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class H2Region:
    title: str
    start: int
    end: int


@dataclass
class Mention:
    repo: str
    issue: int
    occurrence: int  # 1-based, per (repo, issue), in document order
    start: int
    end: int  # span of the literal mention token, not the whole reference
    raw_text: str  # the matched reference text (e.g. "#794–#797")
    line: int
    col: int
    heading: str
    unit_kind: str  # "preamble" | "list-item" | "prose"
    is_board_link: bool = False
    role: str = "claim"  # "claim" | "citation" | "uncertain"

    @property
    def mention_id(self) -> str:
        return f"{self.repo}:{self.issue}:{self.occurrence}"


def h2_regions(text: str) -> list[H2Region]:
    """Preamble plus every `## ` heading region, covering 0..len(text)."""
    matches = list(_H2.finditer(text))
    if not matches:
        return [H2Region("(preamble)", 0, len(text))]
    regions = []
    if matches[0].start() > 0:
        regions.append(H2Region("(preamble)", 0, matches[0].start()))
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        regions.append(H2Region(m.group(1).strip(), m.start(), end))
    return regions


def heading_for(offset: int, regions: list[H2Region]) -> str:
    for region in regions:
        if region.start <= offset < region.end:
            return region.title
    return regions[-1].title if regions else "(preamble)"


def _unit_kind(offset: int, text: str, region: H2Region) -> str:
    """"list-item" if inside a bullet/numbered block, else "prose"/"preamble"."""
    if region.title == "(preamble)":
        # Preamble may itself contain bullets (e.g. a legend table) — still
        # distinguish list-item vs bare prose within it.
        pass
    local_starts = [
        m.start() for m in _BLOCK_START.finditer(text, region.start, region.end)
    ]
    if not local_starts or offset < local_starts[0]:
        return "preamble" if region.title == "(preamble)" else "prose"
    return "list-item"


def sentence_span(offset: int, text: str, region: H2Region) -> tuple[int, int]:
    """The sentence containing `offset`, clipped to `region` (H2 is a hard
    evidence barrier — no sentence may cross a heading boundary)."""
    for m in _SENTENCE.finditer(text, region.start, region.end):
        if m.start() <= offset < m.end():
            return m.start(), m.end()
    return region.start, region.end


def paragraph_span(offset: int, text: str, region: H2Region) -> tuple[int, int]:
    """Blank-line-bounded paragraph containing `offset`, clipped to `region`."""
    breaks = [region.start] + [
        m.end() for m in _PARAGRAPH_BREAK.finditer(text, region.start, region.end)
    ] + [region.end]
    for a, b in zip(breaks, breaks[1:]):
        if a <= offset < b:
            return a, b
    return region.start, region.end


def unit_span(offset: int, text: str, region: H2Region) -> tuple[int, int]:
    """C4's structural unit: the enclosing top-level list item if the offset
    is inside one, else the enclosing heading-bounded prose run (== region)."""
    starts = [m.start() for m in _BLOCK_START.finditer(text, region.start, region.end)]
    if not starts or offset < starts[0]:
        return region.start, region.end
    ends = starts[1:] + [region.end]
    for a, b in zip(starts, ends):
        if a <= offset < b:
            return a, b
    return region.start, region.end


def context(text: str, start: int, end: int, radius: int = 80) -> str:
    lo = max(0, start - radius)
    hi = min(len(text), end + radius)
    before = text[lo:start].replace("\n", " ")
    marked = text[start:end]
    after = text[end:hi].replace("\n", " ")
    return f"{before}»{marked}«{after}"


# Headings that are retrospective BY CONSTRUCTION -- a record of what happened,
# where naming a closed issue is the point rather than a stale claim.
#
# `## Dependencies that bite` is deliberately NOT here, though it carries the
# most flags. Both of this suite's adversarial canaries (#860, #858) live in
# that section and are GENUINE drift; blanket-clearing it fails them, which is
# the false-clean direction phases 1-2 exist to prevent. Verified by running
# the suite: adding it turns both canaries green-when-they-should-be-red.
_RETROSPECTIVE_SECTION = re.compile(
    r"\b(settled|closed|archived|resolved|consolidation|history|historical"
    r"|retrospective)\b", re.IGNORECASE)


def _enclosed_by(text: str, start: int, end: int, lo: int, hi: int,
                 opener: str, closer: str) -> bool:
    """Is [start,end) inside an unclosed `opener` within [lo,hi)?

    Scoped to the sentence/paragraph rather than the LINE: cuts.md and
    PRIORITIES.md both wrap parentheticals across lines, and a line-scoped
    check misses every wrapped one (hrse#917 phase 3, measured).
    """
    before = text[lo:start]
    if opener == closer:                      # quotes: odd count means inside
        return before.count(opener) % 2 == 1
    return before.rfind(opener) > before.rfind(closer)


def is_citation_shaped(offset_start: int, offset_end: int, text: str,
                       region: H2Region) -> str | None:
    """Structural marks that make a mention a REFERENCE rather than a claim.

    Each is a typographic fact about where the mention sits, not an
    interpretation of what the sentence means -- and critically, none of them
    touches what counts as CLOSURE EVIDENCE. hrse#917 phases 1/2 exist because
    loosening evidence binding fails toward false-clean; this layer is
    orthogonal to it and leaves the evidence rules exactly as they are.

    Measured on the live doc: these four cover 54 of 67 flags, and the
    remainder turned out to be genuine drift rather than noise.
    """
    if _RETROSPECTIVE_SECTION.search(getattr(region, "title", "") or ""):
        return "retrospective-section"
    para_start, para_end = paragraph_span(offset_start, text, region)
    if _enclosed_by(text, offset_start, offset_end, para_start, para_end, "`", "`"):
        return "backticked"          # the token is the subject of discussion
    if _enclosed_by(text, offset_start, offset_end, para_start, para_end, "(", ")"):
        return "parenthetical"       # an aside, not the sentence's assertion
    if _enclosed_by(text, offset_start, offset_end, para_start, para_end, '"', '"'):
        return "quoted"              # someone else's words, quoted
    return None


def classify_role(offset_start: int, offset_end: int, text: str, region: H2Region) -> str:
    """Deterministic claim/citation heuristic. Uncertain defaults to "claim"
    per NC — a mis-classified citation costs a false positive; a
    mis-classified claim costs a silent false-clean, the worse direction."""
    sent_start, sent_end = sentence_span(offset_start, text, region)
    sentence = text[sent_start:sent_end]
    for m in _CITATION_CLUSTER.finditer(text, region.start, region.end):
        if m.start() <= offset_start and offset_end <= m.end():
            return "citation"
    if _CITATION_CUES.search(sentence):
        return "citation"
    if is_citation_shaped(offset_start, offset_end, text, region):
        return "citation"
    return "claim"


def _line_col(text: str, offset: int) -> tuple[int, int]:
    line = text.count("\n", 0, offset) + 1
    last_nl = text.rfind("\n", 0, offset)
    col = offset - last_nl if last_nl != -1 else offset + 1
    return line, col


def _expand_range(low: str, high: Optional[str]) -> list[int]:
    lo = int(low)
    if high is None:
        return [lo]
    return list(range(lo, int(high) + 1))


def extract_mentions(text: str) -> list[Mention]:
    """Every mention and board-link exclusion in `text`, in document order,
    with per-(repo, issue) occurrence numbering assigned across the whole
    document — independent of any later coverage-mode restriction."""
    regions = h2_regions(text)
    exclude_spans: list[tuple[int, int]] = []
    raw: list[tuple[int, int, str, int, bool]] = []  # start, end, repo, issue, is_board_link

    for m in _BOARD_LINK.finditer(text):
        exclude_spans.append(m.span())
        repo = HRSE_REPO if m.group(1).lower() == "hrse" else FORGE_REPO
        raw.append((m.start(), m.end(), repo, int(m.group(2)), True))

    def _excluded(pos: int) -> bool:
        return any(a <= pos < b for a, b in exclude_spans)

    forge_spans: list[tuple[int, int]] = []
    for m in _FORGE_REF.finditer(text):
        if _excluded(m.start()):
            continue
        forge_spans.append(m.span())
        for part in re.split(r"[-–/]#?", m.group(1)):
            raw.append((m.start(), m.end(), FORGE_REPO, int(part), False))

    def _in_forge_or_excluded(pos: int) -> bool:
        return _excluded(pos) or any(a <= pos < b for a, b in forge_spans)

    for m in _ISSUE_REF.finditer(text):
        if _in_forge_or_excluded(m.start()):
            continue
        for issue in _expand_range(m.group(1), m.group(2)):
            raw.append((m.start(), m.end(), HRSE_REPO, issue, False))

    raw.sort(key=lambda r: r[0])
    counters: dict[tuple[str, int], int] = {}
    mentions: list[Mention] = []
    for start, end, repo, issue, is_board_link in raw:
        key = (repo, issue)
        counters[key] = counters.get(key, 0) + 1
        line, col = _line_col(text, start)
        region = next(r for r in regions if r.start <= start < r.end)
        raw_text = text[start:end]
        if is_board_link:
            unit_kind = "board-link"
            role = "n/a"
        else:
            unit_kind = _unit_kind(start, text, region)
            role = classify_role(start, end, text, region)
        mentions.append(
            Mention(
                repo=repo,
                issue=issue,
                occurrence=counters[key],
                start=start,
                end=end,
                raw_text=raw_text,
                line=line,
                col=col,
                heading=region.title,
                unit_kind=unit_kind,
                is_board_link=is_board_link,
                role=role,
            )
        )
    return mentions
