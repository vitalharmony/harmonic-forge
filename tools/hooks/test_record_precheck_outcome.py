"""harmonic-forge#1448 AC1: the post-run outcome row, joined to the gate row by
hashed tool_use_id and session. Synthetic store only."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
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
        decision_row.record_gate(self.store, decision_row.row(
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

    def _malformed_receipt(self, fp) -> None:
        bad = decision_row.row("allowed-receipt", tool="tools/gh/l1_post.py", mode="post",
                               klass="github-write", fp="f" * 64, sid=SESSION, tuid=TUID)
        bad["fp"] = fp
        decision_row.write_call(self.store, bad)

    def test_malformed_receipt_fp_falls_back_to_call_file(self) -> None:
        """harmonic-forge#1460 AC1/AC2: an int fp on the receipt does not lose the row."""
        decision_row.record_gate(self.store, decision_row.row(
            "precheck", tool="tools/gh/kill_check.py", mode="precheck-form", klass="",
            fp="e" * 64, sid=SESSION, tuid=TUID))
        self._malformed_receipt(123)
        rec.record(self.payload())
        post = self.rows()[-1]
        self.assertEqual((post["outcome"], post["tool"], post["fp"]),
                         ("ran-ok", "tools/gh/kill_check.py", "e" * 12))

    def test_both_files_malformed_records_nothing_and_keeps_files(self) -> None:
        """harmonic-forge#1460 AC2, in-process: main() swallows exceptions, so a
        subprocess exit code would prove nothing."""
        for fp in (123, ["x"]):
            with self.subTest(fp=fp):
                self._malformed_receipt(fp)
                path = decision_row.call_path(self.store, decision_row.digest(SESSION), decision_row.digest(TUID))
                path.parent.mkdir(parents=True, exist_ok=True)
                call = decision_row.row("precheck", tool="tools/x.py", mode="m", klass="", fp="e" * 64,
                                        sid=SESSION, tuid=TUID)
                call["fp"] = fp
                path.write_text(json.dumps(call), encoding="utf-8")
                before = len(self.rows())
                rec.record(self.payload())  # must not raise
                self.assertEqual(len(self.rows()), before)
                self.assertTrue(path.exists())
                self.assertTrue(decision_row.receipt_path(path).exists())

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
        decision_row.record_gate(self.store, decision_row.row(
            "precheck", tool="tools/gh/kill_check.py", mode="precheck-form", klass="",
            fp="e" * 64, sid=SESSION, tuid=TUID))
        decision_row.record_gate(self.store, decision_row.row(
            "error", sid=SESSION, tuid=TUID, reason="fault"))
        rec.record(self.payload("PostToolUseFailure", error="Exit code 1"))
        posts = [r for r in self.rows() if r["outcome"] == "ran-failed"]
        self.assertEqual(len(posts), 1)
        self.assertEqual((posts[0]["tool"], posts[0]["mode"]), ("tools/gh/l1_post.py", "post"))

    def test_join_survives_log_growth(self) -> None:
        """Sticky-wicket D: ~2 MB of other sessions' rows between the gate row
        and the outcome never lose the join."""
        self.gate_row()
        filler = json.dumps(decision_row.row("allowed-receipt", tool="tools/x.py", mode="m", klass="k",
                                             fp="a" * 64, sid="other", tuid="other")) + "\n"
        with self.log.open("a", encoding="utf-8") as fh:
            fh.write(filler * (2 * 1024 * 1024 // len(filler) + 1))
        self.assertGreater(self.log.stat().st_size, 2 * 1024 * 1024)
        rec.record(self.payload("PostToolUseFailure", error="Exit code 2"))
        post = self.rows()[-1]
        self.assertEqual((post["outcome"], post["tool"], post["mode"], post["exit"]),
                         ("ran-failed", "tools/gh/l1_post.py", "post", 2))

    def test_call_file_deleted_after_join_and_stale_pruned(self) -> None:
        self.gate_row()
        call = decision_row.call_path(self.store, decision_row.digest(SESSION), decision_row.digest(TUID))
        receipt = decision_row.receipt_path(call)
        decision_row.record_gate(self.store, decision_row.row(
            "precheck", tool="tools/x.py", sid=SESSION, tuid=TUID))
        self.assertTrue(receipt.exists())
        self.assertTrue(call.exists())
        stale = self.store / "calls" / "old-old.json"
        stale.write_text("{}")
        old = time.time() - decision_row.CALL_TTL - 60
        os.utime(stale, (old, old))
        rec.record(self.payload())
        self.assertFalse(receipt.exists())
        self.assertFalse(call.exists())
        self.assertFalse(stale.exists())
        rec.record(self.payload())
        self.assertEqual(len([r for r in self.rows() if r["outcome"] == "ran-ok"]), 1)

    def _paths(self) -> tuple[Path, Path]:
        call = decision_row.call_path(self.store, decision_row.digest(SESSION), decision_row.digest(TUID))
        return call, decision_row.receipt_path(call)

    def _precheck_row(self) -> None:
        decision_row.record_gate(self.store, decision_row.row(
            "precheck", tool="tools/gh/kill_check.py", mode="precheck-form", klass="",
            fp="e" * 64, sid=SESSION, tuid=TUID))

    def test_receipt_then_precheck_joins_to_receipt(self) -> None:
        self.gate_row()
        self._precheck_row()
        rec.record(self.payload())
        self.assertEqual((self.rows()[-1]["outcome"], self.rows()[-1]["tool"]),
                         ("ran-ok", "tools/gh/l1_post.py"))

    def test_invalid_receipt_falls_back_to_json(self) -> None:
        for bad in (b"not json", json.dumps({"outcome": "allowed-receipt", "tuid": "x",
                                              "session": decision_row.digest(SESSION)}).encode()):
            self._precheck_row()
            call, receipt = self._paths()
            receipt.write_bytes(bad)
            rec.record(self.payload())
            post = self.rows()[-1]
            self.assertEqual((post["outcome"], post["tool"]), ("ran-ok", "tools/gh/kill_check.py"))
            self.assertFalse(receipt.exists())
            self.assertFalse(call.exists())

    def test_recorder_never_blocks_on_planted_fifo(self) -> None:
        self._precheck_row()
        call, receipt = self._paths()
        os.mkfifo(receipt)
        worker = threading.Thread(target=rec.record, args=(self.payload(),), daemon=True)
        worker.start()
        worker.join(timeout=5)
        self.assertFalse(worker.is_alive(), "recorder blocked on a planted FIFO")
        self.assertEqual((self.rows()[-1]["outcome"], self.rows()[-1]["tool"]),
                         ("ran-ok", "tools/gh/kill_check.py"))

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
