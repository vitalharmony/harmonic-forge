"""harmonic-forge#874 step 1 -- the probe-only `/auto-ae` UserPromptSubmit hook,
driven as a real hook process against a temporary HOME."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
HOOK = HERE / "auto_ae_toggle.py"
sys.path.insert(0, str(HERE))
import auto_ae_toggle  # noqa: E402

SECRET_SESSION = "sess-SECRET-1234"
SECRET_TRANSCRIPT = "/home/x/.claude/projects/p/SECRET-transcript.jsonl"


def _payload(prompt):
    return {"hook_event_name": "UserPromptSubmit", "prompt": prompt,
            "session_id": SECRET_SESSION, "transcript_path": SECRET_TRANSCRIPT,
            "cwd": "/tmp"}


def _run(stdin, home, lane="1", entrypoint="cli"):
    env = {k: v for k, v in os.environ.items()
           if k not in ("LANE", "CLAUDE_CODE_ENTRYPOINT")}
    env["HOME"] = home
    if lane is not None:
        env["LANE"] = lane
    if entrypoint is not None:
        env["CLAUDE_CODE_ENTRYPOINT"] = entrypoint
    result = subprocess.run([sys.executable, str(HOOK)], input=stdin, env=env,
                            capture_output=True, text=True)
    return result


def _probe(home):
    return Path(home) / ".local" / "state" / "auto-ae" / "probe.jsonl"


class RecordsOneLine(unittest.TestCase):
    def test_lane1_auto_ae_prompt_appends_one_redacted_line(self):
        with tempfile.TemporaryDirectory() as home:
            prompt = "<command-name>/auto-ae</command-name>\n<command-args>status</command-args>"
            result = _run(json.dumps(_payload(prompt)), home)
            self.assertEqual(result.returncode, 0)
            out = json.loads(result.stdout)
            self.assertEqual(out, {"systemMessage": auto_ae_toggle.RECORDED})
            lines = _probe(home).read_text().splitlines()
            self.assertEqual(len(lines), 1)
            entry = json.loads(lines[0])
            self.assertEqual(entry["prompt"], prompt)
            self.assertEqual(entry["LANE"], "1")
            self.assertEqual(entry["CLAUDE_CODE_ENTRYPOINT"], "cli")
            self.assertEqual(entry["payload_keys"],
                             ["cwd", "hook_event_name", "prompt", "session_id",
                              "transcript_path"])
            self.assertIn("timestamp", entry)
            raw = _probe(home).read_text()
            self.assertNotIn(SECRET_SESSION, raw)
            self.assertNotIn(SECRET_TRANSCRIPT, raw)

    def test_match_is_case_insensitive_and_appends(self):
        with tempfile.TemporaryDirectory() as home:
            _run(json.dumps(_payload("AUTO-AE status")), home)
            _run(json.dumps(_payload("/Auto-Ae on")), home)
            self.assertEqual(len(_probe(home).read_text().splitlines()), 2)

    def test_missing_entrypoint_is_recorded_as_null(self):
        with tempfile.TemporaryDirectory() as home:
            _run(json.dumps(_payload("/auto-ae")), home, entrypoint=None)
            entry = json.loads(_probe(home).read_text())
            self.assertIsNone(entry["CLAUDE_CODE_ENTRYPOINT"])


class DecidesAndGrantsNothing(unittest.TestCase):
    def test_output_carries_no_decision_or_context(self):
        with tempfile.TemporaryDirectory() as home:
            out = json.loads(_run(json.dumps(_payload("/auto-ae on")), home).stdout)
            self.assertEqual(set(out), {"systemMessage"})
            self.assertIn("nothing toggled", out["systemMessage"])

    def test_writes_nothing_but_the_probe_log(self):
        with tempfile.TemporaryDirectory() as home:
            _run(json.dumps(_payload("/auto-ae on")), home)
            written = sorted(str(p.relative_to(home)) for p in Path(home).rglob("*")
                             if p.is_file())
            self.assertEqual(written, [".local/state/auto-ae/probe.jsonl"])


class SilentWhenNotApplicable(unittest.TestCase):
    def _assert_silent(self, stdin, lane="1"):
        with tempfile.TemporaryDirectory() as home:
            result = _run(stdin, home, lane=lane)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stdout.strip(), "")
            self.assertFalse(_probe(home).exists())

    def test_other_lanes_and_no_lane_record_nothing(self):
        for lane in ("2", "3", None, ""):
            with self.subTest(lane=lane):
                self._assert_silent(json.dumps(_payload("/auto-ae on")), lane=lane)

    def test_prompt_without_auto_ae_records_nothing(self):
        self._assert_silent(json.dumps(_payload("BATCH F874 auto ae")))
        self._assert_silent(json.dumps(_payload("autoae")))

    def test_non_string_or_missing_prompt_records_nothing(self):
        self._assert_silent(json.dumps({"prompt": ["auto-ae"]}))
        self._assert_silent(json.dumps({"session_id": "x"}))
        self._assert_silent(json.dumps(["auto-ae"]))


class NeverRaises(unittest.TestCase):
    def test_malformed_stdin_exits_zero_and_records_nothing(self):
        with tempfile.TemporaryDirectory() as home:
            result = _run("not json auto-ae", home)
            self.assertEqual(result.returncode, 0)
            self.assertIn("NOT recorded", json.loads(result.stdout)["systemMessage"])
            self.assertFalse(_probe(home).exists())

    def test_unwritable_probe_dir_exits_zero(self):
        with tempfile.TemporaryDirectory() as home:
            # A FILE where the state directory must go makes mkdir fail.
            (Path(home) / ".local").mkdir()
            (Path(home) / ".local" / "state").write_text("blocker")
            result = _run(json.dumps(_payload("/auto-ae")), home)
            self.assertEqual(result.returncode, 0)
            self.assertIn("NOT recorded", json.loads(result.stdout)["systemMessage"])

    def test_malformed_stdin_outside_lane1_is_silent(self):
        with tempfile.TemporaryDirectory() as home:
            result = _run("not json", home, lane="2")
            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
