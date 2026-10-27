#!/usr/bin/env python3
"""harmonic-forge#1134: l1_post reuses its own same-SHA check receipt.

The receipt store, the forge HEAD and git are faked; nothing reads the
operator's real ~/.cache receipts or runs a real check.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "hooks"))

import check_receipt as C  # noqa: E402
import l1_post as L  # noqa: E402

SLUG = "vitalharmony__harmonic-forge"
SHA = "a" * 40
FORGE = "f" * 40


def _done(stdout: str = "", code: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], code, stdout, "")


class ReceiptContentTests(unittest.TestCase):
    """AC1: the receipt names what it depends on."""

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patch = mock.patch.object(C, "STORE", Path(self.tmp.name))
        patch.start()
        self.addCleanup(patch.stop)

    def _record(self, env: dict[str, str]) -> dict:
        outputs = {("status", "--porcelain"): "", ("rev-parse", "HEAD"): SHA,
                   ("remote", "get-url", "origin"): "https://github.com/vitalharmony/harmonic-forge.git"}
        with mock.patch.object(C, "_git", side_effect=lambda *a, cwd=None: outputs[a]), \
                mock.patch.object(C, "forge_sha", return_value=FORGE), \
                mock.patch.dict(os.environ, env, clear=False):
            # harmonic-forge#1213: l1-post exports both receipt vars into the
            # check it runs; drop whichever this case did not pass explicitly.
            for key in (C.SOURCE_ENV, C.FORGE_SHA_ENV):
                if key not in env:
                    os.environ.pop(key, None)
            C.record(".")
        return json.loads(C.receipt_path(SLUG, SHA).read_text(encoding="utf-8"))

    def test_record_writes_repo_sha_forge_and_source(self):
        self.assertEqual(self._record({}), {"repo": SLUG, "sha": SHA, "forge_sha": FORGE, "source": "check"})

    def test_inherited_forge_sha_does_not_leak_into_a_case_that_did_not_pass_it(self):
        with mock.patch.dict(os.environ, {C.FORGE_SHA_ENV: "d" * 40, C.SOURCE_ENV: C.L1_POST_SOURCE}):
            self.assertEqual(self._record({}), {"repo": SLUG, "sha": SHA, "forge_sha": FORGE, "source": "check"})

    def test_l1_post_scratch_source(self):
        data = self._record({C.SOURCE_ENV: C.L1_POST_SOURCE})
        self.assertEqual(data["source"], C.L1_POST_SOURCE)

    def test_forge_head_moved_during_check_records_no_forge_sha(self):
        data = self._record({C.SOURCE_ENV: C.L1_POST_SOURCE, C.FORGE_SHA_ENV: "e" * 40})
        self.assertIsNone(data["forge_sha"])

    def test_forge_dirty_before_check_records_no_forge_sha(self):
        data = self._record({C.SOURCE_ENV: C.L1_POST_SOURCE, C.FORGE_SHA_ENV: ""})
        self.assertIsNone(data["forge_sha"])

    def test_forge_unmoved_records_the_pre_check_value(self):
        data = self._record({C.SOURCE_ENV: C.L1_POST_SOURCE, C.FORGE_SHA_ENV: FORGE})
        self.assertEqual(data["forge_sha"], FORGE)

    def test_a_dirty_forge_tree_has_no_identity(self):
        outputs = {("status", "--porcelain"): " M tools/x.py", ("rev-parse", "HEAD"): FORGE}
        with mock.patch.object(C, "_git", side_effect=lambda *a, cwd=None: outputs[a]):
            self.assertIsNone(C.forge_sha())
        outputs[("status", "--porcelain")] = ""
        with mock.patch.object(C, "_git", side_effect=lambda *a, cwd=None: outputs[a]):
            self.assertEqual(C.forge_sha(), FORGE)

    def test_lookup_none_for_missing_empty_and_unparseable(self):
        self.assertIsNone(C.lookup(SLUG, SHA))
        path = C.receipt_path(SLUG, SHA)
        path.parent.mkdir(parents=True)
        path.touch()
        self.assertIsNone(C.lookup(SLUG, SHA))
        path.write_text("{not json", encoding="utf-8")
        self.assertIsNone(C.lookup(SLUG, SHA))


class ReuseTests(unittest.TestCase):
    """AC2: only a matching l1-post-scratch receipt skips the check."""

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for patch in (mock.patch.object(C, "STORE", Path(self.tmp.name)),
                      mock.patch.object(C, "forge_sha", return_value=FORGE),
                      mock.patch.object(L, "cheap_static_checks")):
            patch.start()
            self.addCleanup(patch.stop)

    def write(self, **fields):
        data = {"repo": SLUG, "sha": SHA, "forge_sha": FORGE, "source": C.L1_POST_SOURCE, **fields}
        path = C.receipt_path(SLUG, SHA)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")

    def static(self):
        """Run static_checks with git faked; returns (result, mise-check called?)."""
        calls: list[tuple] = []

        def fake_run(*argv, cwd=None, env=None):
            calls.append(argv)
            if argv[:3] == ("git", "rev-parse", "--show-toplevel"):
                return _done(self.tmp.name)
            if argv[:4] == ("git", "remote", "get-url", "origin"):
                return _done("https://github.com/vitalharmony/harmonic-forge.git")
            raise RuntimeError("past the receipt: the full check would run")

        def past_receipt(*_a, **_k):
            raise RuntimeError("past the receipt: the full check would run")

        # The scratch worktree is created before any further `run`; reaching
        # it means the receipt was not reused (and it must never take the
        # real scratch lock in a test).
        with mock.patch.object(L, "run", side_effect=fake_run), \
                mock.patch.object(L._scratch, "scratch_dir", side_effect=past_receipt):
            try:
                result = L.static_checks(SHA, "fix/1134-x")
            except RuntimeError:
                return None, True
        return result, any(c[:3] == ("mise", "run", "check") for c in calls)

    def test_a_matching_receipt_skips_mise_check(self):
        self.write()
        result, ran_check = self.static()
        self.assertFalse(ran_check)
        # Only what ran is listed, and the attempt cannot read as a fresh 0 ms pass.
        self.assertEqual(result[0], ["mise-check:receipt", "origin-main-ancestor", "branch-sha-match"])
        self.assertIsNone(result[1])
        self.assertEqual(result[2], {"result": "reused"})

    def test_dry_run_reuse_prints_the_ac3_line(self):
        self.write()
        out = io.StringIO()
        with mock.patch.object(L, "run", side_effect=lambda *a, cwd=None, env=None: _done(
                self.tmp.name if a[:3] == ("git", "rev-parse", "--show-toplevel")
                else "https://github.com/vitalharmony/harmonic-forge.git")), \
                contextlib.redirect_stdout(out):
            L.static_checks(SHA, "fix/1134-x", dry_run=True)
        self.assertIn(f"dry run: check receipt found for {SHA[:12]}; the real post reuses it",
                      out.getvalue())

    def test_a_lane_worktree_receipt_still_runs_the_check(self):
        self.write(source="check")
        self.assertTrue(self.static()[1])

    def test_a_moved_forge_head_runs_the_check(self):
        self.write(forge_sha="e" * 40)
        self.assertTrue(self.static()[1])

    def test_another_sha_or_empty_receipt_runs_the_check(self):
        self.write(sha="b" * 40)
        self.assertTrue(self.static()[1])
        C.receipt_path(SLUG, SHA).write_text("", encoding="utf-8")
        self.assertTrue(self.static()[1])


class KeptLogsTests(unittest.TestCase):
    """AC3: a failed check's step logs survive the worktree's removal."""

    def test_gate_logs_are_copied_out(self):
        with TemporaryDirectory() as scratch, TemporaryDirectory() as keep:
            steps = Path(scratch) / "logs" / "gate-steps"
            steps.mkdir(parents=True)
            (steps / "25.log").write_text("boom", encoding="utf-8")
            with mock.patch.object(L, "FAILED_CHECK_LOGS", Path(keep)):
                kept = L._keep_gate_logs(Path(scratch), SHA)
            self.assertIsNotNone(kept)
            self.assertEqual((Path(kept) / "25.log").read_text(encoding="utf-8"), "boom")

    def test_two_failures_in_one_second_keep_both(self):
        with TemporaryDirectory() as scratch, TemporaryDirectory() as keep:
            steps = Path(scratch) / "logs" / "gate-steps"
            steps.mkdir(parents=True)
            (steps / "25.log").write_text("boom", encoding="utf-8")
            with mock.patch.object(L, "FAILED_CHECK_LOGS", Path(keep)):
                first, second = L._keep_gate_logs(Path(scratch), SHA), L._keep_gate_logs(Path(scratch), SHA)
            self.assertIsNotNone(first)
            self.assertIsNotNone(second)
            self.assertNotEqual(first, second)

    def test_a_failed_check_returns_a_kept_logs_path_that_exists(self):
        """AC3 kill check: static_checks' own fail branch carries the kept path."""
        with TemporaryDirectory() as holder, TemporaryDirectory() as keep:
            holder_path = Path(holder)

            def fake_run(*argv, cwd=None, env=None):
                if argv[:3] == ("git", "rev-parse", "--show-toplevel"):
                    return _done(holder)
                if argv[:3] == ("git", "worktree", "add"):
                    steps = holder_path / "wt" / "logs" / "gate-steps"
                    steps.mkdir(parents=True)
                    (steps / "25.log").write_text("boom", encoding="utf-8")
                    return _done()
                if argv[:3] == ("mise", "run", "check"):
                    return _done("step 25 FAILED", code=1)
                return _done()

            holder_cm = mock.MagicMock()
            holder_cm.__enter__.return_value = holder_path
            with mock.patch.object(L, "run", side_effect=fake_run), \
                    mock.patch.object(L, "cheap_static_checks"), \
                    mock.patch.object(L, "_receipt_matches", return_value=False), \
                    mock.patch.object(L, "resolve_sha", return_value=SHA), \
                    mock.patch.object(L, "_source_repo_is_hrse", return_value=False), \
                    mock.patch.object(L, "_private_check_tmp", return_value=(holder_path, {})), \
                    mock.patch.object(L._scratch, "scratch_dir", return_value=holder_cm), \
                    mock.patch.object(L, "FAILED_CHECK_LOGS", Path(keep)):
                _checks, _timing, check = L.static_checks(SHA, "fix/1134-x")
            self.assertEqual(check["result"], "fail")
            self.assertTrue((Path(check["logs"]) / "25.log").is_file())

    def test_no_logs_dir_returns_none(self):
        with TemporaryDirectory() as scratch:
            self.assertIsNone(L._keep_gate_logs(Path(scratch), SHA))


if __name__ == "__main__":
    unittest.main()
