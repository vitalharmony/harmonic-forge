"""harmonic-forge#1448 AC1 join key and AC2: the gate threads the hashed
`tool_use_id` onto its rows, and a tool-side refusal is a `refused-backstop`
row. Synthetic fixtures only: a temp git repo, a temp store."""
from __future__ import annotations

import contextlib
import io
import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
FORGE = HERE.parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(FORGE / "tools" / "gh"))
import decision_row  # noqa: E402
import gate  # noqa: E402
import gate_receipt as R  # noqa: E402
from test_gate_receipt import L1, L1_ARGS, L1_CMD, L1_REAL, SESSION, Store  # noqa: E402

#: The only keys any row may carry (AC1 row discipline).
ROW_KEYS = {"ts", "outcome", "tool", "mode", "class", "fp", "session", "tuid", "exit", "reason"}


class Telemetry(Store):
    def rows(self) -> list[dict]:
        path = self.root / "store" / "decisions.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines()]

    def gate_with(self, command: str, tuid: str | None) -> str | None:
        payload = {"tool_name": "Bash", "tool_input": {"command": command},
                   "session_id": SESSION, "cwd": str(self.repo)}
        if tuid:
            payload["tool_use_id"] = tuid
        return gate.decide(payload)

    def test_gate_rows_carry_hashed_tuid(self) -> None:
        self.record(L1, L1_ARGS)
        self.assertIsNone(self.gate_with(L1_CMD, "toolu_abc"))
        row = self.rows()[-1]
        self.assertEqual(row["outcome"], "allowed-receipt")
        self.assertEqual(row["tuid"], decision_row.digest("toolu_abc"))
        self.assertNotIn("toolu_abc", json.dumps(row))
        self.assertEqual(set(row), ROW_KEYS)

    def test_gate_writes_call_file_beside_row(self) -> None:
        """Sticky-wicket D: the recorder's join reads this file, not the log."""
        self.record(L1, L1_ARGS)
        self.assertIsNone(self.gate_with(L1_CMD, "toolu_call"))
        call = decision_row.call_path(self.root / "store", decision_row.digest(SESSION),
                                      decision_row.digest("toolu_call"))
        self.assertEqual(json.loads(call.read_text()), self.rows()[-1])

    def test_call_file_fault_never_changes_decision(self) -> None:
        self.record(L1, L1_ARGS)
        with mock.patch.object(decision_row.tempfile, "mkstemp", side_effect=OSError("full")):
            self.assertIsNone(self.gate_with(L1_CMD, "toolu_full"))
        self.assertEqual(self.rows()[-1]["outcome"], "allowed-receipt")

    def test_fault_reason(self) -> None:
        with mock.patch.object(gate, "_calls_in", side_effect=RuntimeError("boom")):
            self.gate_with("echo hi", "t2")
        self.assertEqual([(r["outcome"], r["reason"], r["tuid"]) for r in self.rows()],
                         [("error", "fault", decision_row.digest("t2"))])

    def test_parse_reason(self) -> None:
        with mock.patch.object(gate, "_calls_in", return_value=([], [], False)):
            self.gate_with("anything", "t3")
        self.assertEqual([(r["outcome"], r["reason"]) for r in self.rows()], [("error", "parse")])

    def test_fail_open_then_backstop_pair(self) -> None:
        with mock.patch.object(gate, "_calls_in", side_effect=RuntimeError("boom")):
            self.assertIsNone(self.gate_with(L1_CMD, "t4"))
        self.assertIsNotNone(R.consume(L1, L1_REAL, cwd=self.repo))
        rows = self.rows()
        self.assertEqual([r["outcome"] for r in rows], ["error", "refused-backstop"])
        back = rows[1]
        self.assertEqual(back["session"], rows[0]["session"])
        self.assertEqual((back["tool"], back["reason"], back["tuid"]), (L1, "no-receipt", None))
        self.assertEqual(len(back["fp"]), 12)
        self.assertEqual(set(back), ROW_KEYS)

    def test_extra_moved_reason(self) -> None:
        argv = ["--check", str(self.repo), "1", "main"]
        self.record("tools/lane/lane1_rebase.py", argv, extra="old|main")
        self.assertIsNotNone(R.consume("tools/lane/lane1_rebase.py", argv[1:], cwd=self.repo, extra="new|main"))
        self.assertEqual([(r["outcome"], r["reason"]) for r in self.rows()],
                         [("refused-backstop", "extra-moved")])

    def test_allowed_consume_writes_no_row(self) -> None:
        self.record(L1, L1_ARGS)
        self.assertIsNone(R.consume(L1, L1_REAL, cwd=self.repo))
        self.assertEqual(self.rows(), [])

    def _deadline_rows(self) -> list[dict]:
        return [r for r in self.rows() if r["reason"] == "deadline"]

    def test_cli_deadline_row(self) -> None:
        with mock.patch.object(R, "fingerprint", side_effect=R.Deadline("late")), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(R.main(["consume", "--tool", "tools/lane/lanes-claude", "--",
                                     "--session", "x"]), 1)
        rows = self._deadline_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["outcome"], rows[0]["tool"]), ("refused-backstop", "tools/lane/lanes-claude"))

    def test_preflight_require_deadline_row(self) -> None:
        import _preflight  # noqa: PLC0415
        with mock.patch.object(R, "fingerprint", side_effect=R.Deadline("late")):
            self.assertIn("could not be checked", _preflight.require(L1, L1_REAL, cwd=self.repo))
        rows = self._deadline_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["tool"], L1)

    def test_state_stays_under_store_dir(self) -> None:
        """AC5: rows land in the store, never inside forge."""
        with mock.patch.object(gate, "_calls_in", return_value=([], [], False)):
            self.gate_with("x", "t5")
        self.assertTrue((self.root / "store" / "decisions.jsonl").exists())
        self.assertFalse(str(R.store_dir()).startswith(str(FORGE)))

    def test_no_network_in_telemetry_paths(self) -> None:
        done = subprocess.run(
            ["git", "grep", "-n", "-e", "urlopen", "-e", r"requests\.", "-e", "gh api", "--",
             "tools/precheck/decision_row.py", "tools/precheck/gate_report.py",
             "tools/hooks/record_precheck_outcome.py"],
            cwd=FORGE, capture_output=True, text=True, check=False)
        self.assertEqual(done.stdout, "")

    def test_head_move_does_not_matter_to_tuid(self) -> None:
        """KC1b: the tuid on the gate row is independent of live state."""
        self.record(L1, L1_ARGS)
        self.gate_with(L1_CMD, "t6")
        subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", "b"], cwd=self.repo, check=True)
        self.assertEqual(self.rows()[-1]["tuid"], decision_row.digest("t6"))


if __name__ == "__main__":
    unittest.main()
