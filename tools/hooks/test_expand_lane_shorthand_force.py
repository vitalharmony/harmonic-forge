#!/usr/bin/env python3
"""harmonic-forge#1169 AC1: `FORCE <KEY>: <reason>` writes a pending grant.

The grant store is a temp dir and the head lookup is faked; nothing here
reaches ~/.claude/state/force/ or a real preclose receipt.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "gh"))
import expand_lane_shorthand as m  # noqa: E402
import force_grant  # noqa: E402

HEAD = "c" * 40


class ForceTrigger(unittest.TestCase):
    def setUp(self) -> None:
        self.state = Path(tempfile.mkdtemp())
        for target, attr, value in ((force_grant, "FORCE_STATE_DIR", self.state),
                                    (force_grant, "covered_head", lambda repo, issue: HEAD)):
            patcher = mock.patch.object(target, attr, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        os.environ.pop("CLAUDE_CODE_ENTRYPOINT", None)

    def force(self, prompt: str, lane: str | None = "1") -> str:
        return m.authorize_force(prompt, "sess-7", lane)

    def grant(self, key: str = "F1166") -> dict | None:
        path = self.state / f"{key}.json"
        return json.loads(path.read_text()) if path.exists() else None

    def test_a_typed_first_line_writes_a_pending_grant_bound_to_the_head(self) -> None:
        receipt = self.force("FORCE F1166: both post-verdict findings fixed")
        self.assertIn("pending grant written for F1166", receipt)
        grant = self.grant()
        self.assertEqual((grant["status"], grant["sha"], grant["repo"], grant["issue"]),
                         ("pending", HEAD, "vitalharmony/harmonic-forge", 1166))
        self.assertEqual(grant["reason"], "both post-verdict findings fixed")

    def test_a_provenance_failure_writes_nothing(self) -> None:
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_ENTRYPOINT": "sdk-py"}):
            receipt = self.force("FORCE F1166: reason")
        self.assertIn("FORCE REFUSED", receipt)
        self.assertIn("CLAUDE_CODE_ENTRYPOINT", receipt)
        self.assertIsNone(self.grant())

    def test_quoted_misplaced_lowercase_or_empty_lines_write_nothing(self) -> None:
        for prompt in ("```\nFORCE F1166: reason\n```", "> FORCE F1166: reason",
                       "please\nFORCE F1166: reason", "  FORCE F1166: reason",
                       "force F1166: reason", "FORCE F1166:"):
            with self.subTest(prompt=prompt):
                self.assertIn("FORCE REFUSED", self.force(prompt))
                self.assertIsNone(self.grant())

    def test_lane_other_than_one_and_a_foreign_key_write_nothing(self) -> None:
        self.assertIn("LANE is '2'", self.force("FORCE F1166: reason", lane="2"))
        self.assertIn("cross-account", self.force("FORCE K12: reason"))
        self.assertIsNone(self.grant())
        self.assertIsNone(self.grant("K12"))

    def test_no_preclose_receipt_writes_nothing(self) -> None:
        with mock.patch.object(force_grant, "covered_head", lambda repo, issue: None):
            self.assertIn("no preclose receipt", self.force("FORCE F1166: reason"))
        self.assertIsNone(self.grant())

    def test_a_prompt_without_force_is_silent(self) -> None:
        self.assertEqual(self.force("BATCH F1166"), "")


if __name__ == "__main__":
    unittest.main()
