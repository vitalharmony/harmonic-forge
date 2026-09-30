import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from importlib.machinery import SourceFileLoader

SCRIPT = Path(__file__).with_name("lane-queue-run")
SPEC = importlib.util.spec_from_loader("lane_queue_run", SourceFileLoader("lane_queue_run", str(SCRIPT)))
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(MODULE)


class QueueRunTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "project"
        (self.repo / ".codex").mkdir(parents=True)
        hooks_dir = self.root / "hooks"
        hooks_dir.mkdir()
        for name in MODULE.verify_codex_registration.REQUIRED_HOOKS:
            (hooks_dir / name).write_text("# stub\n")
        (self.repo / ".codex" / "hooks.json").write_text(json.dumps({"hooks": {"PreToolUse": [{"matcher": "^Bash$", "hooks": [
            {"type": "command", "command": f"python3 {hooks_dir / name}"} for name in MODULE.verify_codex_registration.REQUIRED_HOOKS
        ]}]}}))
        self.home = self.root / "home"
        (self.home / ".codex").mkdir(parents=True)
        (self.home / ".codex" / "config.toml").write_text(
            f'[projects."{self.repo.resolve()}"]\ntrust_level = "trusted"\n')
        self.queue = self.root / "queue.json"

    def write_queue(self, items):
        self.queue.write_text(json.dumps({"items": items}))

    def item(self, ident="one", status="pending", note=""):
        return {"id": ident, "kind": "work", "status": status, "note": note}

    def args(self, *extra):
        return ["--lane", "1", "--session", "abc", "--queue", str(self.queue), "--repo", str(self.repo), *extra]

    def good(self):
        return patch.object(MODULE, "session_cwd", return_value=str(self.repo)), patch.object(
            MODULE, "resume_args", return_value=["--no-daemon", "-c", "x=y"])

    def test_stubbed_resume_runs_all_items_and_replays_environment(self):
        self.write_queue([self.item("one"), self.item("two")])
        calls = []
        def execute(command, **kwargs):
            calls.append((command, kwargs))
            state = json.loads(self.queue.read_text())
            next(item for item in state["items"] if item["status"] == "in_progress")["status"] = "done"
            self.queue.write_text(json.dumps(state))
            return SimpleNamespace(returncode=0)
        cwd, flags = self.good()
        with cwd, flags:
            self.assertEqual(MODULE.run(self.args(), execute=execute, home=self.home), 0)
        self.assertEqual([call[0][:3] for call in calls], [["codex", "exec", "--no-daemon"]] * 2)
        self.assertTrue(all("--dangerously-bypass-hook-trust" not in call[0] for call in calls))
        self.assertEqual(calls[0][1]["env"]["LANE"], "1")
        self.assertEqual(calls[0][1]["env"]["LANE_AGENT"], "codex")
        self.assertEqual([item["status"] for item in json.loads(self.queue.read_text())["items"]], ["done", "done"])

    def test_registry_helper_replays_sandbox_add_dirs_and_config_tokens(self):
        flags = MODULE.resume_args("3")
        self.assertEqual(flags[:2], ["--sandbox", "danger-full-access"])
        self.assertIn("--add-dir", flags)
        self.assertIn("-c", flags)
        self.assertIn("sandbox_workspace_write.exclude_slash_tmp=true", flags)
        self.assertNotIn("--dangerously-bypass-hook-trust", flags)

    def test_queued_resume_is_unsandboxed_at_every_lane(self):
        """harmonic-forge#840 AC3: a queued `codex exec ... resume` runs with no
        sandbox at Lanes 1, 2 and 3, from the same registry table."""
        for lane in ("1", "2", "3"):
            with self.subTest(lane=lane):
                flags = MODULE.resume_args(lane)
                self.assertEqual(flags.count("--sandbox"), 1)
                self.assertEqual(flags[flags.index("--sandbox") + 1], "danger-full-access")

    def test_empty_and_existing_blocker_do_not_resume(self):
        for item in ([], [self.item(status="blocked", note="human needed")]):
            self.write_queue(item)
            cwd, flags = self.good()
            with cwd, flags as flags_mock:
                self.assertEqual(MODULE.run(self.args(), home=self.home), 0)
            flags_mock.assert_not_called()

    def test_recovery_blocks_in_progress_without_rerun(self):
        self.write_queue([self.item(status="done"), self.item("two", "in_progress")])
        cwd, flags = self.good()
        with cwd, flags as flags_mock:
            self.assertEqual(MODULE.run(self.args(), home=self.home), 0)
        flags_mock.assert_not_called()
        self.assertEqual(json.loads(self.queue.read_text())["items"][1]["status"], "blocked")

    def test_nonzero_resume_blocks_item_and_returns_its_code(self):
        self.write_queue([self.item()])
        cwd, flags = self.good()
        with cwd, flags:
            self.assertEqual(MODULE.run(self.args(), execute=lambda *_a, **_k: SimpleNamespace(returncode=7), home=self.home), 7)
        state = json.loads(self.queue.read_text())["items"][0]
        self.assertEqual((state["status"], state["note"]), ("blocked", "resume exited 7"))

    def test_success_without_state_update_becomes_blocked_not_false_empty(self):
        self.write_queue([self.item()])
        cwd, flags = self.good()
        with cwd, flags:
            self.assertEqual(MODULE.run(self.args(), execute=lambda *_a, **_k: SimpleNamespace(returncode=0), home=self.home), 0)
        self.assertEqual(json.loads(self.queue.read_text())["items"][0]["status"], "blocked")

    def test_success_that_returns_item_to_pending_becomes_blocked_not_rerun(self):
        self.write_queue([self.item()])
        def execute(*_args, **_kwargs):
            state = json.loads(self.queue.read_text())
            state["items"][0]["status"] = "pending"
            self.queue.write_text(json.dumps(state))
            return SimpleNamespace(returncode=0)
        cwd, flags = self.good()
        with cwd, flags:
            self.assertEqual(MODULE.run(self.args(), execute=execute, home=self.home), 0)
        self.assertEqual(json.loads(self.queue.read_text())["items"][0]["status"], "blocked")

    def test_max_steps_stops_after_budget(self):
        self.write_queue([self.item("one"), self.item("two")])
        calls = []
        def execute(*_args, **_kwargs):
            calls.append(1)
            state = json.loads(self.queue.read_text())
            next(item for item in state["items"] if item["status"] == "in_progress")["status"] = "done"
            self.queue.write_text(json.dumps(state))
            return SimpleNamespace(returncode=0)
        cwd, flags = self.good()
        with cwd, flags:
            self.assertEqual(MODULE.run(self.args("--max-steps", "1"), execute=execute, home=self.home), 0)
        self.assertEqual(len(calls), 1)
        self.assertEqual(json.loads(self.queue.read_text())["items"][1]["status"], "pending")
        self.assertIn("stopped: max steps 1", next((self.home / ".cache" / "lane-queue-run").glob("*.log")).read_text())

    def test_interrupt_is_a_logged_stop(self):
        self.write_queue([self.item()])
        cwd, flags = self.good()
        with cwd, flags:
            self.assertEqual(MODULE.run(self.args(), execute=lambda *_a, **_k: (_ for _ in ()).throw(KeyboardInterrupt()), home=self.home), 130)
        self.assertIn("stopped: interrupted", next((self.home / ".cache" / "lane-queue-run").glob("*.log")).read_text())

    def test_mismatched_cwd_wrong_lane_and_untrusted_repo_refuse_before_resume(self):
        self.write_queue([])
        with patch.object(MODULE, "session_cwd", return_value="/wrong"), patch.object(MODULE, "resume_args") as flags:
            self.assertEqual(MODULE.run(self.args(), home=self.home), 2)
        flags.assert_not_called()
        lane2 = self.root / "project-lane2"
        (lane2 / ".codex").mkdir(parents=True)
        (lane2 / ".codex" / "hooks.json").write_text("{}")
        with (self.home / ".codex" / "config.toml").open("a") as config:
            config.write(f'\n[projects."{lane2.resolve()}"]\ntrust_level = "trusted"\n')
        with patch.object(MODULE, "session_cwd", return_value=str(lane2)), patch.object(MODULE, "resume_args") as flags:
            self.assertEqual(MODULE.run(["--lane", "2", "--session", "abc", "--queue", str(self.queue), "--repo", str(lane2)], home=self.home), 2)
        flags.assert_not_called()

    def test_rerun_skips_done_item(self):
        self.write_queue([self.item(status="done")])
        cwd, flags = self.good()
        with cwd, flags as flags_mock:
            self.assertEqual(MODULE.run(self.args(), home=self.home), 0)
            self.assertEqual(MODULE.run(self.args(), home=self.home), 0)
        flags_mock.assert_not_called()
        (self.home / ".codex" / "config.toml").write_text("")
        with patch.object(MODULE, "session_cwd", return_value=str(self.repo)), patch.object(MODULE, "resume_args") as flags:
            self.assertEqual(MODULE.run(self.args(), home=self.home), 2)
        flags.assert_not_called()

    def test_second_driver_for_same_session_is_refused_even_with_other_queue(self):
        self.write_queue([])
        with MODULE.run_locks("abc", self.queue, self.home):
            with patch.object(MODULE, "session_cwd", return_value=str(self.repo)), patch.object(MODULE, "resume_args") as flags:
                self.assertEqual(MODULE.run(self.args(), home=self.home), 2)
        flags.assert_not_called()

    def test_same_queue_is_refused_for_a_different_session(self):
        self.write_queue([])
        with MODULE.run_locks("other", self.queue, self.home):
            with patch.object(MODULE, "session_cwd", return_value=str(self.repo)), patch.object(MODULE, "resume_args") as flags:
                self.assertEqual(MODULE.run(self.args("--session", "abc"), home=self.home), 2)
        flags.assert_not_called()

    def test_thread_name_and_uuid_share_canonical_lock_identity(self):
        index = self.home / ".codex" / "session_index.jsonl"
        index.write_text(json.dumps({"thread_name": "thread", "id": "uuid"}) + "\n")
        self.assertEqual(MODULE.session_identity("thread", self.home), "uuid")
        self.assertEqual(MODULE.session_identity("uuid", self.home), "uuid")

    def test_empty_hook_registration_is_not_trusted(self):
        hooks = self.repo / ".codex" / "hooks.json"
        hooks.parent.mkdir(exist_ok=True)
        hooks.write_text('{"hooks": {"PreToolUse": []}}')
        self.assertFalse(MODULE.trusted(self.repo, self.home))

    def test_duplicate_thread_entries_canonicalize_old_uuid_to_latest(self):
        index = self.home / ".codex" / "session_index.jsonl"
        index.write_text("\n".join([
            json.dumps({"thread_name": "thread", "id": "uuid-old"}),
            json.dumps({"thread_name": "thread", "id": "uuid-new"}),
        ]) + "\n")
        self.assertEqual(MODULE.session_identity("thread", self.home), "uuid-new")
        self.assertEqual(MODULE.session_identity("uuid-old", self.home), "uuid-new")
        self.assertEqual(MODULE.session_identity("uuid-new", self.home), "uuid-new")

    def test_post_resume_state_failure_blocks_and_logs(self):
        self.write_queue([self.item()])
        cwd, flags = self.good()
        def execute(*_args, **_kwargs):
            self.queue.write_text("not-json")
            return SimpleNamespace(returncode=0)
        with cwd, flags:
            self.assertEqual(MODULE.run(self.args(), execute=execute, home=self.home), 2)
        state = json.loads(self.queue.read_text())
        self.assertEqual(state["items"][0]["status"], "blocked")

    def test_real_subprocess_uses_stub_codex_and_records_nonzero_stop(self):
        self.write_queue([self.item()])
        sessions = self.home / ".codex" / "sessions" / "2026" / "09" / "26"
        sessions.mkdir(parents=True)
        (sessions / "rollout-x-abc.jsonl").write_text(json.dumps({"session_meta": {"cwd": str(self.repo)}}) + "\n")
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        stub = bin_dir / "codex"
        stub.write_text("#!/bin/sh\nexit 9\n")
        stub.chmod(0o755)
        env = os.environ | {"HOME": str(self.home), "PATH": f"{bin_dir}:{os.environ['PATH']}"}
        result = subprocess.run([sys.executable, str(SCRIPT), *self.args()], cwd=self.repo, env=env, text=True, capture_output=True)
        self.assertEqual(result.returncode, 9, result.stderr)
        self.assertEqual(json.loads(self.queue.read_text())["items"][0]["status"], "blocked")


if __name__ == "__main__":
    unittest.main()
