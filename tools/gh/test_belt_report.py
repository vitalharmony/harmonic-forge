#!/usr/bin/env python3
"""harmonic-forge#519 — the tick-log reader.

The five behaviours Lane 1's spec named as test cases, plus the two that make
the rest load-bearing: an empty log must not read as clean, and the default
path must not touch the network.
"""

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from belt_mechanics import TickLog, _entry  # noqa: E402
import belt_report  # noqa: E402


def write_log(dirpath, records):
    path = Path(dirpath) / "ticks.jsonl"
    with path.open("w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec) + "\n")
    return path


def run_report(path, window=belt_report.DEFAULT_WINDOW, last=0):
    records, malformed = belt_report.load(path)
    buf = io.StringIO()
    with redirect_stdout(buf):
        belt_report.report(records, malformed, last, window, path)
    return buf.getvalue()


class TestAnEmptyLogIsNotACleanReport(unittest.TestCase):
    """TC1, and the reason this issue exists at all.

    The `LANE3_ACTIVE` marker was write-only and read as fine. A reporter that
    prints zeros against an empty file repeats that defect exactly."""

    def test_a_missing_log_reports_no_data(self):
        with tempfile.TemporaryDirectory() as d:
            out = run_report(Path(d) / "absent.jsonl")
        self.assertIn("NO DATA", out)
        self.assertNotIn("FINDINGS", out)

    def test_an_existing_but_empty_log_reports_no_data(self):
        """Distinct from the above: the file exists, so the belt may look armed."""
        with tempfile.TemporaryDirectory() as d:
            path = write_log(d, [])
            out = run_report(path)
        self.assertIn("NO DATA", out)
        self.assertIn("zero records", out)

    def test_a_healthy_log_with_no_findings_says_so_in_words(self):
        """AC6: two lines, not zero. Silence is not a report."""
        with tempfile.TemporaryDirectory() as d:
            path = write_log(d, [{
                "ts": "2026-09-09T05:00:00Z", "lane": "2", "trigger": "monitor",
                "calls_rest": 2, "calls_graphql": 0,
                "repos_polled": [{"account": "vitalharmony", "repo": "hrse", "ok": True}],
                "matched": [_entry("hrse#1", "2026-09-09T04:59:00Z")],
                "emitted": [_entry("hrse#1")], "owed_found": [],
                "actions_taken": ["posted on hrse#1"], "lock": "acquired",
            }])
            out = run_report(path)
        self.assertNotIn("NO DATA", out)
        self.assertIn("FINDINGS", out)
        self.assertIn("none —", out)


class TestDetectionToActionIsLabelledHonestly(unittest.TestCase):
    """TC2. The number is real; the claim about what it measures is the risk."""

    def _two_tick_log(self, d):
        return write_log(d, [
            {"ts": "2026-09-09T05:00:00Z", "lane": "2", "trigger": "monitor",
             "matched": [_entry("hrse#1725", "2026-09-09T04:50:00Z")],
             "emitted": [], "owed_found": [], "actions_taken": [],
             "repos_polled": [], "calls_rest": 1, "calls_graphql": 0, "lock": "acquired"},
            {"ts": "2026-09-09T05:10:00Z", "lane": "2", "trigger": "loop",
             "matched": [], "emitted": [], "owed_found": [],
             "actions_taken": ["replied on hrse#1725"],
             "repos_polled": [], "calls_rest": 1, "calls_graphql": 0, "lock": "acquired"},
        ])

    def test_the_delta_is_computed(self):
        with tempfile.TemporaryDirectory() as d:
            out = run_report(self._two_tick_log(d))
        self.assertIn("lane 2: n=1", out)
        self.assertIn("median=600s", out)

    def test_it_is_named_detection_to_action_not_latency(self):
        with tempfile.TemporaryDirectory() as d:
            out = run_report(self._two_tick_log(d))
        self.assertIn("DETECTION-TO-ACTION", out)
        self.assertIn("NOT posted-to-action", out)

    def test_posted_to_detected_is_reported_when_posted_at_exists(self):
        """The field F519 added to F518's record, doing the job it was added for."""
        with tempfile.TemporaryDirectory() as d:
            out = run_report(self._two_tick_log(d))
        self.assertIn("posted->detected", out)
        self.assertIn("median=600s", out)

    def test_a_marker_that_never_reached_an_action_is_a_finding(self):
        with tempfile.TemporaryDirectory() as d:
            path = write_log(d, [{
                "ts": "2026-09-09T05:00:00Z", "lane": "2", "trigger": "monitor",
                "matched": [_entry("hrse#1725", "2026-09-09T04:50:00Z")],
                "emitted": [], "owed_found": [], "actions_taken": [],
                "repos_polled": [], "calls_rest": 1, "calls_graphql": 0, "lock": "acquired",
            }])
            out = run_report(path)
        self.assertIn("never reached an action", out)
        self.assertIn("hrse#1725", out)

    def test_a_log_without_posted_at_says_downtime_is_invisible(self):
        with tempfile.TemporaryDirectory() as d:
            path = write_log(d, [{
                "ts": "2026-09-09T05:00:00Z", "lane": "2", "trigger": "monitor",
                "matched": ["hrse#1725"], "emitted": [], "owed_found": [],
                "actions_taken": [], "repos_polled": [],
                "calls_rest": 1, "calls_graphql": 0, "lock": "acquired",
            }])
            out = run_report(path)
        self.assertIn("no matched entry carries a posted_at", out)


class TestAMalformedRefIsReportedNotDropped(unittest.TestCase):
    """TC3. Dropping it understates a repo's count, which reads exactly like a
    quiet repo — the one distinction this report exists to make."""

    def test_a_bare_number_is_a_finding(self):
        with tempfile.TemporaryDirectory() as d:
            path = write_log(d, [{
                "ts": "2026-09-09T05:00:00Z", "lane": "2", "trigger": "monitor",
                "matched": [_entry("1725")], "emitted": [], "owed_found": [],
                "actions_taken": [], "repos_polled": [],
                "calls_rest": 1, "calls_graphql": 0, "lock": "acquired",
            }])
            out = run_report(path)
        self.assertIn("do not match the documented ref format", out)
        self.assertIn("'1725'", out)

    def test_a_wellformed_ref_produces_no_such_finding(self):
        with tempfile.TemporaryDirectory() as d:
            path = write_log(d, [{
                "ts": "2026-09-09T05:00:00Z", "lane": "2", "trigger": "monitor",
                "matched": [_entry("harmonic-forge#518")], "emitted": [],
                "owed_found": [], "actions_taken": ["acted on harmonic-forge#518"],
                "repos_polled": [], "calls_rest": 1, "calls_graphql": 0,
                "lock": "acquired",
            }])
            out = run_report(path)
        self.assertNotIn("do not match the documented ref format", out)

    def test_an_unparseable_line_is_counted(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "ticks.jsonl"
            path.write_text('{"ts": "2026-09-09T05:00:00Z", "lane": "2"}\nnot json\n')
            out = run_report(path)
        self.assertIn("unparseable log line", out)


class TestTheGraphqlDefectNamesItsTick(unittest.TestCase):
    """AC4. A count tells you it happened; the tick tells you where to look."""

    def test_the_offending_tick_timestamp_is_printed(self):
        with tempfile.TemporaryDirectory() as d:
            path = write_log(d, [{
                "ts": "2026-09-09T05:00:00Z", "lane": "1", "trigger": "monitor",
                "matched": [], "emitted": [], "owed_found": [], "actions_taken": [],
                "repos_polled": [], "calls_rest": 0, "calls_graphql": 3,
                "lock": "acquired",
            }])
            out = run_report(path)
        self.assertIn("GraphQL call(s) from a scheduled path", out)
        self.assertIn("2026-09-09T05:00:00Z", out)


class TestTheWindowIsAParameterAndSaysItIsAGuess(unittest.TestCase):
    """AC2. The hardcoded `n >= 3` becomes a flag, and the report refuses to
    present an uncalibrated default as if it were measured."""

    def _quiet_repo_log(self, d, polls):
        return write_log(d, [{
            "ts": f"2026-09-09T05:0{i}:00Z", "lane": "2", "trigger": "monitor",
            "matched": [], "emitted": [], "owed_found": [], "actions_taken": [],
            "repos_polled": [{"account": "vitalharmony", "repo": "hrse", "ok": True}],
            "calls_rest": 1, "calls_graphql": 0, "lock": "acquired",
        } for i in range(polls)])

    def test_below_the_window_is_not_flagged(self):
        with tempfile.TemporaryDirectory() as d:
            out = run_report(self._quiet_repo_log(d, 2), window=5)
        self.assertNotIn("produced zero events", out)

    def test_at_the_window_it_is_flagged(self):
        with tempfile.TemporaryDirectory() as d:
            out = run_report(self._quiet_repo_log(d, 5), window=5)
        self.assertIn("produced zero events", out)

    def test_the_default_is_labelled_unvalidated(self):
        with tempfile.TemporaryDirectory() as d:
            out = run_report(self._quiet_repo_log(d, 1))
        self.assertIn("UNVALIDATED DEFAULT", out)


class TestTheDefaultPathMakesNoApiCall(unittest.TestCase):
    """TC4, and AC1's whole point. Asserted against the source rather than by
    trusting the code path, because a single stray call would be invisible."""

    def test_no_network_call_outside_the_audit_function(self):
        src = Path(__file__).parent.joinpath("belt_report.py").read_text()
        before_audit = src.split("def audit(")[0]
        for forbidden in ("subprocess.run", "gh api", "urllib", "requests."):
            self.assertNotIn(
                forbidden, before_audit,
                f"{forbidden!r} appears before def audit() — the default path must "
                "make no API call (AC1)",
            )

    def test_audit_is_not_wired_into_any_scheduled_path(self):
        """AC16: nothing scheduled spends quota. If `--audit` ever appears in a
        cron, loop or monitor definition, this fails."""
        root = Path(__file__).resolve().parents[2]
        hits = []
        for pattern in ("*.md", "*.toml", "*.json"):
            for f in root.rglob(pattern):
                if ".git/" in str(f):
                    continue
                try:
                    text = f.read_text(encoding="utf-8", errors="ignore")
                except OSError:
                    continue
                if "belt_report.py" in text and "--audit" in text:
                    if "never" not in text and "Never" not in text:
                        hits.append(str(f))
        self.assertEqual(hits, [], f"--audit referenced without a never-scheduled caveat: {hits}")


class TestTickLogRecordsTheMarkerTimestamp(unittest.TestCase):
    """TC5 — F518's suite extended for the new shape, not weakened."""

    def test_record_match_carries_posted_at(self):
        with tempfile.TemporaryDirectory() as d:
            log = TickLog(path=Path(d) / "t.jsonl", lane="2", trigger="monitor")
            log.record_match("hrse#1725", posted_at="2026-09-09T04:50:00Z")
            rec = log.write()
        self.assertEqual(rec["matched"][0]["id"], "hrse#1725")
        self.assertEqual(rec["matched"][0]["posted_at"], "2026-09-09T04:50:00Z")

    def test_a_bare_append_still_writes_a_wellformed_entry(self):
        """A legacy caller must not produce a differently-shaped record — it
        produces a complete one with an absent timestamp, which the reader
        reports rather than silently treating as zero latency."""
        with tempfile.TemporaryDirectory() as d:
            log = TickLog(path=Path(d) / "t.jsonl", lane="2", trigger="monitor")
            log.matched.append("hrse#1725")
            rec = log.write()
        self.assertEqual(rec["matched"], [{"id": "hrse#1725", "posted_at": None}])

    def test_a_missing_timestamp_is_null_never_the_tick_time(self):
        """Filling it in with the tick's own time would make detection-to-action
        read as zero — a fabricated number, worse than an absent one."""
        with tempfile.TemporaryDirectory() as d:
            log = TickLog(path=Path(d) / "t.jsonl", lane="2", trigger="monitor")
            log.record_match("hrse#1")
            rec = log.write()
        self.assertIsNone(rec["matched"][0]["posted_at"])
        self.assertNotEqual(rec["matched"][0]["posted_at"], rec["ts"])

    def test_emitted_and_owed_found_get_the_same_shape(self):
        with tempfile.TemporaryDirectory() as d:
            log = TickLog(path=Path(d) / "t.jsonl", lane="3", trigger="loop")
            log.record_emit("hrse#2", posted_at="2026-09-09T04:00:00Z")
            log.record_owed("hrse#3")
            rec = log.write()
        self.assertEqual(rec["emitted"][0]["posted_at"], "2026-09-09T04:00:00Z")
        self.assertEqual(rec["owed_found"][0]["id"], "hrse#3")


if __name__ == "__main__":
    unittest.main()
