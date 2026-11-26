#!/usr/bin/env python3
"""harmonic-forge#1560: no writes into a worktree whose own check holds its lock.

The lock is a real `flock` held by a child bash, in a temp `WORKTREE_BUSY_DIR`;
no test touches ~/.claude/state/worktree-busy.
"""
from __future__ import annotations

import io
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "worktree"))
import busy_lock  # noqa: E402
import worktree_busy_guard as guard  # noqa: E402

HOOK = HERE / "worktree_busy_guard.py"
XF = "/home/u/harmonic-forge/tools/lane/cross_family_call.sh"


def wait_for(predicate, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


class Busy(unittest.TestCase):
    """W is busy: a child bash holds its lock and the sidecar names that shell."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(os.path.realpath(tmp.name))
        self.store = base / "store"
        env = mock.patch.dict(os.environ, {"WORKTREE_BUSY_DIR": str(self.store)})
        env.start()
        self.addCleanup(env.stop)
        self.W = str(base / "harmonic-forge-894-impl")
        self.sibling = str(base / "harmonic-forge-894-impl2")
        os.mkdir(self.W)
        os.mkdir(self.sibling)
        self.child = self.hold(self.W)

    def hold(self, root: str) -> subprocess.Popen:
        lock = busy_lock.lock_path(root)
        lock.parent.mkdir(parents=True, exist_ok=True)
        script = (f"exec 9>{shlex.quote(str(lock))}; flock 9; "
                  f"python3 {shlex.quote(str(HERE.parent / 'worktree' / 'busy_lock.py'))} sidecar "
                  f"--root {shlex.quote(root)} --head cafef00d; sleep 30; true")
        child = subprocess.Popen(["bash", "-c", script], env=dict(os.environ))

        def stop():
            if child.poll() is None:
                child.kill()
            child.wait()
        self.addCleanup(stop)
        self.assertTrue(wait_for(lambda: busy_lock.is_busy(root)), "lock never became busy")
        return child

    def bash(self, command: str) -> str | None:
        return guard.guard({"tool_name": "Bash", "tool_input": {"command": command}})

    def edit(self, tool: str, path: str, key: str = "file_path") -> str | None:
        return guard.guard({"tool_name": tool, "tool_input": {key: path, "old_string": "a",
                                                              "new_string": "b"}})


class FileTools(Busy):
    def test_edit_under_busy_root_denied_naming_the_check(self):
        """AC1."""
        reason = self.edit("Edit", f"{self.W}/tools/gh/test_l1_post_ae_ci.py")
        self.assertIsNotNone(reason)
        self.assertIn(self.W, reason)
        self.assertIn("cafef00d", reason)
        self.assertIn(f"kill -- -{self.child.pid}", reason)
        self.assertIn(f"shell_pid {self.child.pid}", reason)
        self.assertIn("a full check is running in this worktree (R-0376)", reason)
        self.assertIn("task-stop tool", reason)

    def test_every_file_tool_denied(self):
        for tool in ("Write", "Edit", "MultiEdit"):
            self.assertIsNotNone(self.edit(tool, f"{self.W}/x.py"), tool)
        self.assertIsNotNone(self.edit("NotebookEdit", f"{self.W}/n.ipynb", key="notebook_path"))

    def test_relative_path_resolved_against_cwd(self):
        payload = {"tool_name": "Write", "cwd": self.W, "tool_input": {"file_path": "sub/x.py"}}
        self.assertIsNotNone(guard.guard(payload))

    def test_sibling_prefix_worktree_allowed(self):
        """AC3 boundary: W's name is a prefix of the sibling's."""
        self.assertIsNone(self.edit("Edit", f"{self.sibling}/x"))
        self.assertIsNone(self.bash(f"echo x > {self.sibling}/x"))
        self.assertIsNone(self.bash(f"git -C {self.sibling} commit -m x"))

    def test_other_worktree_allowed(self):
        self.assertIsNone(self.edit("Write", "/tmp/elsewhere/x.py"))

    def test_allowed_once_the_check_exits(self):
        """AC3: killed (and so, any exit) frees W."""
        self.child.kill()
        self.child.wait()
        self.assertIsNone(self.edit("Edit", f"{self.W}/x.py"))
        self.assertIsNone(self.bash(f"git -C {self.W} commit -m x"))

    def test_corrupt_sidecar_never_blocks(self):
        """AC4."""
        busy_lock.sidecar_path(self.W).write_text("{garbage")
        self.assertIsNone(self.edit("Edit", f"{self.W}/x.py"))
        self.assertIsNone(self.bash(f"echo x > {self.W}/f"))

    def test_other_tools_ignored(self):
        self.assertIsNone(guard.guard({"tool_name": "Read", "tool_input": {"file_path": f"{self.W}/x"}}))


class BashAlwaysAllowed(Busy):
    """Amendment AC3: Bash is never denied by this hook, even a write naming a
    busy W; the check's integrity step catches that instead."""

    def test_bash_writes_into_a_busy_worktree_are_allowed(self):
        for command in (f"git -C {self.W} commit -m x", f"cd {self.W} && git checkout x",
                        f"echo x > {self.W}/f", f"rm -rf {self.W}"):
            with self.subTest(command=command):
                self.assertIsNone(self.bash(command))


class FailOpen(unittest.TestCase):
    """AC4: the hook never denies on its own failure."""

    def run_hook(self, script: Path, payload: object, env: dict | None = None):
        return subprocess.run([sys.executable, str(script)], input=json.dumps(payload),
                              capture_output=True, text=True, env=env, timeout=30)

    def test_import_failure_allows(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        lonely = Path(tmp) / "hooks" / "worktree_busy_guard.py"
        lonely.parent.mkdir()
        shutil.copy(HOOK, lonely)
        done = self.run_hook(lonely, {"tool_name": "Edit", "tool_input": {"file_path": "/x"}})
        self.assertEqual(done.returncode, 0)
        self.assertEqual(done.stdout.strip(), "")
        self.assertIn("ModuleNotFoundError", done.stderr)

    def test_exception_in_guard_allows(self):
        stdin = io.TextIOWrapper(io.BytesIO(b'{"tool_name": "Edit", "tool_input": {"file_path": "/x"}}'))
        out = io.StringIO()
        with mock.patch.object(guard, "guard", side_effect=RuntimeError("boom")), \
                mock.patch.object(sys, "stdin", stdin), mock.patch.object(sys, "stdout", out), \
                mock.patch.object(sys, "stderr", io.StringIO()):
            self.assertEqual(guard.main(), 0)
        self.assertEqual(out.getvalue(), "")

    def test_unparseable_payload_allows(self):
        done = subprocess.run([sys.executable, str(HOOK)], input="not json",
                              capture_output=True, text=True, timeout=30)
        self.assertEqual(done.returncode, 0)
        self.assertEqual(done.stdout.strip(), "")

    def test_no_busy_roots_allows(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        env = dict(os.environ, WORKTREE_BUSY_DIR=str(Path(tmp) / "empty"))
        done = self.run_hook(HOOK, {"tool_name": "Bash", "tool_input": {"command": "rm -rf /x"}}, env)
        self.assertEqual((done.returncode, done.stdout.strip()), (0, ""))


class HookProcess(Busy):
    def test_hook_process_denies_with_pretooluse_json(self):
        done = subprocess.run([sys.executable, str(HOOK)], capture_output=True, text=True,
                              input=json.dumps({"tool_name": "Write",
                                                "tool_input": {"file_path": f"{self.W}/x"}}),
                              env=dict(os.environ), timeout=30)
        self.assertEqual(done.returncode, 0, done.stderr)
        out = json.loads(done.stdout)["hookSpecificOutput"]
        self.assertEqual(out["permissionDecision"], "deny")
        self.assertIn(self.W, out["permissionDecisionReason"])


class Wiring(Busy):
    """Sticky-wicket fix C: each registered row, run as written through `sh -c`
    with HOME in a temp dir. With `harmonic-forge` there linking to this
    checkout (so it runs this branch's guard, not the deployed one) it denies a
    write into a busy W; with no script there the guarded idiom allows the same
    write instead of exiting 2 (which would block every tool call)."""

    @staticmethod
    def commands(path: Path) -> list[str]:
        data = json.loads(path.read_text(encoding="utf-8"))
        return [_operator_layout(hook["command"]) for groups in data["hooks"].values() for group in groups
                for hook in group.get("hooks", []) if "worktree_busy_guard.py" in hook.get("command", "")]

    def run_row(self, command: str, linked: bool) -> subprocess.CompletedProcess:
        with tempfile.TemporaryDirectory() as home:
            if linked:
                os.symlink(HERE.parents[1], os.path.join(home, "harmonic-forge"))
            return subprocess.run(["sh", "-c", command], capture_output=True, text=True, timeout=30,
                                  input=json.dumps({"tool_name": "Write",
                                                    "tool_input": {"file_path": f"{self.W}/x"}}),
                                  env=dict(os.environ, HOME=home))

    def test_each_registered_row_runs_the_guard_and_allows_when_it_is_absent(self):
        checkout = HERE.parents[1]
        for path in (checkout / ".claude" / "settings.json", checkout / "tools" / "lane" / "lane_hooks.json"):
            with self.subTest(path=path.name):
                (command,) = self.commands(path)
                done = self.run_row(command, linked=True)
                self.assertEqual(done.returncode, 0, done.stderr)
                out = json.loads(done.stdout)["hookSpecificOutput"]
                self.assertEqual(out["permissionDecision"], "deny")
                self.assertIn(self.W, out["permissionDecisionReason"])
                absent = self.run_row(command, linked=False)
                self.assertEqual((absent.returncode, absent.stdout.strip()), (0, ""), absent.stderr)

    @staticmethod
    def resolved(path: Path, tool: str) -> list[str]:
        """The guard rows Claude Code runs for `tool`'s PreToolUse: a group's
        matcher is a regex matched against the whole tool name; "" or "*" is all."""
        data = json.loads(path.read_text(encoding="utf-8"))
        return [_operator_layout(hook["command"]) for group in data["hooks"].get("PreToolUse", [])
                if group.get("matcher", "") in ("", "*") or re.fullmatch(group["matcher"], tool)
                for hook in group.get("hooks", []) if "worktree_busy_guard.py" in hook.get("command", "")]

    def test_the_registered_matcher_routes_every_guarded_tool_to_the_guard(self):
        """Post-verdict gap: the row reached through its matcher, not called directly."""
        checkout = HERE.parents[1]
        for path in (checkout / ".claude" / "settings.json", checkout / "tools" / "lane" / "lane_hooks.json"):
            for tool, key in (("Write", "file_path"), ("Edit", "file_path"), ("MultiEdit", "file_path"),
                              ("NotebookEdit", "notebook_path"), ("Bash", None)):
                with self.subTest(path=path.name, tool=tool):
                    (command,) = self.resolved(path, tool)
                    if key is None:
                        continue  # Bash reaches the guard, which never denies it
                    with tempfile.TemporaryDirectory() as home:
                        os.symlink(checkout, os.path.join(home, "harmonic-forge"))
                        done = subprocess.run(["sh", "-c", command], capture_output=True, text=True,
                                              input=json.dumps({"tool_name": tool,
                                                                "tool_input": {key: f"{self.W}/x"}}),
                                              env=dict(os.environ, HOME=home), timeout=30)
                    self.assertEqual(done.returncode, 0, done.stderr)
                    self.assertEqual(json.loads(done.stdout)["hookSpecificOutput"]["permissionDecision"],
                                     "deny", tool)


def _operator_layout(command: str) -> str:
    """A stored row as the operator layout renders it (harmonic-forge#1526): the
    `@FORGE_ROOT@` placeholder becomes `${HOME}/harmonic-forge`, which these
    tests link to this checkout under a temp HOME."""
    return command.replace("@FORGE_ROOT@", "${HOME}/harmonic-forge")


if __name__ == "__main__":
    unittest.main()
