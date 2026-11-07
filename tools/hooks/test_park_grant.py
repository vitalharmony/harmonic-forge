#!/usr/bin/env python3
"""harmonic-forge#1281: the PARK grant store (`park_grant.py`).

Every test points `PARK_STATE_DIR` at a temp dir; nothing here reaches
~/.claude/state/park/.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import park_grant  # noqa: E402

SESSION = "sess-1281"
NOW = datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc)


class Store(unittest.TestCase):
    def setUp(self) -> None:
        self.state = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.state, True)
        patcher = mock.patch.object(park_grant, "PARK_STATE_DIR", self.state)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_usable_until_expiry_then_not(self) -> None:
        park_grant.write("H1", SESSION, "PARK H1", NOW)
        self.assertTrue(park_grant.usable(SESSION, "H1", NOW + timedelta(minutes=59)))
        self.assertFalse(park_grant.usable(SESSION, "H1", NOW + timedelta(hours=1)))

    def test_consume_marks_and_keeps_the_file(self) -> None:
        park_grant.write("H1", SESSION, "PARK H1", NOW)
        park_grant.consume(SESSION, "H1", NOW)
        data = json.loads((self.state / SESSION / "H1.json").read_text())
        self.assertEqual(data["consumed_at"], "2026-11-07T12:00:00Z")
        self.assertFalse(park_grant.usable(SESSION, "H1", NOW))
        with self.assertRaises(OSError):
            park_grant.consume(SESSION, "H1", NOW)

    def test_consume_without_grant_raises(self) -> None:
        with self.assertRaises(OSError):
            park_grant.consume(SESSION, "H1", NOW)

    def test_a_new_line_replaces_a_consumed_grant(self) -> None:
        park_grant.write("H1", SESSION, "PARK H1", NOW)
        park_grant.consume(SESSION, "H1", NOW)
        park_grant.write("H1", SESSION, "PARK H1", NOW)
        self.assertTrue(park_grant.usable(SESSION, "H1", NOW))

    def test_unreadable_or_invalid_file_is_not_usable(self) -> None:
        path = self.state / SESSION / "H1.json"
        path.parent.mkdir(parents=True)
        path.write_text("{not json")
        self.assertFalse(park_grant.usable(SESSION, "H1", NOW))
        path.write_text(json.dumps({"key": "H1", "session": SESSION, "consumed_at": None,
                                    "expires_at": "garbage"}))
        self.assertFalse(park_grant.usable(SESSION, "H1", NOW))

    def test_session_or_key_that_cannot_name_a_path_is_refused(self) -> None:
        for session, key in (("../x", "H1"), ("", "H1"), (SESSION, "H1/../x"), (".", "H1")):
            with self.subTest(session=session, key=key):
                with self.assertRaises(ValueError):
                    park_grant.write(key, session, "PARK H1", NOW)
                self.assertFalse(park_grant.usable(session, key, NOW))
        self.assertEqual(list(self.state.rglob("*.json")), [])

    def test_prune_drops_week_old_grants_and_empty_dirs(self) -> None:
        park_grant.write("H1", "old-session", "PARK H1", NOW)
        old = time.time() - 8 * 86400
        for path in (self.state / "old-session").iterdir():
            os.utime(path, (old, old))
        park_grant.write("H2", SESSION, "PARK H2", NOW)
        self.assertFalse((self.state / "old-session").exists())
        self.assertTrue((self.state / SESSION / "H2.json").exists())


if __name__ == "__main__":
    unittest.main()
