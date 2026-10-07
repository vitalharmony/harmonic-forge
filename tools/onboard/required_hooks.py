"""Platform hooks every onboarded checkout's settings.json must register
(harmonic-forge#918). Its own module: `forge_onboard.py` is far past R-0006's cap.

Each requirement is `(script, event, tools)`. `tools` is the set of tool names
the registration's matcher must cover -- tested as the regex Claude Code treats
the matcher as, never as a substring (`NotebookEdit` contains `Edit` and covers
nothing the guard acts on) -- or `None` for an event with no matcher (a
`UserPromptSubmit` hook). The union over every block naming the script counts, so
one block per tool is as good as one alternation.
"""
from __future__ import annotations

import re

GUARDED_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit", "Bash")
REQUIRED_HOOKS: tuple[tuple[str, str, tuple[str, ...] | None], ...] = (
    ("require_ci_plan.py", "PreToolUse", GUARDED_TOOLS),
    # The deny message tells the lane the operator can type `ALLOW EDIT`; with
    # the guard registered and this not, nothing would ever record the grant.
    ("grant_ci_plan_override.py", "UserPromptSubmit", None),
)


def _covers(matcher: object, tool: str) -> bool:
    if matcher in (None, "", "*"):
        return True
    try:
        return re.fullmatch(str(matcher), tool) is not None
    except re.error:
        return False


def required_hook_gaps(hooks: dict) -> list[str]:
    """Each requirement `hooks` does not satisfy, as a readable string."""
    gaps: list[str] = []
    for script, event, tools in REQUIRED_HOOKS:
        blocks = hooks.get(event) if isinstance(hooks.get(event), list) else []
        named = [b for b in blocks if isinstance(b, dict)
                 and any(isinstance(h, dict) and script in str(h.get("command", ""))
                         for h in b.get("hooks") or [])]
        if not named:
            gaps.append(f"{script} (not registered under {event})")
            continue
        uncovered = [t for t in tools or () if not any(_covers(b.get("matcher"), t)
                                                       for b in named)]
        if uncovered:
            gaps.append(f"{script} (under {event}, matcher does not cover "
                        + ", ".join(uncovered) + ")")
    return gaps
