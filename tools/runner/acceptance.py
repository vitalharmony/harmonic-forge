"""The one acceptance-section matcher the te-runner uses (harmonic-forge#1112 AC11).

`acceptance_criteria` (preflight (f)'s before-spend refusal) and
`te_run.ac_titles` both read an issue body through here, so a body one of them
accepts is a body every one of them accepts.

The heading is case-insensitive and anchored: `## Acceptance criteria`,
`## Acceptance`, `### acceptance criteria (amended)`, with an optional trailing
colon, and nothing else on the line, so `## Acceptance testing notes` or a
wrapped table cell starting `## Acceptance criteria\s*$`; ...` never matches
(harmonic-forge#1112 sticky-wicket patch 1). Lines inside fenced code are
never headings. The section runs to the next heading of the same or a higher
level.
"""
from __future__ import annotations

import re
import subprocess

import sandbox

HEADING = re.compile(r"^(#{1,6})\s*Acceptance(\s+criteria)?\s*(\(.*\))?\s*:?\s*$",
                     re.IGNORECASE)
FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
#: A top-level criterion: a numbered item or a checkbox item, not indented.
ITEM = re.compile(r"^(?:\d+\.|[-*]\s+\[[ xX]\])\s+(.*\S)")


def _unfenced(lines: list[str]) -> list[bool]:
    """For each line, whether it sits outside fenced code (fence lines count as inside).
    A fence closes only on the opening's character, at least as long, with
    nothing after it but spaces (CommonMark)."""
    outside: list[bool] = []
    opening: str | None = None
    for line in lines:
        match = FENCE.match(line)
        if opening is None and match:
            opening = match.group(1)
            outside.append(False)
        elif opening is not None:
            if (match and match.group(1)[0] == opening[0]
                    and len(match.group(1)) >= len(opening) and not match.group(2).strip()):
                opening = None
            outside.append(False)
        else:
            outside.append(True)
    return outside


def section(body: str) -> str | None:
    """The first acceptance section's text, stripped; None when there is none."""
    lines = (body or "").splitlines()
    outside = _unfenced(lines)
    start = next((n for n, line in enumerate(lines) if outside[n] and HEADING.match(line)), None)
    if start is None:
        return None
    level = len(HEADING.match(lines[start]).group(1))
    end_heading = re.compile(rf"^#{{1,{level}}}\s")
    end = next((n for n in range(start + 1, len(lines))
                if outside[n] and end_heading.match(lines[n])), len(lines))
    return "\n".join(lines[start + 1:end]).strip()


def items(text: str | None) -> list[str]:
    """Each top-level criterion's text, in order."""
    found = []
    for line in (text or "").splitlines():
        match = ITEM.match(line)
        if match:
            found.append(match.group(1).strip())
    return found


def issue_body(repo: str, issue: int, env: dict[str, str]) -> str:
    try:
        result = subprocess.run(["gh", "api", f"repos/{repo}/issues/{issue}", "--jq", ".body"],
                                env=env, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise sandbox.RunnerEnvError(f"cannot read {repo}#{issue}: {exc}") from exc
    if result.returncode:
        raise sandbox.RunnerEnvError(f"cannot read {repo}#{issue}: {result.stderr.strip()}")
    return result.stdout


def acceptance_criteria(repo: str, issue: int, env: dict[str, str]) -> str:
    """The acceptance section through the shared matcher (harmonic-forge#1112 AC11)."""
    text = section(issue_body(repo, issue, env))
    if not text:
        raise sandbox.RunnerEnvError(f"{repo}#{issue} has no acceptance criteria section")
    # A section with no criterion items is refused, never one blanket
    # assumption (harmonic-forge#1112 sticky-wicket patch 1).
    if not items(text):
        raise sandbox.RunnerEnvError(f"{repo}#{issue}'s acceptance section lists no criteria")
    return text
