"""Content-bound reading of a comment's own `l1-post` footer (harmonic-forge#851).

Shared by `l1_post.py --auto-ae` (the poster's refusal) and
`check_lane3_ready` (the consumer's), so both halves of the auto-AE carve-out
read one footer one way. Pure: callers fetch the thread bodies themselves.

**Why content-bound, not positional** (sticky-wicket REFORGE, preclose pass 2).
A footer's position in a mutable comment body is a property of the latest
edit, not of the comment: a quote, a GitHub quote-reply or one appended
character moves it. Both position rules this change tried -- first marker,
then last marker with nothing after it -- failed open. The integrity data
already exists: every tool-posted footer except `discussion` carries
`body-sha256`, the digest of the body it was appended to (`l1_post.post_kind`,
`post_lane_discussion`, both hashing the rstripped body). So a marker is this
comment's own attestation **only if that digest matches the text before it**.
A quoted marker fails (its digest is another body's), and so does an edit
inside the attested text.

**Three states, never one overloaded None.** `ATTESTED` (a marker whose digest
matches its prefix), `UNREADABLE` (a marker carrying a digest that matches
nothing: quoted, edited or forged), and `ABSENT` (no marker, or only markers
with no digest: legacy comments and `discussion`). Every auto-AE path refuses
on `UNREADABLE`, and `ABSENT` never authorizes an auto-AE.

Measured live before landing (issue threads hrse#2163, #1890, #2161, #1895,
#1888 and harmonic-forge#848): every `ae`, `sweep`, `spec`, `handoff`,
`gate-result`, `ready-for-l3` and `rework` footer attested under this rule;
only `discussion`, `completion` and `plan` carry no digest.
"""
from __future__ import annotations

import enum
import hashlib
import re
from collections.abc import Iterable
from dataclasses import dataclass

#: One marker, never spanning a `-->` boundary: a footer carries no `>` before
#: its own close, so `[^>]*?` cannot run from one marker into the next.
_ANY_MARKER_RE = re.compile(r"<!--\s*l1-post\s+v\d+;[^>]*?-->")
_KIND_RE = re.compile(r"kind=([\w-]+)")
_DIGEST_RE = re.compile(r"body-sha256=([0-9a-f]{64})")
_MUTATES_LIVE_RE = re.compile(r"mutates-live=(\w+)")


class FooterState(enum.Enum):
    ATTESTED = "attested"
    UNREADABLE = "unreadable"
    ABSENT = "absent"


@dataclass(frozen=True)
class Footer:
    """What a body's own footer establishes.

    `marker` is the attested marker text (ATTESTED only). `prefix` is the
    attested body -- the exact text its digest covers -- which is the only
    text a consumer may read content (a write tier, an Authorized line) from.
    `claimed_kinds` is every `kind=` any marker in the body names, attested or
    not, so a caller can refuse an UNREADABLE body that claims to be the kind
    it is looking for."""
    state: FooterState
    marker: str | None = None
    prefix: str | None = None
    claimed_kinds: frozenset[str] = frozenset()

    @property
    def kind(self) -> str | None:
        if self.marker is None:
            return None
        found = _KIND_RE.search(self.marker)
        return found.group(1) if found else None


def attested_footer(body: str | None) -> Footer:
    """The body's own footer, decided by its digest, never by its position."""
    text = body or ""
    markers = list(_ANY_MARKER_RE.finditer(text))
    claimed = frozenset(k.group(1) for m in markers if (k := _KIND_RE.search(m.group(0))))
    if not markers:
        return Footer(FooterState.ABSENT)
    for match in reversed(markers):
        digest = _DIGEST_RE.search(match.group(0))
        if digest is None:
            continue
        prefix = text[:match.start()].rstrip("\n")
        if hashlib.sha256(prefix.encode()).hexdigest() == digest.group(1):
            return Footer(FooterState.ATTESTED, match.group(0), prefix, claimed)
    last = markers[-1]
    if _DIGEST_RE.search(last.group(0)):
        # The comment's own (last) marker carries a digest that matches
        # nothing: edited, forged, or a quote-reply ending a hand-posted body.
        return Footer(FooterState.UNREADABLE, claimed_kinds=claimed)
    # The comment's own marker carries no digest: a `discussion` or a legacy
    # comment. Earlier digested markers in it are quotes, not state. It still
    # names its kind for legacy callers, but ABSENT never authorizes an auto-AE.
    return Footer(FooterState.ABSENT, last.group(0), text[:last.start()].rstrip("\n"), claimed)


#: `newest_handoff_mutates_live` result for a newest handoff whose footer
#: cannot be trusted. Distinct from None (no field) so a caller can say why it
#: refused; both refuse.
UNREADABLE_HANDOFF = "unreadable"


def newest_handoff_mutates_live(bodies: Iterable[str]) -> bool | str | None:
    """`True`/`False` from the newest handoff's attested `mutates-live` field.

    `None` when there is no handoff, or the newest one carries no field (every
    handoff posted before harmonic-forge#851). `UNREADABLE_HANDOFF` when the
    newest body claiming to be a handoff has a footer that does not attest:
    the scan does not skip past it to an older, safer-looking handoff.
    Callers refuse on anything but `False`."""
    found: bool | str | None = None
    for body in bodies:
        footer = attested_footer(body)
        if footer.state is FooterState.ATTESTED:
            if footer.kind != "handoff":
                continue
        elif footer.state is FooterState.UNREADABLE:
            # Its own marker does not attest, so what it is cannot be
            # trusted; if any marker in it claims handoff, refuse rather than
            # fall back to an older handoff (preclose pass 2 survivor 2).
            if "handoff" in footer.claimed_kinds:
                found = UNREADABLE_HANDOFF
            continue
        else:  # ABSENT: a legacy handoff with no digest still counts, as no field.
            if footer.kind != "handoff":
                continue
        field = _MUTATES_LIVE_RE.search(footer.marker or "")
        found = None if field is None else field.group(1) == "true"
    return found
