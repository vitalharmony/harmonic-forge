#!/usr/bin/env python3
"""Name the batch a denial just interrupted (harmonic-forge#509 AC3).

`#502` gave `decide()` four distinguishable refusal reasons. It did nothing for
the **deny surface** — thirteen hooks that can halt a run, none of which knows a
batch exists. Three of the operator's interruptions during the `#493` run were
denials of that kind, and from the operator's side a denial and a permission
prompt are the same event: the run stopped and it is not obvious why.

## This does NOT relax any guard

**Nothing here returns `allow`, and nothing here reads a decision.** The single
public function appends a sentence to a message a hook has *already decided* to
emit. `#336` established why the alternative is unavailable:

> a static `permissions.ask` rule was found to always beat a hook's `allow`
> regardless of hook order or content ... Two hooks independently deciding the
> same command class is undefined behavior under "strongest decision wins"
> composition; one hook now owns each class end to end.

So a hook that consulted `batch_auth` to soften its own verdict would be
reintroducing exactly the composition failure `#336` removed. AC4 asserts by
test that none does; this module is built so that it *cannot*, by having no
return path that a caller could branch on.

## Fails silent, always

A hook is mid-denial when it calls this. If annotating raises, the operator
loses the denial itself — which is far worse than losing the annotation. Every
failure path returns the original message unchanged.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

#: Cap on how many keys are named before the list is elided. A batch of five is
#: typical; a message listing forty would bury the denial it is annotating.
#: Raised from 6 to 25 (harmonic-forge#567 AC7) -- 6 hid the key the operator
#: actually cared about as soon as a batch ran ~13 keys concurrently, which is
#: "normal operation" for this house, not an edge case. 25 comfortably covers
#: any batch this project has run and still elides a truly unusual spike;
#: `batch_auth.py`'s `PRUNE_GRACE_HOURS` bounds the state file's total size
#: but not how many entries are LIVE at once, so this cap is still doing real
#: work rather than being made moot by that change.
_MAX_KEYS = 25


def live_batch_keys(now: datetime | None = None) -> list[str]:
    """Issue keys with an unexpired authorization, sorted. `[]` on any failure.

    Read-only against `batch_auth`'s state. Deliberately does not distinguish
    consumed from unconsumed targets: the question this answers is "was a batch
    running when we stopped it", and a batch mid-close still has consumed
    merges. `block_batch_stop.py` asks a different question and keys on the
    close target for its own reasons.
    """
    try:
        from batch_auth import STATE_PATH, _load  # noqa: PLC0415

        state = _load(STATE_PATH)
    except Exception:
        return []
    if not isinstance(state, dict):
        return []
    now = now or datetime.now(timezone.utc)
    keys: list[str] = []
    for key, entry in state.items():
        if not isinstance(entry, dict):
            continue
        try:
            if now < datetime.fromisoformat(entry["expires_at"]):
                keys.append(str(key))
        except (KeyError, TypeError, ValueError):
            continue
    return sorted(keys)


def _top_up_hint(target_key: str | None, keys: list[str]) -> str:
    """Name `top-up` when the acting key is simply not in the batch.

    harmonic-forge#600 AC2. `top-up` has existed since #567 -- it EXTENDS a
    live grant rather than replacing it, which is exactly what adding
    newly-discovered work to a running batch needs -- and no denial message
    mentioned it. The operator was told to satisfy the guard and to preflight
    NEXT time, with nothing about the one command that fixes this time.

    Only on the not-in-the-batch branch. When the key IS authorized the batch
    is not the problem, and suggesting `top-up` would point at the wrong thing.
    """
    if not target_key or target_key.upper() in {k.upper() for k in keys}:
        return ""
    return (
        f"\n\nTo bring {target_key.upper()} into the RUNNING batch rather than "
        f"replacing it:\n"
        f"  python3 tools/hooks/batch_auth.py top-up {target_key.upper()}\n"
        f"`top-up` extends a live grant and leaves its targets untouched; "
        f"`authorize` REPLACES the entry, resetting consumption and wiping "
        f"recorded PR links (harmonic-forge#567). Use top-up mid-batch."
    )


def annotate(message: str, *, target_key: str | None = None,
             now: datetime | None = None) -> str:
    """`message` plus a line naming the interrupted batch, or `message` as-is.

    `target_key` is the issue key the denied command was acting on, when the
    hook knows it — `H1631`, `F502`. Naming it matters more than listing the
    batch: "this stopped on F502" is actionable, "a batch is live" is not.

    Never raises. Never returns anything a caller can branch on to soften a
    verdict — the return type is the message, and the only difference is
    whether a sentence was appended.
    """
    try:
        keys = live_batch_keys(now)
        if not keys:
            return message

        if target_key and target_key.upper() in {k.upper() for k in keys}:
            head = (f"[BATCH] This denial interrupted an authorized batch, on "
                    f"{target_key.upper()}.")
        elif target_key:
            # harmonic-forge#600 AC2. "NOT one of the authorized keys" reads
            # as operator error. The commonest cause is the opposite: the work
            # was DISCOVERED after the batch was authorized -- the belt exists
            # to surface work nobody planned, so belt-found work is by
            # construction never in the key set. Same denial, different remedy,
            # and the old message named neither.
            head = (f"[BATCH] This denial interrupted a session with a live "
                    f"batch, while acting on {target_key.upper()}, which the "
                    f"batch does not cover. If this work was discovered after "
                    f"the batch was authorized -- which is what the belt is "
                    f"for -- it was never going to be in the key set.")
        else:
            head = "[BATCH] This denial interrupted an authorized batch."

        # Elide from `shown` and derive the suffix from the SAME slice.
        # Keying the suffix off `keys` while listing `shown` meant deleting the
        # slice still produced ", +N more" — all forty keys printed, followed
        # by a count claiming they were not.
        shown = keys[:_MAX_KEYS]
        if target_key and target_key.upper() not in {k.upper() for k in shown}:
            # The acting key must always appear. With more live grants than the
            # cap, sorting alone can push it out — `H1636` behind six `F` keys.
            shown = [target_key.upper()] + shown[:_MAX_KEYS - 1]
        # max(0, ...): `shown` is extended with the acting key when sorting
        # elided it, which can push it past the cap and make this negative --
        # printing a literal "+-1 more" (observed). A count of hidden keys is
        # never negative; the honest floor is zero.
        hidden = max(0, len(keys) - len(shown))
        listed = ", ".join(shown)
        if hidden:
            listed += f", +{hidden} more"

        return (
            f"{message}\n\n{head} Live keys: {listed}.\n"
            "The batch is NOT authorized past this guard — a BATCH grant covers "
            "`gh pr merge` and `gh issue close`, never a protocol guard. "
            "Satisfy the condition above and continue; do not look for a way "
            "around it.\n"
            "Many of these are satisfiable before a batch starts — run "
            "`mise run batch-preflight --key <KEY> ...` at the top of the next "
            "one (harmonic-forge#509)." + _top_up_hint(target_key, keys)
        )
    except Exception:
        # A hook is mid-denial. Losing the annotation is survivable; losing the
        # denial is not.
        return message
