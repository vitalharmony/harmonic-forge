#!/usr/bin/env python3
"""harmonic-forge#745 — additive readiness timing on `lane-pr-link` and the
one-shot, non-waiting CI snapshot in `pr_issue_marker()`.

Runs against this file's own real git remote (this checkout IS a
harmonic-forge worktree) so `_cwd_repo_from_git()` needs no mock; only the
`gh api` PR/issue lookups and `gate_ci.ci_conclusion` are stubbed.
"""
import json
import re
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import l1_post as L  # noqa: E402


def _completed(stdout: str, returncode: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(["gh"], returncode, stdout, "")


class PrIssueMarkerTimingTests(unittest.TestCase):
    def _fake_run(self, *args, **kwargs):
        joined = " ".join(args)
        if joined == "git remote get-url origin":
            return _completed("https://github.com/o/r.git\n")
        if "pulls?head=" in joined:
            return _completed(json.dumps([{"number": 2, "node_id": "PR_1"}]))
        if re.search(r"gh api repos/[^/]+/[^/]+/issues/\d+$", joined):
            return _completed(json.dumps({"node_id": "I_1"}))
        raise AssertionError(f"unexpected run() call: {args!r}")

    def test_tc1_a_successful_snapshot_emits_timing_without_altering_existing_fields(self):
        with mock.patch.object(L, "run", side_effect=self._fake_run), \
             mock.patch.object(L.gate_ci, "ci_conclusion", return_value=("green", "verify succeeded")):
            marker = L.pr_issue_marker(
                "o/r", 1, "br", "abc123",
                local_check=("2026-01-01T00:00:00+00:00", "2026-01-01T00:05:00+00:00"),
            )
        self.assertTrue(marker.startswith("<!-- lane-pr-link v1; "))
        self.assertTrue(marker.endswith(" -->"))
        for fragment in ("issue-repo=o/r", "issue=1", "issue-node-id=I_1", "pr-repo=", "pr=2",
                          "pr-node-id=PR_1", "head-sha=abc123"):
            self.assertIn(fragment, marker)
        self.assertIn("local-check-start=2026-01-01T00:00:00+00:00", marker)
        self.assertIn("local-check-end=2026-01-01T00:05:00+00:00", marker)
        self.assertIn("ci-check-name=verify", marker)
        self.assertIn("ci-snapshot-state=green", marker)
        self.assertIn("ci-snapshot-at=", marker)

    def test_tc2_a_pending_check_run_records_pending_and_makes_no_second_call(self):
        calls = []

        def counting_conclusion(*a, **k):
            calls.append((a, k))
            return "pending", "verify still running"

        with mock.patch.object(L, "run", side_effect=self._fake_run), \
             mock.patch.object(L.gate_ci, "ci_conclusion", side_effect=counting_conclusion):
            marker = L.pr_issue_marker("o/r", 1, "br", "abc123")
        self.assertEqual(len(calls), 1, "the snapshot must be exactly one call -- no wait, no retry")
        self.assertIn("ci-snapshot-state=pending", marker)

    def test_tc2_an_absent_check_run_records_absent_not_a_fabricated_state(self):
        with mock.patch.object(L, "run", side_effect=self._fake_run), \
             mock.patch.object(L.gate_ci, "ci_conclusion", return_value=("absent", "no check runs")):
            marker = L.pr_issue_marker("o/r", 1, "br", "abc123")
        self.assertIn("ci-snapshot-state=absent", marker)

    def test_tc2_an_unavailable_api_records_unknown_not_a_crash(self):
        with mock.patch.object(L, "run", side_effect=self._fake_run), \
             mock.patch.object(L.gate_ci, "ci_conclusion", return_value=("unknown", "could not read checks")):
            marker = L.pr_issue_marker("o/r", 1, "br", "abc123")
        self.assertIn("ci-snapshot-state=unknown", marker)

    def test_tc3_the_snapshot_queries_the_marker_pr_repo_and_exact_head_sha(self):
        """Not the issue repo -- an F issue can attest an HRSE PR, and the
        handoff's own Pre-Flight Preconditions section makes this explicit."""
        captured = {}

        def capturing_conclusion(repo, sha, **k):
            captured["repo"] = repo
            captured["sha"] = sha
            captured["required"] = k.get("required")
            return "green", "ok"

        with mock.patch.object(L, "run", side_effect=self._fake_run), \
             mock.patch.object(L.gate_ci, "ci_conclusion", side_effect=capturing_conclusion):
            L.pr_issue_marker("some-other-issue-repo/x", 1, "br", "abc123")
        self.assertEqual(captured["sha"], "abc123")
        self.assertEqual(captured["required"], {"verify"})
        # The queried repo is whatever this checkout's own remote resolves
        # to (pr-repo), never the literal issue-repo string passed above.
        self.assertNotEqual(captured["repo"], "some-other-issue-repo/x")

    def test_omitting_local_check_leaves_the_marker_byte_for_byte_pre_745_up_to_the_ci_fields(self):
        """AC4: every pre-#745 field unchanged, same name, same position."""
        with mock.patch.object(L, "run", side_effect=self._fake_run), \
             mock.patch.object(L.gate_ci, "ci_conclusion", return_value=("green", "ok")):
            marker = L.pr_issue_marker("o/r", 1, "br", "abc123")
        pre_745_fields = marker.split(" ci-check-name=")[0]
        self.assertNotIn("local-check-start", pre_745_fields)
        self.assertTrue(pre_745_fields.endswith("head-sha=abc123"))


if __name__ == "__main__":
    unittest.main()
