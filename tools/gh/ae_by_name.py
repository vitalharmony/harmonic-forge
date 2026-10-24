#!/usr/bin/env python3
"""The one `**AE by name:**` grammar (harmonic-forge#1179, reforged after preclose pass 2).

A Lane 3 spec flags the cases the AE must name, and the AE answers on its own
line:

    spec:  **AE by name:** none
           **AE by name:** TC1: <what>; TC2: <what>
    AE:    **AE by name:** none
           **AE by name:** TC1, TC2

Every consumer parses through this module: `post_lane_discussion.py` at
spec-post time, and `l1_post.py` at AE time and sweep time. Passes 1 and 2
each found the copies disagreeing: two files, each re-matching the line with
its own regex, so a near miss, a padded id or a second line could pass one
check and fail the other. With one grammar, the spec-post check and the AE-time
re-check give the same verdict by construction.

The rules:
- Any unfenced line that mentions "AE by name" is the line and must parse
  exactly. A near miss (`- **AE by name:**`, `**AE by name**:`,
  `**Needing AE by name:**`) is refused, never read as absent: absent means
  "flags nothing", which is the hrse#2512 failure.
- At most one such line per body.
- A spec value is `none`, or `; `-separated entries, each `<case id>:` and a
  non-empty description.
- An AE value is `none`, or `,`-separated case ids.
- Case ids are canonical everywhere: `TC01`, `TC1`, `tc 1` and `P1` are case
  `1`, matching `l1_post._canonical_case_id`.
"""
from __future__ import annotations

import re

#: The exact line. Case-insensitive, as the first version was.
_LINE = re.compile(r"(?i)^[ \t]*\*\*AE by name:\*\*[ \t]*(.*?)[ \t]*$")
#: Any mention of the flag. A mention that is not `_LINE` is a near miss.
_MENTION = re.compile(r"(?i)\bAE[ \t]+by[ \t]+name\b")
_CASE = re.compile(r"(?i)^(?:TC|P)[- ]?(\d+)$")
#: A sweep's carried-forward case (R-0366): `n. TCn — carried forward, …`.
_CARRIED = re.compile(r"(?im)^[ \t]*(?:\d+\.|[-*])[ \t]*TC[- ]?(\d+)[ \t]*[:—-]+[ \t]*carried forward")


class ByNameError(ValueError):
    """The body's `AE by name` line does not parse. The message says why."""


def canonical(case: str) -> str:
    """`TC01` -> `1`. Raises ByNameError for anything that is not a case id."""
    match = _CASE.match(case.strip())
    if not match:
        raise ByNameError(f"{case.strip()!r} is not a case id like TC2")
    return str(int(match.group(1)))


def _value(body: str, where: str) -> str | None:
    """The one value on the body's `AE by name` line, or None when it has none."""
    values, fenced = [], False
    for line in body.splitlines():
        if line.lstrip().startswith("```"):
            fenced = not fenced
            continue
        if fenced or not _MENTION.search(line):
            continue
        match = _LINE.match(line)
        if not match:
            raise ByNameError(
                f"{where} line {line.strip()!r} mentions `AE by name` but is not the line; "
                "write it exactly as `**AE by name:** …`")
        values.append(match.group(1))
    if len(values) > 1:
        raise ByNameError(f"{where} carries {len(values)} `**AE by name:**` lines; it takes at most one")
    return values[0] if values else None


def parse_spec(body: str) -> set[str]:
    """The canonical case ids a spec flags; an absent line or `none` flags nothing."""
    value = _value(body, "the spec's")
    if value is None or value.lower() == "none":
        return set()
    flagged = set()
    for entry in value.split(";"):
        case, sep, what = entry.partition(":")
        if not sep or not what.strip():
            raise ByNameError(f"the spec's entry {entry.strip()!r} needs `TCn: <what>`, with a description")
        flagged.add(canonical(case))
    return flagged


def parse_ae(body: str) -> set[str] | None:
    """The canonical case ids an AE names, or None when it carries no line."""
    value = _value(body, "the AE's")
    if value is None:
        return None
    if value.lower() == "none":
        return set()
    return {canonical(entry) for entry in value.split(",")}


def carried(sweep_body: str) -> set[str]:
    """The canonical case ids a sweep carries forward (R-0366)."""
    return {str(int(case)) for case in _CARRIED.findall(sweep_body or "")}


def label(cases: set[str]) -> str:
    return ", ".join("TC" + case for case in sorted(cases, key=int))
