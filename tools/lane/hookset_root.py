"""How the lane hook set names the forge root (harmonic-forge#1526).

The stored set (`lane_hooks.json`, `lane_hooks.operator.json`) names every
script as `@FORGE_ROOT@/tools/hooks/<name>.py`, never a home-relative path.
`lane_hookset.py` renders the placeholder for one resolved root:

  * `${HOME}/<name>` when a direct child `<name>` of `$HOME` resolves to the
    root, preferring the root's own basename, else the first such name in
    sorted order. A hook runs twice when its command string differs across
    settings sources, so the operator layout (`$HOME` links the checkout)
    renders byte-identical to the project settings that already name it so.
    The checkout's own basename never decides the form: a worktree or a copy
    that `$HOME` links renders the same way.
  * the absolute resolved root otherwise, which must fullmatch
    `ABSOLUTE_RE`: the root is inserted into double-quoted shell text, so a
    space or a shell metacharacter refuses rather than gets quoted.

Containment (harmonic-forge#1526 sticky-wicket A1, A2): a stored command is
never parsed as shell text. It must fullmatch one of the two `TEMPLATES`,
whose only variable is the script name, so the shell's reading of it is known
by construction. `render_checked` expands each rendered path the way the shell
does (`${HOME}` from `HOME`, never the account record) and refuses unless its
strict realpath is a regular file under the strict realpath of the root. A
symlinked hooks directory, a `.py_disabled` sibling, a `dir/..` component over
a missing directory or an unset `HOME` refuses the build.

Its own module so `lane_hookset.py` stays within the line cap; `lane_hookset`
re-exports every name here.
"""
from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path

PLACEHOLDER = "@FORGE_ROOT@"
# The two stored command shapes (sticky-wicket A1). Nothing else is accepted.
TEMPLATES = tuple(re.compile(t) for t in (
    r'python3 "@FORGE_ROOT@/tools/hooks/(?P<n>[A-Za-z0-9_]+)\.py"',
    r'f="@FORGE_ROOT@/tools/hooks/(?P<n>[A-Za-z0-9_]+)\.py"; \[ -f "\$f" \] && python3 "\$f" \|\| true'))
HOME_NAME_RE = re.compile(r"[A-Za-z0-9._@+-]+")
ABSOLUTE_RE = re.compile(r"/[A-Za-z0-9._/@+-]+")
# A command recorded before #1526 named its script under a `$HOME` child.
RECORDED_RE = re.compile(r"\$\{HOME\}/[A-Za-z0-9._@+-]+/(tools/hooks/)")


class SetError(ValueError):
    """The set (or the policy merged into it) cannot be passed to a session."""


def shell_home() -> Path:
    """`$HOME` as the shell expands it. Unset or empty refuses: `Path.home()`
    would fall back to the account record while the shell expands `${HOME}`
    to nothing, so Python and the session would disagree on every target."""
    home = os.environ.get("HOME", "")
    if not home:
        raise SetError("HOME is unset or empty: the shell would expand ${HOME} to nothing, "
                       "so no hook target can be checked where the session runs it")
    return Path(home)


def stored_paths(command: str) -> list[str]:
    """`[tools/hooks/<name>.py]` for a stored command that fullmatches one of
    `TEMPLATES`; any other text raises."""
    for template in TEMPLATES:
        if (match := template.fullmatch(command)):
            return [f"tools/hooks/{match.group('n')}.py"]
    raise SetError(f"command does not name a forge hook script as {PLACEHOLDER}/tools/"
                   f"hooks/<name>.py in one of the two stored command templates: {command}")


def render_checked(data: dict, root: Path) -> dict:
    """`render(data, spelling(root))`, refusing unless every rendered path, as
    the shell expands it, resolves to a file under the realpath of `root`."""
    prefix = spelling(root)  # resolves `root` strictly, or raises
    real_root = os.path.realpath(root, strict=True)
    bad = []
    for blocks in (data.get("hooks") or {}).values():
        for block in blocks:
            for handler in block.get("hooks") or ():
                for rel in stored_paths(handler["command"]):
                    text = f"{prefix}/{rel}"
                    if text.startswith("${HOME}/"):
                        text = str(shell_home()) + text[len("${HOME}"):]
                    try:
                        real = os.path.realpath(text, strict=True)
                        regular = stat.S_ISREG(os.stat(real).st_mode)
                    except OSError:
                        real, regular = "(does not resolve)", False
                    if not regular or os.path.commonpath([real, real_root]) != real_root:
                        bad.append(f"{prefix}/{rel} -> {real}")
    if bad:
        raise SetError(f"hook target(s) do not resolve to a file under forge root "
                       f"{real_root}: {', '.join(sorted(set(bad)))}")
    return render(data, prefix)


def spelling(root: Path) -> str:
    """The text that replaces `PLACEHOLDER` for `root` (module docstring)."""
    try:
        target = Path(root).resolve(strict=True)
    except OSError as exc:
        raise SetError(f"forge root {root} does not resolve: {exc}") from None
    home = shell_home()
    names = []
    try:
        children = sorted(home.iterdir())
    except OSError:
        children = []
    for child in children:
        if not HOME_NAME_RE.fullmatch(child.name):
            continue
        try:
            if child.resolve(strict=True) == target:
                names.append(child.name)
        except (OSError, RuntimeError):
            continue
    if names:
        return "${HOME}/" + (Path(root).name if Path(root).name in names else sorted(names)[0])
    if ABSOLUTE_RE.fullmatch(str(target)):
        return str(target)
    raise SetError(f"forge root {target} is not linked from $HOME and its path holds a "
                   f"character outside [A-Za-z0-9._/@+-]: it cannot be written into a "
                   f"hook command")


def render(data: dict, prefix: str) -> dict:
    """A deep copy of `data` with `PLACEHOLDER` replaced by `prefix` in every
    handler `command`."""
    out = json.loads(json.dumps(data))
    for blocks in (out.get("hooks") or {}).values():
        for block in blocks if isinstance(blocks, list) else ():
            for handler in (block.get("hooks") or ()) if isinstance(block, dict) else ():
                if isinstance(handler, dict) and isinstance(handler.get("command"), str):
                    handler["command"] = handler["command"].replace(PLACEHOLDER, prefix)
    return out


def stored_form(command: str) -> str:
    """A command recorded under a `$HOME` child, in the stored placeholder form
    (the frozen pre-split record predates #1526)."""
    return RECORDED_RE.sub(PLACEHOLDER + r"/\1", command)
