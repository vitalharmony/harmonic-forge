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
    """The body's own footer, decided by its digest, never by its position.

    Only the body's LAST marker can be its own footer (sticky-wicket PATCH,
    reforge pass 1): a digest that matches the text before it is ATTESTED, a
    digest that matches nothing is UNREADABLE, and no digest is ABSENT. There
    is no search through earlier markers -- an embedded older footer, quoted
    with or without a digest, is never the body's own. ABSENT carries no
    marker and no prefix, so it can never nominate a kind."""
    text = body or ""
    markers = list(_ANY_MARKER_RE.finditer(text))
    claimed = frozenset(k.group(1) for m in markers if (k := _KIND_RE.search(m.group(0))))
    if not markers:
        return Footer(FooterState.ABSENT)
    last = markers[-1]
    digest = _DIGEST_RE.search(last.group(0))
    if digest is None:
        return Footer(FooterState.ABSENT, claimed_kinds=claimed)
    prefix = text[:last.start()].rstrip("\n")
    if hashlib.sha256(prefix.encode()).hexdigest() == digest.group(1):
        return Footer(FooterState.ATTESTED, last.group(0), prefix, claimed)
    return Footer(FooterState.UNREADABLE, claimed_kinds=claimed)


def footer_digest(footer: Footer) -> str | None:
    """The `body-sha256` of an ATTESTED footer."""
    if footer.state is not FooterState.ATTESTED or footer.marker is None:
        return None
    found = _DIGEST_RE.search(footer.marker)
    return found.group(1) if found else None


def thread_footers(bodies: Iterable[str]) -> list[Footer]:
    """Each body's footer, in thread order, with first attestation winning.

    A byte-identical copy of an earlier comment re-attests by construction --
    no content rule can tell a copy from its original -- so a digest already
    attested by an earlier comment makes the later one UNREADABLE, never
    ABSENT: no consumer may read a replay as permissive (reforge pass 1
    survivor 3). Two genuinely distinct comments with identical attested
    text also collide, and the newer is refused; that is fail-closed."""
    seen: set[str] = set()
    out: list[Footer] = []
    for body in bodies:
        footer = attested_footer(body)
        digest = footer_digest(footer)
        if digest is not None:
            if digest in seen:
                footer = Footer(FooterState.UNREADABLE, claimed_kinds=footer.claimed_kinds)
            else:
                seen.add(digest)
        out.append(footer)
    return out


#: `newest_handoff_mutates_live` result for a newest handoff whose footer
#: cannot be trusted. Distinct from None (no field) so a caller can say why it
#: refused; both refuse.
UNREADABLE_HANDOFF = "unreadable"


def newest_handoff_mutates_live(bodies: Iterable[str]) -> bool | str | None:
    """`True`/`False` from the newest handoff's attested `mutates-live` field.

    `None` when there is no attested handoff, or the newest one carries no
    field (every handoff posted before harmonic-forge#851). `UNREADABLE_HANDOFF`
    when the newest body claiming to be a handoff does not attest (edited,
    forged, quoted, or a replayed copy): the scan never skips past it to an
    older, safer-looking handoff. Callers refuse on anything but `False`."""
    found: bool | str | None = None
    for footer in thread_footers(bodies):
        if footer.state is FooterState.ATTESTED:
            if footer.kind != "handoff":
                continue
            field = _MUTATES_LIVE_RE.search(footer.marker or "")
            found = None if field is None else field.group(1) == "true"
        elif footer.state is FooterState.UNREADABLE and "handoff" in footer.claimed_kinds:
            found = UNREADABLE_HANDOFF
    return found
