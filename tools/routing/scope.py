"""The file scope a filing body declares (harmonic-forge#1232, semantic routing A2).

At filing time the only scope there is is the backticked paths in the issue
body. `scope_for_filing` turns them into A1's `Scope` for `gates.evaluate`:
one repo half (the issue's own), the labels the filing carries, no threat
model (a handoff declares that later), and never runner-eligible.

`PATH_TOKEN` is HRSE2's pattern from
`.claude/skills/sprint-plan/scripts/milestone_summary_analysis.py`. Forge code
cannot import an HRSE2 skill (making it shared is harmonic-forge#101), so the
pattern is restated here; harmonic-forge#101 reconciles the two.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from gates import Scope  # noqa: E402

#: Broader than HRSE2's: pass 1 of harmonic-forge#1232 found a multi-segment
#: directory (`tools/hooks/`), a `:line` suffix and an extensionless file
#: (`tools/hooks/Makefile`) all extracted nothing and filed as Path 1.
#: Group 1 is the path alone; the optional `:42` or `:42-60` is not captured.
PATH_TOKEN = re.compile(
    r"`((?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]*"
    r"|[A-Za-z0-9_.-]+\.[A-Za-z0-9]+"
    r"|\.[a-z]+rules)"
    r"(?::\d+(?:-\d+)?)?`")


def paths_from_body(body: str) -> list[str]:
    """Backticked file or directory paths in `body`, in order, once each."""
    seen: list[str] = []
    for match in PATH_TOKEN.finditer(body or ""):
        path = match.group(1)
        if path not in seen:
            seen.append(path)
    return seen


def scope_for_filing(repo: str, body: str, labels: list[str]) -> Scope:
    paths = paths_from_body(body)
    return Scope(repo=repo, halves={repo: paths} if paths else {}, labels=list(labels or []),
                 threat_model=None, runner_eligible=False)
