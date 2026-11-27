#!/usr/bin/env python3
"""Crash contract for every keyed PreToolUse hook (harmonic-forge#1608 P4).

A PreToolUse handler registered with `"onFailure": "block"` turns any hook
failure into a refusal: a timeout, an exit other than 0 or 2, a start failure
or stdout that is not JSON (code.claude.com/docs/en/hooks § "Block the action
when a hook fails"). So a hook that crashes or stalls on a benign call locks
the session out of that tool. This test runs every keyed hook against benign
and shell-hostile payloads and asserts the contract the CLI enforces:

  * exit code 0 or 2;
  * stdout empty or one parseable JSON value;
  * wall time under half the handler's declared timeout (60 s when unset).

The hook list is read from the three tracked registration files, never
written down here, so a hook added to any of them is covered the day it is
registered. Each script runs directly (`python3 <script>`), not through its
registered command string, so a `|| true` guard cannot hide a crash from the
test (harmonic-forge#1613 owns that guard's own failure path).

Every run gets a scratch HOME and no GitHub credentials, so nothing here
reads or writes the operator's real state or the network's.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
#: The three tracked registration files (the same set test_lane_hookset pins).
REGISTRATION_FILES = (
    ROOT / ".claude" / "settings.json",
    ROOT / "tools" / "lane" / "lane_hooks.json",
    ROOT / "tools" / "lane" / "lane_hooks.operator.json",
)
#: The script a handler runs, in either root spelling the files use.
SCRIPT_RE = re.compile(r'(?:@FORGE_ROOT@|\$\{HOME\}/harmonic-forge)/(tools/[\w./-]+?\.py)"')
#: The CLI's default command-hook timeout, when a handler declares none.
DEFAULT_TIMEOUT = 60
LANES = (None, "1", "2", "3")

#: Every tool name a keyed matcher can name; a script is run for each its
#: matchers accept.
TOOLS = ("Bash", "Read", "Edit", "Write", "MultiEdit", "NotebookEdit", "TaskCreate", "TaskUpdate",
         "SendMessage", "Monitor", "CronCreate", "Skill", "ScheduleWakeup", "RemoteTrigger")

_BIG = "echo " + "x" * 200_000
_UNKNOWN_HOME = "~nosuchuser-1608/x"

#: Bash commands: benign, then shell-hostile. Never executed, only inspected.
BASH_COMMANDS = (
    "ls",
    "git status",
    "gh issue view 12 --repo vitalharmony/harmonic-forge",
    "git commit -m $'Don\\'t fail open'",          # shlex: No closing quotation
    "cat <<\\EOF\nhello\nEOF",
    "cat <<'EOF' > /tmp/x\nit's here $(x) `y`\nEOF",
    "cd '~nosuchuser-1608' 2>/dev/null || pwd",
    f"gh pr create --body-file {_UNKNOWN_HOME}",
    f"gh pr create --body-file={_UNKNOWN_HOME}",
    f"gh issue comment 1 --repo vitalharmony/harmonic-forge --body-file {_UNKNOWN_HOME}",
    f"gh pr comment 1 --repo vitalharmony/harmonic-forge --body-file={_UNKNOWN_HOME}",
    f"cd {_UNKNOWN_HOME} && mypy .",
    "echo 'café ☃ \U0001f600' — done",
    "echo 'unterminated",
    _BIG,
    "",
)
FILE_PATHS = ("/tmp/hook-crash-contract.py", _UNKNOWN_HOME, "relative/x.py", "")


def _payloads(tool: str) -> list[dict]:
    """The corpus for one tool name, including a payload with keys missing."""
    if tool in ("Bash", "Monitor"):
        inputs = [{"command": c} for c in BASH_COMMANDS]
    elif tool in ("Read", "Write", "Edit", "MultiEdit"):
        inputs = [{"file_path": p, "content": "x", "old_string": "a", "new_string": "b☃",
                   "edits": [{"old_string": "a", "new_string": "b"}]} for p in FILE_PATHS]
    elif tool == "NotebookEdit":
        inputs = [{"notebook_path": p, "new_source": "x"} for p in FILE_PATHS]
    elif tool == "TaskCreate":
        inputs = [{"subject": "x", "description": "y"}, {"subject": "H1608 it's ☃"}]
    elif tool == "TaskUpdate":
        inputs = [{"taskId": "1", "status": "completed"}, {"taskId": "", "status": "deleted"}]
    elif tool == "SendMessage":
        inputs = [{"to": "x", "message": "y"}]
    elif tool == "CronCreate":
        inputs = [{"cron": "* * * * *", "prompt": "x"}]
    elif tool == "Skill":
        inputs = [{"skill": "x"}]
    else:
        inputs = [{}]
    out = [{"tool_name": tool, "tool_input": i} for i in inputs]
    out.append({"tool_name": tool})  # tool_input missing entirely
    return out


def keyed_hooks() -> dict[Path, tuple[int, set[str]]]:
    """Script -> (smallest declared timeout, tool names its matchers accept),
    for every PreToolUse handler with `onFailure: "block"` in the files."""
    found: dict[Path, tuple[int, set[str]]] = {}
    for path in REGISTRATION_FILES:
        data = json.loads(path.read_text(encoding="utf-8"))
        for block in data.get("hooks", {}).get("PreToolUse", []):
            matcher = block.get("matcher") or ".*"
            tools = {t for t in TOOLS if re.fullmatch(matcher, t)}
            for handler in block.get("hooks", []):
                if handler.get("onFailure") != "block":
                    continue
                scripts = SCRIPT_RE.findall(handler.get("command", ""))
                if len(scripts) != 1:
                    raise AssertionError(f"{path.name}: no single script in {handler!r}")
                script = ROOT / scripts[0]
                timeout = handler.get("timeout", DEFAULT_TIMEOUT)
                old_timeout, old_tools = found.get(script, (timeout, set()))
                found[script] = (min(timeout, old_timeout), old_tools | tools)
    return found


class CrashContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp(prefix="hook-crash-contract-"))
        cls.home = cls.tmp / "home"
        cls.cwd = cls.tmp / "cwd"
        cls.home.mkdir()
        cls.cwd.mkdir()

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _env(self, lane: str | None) -> dict:
        env = {k: v for k, v in os.environ.items()
               if k not in ("LANE", "GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN")
               and not k.startswith("CLAUDE")}
        env.update(HOME=str(self.home), GH_CONFIG_DIR=str(self.home / ".config" / "gh"),
                   XDG_CONFIG_HOME=str(self.home / ".config"), GIT_CONFIG_GLOBAL="/dev/null")
        if lane is not None:
            env["LANE"] = lane
        return env

    def _run(self, script: Path, payload: dict, lane: str | None, deleted_cwd: bool,
             limit: float) -> str | None:
        """One run; the failure text, or None when the contract holds."""
        cwd = self.cwd
        if deleted_cwd:
            cwd = Path(tempfile.mkdtemp(dir=self.tmp))
        body = {"session_id": "crash-contract", "transcript_path": str(self.home / "t.jsonl"),
                "cwd": str(cwd), "hook_event_name": "PreToolUse", "tool_use_id": "toolu_1",
                "permission_mode": "default", **payload}
        argv = [sys.executable, str(script)]
        if deleted_cwd:
            # The process cwd is gone too, as in a session whose cwd was removed.
            argv = ["sh", "-c", 'cd "$1" && rmdir "$1" && shift && exec "$@"', "sh", str(cwd), *argv]
        start = time.monotonic()
        try:
            done = subprocess.run(argv, input=json.dumps(body), capture_output=True, text=True,
                                  cwd=self.cwd, env=self._env(lane), timeout=limit * 2)
        except subprocess.TimeoutExpired:
            return f"timed out after {limit * 2:.1f}s"
        elapsed = time.monotonic() - start
        problems = []
        if done.returncode not in (0, 2):
            tail = (done.stderr.strip().splitlines() or [""])[-1]
            problems.append(f"exit {done.returncode}: {tail[:200]}")
        out = done.stdout.strip()
        if out:
            try:
                json.loads(out)
            except ValueError:
                problems.append(f"stdout is not JSON: {out[:120]!r}")
        if elapsed >= limit:
            problems.append(f"took {elapsed:.1f}s, limit {limit:.1f}s")
        return "; ".join(problems) or None

    def test_the_hook_list_is_read_from_the_registration_files(self):
        hooks = keyed_hooks()
        self.assertGreaterEqual(len(hooks), 20)
        for script in hooks:
            with self.subTest(script=script.name):
                self.assertTrue(script.is_file(), f"registered but missing: {script}")

    def _assert_contract(self, script: Path, timeout: int, tools: set[str]) -> None:
        """Exit 0 or 2, empty-or-JSON stdout, under half the timeout, for
        every payload, every LANE, and a deleted cwd."""
        limit = timeout / 2
        jobs = []
        for tool in sorted(tools):
            for payload in _payloads(tool):
                for lane in LANES:
                    jobs.append((script, payload, lane, False, limit))
        # A deleted cwd, once per LANE, on the first payload.
        for lane in LANES:
            jobs.append((script, _payloads(sorted(tools)[0])[0], lane, True, limit))
        workers = max(2, min(8, (os.cpu_count() or 2) // 2))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(lambda job: (job, self._run(*job)), jobs))
        failures = []
        for (script, payload, lane, deleted, _limit), problem in results:
            if problem:
                command = (payload.get("tool_input") or {}).get("command", "")
                failures.append(f"{script.relative_to(ROOT)} LANE={lane} tool={payload['tool_name']}"
                                f"{' deleted-cwd' if deleted else ''} "
                                f"input={command[:60]!r}: {problem}")
        self.assertEqual(failures, [], "\n" + "\n".join(failures))


def _contract_test(script: Path, timeout: int, tools: set[str]):
    def test(self: CrashContract) -> None:
        self._assert_contract(script, timeout, tools)
    test.__doc__ = f"{script.relative_to(ROOT)}: the crash contract on every payload its matchers take."
    return test


# One test per keyed script, named after it, so `-k <script stem>` runs one
# hook; the list itself still comes only from the registration files.
for _script, (_timeout, _tools) in sorted(keyed_hooks().items()):
    setattr(CrashContract, f"test_{_script.stem}_keeps_the_crash_contract",
            _contract_test(_script, _timeout, _tools))


if __name__ == "__main__":
    unittest.main()
