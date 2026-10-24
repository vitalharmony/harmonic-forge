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
        G.consume(KEY, self.require()["granted_at"], SHA, NOW)
        with self.assertRaisesRegex(G.GrantRefused, "consumed"):
            self.require()


class GrantIdentityTests(Store):
    """Preclose pass 1, cluster 1: a grant write never lands on a grant it did not read."""

    def test_consume_refuses_a_grant_replaced_after_require(self):
        """Finding 1: a new FORCE line between require and consume is not spent."""
        self.recorded()
        token = G.require(REPO, ISSUE, SHA, NOW)["granted_at"]
        self.pending(now=NOW + timedelta(minutes=1))
        with self.assertRaisesRegex(G.GrantRefused, "replaced"):
            G.consume(KEY, token, SHA, NOW)
        self.assertEqual(json.loads(G.grant_path(KEY).read_text())["status"], "pending")

    def test_record_refuses_when_the_grant_is_replaced_during_the_post(self):
        """Finding 2: a corrected FORCE line during the post is not overwritten."""
        self.pending()

        def run(argv, **_kw):
            if "--method" in argv:
                holder["body"] = Path(argv[argv.index("-F") + 1].split("@", 1)[1]).read_text()
                self.pending(now=NOW + timedelta(minutes=1))  # the operator's corrected line
                return _done("https://github.com/vitalharmony/harmonic-forge/issues/1166#issuecomment-77")
            return _done(holder["body"])

        holder: dict = {}
        with self.assertRaisesRegex(SystemExit, "replaced during the post"):
            G.record(KEY, now=NOW, runner=run)
        newer = json.loads(G.grant_path(KEY).read_text())
        self.assertEqual((newer["status"], newer["granted_at"]), ("pending", "2026-10-23T12:01:00Z"))

    def test_a_late_record_never_revives_a_consumed_grant(self):
        """Finding 5: an overlapping record after consumption leaves it consumed."""
        self.pending()

        def run(argv, **_kw):
            if "--method" in argv:
                holder["body"] = Path(argv[argv.index("-F") + 1].split("@", 1)[1]).read_text()
                return _done("https://github.com/vitalharmony/harmonic-forge/issues/1166#issuecomment-77")
            # While this slow record waits on its refetch, another record and a
            # forced completion run to the end.
            G.record(KEY, now=NOW, runner=self.runner({}))
            G.consume(KEY, G.require(REPO, ISSUE, SHA, NOW)["granted_at"], SHA, NOW)
            return _done(holder["body"])

        holder: dict = {}
        G.record(KEY, now=NOW, runner=run)
        self.assertEqual(json.loads(G.grant_path(KEY).read_text())["status"], "consumed")
        with self.assertRaisesRegex(G.GrantRefused, "consumed"):
            G.require(REPO, ISSUE, SHA, NOW)


class ForceHeadEpochTests(unittest.TestCase):
    """F1169 pass 2: a post-verdict check carried across a reforge never binds."""

    def test_a_carried_post_verdict_check_yields_to_the_current_pass(self):
        import preclose_passes  # noqa: PLC0415
        receipt = {"reviewed_sha": "b" * 40, "status": "complete",
                   "pass_history": [{"sha": "a" * 40, "surviving": 2, "epoch": 0},
                                    {"sha": "a" * 40, "surviving": 2, "epoch": 0},
                                    {"sha": "b" * 40, "surviving": 1, "epoch": 1},
                                    {"sha": "b" * 40, "surviving": 1, "epoch": 1}],
                   "post_verdict_check": {"base_sha": "a" * 40, "head_sha": "c" * 40}}
        self.assertEqual(preclose_passes.force_head(receipt), "b" * 40)
        receipt["post_verdict_check"] = {"base_sha": "b" * 40, "head_sha": "d" * 40}
        self.assertEqual(preclose_passes.force_head(receipt), "d" * 40)


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



class ModeBindingTests(Store):
    """harmonic-forge#1174 AC2: each override needs a grant of its own mode."""

    def usable(self, mode: str = "plain", sha: str | None = SHA, prefix: str | None = None) -> dict:
        G.write_pending(KEY, REPO, ISSUE, sha, "r", "s1", "FORCE line", NOW, mode=mode, sha_prefix=prefix)
        return G.record(KEY, now=NOW, runner=self.runner({}))

    def test_plain_grant_does_not_authorize_reforge(self):
        self.usable()
        with self.assertRaises(G.GrantRefused) as ctx:
            G.require(REPO, ISSUE, SHA, NOW, mode="reforge")
        self.assertIn("plain force, not a reforge one", str(ctx.exception))

    def test_reforge_grant_does_not_authorize_plain_force(self):
        self.usable("reforge", None, SHA[:12])
        with self.assertRaises(G.GrantRefused):
            G.require(REPO, ISSUE, SHA, NOW)

    def test_reforge_grant_matches_its_prefix_only(self):
        self.usable("reforge", None, SHA[:12])
        self.assertEqual(G.require(REPO, ISSUE, SHA, NOW, mode="reforge")["mode"], "reforge")

    def test_reforge_grant_for_another_head_is_refused(self):
        self.usable("reforge", None, SHA[:12])
        with self.assertRaises(G.GrantRefused) as ctx:
            G.require(REPO, ISSUE, MOVED, NOW, mode="reforge")
        self.assertIn("names aaaaaaaaaaaa", str(ctx.exception))

    def test_cluster_grant_binds_the_full_sha(self):
        self.usable("cluster")
        self.assertEqual(G.require(REPO, ISSUE, SHA, NOW, mode="cluster")["mode"], "cluster")
        with self.assertRaises(G.GrantRefused):
            G.require(REPO, ISSUE, MOVED, NOW, mode="cluster")

    def test_a_grant_with_no_mode_reads_as_plain(self):
        self.usable()
        path = G.grant_path(KEY)
        data = json.loads(path.read_text()); data.pop("mode", None); data.pop("sha_prefix", None)
        path.write_text(json.dumps(data))
        self.assertEqual(G.require(REPO, ISSUE, SHA, NOW)["key"], KEY)
        with self.assertRaises(G.GrantRefused):
            G.require(REPO, ISSUE, SHA, NOW, mode="reforge")

    def test_consume_checks_the_mode_too(self):
        grant = self.usable("cluster")
        with self.assertRaises(G.GrantRefused):
            G.consume(KEY, grant["granted_at"], SHA, NOW, mode="plain")
        self.assertEqual(G.consume(KEY, grant["granted_at"], SHA, NOW, mode="cluster")["status"], "consumed")

if __name__ == "__main__":
    unittest.main()
