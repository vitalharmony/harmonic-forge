"""harmonic-forge#1291: the PreToolUse precheck gate. Synthetic fixtures only:
a temp git repo, a temp store, a temp FORCE grant store."""
from __future__ import annotations

import contextlib
import io
import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
FORGE = HERE.parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(FORGE / "tools" / "gh"))
sys.path.insert(0, str(FORGE / "tools" / "lane"))
import force_grant  # noqa: E402
import gate  # noqa: E402
import gate_receipt as R  # noqa: E402

SESSION = "sess-1291"
FORCE_CMD = ("python3 tools/gh/preclose_check.py --repo vitalharmony/harmonic-forge --issue 1291 "
             "--complete --force")
PRECHECK = "python3 tools/gh/preclose_check.py --repo vitalharmony/harmonic-forge --issue 1291 --complete"
UNBACKSTOPPED = {"repo_hygiene": "python3 tools/gh/repo_hygiene.py --prune-worktrees",
                 "lanes-claude": "lanes-claude --session x",
                 "check_lane3_ready": "python3 tools/gh/check_lane3_ready.py --mark-active --issue 5"}


class GateCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        for args in (("init", "-q", "-b", "main"), ("config", "user.name", "t"),
                     ("config", "user.email", "t@example.test"), ("commit", "-q", "--allow-empty", "-m", "a")):
            subprocess.run(["git", *args], cwd=self.repo, check=True, capture_output=True)
        env = mock.patch.dict(os.environ, {"PRECHECK_GATE_DIR": str(self.root / "store"),
                                           "CLAUDE_CODE_SESSION_ID": SESSION, "HF_CALLER_CWD": ""})
        env.start()
        self.addCleanup(env.stop)
        grants = mock.patch.object(force_grant, "FORCE_STATE_DIR", self.root / "force")
        grants.start()
        self.addCleanup(grants.stop)

    def decide(self, command: str, session: str = SESSION) -> str | None:
        return gate.decide({"tool_name": "Bash", "tool_input": {"command": command},
                            "session_id": session, "cwd": str(self.repo)})

    def record(self, tool: str, argv: list[str]) -> None:
        R.record(tool, argv, self.repo, _argv0=str(FORGE / tool))

    def rows(self) -> list[dict]:
        path = self.root / "store" / "decisions.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


class Decision1(GateCase):
    def test_unreceipted_force_is_refused(self) -> None:
        self.assertIsNotNone(self.decide(FORCE_CMD))

    def test_receipt_allows(self) -> None:
        self.record("tools/gh/preclose_check.py", shlex.split(PRECHECK)[2:])
        self.assertIsNone(self.decide(FORCE_CMD))

    def test_the_precheck_form_is_allowed(self) -> None:
        self.assertIsNone(self.decide(PRECHECK))

    def test_a_sessionless_call_is_ungated(self) -> None:
        self.assertIsNone(self.decide(FORCE_CMD, session=""))
        self.assertEqual(self.rows()[-1]["outcome"], "ungated")

    def test_abbreviated_force_still_gated(self) -> None:
        self.assertIsNotNone(self.decide(FORCE_CMD.replace("--force", "--forc")))
        self.assertIsNotNone(self.decide(FORCE_CMD.replace("--force", "--fo")))

    def test_help_is_not_gated(self) -> None:
        self.assertIsNone(self.decide(FORCE_CMD + " --help"))

    def test_chained_and_nested_calls_are_found(self) -> None:
        self.assertIsNotNone(self.decide(f"cd {self.repo} && {FORCE_CMD}"))
        self.assertIsNotNone(self.decide(f"bash -c {shlex.quote(FORCE_CMD)}"))
        self.assertIsNotNone(self.decide(f"timeout 60 {FORCE_CMD}"))
        self.assertIsNotNone(self.decide(FORCE_CMD.replace("tools/gh/", str(FORGE / "tools/gh") + "/")))


class Decision3RefusalText(GateCase):
    def test_refusal_prints_exact_precheck(self) -> None:
        reason = self.decide(FORCE_CMD)
        self.assertIn(f"  {PRECHECK}\n", reason)
        self.assertIn("Argument fingerprint: ", reason)
        self.assertIn(gate.PRECEDENT, reason)

    def test_refusal_prints_force_line_with_id(self) -> None:
        reason = self.decide(FORCE_CMD)
        rid = reason.split("Refusal id: ")[1][:12]
        self.assertIn(f"FORCE F1291 gate {rid}: <reason>", reason)
        record = gate.read_refusal(rid)
        self.assertEqual((record["key"], record["mode"], record["session"]), ("F1291", "--complete --force", SESSION))

    def test_a_call_with_no_issue_prints_the_key_placeholder(self) -> None:
        self.assertIn("FORCE <KEY> gate ", self.decide("python3 tools/gh/repo_hygiene.py --prune-worktrees"))

    def test_refusal_records_are_never_overwritten(self) -> None:
        reason = self.decide(FORCE_CMD)
        rid = reason.split("Refusal id: ")[1][:12]
        before = (self.root / "store" / "refusals" / f"{rid}.json").read_text()
        self.decide(FORCE_CMD)
        self.assertEqual((self.root / "store" / "refusals" / f"{rid}.json").read_text(), before)

    def test_mise_wrapper_gated(self) -> None:
        reason = self.decide("mise run preclose-check -- --repo vitalharmony/harmonic-forge --issue 1291 "
                             "--complete --force")
        self.assertIn("mise run preclose-check -- --repo vitalharmony/harmonic-forge --issue 1291 --complete\n",
                      reason)
        lane3 = self.decide("mise run lane3-begin -- --issue 5")
        self.assertIn("check_lane3_ready.py --issue 5\n", lane3)
        self.assertIsNone(self.decide("mise run lane3-end"))

    def test_mise_and_direct_share_one_receipt(self) -> None:
        self.record("tools/gh/preclose_check.py", shlex.split(PRECHECK)[2:])
        self.assertIsNone(self.decide("mise run preclose-check -- --repo vitalharmony/harmonic-forge "
                                      "--issue 1291 --complete --force"))

    def test_ambiguous_or_foreign_prefix_not_precheck(self) -> None:
        # `--c` is a prefix of several of preclose_check's options: never the strip token.
        tool = R.precheck_tool("tools/gh/preclose_check.py")
        self.assertIsNone(R.resolve_option("--c", R.tool_options()["tools/gh/preclose_check.py"], True))
        self.assertEqual(R.canonical(tool, R.classify(tool, ["--complete", "--c"])[0], ["--complete", "--c"]),
                         ["--complete", "--c"])
        # `--dry` resolves to l1_post's --dry-run, so it is the precheck there.
        self.assertIsNone(self.decide("python3 tools/gh/l1_post.py --issue 1 --kind ae --sha HEAD "
                                      "--branch b --file f --dry"))
        # repo_hygiene has no `--validate`: kill_check's add token is foreign there,
        # and `--dr` is ambiguous with nothing, so --prune-worktrees stays a real run.
        self.assertIsNotNone(self.decide("python3 tools/gh/repo_hygiene.py --prune-worktrees --validate"))
        self.assertIsNone(self.decide("python3 tools/gh/repo_hygiene.py --prune-worktrees --dr"))

    def test_lanes_claude_dry_run_in_value_position_is_launch(self) -> None:
        self.assertIsNotNone(self.decide("lanes-claude --session --dry-run"))
        self.assertIsNotNone(self.decide("lanes-claude --project --dry-run"))
        self.assertIsNone(self.decide("lanes-claude --session x --dry-run"))
        self.assertIsNotNone(self.decide("lanes-claude --dry-run --session"))
        self.assertIsNone(self.decide("lanes-claude --stop-lanes"))
        self.assertIsNotNone(self.decide("lanes-claude --unknown --dry-run"))

    def test_lane1_rebase_precheck_is_flag_first(self) -> None:
        import lane1_rebase  # noqa: PLC0415
        reason = self.decide(f"python3 tools/lane/lane1_rebase.py {self.repo} 1291 main")
        printed = shlex.split(reason.split("\n")[2].strip())
        self.assertEqual(printed[2], "--check")
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err), \
                mock.patch.object(sys, "argv", [str(FORGE / "tools/lane/lane1_rebase.py")]):
            lane1_rebase.main(printed[2:])
        self.assertNotIn("usage:", err.getvalue())  # parsed: never the usage refusal
        via_mise = self.decide(f"mise run lane1-rebase {self.repo} 1291 main")
        self.assertIn(f"lane1_rebase.py --check {self.repo} 1291 main\n", via_mise)


class Decision5Override(GateCase):
    def _grant(self, rid: str) -> None:
        refusal = gate.read_refusal(rid)
        grant = force_grant.write_pending("F1291", "vitalharmony/harmonic-forge", 1291, None, "r", SESSION,
                                          "FORCE line", mode="gate",
                                          gate={"gate_tool": refusal["tool"], "gate_mode": refusal["mode"],
                                                "arg_fp": refusal["fp"], "gate_refusal_id": rid})
        force_grant.imc_waiver.write_json_atomic(force_grant.grant_path("F1291"),
                                                 {**grant, "status": "usable"})

    def test_a_gate_grant_releases_exactly_one_call(self) -> None:
        rid = self.decide(FORCE_CMD).split("Refusal id: ")[1][:12]
        self._grant(rid)
        self.assertIsNone(self.decide(FORCE_CMD))
        self.assertEqual(self.rows()[-1]["outcome"], "allowed-override")
        # The tool-side backstop admits that one call too, once.
        argv = shlex.split(FORCE_CMD)[2:]
        self.assertIsNone(R.consume("tools/gh/preclose_check.py", argv, cwd=self.repo))
        self.assertIsNotNone(R.consume("tools/gh/preclose_check.py", argv, cwd=self.repo))
        self.assertIsNotNone(self.decide(FORCE_CMD))

    def test_a_gate_grant_never_releases_another_call(self) -> None:
        rid = self.decide(FORCE_CMD).split("Refusal id: ")[1][:12]
        self._grant(rid)
        self.assertIsNotNone(self.decide(FORCE_CMD + " --allow-dirty"))


class Decision6Log(GateCase):
    def test_every_outcome_logged(self) -> None:
        self.decide(FORCE_CMD)
        self.decide(PRECHECK)
        self.record("tools/gh/preclose_check.py", shlex.split(PRECHECK)[2:])
        self.decide(FORCE_CMD)
        self.decide(FORCE_CMD, session="")
        self.decide("git push origin feat/x")
        outcomes = [row["outcome"] for row in self.rows()]
        self.assertEqual(outcomes, ["refused", "precheck", "allowed-receipt", "ungated", "guarded-elsewhere"])
        for row in self.rows():
            self.assertEqual(set(row), {"ts", "outcome", "tool", "mode", "class", "fp", "session"})

    def test_log_never_holds_argv(self) -> None:
        self.decide(FORCE_CMD + " --own-model secret-model-name")
        self.decide("mise run lane3-begin -- --issue 424242")
        text = (self.root / "store" / "decisions.jsonl").read_text()
        # The mode column is the registry's mode name, never the call's tokens.
        for token in ("vitalharmony", "secret-model-name", "424242", "--own-model", "--issue", SESSION):
            self.assertNotIn(token, text)

    def test_outside_families_are_never_refused(self) -> None:
        self.assertIsNone(self.decide("gh pr merge 5 --squash"))
        self.assertIsNone(self.decide("git push origin main"))


class Decision7FailClosed(GateCase):
    def test_unbackstopped_mode_fails_closed_on_error(self) -> None:
        for name, command in UNBACKSTOPPED.items():
            with self.subTest(tool=name, failure="raises"), \
                    mock.patch.object(R, "fingerprint", side_effect=RuntimeError("boom")):
                self.assertIn("fail closed", self.decide(command) or "")
            with self.subTest(tool=name, failure="deadline"), \
                    mock.patch.object(R, "DEADLINE_S", 0.0):
                self.assertIn("fail closed", self.decide(command) or "")

    def test_deadline_bounds_a_slow_resolver(self) -> None:
        """A slow `git rev-parse` hits the internal deadline, never the hook timeout."""
        def slow(*args, **kwargs):
            time.sleep(0.05)
            raise subprocess.TimeoutExpired(args[0], kwargs.get("timeout"))
        started = time.monotonic()
        with mock.patch.object(R.subprocess, "run", side_effect=slow):
            self.assertIsNone(self.decide(FORCE_CMD))  # backstopped: open, consume still refuses
        self.assertLess(time.monotonic() - started, R.DEADLINE_S + 1)
        self.assertEqual(self.rows()[-1]["outcome"], "error")

    def test_backstopped_mode_fails_open_on_error(self) -> None:
        with mock.patch.object(R, "fingerprint", side_effect=RuntimeError("boom")):
            self.assertIsNone(self.decide(FORCE_CMD))
        self.assertEqual(self.rows()[-1]["outcome"], "error")
        argv = shlex.split(FORCE_CMD)[2:]
        self.assertIsNotNone(R.consume("tools/gh/preclose_check.py", argv, cwd=self.repo))

    def test_an_unparseable_command_naming_an_unbackstopped_tool_is_denied(self) -> None:
        self.assertIsNotNone(self.decide("python3 tools/gh/repo_hygiene.py --prune-worktrees 'unclosed"))
        self.assertIsNone(self.decide("echo 'unclosed"))


class HarnessLayer(unittest.TestCase):
    def test_refuses_a_real_run_and_never_an_ordinary_command(self) -> None:
        self.assertIsNotNone(gate.refuses(FORCE_CMD, "/tmp", frozenset()))
        self.assertIsNone(gate.refuses(PRECHECK, "/tmp", frozenset()))
        for command in ("ls -la", "git status", "rg -n x ~/.claude/state/precheck-gate/decisions.jsonl",
                        "cat ~/.claude/state/precheck-gate/decisions.jsonl", "grep x f", "less f"):
            self.assertIsNone(gate.refuses(command, "/tmp", frozenset()), command)

    def test_refuses_routes_through_the_store_guard(self) -> None:
        self.assertIsNotNone(gate.refuses("echo x > ~/.claude/state/precheck-gate/a.json", "/tmp", frozenset()))

    def test_the_layer_set_names_this_module(self) -> None:
        layers = json.loads((HERE / "layersets" / "registry-v1.json").read_text())["layers"]
        self.assertIn("../gate.py", [layer.get("module") for layer in layers])


if __name__ == "__main__":
    unittest.main()
