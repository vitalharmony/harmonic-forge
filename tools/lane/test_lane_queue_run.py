import importlib.util
import json
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
        self.repo = self.root / "repo"
        (self.repo / ".codex").mkdir(parents=True)
        (self.repo / ".codex" / "hooks.json").write_text("{}")
        self.home = self.root / "home"
        (self.home / ".codex").mkdir(parents=True)
        (self.home / ".codex" / "config.toml").write_text(str((self.repo / ".codex" / "hooks.json").resolve()))
        self.queue = self.root / "queue.json"

    def write_queue(self, items):
        self.queue.write_text(json.dumps({"items": items}))

    def args(self, *extra):
        return ["--lane", "1", "--session", "abc", "--queue", str(self.queue), "--repo", str(self.repo), *extra]

    def test_runs_pending_items_until_empty(self):
        self.write_queue([{ "id": "one", "kind": "x", "status": "pending", "note": "" }])
        def execute(command, **_kwargs):
            state = json.loads(self.queue.read_text())
            state["items"][0]["status"] = "done"
            self.queue.write_text(json.dumps(state))
            return SimpleNamespace(returncode=0)
        with patch.object(MODULE, "session_cwd", return_value=str(self.repo)), \
             patch.object(MODULE, "resume_args", return_value=["--no-daemon"]):
            self.assertEqual(MODULE.run(self.args(), execute=execute, home=self.home), 0)
        self.assertEqual(json.loads(self.queue.read_text())["items"][0]["status"], "done")

    def test_recovery_blocks_in_progress_without_resume(self):
        self.write_queue([{ "id": "one", "kind": "x", "status": "in_progress", "note": "" }])
        with patch.object(MODULE, "session_cwd", return_value=str(self.repo)), \
             patch.object(MODULE, "resume_args") as flags:
            self.assertEqual(MODULE.run(self.args(), home=self.home), 0)
        flags.assert_not_called()
        self.assertEqual(json.loads(self.queue.read_text())["items"][0]["status"], "blocked")

    def test_mismatched_session_refuses_before_resume(self):
        self.write_queue([])
        with patch.object(MODULE, "session_cwd", return_value="/wrong"), \
             patch.object(MODULE, "resume_args") as flags:
            self.assertEqual(MODULE.run(self.args(), home=self.home), 2)
        flags.assert_not_called()

    def test_max_steps_stops(self):
        self.write_queue([{ "id": "one", "kind": "x", "status": "pending", "note": "" }])
        with patch.object(MODULE, "session_cwd", return_value=str(self.repo)), \
             patch.object(MODULE, "resume_args", return_value=[]):
            self.assertEqual(MODULE.run(self.args("--max-steps", "1"),
                                        execute=lambda *_a, **_kw: SimpleNamespace(returncode=0),
                                        home=self.home), 0)


if __name__ == "__main__":
    unittest.main()
