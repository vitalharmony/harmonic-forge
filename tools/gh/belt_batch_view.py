#!/usr/bin/env python3
"""A read-only view of BATCH state, for the belt (harmonic-forge#600 AC1/AC4).

AC4 is the constraint that shapes this file: *"Neither mechanism gains a
dependency on the other's internals. A shared state file or a documented
interface, not `watch_lane_posts.py` importing `batch_auth`."*

So this reads the state file and imports nothing from `tools/hooks/`. The
coupling that remains is one path and two key names -- `expires_at`, and the
top-level mapping being keyed by batch key -- and that is the documented
interface, asserted by this module's own tests so a change to the file format
fails here rather than silently making every belt cycle report "no batch".

**It never writes, never prunes, never locks.** `batch_auth.py` owns that file;
a second writer would race its lock for no benefit, since nothing here needs
the file to change. A stale entry is handled by ignoring it, not by cleaning
it up.

**It fails open, deliberately.** Every failure mode -- file absent, unreadable,
malformed, an entry with an unparseable or timezone-naive `expires_at` --
reports "no live batch". The consequence of a false negative is one missing
advisory line on stderr; the consequence of a false positive is a belt that
tells every session a batch is in flight when none is, which is the kind of
wrong-but-confident output that gets a mechanism ignored. `batch_auth` itself
is the gate; this is a narrator.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

#: Owned and written by `tools/hooks/batch_auth.py` (`STATE_PATH` there). Named
#: here rather than imported: importing it is the dependency AC4 forbids.
BATCH_STATE_PATH = Path.home() / ".claude" / "state" / "batch-authorized.json"


def live_batch_keys(
    now: datetime | None = None,
    state_path: Path | None = None,
) -> tuple[str, ...]:
    """Every batch key whose authorization has not expired, sorted.

    Empty when there is no live batch, and empty on every failure -- see the
    module docstring for why that direction.
    """
    path = state_path if state_path is not None else BATCH_STATE_PATH
    moment = now if now is not None else datetime.now(timezone.utc)
    try:
        state = json.loads(path.read_text())
    except (OSError, ValueError):
        return ()
    if not isinstance(state, dict):
        return ()

    live = []
    for key, entry in state.items():
        if not isinstance(entry, dict):
            continue
        try:
            if moment < datetime.fromisoformat(entry["expires_at"]):
                live.append(key)
        except (KeyError, TypeError, ValueError):
            # A naive `expires_at` parses and then raises TypeError on the
            # comparison -- the same shape harmonic-forge#567 found ten of in
            # the live file. Not live, rather than crashing a poll loop.
            continue
    return tuple(sorted(live))


def deferral_notice(keys: tuple[str, ...], queued: int) -> str | None:
    """The one advisory line the belt emits when it finds work during a live
    batch, or None when there is nothing to say.

    Stderr only, and that is AC5: the belt's stdout rows are a parsed contract
    and do not change shape to carry batch state. This line sits beside them.

    It states all three things AC1 asks for -- that a batch is in flight, that
    the item is still queued rather than dropped, and what to do about it --
    because a notice that says only "a batch is live" reads as a reason the
    item will not be offered, which is the opposite of what happens.
    """
    if not keys or queued <= 0:
        return None
    item_word = "item" if queued == 1 else "items"
    return (
        f"[watch_lane_posts] BATCH {', '.join(keys)} is live. {queued} queued "
        f"{item_word} above {'is' if queued == 1 else 'are'} NOT covered by it "
        f"-- the belt found {'it' if queued == 1 else 'them'} after the batch "
        f"was authorized, which is what the belt is for. "
        f"{'It stays' if queued == 1 else 'They stay'} queued and will be "
        f"offered again next cycle; nothing is dropped. To bring one under the "
        f"batch, ask the OPERATOR to run, in their own terminal: "
        f"python3 ~/harmonic-forge/tools/hooks/batch_auth.py top-up <KEY>"
    )
