"""Tests for the te-runner queue and eligibility check (harmonic-forge#1055 AC1, AC2)."""
from __future__ import annotations

import json
import re
import shutil
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
        te_queue.add(self.queue, ["F7"])

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

    def test_affected_files_parses_plain_and_backticked_paths(self):
        self.assertEqual(te_queue.affected_files(self.seams["handoff"]),
                         ["tools/telemetry/widget.py", "tools/telemetry/test_widget.py"])


if __name__ == "__main__":
    unittest.main()
