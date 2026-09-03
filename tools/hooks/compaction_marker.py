#!/usr/bin/env python3
"""`SessionStart` handler for context compaction (harmonic-forge#446).

WHAT THIS IS FOR
-------------------
A lane session that gets auto-compacted loses ~96% of its context (measured:
936,523 -> 33,974 tokens) and keeps working as though nothing happened. This
fires once at the compaction rebuild, writes a marker, and injects a short
situational note into the reconstructed context.

WHY THE INJECTION CARRIES NO RULES
-------------------------------------
The harness re-reads the **auto-loaded** surface after a compaction --
`CLAUDE.md` and `.claude/rules/*.md` -- proven live by putting a token in each,
changing it after the compaction, and seeing the NEW value come back. So
shipping rule text here would duplicate content that is already restored,
spending the context budget this issue exists to protect.

But the auto-loaded surface is NOT the lane's directive corpus, and conflating
the two was this issue's most expensive error. `harmonic-forge/CLAUDE.md` is 19
lines -- a pointer -- and the repo has no `.claude/rules/` at all. The real
corpus is Read-tool-loaded and never comes back on its own:

    3-lane-protocol.md          1573
    rules/universal-agent.md     598
    rules/testing-gate.md        250
    rules/universal-lane1.md     229
    rules/universal-claude.md     90
    docs/agent-foundation.md      75

Telling a compacted session "your directives are back" would suppress the exact
recovery action it needs. So the payload states the split -- auto-loaded surface
restored, protocol corpus NOT -- and names the paths to re-read. A path list,
not rule content.

WHAT DOES NOT FIRE HERE
--------------------------
The subagent-compaction shape (`agentType == "subagent"` with
`delegatedObservation == true`) does not reach this hook. That is the harness's
behavior, not an omission -- named so its absence is not later read as a defect.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

#: Same convention as `item_list_cache.py:30` / `model_tier_gate.py:94`.
MARKER_DIR = Path(tempfile.gettempdir()) / "harmonic-forge-compaction-gate"

#: Markers older than this are pruned opportunistically. Generous on purpose:
#: nothing reads them yet (harmonic-forge#451 is blocked), and deleting one
#: early is strictly worse than keeping it -- see `prune_markers`.
TTL_SECONDS = 7 * 24 * 60 * 60

#: The corpus a compacted session must re-read, by LANE. Paths, never content.
_ALWAYS = ("3-lane-protocol.md", "rules/universal-agent.md")
_BY_LANE = {"1": ("rules/universal-lane1.md",), "3": ("rules/testing-gate.md",)}


def forge_root() -> str:
    """Where the protocol corpus lives, for the paths named in the injection."""
    return os.environ.get("HARMONIC_FORGE_ROOT") or str(Path.home() / "harmonic-forge")


def lane_of(env: dict[str, str]) -> str:
    """`LANE`, or `"unknown"`.

    Never a bare `LANE=` and never a raise: this hook runs at the moment a
    session is least able to cope with a crash, and a session with no `LANE` is
    a legitimate state (an operator shell, a subagent), not an error.
    """
    lane = (env.get("LANE") or "").strip()
    return lane if lane else "unknown"


def corpus_for(lane: str) -> list[str]:
    root = forge_root()
    return [f"{root}/{p}" for p in _ALWAYS + _BY_LANE.get(lane, ())]


def build_context(compacted_at: str, lane: str, cwd: str) -> str:
    """The injected payload. Situational state and paths — no rule text."""
    paths = "\n".join(f"  - {p}" for p in corpus_for(lane))
    return (
        f"This session was compacted at {compacted_at}. You are LANE={lane} in {cwd}.\n"
        f"Your CLAUDE.md and .claude/rules/ directives were re-loaded automatically. "
        f"Your protocol corpus was NOT — re-read:\n{paths}\n"
        f"Your task state is also gone — re-read the issue thread before acting."
    )


def prune_markers(now: float) -> None:
    """Drop markers past the TTL, keyed on their own `compacted_at`.

    Keyed on the recorded timestamp and **never on file mtime**: a long-running
    session's marker keeps its original `compacted_at` while its mtime may be
    refreshed or stale for unrelated reasons. Pruning a live session's marker
    would make harmonic-forge#451's gate silently conclude "no compaction
    happened" — a false negative in a guard, which is the failure shape
    harmonic-forge#440 exists as a warning about.

    Per-entry errors are swallowed. Concurrent lanes write and prune this
    directory simultaneously, so a `FileNotFoundError` mid-iteration is normal
    operation. A prune problem must never fail the hook — the injection is the
    product; the prune is housekeeping.
    """
    try:
        entries = list(MARKER_DIR.iterdir())
    except (FileNotFoundError, PermissionError, OSError):
        return
    for entry in entries:
        try:
            data = json.loads(entry.read_text(encoding="utf-8"))
            stamp = datetime.fromisoformat(data["compacted_at"]).timestamp()
            if now - stamp > TTL_SECONDS:
                entry.unlink()
        except (FileNotFoundError, PermissionError, OSError,
                ValueError, KeyError, TypeError):
            continue


def write_marker(session_id: str, payload: dict) -> None:
    """Atomic write — temp file in the same directory, then `os.replace`.

    Concurrent lanes share this directory, and #451 will read these while they
    are being written. A partially-written marker parses as corrupt and reads as
    "no compaction", so the write must be all-or-nothing.
    """
    MARKER_DIR.mkdir(parents=True, exist_ok=True)
    target = MARKER_DIR / f"{session_id}.json"
    handle, tmp_name = tempfile.mkstemp(dir=str(MARKER_DIR), suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(payload, stream)
        os.replace(tmp_name, target)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def handle(payload: dict, env: dict[str, str], now: float | None = None) -> dict:
    """Returns the hook's stdout object. `{}` means "do nothing"."""
    if payload.get("source") != "compact":
        return {}

    now = time.time() if now is None else now
    compacted_at = datetime.fromtimestamp(now, tz=timezone.utc).isoformat()
    session_id = payload.get("session_id") or ""
    lane = lane_of(env)
    cwd = payload.get("cwd") or "an unknown directory"

    if session_id:
        try:
            write_marker(session_id, {
                "compacted_at": compacted_at, "source": "compact",
                "lane": lane, "cwd": cwd,
            })
            prune_markers(now)
        except (OSError, ValueError):
            # The injection still ships. A marker we could not persist costs
            # #451 a signal; a hook that raised here would cost the session its
            # recovery note, which is the thing that actually helps right now.
            pass

    return {"hookSpecificOutput": {
        "hookEventName": "SessionStart",
        "additionalContext": build_context(compacted_at, lane, cwd),
    }}


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        # Visible, not silent: a malformed payload means the gate did not run,
        # and a quiet `{}` here is indistinguishable from "no compaction".
        print(json.dumps({"systemMessage":
                          "compaction_marker: malformed hook payload; no marker written"}))
        return
    if not isinstance(payload, dict):
        print(json.dumps({"systemMessage":
                          "compaction_marker: hook payload was not an object"}))
        return
    print(json.dumps(handle(payload, dict(os.environ))))


if __name__ == "__main__":
    main()
