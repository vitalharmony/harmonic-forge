#!/usr/bin/env python3
"""`forge-onboard`'s `skills` check (harmonic-forge#917).

LeasePAL and all three ke'nekted repos passed onboarding with no
`.claude/platform-skills.toml` and so no belt skill: harmonic-forge#540's
defect class in four more repos, unseen because `forge_onboard.py` checked
hooks, directives and identity but never the declared platform skills.

**Verifies against the canonical platform checkout, not this one.** Links
point at `platform_source()` (the manifest's harmonic-forge checkout), so a
`sync_rules.py` loaded from an implementation worktree compares every link
against the wrong root and reports drift that is not there.

Beside `forge_onboard.py`, not in it, for the same reason as
`forge_onboard_identity.py`: that file is far past R-0006's cap.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
from pathlib import Path
from types import ModuleType
from typing import Callable

from manifest import Project

OK, FAIL, SKIP = "ok", "FAIL", "skip"


def load_sync_rules(source: Path) -> ModuleType:
    """`sync_rules.py` from the platform checkout `source`, under a private module name."""
    path = source / "sync_rules.py"
    spec = importlib.util.spec_from_file_location("_forge_onboard_sync_rules", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def check_skills(project: Project, check: Callable, source: Path,
                 sync_rules: ModuleType | None = None):
    """FAIL when an onboarded checkout declares no platform skills or one is not linked.

    An absent manifest is a FAIL, never a pass: an undeclared repo and a
    correctly linked one are indistinguishable without it (harmonic-forge#540).
    An explicit `skills = []` is a declaration and passes.
    """
    if project.checkout is None:
        return check("skills", SKIP, "no checkout")
    if not project.checkout.is_dir():
        return check("skills", SKIP, "checkout not present")
    try:
        sync_rules = sync_rules or load_sync_rules(source)
    except Exception as exc:  # noqa: BLE001 -- reported, never swallowed
        return check("skills", FAIL, f"cannot load {source / 'sync_rules.py'}: {exc}")
    captured = io.StringIO()
    try:
        with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
            declared = sync_rules.load_skill_manifest(project.checkout)
            if declared is None:
                return check("skills", FAIL, "no .claude/platform-skills.toml — "
                             "nothing states which platform skills this repo consumes")
            names = sync_rules.expected_skill_names(declared)
            linked = sync_rules.verify_links(project.checkout, names)
    except sync_rules.ManifestError as exc:
        return check("skills", FAIL, f"unreadable .claude/platform-skills.toml: {exc}")
    if not linked:
        first = next((line.strip() for line in captured.getvalue().splitlines()
                      if line.strip()), "a declared link does not resolve")
        return check("skills", FAIL, first)
    return check("skills", OK, f"{len(names)} declared skill(s) linked"
                 if names else "no skills declared")
