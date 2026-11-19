"""harmonic-forge#1291 decisions 2-4: state-bound receipts, written only at a
tool's clean point. Synthetic fixtures only: a temp git repo, a temp store."""
from __future__ import annotations

import contextlib
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
FORGE = HERE.parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(FORGE / "tools" / "gh"))
import gate  # noqa: E402
import gate_receipt as R  # noqa: E402

SESSION = "sess-1291"
PRECLOSE = str(FORGE / "tools" / "gh" / "preclose_check.py")


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class Store(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "config", "user.name", "t")
        git(self.repo, "config", "user.email", "t@example.test")
        git(self.repo, "commit", "-q", "--allow-empty", "-m", "a")
        env = mock.patch.dict(os.environ, {"PRECHECK_GATE_DIR": str(self.root / "store"),
                                           "CLAUDE_CODE_SESSION_ID": SESSION, "HF_CALLER_CWD": ""})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("PREFLIGHT_RECEIPT_DIR", None)

    def record(self, tool: str, argv: list[str], **kw):
        return R.record(tool, argv, self.repo, _argv0=str(FORGE / tool), **kw)

    def gate(self, command: str) -> str | None:
        return gate.decide({"tool_name": "Bash", "tool_input": {"command": command},
                            "session_id": SESSION, "cwd": str(self.repo)})

    def receipts(self) -> list[Path]:
        return list((self.root / "store").rglob("*.json")) if (self.root / "store").exists() else []


PRECLOSE_ARGS = ["--repo", "vitalharmony/harmonic-forge", "--issue", "1291", "--complete"]
PRECLOSE_CMD = "python3 tools/gh/preclose_check.py " + " ".join(PRECLOSE_ARGS)


class ReceiptBinding(Store):
    def test_receipt_bound_to_exact_args(self) -> None:
        self.record("tools/gh/preclose_check.py", PRECLOSE_ARGS)
        self.assertIsNone(self.gate(PRECLOSE_CMD + " --force"))
        self.assertIsNotNone(self.gate(PRECLOSE_CMD + " --force --allow-dirty"))
        self.assertIsNotNone(self.gate(PRECLOSE_CMD.replace("1291", "1292") + " --force"))
        self.assertIsNotNone(self.gate(PRECLOSE_CMD.replace("--complete", "--complete --tier deep")
                                       + " --force"))

    def test_receipt_dies_when_head_moves(self) -> None:
        """kill_check's run binds --head (registry bind_args)."""
        checks = self.root / "checks.json"
        checks.write_text("[]")
        argv = ["run", "--repo", "R", "--issue", "1", "--checks", str(checks), "--validate"]
        self.record("tools/gh/kill_check.py", argv)
        real = "python3 tools/gh/kill_check.py " + " ".join(argv[:-1])
        self.assertIsNone(self.gate(real))
        git(self.repo, "commit", "-q", "--allow-empty", "-m", "b")
        self.assertIsNotNone(self.gate(real))

    def test_preclose_receipt_dies_when_head_moves(self) -> None:
        """Finding 6: preclose's force modes bind the resolved --head."""
        self.record("tools/gh/preclose_check.py", PRECLOSE_ARGS)
        self.assertIsNone(self.gate(PRECLOSE_CMD + " --force"))
        git(self.repo, "commit", "-q", "--allow-empty", "-m", "b")  # HEAD moved, within 30 min
        self.assertIn("real run with no live precheck receipt", self.gate(PRECLOSE_CMD + " --force"))

    def test_checks_file_content_is_bound(self) -> None:
        checks = self.root / "checks.json"
        checks.write_text("[]")
        argv = ["run", "--repo", "R", "--issue", "1", "--checks", str(checks), "--validate"]
        self.record("tools/gh/kill_check.py", argv)
        checks.write_text('[{"patch": "x"}]')
        self.assertIsNotNone(self.gate("python3 tools/gh/kill_check.py " + " ".join(argv[:-1])))

    def test_unbound_mode_expires_after_1800s(self) -> None:
        argv = ["--prune-worktrees", "--dry-run"]
        self.record("tools/gh/repo_hygiene.py", argv, now=1000.0)
        tool = R.precheck_tool("tools/gh/repo_hygiene.py")
        mode, real = R.classify(tool, ["--prune-worktrees"])
        self.assertTrue(real)
        fp = R.fingerprint(tool, mode, ["--prune-worktrees"], self.repo)
        self.assertIsNotNone(R.live_receipt(SESSION, fp, now=1000.0 + 1799))
        self.assertIsNone(R.live_receipt(SESSION, fp, now=1000.0 + 1800))

    def test_the_receipt_never_holds_the_command_text(self) -> None:
        self.record("tools/gh/preclose_check.py", PRECLOSE_ARGS)
        (path,) = self.receipts()
        self.assertNotIn("vitalharmony/harmonic-forge", path.read_text())


class ReceiptWriters(Store):
    def test_only_the_tool_itself_records(self) -> None:
        with self.assertRaises(R.ReceiptRefused):
            R.record("tools/gh/preclose_check.py", PRECLOSE_ARGS, self.repo, _argv0="/usr/bin/python3")
        with self.assertRaises(R.ReceiptRefused):
            R.record("tools/gh/preclose_check.py", PRECLOSE_ARGS, self.repo,
                     _argv0=str(FORGE / "tools" / "gh" / "l1_post.py"))
        self.assertEqual(self.receipts(), [])

    def test_a_real_run_is_never_recorded_as_its_precheck(self) -> None:
        with self.assertRaises(R.ReceiptRefused):
            self.record("tools/gh/preclose_check.py", [*PRECLOSE_ARGS, "--force"])

    def test_the_cli_records_only_for_a_non_python_tool(self) -> None:
        self.assertEqual(R.main(["record", "--tool", "tools/gh/preclose_check.py", "--", *PRECLOSE_ARGS]), 2)
        with mock.patch.dict(os.environ, {"HF_CALLER_CWD": str(self.repo)}):
            self.assertEqual(R.main(["record", "--tool", "tools/lane/lanes-claude", "--",
                                     "--session", "x", "--dry-run"]), 0)
        self.assertIsNone(self.gate("lanes-claude --session x"))

    def _preclose_main(self, argv: list[str], complete) -> None:
        import preclose_check  # noqa: PLC0415
        with mock.patch.object(sys, "argv", [PRECLOSE, *argv]), \
                mock.patch.object(preclose_check, "complete", complete), \
                mock.patch.dict(os.environ, {"HF_CALLER_CWD": str(self.repo)}), \
                contextlib.redirect_stderr(io.StringIO()):
            with contextlib.suppress(SystemExit):
                preclose_check.main()

    def test_failed_precheck_writes_no_receipt(self) -> None:
        """Decision 2: preclose records after a clean non-force run, never before."""
        argv = [*PRECLOSE_ARGS, "--own-model", "m"]

        def refused(args):
            raise SystemExit("preclose-check: refused")
        self._preclose_main(argv, refused)
        self.assertEqual(self.receipts(), [])
        self._preclose_main(argv, lambda args: 1)
        self.assertEqual(self.receipts(), [])
        self._preclose_main(argv, lambda args: 0)
        self.assertEqual(len(self.receipts()), 1)
        self.assertIsNone(self.gate(PRECLOSE_CMD + " --own-model m --force"))

    def test_lane3_readiness_run_unlocks_mark_active(self) -> None:
        """Finding 4: a passed check_lane3_ready run without --mark-active is
        the receipt writer for lane3-begin's --mark-active run."""
        import check_lane3_ready as C  # noqa: PLC0415
        sweep = {"html_url": "u", "body": ""}
        patches = [mock.patch.object(sys, "argv", [str(FORGE / "tools/gh/check_lane3_ready.py")]),
                   mock.patch.object(C, "current_repo", return_value="vitalharmony/harmonic-forge"),
                   mock.patch.object(C, "apply_project_identity"),
                   mock.patch.object(C, "fetch_comments", return_value=[]),
                   mock.patch.object(C, "current_head_sha", return_value="a" * 40),
                   mock.patch.object(C, "resolve_gate_authority", return_value=({"html_url": "u", "id": 1}, "ok")),
                   mock.patch.object(C, "latest_by_kind", return_value=sweep),
                   mock.patch.object(C, "parse_write_tier", return_value="R"),
                   mock.patch.object(C, "tier_w_availability", return_value=""),
                   mock.patch.object(C, "clear_marker"), mock.patch.object(C, "mark_active"),
                   mock.patch.dict(os.environ, {"HF_CALLER_CWD": str(self.repo)}),
                   contextlib.redirect_stdout(io.StringIO())]
        self.assertIsNotNone(self.gate("mise run lane3-begin -- --issue 5"))
        with contextlib.ExitStack() as stack:
            for patch in patches:
                stack.enter_context(patch)
            C.main(["--issue", "5"])
        self.assertIsNone(self.gate("mise run lane3-begin -- --issue 5"))

    def test_override_receipt_admits_the_backstop_once(self) -> None:
        tool = R.precheck_tool("tools/gh/preclose_check.py")
        argv = [*PRECLOSE_ARGS, "--force"]
        mode, _ = R.classify(tool, argv)
        fp = R.fingerprint(tool, mode, argv, self.repo)
        R.write_receipt(SESSION, tool, mode, fp, kind="override", ttl=R.OVERRIDE_TTL_S)
        self.assertIsNone(R.consume("tools/gh/preclose_check.py", argv, cwd=self.repo))
        self.assertIsNotNone(R.consume("tools/gh/preclose_check.py", argv, cwd=self.repo))

    def test_tool_only_extra_state_is_bound_by_the_backstop(self) -> None:
        argv = ["--check", str(self.repo), "1", "main"]
        self.record("tools/lane/lane1_rebase.py", argv, extra="old|main")
        self.assertIsNone(R.consume("tools/lane/lane1_rebase.py", argv[1:], cwd=self.repo, extra="old|main"))
        self.assertIsNotNone(R.consume("tools/lane/lane1_rebase.py", argv[1:], cwd=self.repo, extra="new|main"))


if __name__ == "__main__":
    unittest.main()
