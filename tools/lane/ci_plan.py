"""Print the gate a lane must pass and the tests that constrain its edit
(harmonic-forge#918), then record that it was read.

    python3 ~/harmonic-forge/tools/lane/ci_plan.py [--session ID] [path ...]

Run from any onboarded checkout or worktree (any subdirectory of it). It prints:

1. **The gate's steps**: the project's `protocol.gate_task` (`projects.toml`),
   expanded through `mise tasks info --json` -- `depends` first, then each `run`
   entry, following a `--task <x>` indirection (hrse's `check` runs
   `run_gate_steps.py --task check-steps`) -- one numbered command per script
   line, comments and `set -e` dropped. A gate task this checkout does not
   define is reported, not fatal: there is then nothing to read, and a lane must
   not be locked out of its edits for it.
2. **The constraints map** (`ci_plan_constraints.py`): for each file the branch
   changes against its merge base with `origin/main` (committed, uncommitted or
   new) plus every `path` argument (a handoff's Affected Files), the test files
   naming it, strongest matches first, then their conftest / setup files.

RECEIPT. On success, and only when a session id is known (`--session`, else
`$CLAUDE_CODE_SESSION_ID`), it records this checkout and branch in
`~/.cache/harmonic-forge/ci_plan/<session id>.json` -- the tool's own exit is the
authoritative success signal, because a PostToolUse payload carries no exit
status. `tools/hooks/require_ci_plan.py` allows an edit only in a checkout and
branch the session has read. A head change on the same branch does NOT
invalidate it. `--session` exists because after `--resume` the environment id
may differ from the hook payload's; the hook's deny message prints the right
one. The receipt is unconditional once a git checkout and a session id exist: a plan
that cannot be fully computed is printed as warnings. Only a directory that is not
a git checkout fails (exit 1, no receipt). Codex lanes have no session id: they get
the plan and no receipt.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "onboard"))
from ci_plan_constraints import (changed_files, constraints, git as _git,  # noqa: E402,F401
                                 repo_root, root_relative)
from manifest_identity import ManifestError, project_for_path  # noqa: E402

RECEIPT_DIR_ENV = "HARMONIC_FORGE_CI_PLAN_DIR"
SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_INDIRECT = re.compile(r"--task[ =](\S+)")
_NOISE = re.compile(r"^set [-+]")


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


def checkout_key(cwd: Path) -> tuple[str, str]:
    """`(toplevel, branch)` of the checkout `cwd` is in: what a receipt certifies."""
    top = (_git(cwd, "rev-parse", "--show-toplevel") or "").strip()
    branch = (_git(cwd, "branch", "--show-current") or "").strip()
    return top, branch


def has_receipt(session_id: object, cwd: Path) -> bool:
    """Has this session read the gate for the checkout and branch `cwd` is in?"""
    path = receipt_path(session_id)
    if path is None or not path.is_file():
        return False
    try:
        reads = json.loads(path.read_text(encoding="utf-8")).get("reads", {})
    except (OSError, ValueError, AttributeError):
        return False
    top, branch = checkout_key(cwd)
    entry = reads.get(top) if isinstance(reads, dict) else None
    return bool(top) and isinstance(entry, dict) and entry.get("branch") == branch


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


def script_lines(entry: str) -> list[str]:
    """The command lines of one `run` entry: continuations joined, blank lines,
    `#` comments and `set -e`-style options dropped."""
    joined = re.sub(r"\s*\\\n\s*", " ", str(entry))
    lines = [line.strip() for line in joined.splitlines()]
    return [line for line in lines if line and not line.startswith("#") and not _NOISE.match(line)]


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
            commands += script_lines(entry)
    return commands


@contextlib.contextmanager
def _receipt_lock(path: Path, wait_seconds: float = 5.0):
    """Serialize read-modify-write of one session's receipt map (the pattern
    `batch_auth._locked_state` uses, with a bounded wait instead of failing)."""
    fd = os.open(str(path.with_suffix(".lock")), os.O_CREAT | os.O_RDWR, 0o600)
    deadline = time.monotonic() + wait_seconds
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() > deadline:
                    raise OSError("receipt lock held too long")
                time.sleep(0.05)
        yield
    finally:
        os.close(fd)


def write_receipt(cwd: Path, session_id: str | None) -> str:
    path = receipt_path(session_id)
    if path is None:
        return "no session id: plan printed, no receipt written (Codex lane)"
    top, branch = checkout_key(cwd)
    head = (_git(cwd, "rev-parse", "HEAD") or "").strip()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with _receipt_lock(path):
            try:
                reads = json.loads(path.read_text(encoding="utf-8")).get("reads", {})
            except (OSError, ValueError, AttributeError):
                reads = {}
            reads[top] = {"branch": branch, "head": head, "created": time.time()}
            handle = tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False,
                                                 suffix=".tmp")
            with handle:
                handle.write(json.dumps({"reads": reads}) + "\n")
            os.replace(handle.name, path)
    except OSError as exc:
        return (f"WARNING: the receipt could not be written ({exc}); the guard fails "
                "open while its directory is unwritable")
    return f"receipt written for this checkout and branch ({path})"


def main(argv: list[str]) -> int:
    """Always exits 0 with a receipt once a git checkout and session id exist:
    only the READING is enforced, so a plan that cannot be fully computed
    (no registered project, no gate task, no merge base, a failing search) is
    printed as warnings, never as a reason to withhold the receipt
    (harmonic-forge#918 sticky-wicket ruling). Not a git checkout: exit 1."""
    session = os.environ.get("CLAUDE_CODE_SESSION_ID")
    args = list(argv)
    if "--session" in args:
        index = args.index("--session")
        session = args[index + 1] if index + 1 < len(args) else ""
        del args[index:index + 2]
    invoked = Path.cwd()
    try:
        root = repo_root(invoked)
    except RuntimeError as exc:
        print(f"ci_plan: FAILED, no receipt written: {exc}", file=sys.stderr)
        return 1
    warnings: list[str] = []
    project, task = None, None
    try:
        project = resolve_project(root)
        task = project.protocol.gate_task if project.protocol else None
        if not task:
            warnings.append(f"{project.name} declares no protocol.gate_task in projects.toml")
    except (ManifestError, OSError, ValueError) as exc:
        warnings.append(f"no registered project owns this checkout ({exc})")
    name = project.name if project else root.name
    if task:
        try:
            steps = expand(task, root)
            print(f"GATE: `mise run {task}` for {name} -- {len(steps)} step(s), in run order")
            for number, command in enumerate(steps, 1):
                print(f"{number:3}. {command}")
        except (RuntimeError, OSError, subprocess.SubprocessError, ValueError) as exc:
            warnings.append(f"`mise run {task}` could not be read here ({exc})")
    try:
        changed = changed_files(root)
    except RuntimeError as exc:
        warnings.append(f"{exc}; mapping only the paths given as arguments")
        changed = []
    files = sorted(set(changed) | {root_relative(a, invoked, root) for a in args if a})
    try:
        mapped = constraints(root, files)
    except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
        warnings.append(f"the constraints search failed ({exc})")
        mapped = []
    for warning in warnings:
        print(f"WARNING: {warning}\n  (no plan to read for this part; the receipt is still written)")
    print(f"\nCONSTRAINTS: tests that name the {len(files)} file(s) this branch changes")
    print("\n".join(mapped) if mapped else "  (no test names any changed file"
          + ("; the branch changes nothing yet: pass the files you intend to edit as "
             "arguments to see their tests)" if not files else ")"))
    print(f"\n{write_receipt(root, session)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
