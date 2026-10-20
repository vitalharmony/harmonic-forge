"""Tests for the te-runner report (harmonic-forge#1056)."""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import report  # noqa: E402

GOLDEN = Path(__file__).resolve().parent / "testdata" / "report_golden.md"
NOW = dt.datetime(2026, 10, 20, 12, 0, 0, tzinfo=dt.timezone.utc)
RUN = "run-20261020T120000Z"
CROSS = "Red-team provenance: cross-family (codex / gpt-6.1-sol) — 3 of 3 confirmed"


def green(key: str, pr: str) -> dict:
    return {"id": key, "kind": "te-issue", "status": "done", "note": "", "reason": "",
            "verdict": {"state": "green", "reason": None, "detail": "all clauses passed"},
            "facts": {"kill": [{"ac": "AC1", "base_rc": 1, "head_rc": 0},
                               {"ac": "AC2", "base_rc": 1, "head_rc": 0}],
                      "preclose": {"status": "complete", "survivors": 0, "provenance": CROSS},
                      "live": {"pr_exists": True, "head_sha_matches": True, "ci": "pending"}},
            "ac_titles": ["The widget returns 2.", "Nothing else changes."],
            "diff_stat": "2 files changed, 10 insertions(+)", "head_sha": "a" * 40,
            "branch": f"fix/{key[1:]}-te-runner", "pr": pr, "pushed_sha": "a" * 40,
            "preclose_summary": pr + "#issuecomment-1", "live_read_at": "2026-10-20T11:00:00Z",
            "run_id": RUN}


def aside(key: str, reason: str, detail: str) -> dict:
    return {"id": key, "kind": "te-issue", "status": "done", "note": detail, "reason": reason,
            "verdict": {"state": "set-aside", "reason": reason, "detail": detail},
            "facts": {}, "ac_titles": [], "diff_stat": "", "head_sha": "b" * 40,
            "branch": f"fix/{key[1:]}-te-runner", "pr": "", "pushed_sha": "", "run_id": RUN}


QUEUE = {"run_id": RUN, "items": [
    green("F7", "https://github.com/vitalharmony/harmonic-forge/pull/101"),
    aside("F8", "kill-check-fail", "AC1 passed on the base"),
    {"id": "F9", "kind": "te-issue", "status": "blocked",
     "note": "git worktree add failed: secret-ish free text", "reason": "", "run_id": RUN},
    {"id": "F10", "kind": "te-issue", "status": "pending", "note": ""},
]}


class Render(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)

    def render(self, state: dict) -> str:
        queue = self.dir / "queue.json"
        queue.write_text(json.dumps(state))
        return report.render(queue, self.dir / "out", now=NOW).read_text()

    def test_golden(self):
        self.assertEqual(self.render(QUEUE), GOLDEN.read_text())

    def test_report_path_is_named_by_run_id(self):
        queue = self.dir / "queue.json"
        queue.write_text(json.dumps(QUEUE))
        path = report.render(queue, self.dir / "out", now=NOW)
        self.assertEqual(path.name, "run-20261020T120000Z.md")

    def test_batch_line_lists_only_green_items(self):
        text = self.render(QUEUE)
        block = text.split("```")[1].strip().splitlines()
        self.assertEqual(block, ["BATCH F7", "close F7"])
        self.assertIn("Inert data. Type BATCH only after every card above is checked.", text)

    def test_every_green_card_has_three_unchecked_boxes(self):
        state = {"run_id": RUN, "items": [green("F7", "https://x/pull/1"), green("F11", "https://x/pull/2")]}
        text = self.render(state)
        for box in ("- [ ] scope", "- [ ] containment", "- [ ] honesty"):
            self.assertEqual(text.count(box), 2, box)
        self.assertNotIn("[x]", text)

    def test_card_fields(self):
        text = self.render(QUEUE)
        for field in ("AC1 base 1 / head 0", "Preclose survivors: none", CROSS,
                      "pending — approve workflows, as of 2026-10-20T11:00:00Z, "
                      "not authoritative: re-read live at BATCH time",
                      "https://github.com/vitalharmony/harmonic-forge/pull/101#issuecomment-1",
                      "The widget returns 2.", "2 files changed"):
            self.assertIn(field, text)

    def test_set_aside_entries_name_decision_and_recommendation(self):
        text = self.render(QUEUE)
        section = text.split("## Set aside")[1].split("## Merge")[0]
        entries = [l for l in section.splitlines() if re.match(r"\d+\. ", l)]
        self.assertEqual(len(entries), 2)
        for entry in entries:
            self.assertIn("Decision:", entry)
            self.assertIn("Recommendation:", entry)
        self.assertIn("AC1 passed on the base", entries[0])

    def test_blocked_note_free_text_is_never_rendered(self):
        self.assertNotIn("secret-ish free text", self.render(QUEUE))

    def test_an_earlier_runs_items_are_never_proposed_again(self):
        old = green("F7", "https://x/pull/1")
        old["run_id"] = "run-20261019T020000Z"  # merged and closed after that run
        state = {"run_id": RUN, "items": [old, green("F11", "https://x/pull/2"),
                                          {"id": "F12", "kind": "te-issue", "status": "pending",
                                           "note": ""}]}
        text = self.render(state)
        self.assertEqual(text.split("```")[1].strip().splitlines(), ["BATCH F11", "close F11"])
        self.assertNotIn("F7", text)
        self.assertIn("2 items: 1 green, 0 set aside, 0 blocked, 1 not run.", text)

    def test_empty_queue_says_so(self):
        text = self.render({"items": []})
        for line in ("No items in the queue.", "No green PRs this run.",
                     "Nothing set aside this run.", "(no green PRs: nothing to merge)"):
            self.assertIn(line, text)
        self.assertNotIn("BATCH F", text)

    def test_no_lane_status_token_in_report(self):
        state = {"run_id": RUN, "items": [aside("F8", "check-fail", "L3F gate said L2D was wrong; L1B")]}
        text = self.render(state) + self.render(QUEUE)
        self.assertIsNone(re.search(r"\bL[1-3][BDSPF]\b", text), text)

    def test_item_over_twelve_lines_fails(self):
        # A green item with many ACs and kill checks still renders in at most 12 lines.
        big = green("F7", "https://x/pull/1")
        big["ac_titles"] = [f"criterion {n} " + "x" * 90 for n in range(30)]
        big["facts"]["kill"] = [{"ac": f"AC{n}", "base_rc": 1, "head_rc": 0} for n in range(30)]
        text = self.render({"run_id": RUN, "items": [big]})
        card = text.split("## Green")[1].split("## Set aside")[0].strip().splitlines()
        list_line = [l for l in text.splitlines() if l.startswith("- F7 ·")]
        self.assertLessEqual(len([l for l in card if l.strip()]) + len(list_line),
                             report.MAX_ITEM_LINES)

    def test_detail_is_capped(self):
        text = self.render({"run_id": RUN, "items": [aside("F8", "check-fail", "y" * 500)]})
        self.assertNotIn("y" * 201, text)


class Notify(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)
        self.topic = self.dir / "topic"
        self.topic.write_text("te-runner-secret-topic-1234\n")
        self.report = self.dir / "run.md"
        self.report.write_text("# te-runner report run-x\n\n4 items: 1 green, 1 set aside.\n")
        self.curl = self.dir / "curl"
        self.curl.write_text("#!/bin/sh\nprintf '%s\\n' \"$@\" > \"$(dirname \"$0\")/args\"\n"
                             "exit ${FAKE_CURL_RC:-0}\n")
        self.curl.chmod(0o755)

    def notify(self, **env) -> tuple[int, str]:
        import contextlib
        import io
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, {"NTFY_TOPIC_FILE": str(self.topic), **env}), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = report.notify(self.report, curl=str(self.curl))
        return rc, out.getvalue() + err.getvalue()

    def test_notify_sends_counts_and_path_to_the_topic(self):
        rc, _ = self.notify()
        self.assertEqual(rc, 0)
        args = (self.dir / "args").read_text().splitlines()
        self.assertEqual(args[-1], "https://ntfy.sh/te-runner-secret-topic-1234")
        message = args[args.index("-d") + 1]
        self.assertIn("4 items: 1 green, 1 set aside.", message)
        self.assertIn(str(self.report), message)

    def test_topic_value_is_never_emitted(self):
        for rc_env in ("0", "22"):
            _, output = self.notify(FAKE_CURL_RC=rc_env)
            self.assertNotIn("secret-topic", output, rc_env)

    def test_missing_topic_file_refuses(self):
        self.topic.unlink()
        rc, _ = self.notify()
        self.assertEqual(rc, 2)
        self.assertFalse((self.dir / "args").exists())

    def test_nothing_posts_to_github(self):
        source = Path(report.__file__).read_text()
        # No gh argv token, no GitHub API host, no comment command.
        self.assertIsNone(re.search(r"[\"']gh[\"']|api\.github\.com|\bcomment\b", source))


if __name__ == "__main__":
    unittest.main()
