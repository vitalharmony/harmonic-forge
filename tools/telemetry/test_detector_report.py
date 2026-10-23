#!/usr/bin/env python3
"""Tests for detector_report.py and the D1 view (harmonic-forge#1159).

Fixture logs and registries in temp directories, never the real store.
DuckDB is not installed in CI, so the view tests skip there with the install
command; the pure-Python parts run everywhere.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import detector_report as dr  # noqa: E402

try:
    import duckdb  # noqa: F401
    HAVE_DUCKDB = True
except ImportError:
    HAVE_DUCKDB = False
needs_duckdb = unittest.skipUnless(HAVE_DUCKDB, f"duckdb not installed ({dr.INSTALL_HINT})")

REGISTRY = '''
[[rule]]
id = "R-9001"
file = "rules/a.md"
statement = "Two misses, nothing enforces it."
hooks = []

[[rule]]
id = "R-9002"
file = "rules/a.md"
statement = "Hooked, still missed."
hooks = [{script = "tools/hooks/remind_x.py", events = ["Stop"]}]

[[rule]]
id = "R-9003"
file = "rules/a.md"
statement = "One miss only."
hooks = []

[[rule]]
id = "R-9004"
file = "rules/a.md"
statement = "Guarded by the gate hook."
hooks = ["tools/gh/preclose_check.py"]

[[rule]]
id = "R-9005"
file = "rules/a.md"
statement = "An IMC waiver is recorded before the run."
hooks = []
'''


def _miss(rule_ref: str, *, existed: str = "rule", caught: str = "self", repo: str | None = None,
          ts: str = "2026-10-01T00:00:00Z") -> dict:
    return {"ts": ts, "caught_by": caught, "rule_existed": existed, "rule_ref": rule_ref,
            "expected": "x", "happened": "y", "lane": "1", "repo": repo, "issue": None,
            "session": None, "turns_lost": 0, "work_discarded": None, "account": "narrative"}


BASE_MISSES = [
    _miss("R-9001", caught="operator"), _miss("R-9001 / some skill", ts="2026-10-05T00:00:00Z"),
    _miss("R-9002"), _miss("R-9002", existed="scoped_out"),
    _miss("R-9003"),
    _miss("R-9004"), _miss("R-9004"),
    _miss("R-9005"), _miss("R-9005"),
    _miss("harmonic-forge#838"),
    _miss("R-9003", existed="unpromoted"),
]


class Fixture(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.registry = self.tmp / "registry.toml"
        self.registry.write_text(REGISTRY, encoding="utf-8")
        self.views = self.tmp / "views"
        shutil.copytree(HERE / "views", self.views)
        self.out = self.tmp / "out"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def write_log(self, records: list[dict], name: str = "protocol-failures.jsonl") -> Path:
        log = self.tmp / name
        log.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
        return log

    def run_report(self, records: list[dict], *extra: str) -> str:
        log = self.write_log(records)
        missing = self.tmp / "absent" / "protocol-failures.jsonl"
        if "--base-checks" not in extra:  # never the real te-runner cache
            extra = ("--base-checks", str(self.tmp / "no-base-checks"), *extra)
        code = dr.main(["--miss-log", str(log), "--miss-log", str(missing), "--registry", str(self.registry),
                        "--views", str(self.views), "--out-dir", str(self.out), "--date", "2026-10-22", *extra])
        self.assertEqual(code, 0)
        return (self.out / "2026-10-22.md").read_text(encoding="utf-8")

    @staticmethod
    def row_of(report: str, rule_id: str) -> str | None:
        return next((line for line in report.splitlines() if line.startswith(f"| {rule_id} ")), None)


class PurePythonTests(Fixture):
    def test_both_hook_shapes_normalize(self) -> None:
        self.assertEqual(dr.hook_basename({"script": "tools/hooks/remind_x.py"}), "remind_x.py")
        self.assertEqual(dr.hook_basename("tools/gh/preclose_check.py"), "preclose_check.py")
        self.assertEqual(dr.hook_basename("gh_shim"), "gh_shim")

    def test_protected_bases(self) -> None:
        targets = json.loads((HERE / "views" / "protected_targets.json").read_text(encoding="utf-8"))
        rows = {r[0]: r for r in dr.rule_hook_rows(dr.query_rules.load_rules(self.registry), targets)}
        self.assertTrue(rows["R-9004"][3], "hook basename basis")
        self.assertTrue(rows["R-9005"][3], "statement regex basis")
        self.assertFalse(rows["R-9001"][3])
        self.assertTrue(dr.is_protected({"id": "R-9001", "statement": ""}, [], {"rule_ids": ["R-9001"]}))

    def test_partition_marks_unresolved_and_keeps_narrative(self) -> None:
        rows = dr.miss_rows([_miss("R-9001"), _miss("R-9001", repo="vitalharmony/hrse")], None, None, None)
        self.assertEqual(rows[0][6:9], ("unresolved", "unresolved", "unresolved"))
        self.assertEqual(rows[1][6:9], ("unresolved", "vitalharmony", "hrse"))
        self.assertEqual(rows[0][9], "narrative")

    def test_unresolved_rule_ref_counted_not_guessed(self) -> None:
        rows = dr.miss_rows(BASE_MISSES, None, None, None)
        self.assertEqual(dr.unresolved_rule_refs(rows), 1)


@needs_duckdb
class ViewTests(Fixture):
    def test_rule_with_two_in_force_misses_is_listed_as_no_hook(self) -> None:
        row = self.row_of(self.run_report(BASE_MISSES), "R-9001")
        self.assertIsNotNone(row)
        self.assertIn("| 2 | 1 | 2026-10-05T00:00:00Z | no hook |", row)

    def test_one_miss_rule_is_absent(self) -> None:
        self.assertIsNone(self.row_of(self.run_report(BASE_MISSES), "R-9003"))

    def test_hooked_rule_is_hook_not_binding(self) -> None:
        row = self.row_of(self.run_report(BASE_MISSES), "R-9002")
        self.assertIsNotNone(row)
        self.assertIn("hook not binding", row)

    def test_protected_rule_is_suppressed_and_counted(self) -> None:
        report = self.run_report(BASE_MISSES)
        self.assertNotIn("R-9004", report)
        self.assertIn("2 candidate(s) suppressed (protected target).", report)

    def test_statement_only_preclose_rule_is_suppressed(self) -> None:
        self.assertNotIn("R-9005", self.run_report(BASE_MISSES))

    def test_unresolved_rule_ref_line(self) -> None:
        self.assertIn("1 in-force miss(es) with an unresolved rule_ref.", self.run_report(BASE_MISSES))

    def test_second_account_row_is_filtered(self) -> None:
        records = [_miss("R-9001"), _miss("R-9001", repo="kenekted/kenekted-platform")]
        self.assertIsNotNone(self.row_of(self.run_report(records), "R-9001"))
        self.assertIsNone(self.row_of(self.run_report(records, "--org", "vitalharmony"), "R-9001"))

    def test_a_new_view_file_is_reported_without_runner_change(self) -> None:
        (self.views / "d9_probe.sql").write_text(
            "CREATE OR REPLACE VIEW d9_probe AS SELECT count(*) AS n FROM misses_raw;\n", encoding="utf-8")
        report = self.run_report(BASE_MISSES)
        self.assertIn("## d9_probe", report)
        self.assertLess(report.index("## d1_enforcement_gap"), report.index("## d9_probe"))

    def test_malformed_log_line_is_skipped_and_counted(self) -> None:
        log = self.write_log(BASE_MISSES)
        with log.open("a", encoding="utf-8") as handle:
            handle.write('{"ts": "2026-10-0\n[1, 2]\n')
        code = dr.main(["--miss-log", str(log), "--registry", str(self.registry), "--views", str(self.views),
                        "--out-dir", str(self.out), "--date", "2026-10-22",
                        "--base-checks", str(self.tmp / "no-base-checks")])
        self.assertEqual(code, 0)
        report = (self.out / "2026-10-22.md").read_text(encoding="utf-8")
        self.assertIn("2 malformed miss-log line(s) skipped.", report)
        self.assertIsNotNone(self.row_of(report, "R-9001"))

    def test_unregistered_rule_is_suppressed_and_counted(self) -> None:
        report = self.run_report(BASE_MISSES + [_miss("R-9999"), _miss("R-9999")])
        self.assertNotIn("R-9999", report)
        self.assertIn("3 candidate(s) suppressed (protected target).", report)

    def test_report_files_nothing_outside_out_dir(self) -> None:
        before = sorted(p for p in self.tmp.rglob("*") if p.is_file())
        self.run_report(BASE_MISSES)
        after = sorted(p for p in self.tmp.rglob("*") if p.is_file())
        self.assertEqual(set(after) - set(before) - {self.tmp / "protocol-failures.jsonl"},
                         {self.out / "2026-10-22.md"})


class DateArgumentTests(unittest.TestCase):
    def test_date_that_is_a_path_is_refused(self) -> None:
        from unittest import mock
        import io
        with mock.patch("sys.stderr", new_callable=io.StringIO), self.assertRaises(SystemExit):
            dr.main(["--date", "../../escape", "--out-dir", "/nonexistent-never-written"])


def _base_check(sha: str, failures: list[str], *, complete: bool = True) -> dict:
    """The exact shape te_run.base_check caches (a schema change fails here)."""
    return {"sha": sha, "steps": [{"name": "run_tests", "rc": 1 if failures else 0}], "step_failures": [],
            "complete": complete, "test_rc": 1 if failures else 0, "failures": failures,
            "module_errors": [], "ran": 10, "printed_ran": True}


T_LANE = "test_a (test_lane_refresh.Cls.test_a)"
T_OTHER = "test_b (test_detector_report.Cls.test_b)"
T_DENY = "test_c (test_deny_check.Cls.test_c)"
T_BAD = "not a parseable id"


@needs_duckdb
class BaseCheckViewTests(Fixture):
    def write_checks(self, checks: list[dict]) -> Path:
        root = self.tmp / "base-check"
        for when, check in enumerate(checks):  # oldest first
            path = root / check["sha"] / "result.json"
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps(check), encoding="utf-8")
            os.utime(path, (1_000_000 + when, 1_000_000 + when))
        return root

    def report(self, checks: list[dict]) -> str:
        return self.run_report([], "--base-checks", str(self.write_checks(checks)))

    @staticmethod
    def d2_row(report: str, test: str) -> str | None:
        return next((line for line in report.splitlines() if line.startswith(f"| {test} |")), None)

    def test_three_check_streak_is_listed_with_n(self) -> None:
        report = self.report([_base_check(s, [T_LANE]) for s in ("a1", "a2", "a3")])
        self.assertEqual(self.d2_row(report, T_LANE), f"| {T_LANE} | 3 |")

    def test_passed_in_newest_check_is_absent(self) -> None:
        report = self.report([_base_check("a1", [T_LANE]), _base_check("a2", [T_LANE]), _base_check("a3", [])])
        self.assertIsNone(self.d2_row(report, T_LANE))

    def test_a_gap_breaks_the_streak(self) -> None:
        report = self.report([_base_check("a1", [T_LANE]), _base_check("a2", []),
                              _base_check("a3", [T_LANE]), _base_check("a4", [T_LANE])])
        self.assertEqual(self.d2_row(report, T_LANE), f"| {T_LANE} | 2 |")

    def test_incomplete_check_is_ignored(self) -> None:
        report = self.report([_base_check("a1", [T_LANE]), _base_check("a2", [T_LANE], complete=False)])
        self.assertIsNone(self.d2_row(report, T_LANE))

    def test_protected_test_is_suppressed_and_counted(self) -> None:
        checks = [_base_check(s, [T_DENY, T_BAD, T_OTHER]) for s in ("a1", "a2")]
        report = self.report(checks)
        self.assertNotIn(T_DENY, report)
        self.assertNotIn(T_BAD, report)
        self.assertIsNotNone(self.d2_row(report, T_OTHER))
        section = report[report.index("## d2_persistent_base_failure"):]
        self.assertIn("2 candidate(s) suppressed (protected target).", section)

    def test_mtime_tie_is_broken_by_sha(self) -> None:
        x, y = T_LANE, T_OTHER
        root = self.write_checks([_base_check("o", [x, y]), _base_check("a", [x]), _base_check("b", [y]),
                                  _base_check("n", [x, y])])
        for sha in ("a", "b"):
            os.utime(root / sha / "result.json", (1_000_001.5, 1_000_001.5))
        # Tied a and b: sha "a" sorts newer, so x fails in n and a (streak 2)
        # and y is broken at a; the reverse tiebreak would list y instead.
        report = self.run_report([], "--base-checks", str(root))
        self.assertEqual(self.d2_row(report, x), f"| {x} | 2 |")
        self.assertIsNone(self.d2_row(report, y))

    def test_empty_cache_renders_an_empty_section(self) -> None:
        report = self.run_report([], "--base-checks", str(self.tmp / "absent"))
        self.assertIn("## d2_persistent_base_failure\n\nNo rows.", report)


class BaseCheckProtectedTests(unittest.TestCase):
    def test_module_resolution(self) -> None:
        targets = {"test_modules": ["test_deny_check"]}
        modules = {"test_lane_refresh": 1, "test_deny_check": 1, "test_twice": 2}
        checks = [("s", True, [T_LANE, T_DENY, T_BAD, "x (test_twice.C.m)", "y (test_gone.C.m)"], 0.0)]
        got = dict(dr.base_check_protected_rows(checks, targets, modules))
        self.assertEqual(got, {T_LANE: False, T_DENY: True, T_BAD: True,
                               "x (test_twice.C.m)": True, "y (test_gone.C.m)": True})

    def test_tracked_modules_reads_git(self) -> None:
        self.assertEqual(dr.tracked_modules(dr.ROOT).get("test_detector_report"), 1)


class MissingDuckdbTests(unittest.TestCase):
    def test_missing_duckdb_exits_with_install_command(self) -> None:
        from unittest import mock
        import io
        with mock.patch.dict(sys.modules, {"duckdb": None}), \
                mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            self.assertEqual(dr.main(["--out-dir", "/nonexistent-never-written"]), 2)
        self.assertIn(dr.INSTALL_HINT, err.getvalue())


if __name__ == "__main__":
    unittest.main()
