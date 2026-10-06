#!/usr/bin/env python3
"""Report-only: name orphaned test graphs at a lane's finish line (hrse#2218).

A killed run leaves its test Neo4j container running, and a lane that posts its status never
looks. `l1_post.py` (a `ready-for-l3`) and `l2_post.py` (a completion) call `report()` so the
operator sees an orphan the moment a lane finishes. It is **report only**, in two senses:

* It runs the repo's own `mise run test-graph-orphans`, which removes nothing; reaping is
  `graph_fixture.py reap`, run by `lane3-end` or by hand.
* It can never change the post's outcome. A non-git directory, a repo that does not declare the
  task, a missing `mise`, a non-zero exit and a timeout each print one line and return.

Declaration is a plain text match for `[tasks.test-graph-orphans]` in the repo root's
`mise.toml`, never a `mise tasks` call: against an untrusted config that would prompt, and a
prompt in a posting tool is a hang. For the same reason `mise` runs with stdin closed and a
hard timeout, and `MISE_YES` is never set (it would trust an untrusted config).
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Callable, Optional

TASK = "test-graph-orphans"
DECLARATION = f"[tasks.{TASK}]"
TIMEOUT_S = 20
HEADING = f"[{TASK}]"

Runner = Callable[..., "subprocess.CompletedProcess[str]"]


def _say(line: str) -> None:
    print(f"{HEADING} {line}", flush=True)


def _print_output(output: str) -> None:
    """Print the task's last 20 lines under the heading, and say how many earlier ones were cut:
    a note the report drops silently is a graph the lane never sees."""
    lines = output.splitlines()
    if len(lines) > 20:
        _say(f"({len(lines) - 20} earlier line(s) not shown; run `mise run {TASK}` for all)")
    for line in lines[-20:]:
        print(f"{HEADING} {line}", flush=True)


def report(cwd: Optional[Path] = None, runner: Runner = subprocess.run) -> bool:
    """Print the orphan report for the repo at `cwd`. True when it printed orphans (exit 1 of the
    task); False in every other case, including every failure. Never raises."""
    try:
        top = runner(["git", "rev-parse", "--show-toplevel"], cwd=cwd, capture_output=True, text=True,
                     timeout=TIMEOUT_S)
        if top.returncode != 0 or not top.stdout.strip():
            _say("not in a git checkout; skipped")
            return False
        root = Path(top.stdout.strip())
        mise_toml = root / "mise.toml"
        if not mise_toml.is_file() or DECLARATION not in mise_toml.read_text(encoding="utf-8", errors="replace"):
            _say(f"{root.name} declares no {TASK} task; skipped")
            return False
        # `-q` quiets mise's own informational output (the `[task] $ command` echo, and its WARN
        # lines). The task's stdout, stderr and exit code, and mise's errors, are unchanged
        # (verified on mise 2026.9.14), so a clean run has empty output (harmonic-forge#915).
        result = runner(["mise", "run", "-q", TASK], cwd=root, capture_output=True, text=True,
                        stdin=subprocess.DEVNULL, timeout=TIMEOUT_S)
    except FileNotFoundError as exc:
        _say(f"{exc.filename or 'a required program'} is not installed; skipped")
        return False
    except subprocess.TimeoutExpired:
        _say(f"timed out after {TIMEOUT_S}s; skipped")
        return False
    except Exception as exc:  # noqa: BLE001 -- a report must never change a post's outcome
        _say(f"could not run ({type(exc).__name__}); skipped")
        return False
    output = ((result.stdout or "") + (result.stderr or "")).strip()
    if result.returncode == 0:
        if not output:
            _say("no orphaned test graphs")
            return False
        # exit 0 can still carry notes the operator must see: a graph with no readable holder, or a
        # stopped gate graph (hrse#2218). Print them; the post's outcome stays untouched.
        _say("no orphaned test graphs; notes:")
        _print_output(output)
        return False
    _print_output(output)
    if not output:
        _say(f"exited {result.returncode} with no output")
    return result.returncode == 1


if __name__ == "__main__":
    report()
