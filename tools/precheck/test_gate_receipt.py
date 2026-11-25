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
        (self.repo / "b.md").write_text("body\n")
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


#: The sample gated tool: l1_post (precheck `--dry-run`, binds --sha and --file).
L1 = "tools/gh/l1_post.py"
L1_REAL = ["--repo", "vitalharmony/harmonic-forge", "--issue", "1291", "--kind", "ae", "--sha", "HEAD",
           "--file", "b.md"]
L1_ARGS = [*L1_REAL, "--dry-run"]
L1_CMD = "python3 tools/gh/l1_post.py " + " ".join(L1_REAL)


class ReceiptBinding(Store):
    def test_receipt_bound_to_exact_args(self) -> None:
        self.record(L1, L1_ARGS)
        self.assertIsNone(self.gate(L1_CMD))
        self.assertIsNotNone(self.gate(L1_CMD + " --branch x"))
        self.assertIsNotNone(self.gate(L1_CMD.replace("1291", "1292")))
        self.assertIsNotNone(self.gate(L1_CMD.replace("--kind ae", "--kind handoff")))

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

    def test_l1_post_receipt_dies_when_head_or_body_moves(self) -> None:
        self.record(L1, L1_ARGS)
        self.assertIsNone(self.gate(L1_CMD))
        (self.repo / "b.md").write_text("edited\n")
        self.assertIn("real run with no live precheck receipt", self.gate(L1_CMD))
        (self.repo / "b.md").write_text("body\n")
        self.assertIsNone(self.gate(L1_CMD))
        git(self.repo, "commit", "-q", "--allow-empty", "-m", "b")  # HEAD moved, within 30 min
        self.assertIn("real run with no live precheck receipt", self.gate(L1_CMD))

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
        self.record(L1, L1_ARGS)
        (path,) = self.receipts()
        self.assertNotIn("vitalharmony/harmonic-forge", path.read_text())


class ReceiptWriters(Store):
    def test_only_the_tool_itself_records(self) -> None:
        with self.assertRaises(R.ReceiptRefused):
            R.record(L1, L1_ARGS, self.repo, _argv0="/usr/bin/python3")
        with self.assertRaises(R.ReceiptRefused):
            R.record(L1, L1_ARGS, self.repo, _argv0=str(FORGE / "tools" / "gh" / "kill_check.py"))
        self.assertEqual(self.receipts(), [])

    def test_a_real_run_is_never_recorded_as_its_precheck(self) -> None:
        with self.assertRaises(R.ReceiptRefused):
            self.record(L1, L1_REAL)

    def test_the_cli_records_only_for_a_non_python_tool(self) -> None:
        self.assertEqual(R.main(["record", "--tool", L1, "--", *L1_ARGS]), 2)
        with mock.patch.dict(os.environ, {"HF_CALLER_CWD": str(self.repo)}):
            self.assertEqual(R.main(["record", "--tool", "tools/lane/lanes-claude", "--",
                                     "--session", "x", "--dry-run"]), 0)
        self.assertIsNone(self.gate("lanes-claude --session x"))

    def test_the_second_lane1_flags_are_a_precheck_form(self) -> None:
        args = ["--lane1-name", "CymaGraph", "--second-lane1-dir", "/x/harmonic-forge",
                "--second-lane1-name", "Harmonic Forge"]
        self.assertEqual(R._lanes_claude([*args, "--dry-run"])[0], "dry-run")
        self.assertEqual(R._lanes_claude(args)[0], "launch")
        self.assertEqual(R._lanes_claude(["--session", "x", "--stop-second-lane1"])[0], "stop")
        with mock.patch.dict(os.environ, {"HF_CALLER_CWD": str(self.repo)}):
            self.assertEqual(R.main(["record", "--tool", "tools/lane/lanes-claude", "--",
                                     *args, "--dry-run"]), 0)

    def _preclose_main(self, argv: list[str], complete) -> None:
        import preclose_check  # noqa: PLC0415
        with mock.patch.object(sys, "argv", [PRECLOSE, *argv]), \
                mock.patch.object(preclose_check, "complete", complete), \
                mock.patch.dict(os.environ, {"HF_CALLER_CWD": str(self.repo)}), \
                contextlib.redirect_stderr(io.StringIO()):
            with contextlib.suppress(SystemExit):
                preclose_check.main()

    def test_preclose_force_modes_are_grant_guarded_only(self) -> None:
        """Operator ruling 2026-11-19 (finding 8): preclose's FORCE modes are not
        precheck-gated; a non-force run writes no receipt, and the gate never
        refuses a forced run (force_grant.require is its guard)."""
        argv = ["--repo", "vitalharmony/harmonic-forge", "--issue", "1291", "--complete", "--own-model", "m"]
        self._preclose_main(argv, lambda args: 0)
        self.assertEqual(self.receipts(), [])
        cmd = "python3 tools/gh/preclose_check.py " + " ".join(argv)
        for force in (" --force", " --force --reforge", " --cluster-verdict PATCH --force"):
            self.assertIsNone(self.gate(cmd + force), force)

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

    def test_tool_only_extra_state_is_bound_by_the_backstop(self) -> None:
        argv = ["--check", str(self.repo), "1", "main"]
        self.record("tools/lane/lane1_rebase.py", argv, extra="old|main")
        self.assertIsNone(R.consume("tools/lane/lane1_rebase.py", argv[1:], cwd=self.repo, extra="old|main"))
        self.assertIsNotNone(R.consume("tools/lane/lane1_rebase.py", argv[1:], cwd=self.repo, extra="new|main"))


class RealStoreBackstopGuard(Store):
    """harmonic-forge#1555 AC2: a test process never appends a refused-backstop
    row to the real decision log; the refusal itself is unchanged. The real
    store is patched to this test's temporary store, never the account's. The
    refusal is the extra-moved backstop that
    `test_tool_only_extra_state_is_bound_by_the_backstop` already exercises."""

    TOOL = "tools/lane/lane1_rebase.py"

    def refuse(self, real: Path) -> str | None:
        argv = ["--check", str(self.repo), "1", "main"]
        self.record(self.TOOL, argv, extra="old|main")
        with mock.patch.object(R, "REAL_STORE_DIR", real):
            return R.consume(self.TOOL, argv[1:], cwd=self.repo, extra="new|main")

    def backstops(self) -> list[str]:
        log = self.root / "store" / "decisions.jsonl"
        rows = log.read_text().splitlines() if log.exists() else []
        return [r for r in rows if "refused-backstop" in r]

    def test_tc5_no_backstop_row_at_the_real_store_in_a_test(self) -> None:
        self.assertIsNotNone(self.refuse(self.root / "store"))
        self.assertEqual(self.backstops(), [])

    def test_tc5_a_store_that_is_not_the_real_one_still_logs(self) -> None:
        self.assertIsNotNone(self.refuse(self.root / "elsewhere"))
        self.assertEqual(len(self.backstops()), 1)

    def test_tc6_the_refusal_text_is_the_same_with_the_guard_on(self) -> None:
        unguarded = self.refuse(self.root / "elsewhere")
        guarded = self.refuse(self.root / "store")
        self.assertIsNotNone(guarded)
        self.assertEqual(guarded, unguarded)


if __name__ == "__main__":
    unittest.main()
