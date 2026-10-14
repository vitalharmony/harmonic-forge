"""Tests for fold_due.py (harmonic-forge#982, AC1, AC2, AC7).

Every test uses a temp store, a temp repo and a temp marker path; nothing reads
the operator's real store, settings or cache.
"""
from __future__ import annotations

import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import fold_due  # noqa: E402

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)


def feedback(store: Path, name: str, first_seen: str) -> None:
    (store / name).write_text(
        f"---\nname: {name[:-3]}\ndescription: d\nmetadata:\n  type: feedback\n"
        f"first_seen: {first_seen}\ninstances: 1\n---\nbody\n")


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.store, self.repo, self.marker = root / "store", root / "repo", root / "cache" / "marker.json"
        self.store.mkdir()
        self.repo.mkdir()
        (self.store / "MEMORY.md").write_text("# Memory Index\n" + "- x\n" * 50)
        self.surface = mock.patch.object(fold_due, "_surface_bytes", return_value=1000)
        self.surface_mock = self.surface.start()
        self.repos = mock.patch.object(fold_due, "DEFAULT_REPOS", ())  # never the real checkouts
        self.repos.start()

    def tearDown(self) -> None:
        self.surface.stop()
        self.repos.stop()
        self._tmp.cleanup()

    def record(self, when: datetime) -> dict:
        fold_due.record(self.store, self.repo, when, self.marker)
        return fold_due.read_marker(self.marker)

    def owed(self, marker: dict | None) -> list[str]:
        return [msg for _kind, msg in fold_due.owed(self.store, self.repo, NOW, marker)]


class Triggers(Base):
    """AC1: each trigger fires on its own, and nothing fires when none is due."""

    def test_volume(self) -> None:
        marker = self.record(NOW - timedelta(days=1))
        for i in range(15):
            feedback(self.store, f"feedback_n{i}.md", "2026-10-20")
        fired = self.owed(marker)
        self.assertTrue(any("15 new feedback memories" in f for f in fired), fired)

    def test_volume_counts_files_added_after_the_snapshot_same_day(self) -> None:
        for i in range(3):
            feedback(self.store, f"feedback_old{i}.md", "2026-10-19")
        marker = self.record(NOW - timedelta(hours=1))
        for i in range(15):
            feedback(self.store, f"feedback_n{i}.md", "2026-10-19")  # the fold's own day
        self.assertTrue(any("15 new" in f for f in self.owed(marker)))

    def test_volume_ignores_files_already_in_the_snapshot(self) -> None:
        for i in range(20):
            feedback(self.store, f"feedback_n{i}.md", "2026-10-20")
        marker = self.record(NOW - timedelta(hours=1))
        self.assertEqual(self.owed(marker), [])

    def test_recurrence(self) -> None:
        marker = self.record(NOW - timedelta(days=1))
        lines = [{"rule_ref": "R-0039"}, {"rule_ref": "R-0039"}, {"rule_ref": None}, {"rule_ref": ""}]
        (self.store / "protocol-failures.jsonl").write_text("\n".join(json.dumps(x) for x in lines) + "\n")
        fired = self.owed(marker)
        self.assertTrue(any("R-0039 failed again" in f for f in fired), fired)

    def test_none_or_empty_rule_ref_never_recurs(self) -> None:
        marker = self.record(NOW - timedelta(days=1))
        lines = [{"rule_ref": None}] * 3 + [{"rule_ref": ""}] * 3
        (self.store / "protocol-failures.jsonl").write_text("\n".join(json.dumps(x) for x in lines) + "\n")
        self.assertEqual(self.owed(marker), [])

    def test_failures_before_the_marker_are_not_counted(self) -> None:
        (self.store / "protocol-failures.jsonl").write_text(
            json.dumps({"rule_ref": "R-0039"}) + "\n" + json.dumps({"rule_ref": "R-0039"}) + "\n")
        marker = self.record(NOW - timedelta(days=1))
        self.assertEqual(self.owed(marker), [])

    def test_growth_of_memory_md(self) -> None:
        marker = self.record(NOW - timedelta(days=1))
        (self.store / "MEMORY.md").write_text((self.store / "MEMORY.md").read_text() * 2)
        fired = self.owed(marker)
        self.assertTrue(any("MEMORY.md grew" in f for f in fired), fired)

    def test_growth_of_the_surface(self) -> None:
        marker = self.record(NOW - timedelta(days=1))
        self.surface_mock.return_value = 1200
        fired = self.owed(marker)
        self.assertTrue(any("surface grew" in f for f in fired), fired)

    def test_over_budget_but_unchanged_surface_is_quiet(self) -> None:
        self.surface_mock.return_value = 10 ** 6  # far over any absolute budget
        marker = self.record(NOW - timedelta(days=1))
        self.assertEqual(self.owed(marker), [])

    def test_fallback_after_seven_days(self) -> None:
        marker = self.record(NOW - timedelta(days=7))
        self.assertEqual(self.owed(marker), ["7 days since last fold"])

    def test_truncated_failures_log_counts_from_the_start(self) -> None:
        (self.store / "protocol-failures.jsonl").write_text(
            "".join(json.dumps({"rule_ref": f"R-{i:04d}"}) + "\n" for i in range(10)))
        marker = self.record(NOW - timedelta(days=1))
        (self.store / "protocol-failures.jsonl").write_text(
            json.dumps({"rule_ref": "R-0039"}) + "\n" + json.dumps({"rule_ref": "R-0039"}) + "\n")
        self.assertTrue(any("R-0039 failed again" in f for f in self.owed(marker)))

    def test_replaced_failures_log_of_equal_length_counts_from_the_start(self) -> None:
        (self.store / "protocol-failures.jsonl").write_text(
            json.dumps({"rule_ref": "R-0001"}) + "\n" + json.dumps({"rule_ref": "R-0002"}) + "\n")
        marker = self.record(NOW - timedelta(days=1))
        (self.store / "protocol-failures.jsonl").write_text(
            json.dumps({"rule_ref": "R-0039"}) + "\n" + json.dumps({"rule_ref": "R-0039"}) + "\n")
        self.assertTrue(any("R-0039 failed again" in f for f in self.owed(marker)))

    def test_missing_surface_baseline_for_this_repo_is_reported(self) -> None:
        marker = self.record(NOW - timedelta(days=1))
        marker["surface_bytes"] = {}
        self.assertTrue(any("no surface baseline" in f for f in self.owed(marker)))

    def test_exactly_ten_percent_growth_fires(self) -> None:
        self.assertTrue(fold_due._grew(110, 100))
        self.assertTrue(fold_due._grew(1100, 1000))
        self.assertFalse(fold_due._grew(109, 100))

    def test_nothing_due(self) -> None:
        marker = self.record(NOW - timedelta(days=1))
        self.assertEqual(self.owed(marker), [])


class MainWorktree(unittest.TestCase):
    def test_a_feature_worktree_keys_and_measures_the_main_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            main = Path(tmp) / "main"
            main.mkdir()
            run = lambda *a, cwd=main: subprocess.run(["git", *a], cwd=cwd, check=True,  # noqa: E731
                                                      capture_output=True)
            run("init", "-q")
            run("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "x")
            run("worktree", "add", "-q", "-b", "feat", str(Path(tmp) / "wt"))
            k_main, top_main = fold_due.repo_key(main)
            k_wt, top_wt = fold_due.repo_key(Path(tmp) / "wt")
        self.assertEqual(k_main, k_wt)
        self.assertEqual(top_wt.resolve(), main.resolve())


class Marker(Base):
    """AC2: --record quiets every trigger; a missing or corrupt marker is 'no marker'."""

    def test_record_with_unchanged_inputs_quiets_all_triggers(self) -> None:
        for i in range(15):
            feedback(self.store, f"feedback_n{i}.md", "2026-10-20")
        (self.store / "protocol-failures.jsonl").write_text(
            json.dumps({"rule_ref": "R-0039"}) + "\n" + json.dumps({"rule_ref": "R-0039"}) + "\n")
        marker = self.record(NOW)
        self.assertEqual(self.owed(marker), [])

    def test_missing_marker(self) -> None:
        self.assertEqual(self.owed(fold_due.read_marker(self.marker)), ["no marker"])

    def test_corrupt_marker_reports_no_marker_and_exits_zero(self) -> None:
        self.marker.parent.mkdir(parents=True)
        self.marker.write_text("{not json")
        self.assertIsNone(fold_due.read_marker(self.marker))
        out = io.StringIO()
        with redirect_stdout(out):
            code = fold_due.main(["--store", str(self.store), "--repo", str(self.repo),
                                  "--marker", str(self.marker)])
        self.assertEqual(code, 0)
        self.assertEqual(out.getvalue().splitlines(), ["memory-fold owed: no marker", "kinds: no-marker"])

    def test_marker_with_a_corrupt_field_reads_as_no_marker(self) -> None:
        self.record(NOW)
        data = json.loads(self.marker.read_text())
        for field, bad in (("memory_md_bytes", "x"), ("failures_offset", -1),
                           ("surface_bytes", {"k": {"path": "/x", "bytes": "x"}}),
                           ("feedback_names", "nope"), ("ts", "yesterday")):
            broken = {**data, field: bad}
            self.marker.write_text(json.dumps(broken))
            self.assertIsNone(fold_due.read_marker(self.marker), field)

    def test_record_refuses_on_a_failed_measurement_and_writes_nothing(self) -> None:
        self.surface_mock.return_value = None
        with self.assertRaises(fold_due.MeasurementError):
            fold_due.record(self.store, self.repo, NOW, self.marker)
        self.assertFalse(self.marker.exists())
        self.surface_mock.return_value = 1000
        (self.store / "MEMORY.md").unlink()
        with self.assertRaises(fold_due.MeasurementError):
            fold_due.record(self.store, self.repo, NOW, self.marker)
        self.assertFalse(self.marker.exists())

    def test_record_cli_exits_1_on_any_failure(self) -> None:
        (self.store / "protocol-failures.jsonl").write_bytes(b"\xff\xfe bad")
        with mock.patch.object(fold_due, "snapshot", side_effect=OSError("boom")):
            code = fold_due.main(["--record", "--store", str(self.store), "--repo", str(self.repo),
                                  "--marker", str(self.marker)])
        self.assertEqual(code, 1)
        self.assertFalse(self.marker.exists())

    def test_record_refuses_when_git_cannot_name_the_main_checkout(self) -> None:
        (self.repo / ".git").mkdir()
        with mock.patch.object(fold_due, "main_checkout_known", return_value=False):
            with self.assertRaises(fold_due.MeasurementError):
                fold_due.record(self.store, self.repo, NOW, self.marker)
        self.assertFalse(self.marker.exists())

    def test_record_is_atomic_and_leaves_no_temp_file(self) -> None:
        self.record(NOW)
        self.assertEqual([p.name for p in self.marker.parent.iterdir()], ["marker.json"])

    def test_main_prints_nothing_when_nothing_is_due(self) -> None:
        self.record(datetime.now(timezone.utc))
        out = io.StringIO()
        with redirect_stdout(out):
            code = fold_due.main(["--store", str(self.store), "--repo", str(self.repo),
                                  "--marker", str(self.marker)])
        self.assertEqual((code, out.getvalue()), (0, ""))


if __name__ == "__main__":
    unittest.main()
