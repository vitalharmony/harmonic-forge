#!/usr/bin/env python3
"""The te-runner's serial driver (harmonic-forge#1055, G1, G3).

    te_run.py run --queue FILE --max-budget-usd N [--max-steps 5]

Works a `te_queue.py` queue one item at a time. The harness alone writes item
statuses: the agent is never given the queue path. Per item:

1. re-check eligibility (`te_queue.first_failure`); an ineligible item is
   reduced and set aside without a checkout;
2. create `fix/<N>-te-runner` from `origin/main` in a fresh `git clone
   --shared` (an existing checkout, local branch, `refs/te-runner/<N>` or
   runner-fork branch of that name blocks the item, never reused). The agent
   and every run of its code (the check steps, the kill-check tests, the
   preclose refuters, the cross-family reviewer) run inside `sandbox.py`'s
   allowlist `bwrap` boundary (harmonic-forge#1098);
3. run `claude -p` in it with `te-runner.settings.json`, the runner's own key
   and gh config (`preclose_driver.runner_env`), and stream-json with hook
   events. G3: the run aborts unless `SessionStart`, `UserPromptSubmit` and
   `Stop` hook events each appear and `init.apiKeySource` is not `none`;
4. the check (`CHECK_STEPS`), `kill_runner.py run`, then `kill_check.py` over checks
   generated from the bundle and the agent's own diff reversed (operator
   ruling on this issue), `deny_check.py diff`, and `preclose_driver.py run`
   exactly once (any non-zero exit sets the item aside; never retried);
5. `verdict.reduce` over the facts. Only `green` is pushed to the
   `runner-fork` remote and opened as a PR from the runner identity;
   `pushed_sha` is saved before the push. The live PR is then read into the
   facts and reduced again, and the preclose summary is attached as a PR
   comment. The runner never merges, closes, labels or comments on an issue.

`deny_check.py boundary` runs once at start: a failure aborts the run. Then
`preflight.py`'s checks run (harmonic-forge#1112 AC9); any failure aborts
the run, listing every one. `te_run.py preflight --queue FILE` runs them alone.

No oracle process runs with `PYTHONSAFEPATH` (harmonic-forge#1112 AC2): it kept
the repo off `sys.path`, so no check could import it. A head-authored module
that would shadow pytest or the standard library is a `deny_check.py diff` hit.

The check fact (AC4, reforged after preclose pass 1): the runner runs each
step of `CHECK_STEPS` (mise.toml's `check`, `ci-check` inlined) on its own,
for base and head alike, in a fresh `--shared` clone of the pinned SHA in a
per-process `mkdtemp` directory. Only the test step is base-relative: a test
ID failing on the head and not on the base, a module-level error, a test step
that exits non-zero naming no test, or fewer tests run. Every other step fails
the head on any non-zero exit, and failing on the base is fatal. The base's
result is cached under `BASE_CHECK_ROOT/<sha>/`, under a lock and atomically,
only when the run completed.

Every per-item artifact is kept under `RUNS_ROOT/<run_id>/<issue>/` (AC7); the
newest `KEEP_RUNS` runs are kept, older ones pruned at run start.

Exit 0 when the queue was worked, 3 when the run aborted (boundary, preflight,
G3, or a blocked item), 2 on an error before any item ran.
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import fcntl
import json
import math
import os
import re
import secrets
import shutil
import subprocess
import stat
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "lane"))
sys.path.insert(0, str(HERE.parent / "telemetry"))
import acceptance  # noqa: E402
import deny_check  # noqa: E402
import preclose_driver  # noqa: E402
import preflight  # noqa: E402
import provision  # noqa: E402
import sandbox  # noqa: E402
import queue_state  # noqa: E402
import te_queue  # noqa: E402
import verdict  # noqa: E402

SOURCE = HERE.parent.parent
provision_clone = provision.provision_clone
SETTINGS = HERE / "te-runner.settings.json"
PRECLOSE_DRIVER = HERE / "preclose_driver.py"
WORKTREE_ROOT = Path.home() / "Harmonic_Projects" / ".worktrees"
RUNNER_LOGIN = deny_check.RUNNER_LOGIN
FORK_URL = f"https://github.com/{RUNNER_LOGIN}/harmonic-forge.git"
REQUIRED_HOOKS = ("SessionStart", "UserPromptSubmit", "Stop")
SELF_FLAG = "TE-RUNNER-SELF-FLAG:"
#: The agent clone's `origin` (AC3): manifest code reads owner/name from it.
#: Objects are still borrowed from SOURCE through the clone's alternates.
UPSTREAM_URL = "https://github.com/vitalharmony/harmonic-forge.git"
TE_RUNNER_HOME = Path.home() / ".local" / "share" / "te-runner"
RUNS_ROOT = TE_RUNNER_HOME / "runs"
BASE_CHECK_ROOT = TE_RUNNER_HOME / "base-check"
KEEP_RUNS = 10
CLAUDE = "claude"
#: The runner identity's token, for the agent's gh only: bwrap cannot reach the
#: keyring (harmonic-forge#1107). Mode 0o600, non-empty, or the run aborts.
RUNNER_GH_TOKEN = Path.home() / ".config" / "te-runner" / "gh-token"
#: How much of the agent's stderr a G3 abort carries.
STDERR_TAIL_LINES, STDERR_TAIL_CHARS = 20, 2000
RECONCILED = "recovery (push attempted):"
#: Every `done` item carries these keys (AC3); work_item fills what it learned.
DONE_KEYS = {"reason": "", "verdict": None, "facts": None, "ac_titles": [], "diff_stat": "",
             "head_sha": "", "branch": "", "pr": "", "pushed_sha": "", "preclose_summary": "",
             "live_read_at": ""}


class Abort(Exception):
    """The whole run stops; the current item is blocked with this note."""


class Blocked(Exception):
    """This item cannot proceed; the note says why."""


def log(step: str, message: str) -> None:
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"{stamp} te-run {step}: {message}", file=sys.stderr, flush=True)


def run(argv: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None,
        timeout: int | None = None) -> subprocess.CompletedProcess:
    try:
        # errors="replace": an agent's non-UTF-8 output must never escape the
        # per-item boundary as UnicodeDecodeError. The reverse patch, whose
        # bytes matter, is read separately (kill_check_checks).
        return subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True,
                              errors="replace", timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(argv, 127, "", f"{argv[0]}: {exc}")


def git(*args: str, cwd: Path | None = None) -> str:
    # SOURCE is read at call time, never bound as a default: a default would
    # freeze the module's value and ignore any later change to it.
    result = run(["git", *args], cwd=cwd or SOURCE)
    if result.returncode:
        raise Blocked(f"git {' '.join(args)} failed: {result.stderr.strip()[:200]}")
    return result.stdout.strip()


def source_objects() -> Path:
    """SOURCE's object store: the `--shared` clone borrows its objects from
    here, so each boundary that runs git in the clone names it as a read-only
    input (the allowlist hides everything else in SOURCE)."""
    return (SOURCE / git("rev-parse", "--git-path", "objects")).resolve()


def oracle_env(env: dict[str, str]) -> dict[str, str]:
    """No `PYTHONSAFEPATH`, inherited or set (AC2): the oracle's checks must
    import the repo. Shadowing is denied statically (`deny_check.shadows`)."""
    clean = {k: v for k, v in env.items() if k != "PYTHONSAFEPATH"}
    return {**clean, "PYTHONDONTWRITEBYTECODE": "1"}


def keep(evidence: Path | None, name: str, result: subprocess.CompletedProcess) -> None:
    """A subprocess's stdout and stderr, kept beside the item (AC7)."""
    if evidence is None:
        return
    evidence.mkdir(parents=True, exist_ok=True)
    (evidence / f"{name}.stdout").write_text(result.stdout or "", encoding="utf-8")
    (evidence / f"{name}.stderr").write_text(result.stderr or "", encoding="utf-8")


def prune_runs(root: Path, keep_runs: int = KEEP_RUNS) -> None:
    """The newest `keep_runs` run dirs stay; older ones go. Run ids sort by time."""
    if not root.is_dir():
        return
    runs = sorted(p for p in root.iterdir() if p.is_dir() and not p.is_symlink())
    for old in runs[:-keep_runs] if keep_runs > 0 else runs:
        shutil.rmtree(old, ignore_errors=True)


def new_run_id() -> str:
    # A random suffix: two runs in the same second must never share an id.
    return ("run-" + dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            + "-" + secrets.token_hex(4))


# --- the queue -------------------------------------------------------------------

def reconcile(state: dict) -> bool:
    """A kill between push and save leaves `pushed_sha` without `pr`: make the
    pushed branch visible, then `queue_state.recover` blocks the rest."""
    changed = False
    for item in state["items"]:
        if item["status"] in ("blocked", "in_progress") and item.get("pushed_sha") \
                and not item.get("pr"):
            if item["note"].startswith(RECONCILED):
                continue
            # pushed_sha is saved before the push, so it proves an attempt, not
            # a push: say so, and keep what the run itself recorded.
            item["status"] = "blocked"
            item["note"] = (f"{RECONCILED} branch {item.get('branch')} may have been pushed to "
                            f"runner-fork at {item['pushed_sha'][:12]}; no PR was recorded. "
                            f"Earlier note: {item['note'] or 'none'}")
            changed = True
    return queue_state.recover(state) or changed


def save(path: Path, state: dict) -> None:
    queue_state.save(path, state)


# --- the agent run ---------------------------------------------------------------

def agent_prompt(issue: int, body: str, handoff: str) -> str:
    """The issue and its handoff only: never the queue, never a status (AC6)."""
    return (f"Implement harmonic-forge#{issue} in this checkout from the issue and its Lane 1 "
            "handoff below. Commit your work with `git commit` when it is done; do not push, "
            "and do not open a pull request.\n\n"
            "Commands you may run: Read, Edit and Write; in Bash only `git add`, "
            "`git commit`, `git status`, `git diff`, `git log`, `git show`, `git checkout`, "
            "`git switch`, `git restore`, `git stash`, `git mv`, `git rm`, `git reset`, "
            "`git rev-parse`, `git branch`, `python3`, `mise run`, `mise tasks`, `mkdir`, `rm`, "
            "`cp`, `mv` and `touch`. Everything else is refused, `bash`, `sh`, `env`, `eval` "
            "and `xargs` included.\n\n"
            # harmonic-forge#1137: the allow list matches by prefix, so a command that starts
            # with anything else is refused even when every part of it is allowed.
            "Your working directory is already the checkout. Each Bash call is exactly one of "
            "the commands above, starting with it: never prefix `cd` or use `git -C`, never "
            "chain with `&&` or `;`, never pipe (`|`) or redirect (`>`), and never feed "
            "`python3` a heredoc (`<<`) or `-` stdin script. Instead: run multi-line Python as "
            "`python3 -c '...'`, never as a script file (no scratch file anywhere: `.claude/` is "
            "refused and a file outside the checkout is too); run tests in a "
            "subdirectory with `python3 -m unittest discover -s <dir> -t <dir>` or "
            "`python3 -m unittest <dotted.module>`; read full command output rather than "
            "piping it to `head` or `tail`; and inspect files with Read or `python3 -c`, "
            "never `ls`, `cat`, `grep`, `find`, `sed` or `echo`. Some command shapes are refused "
            "even when every part is allowed, and a refused command sets your item aside.\n\n"
            "A refusal is never a reason to stop: use Read or Edit, or an allowed "
            "equivalent, and commit what exists.\n\n"
            "Set environment variables inside `python3` (`os.environ`), never as a command "
            "prefix: `FOO=1 python3 ...` is refused. `pytest` is not installed: run tests with "
            "`python3 -m unittest` or `python3 tools/run_tests.py`.\n\n"
            "The harness runs the kill checks, `mise run check`, the deny check and the preclose "
            "check after you finish. You do not run them and cannot see them: skip any handoff "
            "step that names them (the kill-check bundle, `preclose-check`).\n\n"
            "An acceptance criterion that needs a deployed or live measurement: implement it "
            "and name it in your final message. That alone is not a reason to self-flag.\n\n"
            "If you cannot complete the issue honestly for any other reason, end your final "
            f"message with one line `{SELF_FLAG} <reason>`.\n\n"
            f"## Issue\n\n{body}\n\n## Handoff\n\n{handoff}\n")


def claude_argv(budget: str) -> list[str]:
    return [CLAUDE, "-p", "--output-format", "stream-json", "--verbose",
            "--include-hook-events", "--settings", str(SETTINGS),
            "--permission-mode", "dontAsk", "--permission-prompts", "none",
            "--max-budget-usd", budget]


def parse_stream(text: str) -> dict:
    hooks: collections.Counter = collections.Counter()
    init, result = None, None
    for line in text.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        kind, subtype = event.get("type"), event.get("subtype")
        if kind == "system" and subtype in ("hook_started", "hook_response"):
            name = event.get("hook_event") or event.get("hook_name")
            if isinstance(name, str):
                hooks[(subtype, name)] += 1
        elif kind == "system" and subtype == "init":
            init = event
        elif kind == "result":
            result = event
    return {"hooks": hooks, "init": init, "result": result}


def g3_problems(parsed: dict) -> list[str]:
    problems = [f"no {hook} hook events" for hook in REQUIRED_HOOKS
                if not (parsed["hooks"][("hook_started", hook)]
                        and parsed["hooks"][("hook_response", hook)])]
    source = (parsed["init"] or {}).get("apiKeySource")
    if not parsed["init"]:
        problems.append("no init event")
    elif source in (None, "none"):
        problems.append(f"init.apiKeySource is {source!r}: not the runner's own key")
    if not isinstance(parsed["result"], dict):
        problems.append("no result event")
    return problems


def run_agent(clone: Path, prompt: str, budget: str, env: dict[str, str],
              log_path: Path, transcripts: Path) -> dict:
    if not SETTINGS.is_file():
        raise Abort(f"settings file {SETTINGS} is missing")
    try:
        # The agent runs inside the bwrap boundary (harmonic-forge#1098): only its
        # clone and a private transcripts dir are writable.
        argv = sandbox.wrap(claude_argv(budget), clone, transcripts, profile="agent",
                            inputs=(source_objects(),))
    except sandbox.SandboxError as exc:
        raise Abort(str(exc)) from exc
    try:
        done = subprocess.run(argv, cwd=clone, env=env, input=prompt,
                              capture_output=True, text=True, errors="replace")
    except OSError as exc:
        raise Blocked(f"claude did not start: {exc}") from exc
    log_path.write_text(done.stdout, encoding="utf-8")
    log_path.with_suffix(log_path.suffix + ".stderr").write_text(done.stderr or "",
                                                                 encoding="utf-8")
    parsed = parse_stream(done.stdout)
    problems = g3_problems(parsed)
    if problems:
        message = "G3: " + "; ".join(problems)
        tail = stderr_tail(done.stderr or "")
        if tail:
            message += "; stderr: " + tail
        raise Abort(message)
    if done.returncode:  # G3 passed: the oracle judges the head, the exit is only logged
        tail = stderr_tail(done.stderr or "")
        log("agent", f"claude exited {done.returncode}" + (f"; stderr: {tail}" if tail else ""))
    return parsed


def stderr_tail(text: str) -> str:
    lines = text.strip().splitlines()[-STDERR_TAIL_LINES:]
    return "\n".join(lines)[-STDERR_TAIL_CHARS:]


def runner_token() -> str | None:
    """The runner token, or None (logged) when it is missing, loose or empty."""
    try:
        mode = stat.S_IMODE(os.stat(RUNNER_GH_TOKEN).st_mode)
        token = RUNNER_GH_TOKEN.read_text(encoding="utf-8").strip()
    except OSError as exc:
        log("token", f"runner gh token {RUNNER_GH_TOKEN} unreadable: {exc}")
        return None
    if mode != 0o600:
        log("token", f"runner gh token {RUNNER_GH_TOKEN} has mode {oct(mode)}, not 0o600")
        return None
    if not token:
        log("token", f"runner gh token {RUNNER_GH_TOKEN} is empty")
        return None
    return token


def probe(agent_env: dict[str, str]) -> bool:
    """HTTPS and identity from inside the agent's boundary, before any item:
    `gh api user` must answer with the runner's login (harmonic-forge#1107)."""
    with tempfile.TemporaryDirectory(prefix="te-run-probe-") as name:
        scratch = Path(name)
        try:
            argv = sandbox.wrap(["gh", "api", "user", "--jq", ".login"], scratch, scratch,
                                profile="agent")
            done = subprocess.run(argv, cwd=scratch, env=agent_env, capture_output=True,
                                  text=True, errors="replace", timeout=120)
        except (sandbox.SandboxError, OSError, subprocess.TimeoutExpired) as exc:
            log("probe", str(exc))
            return False
    login = done.stdout.strip()
    if done.returncode or login != deny_check.RUNNER_LOGIN:
        log("probe", f"exit {done.returncode}, login {login!r}, not {deny_check.RUNNER_LOGIN}; "
                     f"stderr: {stderr_tail(done.stderr or '')}")
        return False
    return True


# --- the oracle --------------------------------------------------------------------

def snapshot_bundle(issue: int, pin: str, scratch: Path) -> Path:
    """`te_queue.snapshot_bundle`, BEFORE the agent runs; a refusal blocks the item."""
    try:
        return te_queue.snapshot_bundle(issue, pin, scratch)
    except te_queue.QueueError as exc:
        raise Blocked(str(exc)) from exc


def kill_facts(issue: int, base: str, head: str, env: dict[str, str], bundle: Path,
               pin: str, evidence: Path | None = None) -> list[dict]:
    result = run([sys.executable, str(HERE / "kill_runner.py"), "run",
                  "--repo", te_queue.REPO, "--issue", str(issue), "--base", base, "--head", head,
                  "--bundle", str(bundle), "--source-repo", str(SOURCE)],
                 cwd=SOURCE, env=oracle_env(env))
    keep(evidence, "kill_runner", result)
    if result.returncode:
        log("kill", f"kill_runner could not run: {result.stderr.strip()[:200]}")
        return []
    try:
        report = json.loads(result.stdout)
        checks, digest = report["checks"], report["bundle_sha256"]
    except (ValueError, KeyError, TypeError):
        return []
    if digest != pin:  # the bundle kill_runner actually ran is the pinned one
        log("kill", "kill_runner ran a bundle other than the pinned one")
        return []
    return checks


def kill_check_checks(bundle: Path, base: str, head: str, scratch: Path) -> Path:
    """One `kill_check.py` check per bundle check: the bundle's files copied
    into the scratch tree, then its test; the stub patch is the agent's change
    reversed, outside the bundle's own paths (operator ruling, harmonic-forge#1055)."""
    owned = set(te_queue.kill_runner.bundle_files(bundle))
    changed = [path for path in git("diff", "--name-only", "--no-renames", "-z", base, head)
               .split("\0") if path and path not in owned]
    patch = scratch / "revert.patch"
    reverse = subprocess.run(["git", "diff", "--binary", "--no-renames", head, base, "--",
                              *changed], cwd=SOURCE, capture_output=True) if changed else None
    if reverse is None or reverse.returncode or not reverse.stdout:
        raise Blocked("the agent's change has nothing to reverse")
    patch.write_bytes(reverse.stdout)
    checks = []
    for entry in te_queue.kill_runner.load_checks(bundle):
        overlay = f"cp -R {_quote(str(bundle / 'files'))}/. . && exec \"$@\""
        # The test executes the agent's code, so it runs inside the oracle
        # boundary, writable = kill_check.py's own scratch tree (the cwd at test
        # time), with the bundle snapshot its one read-only input. The trusted
        # sandbox.py is named by absolute path, never the tree's copy.
        checks.append({"ac": entry["ac"], "mechanism": "the agent's change, reversed",
                       "patch": str(patch),
                       "test": [sys.executable, str(HERE / "sandbox.py"), "here",
                                "--ro", str(bundle), "--",
                                "sh", "-c", overlay, "kill-check", *entry["test"]]})
    path = scratch / "checks.json"
    path.write_text(json.dumps(checks, indent=1), encoding="utf-8")
    return path


def _quote(text: str) -> str:
    return "'" + text.replace("'", "'\\''") + "'"


def kill_receipt(issue: int, base: str, head: str, env: dict[str, str], scratch: Path,
                 bundle: Path) -> bool:
    try:
        checks = kill_check_checks(bundle, base, head, scratch)
    except (Blocked, te_queue.kill_runner.RunnerError) as exc:
        log("kill-check", str(exc))
        return False
    result = run([sys.executable, str(HERE.parent / "gh" / "kill_check.py"), "run",
                  "--repo", te_queue.REPO, "--issue", str(issue), "--base", base, "--head", head,
                  "--checks", str(checks)], cwd=SOURCE, env=oracle_env(env))
    keep(scratch, "kill_check", result)
    log("kill-check", (result.stdout.strip().splitlines() or ["no output"])[-1])
    return result.returncode == 0


def deny_hits(base: str, head: str) -> list[str]:
    result = run([sys.executable, str(HERE / "deny_check.py"), "diff", "--base", base,
                  "--head", head], cwd=SOURCE)
    if result.returncode not in (0, 3):
        return [f"deny-check could not run: {result.stderr.strip()[:150]}"]
    try:
        return json.loads(result.stdout)["hits"]
    except (ValueError, KeyError, TypeError):
        return ["deny-check printed no result"]


def preclose_fact(issue: int, base: str, head: str, tier: str, env: dict[str, str],
                  out: Path, harness: Path | None = None) -> dict:
    """Exactly one driver run; any non-zero exit sets the item aside (operator
    ruling, harmonic-forge#1059): the runner never retries it."""
    result = run([sys.executable, str(PRECLOSE_DRIVER), "run",
                  "--repo", te_queue.REPO, "--issue", str(issue), "--base", base, "--head", head,
                  "--source-repo", str(SOURCE), "--tier", tier, "--out", str(out),
                  *(["--harness-results", str(harness)] if harness else [])],
                 cwd=SOURCE, env=env)
    keep(out.parent, "preclose_driver", result)
    if result.returncode:
        return {"status": f"driver exit {result.returncode}", "survivors": 0, "provenance": ""}
    try:
        return json.loads((out / "preclose.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"status": "driver wrote no preclose.json", "survivors": 0, "provenance": ""}


# --- the base-relative check (AC4) --------------------------------------------------
#
# Sticky-wicket REFORGE (harmonic-forge#1112 preclose pass 1): one fail-fast
# `mise run check` never lists every step's result, so the runner owns the
# step list and runs each step on its own. Only the test step is judged
# against the base, by test ID; every other step must pass absolutely.

#: The `check` task's steps, in order, with `ci-check` inlined: one entry per
#: step, each run on its own (`set -e` and `head` are each step's prelude).
#: `test_te_run.CheckSteps` keeps this equal to mise.toml's run lines.
CHECK_STEPS: tuple[tuple[str, str], ...] = (
    ("py_compile_tools", "find tools -name '*.py' -print0 | xargs -0 -n1 python3 -m py_compile"),
    ("py_compile_sync_rules", "python3 -m py_compile sync_rules.py"),
    ("syntax_banner", 'echo "[CHECK] Syntax-clean: tools/*.py, sync_rules.py"'),
    ("run_tests", "python3 tools/run_tests.py"),
    ("memory_lint_clean",
     "python3 tools/memory/memory_lint.py --store tools/memory/testdata/clean --gate "
     "--today 2026-09-06"),
    ("memory_lint_aging",
     "rc=0; python3 tools/memory/memory_lint.py --store tools/memory/testdata/aging --gate "
     "--today 2026-09-06 >/dev/null || rc=$?\n"
     'test "$rc" -eq 1 || { echo "memory_lint: the aging fixture must FAIL with exit 1 under '
     '--gate (got $rc)"; exit 1; }'),
    ("check_test_main_last", "python3 tools/rules/check_test_main_last.py"),
    ("check_installed_lane_sources",
     'python3 tools/lane/check_installed_lane_sources.py --bin-dir "$HOME/.local/bin" '
     "--skip-if-absent"),
    ("platform_link_report", "python3 tools/onboard/platform_link_report.py --project ."),
    ("check_cross_registry", "python3 tools/rules/check_cross_registry.py"),
    ("context_budget_ratchet", "python3 tools/memory/context_budget_ratchet.py --repo . --gate"),
    ("check_receipt", 'python3 tools/hooks/check_receipt.py record --repo-root . --head "$head"'),
)
#: The one step judged base-relatively, by test ID.
TEST_STEP = "run_tests"
#: Each step runs under mise's toolchain, never through a mise task.
STEP_PREFIX: tuple[str, ...] = ("mise", "exec", "--")
STEP_PRELUDE = "set -e\nhead=$(git rev-parse HEAD)\n"
#: Fresh per-process check trees live here (`mkdtemp`), never at a fixed path.
CHECK_TREE_ROOT = TE_RUNNER_HOME / "check-trees"

TEST_LINE = re.compile(r"^(?:FAIL|ERROR): (.+?)\s*$", re.M)
RAN_LINE = re.compile(r"^Ran (\d+) tests? in ", re.M)
#: A test ID naming one of these failed outside any one test: a module that
#: did not import, or a class or module fixture (pitch named change B).
MODULE_LEVEL = ("unittest.loader._FailedTest", "_ErrorHolder", "setUpModule", "tearDownModule",
                "setUpClass", "tearDownClass")


def parse_check(text: str) -> dict:
    """The test step's failing test IDs, module-level errors, and tests run
    (`printed_ran`: whether any `Ran N tests` line appeared)."""
    tests = [m.group(1) for m in TEST_LINE.finditer(text)]
    ran = [int(m.group(1)) for m in RAN_LINE.finditer(text)]
    return {"failures": sorted(set(tests)),
            "module_errors": sorted({t for t in tests if any(k in t for k in MODULE_LEVEL)}),
            "ran": sum(ran), "printed_ran": bool(ran)}


def run_steps(tree: Path, env: dict[str, str], evidence: Path | None, name: str) -> dict:
    """Every step in `CHECK_STEPS`, each on its own inside the oracle boundary,
    none short-circuiting another; each step's rc and output kept."""
    steps, step_failures, tests = [], [], None
    for step, script in CHECK_STEPS:
        try:
            argv = sandbox.wrap([*STEP_PREFIX, "sh", "-c", STEP_PRELUDE + script], tree,
                                inputs=(source_objects(),))
        except sandbox.SandboxError as exc:
            raise Abort(str(exc)) from exc
        result = run(argv, cwd=tree, env=oracle_env(env))
        keep(evidence, f"{name}-{step}", result)
        steps.append({"name": step, "rc": result.returncode})
        if step == TEST_STEP:
            tests = {"test_rc": result.returncode,
                     **parse_check(f"{result.stdout}\n{result.stderr}")}
        elif result.returncode:
            step_failures.append(f"{step} exited {result.returncode}")
    tests = tests or {"test_rc": 127, "failures": [], "module_errors": [], "ran": 0,
                      "printed_ran": False}
    # Complete: no step crashed (rc other than 0/1) and the test step said how
    # many tests it ran. Only a complete base run is ever cached.
    complete = all(s["rc"] in (0, 1) for s in steps) and tests["printed_ran"]
    return {"steps": steps, "step_failures": step_failures, "complete": complete, **tests}


def check_sha(sha: str, env: dict[str, str], evidence: Path | None, name: str = "check") -> dict:
    """The check on `sha`, in a fresh `--shared` clone of it in a per-process
    `mkdtemp` directory: the same function for base and head, never the
    agent's working tree (preclose survivors 1 and 6)."""
    CHECK_TREE_ROOT.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix=f"{sha[:12]}-", dir=CHECK_TREE_ROOT))
    tree = root / "tree"
    try:
        git("clone", "-q", "--shared", "--no-checkout", str(SOURCE), str(tree))
        git("checkout", "-q", "--detach", sha, cwd=tree)
        git("remote", "set-url", "origin", UPSTREAM_URL, cwd=tree)
        try:
            provision.provision_clone(tree, source_objects())  # harmonic-forge#1127
        except provision.ProvisionError as exc:
            # A step failure, never Abort: the head's is the item's own failure
            # (judge_check), the base's is fatal in preflight, and complete=False
            # keeps it out of the base cache.
            return {"sha": sha, "steps": [], "step_failures": [f"provision_clone: {exc}"],
                    "complete": False, "test_rc": 127, "failures": [],
                    "module_errors": [], "ran": 0, "printed_ran": False}
        return {"sha": sha, **run_steps(tree, env, evidence, name)}
    finally:
        shutil.rmtree(root, ignore_errors=True)


def base_check(base: str, env: dict[str, str]) -> dict:
    """The base's check, once per SHA: under an `fcntl` lock on
    BASE_CHECK_ROOT/<sha>.lock, cached as BASE_CHECK_ROOT/<sha>/result.json
    (written atomically) only when the run completed."""
    BASE_CHECK_ROOT.mkdir(parents=True, exist_ok=True)
    cache = BASE_CHECK_ROOT / base
    stored = cache / "result.json"
    with open(BASE_CHECK_ROOT / f"{base}.lock", "a", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            data = json.loads(stored.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("sha") == base and data.get("complete"):
                return data
        except (OSError, ValueError):
            pass
        cache.mkdir(parents=True, exist_ok=True)
        data = check_sha(base, env, cache)
        if data["complete"]:
            partial = cache / f".result.{os.getpid()}.tmp"
            partial.write_text(json.dumps(data, indent=1) + "\n", encoding="utf-8")
            os.replace(partial, stored)
        return data


def previous_base(base: str) -> dict | None:
    """The newest cached base other than `base`, for preflight's report."""
    found = []
    for path in BASE_CHECK_ROOT.glob("*/result.json") if BASE_CHECK_ROOT.is_dir() else []:
        if path.parent.name != base:
            try:
                found.append((path.stat().st_mtime, json.loads(path.read_text(encoding="utf-8"))))
            except (OSError, ValueError):
                continue
    return max(found, key=lambda pair: pair[0])[1] if found else None


def judge_check(head: dict, base: dict) -> tuple[int, list[str], list[str]]:
    """(check_rc, new failures, base test failures). Only the test step is
    base-relative; every other step fails the head on any non-zero exit."""
    base_failures = sorted(set(base.get("failures") or []))
    new = sorted(set(head["failures"]) - set(base_failures))
    # Change B: a module-level error on the head always counts as new.
    new += [m for m in head["module_errors"] if m not in new]
    # Change A: a test step that exits non-zero naming no failing test crashed
    # before the tests, never an empty difference.
    if head["test_rc"] and not head["failures"]:
        new.append(f"{TEST_STEP} exited {head['test_rc']} naming no failing test")
    # Sticky-wicket patch 5, the pass-1 reforge's crash rule applied to the
    # head: a test step exiting other than 0 or 1 crashed, whatever it listed.
    if head["test_rc"] not in (0, 1):
        new.append(f"test step exited {head['test_rc']}")
    if head["ran"] < (base.get("ran") or 0):
        new.append(f"head ran {head['ran']} tests, base ran {base.get('ran')}")
    # Every other step is absolute: a failure on the base never masks the head.
    new += [f"step {line}" for line in head.get("step_failures") or []]
    rcs = [s["rc"] for s in head.get("steps") or [] if s["rc"]]
    return (rcs[0] if rcs else 1) if new else 0, new, base_failures


# --- the PR ------------------------------------------------------------------------

def live_facts(pr: str, head: str, env: dict[str, str]) -> dict:
    result = run(["gh", "pr", "view", pr, "--repo", te_queue.REPO, "--json",
                  "headRefOid,statusCheckRollup"], env=env)
    try:
        data = json.loads(result.stdout) if not result.returncode else None
    except ValueError:
        data = None
    if not isinstance(data, dict):
        return {"pr_exists": False, "head_sha_matches": False, "ci": "pending"}
    states = [str(c.get("conclusion") or c.get("state") or "").upper()
              for c in data.get("statusCheckRollup") or [] if isinstance(c, dict)]
    ci = ("red" if any(s in ("FAILURE", "ERROR", "CANCELLED", "TIMED_OUT") for s in states)
          else "green" if states and all(s in ("SUCCESS", "NEUTRAL", "SKIPPED") for s in states)
          else "pending")
    return {"pr_exists": True, "head_sha_matches": data.get("headRefOid") == head, "ci": ci}


def open_pr(item: dict, state: dict, queue: Path, issue: int, head: str,
            branch: str, titles: list[str], env: dict[str, str]) -> str:
    # Pushed from SOURCE, never from the agent's clone: the clone's .git/config
    # and hooks are agent-writable and would run as the operator (#1098).
    if run(["git", "remote", "get-url", "runner-fork"], cwd=SOURCE).returncode:
        git("remote", "add", "runner-fork", FORK_URL)
    item.update({"pushed_sha": head, "branch": branch})
    save(queue, state)  # before the push: a kill after it stays visible
    pushed = run(["git", "push", "runner-fork", f"{head}:refs/heads/{branch}"], cwd=SOURCE,
                 env=env)
    if pushed.returncode:
        raise Blocked(f"push to runner-fork failed: {pushed.stderr.strip()[:200]}")
    body = (f"Unattended te-runner implementation for harmonic-forge#{issue}.\n\n"
            + "".join(f"- {title}\n" for title in titles)
            + "\nProposed for the operator's review; the runner never merges.\n")
    created = run(["gh", "pr", "create", "--repo", te_queue.REPO, "--base", "main",
                   "--head", f"{RUNNER_LOGIN}:{branch}",
                   "--title", f"te-runner: {titles[0] if titles else 'implementation'} "
                              f"(harmonic-forge#{issue})", "--body", body], env=env)
    url = created.stdout.strip().splitlines()[-1] if created.stdout.strip() else ""
    if created.returncode or not url.startswith("https://"):
        raise Blocked(f"pushed branch {branch} at {head[:12]}, but gh pr create failed: "
                      f"{created.stderr.strip()[:150]}")
    return url


# --- one item ----------------------------------------------------------------------

def ac_titles(body: str) -> list[str]:
    """One short title per criterion, read through the shared matcher (AC11)."""
    return [re.split(r"(?<=[.:])\s", item, maxsplit=1)[0][:80]
            for item in acceptance.items(acceptance.section(body))]


def self_flag(result: dict | None) -> str:
    text = (result or {}).get("result") or ""
    flags = [line.split(SELF_FLAG, 1)[1].strip() for line in str(text).splitlines()
             if SELF_FLAG in line]
    return flags[-1] if flags else ""


def emit_event(issue: int, head: str, extra: dict, cost: object) -> None:
    import emit  # noqa: PLC0415 -- telemetry is best-effort and imported late
    try:
        emit.emit([{"ts": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "source": "te-runner", "repo": te_queue.REPO, "issue": issue,
                    "sha": head or None, "account": "vitalharmony", "org": "vitalharmony",
                    "actor": "unset", "event_type": "te-runner.item", "subject_kind": "te-issue",
                    "subject_id": f"{te_queue.REPO}#{issue}@{(head or 'none')[:12]}",
                    "provenance": "manual",
                    "attrs": {"reason": extra.get("reason") or "green",
                              "state": (extra.get("verdict") or {}).get("state", "blocked"),
                              "total_cost_usd": cost if isinstance(cost, (int, float)) else -1,
                              "permission_denials": (extra.get("facts") or {}).get(
                                  "permission_denials", -1)}}])
    except Exception as exc:  # noqa: BLE001
        log("telemetry", f"not written: {exc}")


# --- the per-item guards (harmonic-forge#1112 sticky-wicket patch 4) ---------------
#
# Every guard work_item checks before it spends, as one list: work_item calls
# `item_preconditions` first and the preflight (`preflight.item_failures`) calls
# the same function per queued item, so a guard added here is a preflight
# failure with no change to the preflight. Each guard raises Blocked.

def _guard_reuse(issue: int, env: dict[str, str] | None, source: Path) -> None:
    # Every trace an earlier run of this issue leaves counts: its checkout, a
    # local branch, the pinned head ref. A re-queued issue whose clone was
    # deleted must not run the agent again.
    branch = f"fix/{issue}-te-runner"
    clone = WORKTREE_ROOT / f"harmonic-forge-{issue}r-impl"
    if clone.exists() or any(not run(["git", "rev-parse", "--verify", "--quiet", ref],
                                     cwd=source).returncode
                             for ref in (f"refs/heads/{branch}", f"refs/te-runner/{issue}")):
        raise Blocked(f"branch {branch}, refs/te-runner/{issue} or checkout {clone} "
                      "already exists; never reused")


def _guard_fork_branch(issue: int, env: dict[str, str] | None, source: Path) -> None:
    branch = f"fix/{issue}-te-runner"
    remote = run(["git", "ls-remote", "--heads", FORK_URL, f"refs/heads/{branch}"],
                 cwd=source, env=env)
    if remote.returncode:
        raise Blocked(f"cannot read the runner fork for {branch}: {remote.stderr.strip()[:150]}")
    if remote.stdout.strip():
        raise Blocked(f"branch {branch} already exists on the runner fork; never reused")


def _guard_bundle_pin(issue: int, env: dict[str, str] | None, source: Path) -> None:
    if not te_queue.DIGEST_RE.search(te_queue.handoff(issue) or ""):
        raise Blocked("the handoff pins no kill-bundle-sha256")


def _guard_origin_fetch(issue: int, env: dict[str, str] | None, source: Path) -> None:
    # work_item bases the item on a fresh origin/main; a fetch that cannot run
    # is a refusal before spend, never a mid-item stop (post-verdict gap 3).
    fetched = run(["git", "fetch", "-q", "origin", "main"], cwd=source, env=env)
    if fetched.returncode:
        raise preclose_driver.DriverError(
            f"git fetch origin main failed: {fetched.stderr.strip()[:200]}")


ITEM_GUARDS = (_guard_reuse, _guard_fork_branch, _guard_bundle_pin, _guard_origin_fetch)


def item_preconditions(issue: int, env: dict[str, str] | None = None,
                       source: Path | None = None) -> list[str]:
    """Every guard in ITEM_GUARDS, each refusal collected, never only the first."""
    problems = []
    for guard in ITEM_GUARDS:
        try:
            guard(issue, env, Path(source or SOURCE))
        except (Blocked, preclose_driver.DriverError, te_queue.QueueError) as exc:
            # QueueError: _guard_bundle_pin reads the handoff (cross-family
            # finding 2); one guard's refusal never hides another's.
            problems.append(str(exc))
    return problems


def work_item(item: dict, state: dict, queue: Path, budget: str, env: dict[str, str],
              agent_env: dict[str, str], run_dir: Path) -> dict:
    issue = te_queue.issue_number(item["id"])
    failure = te_queue.first_failure(issue)
    body = te_queue.gh_json(f"repos/{te_queue.REPO}/issues/{issue}").get("body") or ""
    titles = ac_titles(body)
    if failure:
        facts = {"eligible": failure[0] != "ineligible",
                 "deny_hits": [failure[1]] if failure[0] == "deny-path" else []}
        return {"facts": facts, "verdict": verdict.reduce(facts), "ac_titles": titles}
    problems = item_preconditions(issue, env)
    if problems:
        raise Blocked("; ".join(problems))
    branch = f"fix/{issue}-te-runner"
    clone = WORKTREE_ROOT / f"harmonic-forge-{issue}r-impl"
    git("fetch", "-q", "origin", "main")
    base = git("rev-parse", "origin/main")
    # A standalone clone, not a linked worktree: its .git lives inside the
    # boundary's one writable directory, so the agent can commit (#1098).
    git("clone", "-q", "--shared", "--no-checkout", str(SOURCE), str(clone))
    git("checkout", "-q", "-b", branch, base, cwd=clone)
    git("remote", "set-url", "origin", UPSTREAM_URL, cwd=clone)  # AC3
    try:
        provision.provision_clone(clone, source_objects())  # harmonic-forge#1127
    except provision.ProvisionError as exc:
        # The fixed clone path Blocks every later run while it exists, and no
        # agent work exists yet.
        shutil.rmtree(clone, ignore_errors=True)
        raise Blocked(f"cannot provision {clone}: {exc}") from exc
    log("clone", f"{clone} on {branch} from {base[:12]}")
    # Kept after the item (AC7): never a temporary directory.
    scratch = run_dir / str(issue)
    scratch.mkdir(parents=True, exist_ok=True)
    handoff = te_queue.handoff(issue) or ""
    pinned = te_queue.DIGEST_RE.search(handoff)
    if not pinned:  # item_preconditions checked it; the handoff changed since
        raise Blocked("the handoff pins no kill-bundle-sha256")
    pin = pinned.group(1)
    bundle = snapshot_bundle(issue, pin, scratch)  # before the agent can touch it
    parsed = run_agent(clone, agent_prompt(issue, body, handoff),
                       budget, agent_env, scratch / "stream.jsonl", scratch / "transcripts")
    result = parsed["result"]
    # The head is read through SOURCE, never the clone's own git, and pinned
    # by a ref so a gc in SOURCE cannot prune it before the push (#1098).
    git("fetch", "-q", str(clone), "HEAD")
    head = git("rev-parse", "FETCH_HEAD")
    git("update-ref", f"refs/te-runner/{issue}", head)
    log("agent", f"head {head[:12]}, cost {result.get('total_cost_usd')}")
    # A fresh clone of the pinned head, never the agent's tree (survivor 6).
    head_check = check_sha(head, env, scratch)
    base_result = base_check(base, env)
    problems = preflight.base_problems(base_result)
    if base_result.get("step_failures"):  # a non-test step failing on the base is fatal
        raise Abort("; ".join(problems))
    check_rc, new_failures, base_failures = judge_check(head_check, base_result)
    log("check", f"{len(new_failures)} new failure(s), {len(base_failures)} test failure(s) "
                 "on the base")
    kill = kill_facts(issue, base, head, env, bundle, pin, scratch)
    # harmonic-forge#1147 AC6: a failed receipt keeps the facts it judged, so
    # the report names each check; the reducer reads the receipt on its own.
    receipt_ok = bool(kill) and kill_receipt(issue, base, head, env, scratch, bundle)
    log("kill", f"{len(kill)} check(s), receipt {'ok' if receipt_ok else 'failed'}")
    hits = deny_hits(base, head)
    log("deny", f"{len(hits)} hit(s)")
    # The harness's own results, for the cross-family brief's evidence (AC14).
    harness = scratch / "harness-results.json"
    harness.write_text(json.dumps({"kill": kill, "kill_receipt": receipt_ok,
                                   "deny_hits": hits, "check": {
        "check_rc": check_rc, "new_failures": new_failures, "base_failures": base_failures}},
        indent=1) + "\n", encoding="utf-8")
    # Sticky-wicket patch 6: once the check, the deny check or the kill receipt
    # failed the item cannot go green, so no paid refuter or cross-family call
    # runs; the preclose fact says why, and the reducer still lists every
    # failed clause (AC8).
    if check_rc or hits or not kill or not receipt_ok:
        preclose = {"status": "skipped: earlier clause failed", "survivors": 0, "provenance": ""}
    else:
        preclose = preclose_fact(issue, base, head, te_queue.tier(issue) or "standard", env,
                                 scratch / "preclose", harness)
    log("preclose", preclose.get("status", "?"))
    summary_path = scratch / "preclose" / "summary.md"
    summary = summary_path.read_text(encoding="utf-8") if summary_path.is_file() else ""
    facts = {"eligible": True, "deny_hits": hits,
             "permission_denials": len(result.get("permission_denials") or []),
             "budget_exceeded": result.get("subtype") == "error_max_budget_usd",
             "self_flag": self_flag(result), "check_rc": check_rc,
             "check_new_failures": new_failures, "check_base_failures": base_failures,
             "kill": kill, "kill_receipt": receipt_ok,
             "preclose": preclose,
             # Every clause but the PR's: a branch is pushed only on green.
             "live": {"pr_exists": True, "head_sha_matches": True, "ci": "pending"}}
    outcome = verdict.reduce(facts)
    extra = {"head_sha": head, "branch": branch,
             "diff_stat": (git("diff", "--stat", base, head).splitlines() or [""])[-1].strip(),
             "ac_titles": titles, "total_cost_usd": result.get("total_cost_usd")}
    if outcome["state"] == "green":
        pr = open_pr(item, state, queue, issue, head, branch, titles, env)
        extra["pr"] = pr
        facts["live"] = live_facts(pr, head, env)
        extra["live_read_at"] = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        outcome = verdict.reduce(facts)
        if summary:
            posted = run(["gh", "pr", "comment", pr, "--repo", te_queue.REPO, "--body",
                          summary], env=env)
            url = posted.stdout.strip().splitlines()[-1] if posted.stdout.strip() else ""
            # The report links the summary (harmonic-forge#1056 AC3).
            extra["preclose_summary"] = url if url.startswith("https://") else ""
    return {**extra, "facts": facts, "verdict": outcome}


def run_queue(queue: Path, budget: str, max_steps: int) -> int:
    with te_queue.queue_lock(queue):
        return _run_queue(queue, budget, max_steps)


def set_status(item: dict, status: str, run_id: str, **fields: object) -> None:
    """Every status change goes through here, so every item this run touches
    carries the run's id and the report can scope to it (harmonic-forge#1056
    sticky-wicket PATCH: stamping by hand at each write site missed some)."""
    item.update({**fields, "status": status, "run_id": run_id})


def _run_queue(queue: Path, budget: str, max_steps: int) -> int:
    state = queue_state.load(queue)
    # The run's id first, saved at once: a run that ends before any item (a
    # boundary failure, an empty queue) must still replace the last run's id,
    # or the report would re-propose that run's work.
    run_id = new_run_id()
    state["run_id"] = run_id
    # The unit's invocation beside the run id: ExecStopPost's report checks it,
    # so a later run that ends before this point never re-reports this one
    # (harmonic-forge#1112 sticky-wicket patch 2).
    state["invocation_id"] = os.environ.get("INVOCATION_ID", "")
    before = {item["id"]: (item["status"], item["note"]) for item in state["items"]}
    reconcile(state)  # queue_state.recover is imported unmodified, so its changes are
    for item in state["items"]:  # detected and stamped here rather than inside it
        if (item["status"], item["note"]) != before[item["id"]]:
            set_status(item, item["status"], run_id)
    save(queue, state)
    run_dir = RUNS_ROOT / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    prune_runs(RUNS_ROOT)
    problems = deny_check.boundary()
    if problems:
        log("boundary", "; ".join(problems))
        return 3
    env = preclose_driver.runner_env()
    token = runner_token()
    if token is None:
        return 3
    # Only the agent (and the probe) holds the runner token; the shared env
    # feeds the oracle, kill checks, preclose and the PR, and stays token-free.
    agent_env = {**env, "GH_TOKEN": token}
    if not probe(agent_env):
        return 3
    if run_preflight(state, run_dir, env, agent_env)["failures"]:
        return 3
    blocked = False
    # harmonic-forge#1147 AC2a: an unchanged item is passed over before
    # work_item, whose first_failure path would rewrite its reason.
    # The item stays `pending`, so a later fix re-arms it with no queue edit.
    passed_over = set()
    for pending in [i for i in state["items"] if i.get("status") == "pending"]:
        try:
            skipped = te_queue.unchanged(pending)
        except te_queue.QueueError as exc:  # undecidable: pass over this item only
            skipped = f"undecidable ({exc}); passed over this run"
        if skipped:
            passed_over.add(pending["id"])
            log("item", f"{pending['id']} {skipped}")
    worked = 0  # a passed-over item uses no step; each one joins passed_over
    while worked < max_steps:
        item = next((i for i in state["items"]
                     if i.get("status") == "pending" and i["id"] not in passed_over), None)
        if item is None:
            break
        # The dependencies this run works against, read before it starts
        # (sticky-wicket PATCH): a failed read passes the item over, pending.
        try:
            snapshot = te_queue.outcome_record(te_queue.issue_number(item["id"]), run_id, None)
        except te_queue.QueueError as exc:
            passed_over.add(item["id"])
            log("item", f"{item['id']} undecidable ({exc}); passed over this run")
            continue
        set_status(item, "in_progress", run_id)
        worked += 1
        save(queue, state)
        log("item", f"{item['id']} in progress")
        try:
            extra = work_item(item, state, queue, budget, env, agent_env, run_dir)
        except Exception as exc:  # noqa: BLE001 -- nothing may leave an item in_progress
            set_status(item, "blocked", run_id, note=str(exc))
            save(queue, state)
            emit_event(te_queue.issue_number(item["id"]), item.get("head_sha", ""),
                       {"reason": "blocked"}, None)
            log("item", f"{item['id']} blocked: {exc}")
            if not isinstance(exc, Blocked):  # Abort, or anything unexpected: stop the run
                return 3
            blocked = True
            continue
        outcome = extra["verdict"]
        set_status(item, "done", run_id, **extra, reason=outcome.get("reason") or "",
                   note=outcome.get("detail", ""))
        if outcome.get("reason"):  # AC1: what this set-aside depended on
            item["last_outcome"] = {**snapshot, "reason": outcome["reason"]}
        else:  # green: a stale set-aside record must not gate it later
            item.pop("last_outcome", None)
        for key, default in DONE_KEYS.items():  # never overwrites pushed_sha set before a push
            item.setdefault(key, default)
        save(queue, state)
        emit_event(te_queue.issue_number(item["id"]), extra.get("head_sha", ""), item,
                   extra.get("total_cost_usd"))
        log("item", f"{item['id']} done: {outcome['state']} {outcome.get('reason') or ''}")
    return 3 if blocked else 0


def run_preflight(state: dict, run_dir: Path, env: dict[str, str],
                  agent_env: dict[str, str]) -> dict:
    """AC9's checks, AC12's per-item preconditions and AC15's rehearsal; every
    one runs and every failure is logged."""
    scratch = run_dir / "preflight"
    shutil.rmtree(scratch, ignore_errors=True)
    scratch.mkdir(parents=True)
    # A failed fetch is one refusal among the others, never an abort before
    # every check has run and reported (cross-family finding 1).
    fetched = run(["git", "fetch", "-q", "origin", "main"], cwd=SOURCE)
    fetch_problems = ([f"git fetch origin main failed: {fetched.stderr.strip()[:200]}"]
                      if fetched.returncode else [])
    resolved = run(["git", "rev-parse", "--verify", "origin/main^{commit}"], cwd=SOURCE)
    base = "" if resolved.returncode else resolved.stdout.strip()
    issues = preflight.pending_issues(state)

    def permissions() -> list[str]:
        clone = scratch / "probe-clone"
        git("clone", "-q", "--shared", "--no-checkout", str(SOURCE), str(clone))
        git("checkout", "-q", "--detach", base, cwd=clone)
        git("remote", "set-url", "origin", UPSTREAM_URL, cwd=clone)
        provision.provision_clone(clone, source_objects())  # harmonic-forge#1127
        commands = preflight.verification_commands(issues) + list(preflight.PROBE_COMMANDS)
        return preflight.permission_probe(commands, clone, scratch / "probe-transcripts",
                                          agent_env, SETTINGS, (source_objects(),))

    current: dict = {}

    def base_run() -> list[str]:
        current.update(base_check(base, env))
        return preflight.base_problems(current)

    record = preflight.run_all(
        [("origin-fetch", lambda: fetch_problems),
         ("permission-probe", permissions), ("base-check", base_run),
         ("kill-baselines", lambda: preflight.kill_baselines(issues, SOURCE, base, scratch)),
         ("codex", lambda: preflight.codex_parity(scratch, env)),
         ("rehearsal", lambda: preflight.rehearsal(scratch, env)),
         ("per-item", lambda: [line for issue in issues for line in preflight.item_failures(
             issue, SOURCE, base, scratch / f"item-{issue}", env)])],
        [("base-regression", lambda: preflight.base_regression(current, previous_base(base)))],
        run_dir)
    for line in record["reported"]:
        log("preflight", f"reported: {line}")
    for line in record["failures"]:
        log("preflight", line)
    return record


def preflight_only(queue: Path) -> int:
    state = queue_state.load(queue)
    run_dir = RUNS_ROOT / new_run_id()
    run_dir.mkdir(parents=True, exist_ok=True)
    prune_runs(RUNS_ROOT)
    env = preclose_driver.runner_env()
    token = runner_token()
    if token is None:
        return 3
    record = run_preflight(state, run_dir, env, {**env, "GH_TOKEN": token})
    print(run_dir / "preflight.json")
    return 3 if record["failures"] else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    worked = sub.add_parser("run")
    worked.add_argument("--queue", required=True)
    worked.add_argument("--max-budget-usd", required=True)
    worked.add_argument("--max-steps", type=int, default=5)
    sub.add_parser("preflight").add_argument("--queue", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "preflight":
            return preflight_only(Path(args.queue))
        if not (math.isfinite(float(args.max_budget_usd)) and float(args.max_budget_usd) > 0) \
                or args.max_steps <= 0:
            raise ValueError("--max-budget-usd must be a finite positive number and "
                             "--max-steps positive")
        return run_queue(Path(args.queue), args.max_budget_usd, args.max_steps)
    except (ValueError, OSError, te_queue.QueueError, preclose_driver.DriverError,
            deny_check.DenyError) as exc:
        log("error", str(exc))
        return 2


if __name__ == "__main__":
    sys.exit(main())
