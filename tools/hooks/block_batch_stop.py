#!/usr/bin/env python3
"""Refuse to end a turn while a BATCH authorization still has work (harmonic-forge#502).

## Why this is a hook and not a sentence in a rule

Operator, 2026-09-07, verbatim, after the fourth stop in one batch:

> I feel like a broken record. Isn't there something you can do to avoid a
> brain-fart-and-stop condition? It is incredibly frustrating when you are
> supposed to be running a batch.

BATCH's entire purpose is unattended execution. Every stop mid-batch costs the
operator a round-trip to say "keep going", which is the exact cost the feature
exists to remove. It happened four separate times in the harmonic-forge#493
batch alone, each time with the session having *just* finished a unit of work
and having more queued -- not blocked on anything, just... stopping.

`3-lane-protocol.md`'s own conclusion applies: **the fix is structural, not
another sentence.** A session cannot be relied on to remember an instruction at
exactly the moment its attention is elsewhere; that is the failure mode, so a
rule aimed at it is aimed wrong. This blocks the Stop event instead.

## What it does NOT block

**A genuine question.** The Ask-then-stop rule is load-bearing in the opposite
direction: when a session has a real decision for the operator, it must stop,
because work started under an unanswered question runs in the wrong
configuration and gets thrown away. So a turn whose final message actually asks
something is allowed to end.

That is detected from the transcript's last assistant message, and it is
deliberately generous -- a numbered question, a question mark on its own line, a
`DECISIONS NEEDED` section with content. **False negatives here are cheap** (one
more "keep going") and **false positives are expensive** (the operator's real
question goes unasked while the session grinds on), so anything question-shaped
passes.

**An empty batch.** No live authorization, or every target consumed, means the
batch is done and stopping is correct.

**Anything unparseable.** A missing state file, a corrupt transcript, an
unreadable anything -- this returns "allow" and says nothing. A hook that can
wedge a session into never being able to end its turn is far worse than one that
occasionally misses a stop.
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

#: How many trailing transcript records to scan for the last assistant message.
#: The last turn's text is within a few records of the end; reading the whole
#: file on every Stop would make the hook's cost grow with session length.
_TAIL_RECORDS = 40

#: Question-shaped signals in the final message. Generous on purpose -- see the
#: module docstring on why false positives here are the expensive direction.
#: Compiled separately rather than joined with `|`: inline `(?im)` flags are
#: only legal at the start of a pattern, so an alternation of flagged and
#: unflagged branches raises at import -- which, in a hook, would be silent.
#: **The keyword branch is deliberately gone.** It matched a bare "which" or
#: "confirm" anywhere in the message, and measured against 776 real turn-final
#: messages in this project's transcripts it let through 5 of 5 turns whose
#: `DECISIONS NEEDED` section literally said "None" — the exact shape this hook
#: exists to block — four of them on the word "which" appearing in an unrelated
#: table. A detector that passes 100% of its primary target is not generous,
#: it is off.
#:
#: What is left is question SYNTAX, not question vocabulary: a `?` ending a
#: line, or a numbered line containing one. Combined with `_DECISIONS_RE`
#: below, that covers how a session actually asks.
_QUESTION_RES = tuple(re.compile(pattern, flags) for pattern, flags in (
    (r"\?\s*$", re.M),
    (r"^\s*\d+\.\s+.*\?", re.M),
))

#: A `DECISIONS NEEDED` heading counts as asking ONLY when it has content. The
#: batch turn that must be blocked has exactly that heading followed by "None",
#: so treating the heading alone as a question would disable the hook on its
#: own primary case.
#: `[*_ \t]*` before the lookahead so markdown emphasis does not defeat it:
#: `**None**` matched as a live decision and allowed the stop, and 25 real
#: messages in this project use exactly that bolded form.
_DECISIONS_HEADING_RE = re.compile(
    r"^[ \t]*#{1,4}[ \t]*DECISIONS NEEDED[ \t]*$", re.I | re.M)

#: Leading markdown emphasis is stripped before this is tested, so `**None**`
#: and `*None*` read the same as `None`. Both flipped the wrong way before —
#: the lookahead saw `*`, not `N` — and 25 real messages use the bolded form.
_NO_DECISION_RE = re.compile(
    r"^(?:none|no decisions?\b|nothing\b|n/?a\b)", re.I)


def _decisions_section_has_content(text: str) -> bool:
    """True when a `DECISIONS NEEDED` heading is followed by a real decision.

    Scoped to the FIRST non-empty line under the heading. A regex spanning
    further matched content from a later section entirely — which is how
    `*None*` was read as a live decision while `**None**` was the case being
    fixed.
    """
    match = _DECISIONS_HEADING_RE.search(text)
    if not match:
        return False
    for line in text[match.end():].splitlines():
        stripped = line.strip().strip("*_ \t")
        if not stripped:
            continue
        if stripped.startswith("#"):
            return False   # next heading; the section was empty
        return not _NO_DECISION_RE.match(stripped)
    return False


def live_pending(state: dict, now: datetime) -> list[str]:
    """Keys whose authorization is unexpired and still has a linked PR that
    has not been confirmed merged.

    **Keyed on a LINKED-but-unconsumed merge target, not "any unconsumed
    target" and (as of harmonic-forge#612) not a close target either.** The
    first version asked "any unconsumed target" and was wrong in a way that
    would have wedged every session in the repo: a BATCH grant carries TWO
    merge targets so a cross-repo issue does not prompt on its second merge,
    and a single-repo issue consumes exactly one — leaving the spare
    unconsumed forever. So from the moment a batch FINISHED until its 12h TTL
    elapsed, this reported it as pending and the Stop hook refused to end any
    turn, with no action available that could clear it. Measured live: F495
    and F498 were both merged and closed and both still reported pending.

    harmonic-forge#612 removed the close target entirely (BATCH no longer
    grants `gh issue close` at all), which would have made this function
    permanently return nothing — the opposite failure, silently. The
    replacement signal keeps the same shape the close target had (single-use,
    consumed exactly when the work is done) without the spare-target false
    positive: `link_pr()` only ever populates `pr_number` on a target once a
    real PR names it, so an UNLINKED merge target (`pr_number is None`) is
    exactly the "spare capacity for a repo that may never need it" case the
    original bug was about, and is never pending. A LINKED-but-unconsumed
    target means a real PR exists and has not yet been confirmed merged --
    genuine outstanding work, the same role the close target used to play.

    An entry with no merge targets at all (should not occur post-#612, since
    merge is the only authorizable action) is never pending — nothing here
    can tell when such a grant is finished, and guessing in the blocking
    direction is what this docstring is about.
    """
    pending: list[str] = []
    for key, entry in (state or {}).items():
        if not isinstance(entry, dict):
            continue
        try:
            if now >= datetime.fromisoformat(entry["expires_at"]):
                continue
        except (KeyError, TypeError, ValueError):
            continue
        targets = entry.get("targets")
        if not isinstance(targets, list):
            # Pre-`targets` single-action entries: one dict, same fields.
            targets = [entry]
        merges = [t for t in targets if isinstance(t, dict)
                  and "merge" in str(t.get("action", "")).lower()]
        linked_unconsumed = [t for t in merges
                              if t.get("pr_number") is not None and not t.get("consumed")]
        if linked_unconsumed:
            pending.append(key)
    return sorted(pending)


def last_assistant_text(transcript: Path) -> str:
    try:
        lines = transcript.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    for line in reversed(lines[-_TAIL_RECORDS:]):
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if record.get("type") != "assistant":
            continue
        content = (record.get("message") or {}).get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = [b.get("text", "") for b in content
                     if isinstance(b, dict) and b.get("type") == "text"]
            if any(parts):
                return "\n".join(parts)
    return ""


def asks_a_question(text: str) -> bool:
    if not text:
        return False
    if _decisions_section_has_content(text):
        return True
    return any(pattern.search(text) for pattern in _QUESTION_RES)


def decide(payload: dict, state: dict, now: datetime | None = None) -> tuple[str, str]:
    """`("allow", "")` or `("block", reason)`."""
    now = now or datetime.now(timezone.utc)
    pending = live_pending(state, now)
    if not pending:
        return "allow", ""
    # FINDING: this previously BLOCKED when the transcript was unreadable,
    # contradicting the module's own "cannot wedge" contract -- with no text,
    # the question escape hatch can never fire, so the block would stand for
    # the full TTL. Unreadable means unknown, and unknown allows.
    transcript = payload.get("transcript_path") or ""
    text = last_assistant_text(Path(transcript)) if transcript else ""
    if not text:
        return "allow", ""
    if asks_a_question(text):
        return "allow", ""
    return "block", (
        f"BATCH is live for {', '.join(pending)} with unconsumed targets, and "
        "this turn ended without asking the operator anything.\n\n"
        "That is the stop BATCH exists to prevent — it costs a round-trip to "
        "say 'keep going', which is the cost the feature removes.\n\n"
        "Continue the batch: pick up the next issue, or the next step of the "
        "one in flight. If you genuinely need a decision, ASK IT — a turn that "
        "ends with a real question is allowed to end, and stopping on an "
        "unanswered question is correct.\n\n"
        "If the batch is actually finished, close out its issues (which "
        "consumes the targets) or let the authorization expire."
    )


def main(now: datetime | None = None) -> int:
    """`now` is injectable so a test can pin the clock (harmonic-forge#515).

    Production passes nothing and `decide()` resolves the real clock, exactly
    as before. Without this seam the only way to exercise `main()` was against
    the wall clock, so a fixture built from an absolute `NOW` went red the
    instant real time passed its expiry — 2026-09-07T12:00:00Z here — and
    stayed red with no code change on either side. Same defect class as
    harmonic-forge#500's `--today`, and the same remedy: make the clock a
    parameter rather than an ambient fact.
    """
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0
    # Claude Code's documented recursion guard. Honoured FIRST: this hook's
    # block condition is not clearable by anything the session can do within
    # the turn (unlike the sprint-plan enforcer, whose condition is), so
    # without this it would re-fire indefinitely.
    if not isinstance(payload, dict) or payload.get("stop_hook_active"):
        return 0
    try:
        from batch_auth import STATE_PATH, _load  # noqa: PLC0415

        state = _load(STATE_PATH)
    except Exception:
        # A hook that can wedge a session into never ending its turn is worse
        # than one that misses a stop. Any failure here is silent.
        return 0
    try:
        verdict, reason = decide(payload, state, now=now)
    except Exception:
        return 0
    if verdict == "block":
        print(json.dumps({"decision": "block", "reason": reason}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
