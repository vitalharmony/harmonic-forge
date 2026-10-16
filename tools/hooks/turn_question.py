#!/usr/bin/env python3
"""Shared turn-final question detector for Stop hooks (harmonic-forge#1031).

Moved byte for byte out of `block_batch_stop.py`, so that hook and
`enforce_handoff_owed.py` read "this turn ends with a genuine question" the
same way and cannot drift. The rationale for every pattern below is in the
comments that moved with it; `block_batch_stop.py`'s module docstring holds
the false-positive/false-negative argument.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

#: How many trailing transcript records to scan for the last assistant message.
#: The last turn's text is within a few records of the end; reading the whole
#: file on every Stop would make the hook's cost grow with session length.
_TAIL_RECORDS = 40

#: Question-shaped signals in the final message. Generous on purpose -- see the
#: module docstring on why false positives here are the expensive direction.
#: Compiled separately rather than joined with `|`: inline `(?im)` flags are
#: only legal at the start of a pattern, so an alternation of flagged and
#: unflagged branches raises at import -- which, in a hook, would be silent.
#: **The keyword branch is deliberately gone.** It matched a bare "which" or
#: "confirm" anywhere in the message, and measured against 776 real turn-final
#: messages in this project's transcripts it let through 5 of 5 turns whose
#: `DECISIONS NEEDED` section literally said "None" — the exact shape this hook
#: exists to block — four of them on the word "which" appearing in an unrelated
#: table. A detector that passes 100% of its primary target is not generous,
#: it is off.
#:
#: What is left is question SYNTAX, not question vocabulary: a `?` ending a
#: line, or a numbered line containing one. Combined with `_DECISIONS_RE`
#: below, that covers how a session actually asks.
#: harmonic-forge#1031 survivor 5: a `?` followed by closing emphasis
#: (`**Merge now?**`) or by more prose on the same line (`Merge now? I need
#: your answer.`) is still a question -- the first branch tolerates trailing
#: `*`/`_`, the second takes `?` followed by whitespace and more text. Table
#: rows, inline code and the excluded line kinds below are removed first, so
#: neither branch reads a cell or a code example.
#: Post-verdict check survivor 1: `Is it done? Yes, it is.` is a question the
#: session answered itself on the same line -- not asking. Pre-patch it read
#: False; the trailing-prose branch must not change that, so a `?` followed by
#: an answer word does not count.
_SELF_ANSWER = r"(?i:(?:yes|no|yeah|yep|nope|nah)\b)"
_QUESTION_RES = tuple(re.compile(pattern, flags) for pattern, flags in (
    (r"\?[*_]*\s*$", re.M),
    (r"\?[*_]*[ \t]+(?!" + _SELF_ANSWER + r")\S", re.M),
    (r"^\s*\d+\.\s+.*\?", re.M),
))

#: harmonic-forge#1031 sticky-wicket PATCH (survivors 1/2): lines that are not
#: the session asking anything. A heading (`### What changed?`) titles a
#: section, a blockquote quotes someone else, a table row is data. Syntax-level
#: exclusions only -- no vocabulary (see the keyword-branch note above).
_NOT_ASKING_LINE_RE = re.compile(r"^\s*(?:#{1,6}\s|>|\|)")
#: Post-verdict check survivor 2: per CommonMark, a fence opens with 3+ of
#: one char and closes only on a line of the SAME char, at least as long, with
#: nothing else after it. A plain toggle let `~~~` or a shorter run inside a
#: fence close it, hiding the trailing question.
_FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
_INLINE_CODE_RE = re.compile(r"`[^`\n]*`")
_LIST_MARKER_RE = re.compile(r"^(?:[-+*]|\d+[.)])\s+")

#: A `DECISIONS NEEDED` heading counts as asking ONLY when it has content. The
#: batch turn that must be blocked has exactly that heading followed by "None",
#: so treating the heading alone as a question would disable the hook on its
#: own primary case.
#: `[*_ \t]*` before the lookahead so markdown emphasis does not defeat it:
#: `**None**` matched as a live decision and allowed the stop, and 25 real
#: messages in this project use exactly that bolded form.
_DECISIONS_HEADING_RE = re.compile(
    r"^[ \t]*#{1,4}[ \t]*DECISIONS NEEDED[ \t]*$", re.I | re.M)

#: Leading markdown emphasis is stripped before this is tested, so `**None**`
#: and `*None*` read the same as `None`. Both flipped the wrong way before —
#: the lookahead saw `*`, not `N` — and 25 real messages use the bolded form.
_NO_DECISION_RE = re.compile(
    r"^(?:none|no decisions?\b|nothing\b|n/?a\b)", re.I)


def _decisions_section_has_content(text: str) -> bool:
    """True when a `DECISIONS NEEDED` heading is followed by a real decision.

    Scoped to the FIRST non-empty line under the heading. A regex spanning
    further matched content from a later section entirely — which is how
    `*None*` was read as a live decision while `**None**` was the case being
    fixed.
    """
    match = _DECISIONS_HEADING_RE.search(text)
    if not match:
        return False
    for line in text[match.end():].splitlines():
        stripped = _LIST_MARKER_RE.sub("", line.strip()).strip("*_ \t")
        if not stripped:
            continue
        if stripped.startswith("#"):
            return False   # next heading; the section was empty
        return not _NO_DECISION_RE.match(stripped)
    return False


def last_assistant_text(transcript: Path) -> str:
    try:
        lines = transcript.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    for line in reversed(lines[-_TAIL_RECORDS:]):
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if record.get("type") != "assistant":
            continue
        content = (record.get("message") or {}).get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = [b.get("text", "") for b in content
                     if isinstance(b, dict) and b.get("type") == "text"]
            if any(parts):
                return "\n".join(parts)
    return ""


def asks_a_question(text: str) -> bool:
    if not text:
        return False
    if _decisions_section_has_content(text):
        return True
    prose = _prose_lines(text)
    return any(pattern.search(prose) for pattern in _QUESTION_RES)


def _prose_lines(text: str) -> str:
    """The message minus fenced code, headings, blockquotes and table rows,
    with inline code spans removed -- what remains is the session's own
    prose, the only place it can ask."""
    kept: list[str] = []
    fence: str | None = None   # the opening run, e.g. "````" or "~~~"
    for line in text.splitlines():
        match = _FENCE_RE.match(line)
        if fence is None:
            if match and not (match.group(1)[0] == "`" and "`" in match.group(2)):
                fence = match.group(1)
                continue
        else:
            if (match and match.group(1)[0] == fence[0]
                    and len(match.group(1)) >= len(fence)
                    and not match.group(2).strip()):
                fence = None
            continue
        if _NOT_ASKING_LINE_RE.match(line):
            continue
        kept.append(_INLINE_CODE_RE.sub("", line))
    return "\n".join(kept)
