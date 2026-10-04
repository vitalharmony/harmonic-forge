#!/usr/bin/env python3
"""harmonic-forge#893, as reforged after its sticky-wicket ruling: the case map
is the record of what Lane 3 ran, written into the one-line footer; the prose
body is never parsed to validate it, and telemetry never refuses or fails a
post. The extractor stamps one `measurement` per gate, pairs gates with
specs, and the verification report reads that stamp verbatim, never reading
absence as zero."""
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
             results: dict | None = None, lane: str = "3") -> str:
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
             unittest.mock.patch.dict(os.environ, {"LANE": lane}), \
             unittest.mock.patch.object(P, "comment_body", side_effect=comment_body), \
             unittest.mock.patch.object(P, "require_green_ci", return_value=False), \
             unittest.mock.patch.object(P, "require_round_approval", return_value=None), \
             unittest.mock.patch.object(P, "belt_candidates"), \
             unittest.mock.patch.object(sys, "stdout", new=io.StringIO()):
            P.main()
        return posted["text"]

    def post_with_stderr(self, kind: str, body: str, **kw) -> tuple[str, str]:  # noqa: D401
        err = io.StringIO()
        with unittest.mock.patch.object(sys, "stderr", new=err):
            text = self.post(kind, body, **kw)
        return text, err.getvalue()


def last_line(text: str) -> str:
    return text.rstrip().splitlines()[-1]


class TheMapIsTheRecord(Case):
    """TC1/TC2 under the reforge (R1): the map's keys ARE the case ids; the
    body is never parsed to check them, and only the map's shape is checked."""

    def test_a_spec_map_is_written_on_the_footer_line(self):
        footer = last_line(self.post("spec", SPEC_NUMBERED, classes={"TC2": "existing", "1": "ac"}))
        self.assertTrue(footer.startswith("<!-- l1-post v1; kind=spec;"))
        self.assertIn("; classes=1:ac,2:existing -->", footer)

    def test_ids_the_prose_does_not_name_are_still_recorded(self):
        # The body names TC1 and TC2; the map is the record, so its ids stand.
        footer = last_line(self.post("spec", SPEC_TC, classes={"1": "ac", "2": "live", "3": "existing"}))
        self.assertIn("classes=1:ac,2:live,3:existing", footer)

    def test_a_gate_map_and_its_time_are_written(self):
        footer = last_line(self.post("gate-result", GATE_FAIL, results={"1": "pass", "2": "fail"}))
        self.assertIn("results=1:pass,2:fail; gate-ms=", footer)

    def test_a_table_shaped_report_is_recorded_from_its_map(self):
        # pass-2 finding 1: a report whose cases are in a table, or in prose no
        # reader recognises, posts and records exactly the map it was given.
        body = ("## Lane 3 Gate Results — H1\n\n**Verdict:** FAIL — the second case failed.\n"
                "**Finding:** y\n**Next:** z\n\n| Case | Verdict |\n|---|---|\n| 1 | PASS |\n| 2 | FAIL |\n"
                "```\n| 42 | not a case |\n```\n")
        self.assertIn("results=1:pass,2:fail;", last_line(self.post("gate-result", body,
                                                                    results={"1": "pass", "2": "fail"})))


class TelemetryNeverRefuses(Case):
    """R2: a missing or unusable map never stops a post; it is stamped absent
    and named on stderr. FAIL and BLOCKED must always be publishable."""

    def test_a_gate_with_no_map_posts_with_results_absent(self):
        text, err = self.post_with_stderr("gate-result", GATE_FAIL)
        self.assertIn("results=absent; gate-ms=", last_line(text))
        # Amended TC1: a post with no map names the reason, as an unusable one does.
        self.assertIn("no --tc-results was given", err)

    def test_a_spec_with_no_map_posts_with_classes_absent(self):
        self.assertIn("classes=absent", last_line(self.post("spec", SPEC_TC)))

    def test_an_unusable_map_posts_absent_and_says_why(self):
        for name, mapping, why in (
                ("unknown value", {"1": "pass", "2": "smoke"}, "case 2 is 'smoke'"),
                ("duplicate", {"TC1": "pass", "1": "fail"}, "a second time"),
                ("bad key", {"one!": "pass"}, "is not a case id"),
                ("too many", {str(i): "pass" for i in range(P.MAX_CASES + 1)}, "at most")):
            with self.subTest(name):
                text, err = self.post_with_stderr("gate-result", GATE_FAIL, results=mapping)
                self.assertIn("results=absent;", last_line(text))
                self.assertIn(why, err)

    def test_a_map_contradicting_the_verdict_still_posts(self):
        # Agreement is a report-side confidence flag, never a refusal.
        self.assertIn("results=1:pass,2:pass;",
                      last_line(self.post("gate-result", GATE_FAIL, results={"1": "pass", "2": "pass"})))

    def test_a_blocked_gate_that_ran_nothing_records_an_empty_map(self):
        body = ("## Lane 3 Gate Results — H1 — BLOCKED\n\n**Verdict:** BLOCKED\n"
                "**Finding:** no fixture.\n**Next:** provision it.\n")
        self.assertIn("results=; gate-ms=", last_line(self.post("gate-result", body, results={})))

    def test_gate_time_never_raises(self):
        # pass-2 finding 5: a pruned caller directory, or no git at all.
        with unittest.mock.patch.dict(os.environ, {"MISE_ORIGINAL_CWD": str(self.dir / "gone")}):
            self.assertEqual(P.gate_ms(), "unknown")
        with unittest.mock.patch.object(P.subprocess, "run", side_effect=FileNotFoundError("git")):
            self.assertEqual(P.gate_ms(), "unknown")

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


class KeyedOnTheBody(Case):
    """R3: a gate report posted as `discussion` carries the same fields; a Lane
    1 note that recaps a gate under a nested heading does not."""

    def test_a_gate_report_posted_as_discussion_carries_its_fields(self):
        footer = last_line(self.post("discussion", GATE_FAIL, results={"1": "pass", "2": "fail"}))
        self.assertTrue(footer.startswith("<!-- l1-post v1; kind=discussion; posted-by=LANE3; results="), footer)
        self.assertIn("results=1:pass,2:fail; gate-ms=", footer)

    def test_a_recap_under_another_heading_is_untouched(self):
        recap = "## Lane 1 — closing\n\n### Lane 3 Gate Results — H1 — PASS\n**Verdict:** PASS\n"
        self.assertEqual(last_line(self.post("discussion", recap)),
                         "<!-- l1-post v1; kind=discussion; posted-by=LANE3 -->")

    def test_a_plain_discussion_footer_is_byte_identical(self):
        self.assertEqual(last_line(self.post("discussion", "## A note\n\nhello\n")),
                         "<!-- l1-post v1; kind=discussion; posted-by=LANE3 -->")


class OneRecognizer(Case):
    """Epoch-1 finding 4: the posting side and the extractor share one test,
    so they agree on a fenced or top-level `#` line before the artifact."""

    def test_an_explicit_kind_is_the_artifact_whatever_comes_first(self):
        # The posting side's validate_lead already refuses such a body; the
        # extractor still meets it in history, and must agree on the kind.
        import gate_ci  # noqa: PLC0415
        body = "# H1 — Lane 3 verification\n\n" + GATE_FAIL
        self.assertEqual(gate_ci.lane3_artifact(body, "gate-result", "LANE3"), "gate")
        self.assertIsNone(gate_ci.lane3_artifact(body, None, "LANE3"))

    def test_a_fenced_hash_line_is_not_a_heading_on_either_side(self):
        import eras  # noqa: PLC0415
        body = "```\n# all green\n```\n" + GATE_FAIL
        footer = last_line(self.post("discussion", body, results={"1": "pass", "2": "fail"}))
        self.assertIn("results=1:pass,2:fail;", footer)
        self.assertEqual(eras.lane3_artifact_of(eras.clean(body), "discussion", footer), "gate")

    def test_a_lane_1_relay_records_no_map(self):
        text, err = self.post_with_stderr("gate-result", GATE_FAIL, results={"1": "pass", "2": "fail"},
                                          lane="1")
        self.assertNotIn("results=", last_line(text))
        self.assertIn("not a Lane 3 spec or gate report", err)


class FooterFieldOrder(unittest.TestCase):
    """pass-2 finding 4: the producer appends the case fields LAST, after the
    free-text ack field, so the reader's last-match rule finds the real map."""

    def test_case_fields_follow_the_ack_reason(self):
        import eras  # noqa: PLC0415
        footer = P.footer("gate-result", GATE_PASS, "LANE3", ack_no_pr_required="see results=1:fail",
                          case_fields="results=1:pass,2:pass; gate-ms=unknown")
        self.assertLess(footer.index("ack-no-pr-required="), footer.index("results=1:pass"))
        self.assertEqual(eras.parse_case_map(footer, "results"), {"1": "pass", "2": "pass"})

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


class ExtractorStampsOneMeasurement(unittest.TestCase):
    """TC4 and R4: scalars only, so emit() accepts them; every gate carries one
    stamped `measurement`; pairing adds per-class fails; Lane 3 writes none."""

    def events(self, comments: list[dict]) -> list[dict]:
        import extract_threads  # noqa: PLC0415
        get = lambda path: [comments] if "comments" in path else [[]]
        return extract_threads.issue_events("vitalharmony/harmonic-forge", 1, get,
                                            account="vitalharmony", org="vitalharmony")

    def spec(self, extra: str, cid: int = 10) -> dict:
        return {"id": cid, "created_at": f"2026-10-04T{cid:02d}:00:00Z", "body": _footered(SPEC_TC, "spec", extra)}

    def gate(self, extra: str, cid: int = 11, body: str = GATE_FAIL, kind: str = "gate-result",
             by: str = "LANE3") -> dict:
        footered = _footered(body, kind, extra).replace("posted-by=LANE3", f"posted-by={by}")
        return {"id": cid, "created_at": f"2026-10-04T{cid:02d}:00:00Z", "body": footered}

    def the_gate(self, events: list[dict]) -> dict:
        return next(e for e in events if e["event_type"] in ("gate.pass", "gate.fail", "blocked.lane", "unknown"))

    def test_a_new_format_spec_and_gate_are_accepted_by_emit(self):
        import emit  # noqa: PLC0415
        events = self.events([self.spec("classes=1:ac,2:existing"), self.gate("results=1:pass,2:fail; gate-ms=4000")])
        gate = self.the_gate(events)
        self.assertEqual((gate["attrs"]["measurement"], gate["attrs"]["fail_existing"], gate["attrs"]["fail_ac"]),
                         ("measured", 1, 0))
        self.assertIs(gate["attrs"]["map_agrees"], True)
        with tempfile.TemporaryDirectory() as store, \
             unittest.mock.patch.dict(os.environ, {"HARMONIC_FORGE_TELEMETRY_STORE": store}):
            counts = emit.emit([gate, next(e for e in events if e["event_type"] == "spec.posted")])
        self.assertEqual((counts["written"], counts["rejected"]), (2, []))

    def test_every_state_is_stamped(self):
        blocked = ("## Lane 3 Gate Results — H1 — BLOCKED\n\n**Verdict:** BLOCKED\n**Finding:** x\n**Next:** y\n")
        cases = {
            "unpaired": [self.spec("classes=1:ac,2:existing,3:live"), self.gate("results=1:pass,2:fail; gate-ms=1")],
            "no-map": [self.gate("results=absent; gate-ms=unknown")],
            "blocked-no-cases": [self.gate("results=; gate-ms=unknown", body=blocked)],
            "pre-893": [self.gate("ack-no-pr-required=x")],
            "bypass-route": [{"id": 11, "created_at": "2026-10-04T11:00:00Z", "body": GATE_FAIL}],
        }
        for expected, comments in cases.items():
            with self.subTest(expected):
                self.assertEqual(self.the_gate(self.events(comments))["attrs"]["measurement"], expected)

    def test_an_unclassified_newer_spec_resets_the_pairing(self):
        # pass-2 finding 10: round 2's spec carries no classes; round 1's must not pair.
        events = self.events([self.spec("classes=1:existing,2:existing", cid=10), self.spec("classes=absent", cid=11),
                              self.gate("results=1:pass,2:fail; gate-ms=1", cid=12)])
        gate = self.the_gate(events)
        self.assertEqual((gate["attrs"]["measurement"], gate["attrs"]["paired"]), ("unpaired", False))
        self.assertNotIn("fail_existing", gate["attrs"])

    def test_a_gate_posted_as_discussion_by_lane_3_is_a_measured_gate(self):
        events = self.events([self.spec("classes=1:ac,2:live"),
                              self.gate("results=1:pass,2:fail; gate-ms=1", kind="discussion")])
        self.assertEqual(self.the_gate(events)["attrs"]["measurement"], "measured")

    def test_a_lane_1_recap_is_not_a_gate(self):
        recap = "## Lane 1 — closing\n### Lane 3 Gate Results — H1 — PASS\n**Verdict:** PASS\n"
        events = self.events([self.gate("", body=recap, kind="discussion", by="LANE1")])
        self.assertEqual([e["event_type"] for e in events], ["l1.discussion"])

    def test_a_spec_posted_as_discussion_pairs_and_resets(self):
        # Epoch-1 finding 1.
        spec_discussion = {"id": 10, "created_at": "2026-10-04T10:00:00Z",
                           "body": _footered(SPEC_TC, "discussion", "classes=1:ac,2:live")}
        gate = self.gate("results=1:pass,2:fail; gate-ms=1", cid=12)
        attrs = self.the_gate(self.events([spec_discussion, gate]))["attrs"]
        self.assertEqual((attrs["measurement"], attrs["case_ac"], attrs["fail_live"]), ("measured", 1, 1))
        older = self.spec("classes=1:existing,2:existing", cid=9)
        attrs = self.the_gate(self.events([older, spec_discussion, gate]))["attrs"]
        self.assertEqual((attrs["case_existing"], attrs["case_ac"]), (0, 1))

    def test_a_lane_1_post_leading_with_a_gate_heading_is_not_a_gate(self):
        # Lane 1 relaying a gate verbatim is not a measured gate: no event
        # carries a measurement (lane_state's own reading of the heading stands).
        events = self.events([self.gate("results=1:pass,2:fail; gate-ms=1", kind="discussion", by="LANE1")])
        self.assertIn("l1.discussion", [e["event_type"] for e in events])
        self.assertFalse([e for e in events if "measurement" in e["attrs"]], events)

    def test_a_map_contradicting_the_verdict_is_low_confidence(self):
        events = self.events([self.spec("classes=1:ac,2:live"), self.gate("results=1:pass,2:pass; gate-ms=1")])
        self.assertIs(self.the_gate(events)["attrs"]["map_agrees"], False)

    def test_lane3_s_posting_tool_imports_nothing_from_telemetry(self):
        tree = ast.parse((HERE / "post_lane_discussion.py").read_text(encoding="utf-8"))
        names = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        names |= {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
        self.assertFalse(names & {"emit", "eras", "extract_threads", "archive"}, names)


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


class ReportReadsTheStamp(unittest.TestCase):
    """TC6 and R4: every gate lands in the bucket it was stamped with; absence
    is never read as zero."""

    GATE = {"source": "gh-thread", "event_type": "gate.fail", "repo": "o/r", "issue": 2,
            "ts": "2026-10-04T11:00:00Z", "extractor_version": "threads-4",
            "attrs": {"verdict": "FAIL", "comment_id": "9", "edited_at": "x", "measurement": "measured",
                      "tc_count": 1, "paired": True, "case_ac": 1, "fail_ac": 1, "map_agrees": True}}

    def gate(self, cid: str, measurement: str | None, issue: int = 2, **attrs) -> dict:
        base = {"verdict": "FAIL", "comment_id": cid, "edited_at": "x", **attrs}
        if measurement is not None:
            base["measurement"] = measurement
        return {**self.GATE, "issue": issue, "attrs": base}

    def test_every_bucket_is_the_stamp_and_each_is_printed(self):
        gates = [self.GATE] + [self.gate(str(i), m, issue=10 + i) for i, m in enumerate(
            ("unpaired", "no-map", "blocked-no-cases", "pre-893", "bypass-route"))]
        report = VR.build(gates)
        self.assertEqual({k: v for k, v in report["buckets"].items() if k != "missing input"},
                         dict.fromkeys(VR.BUCKETS, 1))
        text = VR.render(report)
        for name in VR.BUCKETS:
            self.assertIn(f"- {name}: 1", text)
        self.assertEqual(report["aggregate"]["gates"], 1)

    def test_a_blocked_gate_with_no_cases_is_not_a_bypass(self):
        # pass-2 finding 9, inverted: BLOCKED-with-no-cases has its own bucket.
        blocked = {**self.gate("5", "blocked-no-cases"), "event_type": "blocked.lane"}
        buckets = VR.build([blocked])["buckets"]
        self.assertEqual((buckets["blocked-no-cases"], buckets["bypass-route"], buckets["pre-893"]), (1, 0, 0))

    def test_an_older_gate_reading_is_pre_893_and_a_non_gate_is_not_a_gate(self):
        # Epoch-1 finding 6: an unstamped reading is a gate only when its
        # marker says so; L2B/L3B extract as blocked.lane and are never gates.
        old = {**self.GATE, "extractor_version": "threads-3",
               "attrs": {"comment_id": "7", "marker": "gate-result"}}
        l2b = {**self.GATE, "event_type": "blocked.lane", "issue": 9,
               "attrs": {"comment_id": "8", "marker": "L2B"}}
        report = VR.build([old, l2b])
        self.assertEqual(report["buckets"]["pre-893"], 1)
        self.assertNotIn(("o/r", 9), report["issues"])

    def test_a_lane_1_relay_never_reaches_the_report(self):
        # Epoch-1 finding 2: a relay by any --kind adds no gate.
        import extract_threads  # noqa: PLC0415
        gate = _footered(GATE_PASS, "gate-result", "results=1:pass,2:pass; gate-ms=1000")
        relays = [_footered(GATE_PASS, kind, "results=1:pass,2:pass; gate-ms=5").replace(
            "posted-by=LANE3", "posted-by=LANE1") for kind in ("gate-result", "discussion")]
        comments = [{"id": i, "created_at": f"2026-10-04T1{i}:00:00Z", "body": b}
                    for i, b in enumerate([gate, *relays], 1)]
        get = lambda path: [comments] if "comments" in path else [[]]
        events = extract_threads.issue_events("o/r", 2, get, account="a", org="o")
        self.assertEqual(VR.build(events)["aggregate"]["gates"] + VR.build(events)["buckets"]["unpaired"], 1)
        self.assertEqual(len(VR.build(events)["issues"][("o/r", 2)]["gates"]), 1)

    def test_an_unreadable_subdirectory_is_reported_not_read_as_empty(self):
        # Epoch-1 finding 5.
        with tempfile.TemporaryDirectory() as store:
            part = Path(store) / "events" / "acct" / "o" / "gh-thread" / "2026-10.jsonl"
            part.parent.mkdir(parents=True)
            part.write_text(json.dumps(self.GATE) + "\n")
            locked = Path(store) / "events" / "acct"
            locked.chmod(0)
            try:
                skipped = []
                VR.load_events(Path(store), None, None, skipped)
            finally:
                locked.chmod(0o755)
        self.assertTrue(any("unreadable directory" in s for s in skipped), skipped)

    def test_the_newest_extractor_version_wins_numerically(self):
        # pass-2 finding 3: threads-10 is newer than threads-9.
        nine = {**self.gate("9", "pre-893"), "extractor_version": "threads-9"}
        ten = {**self.GATE, "extractor_version": "threads-10"}
        for order in ((nine, ten), (ten, nine)):
            report = VR.build(list(order))
            self.assertEqual((report["buckets"]["measured"], report["buckets"]["pre-893"]), (1, 0))

    def test_low_confidence_gates_are_counted(self):
        shaky = self.gate("8", "measured", issue=3, tc_count=1, paired=True, case_ac=1, map_agrees=False)
        report = VR.build([self.GATE, shaky])
        self.assertEqual(report["aggregate"]["low_confidence"], 1)
        self.assertIn("1 low-confidence", VR.render(report))

    def test_ready_for_l3_attempts_join_by_repo_and_issue(self):
        attempts = [{"event_type": "ready-for-l3.attempt", "repo": "O/R", "issue": 2,
                     "attrs": {"local_check_ms": 1000, "ci_green_local_red": True}},
                    {"event_type": "ready-for-l3.attempt", "repo": "o/r", "issue": 2,
                     "attrs": {"local_check_ms": 3000, "ci_green_local_red": False}}]
        report = VR.build([self.GATE, *attempts])
        self.assertEqual(report["buckets"]["missing input"], 0)
        row = [line for line in VR.render(report).splitlines() if line.startswith("| o/r#2")][0]
        self.assertIn("| 2 | 2,000 | 1 |", row)
        self.assertEqual(VR.build([self.GATE])["buckets"]["missing input"], 1)

    def test_unreadable_store_lines_are_reported(self):
        with tempfile.TemporaryDirectory() as store:
            part = Path(store) / "events" / "a" / "o" / "gh-thread" / "2026-10.jsonl"
            part.parent.mkdir(parents=True)
            part.write_text(json.dumps(self.GATE) + "\n{torn line\n")
            skipped = []
            events = VR.load_events(Path(store), None, None, skipped)
        self.assertEqual((len(events), len(skipped)), (1, 1))
        self.assertIn("could not be read", VR.render(VR.build(events), skipped))

    def test_a_missing_store_is_refused_not_read_as_empty(self):
        # pass-2 finding 6.
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(VR.StoreUnreadable):
                VR.load_events(Path(tmp) / "no-such-store", None, None, [])
            argv = ["verification_report.py", "--store", str(Path(tmp) / "no-such-store")]
            err = io.StringIO()
            with unittest.mock.patch.object(sys, "argv", argv), unittest.mock.patch.object(sys, "stderr", err), \
                 unittest.mock.patch.object(sys, "stdout", io.StringIO()) as out, self.assertRaises(SystemExit) as done:
                VR.main()
        self.assertEqual(done.exception.code, 2)
        self.assertIn("nothing was read", err.getvalue())
        self.assertNotIn("nothing is reported as zero", out.getvalue())


if __name__ == "__main__":
    unittest.main()
