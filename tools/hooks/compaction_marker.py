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
corpus is Read-tool-loaded and never comes back on its own.

That corpus is enumerated ONCE, in `CORPUS` below, and the injection is derived
from it. It used to be listed here as well, with line counts, and the two lists
drifted by two entries -- named here as lost and then routed to nobody
(harmonic-forge#464). A second hand-maintained copy of one fact is what produced
that, so there is now one. No filename belongs in this docstring: naming one
here is how the duplicate list starts again.

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
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

#: Same convention as `item_list_cache.py:30` / `model_tier_gate.py:94`.
MARKER_DIR = Path(tempfile.gettempdir()) / "harmonic-forge-compaction-gate"

#: Markers older than this are pruned opportunistically. Generous on purpose:
#: nothing reads them yet (harmonic-forge#451 is blocked), and deleting one
#: early is strictly worse than keeping it -- see `prune_markers`.
TTL_SECONDS = 7 * 24 * 60 * 60

class Routing(str, Enum):
    """How one corpus file reaches a compacted session.

    Two axes, not one, which is the finding that shaped harmonic-forge#464:
    some of the corpus routes by **lane** and some by **repo**, and a structure
    that can only express lanes has nowhere to put the second kind.
    """

    #: Every session, whatever its lane or cwd.
    ALWAYS = "always"
    #: Only the lane named in `CorpusFile.lane`.
    BY_LANE = "by_lane"
    #: Only a session whose cwd is inside the harmonic-forge checkout.
    FORGE_REPO = "forge_repo"
    #: Corpus, deliberately not re-injected. Requires a stated `reason` --
    #: an exclusion should be readable in source, not inferred from a file's
    #: absence from a list, which is precisely how #464's two went missing.
    #: No entry uses this today.
    EXCLUDED = "excluded"


@dataclass(frozen=True)
class CorpusFile:
    """One corpus path and the rule for who is told to re-read it."""

    path: str
    routing: Routing
    #: Required for `BY_LANE`, meaningless otherwise.
    lane: str | None = None
    #: Required for `EXCLUDED`, meaningless otherwise.
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.routing is Routing.BY_LANE and not self.lane:
            raise ValueError(f"{self.path}: BY_LANE routing needs a lane")
        if self.routing is Routing.EXCLUDED and not self.reason:
            raise ValueError(f"{self.path}: EXCLUDED routing needs a stated reason")
        if self.routing is not Routing.BY_LANE and self.lane:
            raise ValueError(f"{self.path}: lane is meaningless for {self.routing.value}")


#: **The single enumeration of the protocol corpus.** Paths, never content.
#:
#: Nothing else in this module may list corpus paths. `corpus_for()` derives the
#: injection from this and the module docstring points here rather than
#: repeating it -- that duplication is what harmonic-forge#464 removed.
#:
#: Line counts are deliberately not recorded: they were a third hand-maintained
#: fact in the old docstring block and would go stale the same way.
CORPUS: tuple[CorpusFile, ...] = (
    CorpusFile("3-lane-protocol.md", Routing.ALWAYS),
    CorpusFile("rules/universal-agent.md", Routing.ALWAYS),
    # Every lane, unconditionally. Lane 2 and Lane 3 accept either Claude Code
    # or Codex (`rules/universal-lane1.md` § Current lane assignment), so this
    # is a deliberate choice rather than an oversight: `universal-lane1.md`
    # defers to this file for tool-use safeguards, the memory system and the
    # concrete advisory-invocation mechanism, and the cost of naming one
    # ignorable path to a Codex session is smaller than the cost of dropping it.
    # A `LANE_AGENT` gate was considered and rejected -- an unset variable there
    # fails by silently dropping the file, which is this bug in a harder-to-see
    # form.
    CorpusFile("rules/universal-claude.md", Routing.ALWAYS),
    CorpusFile("rules/universal-lane1.md", Routing.BY_LANE, lane="1"),
    CorpusFile("rules/testing-gate.md", Routing.BY_LANE, lane="3"),
    # Repo-scoped, not lane-scoped: vendor-neutral direction for agents working
    # *inside* the forge repo ("Editing the platform from inside the platform is
    # the one case with no other coverage"). Loaded by forge's `CLAUDE.md`, a
    # 19-line pointer, so it is Read-loaded and genuinely lost -- but naming it
    # to an HRSE2 lane session is noise.
    CorpusFile("docs/agent-foundation.md", Routing.FORGE_REPO),
)


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


def _inside_forge_repo(cwd: str, root: str) -> bool:
    """Is this session working inside the harmonic-forge checkout?

    Both sides are resolved before comparing, so a symlinked or relative cwd
    does not silently miss — which would drop `agent-foundation.md` for exactly
    the sessions that need it, this issue's own failure shape.

    A cwd that cannot be resolved (deleted directory, permission error) is
    treated as "not in the forge repo": the cost is one unnamed path, against a
    raise in a hook that runs when the session is least able to cope with one.
    """
    try:
        resolved_cwd = Path(cwd).resolve()
        resolved_root = Path(root).resolve()
    except (OSError, ValueError):
        return False
    return resolved_cwd == resolved_root or resolved_root in resolved_cwd.parents


def _routes_to(entry: "CorpusFile", lane: str, cwd: str, root: str) -> bool:
    """Whether one corpus entry is named to this session.

    An unhandled `Routing` raises rather than defaulting either way. Silently
    dropping an unrouted entry is harmonic-forge#464 itself; silently including
    it would make the `EXCLUDED` tier meaningless. `handle()` catches this and
    still ships the injection, so a bad declaration degrades visibly instead of
    costing the session its recovery note.
    """
    if entry.routing is Routing.ALWAYS:
        return True
    if entry.routing is Routing.BY_LANE:
        return entry.lane == lane
    if entry.routing is Routing.FORGE_REPO:
        return _inside_forge_repo(cwd, root)
    if entry.routing is Routing.EXCLUDED:
        return False
    raise ValueError(f"{entry.path}: unhandled routing {entry.routing!r}")


def corpus_for(lane: str, cwd: str = "") -> list[str]:
    """The paths this session is told to re-read, derived from `CORPUS`.

    `cwd` defaults to empty so an existing caller that only knows the lane keeps
    working; an empty cwd simply routes no repo-scoped file, which is the same
    answer it would have given before repo scoping existed.
    """
    root = forge_root()
    return [
        f"{root}/{entry.path}"
        for entry in CORPUS
        if _routes_to(entry, lane, cwd, root)
    ]


def build_context(compacted_at: str, lane: str, cwd: str) -> str:
    """The injected payload. Situational state and paths — no rule text."""
    paths = "\n".join(f"  - {p}" for p in corpus_for(lane, cwd))
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

    try:
        context = build_context(compacted_at, lane, cwd)
    except ValueError as err:
        # A malformed `CORPUS` entry — an unhandled routing value. Visible, not
        # silent: a bad declaration must not cost the session its recovery note,
        # and it must not look like "nothing to re-read" either, which is the
        # exact shape harmonic-forge#464 was about. `systemMessage` surfaces it
        # to the operator alongside a payload naming what can still be derived.
        return {
            "systemMessage": f"compaction_marker: corpus declaration is invalid — {err}",
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": (
                    f"This session was compacted at {compacted_at}. You are LANE={lane} "
                    f"in {cwd}.\nThe protocol-corpus list could not be built ({err}) — "
                    f"re-read your directives from {forge_root()} manually.\n"
                    f"Your task state is also gone — re-read the issue thread before acting."
                ),
            },
        }

    return {"hookSpecificOutput": {
        "hookEventName": "SessionStart",
        "additionalContext": context,
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
