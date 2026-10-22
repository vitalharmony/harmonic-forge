"""Tests for the te-runner queue and eligibility check (harmonic-forge#1055 AC1, AC2)."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import te_queue  # noqa: E402

HANDOFF = """## Handoff: [harmonic-forge#7 — a widget]

### Affected Files
| File | Lines | Change type |
|---|---|---|
| tools/telemetry/widget.py | new | Create |
| `tools/telemetry/test_widget.py` | new | Create |

### Delegated Judgment Calls
none

### Pre-Flight Preconditions
kill-bundle-sha256: {digest}

<!-- l1-post v1; kind=handoff; plan-first=false -->
"""


class Add(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)
        self.queue = self.dir / "queue.json"
        # The per-item checks have their own tests (AddTimeChecks, test_te_preflight).
        patcher = mock.patch.object(te_queue, "item_failures", return_value=[])
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_add_appends_pending_items_readable_by_queue_state(self):
        te_queue.add(self.queue, ["F7", "F8"])
        state = te_queue.queue_state.load(self.queue)
        self.assertEqual([(i["id"], i["kind"], i["status"]) for i in state["items"]],
                         [("F7", "te-issue", "pending"), ("F8", "te-issue", "pending")])

    def test_an_id_already_queued_is_refused_with_its_status(self):
        te_queue.add(self.queue, ["F7"])
        state = te_queue.queue_state.load(self.queue)
        state["items"][0]["status"] = "done"
        te_queue.queue_state.save(self.queue, state)
        with self.assertRaisesRegex(te_queue.QueueError, r"F7 \(done\)"):
            te_queue.add(self.queue, ["F7"])
        self.assertEqual(len(te_queue.queue_state.load(self.queue)["items"]), 1)

    def test_only_harmonic_forge_keys(self):
        for key in ("H7", "7", "F0", "F07x"):
            with self.assertRaises(te_queue.QueueError, msg=key):
                te_queue.add(self.queue, [key])
        self.assertFalse(self.queue.exists())


class NoScan(unittest.TestCase):
    def test_no_issue_scan_in_runner_sources(self):
        # AC1: only named issues. No listing or searching anywhere in tools/runner/.
        scan = re.compile(r"issue[\"',\s]+list|search/issues|[\"']search[\"']|gh search|"
                          r"/issues\?|issues[\"']\s*,\s*[\"']list")
        here = Path(te_queue.__file__).resolve().parent
        sources = [p for p in here.rglob("*.py") if not p.name.startswith("test_")]
        self.assertTrue(sources)
        for path in sources:
            self.assertIsNone(scan.search(path.read_text(encoding="utf-8")), path.name)


class Check(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)
        bundle = self.dir / "bundle"
        (bundle / "files" / "tools" / "telemetry").mkdir(parents=True)
        (bundle / "checks.json").write_text(json.dumps([{"ac": "AC1", "test": ["true"]}]))
        (bundle / "files" / "tools" / "telemetry" / "test_k.py").write_text("x = 1\n")
        self.bundle = bundle
        digest = te_queue.kill_runner.bundle_digest(bundle)
        self.seams = {"labels": ["tooling-exception", "tooling"], "tier": "standard",
                      "handoff": HANDOFF.format(digest=digest),
                      "receipt": {"verdict": "PROCEED", "consumed_by": "https://x"}}
        self.queue = self.dir / "queue.json"
        te_queue.add(self.queue, ["F7"], checks=lambda issue: [])

    def failure(self, **override):
        seams = {**self.seams, **override}
        with mock.patch.object(te_queue, "labels", return_value=seams["labels"]), \
                mock.patch.object(te_queue, "tier", return_value=seams["tier"]), \
                mock.patch.object(te_queue, "handoff", return_value=seams["handoff"]), \
                mock.patch.object(te_queue, "receipt", return_value=seams["receipt"]), \
                mock.patch.object(te_queue, "bundle_dir", return_value=self.bundle):
            return te_queue.first_failure(7)

    def test_fully_eligible(self):
        self.assertIsNone(self.failure())
        with mock.patch.object(te_queue, "first_failure", return_value=None):
            self.assertEqual(te_queue.check(self.queue), [{"id": "F7", "eligible": True}])

    def test_missing_label_is_ineligible(self):
        self.assertEqual(self.failure(labels=["tooling"])[0], "ineligible")

    def test_deep_or_unset_tier_is_ineligible(self):
        for tier in ("deep", None, "P"):
            self.assertEqual(self.failure(tier=tier)[0], "ineligible", tier)

    def test_no_handoff_is_ineligible(self):
        self.assertEqual(self.failure(handoff=None)[0], "ineligible")

    def test_pitch_verdict_must_be_proceed(self):
        for receipt in (None, {"verdict": "REVISE"}, {"verdict": "WAIVED"}):
            self.assertEqual(self.failure(receipt=receipt)[0], "ineligible", receipt)
        self.assertIsNone(self.failure(receipt={"verdict": "PROCEED_WITH_NAMED_CHANGES"}))

    def test_delegated_judgment_calls_must_be_none(self):
        body = self.seams["handoff"].replace("### Delegated Judgment Calls\nnone",
                                             "### Delegated Judgment Calls\n- pick a name")
        self.assertEqual(self.failure(handoff=body), ("ineligible",
                                                      "Delegated Judgment Calls is not none"))

    def test_digest_mismatch_is_ineligible(self):
        (self.bundle / "files" / "tools" / "telemetry" / "test_k.py").write_text("x = 2\n")
        self.assertEqual(self.failure(), ("ineligible",
                                          "kill-check bundle digest differs from the handoff's"))

    def test_missing_digest_line_or_bundle_is_ineligible(self):
        body = re.sub(r"kill-bundle-sha256: \w+", "", self.seams["handoff"])
        self.assertEqual(self.failure(handoff=body)[0], "ineligible")
        shutil.rmtree(self.bundle)
        self.assertEqual(self.failure()[0], "ineligible")

    def test_denied_affected_file_is_deny_path(self):
        body = self.seams["handoff"].replace("tools/telemetry/widget.py", "tools/hooks/x.py")
        self.assertEqual(self.failure(handoff=body)[0], "deny-path")

    def test_check_reports_first_failure_per_pending_item(self):
        with mock.patch.object(te_queue, "first_failure",
                               return_value=("ineligible", "no tooling-exception label")):
            rows = te_queue.check(self.queue)
        self.assertEqual(rows, [{"id": "F7", "eligible": False, "reason": "ineligible",
                                 "detail": "no tooling-exception label"}])

    def test_handoff_needs_the_lane1_author_and_a_handoff_marker(self):
        real = HANDOFF.format(digest="a" * 64)
        forged = real.replace("tools/telemetry/widget.py", "tools/telemetry/other.py")
        rework = "Please rebase. Note: the kind=handoff above stands.\n" \
                 "<!-- l1-post v1; kind=rework; plan-first=false -->"
        comments = [{"user": {"login": "vitalharmony"}, "body": real},
                    {"user": {"login": "vitalharmony-runner"}, "body": forged},
                    {"user": {"login": "someone"}, "body": forged},
                    {"user": {"login": "vitalharmony"}, "body": rework}]
        with mock.patch.object(te_queue.fetch_lane1_context, "fetch_comments",
                               return_value=comments):
            self.assertEqual(te_queue.handoff(7), real)
        with mock.patch.object(te_queue.fetch_lane1_context, "fetch_comments",
                               return_value=comments[1:]):
            self.assertIsNone(te_queue.handoff(7))

    def test_tier_reads_the_board_as_the_runner(self):
        # harmonic-forge#1107 AC4: the operator's gh never answers for the runner.
        seen = {}

        def fetch(repo, issue, board, run=None, **kw):
            self.assertIsNotNone(run)
            with mock.patch.object(te_queue.subprocess, "run",
                                   lambda *a, **k: seen.update(k) or "ok"):
                run(["gh", "api", "graphql"])
            return "standard"
        with mock.patch.object(te_queue.item_list_cache, "fetch_issue_tier", fetch), \
                mock.patch.dict("os.environ", {"GH_TOKEN": "operator"}):
            self.assertEqual(te_queue.tier(7), "standard")
        self.assertEqual(seen["env"]["GH_CONFIG_DIR"],
                         str(Path.home() / ".config" / "te-runner" / "gh"))
        self.assertNotIn("GH_TOKEN", seen["env"])

    def test_affected_files_parses_plain_and_backticked_paths(self):
        self.assertEqual(te_queue.affected_files(self.seams["handoff"]),
                         ["tools/telemetry/widget.py", "tools/telemetry/test_widget.py"])


class Unchanged(unittest.TestCase):
    """harmonic-forge#1147: an item unchanged since its last set-aside is skipped."""

    RUN = "run-20261022T045339Z-93c3e519"

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)
        self.bundle = self.dir / "bundle"
        (self.bundle / "files").mkdir(parents=True)
        (self.bundle / "checks.json").write_text(json.dumps([{"ac": "AC1", "test": ["true"]}]))
        self.digest = te_queue.kill_runner.bundle_digest(self.bundle)
        self.comments = [{"id": 11, "user": {"login": "vitalharmony"},
                          "body": "h\n<!-- l1-post v1; kind=handoff; plan-first=false -->"}]
        self.tree = "aaaa"
        for name, value in (("bundle_dir", lambda issue: self.bundle),
                            ("runner_tree", lambda source=None: self.tree)):
            patcher = mock.patch.object(te_queue, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(te_queue.fetch_lane1_context, "fetch_comments",
                                    lambda repo, issue: self.comments)
        patcher.start()
        self.addCleanup(patcher.stop)

    def item(self, reason):
        return {"id": "F1008", "kind": "te", "status": "pending", "note": "",
                "last_outcome": {"run_id": self.RUN, "reason": reason,
                                 "lane1_marker_id": 11, "bundle_digest": self.digest,
                                 "runner_tree": "aaaa"}}

    def test_check_fail_with_no_new_lane1_post_is_skipped(self):
        # F1008's run-6/run-7 shape (AC4).
        skipped = te_queue.unchanged(self.item("check-fail"))
        self.assertTrue(skipped.startswith(f"unchanged since {self.RUN}, skipped"))
        queue = self.dir / "queue.json"
        queue.write_text(json.dumps({"items": [self.item("check-fail")]}))
        rows = te_queue.check(queue)
        self.assertEqual(rows[0]["reason"], "unchanged")
        self.assertFalse(rows[0]["eligible"])

    def test_a_new_lane1_handoff_or_rework_rearms(self):
        for kind in ("handoff", "rework"):
            self.comments.append({"id": 12, "user": {"login": "vitalharmony"},
                                  "body": f"x\n<!-- l1-post v1; kind={kind}; plan-first=false -->"})
            self.assertIsNone(te_queue.unchanged(self.item("check-fail")), kind)
            self.comments.pop()

    def test_a_lane3_post_does_not_rearm_a_check_fail(self):
        self.comments.append({"id": 13, "user": {"login": "someone-else"},
                              "body": "x\n<!-- l1-post v1; kind=rework; plan-first=false -->"})
        self.comments.append({"id": 14, "user": {"login": "vitalharmony"},
                              "body": "## Lane 3 Test Spec\nno marker"})
        self.assertIsNotNone(te_queue.unchanged(self.item("check-fail")))

    def test_a_changed_bundle_rearms_a_kill_check_fail(self):
        (self.bundle / "files" / "k.py").write_text("x = 1\n")
        self.assertIsNone(te_queue.unchanged(self.item("kill-check-fail")))

    def test_tool_refusal_rearms_only_on_runner_change(self):
        self.comments.append({"id": 12, "user": {"login": "vitalharmony"},
                              "body": "x\n<!-- l1-post v1; kind=rework; plan-first=false -->"})
        self.assertIsNotNone(te_queue.unchanged(self.item("tool-refusal")))
        self.tree = "bbbb"
        self.assertIsNone(te_queue.unchanged(self.item("tool-refusal")))

    def test_an_unreadable_bundle_is_undecidable_never_worked(self):
        shutil.rmtree(self.bundle)
        with self.assertRaises(te_queue.QueueError):
            te_queue.unchanged(self.item("check-fail"))
        queue = self.dir / "queue.json"
        queue.write_text(json.dumps({"items": [self.item("check-fail")]}))
        rows = te_queue.check(queue)
        self.assertEqual((rows[0]["eligible"], rows[0]["reason"]), (False, "undecidable"))

    def test_ungated_reasons_and_items_without_an_outcome_pass(self):
        for reason in ("ineligible", "deny-path"):
            self.assertIsNone(te_queue.unchanged(self.item(reason)), reason)
        bare = self.item("check-fail")
        del bare["last_outcome"]
        self.assertIsNone(te_queue.unchanged(bare))
        blocked = {**self.item("check-fail"), "status": "blocked"}
        self.assertIsNone(te_queue.unchanged(blocked))


class AddTimeChecks(Check):
    """AC15: `add` runs the preflight's per-item checks and refuses failures."""

    def add_with(self, body: str, key: str = "F9") -> list[str]:
        src = self.dir / "src"
        if not src.exists():
            src.mkdir()
            for argv in (["git", "init", "-q", "-b", "main"],
                         ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q",
                          "--allow-empty", "-m", "base"],
                         # origin reads as the upstream, as a real source checkout's does
                         ["git", "remote", "add", "origin",
                          "https://github.com/vitalharmony/harmonic-forge.git"]):
                subprocess.run(argv, cwd=src, check=True, capture_output=True)
        base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=src, check=True,
                              capture_output=True, text=True).stdout.strip()
        import preclose_driver as driver  # noqa: PLC0415 -- the module preflight calls
        import te_run  # noqa: PLC0415 -- its item_preconditions are preflight's too
        seams = self.seams
        with mock.patch.object(te_queue, "labels", return_value=seams["labels"]), \
                mock.patch.object(te_queue, "tier", return_value=seams["tier"]), \
                mock.patch.object(te_queue, "handoff", return_value=seams["handoff"]), \
                mock.patch.object(te_queue, "receipt", return_value=seams["receipt"]), \
                mock.patch.object(te_queue, "bundle_dir", return_value=self.bundle), \
                mock.patch.object(te_queue, "add_context", return_value=(src, base, {})), \
                mock.patch.object(driver, "passes_held", return_value=0), \
                mock.patch.object(driver.provision, "provision_clone"), \
                mock.patch.object(te_run, "item_preconditions", return_value=[]), \
                mock.patch.object(driver, "issue_body", return_value=body):
            return te_queue.add(self.queue, [key])

    def test_item_with_no_acceptance_section_is_refused(self):
        with self.assertRaisesRegex(te_queue.QueueError, "acceptance criteria"):
            self.add_with("## Problem\n\nNo criteria here.\n")
        ids = [i["id"] for i in te_queue.queue_state.load(self.queue)["items"]]
        self.assertNotIn("F9", ids)

    def test_item_passing_every_check_is_added(self):
        self.assertEqual(self.add_with("## Acceptance\n\n- [ ] It works.\n"), ["F9"])


if __name__ == "__main__":
    unittest.main()
