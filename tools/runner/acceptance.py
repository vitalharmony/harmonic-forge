"""The one acceptance-section matcher the te-runner uses (harmonic-forge#1112 AC11).

`preclose_driver.acceptance_criteria`, `te_run.ac_titles` and the preclose
brief's one-assumption-per-criterion list (AC14) all read an issue body through
here, so a body one of them accepts is a body every one of them accepts.

The heading is case-insensitive and may carry trailing text: `## Acceptance
criteria`, `## Acceptance`, `### acceptance criteria (amended)`. The section
runs to the next heading of the same or a higher level.
"""
from __future__ import annotations

import re

HEADING = re.compile(r"^(#{1,6})\s*Acceptance(\s+criteria)?\b.*$", re.IGNORECASE | re.MULTILINE)
#: A top-level criterion: a numbered item or a checkbox item, not indented.
ITEM = re.compile(r"^(?:\d+\.|[-*]\s+\[[ xX]\])\s+(.*\S)")


def section(body: str) -> str | None:
    """The first acceptance section's text, stripped; None when there is none."""
    match = HEADING.search(body or "")
    if not match:
        return None
    level = len(match.group(1))
    rest = body[match.end():]
    end = re.search(rf"^#{{1,{level}}}\s", rest, re.MULTILINE)
    return (rest[:end.start()] if end else rest).strip()


def items(text: str | None) -> list[str]:
    """Each top-level criterion's text, in order."""
    found = []
    for line in (text or "").splitlines():
        match = ITEM.match(line)
        if match:
            found.append(match.group(1).strip())
    return found
