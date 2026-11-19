"""harmonic-forge#1448 AC1: the post-run outcome row, joined to the gate row by
hashed tool_use_id and session. Synthetic store only."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
FORGE = HERE.parents[1]
SCRIPT = HERE / "record_precheck_outcome.py"
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(FORGE / "tools" / "precheck"))
import decision_row  # noqa: E402
import record_precheck_outcome as rec  # noqa: E402

SESSION = "sess-1448"
TUID = "toolu_01ABC"
ROW_KEYS = {"ts", "outcome", "tool", "mode", "class", "fp", "session", "tuid", "exit", "reason"}


class Recorder(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = Path(tmp.name) / "store"
        env = mock.patch.dict(os.environ, {"PRECHECK_GATE_DIR": str(self.store)})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("PREFLIGHT_RECEIPT_DIR", None)
        self.log = self.store / "decisions.jsonl"

    def gate_row(self, outcome="allowed-receipt", tuid=TUID, session=SESSION) -> None:
        decision_row.append(self.log, decision_row.row(
            outcome, tool="tools/gh/l1_post.py", mode="post", klass="github-write", fp="f" * 64,
            sid=session, tuid=tuid))

    def rows(self) -> list[dict]:
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def run_hook(self, payload, raw: str | None = None) -> subprocess.CompletedProcess:
        env = {**os.environ, "PRECHECK_GATE_DIR": str(self.store)}
        return subprocess.run([sys.executable, str(SCRIPT)], input=raw if raw is not None else json.dumps(payload),
                              capture_output=True, text=True, env=env, check=False)

    def payload(self, event="PostToolUse", **extra) -> dict:
        return {"tool_name": "Bash", "hook_event_name": event, "session_id": SESSION,
                "tool_use_id": TUID, "tool_input": {"command": "python3 tools/gh/l1_post.py --secret-arg"},
                **extra}

    def test_ran_ok_row_joins_gate_row_by_tuid(self) -> None:
        self.gate_row()
        rec.record(self.payload())
        post = self.rows()[-1]
        self.assertEqual((post["outcome"], post["exit"], post["tool"], post["mode"], post["class"]),
                         ("ran-ok", 0, "tools/gh/l1_post.py", "post", "github-write"))
        self.assertEqual((post["tuid"], post["session"], post["fp"]),
                         (decision_row.digest(TUID), decision_row.digest(SESSION), "f" * 12))

    def test_ran_failed_row_exit_code_from_error_header(self) -> None:
        self.gate_row()
        rec.record(self.payload("PostToolUseFailure", error="Exit code 3\nstderr text"))
        self.assertEqual((self.rows()[-1]["outcome"], self.rows()[-1]["exit"]), ("ran-failed", 3))

    def test_exit_null_when_no_header(self) -> None:
        self.gate_row()
        rec.record(self.payload("PostToolUseFailure", error="something else\nExit code 3",
                                tool_response={"stderr": "Exit code 9"}))
        self.assertEqual((self.rows()[-1]["outcome"], self.rows()[-1]["exit"]), ("ran-failed", None))

    def test_no_row_without_gate_row(self) -> None:
        self.gate_row(tuid="other")
        self.gate_row(session="other-session")
        rec.record(self.payload())
        self.assertEqual(len(self.rows()), 2)

    def test_several_gate_rows_attribute_to_receipted_call(self) -> None:
        """One command logs allowed-receipt then precheck under one tuid."""
        self.gate_row()
        decision_row.append(self.log, decision_row.row(
            "precheck", tool="tools/gh/kill_check.py", mode="precheck-form", klass="",
            fp="e" * 64, sid=SESSION, tuid=TUID))
        rec.record(self.payload("PostToolUseFailure", error="Exit code 1"))
        posts = [r for r in self.rows() if r["outcome"] == "ran-failed"]
        self.assertEqual(len(posts), 1)
        self.assertEqual((posts[0]["tool"], posts[0]["mode"]), ("tools/gh/l1_post.py", "post"))

    def test_join_survives_head_move(self) -> None:
        """KC1b: the join is on tuid; the post row never recomputes the fp."""
        self.gate_row()
        with mock.patch("gate_receipt.fingerprint", side_effect=AssertionError("never called")):
            rec.record(self.payload())
        self.assertEqual(self.rows()[-1]["outcome"], "ran-ok")

    def test_row_has_no_argv_token(self) -> None:
        self.gate_row()
        rec.record(self.payload("PostToolUseFailure", error="Exit code 1"))
        for row in self.rows():
            self.assertEqual(set(row), ROW_KEYS)
            self.assertNotIn("secret-arg", json.dumps(row))
            self.assertNotIn(TUID, json.dumps(row))
            self.assertNotIn(SESSION, json.dumps(row))

    def test_recorder_fault_is_silent(self) -> None:
        self.gate_row()
        for raw in ("not json", "[]", json.dumps({"tool_name": "Bash"})):
            done = self.run_hook(None, raw=raw)
            self.assertEqual((done.returncode, done.stdout, done.stderr), (0, "", ""))
        blocker = Path(self.store.parent) / "blocker"
        blocker.write_text("x")
        env = {**os.environ, "PRECHECK_GATE_DIR": str(blocker / "store")}
        done = subprocess.run([sys.executable, str(SCRIPT)], input=json.dumps(self.payload()),
                              capture_output=True, text=True, env=env, check=False)
        self.assertEqual((done.returncode, done.stdout, done.stderr), (0, "", ""))
        with mock.patch.object(decision_row, "append", side_effect=RuntimeError("raise")), \
                mock.patch.object(sys, "stdin", __import__("io").StringIO(json.dumps(self.payload()))):
            rec.main()

    def test_end_to_end_hook(self) -> None:
        self.gate_row()
        done = self.run_hook(self.payload())
        self.assertEqual((done.returncode, done.stdout, done.stderr), (0, "", ""))
        self.assertEqual(self.rows()[-1]["outcome"], "ran-ok")


if __name__ == "__main__":
    unittest.main()
