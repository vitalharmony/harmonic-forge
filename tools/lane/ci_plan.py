#!/usr/bin/env python3
"""Print the gate a lane must pass and the tests that constrain its edit
(harmonic-forge#918), then record that it was read.

    python3 ~/harmonic-forge/tools/lane/ci_plan.py [path ...]

Run from any onboarded checkout or worktree. It prints:

1. **The gate's steps**: the project's `protocol.gate_task` (`projects.toml`),
   expanded through `mise tasks info --json` -- `depends` first, then each `run`
   entry, following a `--task <x>` indirection (hrse's `check` runs
   `run_gate_steps.py --task check-steps`) -- one numbered command per line.
2. **The constraints map**: for each file the branch changes against its merge
   base with `origin/main` (committed or not), plus every `path` argument (a
   handoff's Affected Files), each test file naming that file's basename -- or,
   for Python, its dotted module path -- with the matching line, which finds a
   test that reads a helper's source text as well as one that imports it; then
   the conftest / setup file of each matched test directory.

RECEIPT. On success, and only when `$CLAUDE_CODE_SESSION_ID` is non-empty, it
writes `~/.cache/harmonic-forge/ci_plan/<session id>.json` (branch, head,
timestamp) -- the tool's own exit is the authoritative success signal, because
a PostToolUse payload carries no exit status. `tools/hooks/require_ci_plan.py`
keys on it. The receipt is SESSION-scoped only: a later head change does not
invalidate it (harmonic-forge#918). Any failure writes nothing and exits 1.
Codex lanes have no session variable: they get the plan and no receipt.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "onboard"))
from manifest_identity import ManifestError, project_for_path  # noqa: E402

RECEIPT_DIR_ENV = "HARMONIC_FORGE_CI_PLAN_DIR"
SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
STOPLIST = {"index", "app", "main", "utils", "__init__", "init", "test", "tests"}
TEST_PATHSPECS = [":(glob)**/tests/**", ":(glob)**/test/**", ":(glob)**/*_test.*",
                  ":(glob)**/*Test.*", ":(glob)**/test_*"]
SETUP_NAMES = ("conftest.py", "flutter_test_config.dart", "TestCase.php", "setup.ts",
               "setupTests.ts", "vitest.setup.ts")
MAX_HITS = 8
_INDIRECT = re.compile(r"--task[ =](\S+)")


def receipt_dir() -> Path:
    override = os.environ.get(RECEIPT_DIR_ENV)
    return Path(override) if override else Path.home() / ".cache" / "harmonic-forge" / "ci_plan"


def receipt_path(session_id: object) -> Path | None:
    if not isinstance(session_id, str) or not SESSION_ID_RE.match(session_id):
        return None
    return receipt_dir() / f"{session_id}.json"


def allow_path(session_id: object) -> Path | None:
    """The operator's `ALLOW EDIT` grant for this session (grant_ci_plan_override.py)."""
    path = receipt_path(session_id)
    return path.with_suffix(".allow") if path else None


def _git(cwd: Path, *args: str) -> str | None:
    try:
        done = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True,
                              text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout if done.returncode == 0 else None


def resolve_project(cwd: Path):
    """The owning project; a per-issue worktree is registered nowhere, so on a
    miss ask git for the common dir, whose parent is the main checkout (the
    fallback `manifest_identity.workspace_for` uses, harmonic-forge#917)."""
    try:
        return project_for_path(cwd)
    except ManifestError:
        common = _git(cwd, "rev-parse", "--path-format=absolute", "--git-common-dir")
        if not common or not common.strip():
            raise
        return project_for_path(Path(common.strip()).parent)


def task_info(task: str, cwd: Path) -> dict:
    done = subprocess.run(["mise", "tasks", "info", "--json", task], cwd=cwd,
                          capture_output=True, text=True, timeout=60)
    if done.returncode != 0:
        raise RuntimeError(f"mise task {task!r} not found in {cwd}: {done.stderr.strip()[:200]}")
    return json.loads(done.stdout)


def expand(task: str, cwd: Path, seen: set[str] | None = None) -> list[str]:
    """Every command `task` runs, in order, `depends` first, `--task` followed."""
    seen = set() if seen is None else seen
    if task in seen:
        return []
    seen.add(task)
    info = task_info(task, cwd)
    commands: list[str] = []
    for dep in info.get("depends") or []:
        commands += expand(str(dep).split()[0], cwd, seen)
    run = info.get("run") or []
    for entry in [run] if isinstance(run, str) else run:
        indirect = _INDIRECT.search(entry) if "run_gate_steps" in entry else None
        if indirect:
            commands += expand(indirect.group(1), cwd, seen)
        else:
            commands.append(" ; ".join(line.strip() for line in str(entry).splitlines()
                                       if line.strip() and line.strip() != "set -e"))
    return commands


def changed_files(cwd: Path) -> list[str]:
    base = _git(cwd, "merge-base", "origin/main", "HEAD")
    if not base:
        raise RuntimeError("cannot find the merge base with origin/main (run `git fetch`)")
    out = _git(cwd, "diff", "--name-only", base.strip())
    if out is None:
        raise RuntimeError("git diff against the merge base failed")
    return [line for line in out.splitlines() if line]


def _is_test(path: str) -> bool:
    name = Path(path).name
    return bool(re.search(r"(^|/)(tests?)/", path) or name.startswith("test_")
                or re.search(r"(_test|Test)\.", name))


def needles(path: str) -> list[str]:
    stem = Path(path).stem
    if stem.lower() in STOPLIST:
        return []
    found = [stem]
    if path.endswith(".py"):
        parts = Path(path).with_suffix("").parts
        found += [".".join(parts[-n:]) for n in (2, 3) if len(parts) >= n]
    return found


def constraints(cwd: Path, files: list[str]) -> list[str]:
    lines: list[str] = []
    for path in files:
        if _is_test(path):
            continue
        hits: dict[str, str] = {}
        for needle in needles(path):
            out = _git(cwd, "grep", "-n", "-w", "-F", "-e", needle, "--", *TEST_PATHSPECS)
            for row in (out or "").splitlines():
                name, _, rest = row.partition(":")
                hits.setdefault(name, rest.strip()[:110])
        if not hits:
            continue
        lines.append(f"  {path}")
        for name, text in list(hits.items())[:MAX_HITS]:
            lines.append(f"    {name}:{text}")
        if len(hits) > MAX_HITS:
            lines.append(f"    ... and {len(hits) - MAX_HITS} more test file(s)")
        for setup in _setup_files(cwd, hits):
            lines.append(f"    setup: {setup}")
    return lines


def _setup_files(cwd: Path, test_files: dict[str, str]) -> list[str]:
    found: list[str] = []
    for name in test_files:
        directory = Path(name).parent
        while True:
            for setup in SETUP_NAMES:
                candidate = directory / setup
                if (cwd / candidate).is_file() and str(candidate) not in found:
                    found.append(str(candidate))
            if directory == Path("."):
                break
            directory = directory.parent
    return found


def write_receipt(cwd: Path) -> str:
    path = receipt_path(os.environ.get("CLAUDE_CODE_SESSION_ID"))
    if path is None:
        return "no $CLAUDE_CODE_SESSION_ID: plan printed, no receipt written (Codex lane)"
    path.parent.mkdir(parents=True, exist_ok=True)
    branch = (_git(cwd, "branch", "--show-current") or "").strip()
    head = (_git(cwd, "rev-parse", "HEAD") or "").strip()
    path.write_text(json.dumps({"branch": branch, "head": head, "created": time.time()}) + "\n",
                    encoding="utf-8")
    return f"receipt written for this session ({path})"


def main(argv: list[str]) -> int:
    cwd = Path.cwd()
    try:
        project = resolve_project(cwd)
        task = project.protocol.gate_task if project.protocol else None
        if not task:
            raise RuntimeError(f"{project.name} declares no protocol.gate_task in projects.toml")
        steps = expand(task, cwd)
        files = sorted(set(changed_files(cwd)) | {a for a in argv if a})
        mapped = constraints(cwd, files)
    except (ManifestError, RuntimeError, OSError, subprocess.SubprocessError, ValueError) as exc:
        print(f"ci_plan: FAILED, no receipt written: {exc}", file=sys.stderr)
        return 1
    print(f"GATE: `mise run {task}` for {project.name} -- {len(steps)} step(s), in run order")
    for number, command in enumerate(steps, 1):
        print(f"{number:3}. {command}")
    print(f"\nCONSTRAINTS: tests that name the {len(files)} file(s) this branch changes")
    print("\n".join(mapped) if mapped else "  (no test names any changed file)")
    print(f"\n{write_receipt(cwd)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
