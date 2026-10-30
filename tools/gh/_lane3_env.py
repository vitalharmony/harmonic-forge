"""harmonic-forge#1218: a sweep refuses until Lane 3's own environment is ready.

R-0125 makes the gate-readiness sweep Lane 1's job, including what Lane 3's
environment needs, and it recurred five times with the rule in force. Every
check here runs from Lane 3's vantage, never Lane 1's (AC1: the link checker
judges links against the checkout it runs from, so the main checkout's copy
reports false drift), and every check is read-only (AC3): no install, no link
repair, no write in the Lane 3 worktree.

- `links_ok`: the platform link report, from Lane 3's worktree (AC1).
- `requires` / `probe_requires`: a sweep case's `Requires:` line (AC2).
- `preflight_ok`: the repo's own `lane3-preflight` mise task, when declared
  (AC5, the hrse#2522 recurrence: `lane3-begin`'s lease needs a graph whose
  pin had drifted from native, and nothing Lane 1 ran could see it).
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
import json
import os
from collections.abc import Callable
from pathlib import Path

_HERE = Path(__file__).resolve()
sys.path.insert(0, str(_HERE.parents[1] / "onboard"))
import manifest  # noqa: E402

FORGE_REPO = "vitalharmony/harmonic-forge"
REPAIR = "mise run hooks-install"
PREFLIGHT_TASK = "lane3-preflight"
LINK_TIMEOUT = 60
IMPORT_TIMEOUT = 30
PREFLIGHT_TIMEOUT = 120

REQUIRES_LINE = re.compile(r"(?im)^[ \t]*(?:[-*+][ \t]+)?\**[ \t]*Requires[ \t]*\**[ \t]*:(.*)$")
#: Any `Requires:` text at all. A count that differs from REQUIRES_LINE's is a
#: field the parser could not attribute, and refuses (preclose pass 1 #1/#2:
#: no match must never read as "nothing to check").
REQUIRES_LOOSE = re.compile(r"(?i)\bRequires[ \t]*\**[ \t]*:")
def probe_env() -> dict[str, str]:
    """Probe subprocesses never write bytecode into the tree they probe (AC3).
    Built per call, so it is this process's environment at probe time."""
    return {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
REQUIRES_TOKEN = re.compile(r"(bin|py):([A-Za-z0-9_.+-]+)")

Runner = Callable[..., subprocess.CompletedProcess]


class Lane3EnvError(Exception):
    """A refusal; the message names the worktree, the case and the token."""


def lane3_dir(repo: str, projects: dict | None = None) -> Path | None:
    """The manifest's Lane 3 worktree, or None when the repo runs no Lane 3.

    Selected by lane position, never by a name suffix: `Protocol.worktree_names`
    lists lanes (2, 3) in order whatever its `worktree_name` template spells
    (preclose pass 1 #5). A repo that runs Lane 3 but resolves no Lane 3
    worktree refuses rather than going unchecked."""
    project = (projects if projects is not None else manifest.by_repo()).get(repo)
    if project is None or project.protocol is None or not project.protocol.runs_lane3:
        return None
    worktrees = project.worktrees
    if len(worktrees) < 2:
        raise Lane3EnvError(f"{repo}: the manifest runs Lane 3 but no Lane 3 worktree resolves")
    return worktrees[1]


def _platform_source() -> Path:
    sys.path.insert(0, str(_HERE.parents[1] / "onboard"))
    import forge_onboard
    return forge_onboard.platform_source()


def _tail(result: subprocess.CompletedProcess, lines: int = 5) -> str:
    text = ((result.stderr or "") + (result.stdout or "")).strip().splitlines()
    return "\n".join(text[-lines:])


def links_ok(repo: str, lane3: Path, run: Runner = subprocess.run,
             platform: Callable[[], Path] = _platform_source) -> tuple[bool, str]:
    """AC1: the link report from Lane 3's vantage. Exit 0 is the only ok."""
    root = lane3 if repo == FORGE_REPO else platform()
    script = root / "tools" / "onboard" / "platform_link_report.py"
    if not script.is_file():
        return False, f"link report not found at {script}"
    try:
        result = run([sys.executable, "-B", str(script), "--project", str(lane3)],
                     cwd=str(lane3), capture_output=True, text=True, timeout=LINK_TIMEOUT,
                     env=probe_env())
    except subprocess.TimeoutExpired:
        return False, f"link report timed out after {LINK_TIMEOUT}s"
    return result.returncode == 0, _tail(result)


def case_label(case: str) -> str:
    """`TC<n>` for l1_post's bare case ids (`1`, `2a`)."""
    return f"TC{case}" if case[:1].isdigit() else case.upper()


def requires(case_blocks: dict[str, str]) -> dict[str, list[tuple[str, str]]]:
    """AC2: {case id: [(kind, name), ...]} from each case's one `Requires:` line.

    `case_blocks` is l1_post's per-case split (`sweep_case_blocks`), so a Tier P
    `P<n>` line is attributed to the case it canonicalizes to. A second
    `Requires:` line in one case, or a line outside the grammar, raises."""
    found: dict[str, list[tuple[str, str]]] = {}
    for case, block in case_blocks.items():
        lines = REQUIRES_LINE.findall(block)
        if len(REQUIRES_LOOSE.findall(block)) != len(lines):
            raise Lane3EnvError(
                f"{case_label(case)}: unattributed `Requires:` text; put the field on "
                "its own line (grammar: `Requires: bin:<name>; py:<module>`)")
        if not lines:
            continue
        if len(lines) > 1:
            raise Lane3EnvError(f"{case_label(case)}: more than one `Requires:` line")
        text = lines[0].strip().strip("*").strip()
        parts = [p.strip() for p in text.split(";")]
        tokens = []
        for part in parts:
            match = REQUIRES_TOKEN.fullmatch(part)
            if not match:
                raise Lane3EnvError(
                    f"{case_label(case)}: malformed `Requires:` token {part!r} "
                    "(grammar: `Requires: bin:<name>; py:<module>`)")
            tokens.append((match.group(1), match.group(2)))
        found[case] = tokens
    return found


def interpreter(lane3: Path) -> str:
    venv = lane3 / "backend" / ".venv" / "bin" / "python3"
    return str(venv) if venv.exists() else "python3"


def probe_requires(needs: dict[str, list[tuple[str, str]]], lane3: Path,
                   run: Runner = subprocess.run,
                   which: Callable[[str], str | None] = shutil.which) -> list[str]:
    """AC2: every miss, as `TC<n>: <kind>:<name>`; empty means all present."""
    misses = []
    interp = interpreter(lane3)
    for case, tokens in needs.items():
        for kind, name in tokens:
            if kind == "bin":
                ok = which(name) is not None
            else:
                try:
                    ok = run([interp, "-I", "-B", "-c", f"import {name}"], cwd=str(lane3),
                             capture_output=True, text=True, timeout=IMPORT_TIMEOUT,
                             env=probe_env()).returncode == 0
                except (subprocess.TimeoutExpired, OSError):
                    ok = False
            if not ok:
                misses.append(f"{case_label(case)}: {kind}:{name}")
    return misses


def declares_preflight(lane3: Path, run: Runner = subprocess.run) -> bool:
    """AC5: whether mise, run in Lane 3's worktree, resolves the preflight task.

    mise is asked rather than one config file parsed, so `.mise.toml`,
    `mise.local.toml`, `.config/mise.toml` and file tasks all count (preclose
    pass 1 #6). A config mise cannot read raises rather than reading as "not
    declared" (#3)."""
    try:
        result = run(["mise", "tasks", "ls", "--json", "--hidden"], cwd=str(lane3),
                     capture_output=True, text=True, timeout=PREFLIGHT_TIMEOUT)
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise Lane3EnvError(f"`mise tasks ls` could not run in {lane3}: {exc}") from exc
    if result.returncode != 0:
        raise Lane3EnvError(f"`mise tasks ls` failed in {lane3}:\n{_tail(result)}")
    try:
        tasks = json.loads(result.stdout)
    except ValueError as exc:
        raise Lane3EnvError(f"`mise tasks ls --json` in {lane3} printed no JSON: {exc}") from exc
    return any(isinstance(t, dict) and (t.get("name") == PREFLIGHT_TASK
                                        or PREFLIGHT_TASK in (t.get("aliases") or []))
               for t in tasks)


def preflight_ok(lane3: Path, run: Runner = subprocess.run) -> tuple[bool, str]:
    """AC5: `mise run lane3-preflight` in Lane 3's worktree. Exit 0 is the only ok."""
    try:
        result = run(["mise", "run", PREFLIGHT_TASK], cwd=str(lane3),
                     capture_output=True, text=True, timeout=PREFLIGHT_TIMEOUT)
    except subprocess.TimeoutExpired:
        return False, f"{PREFLIGHT_TASK} timed out after {PREFLIGHT_TIMEOUT}s"
    except OSError as exc:
        return False, f"{PREFLIGHT_TASK} could not run: {exc}"
    return result.returncode == 0, _tail(result)


def check(repo: str, case_blocks: dict[str, str], *, lane3: Path | None = None,
          run: Runner = subprocess.run,
          which: Callable[[str], str | None] = shutil.which,
          platform: Callable[[], Path] = _platform_source) -> list[str]:
    """Every refusal for this sweep, as messages; empty means ready.

    A repo whose manifest declares no Lane 3 worktree is not checked (AC1).
    The `Requires:` grammar is checked even then, since a malformed line is the
    author's error whatever the environment."""
    needs = requires(case_blocks)
    if lane3 is None:
        lane3 = lane3_dir(repo)
    if lane3 is None:
        return []
    if not lane3.is_dir():
        return [f"Lane 3 worktree {lane3} does not exist; create it, then run "
                f"`{REPAIR}` there"]
    reasons = []
    ok, detail = links_ok(repo, lane3, run=run, platform=platform)
    if not ok:
        reasons.append(f"Lane 3's platform links do not verify from {lane3}; run "
                       f"`{REPAIR}` there:\n{detail}")
    reasons += [f"Lane 3 ({lane3}) lacks {miss}" for miss in probe_requires(needs, lane3, run, which)]
    if declares_preflight(lane3, run=run):
        ok, detail = preflight_ok(lane3, run=run)
        if not ok:
            reasons.append(f"Lane 3's `mise run {PREFLIGHT_TASK}` failed in {lane3}:\n{detail}")
    return reasons
