"""The newest handoff's `mutates-live` declaration (harmonic-forge#851).

Shared by `l1_post.py --auto-ae` (the poster's refusal) and
`check_lane3_ready.resolve_gate_authority` (the consumer's), so the two halves
of the auto-AE carve-out read one footer one way. Pure: callers fetch the
thread bodies themselves.
"""
from __future__ import annotations

import re
from collections.abc import Iterable

_FENCE_RE = re.compile(r"^(`{3,}|~{3,})[^\n]*\n.*?^\1[ \t]*$", re.DOTALL | re.MULTILINE)
_MARKER_RE = re.compile(r"<!--\s*l1-post\s+v\d+;.*?-->", re.DOTALL)
_KIND_RE = re.compile(r"kind=([\w-]+)")
_MUTATES_LIVE_RE = re.compile(r"mutates-live=(\w+)")


def newest_handoff_mutates_live(bodies: Iterable[str]) -> bool | None:
    """`True`/`False` from the newest `kind=handoff` footer's `mutates-live`
    field. `None` when there is no handoff, or the newest one carries no field
    (every handoff posted before harmonic-forge#851). Callers treat `None` as
    `True`: a legacy handoff is refused, never assumed safe."""
    found: bool | None = None
    for body in bodies:
        marker = _MARKER_RE.search(_FENCE_RE.sub("", body or ""))
        if not marker:
            continue
        kind = _KIND_RE.search(marker.group(0))
        if not kind or kind.group(1) != "handoff":
            continue
        field = _MUTATES_LIVE_RE.search(marker.group(0))
        found = None if field is None else field.group(1) == "true"
    return found
