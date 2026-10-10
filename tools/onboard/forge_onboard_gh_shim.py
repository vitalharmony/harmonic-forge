#!/usr/bin/env python3
"""`forge-onboard`'s `gh shim` check (harmonic-forge#960).

R-0363's first layer is `~/.local/bin/gh`, a link to the platform's
`tools/gh/gh_shim`, first on PATH. It was never installed, and no check said
so: every `gh` call made from inside a script skipped the scan refusal and the
budget floor while the doctor reported green.

**Detects only; never installs.** Installing is the operator's own action
(`tools/gh/install_gh_shim.sh:8-10`, operator decision 7, 2026-10-09), so this
check prints the literal install command and writes nothing, under `--apply`
too.

User-scope, like `check_memory`: the same answer for every project.

Paths are compared resolved, never as text: the operator's PATH spells
`~/.local/bin` as `~/.local/share/../bin`.

Beside `forge_onboard.py`, not in it, for the same reason as
`forge_onboard_skills.py`: that file is far past R-0006's cap.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Callable

OK, FAIL = "ok", "FAIL"
NAME = "gh shim"


def check_gh_shim(make_check: Callable, platform: Path) -> object:
    target = Path.home() / ".local" / "bin" / "gh"
    shim = platform / "tools" / "gh" / "gh_shim"
    cmd = f"bash {platform / 'tools' / 'gh' / 'install_gh_shim.sh'}"
    if not target.is_symlink():
        if target.exists():
            return make_check(NAME, FAIL, f"{target} is a real file, not the shim; inspect it, then: {cmd}")
        return make_check(NAME, FAIL, f"{target} is missing (R-0363); operator installs it with: {cmd}")
    if os.path.realpath(target) != os.path.realpath(shim):
        return make_check(NAME, FAIL, f"{target} -> {os.readlink(target)}, not {shim}; re-link with: {cmd}")
    if not (shim.is_file() and os.access(shim, os.X_OK)):
        # The link is right but its target is gone or not executable: the PATH
        # lookup would skip it, and "put ~/.local/bin first" would be the wrong cause.
        return make_check(NAME, FAIL, f"{target} links to {shim}, which is missing or not "
                                      f"executable; restore it in the platform checkout, then: {cmd}")
    found = shutil.which("gh")
    resolved = os.path.realpath(found) if found else None
    if resolved != os.path.realpath(target):
        return make_check(NAME, FAIL, f"gh on PATH resolves to {resolved}, not {target}; "
                                      f"put ~/.local/bin first on PATH, then: {cmd}")
    return make_check(NAME, OK, f"{target} -> {shim}")
