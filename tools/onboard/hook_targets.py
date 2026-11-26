"""Where a hooks block's platform commands point, and whether they exist
(harmonic-forge#547, moved here by harmonic-forge#1307).

Its own module for the same reason `required_hooks.py` is: the lane launcher's
`tools/lane/lane_hookset.py` validates the Forge lane hook set with
`unresolvable_hook_targets` at every launch, and must not import
`forge_onboard.py` and its manifest stack to do it. `forge_onboard.py`
re-imports two names, so its callers and tests are unchanged.

The path is never decomposed (harmonic-forge#1526 sticky-wicket A3): each
quoted or bare word ending in `.py` that contains `/tools/` is expanded whole
(`~` when bare, then `$VAR`/`${VAR}` unless single-quoted) and checked as a
file. No root/component split: the checkout may have any name and a `${HOME}`
anchor is never dropped.
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


# A double-quoted, single-quoted or bare word that names a `/tools/...py` path.
_TOOLS_WORD = re.compile(r'"([^"]*/tools/[^"]*\.py)"|\'([^\']*/tools/[^\']*\.py)\''
                         r'|(?<![^\s;=])([^\s"\';]*/tools/[^\s"\';]*\.py)(?![^\s;])')


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
        for match in _TOOLS_WORD.finditer(command):
            double, single, bare = match.groups()
            # As the shell does: `~` expands only bare, nothing in single quotes.
            candidate = Path(single if single is not None else os.path.expandvars(
                os.path.expanduser(bare) if bare is not None else double))
            # A word that does not expand to an absolute path (`./x`, an unset
            # `${CLAUDE_PROJECT_DIR}`) is the project's own, not a platform path.
            if candidate.is_absolute() and not candidate.is_file():
                missing.append(str(candidate))
    return sorted(set(missing))
