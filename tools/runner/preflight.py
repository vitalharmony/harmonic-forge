#!/usr/bin/env python3
"""The te-runner's preflight (harmonic-forge#1112 AC9).

Runs before the first item (`te_run.py run`) and on its own
(`te_run.py preflight --queue FILE`). Five checks, each independent: every one
runs and every failure is listed, never only the first, so one run surfaces
every stage that cannot work before any agent spend.

a. permission probe: `claude -p` with the agent's settings, inside the agent's
   boundary, on a cheap model with a budget of at most $0.10, runs each queued
   item's verification commands plus `PROBE_COMMANDS`, launched exactly as
   the agent is (`te_run.launch_base`: no discovered settings files, the
   settings' own `hooks` block only, never `--bare`; harmonic-forge#1195).
   `SENTINEL_COMMAND` must be
   denied, in `permission_denials` and by a `PreToolUse` `hook_response`
   event: hook presence alone is not the evidence. Any other denial fails,
   and so does a command the probe never attempted (undecidable is a
   failure), a non-zero claude exit, a result event with `is_error` true, or
   a result subtype other than `success`.
b. base check: the runner's per-step check (`te_run.CHECK_STEPS`) on the base.
   Any step other than the test step failing on the base is fatal, and so is
   a base run that crashed (`base_problems`): the head is judged absolutely on
   those steps, so a failing base can never mask one. A non-empty base test
   failure set where the previous cached base's was empty is reported, not
   fatal.
c. kill-bundle baselines: every queued item's bundle checks run on the base in
   the oracle boundary and must each fail with a test failure. A pass, an
   import or collection error, or an output naming no test failure is fatal.
d. reviewer codex: `codex --version` inside the reviewer boundary must equal
   the operator's `~/.npm-global/bin/codex --version`.
e. per item (AC12): `item_failures`, the pipeline's own read-only
   preconditions: `te_queue` eligibility, the kill-bundle snapshot and pin
   check, `acceptance.acceptance_criteria` (an issue body with no acceptance
   section, or one listing no criteria, is refused before spend) and
   `te_run.item_preconditions`.

`preflight.json` in the run dir records every check's outcome.
"""
from __future__ import annotations

import json
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import acceptance  # noqa: E402
import deny_check  # noqa: E402
import sandbox  # noqa: E402
import te_queue  # noqa: E402

kill_runner = te_queue.kill_runner

PROBE_MODEL = "claude-haiku-5-5"
PROBE_BUDGET = "0.10"
PROBE_TIMEOUT = 900
#: AC5's commands, one concrete call each, plus `mise run check --help`.
PROBE_COMMANDS = (
    "mkdir -p preflight-probe",
    "touch preflight-probe/a",
    "cp preflight-probe/a preflight-probe/b",
    "mv preflight-probe/b preflight-probe/c",
    "rm preflight-probe/c",
    "git rev-parse HEAD",
    "git branch --show-current",
    "git stash list",
    "git checkout -b preflight-probe-1",
    "git switch -c preflight-probe-2",
    "git restore --staged .",
    "git reset -q",
    "git add preflight-probe/a",
    "git mv preflight-probe/a preflight-probe/d",
    "git rm -q --cached preflight-probe/d",
    "mise tasks ls",
    "mise run check --help",
)
#: Denied by te-runner.settings.json's own PreToolUse hook, never by its
#: permissions (harmonic-forge#1195): the probe must see it denied.
SENTINEL_COMMAND = deny_check.SENTINEL_COMMAND
OPERATOR_CODEX = sandbox.HOME / ".npm-global" / "bin" / "codex"
#: An output carrying any of these failed before its tests could run.
COLLECTION_ERRORS = ("ModuleNotFoundError", "ImportError", "SyntaxError",
                     "unittest.loader._FailedTest", "ERROR collecting", "NO TESTS RAN",
                     "\nRan 0 tests")
#: An output carrying one of these ran a test that failed.
TEST_FAILURE = re.compile(r"^FAIL: |AssertionError|^FAILED ", re.M)


def _run(argv: list[str], *, cwd: Path, env: dict[str, str] | None = None,
         stdin: str | None = None, timeout: int = PROBE_TIMEOUT) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(argv, cwd=cwd, env=env, input=stdin, capture_output=True,
                              text=True, errors="replace", timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(argv, 127, "", f"{argv[0]}: {exc}")


def pending_issues(state: dict) -> list[int]:
    return [te_queue.issue_number(item["id"]) for item in state["items"]
            if item.get("status") == "pending"]


def verification_commands(issues: list[int]) -> list[str]:
    """Each queued item's kill-bundle test commands, as shell text."""
    commands = []
    for issue in issues:
        for check in kill_runner.load_checks(te_queue.bundle_dir(issue)):
            commands.append(shlex.join(check["test"]))
    return list(dict.fromkeys(commands))


def probe_prompt(commands: list[str]) -> str:
    listed = "\n".join(f"{n}. `{c}`" for n, c in enumerate(commands, 1))
    return ("This is a permission probe. Run each command below with the Bash tool, exactly as "
            "written, one tool call per command, in order. Do not run anything else, do not fix "
            "or retry a failure, and do not edit files. Then reply `done`.\n\n" + listed + "\n")


def parse_probe(stdout: str) -> tuple[list[str], list[str], dict | None]:
    """(attempted Bash commands, denied commands, result event)."""
    attempted, denied, result, _hooks = parse_probe_hooks(stdout)
    return attempted, denied, result


def parse_probe_hooks(stdout: str) -> tuple[list[str], list[str], dict | None, list[dict]]:
    """`parse_probe` plus every `PreToolUse` `hook_response` event."""
    attempted, denied, result, hooks = [], [], None, []
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "assistant":
            for part in (event.get("message") or {}).get("content") or []:
                if isinstance(part, dict) and part.get("type") == "tool_use" \
                        and part.get("name") == "Bash":
                    attempted.append(str((part.get("input") or {}).get("command", "")))
        elif event.get("type") == "result":
            result = event
        elif (event.get("type") == "system" and event.get("subtype") == "hook_response"
              and (event.get("hook_event") or event.get("hook_name")) == "PreToolUse"):
            hooks.append(event)
    for denial in (result or {}).get("permission_denials") or []:
        tool_input = denial.get("tool_input") if isinstance(denial, dict) else None
        denied.append(str((tool_input or {}).get("command") or denial))
    return attempted, denied, result, hooks


def permission_probe(commands: list[str], clone: Path, transcripts: Path, agent_env: dict,
                     settings: Path, inputs: tuple[Path, ...]) -> list[str]:
    import te_run  # noqa: PLC0415 -- te_run imports this module
    argv = te_run.launch_base(settings) + ["--model", PROBE_MODEL,
                                           "--max-budget-usd", PROBE_BUDGET]
    wrapped = sandbox.wrap(argv, clone, transcripts, profile="agent", inputs=inputs)
    commands = [c for c in commands if c != SENTINEL_COMMAND] + [SENTINEL_COMMAND]
    done = _run(wrapped, cwd=clone, env=agent_env, stdin=probe_prompt(commands))
    attempted, denied, result, hooks = parse_probe_hooks(done.stdout)
    if result is None:
        return [f"permission probe: no result event (exit {done.returncode}): "
                f"{done.stderr.strip()[-200:]}"]
    # Preclose survivor 5: a probe that attempted every command but failed
    # (non-zero exit, is_error, a non-success subtype) proves nothing ran.
    failures = []
    if done.returncode:
        failures.append(f"permission probe: claude exited {done.returncode}")
    if result.get("is_error"):
        failures.append("permission probe: the result event has is_error true")
    if result.get("subtype") != "success":
        failures.append(f"permission probe: result subtype {result.get('subtype')!r}, "
                        "not 'success'")
    if SENTINEL_COMMAND not in denied:
        failures.append(f"permission probe: the sentinel `{SENTINEL_COMMAND}` was not denied "
                        "(the settings' PreToolUse hook did not enforce in the agent's "
                        "launch mode)")
    if not any("deny" in json.dumps(event) for event in hooks):
        failures.append("permission probe: no PreToolUse hook_response event carries the "
                        "sentinel's deny")
    failures += [f"permission probe: denied `{command}`" for command in denied
                 if command != SENTINEL_COMMAND]
    failures += [f"permission probe: never attempted `{command}`" for command in commands
                 if command not in attempted]
    return failures


def base_problems(base: dict) -> list[str]:
    """Fatal (sticky-wicket REFORGE, harmonic-forge#1112): every non-test step
    that failed on the base, and a base run that did not complete."""
    sha = str(base.get("sha", "?"))[:12]
    found = [f"base {sha}: {line}" for line in base.get("step_failures") or []]
    if not base.get("complete"):
        found.append(f"base {sha}: the check did not complete (a step crashed or the test "
                     "step printed no `Ran` line); nothing was cached")
    return found


def base_regression(current: dict, previous: dict | None) -> list[str]:
    """Reported, never fatal: a base that newly fails where the last did not."""
    if previous is None or previous.get("failures") or not current.get("failures"):
        return []
    return [f"base {current.get('sha', '?')[:12]} fails {len(current['failures'])} check(s) where "
            f"base {previous.get('sha', '?')[:12]} failed none: "
            + "; ".join(current["failures"][:5])]


def classify_baseline(rc: int, output: str) -> str | None:
    """None when a check failed on the base with a test failure; else why not."""
    if rc == 0:
        return "passes on the base"
    if any(marker in output for marker in COLLECTION_ERRORS):
        return f"import or collection error on the base (broken-test, exit {rc})"
    if not TEST_FAILURE.search(output):
        return f"no test failure recognized on the base (exit {rc})"
    return None


def kill_baselines(issues: list[int], source: Path, base: str, scratch: Path,
                   repo: str = te_queue.REPO, timeout: int = 300) -> list[str]:
    failures = []
    for issue in issues:
        try:
            bundle = te_queue.bundle_dir(issue)
            checks = kill_runner.load_checks(bundle)
            files = kill_runner.bundle_files(bundle)
        except (OSError, ValueError, kill_runner.RunnerError) as exc:
            failures.append(f"kill bundle F{issue}: unreadable: {exc}")
            continue
        for number, check in enumerate(checks):
            tree = scratch / f"kill-{issue}-{number}"
            shutil.rmtree(tree, ignore_errors=True)
            try:
                kill_runner.materialize(source, base, tree)
                kill_runner.overlay(bundle, tree, files)
                argv = sandbox.wrap(check["test"], tree)
            except (OSError, kill_runner.RunnerError, sandbox.SandboxError) as exc:
                failures.append(f"kill bundle F{issue} {check['ac']}: cannot run on the base: {exc}")
                continue
            done = _run(argv, cwd=tree, env=kill_runner.test_env(repo, tree), timeout=timeout)
            shutil.rmtree(tree, ignore_errors=True)
            why = classify_baseline(done.returncode, done.stdout + "\n" + done.stderr)
            if why:
                failures.append(f"kill bundle F{issue} {check['ac']}: {why}")
    return failures


def codex_parity(scratch: Path, env: dict) -> list[str]:
    inside = _run(sandbox.wrap(["codex", "--version"], scratch, profile="reviewer"),
                  cwd=scratch, env=env, timeout=120)
    operator = _run([str(OPERATOR_CODEX), "--version"], cwd=scratch, env=env, timeout=120)
    if inside.returncode or operator.returncode:
        return [f"codex: --version failed (reviewer boundary exit {inside.returncode}, "
                f"operator exit {operator.returncode})"]
    if inside.stdout.strip() != operator.stdout.strip():
        return [f"codex: reviewer boundary runs {inside.stdout.strip()!r}, the operator's "
                f"{OPERATOR_CODEX} is {operator.stdout.strip()!r}"]
    return []


def item_failures(issue: int, source: Path, base: str, scratch: Path, env: dict) -> list[str]:
    """Every read-only per-item precondition the pipeline checks before it
    spends (harmonic-forge#1112 AC12), by calling the pipeline's own functions:
    `te_queue` eligibility, the kill-bundle snapshot and pin check,
    `acceptance.acceptance_criteria` and `te_run.item_preconditions`
    (work_item's own guards). Every refusal is listed, each
    prefixed with the item; a raising check is a refusal, never a stop.
    `te_queue.py add` runs this too (AC15)."""
    failures: list[str] = []
    scratch.mkdir(parents=True, exist_ok=True)

    def eligibility() -> list[str]:
        failure = te_queue.first_failure(issue)
        return [f"{failure[0]}: {failure[1]}"] if failure else []

    def bundle() -> list[str]:
        te_queue.snapshot_bundle(issue, te_queue.bundle_pin(issue), scratch / "kill")
        return []

    def criteria() -> list[str]:
        acceptance.acceptance_criteria(te_queue.REPO, issue, env)  # raises on a refusal
        return []

    def guards() -> list[str]:
        import te_run  # noqa: PLC0415 -- te_run imports this module
        return te_run.item_preconditions(issue, env, source)

    for name, check in (("eligibility", eligibility), ("kill bundle", bundle),
                        ("acceptance", criteria), ("item preconditions", guards)):
        try:
            found = check()
        except Exception as exc:  # noqa: BLE001 -- one check never hides another
            found = [str(exc)]
        failures += [f"F{issue} {name}: {line}" for line in found]
    return failures


def run_all(checks: list[tuple[str, Callable[[], list[str]]]],
            reported: list[tuple[str, Callable[[], list[str]]]], out: Path) -> dict:
    """Every check runs; a raising check is a failure of its own, never a stop."""
    record: dict = {"failures": [], "reported": [], "checks": {}}
    for kind, group in (("failures", checks), ("reported", reported)):
        for name, check in group:
            try:
                found = list(check())
            except Exception as exc:  # noqa: BLE001 -- one check never hides another
                found = [f"{name}: could not run: {exc}"]
            record["checks"][name] = {"ok": not found, "findings": found,
                                      "fatal": kind == "failures"}
            record[kind] += found
    out.mkdir(parents=True, exist_ok=True)
    (out / "preflight.json").write_text(json.dumps(record, indent=1) + "\n", encoding="utf-8")
    return record
