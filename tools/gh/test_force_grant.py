#!/usr/bin/env python3
"""harmonic-forge#1169: the FORCE grant store, its record step, and its use
by preclose_check's plain --force. The store and GitHub are faked; nothing
reaches the operator's ~/.claude/state/force/."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "hooks"))

import force_grant as G  # noqa: E402
import preclose_check as P  # noqa: E402

REPO, ISSUE, KEY = "vitalharmony/harmonic-forge", 1166, "F1166"
SHA, MOVED = "a" * 40, "b" * 40
NOW = datetime(2026, 10, 23, 12, 0, tzinfo=timezone.utc)


def _done(stdout: str = "", code: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], code, stdout, "")


class Store(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patcher = mock.patch.object(G, "FORCE_STATE_DIR", Path(tmp.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        env = mock.patch.dict(os.environ, {"LANE": "1"})
        env.start()
        self.addCleanup(env.stop)

    def pending(self, sha: str = SHA, now: datetime = NOW) -> dict:
        return G.write_pending(KEY, REPO, ISSUE, sha, "both findings fixed", "s1",
                               f"FORCE {KEY}: both findings fixed", now)

    def runner(self, body_holder: dict, fail_refetch: bool = False):
        def run(argv, **_kw):
            if "--method" in argv:
                body_holder["body"] = Path(argv[argv.index("-F") + 1].split("@", 1)[1]).read_text()
                return _done("https://github.com/vitalharmony/harmonic-forge/issues/1166#issuecomment-77")
            return _done("" if fail_refetch else body_holder["body"])
        return run

    def recorded(self) -> dict:
        self.pending()
        return G.record(KEY, now=NOW, runner=self.runner({}))


class RecordTests(Store):
    """AC2: public before usable."""

    def test_record_posts_refetches_and_marks_usable(self):
        grant = self.recorded()
        self.assertEqual(grant["status"], "usable")
        self.assertEqual(grant["comment_id"], "77")

    def test_a_refetch_failure_leaves_it_pending_and_a_retry_completes(self):
        self.pending()
        with self.assertRaises(SystemExit):
            G.record(KEY, now=NOW, runner=self.runner({}, fail_refetch=True))
        self.assertEqual(json.loads(G.grant_path(KEY).read_text())["status"], "pending")
        self.assertEqual(G.record(KEY, now=NOW, runner=self.runner({}))["status"], "usable")

    def test_record_refuses_outside_lane_1_and_with_no_grant(self):
        with self.assertRaises(SystemExit):
            G.record(KEY, now=NOW, runner=self.runner({}))
        self.pending()
        with mock.patch.dict(os.environ, {"LANE": "2"}), self.assertRaises(SystemExit):
            G.record(KEY, now=NOW, runner=self.runner({}))


class RequireTests(Store):
    """AC3: bound, single use, narrow."""

    def require(self, sha: str = SHA, now: datetime = NOW):
        return G.require(REPO, ISSUE, sha, now)

    def test_a_usable_grant_for_the_head_is_accepted(self):
        self.recorded()
        self.assertEqual(self.require()["key"], KEY)

    def test_no_grant_pending_expired_consumed_and_moved_head_are_refused(self):
        with self.assertRaisesRegex(G.GrantRefused, "no FORCE grant"):
            self.require()
        self.pending()
        with self.assertRaisesRegex(G.GrantRefused, "pending"):
            self.require()
        G.record(KEY, now=NOW, runner=self.runner({}))
        with self.assertRaisesRegex(G.GrantRefused, "head moved"):
            self.require(MOVED)
        with self.assertRaisesRegex(G.GrantRefused, "expired"):
            self.require(now=NOW + timedelta(hours=G.TTL_HOURS + 1))
        G.consume(KEY, SHA, NOW)
        with self.assertRaisesRegex(G.GrantRefused, "consumed"):
            self.require()


class PrecloseGateTests(Store):
    """AC3/AC4 inside preclose_check: the gate and the event."""

    def test_require_force_grant_refuses_with_no_grant(self):
        with self.assertRaises(SystemExit) as refused:
            P.require_force_grant(REPO, ISSUE, SHA)
        self.assertIn("#1169", str(refused.exception))

    def test_the_force_event_carries_the_comment_id_and_hash_never_the_reason(self):
        grant = self.recorded()
        events: list = []
        fake_emit = mock.MagicMock()
        fake_emit.emit = lambda batch: events.extend(batch) or {}
        fake_archive = mock.MagicMock()
        fake_archive.origin_for_repo.return_value = mock.MagicMock(account="vitalharmony", org="vitalharmony")
        with mock.patch.dict(sys.modules, {"emit": fake_emit, "archive": fake_archive}):
            P.emit_force(REPO, ISSUE, SHA, "force", 2, grant)
            P.emit_force(REPO, ISSUE, SHA, "reforge", 1, None)
        forced, reforged = events
        self.assertEqual(forced["event_type"], "preclose.force")
        self.assertEqual((forced["attrs"]["granted"], forced["attrs"]["reason_comment_id"]), (True, "77"))
        self.assertEqual(forced["attrs"]["reason_sha256"], G.reason_sha256("both findings fixed"))
        self.assertNotIn("both findings fixed", json.dumps(events))
        self.assertEqual((reforged["attrs"]["granted"], reforged["attrs"]["mode"]), (False, "reforge"))


if __name__ == "__main__":
    unittest.main()
