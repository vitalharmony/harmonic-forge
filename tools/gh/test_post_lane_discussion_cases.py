#!/usr/bin/env python3
"""harmonic-forge#893 -- per-case classes on a Lane 3 spec, per-case verdicts
and a derived gate time on a gate result, written into the one-line footer;
the extractor turns them into scalar counts and pairs gates with specs; the
verification report never reads absence as zero."""
import ast
import hashlib
import io
import json
import os
import sys
import tempfile
import time
import unittest
import unittest.mock
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "telemetry"))

import post_lane_discussion as P  # noqa: E402
import verification_report as VR  # noqa: E402

SPEC_TC = ("## Lane 3 Test Spec — H1\n\n**Cases:** 2.\n**Next:** submit for HITL approval.\n\n"
           "### Test cases\n- TC1 — a thing.\n- TC2 — another.\n")
SPEC_NUMBERED = ("## Lane 3 Test Spec — H1\n\n**Cases:** 2.\n**Next:** submit for HITL approval.\n\n"
                 "### Test cases\n1. A thing.\n2. Another.\n")
GATE_PASS = ("## Lane 3 Gate Results — H1\n\n**Verdict:** PASS — both ran.\n**Finding:** none.\n"
             "**Next:** Lane 1 merges.\n\n### Test cases\n- TC1 — pass\n- TC2 — pass\n")
GATE_FAIL = GATE_PASS.replace("PASS — both ran", "FAIL — TC2 failed")


class Case(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def json_file(self, data: dict) -> Path:
        path = self.dir / f"m{len(list(self.dir.iterdir()))}.json"
        path.write_text(json.dumps(data))
        return path

    def post(self, kind: str, body: str, classes: dict | None = None,
             results: dict | None = None) -> str:
        """Run main(); return the posted text (body plus footer)."""
        path = self.dir / "body.md"
        path.write_text(body)
        argv = ["post_lane_discussion.py", "--repo", "vitalharmony/harmonic-forge",
                "--issue", "1", "--file", str(path), "--kind", kind]
        if classes is not None:
            argv += ["--tc-classes", str(self.json_file(classes))]
        if results is not None:
            argv += ["--tc-results", str(self.json_file(results))]
        posted = {}
        def comment_body(repo, issue, text):
            posted["text"] = text
            return "https://example/1", 1
        with unittest.mock.patch.object(sys, "argv", argv), \
             unittest.mock.patch.dict(os.environ, {"LANE": "3"}), \
             unittest.mock.patch.object(P, "comment_body", side_effect=comment_body), \
             unittest.mock.patch.object(P, "require_green_ci", return_value=False), \
             unittest.mock.patch.object(P, "require_round_approval", return_value=None), \
             unittest.mock.patch.object(P, "belt_candidates"), \
             unittest.mock.patch.object(sys, "stdout", new=io.StringIO()):
            P.main()
        return posted["text"]

    def refuses(self, kind: str, body: str, **kw) -> str:
        with self.assertRaises(SystemExit) as ctx:
            self.post(kind, body, **kw)
        return str(ctx.exception.code)


class ClassesAndResultsMatchTheBody(Case):
    """TC1, for both spec shapes."""

    def test_the_flags_are_required_for_their_kind(self):
        self.assertIn("--tc-classes", self.refuses("spec", SPEC_TC))
        self.assertIn("--tc-results", self.refuses("gate-result", GATE_PASS))

    def test_the_flags_are_refused_on_another_kind(self):
        self.assertIn("only with --kind spec",
                      self.refuses("gate-result", GATE_PASS, classes={"1": "ac"}, results={"1": "pass", "2": "pass"}))

    def test_a_missing_id_is_refused_and_named(self):
        for body in (SPEC_TC, SPEC_NUMBERED):
            with self.subTest(shape=body.splitlines()[-1]):
                self.assertIn("no entry for case(s) 2", self.refuses("spec", body, classes={"TC1": "ac"}))

    def test_an_extra_id_is_refused_and_named(self):
        for body in (SPEC_TC, SPEC_NUMBERED):
            with self.subTest(shape=body.splitlines()[-1]):
                self.assertIn("3 are not in the body", self.refuses(
                    "spec", body, classes={"1": "ac", "2": "live", "3": "ac"}))

    def test_an_unknown_class_is_refused(self):
        self.assertIn("case 2 is 'smoke'", self.refuses("spec", SPEC_TC, classes={"1": "ac", "2": "smoke"}))

    def test_a_matching_map_is_written_on_the_footer_line(self):
        text = self.post("spec", SPEC_NUMBERED, classes={"TC2": "existing", "1": "ac"})
        footer = text.rstrip().splitlines()[-1]
        self.assertTrue(footer.startswith("<!-- l1-post v1; kind=spec;"))
        self.assertIn("; classes=1:ac,2:existing -->", footer)


class ResultsAgreeWithTheVerdict(Case):
    """TC2."""

    def test_a_pass_with_a_fail_is_refused(self):
        self.assertIn("says PASS but case(s) 2 did not pass",
                      self.refuses("gate-result", GATE_PASS, results={"1": "pass", "2": "fail"}))

    def test_a_fail_with_no_fail_is_refused(self):
        self.assertIn("says FAIL but no case is fail",
                      self.refuses("gate-result", GATE_FAIL, results={"1": "pass", "2": "blocked"}))

    def test_an_agreeing_map_posts(self):
        footer = self.post("gate-result", GATE_FAIL, results={"1": "pass", "2": "fail"}).rstrip().splitlines()[-1]
        self.assertIn("results=1:pass,2:fail; gate-ms=", footer)


class PanelPass1Fixes(Case):
    """F893 preclose pass 1."""

    # Bare numbers in the first column and no TC marker anywhere: only the
    # table reader can find these ids (a TC mention elsewhere would let
    # l1_post.case_ids find them and make the test vacuous).
    TABLE_FAIL = ("## Lane 3 Gate Results — H1\n\n**Verdict:** FAIL — the second case failed.\n"
                  "**Finding:** y\n**Next:** z\n\n| Case | Verdict | Evidence |\n|---|---|---|\n"
                  "| 1 | PASS | a |\n| **2** | FAIL | b |\n")
    CASELESS_FAIL = "## Lane 3 Gate Results — H1\n\n**Verdict:** FAIL — x.\n**Finding:** y\n**Next:** z\n"
    CASELESS_BLOCKED = ("## Lane 3 Gate Results — H1 — BLOCKED\n\n**Verdict:** BLOCKED\n"
                        "**Finding:** no fixture.\n**Next:** provision it.\n")

    def test_a_table_shaped_gate_report_yields_its_case_ids(self):
        footer = self.post("gate-result", self.TABLE_FAIL, results={"1": "pass", "TC2": "fail"})
        self.assertIn("results=1:pass,2:fail;", footer)
        self.assertIn("no entry for case(s) 2", self.refuses("gate-result", self.TABLE_FAIL, results={"1": "pass"}))

    def test_only_a_blocked_gate_may_list_no_cases(self):
        self.assertIn("lists no case ids", self.refuses("gate-result", self.CASELESS_FAIL, results={}))
        self.assertIn("results=;", self.post("gate-result", self.CASELESS_BLOCKED, results={}))

    def test_blocked_cases_disagree_with_pass_and_blocked_needs_a_blocked_case(self):
        self.assertIn("did not pass", self.refuses("gate-result", GATE_PASS, results={"1": "pass", "2": "blocked"}))
        blocked = GATE_PASS.replace("**Verdict:** PASS — both ran", "**Verdict:** BLOCKED — fixture missing")
        self.assertIn("no case is blocked", self.refuses("gate-result", blocked, results={"1": "pass", "2": "pass"}))

    def test_a_case_named_twice_is_refused(self):
        self.assertIn("a second time", self.refuses("spec", SPEC_TC, classes={"TC1": "ac", "1": "live", "2": "ac"}))

    def test_gate_time_reads_the_callers_directory_not_the_tools_worktree(self):
        git_dir = self.dir / "gate-git"
        git_dir.mkdir()
        (git_dir / "LANE3_ACTIVE").touch()
        seen = {}

        def fake_run(cmd, cwd=None, **kw):
            seen["cwd"] = cwd
            return unittest.mock.Mock(returncode=0, stdout=str(git_dir) + "\n")
        with unittest.mock.patch.dict(os.environ, {"MISE_ORIGINAL_CWD": "/the/gate/worktree"}), \
             unittest.mock.patch.object(P.subprocess, "run", side_effect=fake_run):
            self.assertNotEqual(P.gate_ms(), "unknown")
        self.assertEqual(seen["cwd"], "/the/gate/worktree")

    def test_a_free_text_field_cannot_shadow_the_case_map(self):
        import eras  # noqa: PLC0415
        footer = ("<!-- l1-post v1; kind=gate-result; posted-by=LANE3; body-sha256=ab; "
                  "ack-no-pr-required=see results=1:pass; results=1:fail; gate-ms=unknown -->")
        self.assertEqual(eras.parse_case_map(footer, "results"), {"1": "fail"})


class GateTimeIsDerived(Case):
    """TC3."""

    def git_dir(self, marker_age: float | None) -> Path:
        git_dir = self.dir / "git"
        git_dir.mkdir(exist_ok=True)
        marker = git_dir / "LANE3_ACTIVE"
        if marker_age is not None:
            marker.touch()
            os.utime(marker, (time.time() - marker_age, time.time() - marker_age))
        return git_dir

    def gate_ms_with(self, git_dir: Path) -> str:
        done = unittest.mock.Mock(returncode=0, stdout=str(git_dir) + "\n")
        with unittest.mock.patch.object(P.subprocess, "run", return_value=done):
            return P.gate_ms()

    def test_a_missing_marker_reads_unknown_never_zero(self):
        self.assertEqual(self.gate_ms_with(self.git_dir(None)), "unknown")

    def test_a_stale_marker_reads_unknown(self):
        self.assertEqual(self.gate_ms_with(self.git_dir(P.LANE3_MARKER_MAX_AGE_SECONDS + 60)), "unknown")

    def test_a_fresh_marker_gives_elapsed_milliseconds(self):
        value = self.gate_ms_with(self.git_dir(90))
        self.assertTrue(value.isdigit() and 89_000 <= int(value) <= 120_000, value)


def _footered(body: str, kind: str, extra: str) -> str:
    digest = hashlib.sha256(body.rstrip("\n").encode()).hexdigest()
    return f"{body.rstrip()}\n\n<!-- l1-post v1; kind={kind}; posted-by=LANE3; body-sha256={digest}; {extra} -->"


class ExtractorCarriesScalarCounts(unittest.TestCase):
    """TC4 (replaced by the 2026-10-04 correction): scalars only, so emit()
    accepts them; the pairing adds per-class fails; Lane 3 writes no telemetry."""

    def events(self, spec_extra: str, gate_extra: str) -> list[dict]:
        import extract_threads  # noqa: PLC0415
        spec = {"id": 10, "created_at": "2026-10-04T10:00:00Z", "body": _footered(SPEC_TC, "spec", spec_extra)}
        gate = {"id": 11, "created_at": "2026-10-04T11:00:00Z",
                "body": _footered(GATE_FAIL, "gate-result", gate_extra)}
        get = lambda path: [[spec, gate]] if "comments" in path else [[]]
        return extract_threads.issue_events("vitalharmony/harmonic-forge", 1, get,
                                            account="vitalharmony", org="vitalharmony")

    def test_a_new_format_spec_and_gate_are_accepted_by_emit(self):
        import emit  # noqa: PLC0415
        events = self.events("classes=1:ac,2:existing", "results=1:pass,2:fail; gate-ms=4000")
        gate = next(e for e in events if e["event_type"] == "gate.fail")
        self.assertEqual((gate["attrs"]["paired"], gate["attrs"]["fail_existing"], gate["attrs"]["fail_ac"]),
                         (True, 1, 0))
        spec = next(e for e in events if e["event_type"] == "spec.posted")
        self.assertEqual((spec["attrs"]["tc_ac"], spec["attrs"]["tc_existing"]), (1, 1))
        with tempfile.TemporaryDirectory() as store, \
             unittest.mock.patch.dict(os.environ, {"HARMONIC_FORGE_TELEMETRY_STORE": store}):
            counts = emit.emit([e for e in events if e["event_type"] in ("gate.fail", "spec.posted")])
        self.assertEqual((counts["written"], counts["rejected"]), (2, []))

    def test_a_gate_whose_spec_has_other_ids_is_unpaired(self):
        events = self.events("classes=1:ac,2:existing,3:live", "results=1:pass,2:fail; gate-ms=unknown")
        gate = next(e for e in events if e["event_type"] == "gate.fail")
        self.assertIs(gate["attrs"]["paired"], False)
        self.assertNotIn("fail_ac", gate["attrs"])
        self.assertIs(gate["attrs"]["gate_ms_known"], False)
        self.assertNotIn("gate_ms", gate["attrs"])

    def test_lane3_s_posting_tool_imports_nothing_from_telemetry(self):
        tree = ast.parse((HERE / "post_lane_discussion.py").read_text(encoding="utf-8"))
        names = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        names |= {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
        self.assertFalse(names & {"emit", "eras", "extract_threads", "archive"}, names)
        self.assertNotIn("telemetry", (HERE / "post_lane_discussion.py").read_text(encoding="utf-8").split(
            "_FORGE = ")[1].split("\n")[0])


class ExistingConsumersStillParse(unittest.TestCase):
    """TC5: the new fields sit on the footer's one physical line, so every
    footer reader still reads kind, sha and digest from a new-format post."""

    NEW = _footered(GATE_PASS, "gate-result", "results=1:pass,2:pass; gate-ms=1000")
    SPEC_NEW = _footered(SPEC_TC, "spec", "classes=1:ac,2:live")

    def test_eras_finds_the_footer(self):
        import eras  # noqa: PLC0415
        self.assertIn("kind=gate-result", eras.own_footer(eras.clean(self.NEW)))

    def test_check_lane3_ready_reads_kind_and_verifies_the_digest(self):
        import check_lane3_ready as clr  # noqa: PLC0415
        self.assertEqual(clr.FOOTER_KIND.search(self.NEW).group(1), "gate-result")
        self.assertTrue(clr.verify_body_sha256({"body": self.NEW}))
        self.assertTrue(clr.verify_body_sha256({"body": self.SPEC_NEW}))

    def test_gate_ci_still_reads_the_verdict(self):
        import gate_ci  # noqa: PLC0415
        self.assertEqual(gate_ci.verdict_of(self.NEW), "PASS")

    def test_lane_state_reads_a_validated_transition(self):
        import eras  # noqa: PLC0415
        timeline = eras.lane_state.parse_timeline([{"id": 1, "created_at": "2026-10-04T10:00:00Z",
                                                    "body": self.SPEC_NEW}])
        self.assertTrue(any(t.key == eras.lane_state.KEY_SPEC_POSTED and t.validated for t in timeline))

    def test_watch_and_fetch_footer_readers_match(self):
        import fetch_lane1_context  # noqa: PLC0415
        import watch_lane_posts  # noqa: PLC0415
        for module in (fetch_lane1_context, watch_lane_posts):
            patterns = [v for v in vars(module).values()
                        if isinstance(v, __import__("re").Pattern) and "l1-post" in v.pattern]
            self.assertTrue(patterns, module.__name__)
            self.assertTrue(any(p.search(self.NEW) for p in patterns), module.__name__)


class ProducerOutputStillParses(unittest.TestCase):
    """TC5, driven by the real producer: footer() with case fields, read back by
    every footer consumer (a change that broke the one-line shape fails here)."""

    def posted(self, kind: str, body: str, fields: str) -> str:
        return body.rstrip("\n") + P.footer(kind, body, "LANE3", case_fields=fields)

    def test_every_consumer_reads_a_real_new_format_post(self):
        import re  # noqa: PLC0415
        import check_lane3_ready as clr  # noqa: PLC0415
        import eras  # noqa: PLC0415
        import fetch_lane1_context  # noqa: PLC0415
        import gate_ci  # noqa: PLC0415
        import watch_lane_posts  # noqa: PLC0415
        import _standing_grant  # noqa: PLC0415
        gate = self.posted("gate-result", GATE_PASS, "results=1:pass,2:pass; gate-ms=1000")
        spec = self.posted("spec", SPEC_TC, "classes=1:ac,2:live")
        self.assertEqual(gate.rstrip().count("\n<!--"), 1, "the footer stays one physical line")
        last = gate.rstrip().splitlines()[-1]
        self.assertTrue(last.startswith("<!-- l1-post") and "results=1:pass,2:pass" in last, last)
        self.assertIn("classes=1:ac,2:live", spec.rstrip().splitlines()[-1])
        self.assertEqual(clr.FOOTER_KIND.search(gate).group(1), "gate-result")
        self.assertTrue(clr.verify_body_sha256({"body": gate}) and clr.verify_body_sha256({"body": spec}))
        self.assertEqual(gate_ci.verdict_of(gate), "PASS")
        self.assertIn("results=1:pass,2:pass", eras.own_footer(eras.clean(gate)))
        self.assertTrue(_standing_grant._BODY_SHA_MARKER.search(gate))
        timeline = eras.lane_state.parse_timeline([{"id": 1, "created_at": "2026-10-04T10:00:00Z", "body": spec}])
        self.assertTrue(any(t.key == eras.lane_state.KEY_SPEC_POSTED and t.validated for t in timeline))
        for module in (fetch_lane1_context, watch_lane_posts):
            patterns = [v for v in vars(module).values() if isinstance(v, re.Pattern) and "l1-post" in v.pattern]
            self.assertTrue(any(pattern.search(gate) for pattern in patterns), module.__name__)


class ReportNeverReadsAbsenceAsZero(unittest.TestCase):
    """TC6."""

    def test_a_footer_less_gate_is_unmeasured_and_no_attempts_is_missing_input(self):
        gates = [
            {"source": "gh-thread", "event_type": "gate.pass", "repo": "o/r", "issue": 1,
             "ts": "2026-10-04T10:00:00Z", "attrs": {"verdict": "PASS"}},
            {"source": "gh-thread", "event_type": "gate.fail", "repo": "o/r", "issue": 2,
             "ts": "2026-10-04T11:00:00Z", "attrs": {"verdict": "FAIL", "tc_count": 2, "fail_count": 1,
                                                       "paired": True, "case_ac": 1, "case_existing": 1,
                                                       "case_live": 0, "fail_ac": 0, "fail_existing": 1,
                                                       "fail_live": 0, "gate_ms": 5000, "gate_ms_known": True}},
            {"source": "gh-thread", "event_type": "gate.pass", "repo": "o/r", "issue": 3,
             "ts": "2026-10-04T12:00:00Z", "attrs": {"verdict": "PASS", "tc_count": 1, "paired": False}},
        ]
        report = VR.build(gates)
        self.assertEqual(report["buckets"], {"measured": 1, "unmeasured": 1, "unpaired": 1, "missing input": 3})
        text = VR.render(report)
        self.assertIn("- unmeasured: 1", text)
        self.assertIn("missing input", text)
        self.assertIn("token", text)
        self.assertEqual(report["aggregate"]["gates"], 1)

    GATE = {"source": "gh-thread", "event_type": "gate.fail", "repo": "o/r", "issue": 2,
            "ts": "2026-10-04T11:00:00Z", "extractor_version": "threads-3",
            "attrs": {"verdict": "FAIL", "comment_id": "9", "edited_at": "x", "tc_count": 1,
                      "paired": True, "case_ac": 1, "fail_ac": 1}}

    def test_a_blocked_gate_is_counted(self):
        blocked = {**self.GATE, "event_type": "blocked.lane", "attrs": {"verdict": "BLOCKED"}}
        self.assertEqual(VR.build([blocked])["buckets"]["unmeasured"], 1)

    def test_one_reading_per_comment_across_extractor_versions(self):
        old = {**self.GATE, "extractor_version": "threads-2", "attrs": {"verdict": "FAIL", "comment_id": "9"}}
        report = VR.build([old, self.GATE])
        self.assertEqual((report["buckets"]["measured"], report["buckets"]["unmeasured"]), (1, 0))

    def test_ready_for_l3_attempts_join_by_repo_and_issue(self):
        attempts = [{"event_type": "ready-for-l3.attempt", "repo": "O/R", "issue": 2,
                     "attrs": {"local_check_ms": 1000, "ci_green_local_red": True}},
                    {"event_type": "ready-for-l3.attempt", "repo": "o/r", "issue": 2,
                     "attrs": {"local_check_ms": 3000, "ci_green_local_red": False}}]
        report = VR.build([self.GATE, *attempts])
        self.assertEqual(report["buckets"]["missing input"], 0)
        row = [line for line in VR.render(report).splitlines() if line.startswith("| o/r#2")][0]
        self.assertIn("| 2 | 2,000 | 1 |", row)

    def test_unreadable_store_lines_are_reported(self):
        with tempfile.TemporaryDirectory() as store:
            part = Path(store) / "events" / "a" / "o" / "gh-thread" / "2026-10.jsonl"
            part.parent.mkdir(parents=True)
            part.write_text(json.dumps(self.GATE) + "\n{torn line\n")
            skipped = []
            events = VR.load_events(Path(store), None, None, skipped)
        self.assertEqual(len(events), 1)
        self.assertEqual(len(skipped), 1)
        self.assertIn("could not be read", VR.render(VR.build(events), skipped))


if __name__ == "__main__":
    unittest.main()
