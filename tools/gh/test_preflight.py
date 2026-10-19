"""harmonic-forge#1076: guarded lane tools refuse their first real run in a
session until their preflight form has run there."""
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import _preflight  # noqa: E402

L1_POST = HERE / "l1_post.py"
PRECLOSE = HERE / "preclose_check.py"
REBASE = HERE.parent / "lane" / "lane1_rebase.py"
NEEDLE = "first real run of this tool in this session"


class PreflightTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = {**os.environ, "PREFLIGHT_RECEIPT_DIR": self.tmp.name,
                    "CLAUDE_CODE_SESSION_ID": "session-a"}
        patcher = mock.patch.dict(os.environ, self.env)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_tool(self, *argv, session="session-a"):
        env = {**self.env, "CLAUDE_CODE_SESSION_ID": session}
        return subprocess.run([sys.executable, *map(str, argv)], capture_output=True,
                              text=True, env=env, timeout=60)

    # --- the module -------------------------------------------------------

    def test_refuse_then_allow(self) -> None:
        self.assertIn(NEEDLE, _preflight.require("k", "cmd --help"))
        self.assertIn("cmd --help", _preflight.require("k", "cmd --help"))
        _preflight.record("k")
        self.assertIsNone(_preflight.require("k", "cmd --help"))

    def test_another_sessions_receipt_does_not_count(self) -> None:
        _preflight.record("k")
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "session-b"}):
            self.assertIsNotNone(_preflight.require("k", "cmd"))

    def test_empty_session_is_not_enforced(self) -> None:
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": ""}):
            self.assertIsNone(_preflight.require("k", "cmd"))
            _preflight.record("k")  # writes nothing, raises nothing
        self.assertEqual(list(Path(self.tmp.name).rglob("*.json")), [])

    def test_concurrent_records_keep_both_receipts(self) -> None:
        keys = [f"k{i}" for i in range(16)]
        threads = [threading.Thread(target=_preflight.record, args=(k,)) for k in keys]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        for k in keys:
            self.assertIsNone(_preflight.require(k, "cmd"), k)
        self.assertEqual(list(Path(self.tmp.name).rglob(".tmp-*")), [])

    # --- the three tools --------------------------------------------------

    def test_l1_post_refuses_then_allows_after_help(self) -> None:
        argv = (L1_POST, "--repo", "vitalharmony/harmonic-forge", "--issue", "1",
                "--kind", "handoff", "--sha", "0" * 40, "--branch", "main")
        first = self.run_tool(*argv)
        self.assertNotEqual(first.returncode, 0)
        self.assertIn(NEEDLE, first.stderr)
        self.assertIn("l1_post.py --help", first.stderr)
        self.assertEqual(self.run_tool(L1_POST, "--help").returncode, 0)
        self.assertNotIn(NEEDLE, self.run_tool(*argv).stderr)

    def test_lane1_rebase_refuses_then_allows_after_help(self) -> None:
        argv = (REBASE, "/nonexistent", "1", "feat/x")
        first = self.run_tool(*argv)
        self.assertEqual(first.returncode, 1)
        self.assertIn(NEEDLE, first.stderr)
        self.assertEqual(self.run_tool(REBASE, "--help").returncode, 0)
        self.assertNotIn(NEEDLE, self.run_tool(*argv).stderr)

    def _preclose(self, *extra, issue="1", session="session-a"):
        return self.run_tool(PRECLOSE, "--repo", "vitalharmony/harmonic-forge",
                             "--issue", issue, *extra, session=session)

    def test_preclose_force_needs_the_same_run_without_force(self) -> None:
        forced = self._preclose("--complete", "--force", "--findings", "/nonexistent", "--own-model", "m")
        self.assertNotEqual(forced.returncode, 0)
        self.assertIn(NEEDLE, forced.stderr)
        self.assertNotIn("--force", forced.stderr.split(NEEDLE)[1].splitlines()[1])
        self._preclose("--complete", "--findings", "/nonexistent", "--own-model", "m")  # the preflight form
        self.assertNotIn(NEEDLE, self._preclose("--complete", "--force",
                                                "--findings", "/nonexistent", "--own-model", "m").stderr)

    def test_preclose_receipt_matches_repo_issue_and_mode_only(self) -> None:
        self._preclose("--complete", "--findings", "/nonexistent", "--own-model", "m")
        other_issue = self._preclose("--complete", "--force", "--findings", "/nonexistent", "--own-model", "m",
                                     issue="2")
        self.assertIn(NEEDLE, other_issue.stderr)
        other_mode = self._preclose("--force", "--reforge")
        self.assertIn(NEEDLE, other_mode.stderr)
        other_session = self._preclose("--complete", "--force", "--findings", "/nonexistent", "--own-model", "m",
                                       session="session-b")
        self.assertIn(NEEDLE, other_session.stderr)

    def test_abbreviated_force_suggests_an_unforced_command(self) -> None:
        """Preclose pass 1: argparse accepts `--forc`; the suggestion must not."""
        forced = self._preclose("--complete", "--forc", "--findings", "/nonexistent",
                                "--own-model", "m")
        self.assertIn(NEEDLE, forced.stderr)
        suggestion = forced.stderr.split(NEEDLE)[1].splitlines()[1]
        self.assertNotIn("--fo", suggestion)
        argv = suggestion.split()[2:]  # drop `python3 <path>`
        self._preclose(*argv[4:])  # run the suggestion (repo/issue are re-added)
        self.assertNotIn(NEEDLE, self._preclose("--complete", "--forc", "--findings",
                                                "/nonexistent", "--own-model", "m").stderr)

    def test_rejected_no_force_run_writes_no_receipt(self) -> None:
        """Preclose pass 1: a run main() rejects (no --own-model) is no preflight."""
        rejected = self._preclose("--complete", "--findings", "/nonexistent")
        self.assertEqual(rejected.returncode, 2)
        self.assertIn(NEEDLE, self._preclose("--complete", "--force", "--findings",
                                             "/nonexistent", "--own-model", "m").stderr)

    def test_store_error_does_not_break_the_ungated_run(self) -> None:
        """Preclose pass 1: an unwritable store warns; --help still succeeds."""
        blocker = Path(self.tmp.name) / "file-not-dir"
        blocker.write_text("x")
        env = {**self.env, "PREFLIGHT_RECEIPT_DIR": str(blocker)}
        for tool in (L1_POST, REBASE):
            done = subprocess.run([sys.executable, str(tool), "--help"], capture_output=True,
                                  text=True, env=env, timeout=60)
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertIn("receipt not written", done.stderr)
            self.assertNotIn("Traceback", done.stderr)

    def test_l1_post_help_that_fails_to_parse_writes_no_receipt(self) -> None:
        """Sticky PATCH: `--repo --help` exits 2 without help; no receipt."""
        bad = self.run_tool(L1_POST, "--repo", "--help")
        self.assertEqual(bad.returncode, 2)
        argv = (L1_POST, "--repo", "vitalharmony/harmonic-forge", "--issue", "1",
                "--kind", "handoff", "--sha", "0" * 40, "--branch", "main")
        self.assertIn(NEEDLE, self.run_tool(*argv).stderr)

    def test_record_never_raises_when_replace_and_cleanup_fail(self) -> None:
        """Sticky PATCH: os.replace and the cleanup unlink both fail; no raise."""
        with mock.patch("_preflight.os.replace", side_effect=PermissionError("store")), \
             mock.patch("_preflight.Path.unlink", side_effect=PermissionError("cleanup")), \
             mock.patch("sys.stderr") as err:
            _preflight.record("k")  # must not raise
        self.assertTrue(any("receipt not written" in str(c) for c in err.write.call_args_list))
        self.assertIsNotNone(_preflight.require("k", "cmd"))

    def test_refusal_names_the_store(self) -> None:
        """Sticky PATCH: the guarded refusal names the store, so it does not loop."""
        self.assertIn(self.tmp.name, _preflight.require("k", "cmd --help"))

    def test_preclose_without_force_is_never_refused(self) -> None:
        self.assertNotIn(NEEDLE, self._preclose("--complete", "--findings", "/x", "--own-model", "m").stderr)


if __name__ == "__main__":
    unittest.main()
