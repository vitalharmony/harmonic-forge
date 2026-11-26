"""Where a hooks block's platform commands point, and whether they exist
(harmonic-forge#547, moved here by harmonic-forge#1307).

Its own module for the same reason `required_hooks.py` is: the lane launcher's
`tools/lane/lane_hookset.py` validates the Forge lane hook set with
`unresolvable_hook_targets` at every launch, and must not import
`forge_onboard.py` and its manifest stack to do it. `forge_onboard.py`
re-imports two names, so its callers and tests are unchanged.

The path is never decomposed (harmonic-forge#1526 sticky-wicket A3): each
shell word ending in `.py` that contains `/tools/` is expanded whole and
checked as a file. No root/component split: the checkout may have any name
and a `${HOME}` anchor is never dropped. Words split as the shell splits them
(`_words`): unquoted blanks and the operators `;`, `&`, `|` end a word, and
quoted and unquoted parts joined with no blank are one word. Only `$HOME`,
`${HOME}` and a leading `~` are expanded; a word holding any other `$` is not
decided here (post-verdict survivor 1).
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


_BREAK = " \t\n;&|"  # an unquoted one of these ends a shell word
_ASSIGN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
_HOME_VAR = re.compile(r"\$(?:HOME(?![A-Za-z0-9_])|\{HOME\})")


def _words(command: str) -> list[str]:
    """Each shell word of `command` with `$HOME`, `${HOME}` and a leading `~`
    expanded, quotes removed. A word with any other `$` (or an unterminated
    quote) is dropped: its expansion is not known here. An assignment word
    (`f=...`) yields its value."""
    home = os.environ.get("HOME", "")
    words: list[str] = []
    i, n = 0, len(command)
    while i < n:
        if command[i] in _BREAK:
            i += 1
            continue
        if (assign := _ASSIGN.match(command, i)):
            i = assign.end()
        parts: list[str] = []
        known = True
        if command.startswith("~", i) and (i + 1 == n or command[i + 1] in "/" + _BREAK):
            parts.append(os.path.expanduser("~"))
            i += 1
        while i < n and command[i] not in _BREAK:
            c = command[i]
            if c == "'":
                end = command.find("'", i + 1)
                if end < 0:
                    known, i = False, n
                    break
                parts.append(command[i + 1:end])
                i = end + 1
            elif c == '"':
                i += 1
                while i < n and command[i] != '"':
                    if command[i] == "\\" and i + 1 < n and command[i + 1] in '$`"\\':
                        parts.append(command[i + 1])
                        i += 2
                    elif command[i] == "$":
                        if (var := _HOME_VAR.match(command, i)):
                            parts.append(home)
                            i = var.end()
                        else:
                            known, i = False, i + 1
                    else:
                        parts.append(command[i])
                        i += 1
                if i >= n:
                    known = False
                i += 1
            elif c == "\\":
                parts.append(command[i + 1:i + 2])
                i += 2
            elif c == "$":
                if (var := _HOME_VAR.match(command, i)):
                    parts.append(home)
                    i = var.end()
                else:
                    known, i = False, i + 1
            else:
                parts.append(c)
                i += 1
        if known:
            words.append("".join(parts))
    return words


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
        for word in _words(command):
            # A word that does not expand to an absolute path (`./x`,
            # `${CLAUDE_PROJECT_DIR}/...`) is the project's own, not a platform path.
            if "/tools/" in word and word.endswith(".py"):
                candidate = Path(word)
                if candidate.is_absolute() and not candidate.is_file():
                    missing.append(word)
    return sorted(set(missing))
