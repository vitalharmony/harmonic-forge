#!/usr/bin/env python3
"""harmonic-forge#1281 AC1, AC2, AC8: `PARK <KEY>` writes a session-bound grant.

The grant store is a temp dir; nothing here reaches ~/.claude/state/park/.
"""
from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import expand_lane_shorthand as m  # noqa: E402
import park_grant  # noqa: E402

SESSION = "sess-1281"
NOW = datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc)


class ParkTrigger(unittest.TestCase):
    def setUp(self) -> None:
        self.state = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.state, True)
        patcher = mock.patch.object(park_grant, "PARK_STATE_DIR", self.state)
        patcher.start()
        self.addCleanup(patcher.stop)
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("CLAUDE_CODE_ENTRYPOINT", None)

    def park(self, prompt: str, lane: str | None = "1", session: str | None = SESSION) -> str:
        return m.authorize_park(prompt, session, lane, NOW)

    def grant(self, key: str = "H2621", session: str = SESSION) -> dict | None:
        path = self.state / session / f"{key}.json"
        return json.loads(path.read_text()) if path.exists() else None

    def assertNothingWritten(self) -> None:
        self.assertEqual([p for p in self.state.rglob("*.json")], [])

    def test_park_first_line_writes_session_bound_grant(self) -> None:
        for prompt in ("PARK H2621", "PARK H2621: moved to 3.2\nmore text"):
            with self.subTest(prompt=prompt):
                receipt = self.park(prompt)
                self.assertIn("PARK: H2621's open task-list entry may now be completed or "
                              "deleted once in this session", receipt)
                grant = self.grant()
                self.assertEqual((grant["key"], grant["session"], grant["consumed_at"]),
                                 ("H2621", SESSION, None))
                self.assertEqual(grant["line"], prompt.splitlines()[0])
                self.assertEqual(grant["created_at"], "2026-11-07T12:00:00Z")
                self.assertEqual(grant["expires_at"], "2026-11-07T13:00:00Z")
                self.assertIn(grant["expires_at"], receipt)
                self.assertTrue(park_grant.usable(SESSION, "H2621", NOW))

    def test_park_not_first_line_or_lowercase_writes_nothing(self) -> None:
        for prompt in ("please\nPARK H2621", "  PARK H2621", "park H2621", "Park H2621",
                       "PARK H2621 extra"):
            with self.subTest(prompt=prompt):
                receipt = self.park(prompt)
                self.assertIn("PARK REFUSED", receipt)
                self.assertIn("No grant written", receipt)
        self.assertNothingWritten()

    def test_park_in_fence_or_blockquote_writes_nothing(self) -> None:
        for prompt in ("```\nPARK H2621\n```", "> PARK H2621", "~~~\nPARK H2621\n~~~"):
            with self.subTest(prompt=prompt):
                self.assertIn("PARK REFUSED", self.park(prompt))
        # Today's directive_lines never excludes a line that already matched
        # as the first one, so the membership test is pinned directly: a
        # first line directive_lines calls quotation writes nothing.
        with mock.patch.object(m, "directive_lines", lambda prompt: iter(())):
            receipt = self.park("PARK H2621")
        self.assertIn("PARK REFUSED for H2621: a PARK line inside a fenced block", receipt)
        self.assertNothingWritten()

    def test_park_in_task_notification_envelope_writes_nothing(self) -> None:
        receipt = self.park("PARK H2621\n<task-notification>done</task-notification>")
        self.assertIn("PARK REFUSED for H2621", receipt)
        self.assertNothingWritten()

    def test_park_from_non_interactive_entrypoint_writes_nothing(self) -> None:
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_ENTRYPOINT": "sdk-py"}):
            receipt = self.park("PARK H2621")
        self.assertIn("CLAUDE_CODE_ENTRYPOINT", receipt)
        self.assertNothingWritten()

    def test_park_without_lane_writes_nothing(self) -> None:
        for lane in (None, ""):
            with self.subTest(lane=lane):
                self.assertIn("LANE is not set", self.park("PARK H2621", lane=lane))
        self.assertNothingWritten()

    def test_park_with_unknown_prefix_writes_nothing(self) -> None:
        self.assertIn("PARK REFUSED for Z12: no repo prefix", self.park("PARK Z12"))
        self.assertNothingWritten()

    def test_park_without_session_writes_nothing(self) -> None:
        self.assertIn("no session id", self.park("PARK H2621", session=None))
        self.assertNothingWritten()

    def test_no_park_line_returns_empty(self) -> None:
        self.assertEqual(self.park("parking lot H2621 is fine"), "")

    def test_main_puts_the_receipt_in_additional_context(self) -> None:
        payload = {"prompt": "PARK H2621", "session_id": SESSION}
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"LANE": "1"}), \
                mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload))), \
                mock.patch.object(m, "record_attention", lambda *a, **k: False), \
                mock.patch.object(m, "record_attribution", lambda *a, **k: 0), \
                mock.patch("sys.stdout", out):
            m.main()
        context = json.loads(out.getvalue())["hookSpecificOutput"]["additionalContext"]
        self.assertIn("PARK: H2621's open task-list entry", context)
        self.assertIsNotNone(self.grant())


class RealStoreGuard(unittest.TestCase):
    """AC8 (#907/#1257): with the store left at its default, a test process
    never writes it. HOME and REAL_PARK_DIR point at a temp dir so that even a
    broken guard writes there, never to the operator's store."""

    def test_park_never_writes_real_store(self) -> None:
        home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, home, True)
        with mock.patch.dict(os.environ, {"HOME": str(home)}), \
                mock.patch.object(park_grant, "PARK_STATE_DIR", None), \
                mock.patch.object(park_grant, "REAL_PARK_DIR",
                                  home / ".claude" / "state" / "park"):
            self.assertEqual(park_grant.state_dir(), home / ".claude" / "state" / "park")
            with self.assertRaises(OSError):
                park_grant.write("H2621", SESSION, "PARK H2621", NOW)
            receipt = m.authorize_park("PARK H2621", SESSION, "1", NOW)
            self.assertFalse(park_grant.usable(SESSION, "H2621", NOW))
        self.assertIn("PARK REFUSED", receipt)
        self.assertEqual(list(home.rglob("*.json")), [])


if __name__ == "__main__":
    unittest.main()
