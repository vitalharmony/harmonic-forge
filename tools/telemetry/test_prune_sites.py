#!/usr/bin/env python3
"""Every harmonic-forge#826 archive site, and the guards preclose found missing.

One test per site that had none (preclose test-honesty finding 4), plus the
override sentinel, the failure log and the hard ceiling (silent-bypass and
fail-direction findings), the export's routing and per-run atomicity
(second-run finding 1, test-honesty finding 2), and the retention plan.
"""
from __future__ import annotations

import gzip
import json
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
TOOLS = HERE.parent
for sub in ("telemetry", "hooks", "gh", "lane", "onboard"):
    if str(TOOLS / sub) not in sys.path:
        sys.path.insert(0, str(TOOLS / sub))

import archive  # noqa: E402
import actions_retention  # noqa: E402
import belt_candidates  # noqa: E402
import ci_history_export as cix  # noqa: E402
import compaction_marker  # noqa: E402
import enforce_belt_arming  # noqa: E402
import handoff_owed  # noqa: E402
import lane3_audit  # noqa: E402
import preclose_check  # noqa: E402


class _Root(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.root = archive.make_test_root(self.tmp / "archive")
        env = mock.patch.dict(os.environ, {archive.ROOT_ENV: str(self.root),
                                           "XDG_STATE_HOME": str(self.tmp / "state")})
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(self._tmp.cleanup)

    def archived(self, source: str) -> list[dict]:
        rows: list[dict] = []
        for part in sorted(self.root.rglob(f"{source}/*.jsonl.gz")):
            with gzip.open(part, "rt", encoding="utf-8") as handle:
                rows.extend(json.loads(line) for line in handle if line.strip())
        return rows

    def failures(self) -> list[dict]:
        log = archive.failure_log()
        if not log.exists():
            return []
        return [json.loads(l) for l in log.read_text(encoding="utf-8").splitlines() if l.strip()]


class OverrideSentinelTests(_Root):
    def test_an_override_without_the_sentinel_is_refused_and_nothing_is_written(self):
        bare = self.tmp / "bare"
        bare.mkdir()
        with mock.patch.dict(os.environ, {archive.ROOT_ENV: str(bare)}):
            self.assertEqual(archive.archive("belt", [{"a": 1}]), 0)
        self.assertEqual(list(bare.rglob("*.gz")), [])
        self.assertTrue(any("ArchiveRootRefused" in f["reason"] for f in self.failures()))

    def test_a_failure_is_recorded_outside_the_archive_root(self):
        with mock.patch.object(archive, "partition", side_effect=OSError("disk full")):
            self.assertEqual(archive.archive("belt", [{"a": 1}]), 0)
        self.assertEqual(self.failures()[-1]["source"], "belt")
        self.assertFalse(str(archive.failure_log()).startswith(str(self.root)))


class HardCeilingTests(_Root):
    def test_below_the_ceiling_a_failed_archive_keeps_everything(self):
        log = self.tmp / "lane3-audit.jsonl"
        with mock.patch.object(lane3_audit, "MAX_RECORDS", 5), \
                mock.patch.object(lane3_audit, "_archive", return_value=0):
            for i in range(20):
                lane3_audit.record("e", "o", target=str(i), path=log)
        self.assertEqual(len(log.read_text().splitlines()), 20)

    def test_past_the_ceiling_it_trims_and_records_the_forced_loss(self):
        log = self.tmp / "lane3-audit.jsonl"
        with mock.patch.object(lane3_audit, "MAX_RECORDS", 2), \
                mock.patch.object(lane3_audit, "_archive", return_value=0):
            for i in range(25):
                lane3_audit.record("e", "o", target=str(i), path=log)
        self.assertLessEqual(len(log.read_text().splitlines()), 2 * archive.HARD_CEILING_FACTOR)
        forced = [f for f in self.failures() if f["forced_loss"] > 0]
        self.assertTrue(forced)
        self.assertEqual(forced[0]["source"], "lane3-audit")


class HandoffOwedTests(_Root):
    def setUp(self) -> None:
        super().setUp()
        self.owed = self.tmp / "owed"
        self.owed.mkdir()
        patcher = mock.patch.object(handoff_owed, "OWED_DIR", self.owed)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _age(self, path: Path) -> None:
        old = time.time() - (handoff_owed.PRUNE_AFTER_DAYS + 1) * 86400
        os.utime(path, (old, old))

    def test_an_expired_owed_file_is_archived_then_removed(self):
        f = self.owed / "sess.json"
        f.write_text(json.dumps([{"repo": "vitalharmony/hrse", "issue": 7}]), encoding="utf-8")
        self._age(f)
        handoff_owed.prune()
        self.assertFalse(f.exists())
        self.assertEqual(self.archived("handoff-owed")[0]["record"]["issue"], 7)

    def test_a_corrupt_owed_file_is_archived_raw_not_silently_deleted(self):
        f = self.owed / "sess.json"
        f.write_text('[{"repo": "vitalharmony/hrse", "issue": 9}', encoding="utf-8")
        self._age(f)
        handoff_owed.prune()
        self.assertFalse(f.exists())
        self.assertIn('"issue": 9', self.archived("handoff-owed")[0]["record"]["content"])

    def test_a_corrupt_owed_file_is_kept_when_the_archive_fails(self):
        f = self.owed / "sess.json"
        f.write_text("not json", encoding="utf-8")
        self._age(f)
        with mock.patch.object(archive, "archive", return_value=0):
            handoff_owed.prune()
        self.assertTrue(f.exists())


class CompactionMarkerTests(_Root):
    def test_a_ttl_expired_marker_is_archived_then_removed(self):
        markers = self.tmp / "markers"
        markers.mkdir()
        stamp = datetime.now(timezone.utc) - timedelta(seconds=compaction_marker.TTL_SECONDS + 60)
        m = markers / "s.json"
        m.write_text(json.dumps({"compacted_at": stamp.isoformat(), "cwd": "/x"}), encoding="utf-8")
        with mock.patch.object(compaction_marker, "MARKER_DIR", markers):
            compaction_marker.prune_markers(time.time())
        self.assertFalse(m.exists())
        self.assertEqual(self.archived("compaction-marker")[0]["record"]["reason"], "ttl-prune")

    def test_the_marker_is_kept_when_the_archive_fails(self):
        markers = self.tmp / "markers"
        markers.mkdir()
        stamp = datetime.now(timezone.utc) - timedelta(seconds=compaction_marker.TTL_SECONDS + 60)
        m = markers / "s.json"
        m.write_text(json.dumps({"compacted_at": stamp.isoformat()}), encoding="utf-8")
        with mock.patch.object(compaction_marker, "MARKER_DIR", markers), \
                mock.patch.object(archive, "archive", return_value=0):
            compaction_marker.prune_markers(time.time())
        self.assertTrue(m.exists())


class BeltArmingTests(_Root):
    def test_an_expired_grant_is_archived_and_still_removed(self):
        arming = self.tmp / "arming"
        with mock.patch.dict(os.environ, {enforce_belt_arming.ARMING_DIR_ENV: str(arming)}):
            sid = "0123abcd-0000-0000-0000-000000000000"
            enforce_belt_arming.write_grant(sid, "test", now=time.time() - 10_000)
            self.assertIsNone(enforce_belt_arming.read_grant(sid))
            self.assertFalse(enforce_belt_arming.grant_path(sid).exists())
        reasons = [r["record"]["reason"] for r in self.archived("belt-arming-state")]
        self.assertEqual(reasons, ["grant-expired"])

    def test_a_failed_archive_never_leaves_an_expired_grant_usable(self):
        """Semantic delete: the unlink happens whatever the archive returns."""
        arming = self.tmp / "arming"
        with mock.patch.dict(os.environ, {enforce_belt_arming.ARMING_DIR_ENV: str(arming)}), \
                mock.patch.object(archive, "archive", return_value=0):
            sid = "0123abcd-0000-0000-0000-000000000001"
            enforce_belt_arming.write_grant(sid, "test", now=time.time() - 10_000)
            self.assertIsNone(enforce_belt_arming.read_grant(sid))
            self.assertFalse(enforce_belt_arming.grant_path(sid).exists())


class BeltCandidateTests(_Root):
    def test_an_aged_candidate_is_archived_then_pruned(self):
        base = self.tmp / "candidates"
        belt_candidates.record_candidate("vitalharmony/hrse", 5, "handoff", "l1", base_dir=base)
        later = datetime.now(timezone.utc) + timedelta(days=belt_candidates.DEFAULT_MAX_AGE_DAYS + 1)
        belt_candidates.read_candidates(["vitalharmony/hrse"], "l2", queue_kinds={"l2": ("handoff",)},
                                        queue_posters={"l2": ("l1",)}, now=later, base_dir=base,
                                        prune=True)
        self.assertEqual(list(base.glob("*.json")), [])
        self.assertEqual(self.archived("belt-candidates")[0]["record"]["issue"], 5)

    def test_the_candidate_is_kept_when_the_archive_fails(self):
        base = self.tmp / "candidates"
        belt_candidates.record_candidate("vitalharmony/hrse", 6, "handoff", "l1", base_dir=base)
        later = datetime.now(timezone.utc) + timedelta(days=belt_candidates.DEFAULT_MAX_AGE_DAYS + 1)
        with mock.patch.object(archive, "archive", return_value=0):
            belt_candidates.read_candidates(["vitalharmony/hrse"], "l2",
                                            queue_kinds={"l2": ("handoff",)},
                                            queue_posters={"l2": ("l1",)}, now=later,
                                            base_dir=base, prune=True)
        self.assertEqual(len(list(base.glob("*.json"))), 1)


class PrecloseReceiptTests(_Root):
    def test_a_superseded_receipt_is_archived(self):
        with mock.patch.object(preclose_check, "receipt_dir", return_value=self.tmp / "preclose"):
            preclose_check.write_receipt("vitalharmony/harmonic-forge", 1, "a" * 40, 5, "planned")
            preclose_check.write_receipt("vitalharmony/harmonic-forge", 1, "b" * 40, 5, "complete")
        rows = self.archived("preclose-receipts")
        self.assertEqual([r["record"]["reviewed_sha"] for r in rows], ["a" * 40])


class ExportRoutingTests(_Root):
    def test_skip_is_keyed_on_the_retention_set_not_the_plan_maximum(self):
        manifest = self.tmp / "projects.toml"
        manifest.write_text('[[project]]\nname = "a"\nprefix = "A"\nrepo = "o/a"\naccount = "acct"\n',
                            encoding="utf-8")
        with mock.patch.object(cix, "retention", return_value={"days": 90, "maximum_allowed_days": 400}), \
                mock.patch.object(cix, "export_repo", return_value={"runs": 0}) as export, \
                mock.patch("manifest_identity.apply_project_identity"):
            cix.main(["--manifest", str(manifest)])
        export.assert_called_once()

    def test_unreadable_retention_is_exported_not_skipped(self):
        manifest = self.tmp / "projects.toml"
        manifest.write_text('[[project]]\nname = "a"\nprefix = "A"\nrepo = "o/a"\naccount = "acct"\n',
                            encoding="utf-8")
        with mock.patch.object(cix, "retention", return_value=None), \
                mock.patch.object(cix, "export_repo", return_value={"runs": 0}) as export, \
                mock.patch("manifest_identity.apply_project_identity"):
            cix.main(["--manifest", str(manifest)])
        export.assert_called_once()

    def test_a_run_is_marked_done_only_with_its_jobs(self):
        """Second-run finding 1: a failed append leaves the run to retry, jobs and all."""
        origin = archive.Origin("acct", "o", "o/a")

        def gh(path: str):
            if path.endswith("/actions/runs?per_page=100"):
                return [{"workflow_runs": [{"id": 1, "status": "completed", "head_sha": "s"}]}]
            if "/jobs" in path:
                return [{"jobs": [{"id": 11, "status": "completed"}]}]
            if path.endswith("/commits?per_page=100"):
                return [[]]
            return [[]]
        with mock.patch.object(archive, "archive", return_value=0):
            with self.assertRaises(RuntimeError):
                cix.export_repo("o/a", origin, gh, dry_run=False)
        state = cix._load_state("o/a")
        self.assertNotIn(1, state["run"])
        self.assertNotIn(11, state["job"])
        cix.export_repo("o/a", origin, gh, dry_run=False)
        kinds = sorted(r["record"]["kind"] for r in self.archived("ci-history"))
        self.assertEqual(kinds, ["job", "workflow_run"])


class ExportIncrementalTests(_Root):
    def test_a_mid_pass_failure_keeps_every_run_already_archived_oldest_first(self):
        """Correctness finding 2: one transient gh error must not discard the pass."""
        origin = archive.Origin("acct", "o", "o/a")
        runs = [{"id": i, "status": "completed", "head_sha": f"s{i}",
                 "created_at": f"2026-0{i}-01T00:00:00Z"} for i in (3, 1, 2)]

        def gh(path: str):
            if path.endswith("/actions/runs?per_page=100"):
                return [{"workflow_runs": runs}]
            if "/runs/3/jobs" in path:
                raise RuntimeError("gh api: HTTP 502")
            if "/jobs" in path:
                return [{"jobs": []}]
            return [[]]
        with self.assertRaises(RuntimeError):
            cix.export_repo("o/a", origin, gh, dry_run=False)
        self.assertEqual(cix._load_state("o/a")["run"], {1, 2})
        archived = [r["record"]["id"] for r in self.archived("ci-history")]
        self.assertEqual(archived, [1, 2])


class ScriptModeCeilingTests(_Root):
    """Pass-2 finding: `_force_trim` was defined after `__main__`, so the hook
    (run as a script) hit NameError. Exercise the SCRIPT, not an import."""

    def test_belt_wakeup_run_as_a_script_can_reach_force_trim(self):
        import runpy
        ns = runpy.run_path(str(TOOLS / "hooks" / "belt_wakeup.py"), run_name="not_main")
        self.assertIn("_force_trim", ns)
        source = (TOOLS / "hooks" / "belt_wakeup.py").read_text(encoding="utf-8")
        self.assertLess(source.index("def _force_trim("), source.index('if __name__ == "__main__":'))
        source = (TOOLS / "lane" / "lane3_audit.py").read_text(encoding="utf-8")
        self.assertLess(source.index("def _force_trim("), source.index('if __name__ == "__main__":'))

    def test_belt_wakeup_trims_past_the_ceiling_when_archiving_fails(self):
        import belt_wakeup
        log = self.tmp / "fires.jsonl"
        with mock.patch.object(belt_wakeup, "FIRE_LOG_MAX", 2), \
                mock.patch.object(belt_wakeup, "_archive_fires", return_value=0):
            for i in range(25):
                belt_wakeup.record_fire({"session_id": str(i)}, "1", True, path=log)
        self.assertLessEqual(len(log.read_text().splitlines()), 2 * archive.HARD_CEILING_FACTOR)
        self.assertTrue(any(f["source"] == "belt-wakeup-fires" and f["forced_loss"] > 0
                            for f in self.failures()))


class HandoffOwedMixedTests(HandoffOwedTests):
    def test_a_file_with_a_non_dict_entry_is_archived_raw_whole(self):
        f = self.owed / "sess.json"
        f.write_text(json.dumps([{"repo": "vitalharmony/hrse", "issue": 3}, "legacy-string"]),
                     encoding="utf-8")
        self._age(f)
        handoff_owed.prune()
        self.assertFalse(f.exists())
        self.assertIn("legacy-string", json.dumps(self.archived("handoff-owed")[0]["record"]))


class RetentionRoutingTests(_Root):
    def test_a_repo_already_at_400_days_is_skipped(self):
        manifest = self.tmp / "projects.toml"
        manifest.write_text('[[project]]\nname = "a"\nprefix = "A"\nrepo = "o/a"\naccount = "acct"\n',
                            encoding="utf-8")
        with mock.patch.object(cix, "retention", return_value={"days": 400, "maximum_allowed_days": 400}), \
                mock.patch.object(cix, "export_repo") as export, \
                mock.patch("manifest_identity.apply_project_identity"):
            cix.main(["--manifest", str(manifest)])
        export.assert_not_called()

    def test_apply_puts_the_maximum_for_a_raise(self):
        calls = []

        def gh(*args):
            calls.append(args)
            out = json.dumps({"days": 400 if any("PUT" == a for a in args) or len(calls) > 2 else 90,
                              "maximum_allowed_days": 400})
            return mock.Mock(returncode=0, stdout=out, stderr="")
        row = actions_retention.apply("o/a", gh, dry_run=False)
        put = [c for c in calls if "PUT" in c]
        self.assertEqual(len(put), 1)
        self.assertIn("days=400", put[0])
        self.assertEqual(row["action"], "raise")


class RetentionPlanTests(unittest.TestCase):
    def test_plan(self):
        self.assertEqual(actions_retention.plan({"days": 90, "maximum_allowed_days": 400}), "raise")
        self.assertEqual(actions_retention.plan({"days": 400, "maximum_allowed_days": 400}), "ok")
        self.assertEqual(actions_retention.plan({"days": 90, "maximum_allowed_days": 90}), "capped")
        self.assertEqual(actions_retention.plan(None), "unknown")


if __name__ == "__main__":
    unittest.main()
