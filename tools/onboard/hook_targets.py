"""Where a hooks block's platform commands point, and whether they exist
(harmonic-forge#547, moved here by harmonic-forge#1307).

Its own module for the same reason `required_hooks.py` is: the lane launcher's
`tools/lane/lane_hookset.py` validates the Forge lane hook set with
`unresolvable_hook_targets` at every launch, and must not import
`forge_onboard.py` and its manifest stack to do it. `forge_onboard.py`
re-imports the three names, so its callers and tests are unchanged.
"""
from __future__ import annotations

import os
import re
from pathlib import Path


def _hook_commands(hooks: dict) -> list[str]:
    """Every `command` string in a settings.json hooks block, any event."""
    found: list[str] = []
    for matchers in hooks.values():
        if not isinstance(matchers, list):
            continue
        for matcher in matchers:
            if not isinstance(matcher, dict):
                continue
            for entry in matcher.get("hooks") or []:
                if isinstance(entry, dict) and isinstance(entry.get("command"), str):
                    found.append(entry["command"])
    return found


_PLATFORM_HOOK_PATH = re.compile(r'["\']?(\$\{HOME\}|~|/[^"\'\s]*?)/(harmonic-forge/[^"\'\s]+\.py)')


def unresolvable_hook_targets(hooks: dict) -> list[str]:
    """Platform hook scripts a settings.json names that do not exist on disk.

    harmonic-forge#547's preclose-inspection: this check read only the event
    KEY NAMES and never a `command` string, so renaming or moving a platform
    hook script made every session in every consuming repo run a command that
    exits 2 and produces no output. For `SessionStart` that does not block the
    session — it just silently contributes nothing, which is indistinguishable
    from the pre-#547 state the issue was filed about, and this tool reported
    green throughout.

    That is the reconciliation half the skills manifest has and hooks lacked.
    The answer is NOT a `hooks` key on the skills manifest (that manifest
    exists because gitignored symlinks cannot travel with a clone, which is not
    hooks' problem) — it is asserting here that what settings.json names
    actually resolves.
    """
    missing: list[str] = []
    for command in _hook_commands(hooks):
        match = _PLATFORM_HOOK_PATH.search(command)
        if not match:
            continue
        candidate = Path(os.path.expandvars(os.path.expanduser(
            f"{match.group(1)}/{match.group(2)}")))
        if not candidate.is_file():
            missing.append(str(candidate))
    return sorted(set(missing))
