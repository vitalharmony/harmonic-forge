"""The newest handoff's `mutates-live` declaration (harmonic-forge#851).

Shared by `l1_post.py --auto-ae` (the poster's refusal) and
`check_lane3_ready.resolve_gate_authority` (the consumer's), so the two halves
of the auto-AE carve-out read one footer one way. Pure: callers fetch the
thread bodies themselves.
"""
from __future__ import annotations

import re
from collections.abc import Iterable

#: One marker, never spanning a `-->` boundary: a footer carries no `>` before
#: its own close, so `[^>]*?` cannot run from one marker into the next.
_ANY_MARKER_RE = re.compile(r"<!--\s*l1-post\s+v\d+;[^>]*?-->")
_KIND_RE = re.compile(r"kind=([\w-]+)")
_MUTATES_LIVE_RE = re.compile(r"mutates-live=(\w+)")


def trailing_footer(body: str | None) -> str | None:
    """The body's OWN attestation footer: the last `l1-post` marker, and only
    when nothing but whitespace follows it. None otherwise -- never an earlier
    marker (harmonic-forge#851 preclose pass 1 survivors 3 and 4).

    Every posting tool appends its footer last, and `reject_reserved_marker`
    keeps a second, tool-written one out of the body, so the trailing span is
    the only authoritative one. A marker quoted anywhere else -- in a fence, a
    `>` blockquote, inline backticks or bare prose -- is evidence, not state."""
    text = body or ""
    last = None
    for last in _ANY_MARKER_RE.finditer(text):
        pass
    if last is None or text[last.end():].strip():
        return None
    return last.group(0)


def newest_handoff_mutates_live(bodies: Iterable[str]) -> bool | None:
    """`True`/`False` from the newest `kind=handoff` footer's `mutates-live`
    field. `None` when there is no handoff, or the newest one carries no field
    (every handoff posted before harmonic-forge#851). Callers treat `None` as
    `True`: a legacy handoff is refused, never assumed safe."""
    found: bool | None = None
    for body in bodies:
        marker = trailing_footer(body)
        if not marker:
            continue
        kind = _KIND_RE.search(marker)
        if not kind or kind.group(1) != "handoff":
            continue
        field = _MUTATES_LIVE_RE.search(marker)
        found = None if field is None else field.group(1) == "true"
    return found
