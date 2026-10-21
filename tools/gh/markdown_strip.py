"""The visible text of a GitHub comment body (harmonic-forge#1103 reforge).

One reader, shared by every recognizer that must not be fooled by quoted or
code-formatted text: `watch_lane_posts` (markers, gate headings) and `gate_ci`
(`looks_like_a_gate_report`, `verdict_of`). Four preclose passes on #1103
found a new edge of hand-rolled markdown reading each time, in two separate
copies; this module is the single copy, following CommonMark for the three
block constructs that can hide or fake a heading:

- blockquote lines (`>` after up to 3 spaces of indent -- any indent is
  accepted here, the stricter reading for a recognizer);
- fenced code blocks: a run of >= 3 backticks or tildes, indented at most 3
  spaces. A backtick opener whose info string contains a backtick is NOT a
  fence (so a line starting with an inline ```code``` span is text). The
  closer is the same character, at least as long as the opener, followed
  only by whitespace. An UNCLOSED opener strips nothing: it is read as a
  text line and reading continues (#1103 reforge fix 2 -- when the reader
  cannot decide, it keeps the text visible, the side that runs more checks);
- indented code blocks: a line indented 4+ spaces (or a tab) that follows a
  blank line, the start of the body, or another indented-code line. A
  4-space line continuing a paragraph or list item is kept as text.

Inline code spans are left as text. No dependency beyond the stdlib.
"""
from __future__ import annotations

import re

_FENCE_OPEN = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
_FENCE_CLOSE = re.compile(r"^ {0,3}(`{3,}|~{3,})[ \t]*$")
_QUOTE = re.compile(r"^[ \t]*>")
_INDENTED = re.compile(r"^(?: {4}|\t| {0,3}\t)")


def _closes(opener: str, rest: list[str]) -> bool:
    """Does a closer for `opener` appear in `rest`?"""
    for line in rest:
        m = _FENCE_CLOSE.match(line.rstrip("\r\n"))
        if m and m.group(1)[0] == opener[0] and len(m.group(1)) >= len(opener):
            return True
    return False


def visible_text(body: str) -> str:
    """`body` with blockquotes, fenced and indented code blocks removed."""
    kept: list[str] = []
    fence: str | None = None
    prev_blank = True  # start of body counts as a blank line before it
    in_indented = False
    lines = body.splitlines(keepends=True)
    for i, line in enumerate(lines):
        text = line.rstrip("\r\n")
        if fence is not None:
            m = _FENCE_CLOSE.match(text)
            if m and m.group(1)[0] == fence[0] and len(m.group(1)) >= len(fence):
                fence = None
                prev_blank = True
            continue
        blank = not text.strip()
        if in_indented and (blank or _INDENTED.match(text)):
            # Blank lines inside an indented block are part of it; the block
            # ends at the first non-blank line indented less than 4.
            prev_blank = blank
            if blank:
                kept.append(line)
            continue
        in_indented = False
        if not blank and prev_blank and _INDENTED.match(text):
            in_indented = True
            prev_blank = False
            continue
        m = _FENCE_OPEN.match(text)
        if (m and not (m.group(1)[0] == "`" and "`" in m.group(2))
                and _closes(m.group(1), lines[i + 1:])):
            fence = m.group(1)
            continue
        if _QUOTE.match(text):
            prev_blank = True  # a quote interrupts a paragraph
            continue
        kept.append(line)
        prev_blank = blank
    return "".join(kept)
