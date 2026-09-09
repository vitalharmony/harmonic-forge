#!/usr/bin/env python3
"""Tests for the protocol-failure capture writer (harmonic-forge#520)."""
from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import capture_failure as m  # noqa: E402

MINIMAL = [
    "--caught-by", "hook",
    "--rule-existed", "none",
    "--expected", "branch before writing",
    "--happened", "wrote on main",
]


class _Written(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.log = self.root / "protocol-failures.jsonl"

    def run_capture(self, *extra: str) -> int:
        return m.main([*MINIMAL, *extra, "--path", str(self.log)])

    def lines(self) -> list[dict]:
        return [json.loads(line)
                for line in self.log.read_text(encoding="utf-8").splitlines()
                if line.strip()]


class TestRequiredFieldsRefuse(unittest.TestCase):
    """AC3 — `caught_by` and `rule_existed` are required, not optional.

    A record without them is a diary entry. Refusal has to be a hard exit with
    no partial write, because a default would be a guess recorded as data.
    """

    def test_missing_caught_by_refuses(self):
        with self.assertRaises(SystemExit) as caught:
            m.main(["--rule-existed", "none", "--expected", "x", "--happened", "y"])
        self.assertNotEqual(caught.exception.code, 0)

    def test_missing_rule_existed_refuses(self):
        with self.assertRaises(SystemExit) as caught:
            m.main(["--caught-by", "hook", "--expected", "x", "--happened", "y"])
        self.assertNotEqual(caught.exception.code, 0)

    def test_missing_expected_or_happened_refuses(self):
        for drop in ("--expected", "--happened"):
            with self.subTest(drop=drop):
                argv = list(MINIMAL)
                index = argv.index(drop)
                del argv[index:index + 2]
                with self.assertRaises(SystemExit) as caught:
                    m.main(argv)
                self.assertNotEqual(caught.exception.code, 0)

    def test_every_caught_by_value_is_accepted(self):
        for value in m.CAUGHT_BY:
            with self.subTest(value=value), tempfile.TemporaryDirectory() as tmp:
                log = Path(tmp) / "f.jsonl"
                argv = list(MINIMAL)
                argv[argv.index("--caught-by") + 1] = value
                self.assertEqual(m.main([*argv, "--path", str(log)]), 0)

    def test_every_rule_existed_value_is_accepted_with_its_ref_rule(self):
        for value in m.RULE_EXISTED:
            with self.subTest(value=value), tempfile.TemporaryDirectory() as tmp:
                log = Path(tmp) / "f.jsonl"
                argv = list(MINIMAL)
                argv[argv.index("--rule-existed") + 1] = value
                if value in m.RULE_REF_REQUIRED:
                    argv += ["--rule-ref", "R-0344"]
                self.assertEqual(m.main([*argv, "--path", str(log)]), 0)

    def test_an_out_of_enum_value_refuses(self):
        for flag, bogus in (("--caught-by", "somehow"), ("--rule-existed", "maybe")):
            with self.subTest(flag=flag):
                argv = list(MINIMAL)
                argv[argv.index(flag) + 1] = bogus
                with self.assertRaises(SystemExit) as caught:
                    m.main(argv)
                self.assertNotEqual(caught.exception.code, 0)


class TestRuleRefCrossField(_Written):
    """The cross-field rule argparse cannot express."""

    def test_a_named_rule_class_without_a_ref_refuses_without_writing(self):
        code = self.run_capture()  # baseline write so the file exists
        self.assertEqual(code, 0)
        before = self.log.read_text(encoding="utf-8")
        argv = list(MINIMAL)
        argv[argv.index("--rule-existed") + 1] = "rule"
        self.assertEqual(m.main([*argv, "--path", str(self.log)]), 2)
        self.assertEqual(self.log.read_text(encoding="utf-8"), before,
                         "a refusal must not partially write")

    def test_none_with_a_ref_refuses(self):
        self.assertEqual(self.run_capture("--rule-ref", "R-0344"), 2)
        self.assertFalse(self.log.exists())


class TestAppendOnly(_Written):
    """AC2 — three sequential writes produce three lines, none overwritten.

    Asserted against the actual file rather than a mocked handle: the property
    is about what is on disk after the third call, and a mock would assert the
    code's intent instead of its effect.
    """

    def test_three_sequential_captures_produce_three_lines(self):
        for index in range(3):
            self.assertEqual(self.run_capture("--account", f"event {index}"), 0)
        lines = self.lines()
        self.assertEqual(len(lines), 3)
        self.assertEqual([line["account"] for line in lines],
                         ["event 0", "event 1", "event 2"])

    def test_an_existing_log_is_not_truncated(self):
        self.log.write_text('{"ts":"earlier","account":"pre-existing"}\n',
                            encoding="utf-8")
        self.assertEqual(self.run_capture(), 0)
        lines = self.lines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0]["account"], "pre-existing")

    def test_the_writer_never_opens_the_log_for_writing(self):
        """No rewrite path exists, and nothing may add one quietly."""
        source = Path(m.__file__).read_text(encoding="utf-8")
        for forbidden in ('"w"', "'w'", "write_text", "truncate", "os.replace"):
            self.assertNotIn(forbidden, source,
                             f"{forbidden} would introduce a rewrite path")

    def test_each_line_is_independently_parseable_json(self):
        for _ in range(3):
            self.run_capture()
        for line in self.log.read_text(encoding="utf-8").splitlines():
            json.loads(line)


class TestPerLaneDefaultPath(unittest.TestCase):
    """AC4 — the default honors Lane 3's restriction without a flag.

    Asking the invoker to remember the restriction is the failure mode this
    whole issue is about, so the default carries it.
    """

    def test_lane_3_defaults_inside_testplan(self):
        path = m.default_log_path("3")
        self.assertEqual(path.parent, m.TESTPLAN_ROOT)
        self.assertTrue(str(path).startswith(str(m.TESTPLAN_ROOT)))

    def test_other_lanes_default_to_the_shared_memory_store(self):
        for lane in ("1", "2", None, ""):
            with self.subTest(lane=lane):
                self.assertEqual(m.default_log_path(lane).parent, m.MEMORY_STORE)

    def test_lane_3_default_is_not_the_memory_store(self):
        """The specific denial: a Lane 3 write there is refused by the guard,
        so a shared default would make the capture unusable from Lane 3 —
        which is the lane most likely to observe a protocol failure."""
        self.assertNotEqual(m.default_log_path("3").parent, m.MEMORY_STORE)

    def test_an_explicit_path_overrides_the_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "nested" / "f.jsonl"
            self.assertEqual(m.main([*MINIMAL, "--path", str(log)]), 0)
            self.assertTrue(log.exists(), "parent directories are created")


class TestNoNetwork(unittest.TestCase):
    """AC5 — no GitHub call, no network call, anywhere in the capture path.

    Telemetry that costs quota reproduces the class of defect it exists to
    catch, and a capture that can fail on a network error gets skipped.
    """

    def test_the_source_imports_nothing_that_reaches_the_network(self):
        source = Path(m.__file__).read_text(encoding="utf-8")
        for forbidden in ("requests", "urllib", "httpx", "socket",
                          "subprocess", "gh api", "gh issue"):
            self.assertNotIn(forbidden, source)


class TestMemoryStoreIsReadOnlyEvidence(unittest.TestCase):
    """Running a capture must not touch any existing memory file.

    The pre-flight states existing memory files are read-only evidence. This
    asserts it holds in practice, not just in intent — a writer that rewrote a
    memory file while appending its record would corrupt the very corpus the
    analysis reads.
    """

    def test_existing_memory_file_mtimes_are_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp)
            memories = []
            for name in ("feedback_a.md", "feedback_b.md", "MEMORY.md"):
                path = store / name
                path.write_text(f"# {name}\n", encoding="utf-8")
                memories.append(path)
            before = {p: p.stat().st_mtime_ns for p in memories}
            time.sleep(0.01)
            self.assertEqual(
                m.main([*MINIMAL, "--path", str(store / "protocol-failures.jsonl")]),
                0)
            after = {p: p.stat().st_mtime_ns for p in memories}
            self.assertEqual(before, after)
            self.assertTrue((store / "protocol-failures.jsonl").exists())


class TestRecordShape(_Written):
    def test_the_two_required_fields_are_always_present(self):
        self.run_capture()
        record = self.lines()[0]
        self.assertEqual(record["caught_by"], "hook")
        self.assertEqual(record["rule_existed"], "none")
        self.assertIn("ts", record)

    def test_optional_fields_are_recorded_as_null_not_omitted(self):
        """An absent key and a null value read differently to an analysis
        pass; keeping the shape stable means a missing field is visible."""
        self.run_capture()
        record = self.lines()[0]
        for key in ("repo", "issue", "session", "turns_lost",
                    "work_discarded", "account", "rule_ref", "lane"):
            self.assertIn(key, record)

    def test_cost_fields_round_trip(self):
        self.run_capture("--turns-lost", "4", "--work-discarded", "one commit")
        record = self.lines()[0]
        self.assertEqual(record["turns_lost"], 4)
        self.assertEqual(record["work_discarded"], "one commit")


class TestSkillText(unittest.TestCase):
    """AC6 — the skill states the enforcement finding in its own text, so the
    person invoking it is prompted toward the right remedy at the moment they
    have the context."""

    SKILL = Path(__file__).resolve().parents[2] / "skills" / "protocol-failure" / "SKILL.md"

    def test_the_skill_exists_and_names_the_enforcement_finding(self):
        text = self.SKILL.read_text(encoding="utf-8")
        self.assertIn("ENFORCEMENT", text)
        self.assertIn("Writing another rule", text)
        self.assertIn("R-0344", text)

    def test_the_skill_documents_the_self_late_line(self):
        text = self.SKILL.read_text(encoding="utf-8")
        self.assertIn("self_late", text)
        self.assertIn("before any artifact left the session", text)

    def test_the_skill_states_that_absence_is_a_finding(self):
        text = self.SKILL.read_text(encoding="utf-8")
        self.assertIn("not clean, it is unaudited", text)

    def test_every_enum_value_the_writer_accepts_appears_in_the_skill(self):
        text = self.SKILL.read_text(encoding="utf-8")
        for value in list(m.CAUGHT_BY) + list(m.RULE_EXISTED):
            with self.subTest(value=value):
                self.assertIn(value, text)


if __name__ == "__main__":
    unittest.main()
