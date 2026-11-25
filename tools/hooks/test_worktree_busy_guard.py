#!/usr/bin/env python3
"""harmonic-forge#1560: no writes into a worktree whose own check holds its lock.

The lock is a real `flock` held by a child bash, in a temp `WORKTREE_BUSY_DIR`;
no test touches ~/.cache/harmonic-forge/worktree-busy.
"""
from __future__ import annotations

import io
import json
import os
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
        self.assertIn(f"kill {self.child.pid}", reason)
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


class BashDenied(Busy):
    """AC2: a command naming W that writes there."""

    def test_writes_denied(self):
        for command in (f"git -C {self.W} commit -m x",
                        f"cd {self.W} && git checkout x",
                        f"echo x > {self.W}/f",
                        f"git -C {self.W} log > {self.W}/x",
                        f"git -C {self.W} diff --output {self.W}/x",
                        f"git -C {self.W} diff --output={self.W}/x",
                        f"git -C {self.W} status; rm {self.W}/x",
                        f"git -C {self.W} status ;rm {self.W}/x",
                        f"git -C {self.W} status || rm {self.W}/x",
                        f"git -C {self.W} status | tee {self.W}/x",
                        f"git -C {self.W} status & rm {self.W}/x",
                        f"git -C {self.W} status $(rm {self.W}/x)",
                        f"git -C {self.W} status `rm {self.W}/x`",
                        f"git -C {self.W} status\nrm {self.W}/x",
                        f"cd {self.W} && git status && rm x",
                        f"cd {self.W} && rm x",
                        f"rm -rf {self.W}",
                        f"rm '{self.W}'",
                        f"(cd {self.W})",
                        f"sed -i s/a/b/ {self.W}/f",
                        f"cat {self.W}/x > /tmp/y",
                        f"bash {XF} --caller claude --families 2 --posture verify "
                        f"--brief /tmp/b.md --cwd {self.W} > {self.W}/out",
                        f"bash {XF} --caller claude --families 2 --posture probe "
                        f"--brief /tmp/b.md --cwd {self.W}"):
            with self.subTest(command=command):
                self.assertIsNotNone(self.bash(command))


class BashAllowed(Busy):
    def test_reads_allowed(self):
        """AC2 allowlist."""
        for command in (f"cat {self.W}/x",
                        f"grep -rn x {self.W}/tools",
                        f"rg -n x {self.W}",
                        f"ls {self.W}",
                        f"git -C {self.W} status",
                        f"git -C {self.W} status --short",
                        f"git -C {self.W} log --oneline tools",
                        f"git -C {self.W} diff HEAD~1",
                        f"git -C {self.W} show HEAD:mise.toml",
                        f"git -C {self.W} rev-parse HEAD",
                        f"git -C {self.W} ls-files tools",
                        f"cd {self.W} && git status",
                        f"cd {self.W} && git diff --stat"):
            with self.subTest(command=command):
                self.assertIsNone(self.bash(command))

    def test_cross_family_verify_shape_allowed(self):
        """AC2: the R-0358 shape, direct and bash-prefixed, either caller."""
        for command in (f"bash {XF} --caller claude --families 2 --posture verify "
                        f"--brief /tmp/b.md --cwd {self.W}",
                        f"{XF} --caller codex --families 2 --posture verify "
                        f"--brief /tmp/b.md --cwd {self.W}"):
            with self.subTest(command=command):
                self.assertIsNone(self.bash(command))

    def test_command_not_naming_W_allowed(self):
        self.assertIsNone(self.bash("gh issue view 1560"))
        self.assertIsNone(self.bash("echo x > /tmp/f"))

    def test_payload_cwd_is_not_consulted(self):
        payload = {"tool_name": "Bash", "cwd": self.W, "tool_input": {"command": "echo x > f"}}
        self.assertIsNone(guard.guard(payload))


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


if __name__ == "__main__":
    unittest.main()
