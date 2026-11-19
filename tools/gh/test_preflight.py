"""harmonic-forge#1076, #1291: guarded lane tools refuse a real run until its
precheck form has run, in this session, with the same arguments, at the same
state. `_preflight` now delegates to `tools/precheck/gate_receipt.py`, the
receipt the PreToolUse gate reads too; `require()` is the tool-side backstop."""
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
from test_l1_post_dry_run import Harness  # noqa: E402

PRECLOSE = HERE / "preclose_check.py"
#: The module-level sample: kill_check's real run. preclose_check's FORCE modes
#: are grant-guarded only (harmonic-forge#1291 operator ruling 2026-11-19).
KILL = HERE / "kill_check.py"
TOOL = "tools/gh/kill_check.py"
REBASE = HERE.parent / "lane" / "lane1_rebase.py"
NEEDLE = "no live precheck receipt for this exact call"


def real(checks: str, issue: str = "1") -> list[str]:
    return ["run", "--repo", "vitalharmony/harmonic-forge", "--issue", issue, "--checks", checks]


def precheck(checks: str, issue: str = "1") -> list[str]:
    return [*real(checks, issue), "--validate"]


class PreflightTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = {**os.environ, "PRECHECK_GATE_DIR": self.tmp.name,
                    "CLAUDE_CODE_SESSION_ID": "session-a"}
        patcher = mock.patch.dict(os.environ, self.env)
        patcher.start()
        self.addCleanup(patcher.stop)
        argv0 = mock.patch.object(sys, "argv", [str(KILL)])
        argv0.start()
        self.addCleanup(argv0.stop)
        work = tempfile.TemporaryDirectory()  # outside the store, which tests count *.json in
        self.addCleanup(work.cleanup)
        self.checks = str(Path(work.name) / "checks.json")
        Path(self.checks).write_text("[]")
        self.REAL, self.BASE = real(self.checks), precheck(self.checks)

    def run_tool(self, *argv, session="session-a"):
        env = {**self.env, "CLAUDE_CODE_SESSION_ID": session}
        return subprocess.run([sys.executable, *map(str, argv)], capture_output=True,
                              text=True, env=env, timeout=60)

    # --- the module -------------------------------------------------------

    def test_refuse_then_allow(self) -> None:
        self.assertIn(NEEDLE, _preflight.require(TOOL, self.REAL, "cmd --validate"))
        self.assertIn("cmd --validate", _preflight.require(TOOL, self.REAL, "cmd --validate"))
        _preflight.record(TOOL, self.BASE)
        self.assertIsNone(_preflight.require(TOOL, self.REAL))

    def test_the_precheck_form_itself_is_never_refused(self) -> None:
        self.assertIsNone(_preflight.require(TOOL, self.BASE))

    def test_another_sessions_receipt_does_not_count(self) -> None:
        _preflight.record(TOOL, self.BASE)
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "session-b"}):
            self.assertIsNotNone(_preflight.require(TOOL, self.REAL))

    def test_empty_session_is_the_no_session_bucket(self) -> None:
        """harmonic-forge#1291 operator ruling 2026-11-19: a blank session is
        never a skip; it has its own receipts, which no named session shares."""
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": ""}):
            self.assertIn(NEEDLE, _preflight.require(TOOL, self.REAL))
            _preflight.record(TOOL, self.BASE)
            self.assertIsNone(_preflight.require(TOOL, self.REAL))
        self.assertIn(NEEDLE, _preflight.require(TOOL, self.REAL))

    def test_concurrent_records_keep_both_receipts(self) -> None:
        issues = [str(i) for i in range(1, 9)]
        threads = [threading.Thread(target=_preflight.record, args=(TOOL, precheck(self.checks, i)))
                   for i in issues]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        for i in issues:
            self.assertIsNone(_preflight.require(TOOL, real(self.checks, i)), i)
        self.assertEqual(list(Path(self.tmp.name).rglob(".tmp-*")), [])

    def test_only_the_tool_itself_records(self) -> None:
        with mock.patch.object(sys, "argv", ["/usr/bin/pytest"]), mock.patch("sys.stderr") as err:
            _preflight.record(TOOL, self.BASE)
        self.assertTrue(any("receipt not written" in str(c) for c in err.write.call_args_list))
        self.assertIsNotNone(_preflight.require(TOOL, self.REAL))

    def test_record_never_raises_when_the_store_write_fails(self) -> None:
        """Sticky PATCH: os.replace and the cleanup unlink both fail; no raise."""
        with mock.patch("gate_receipt.os.replace", side_effect=PermissionError("store")), \
             mock.patch("gate_receipt.Path.unlink", side_effect=PermissionError("cleanup")), \
             mock.patch("sys.stderr") as err:
            _preflight.record(TOOL, self.BASE)  # must not raise
        self.assertTrue(any("receipt not written" in str(c) for c in err.write.call_args_list))
        self.assertIsNotNone(_preflight.require(TOOL, self.REAL))

    def test_refusal_names_the_store(self) -> None:
        """Sticky PATCH: the guarded refusal names the store, so it does not loop."""
        self.assertIn(self.tmp.name, _preflight.require(TOOL, self.REAL))

    # --- the tools, end to end --------------------------------------------

    def test_lane1_rebase_help_no_longer_unlocks_a_run(self) -> None:
        """harmonic-forge#1215 AC1: the preflight is `--check`, never `--help`."""
        argv = (REBASE, "/nonexistent", "1", "feat/x")
        first = self.run_tool(*argv)
        self.assertEqual(first.returncode, 1)
        self.assertIn(NEEDLE, first.stderr)
        self.assertIn("--check /nonexistent 1 feat/x", first.stderr)
        self.assertEqual(self.run_tool(REBASE, "--help").returncode, 0)
        self.assertIn(NEEDLE, self.run_tool(*argv).stderr)

    def _preclose(self, *extra, issue="1", session="session-a"):
        return self.run_tool(PRECLOSE, "--repo", "vitalharmony/harmonic-forge",
                             "--issue", issue, *extra, session=session)

    def test_preclose_force_is_not_precheck_gated(self) -> None:
        """harmonic-forge#1291 operator ruling 2026-11-19 (finding 8): a forced
        preclose run is guarded by its FORCE grant only, never by a precheck."""
        for force in ("--force", "--forc"):
            forced = self._preclose("--complete", force, "--findings", "/nonexistent", "--own-model", "m")
            self.assertNotIn(NEEDLE, forced.stderr, force)
        self.assertEqual(list(Path(self.tmp.name).rglob("*.json")), [])

    def test_rejected_no_force_run_writes_no_receipt(self) -> None:
        """Preclose pass 1: a run main() rejects (no --own-model) is no preflight."""
        rejected = self._preclose("--complete", "--findings", "/nonexistent")
        self.assertEqual(rejected.returncode, 2)
        self.assertEqual(list(Path(self.tmp.name).rglob("*.json")), [])

    def test_store_error_does_not_break_the_ungated_run(self) -> None:
        blocker = Path(self.tmp.name) / "file-not-dir"
        blocker.write_text("x")
        env = {**self.env, "PRECHECK_GATE_DIR": str(blocker)}
        done = subprocess.run([sys.executable, str(REBASE), "--help"], capture_output=True,
                              text=True, env=env, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertNotIn("Traceback", done.stderr)

    def test_preclose_without_force_is_never_refused(self) -> None:
        self.assertNotIn(NEEDLE, self._preclose("--complete", "--findings", "/x", "--own-model", "m").stderr)


class L1PostDryRunPreflightTests(Harness):
    """harmonic-forge#1115: l1_post's preflight form is a clean --dry-run of
    the same post; --help no longer admits a real post."""

    def test_l1_post_refuses_then_allows_after_dry_run_not_help(self) -> None:
        """AC4."""
        first = self.run_main(*self.r4l3())[0]
        self.assertIn(NEEDLE, first)
        self.assertIn("--dry-run", first)
        self.assertEqual(self.run_main("--help")[0], "0")
        self.assertIn(NEEDLE, self.run_main(*self.r4l3())[0])
        self.assertEqual(self.receipts(), [])
        self.assertIsNone(self.run_main(*self.r4l3("--dry-run"))[0])
        self.assertIsNone(self.run_main(*self.r4l3())[0])

    def test_l1_post_store_error_does_not_break_the_dry_run(self) -> None:
        blocker = self.dir / "file-not-dir"
        blocker.write_text("x")
        with mock.patch.dict(os.environ, {"PREFLIGHT_RECEIPT_DIR": str(blocker)}):
            code, _, err = self.run_main(*self.r4l3("--dry-run"))
        self.assertIsNone(code)
        self.assertIn("receipt not written", err)

    def test_l1_post_refused_dry_run_writes_no_receipt(self) -> None:
        self.overlap = True
        self.assertIn("overlaps", self.run_main(*self.r4l3("--dry-run"))[0])
        self.assertEqual(self.receipts(), [])
        self.overlap = False
        self.assertIn(NEEDLE, self.run_main(*self.r4l3())[0])


if __name__ == "__main__":
    unittest.main()
