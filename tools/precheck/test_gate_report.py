"""harmonic-forge#1448 AC3/AC4/AC5: the counts-only reader and its new cases.
Synthetic store only."""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
FORGE = HERE.parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(FORGE / "tools" / "hooks"))
import decision_row  # noqa: E402
import gate_receipt  # noqa: E402
import gate_report  # noqa: E402
import precheck_store_guard  # noqa: E402

L1 = "tools/gh/l1_post.py"


def _first_mode(path: str) -> dict:
    return gate_receipt.precheck_tool(path)["modes"][0]


class Report(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = Path(tmp.name) / "store"
        env = mock.patch.dict(os.environ, {"PRECHECK_GATE_DIR": str(self.store)})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("PREFLIGHT_RECEIPT_DIR", None)
        self.log = self.store / "decisions.jsonl"
        mode = _first_mode(L1)
        self.mode, self.klass = mode["name"], mode["class"]
        self.now = time.time()

    def add(self, outcome, *, tool=L1, mode=None, klass=None, sid="s1", tuid="t1", ts=None, **kw) -> None:
        decision_row.append(self.log, decision_row.row(
            outcome, tool=tool, mode=self.mode if mode is None else mode,
            klass=self.klass if klass is None else klass, fp="SENTINELFP0123456789", sid=sid, tuid=tuid,
            now=self.now if ts is None else ts, **kw))

    def run_report(self, *argv) -> str:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(gate_report.main(list(argv)), 0)
        return out.getvalue()

    def seed(self) -> None:
        """A first, marked run, so later pairs are judged against history."""
        self.add("allowed-receipt", sid="seed", tuid="seed", ts=self.now - 100)
        self.run_report("--new-cases", "--mark-seen")

    def test_counts_only_output(self) -> None:
        self.add("allowed-receipt", sid="SENTINELSESSION", tuid="SENTINELTUID")
        self.add("ran-ok", sid="SENTINELSESSION", tuid="SENTINELTUID", exit=0)
        text = self.run_report()
        self.assertIn(f"{L1}\t{self.mode}\t{self.klass}\tallowed-receipt\t1", text)
        self.assertIn("total\t2", text)
        for sentinel in ("SENTINEL", "SENTINELFP"[:8], decision_row.digest("SENTINELTUID"),
                         decision_row.digest("SENTINELSESSION"), str(self.now)[:8]):
            self.assertNotIn(sentinel, text)

    def test_unregistered_names_masked(self) -> None:
        self.add("ran-ok", tool="evil\ttool; rm -rf", mode="$(x)", klass="bad\nline")
        decision_row.append(self.log, {**decision_row.row("weird\toutcome", now=self.now)})
        text = self.run_report()
        self.assertNotIn("rm -rf", text)
        self.assertNotIn("$(x)", text)
        self.assertNotIn("weird", text)
        self.assertIn("<unregistered>\t<unregistered>\t<unregistered>\tran-ok\t1", text)

    def test_window(self) -> None:
        self.add("ran-ok", ts=self.now - 3 * 3600)
        self.assertIn("total\t0", self.run_report("--since", "1h"))
        self.assertIn("total\t1", self.run_report("--since", "7d"))

    def test_flag_ran_failed_after_allowed_receipt(self) -> None:
        self.seed()
        self.add("allowed-receipt", sid="a", tuid="x")
        self.add("ran-failed", sid="a", tuid="x", exit=1)
        self.add("allowed-override", sid="b", tuid="y")
        self.add("ran-failed", sid="b", tuid="y", exit=1)
        lines = self.run_report("--new-cases").splitlines()
        self.assertEqual(len(lines), 1)
        self.assertIn("outcome=ran-failed", lines[0])

    def test_flag_ran_failed_with_several_gate_rows(self) -> None:
        """One command, two gate rows under one tuid: allowed-receipt then precheck."""
        self.seed()
        self.add("allowed-receipt", sid="a", tuid="x")
        self.add("precheck", sid="a", tuid="x", mode="precheck-form", klass="")
        self.add("ran-failed", sid="a", tuid="x", exit=1)
        lines = [l for l in self.run_report("--new-cases").splitlines() if "outcome=ran-failed" in l]
        self.assertEqual(len(lines), 1)
        self.assertIn(f"tool={L1} mode={self.mode} outcome=ran-failed", lines[0])

    def test_corrupt_seen_does_not_seed(self) -> None:
        self.add("allowed-receipt")
        self.store.mkdir(parents=True, exist_ok=True)
        gate_report.seen_path().write_text("{not json")
        text = self.run_report("--new-cases")
        self.assertIn("seen-state unreadable", text)
        self.assertIn(f"tool={L1} mode={self.mode}", text)

    def test_non_object_seen_does_not_seed(self) -> None:
        self.add("allowed-receipt")
        self.store.mkdir(parents=True, exist_ok=True)
        gate_report.seen_path().write_text("[1, 2]")
        text = self.run_report("--new-cases")
        self.assertIn("seen-state unreadable", text)
        self.assertIn(f"tool={L1} mode={self.mode}", text)

    def test_unreadable_seen_does_not_seed(self) -> None:
        """Present but unreadable (here a directory: an OSError that is not
        FileNotFoundError) is never a first run."""
        self.add("allowed-receipt")
        gate_report.seen_path().mkdir(parents=True)
        text = self.run_report("--new-cases")
        self.assertIn("seen-state unreadable", text)
        self.assertIn(f"tool={L1} mode={self.mode}", text)

    def test_missing_seen_seeds_silently(self) -> None:
        self.add("allowed-receipt")
        self.assertEqual(self.run_report("--new-cases"), "")

    def test_unwritable_seen_is_visible(self) -> None:
        self.add("allowed-receipt")
        with mock.patch.object(gate_report, "write_seen", side_effect=PermissionError(13, "Permission denied")):
            text = self.run_report("--new-cases", "--mark-seen")
        self.assertIn("seen-state not written", text)

    def test_flag_backstop(self) -> None:
        self.seed()
        self.add("refused-backstop", tuid=None, reason="no-receipt")
        self.assertIn("outcome=refused-backstop", self.run_report("--new-cases"))

    def test_flag_error(self) -> None:
        self.seed()
        self.add("error", tool=None, mode="", klass="", reason="fault")
        self.assertIn("outcome=error", self.run_report("--new-cases"))

    def test_flag_new_pair(self) -> None:
        self.seed()
        other = "tools/gh/kill_check.py"
        mode = _first_mode(other)
        self.add("allowed-receipt", tool=other, mode=mode["name"], klass=mode["class"])
        text = self.run_report("--new-cases")
        self.assertIn(f"tool={other} mode={mode['name']}", text)

    def test_first_run_seeds_pairs(self) -> None:
        self.add("allowed-receipt")
        self.assertEqual(self.run_report("--new-cases", "--mark-seen"), "")
        seen = json.loads((self.store / "report-seen.json").read_text())
        self.assertIn(f"{L1}|{self.mode}", seen["pairs"])

    def test_mark_seen_prints_once(self) -> None:
        self.seed()
        self.add("refused-backstop", tuid=None, reason="no-receipt")
        self.assertNotEqual(self.run_report("--new-cases", "--mark-seen"), "")
        self.assertEqual(self.run_report("--new-cases", "--mark-seen"), "")

    def test_capture_line_shape(self) -> None:
        self.seed()
        self.add("refused-backstop", tuid=None, reason="no-receipt")
        line = self.run_report("--new-cases").strip()
        self.assertIn("capture_failure.py", line)
        self.assertNotIn("gate_receipt", line)
        self.assertNotIn("SENTINEL", line)
        command = line.split("-> ", 1)[1]
        self.assertIsNone(precheck_store_guard.guard({"tool_name": "Bash", "tool_input": {"command": command}}))

    def test_guard_allows_report_command(self) -> None:
        sys.path.insert(0, str(FORGE / "tools" / "lane"))
        import belt_plan  # noqa: PLC0415
        self.assertIsNone(precheck_store_guard.guard(
            {"tool_name": "Bash", "tool_input": {"command": belt_plan.TICK_COMMAND}}))

    def test_report_state_outside_forge(self) -> None:
        self.add("allowed-receipt")
        self.run_report("--new-cases", "--mark-seen")
        for path in (gate_report.seen_path(), self.log):
            self.assertTrue(path.exists())
            self.assertFalse(str(path.resolve()).startswith(str(FORGE)))
        with mock.patch.dict(os.environ, {"PRECHECK_GATE_DIR": "", "PREFLIGHT_RECEIPT_DIR": ""}):
            self.assertFalse(str(gate_report.seen_path()).startswith(str(FORGE)))


if __name__ == "__main__":
    unittest.main()
