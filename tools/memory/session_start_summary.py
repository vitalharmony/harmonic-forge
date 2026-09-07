#!/usr/bin/env python3
"""SessionStart delivery for the memory lint (harmonic-forge#494).

Modelled directly on `tools/hooks/compaction_marker.py`'s emit-on-failure
posture — a deliberate copy, not a new design.

WHY THIS ALWAYS EXITS 0
-----------------------
Not because a non-zero exit would block the session: it would not. The hooks
reference's own table reads `SessionStart | No | Output and exit code are
ignored, except terminalSequence`.

The hazard is the inverse. On a non-zero exit the harness **discards the
output entirely**, producing a hook that is wired, runs, and injects nothing —
indistinguishable from a clean store. So a lint that exits 1 would silently
turn this into a no-op that still looks healthy.

WHY THIS ALWAYS EMITS
---------------------
Same reason, one level up: silence is never a valid output of this script. A
failure to read the store, a crash inside the lint, a missing store path — each
still emits a payload, and adds `systemMessage` so the operator sees it rather
than inferring it from an absence. `compaction_marker.py` states the identical
rule in-source for a malformed payload: "Visible, not silent."
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

HOOK_EVENT = "SessionStart"


def _summary() -> str:
    """One line, counts only. Never a finding list — this is a nudge, not a report."""
    import memory_lint as lint

    store = lint.resolve_store()
    if not store.is_dir():
        raise FileNotFoundError(f"memory store not found: {store}")
    lines, size = lint.index_size(store)
    broken, _issue_refs = lint.check_broken_slugs(store)
    return (
        f"memory: {len(lint.memory_files(store))} files, "
        f"index {lines}/{lint.MAX_INDEX_LINES} lines, "
        f"{len(lint.check_orphans(store))} orphan(s), "
        f"{len(broken)} broken link(s), "
        f"{len(lint.check_staleness(store))} stale (report-only). "
        f"{_context_line()}"
        f"Run `python3 {Path(__file__).parent / 'memory_lint.py'}` for detail."
    )


def _context_line() -> str:
    """The always-loaded context total (harmonic-forge#497), or "" on failure.

    **Degrades to empty rather than raising.** The memory half of this summary
    is the part the hook exists for; a budget measurement that cannot run must
    not cost the session its store report. `build_payload`'s own except would
    otherwise convert a `context_budget` bug into "the memory store was NOT
    checked this session", which would be false and alarming.
    """
    try:
        import context_budget
        return context_budget.summary_line(Path.cwd()) + ". "
    except Exception:  # noqa: BLE001 - see above
        return ""


def build_payload() -> dict[str, object]:
    try:
        context = _summary()
    except Exception as err:  # noqa: BLE001 - see module docstring
        # Visible, not silent. An empty payload here is indistinguishable from
        # a clean store, which is the one outcome this script must never
        # produce.
        return {
            "systemMessage": f"memory_lint: {type(err).__name__}: {err}",
            "hookSpecificOutput": {
                "hookEventName": HOOK_EVENT,
                "additionalContext": (
                    f"memory: lint did not run ({type(err).__name__}: {err}). "
                    "The memory store was NOT checked this session."
                ),
            },
        }
    return {"hookSpecificOutput": {"hookEventName": HOOK_EVENT, "additionalContext": context}}


def main() -> int:
    try:
        sys.stdin.read()
    except Exception:  # noqa: BLE001 - stdin is advisory here; never fatal
        pass
    print(json.dumps(build_payload()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
