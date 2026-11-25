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
shell_parse = gate.shell_parse
import gate_receipt as R  # noqa: E402

SESSION = "sess-1291"
#: The sample gated call: kill_check's real run, keyed to an issue. preclose_check's
#: FORCE modes are grant-guarded only (operator ruling 2026-11-19), never gated here.
FORCE_CMD = ("python3 tools/gh/kill_check.py run --repo vitalharmony/harmonic-forge --issue 1291 "
             "--checks checks.json")
PRECHECK = FORCE_CMD + " --validate"
KILL = "tools/gh/kill_check.py"
MODE = "run"


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
        (self.repo / "checks.json").write_text("[]\n")
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
        self.record(KILL, shlex.split(PRECHECK)[2:])
        self.assertIsNone(self.decide(FORCE_CMD))

    def test_the_precheck_form_is_allowed(self) -> None:
        self.assertIsNone(self.decide(PRECHECK))

    def test_a_sessionless_call_is_gated_in_the_no_session_bucket(self) -> None:
        """Operator ruling 2026-11-19: a blank session is never a skip."""
        for session in ("", "   "):
            with self.subTest(session=session):
                self.assertIsNotNone(self.decide(FORCE_CMD, session=session))
        self.assertEqual(self.rows()[-1]["outcome"], "refused")
        # A receipt the session-less precheck wrote releases the session-less call only.
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": ""}):
            self.record(KILL, shlex.split(PRECHECK)[2:])
        self.assertIsNone(self.decide(FORCE_CMD, session=""))
        self.assertIsNotNone(self.decide(FORCE_CMD))

    def test_abbreviated_precheck_token_is_the_precheck(self) -> None:
        self.assertIsNone(self.decide(FORCE_CMD + " --valid"))

    def test_help_is_not_gated(self) -> None:
        self.assertIsNone(self.decide(FORCE_CMD + " --help"))

    def test_chained_and_nested_calls_are_found(self) -> None:
        self.assertIsNotNone(self.decide(f"cd {self.repo} && {FORCE_CMD}"))
        self.assertIsNotNone(self.decide(f"bash -c {shlex.quote(FORCE_CMD)}"))
        self.assertIsNotNone(self.decide(f"timeout 60 {FORCE_CMD}"))
        self.assertIsNotNone(self.decide(FORCE_CMD.replace("tools/gh/", str(FORGE / "tools/gh") + "/")))


class CiOutageAe(GateCase):
    """harmonic-forge#894 test case 13 (C5): an AE released by a `ci-outage`
    grant is the existing l1_post AE mode, so its own dry run is the precheck
    and the first real post is not refused by this gate."""

    REAL = ("python3 tools/gh/l1_post.py --issue 894 --kind ae --sha HEAD "
            "--branch feat/894-x --file ae.md")

    def test_the_first_real_ci_outage_ae_passes_after_its_dry_run(self) -> None:
        (self.repo / "ae.md").write_text("## Lane 1 AE\n")
        self.assertIsNotNone(self.decide(self.REAL))
        self.record("tools/gh/l1_post.py", shlex.split(self.REAL)[2:] + ["--dry-run"])
        self.assertIsNone(self.decide(self.REAL))


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
        self.assertEqual((record["key"], record["mode"], record["session"]), ("F1291", MODE, SESSION))

    def test_a_call_with_no_issue_prints_the_key_placeholder(self) -> None:
        self.assertIn("FORCE <KEY> gate ", self.decide("python3 tools/gh/repo_hygiene.py --prune-worktrees"))

    def test_refusal_records_are_never_overwritten(self) -> None:
        """Pass 2 (test-honesty): write the SAME id twice with different content;
        the first record must survive (open("x")), or a FORCE line could bind to
        a call the operator never saw."""
        reason = self.decide(FORCE_CMD)
        rid = reason.split("Refusal id: ")[1][:12]
        path = self.root / "store" / "refusals" / f"{rid}.json"
        before = path.read_text()
        other = gate._Call(R.precheck_tool(KILL), ["run", "--repo", "o/x", "--issue", "9", "--checks", "c.json"],
                           ["python3", KILL], None)
        mode = R.classify(other.tool, other.argv)[0]
        gate.write_refusal(rid, "another-session", "F9", other, mode, "f" * 64, 1.0)
        self.assertEqual(path.read_text(), before)

    def test_mise_wrapper_gated(self) -> None:
        reason = self.decide("mise run kill-check -- run --repo vitalharmony/harmonic-forge --issue 1291 "
                             "--checks checks.json")
        self.assertIn("mise run kill-check -- run --repo vitalharmony/harmonic-forge --issue 1291 "
                      "--checks checks.json --validate\n", reason)
        lane3 = self.decide("mise run lane3-begin -- --issue 5")
        self.assertIn("check_lane3_ready.py --issue 5\n", lane3)
        self.assertIsNone(self.decide("mise run lane3-end"))

    def test_mise_and_direct_share_one_receipt(self) -> None:
        self.record(KILL, shlex.split(PRECHECK)[2:])
        self.assertIsNone(self.decide("mise run kill-check -- run --repo vitalharmony/harmonic-forge "
                                      "--issue 1291 --checks checks.json"))

    def test_ambiguous_or_foreign_prefix_not_precheck(self) -> None:
        # A preclose FORCE mode is no longer a precheck tool at all.
        self.assertIsNone(R.precheck_tool("tools/gh/preclose_check.py"))
        self.assertIsNone(self.decide("python3 tools/gh/preclose_check.py --repo vitalharmony/harmonic-forge "
                                      "--issue 1291 --complete --force"))
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
        # The tool spends the grant as its real run starts, once.
        argv = shlex.split(FORCE_CMD)[2:]
        self.assertIsNone(R.consume(KILL, argv, cwd=self.repo))
        self.assertNotEqual(force_grant.read_grant("F1291").get("status"), "usable")
        self.assertIsNotNone(R.consume(KILL, argv, cwd=self.repo))
        self.assertIsNotNone(self.decide(FORCE_CMD))

    def test_the_gate_never_spends_the_grant(self) -> None:
        """Sticky-wicket PATCH (grant spent before the effect is assured): a hook cannot
        know the call runs, so `<FORCE_CMD> && <FORCE_CMD>` and a refused sibling both
        leave the grant usable for the call that does run."""
        rid = self.decide(FORCE_CMD).split("Refusal id: ")[1][:12]
        self._grant(rid)
        self.assertIsNone(self.decide(f"{FORCE_CMD} && {FORCE_CMD}"))
        self.assertIsNotNone(self.decide(f"{FORCE_CMD} && python3 tools/gh/repo_hygiene.py --prune-worktrees"))
        self.assertEqual(force_grant.read_grant("F1291").get("status"), "usable")

    def test_an_abbreviated_bound_flag_is_still_bound(self) -> None:
        """Pass 2 (correctness, silent-bypass, cross-family): `--check` is argparse's
        `--checks`; editing that file after the precheck must kill the receipt."""
        short = FORCE_CMD.replace("--checks", "--check")
        self.record(KILL, shlex.split(short + " --validate")[2:])
        self.assertIsNone(self.decide(short))
        (self.repo / "checks.json").write_text('[{"patch": "x"}]')
        self.assertIsNotNone(self.decide(short))
        self.assertIsNotNone(R.consume(KILL, shlex.split(short)[2:], cwd=self.repo))

    def test_an_unreadable_option_catalog_never_lets_a_real_run_through(self) -> None:
        """Pass 2 cross-family: an unreadable tool_options.json read as `{}`
        made the real run ungated on both sides; now consume() raises."""
        R.tool_options.cache_clear()
        self.addCleanup(R.tool_options.cache_clear)
        with mock.patch.object(R.Path, "read_text", side_effect=OSError("gone")):
            with self.assertRaises(OSError):
                R.consume(KILL, shlex.split(FORCE_CMD)[2:], cwd=self.repo)

    def test_a_repeated_bound_flag_binds_its_last_value(self) -> None:
        """Post-verdict: argparse uses the last --checks; so must the receipt."""
        (self.repo / "effective.json").write_text("[]\n")
        cmd = FORCE_CMD + " --checks effective.json"
        self.record(KILL, shlex.split(cmd + " --validate")[2:])
        self.assertIsNone(self.decide(cmd))
        (self.repo / "effective.json").write_text('[{"patch": "x"}]')
        self.assertIsNotNone(self.decide(cmd))
        self.assertIsNotNone(R.consume(KILL, shlex.split(cmd)[2:], cwd=self.repo))

    def test_a_literal_tilde_path_binds_the_file_the_tool_reads(self) -> None:
        """Post-verdict: a quoted `~/x` reaches the tool as cwd/~/x; the receipt
        must bind that file, not $HOME/x."""
        (self.repo / "~").mkdir()
        (self.repo / "~" / "c.json").write_text("[]\n")
        argv = ["run", "--repo", "vitalharmony/harmonic-forge", "--issue", "1291", "--checks", "~/c.json"]
        self.record(KILL, [*argv, "--validate"])
        self.assertIsNone(R.consume(KILL, argv, cwd=self.repo))
        (self.repo / "~" / "c.json").write_text('[{"patch": "x"}]')
        self.assertIsNotNone(R.consume(KILL, argv, cwd=self.repo))

    def test_an_unkeyable_repo_refuses_and_never_crashes(self) -> None:
        """batch_auth.issue_key returns None for a repo it cannot key: the grant
        lookup falls back to every grant file, never `None.upper()`."""
        cmd = FORCE_CMD.replace("vitalharmony/harmonic-forge", "R")
        self.assertIn("Refusal id: ", self.decide(cmd))
        self.assertIn("no live precheck receipt", R.consume(KILL, shlex.split(cmd)[2:], cwd=self.repo))

    def test_a_gate_grant_never_releases_another_call(self) -> None:
        rid = self.decide(FORCE_CMD).split("Refusal id: ")[1][:12]
        self._grant(rid)
        self.assertIsNotNone(self.decide(FORCE_CMD + " --timeout 5"))
        self.assertIsNotNone(R.consume(KILL, shlex.split(FORCE_CMD + " --timeout 5")[2:], cwd=self.repo))
        self.assertEqual(force_grant.read_grant("F1291").get("status"), "usable")


class Decision6Log(GateCase):
    def test_every_outcome_logged(self) -> None:
        self.decide(FORCE_CMD)
        self.decide(PRECHECK)
        self.record(KILL, shlex.split(PRECHECK)[2:])
        self.decide(FORCE_CMD)
        self.decide("python3 tools/gh/kill_check.py run 'unterminated")
        self.decide("git push origin feat/x")
        outcomes = [row["outcome"] for row in self.rows()]
        self.assertEqual(outcomes, ["refused", "precheck", "allowed-receipt", "error", "guarded-elsewhere"])
        for row in self.rows():
            self.assertEqual(set(row), {"ts", "outcome", "tool", "mode", "class", "fp", "session",
                                        "tuid", "exit", "reason"})

    def test_log_never_holds_argv(self) -> None:
        self.decide(FORCE_CMD + " --base secret-model-name")
        self.decide("mise run lane3-begin -- --issue 424242")
        text = (self.root / "store" / "decisions.jsonl").read_text()
        # The mode column is the registry's mode name, never the call's tokens.
        for token in ("vitalharmony", "secret-model-name", "424242", "--base", "--issue", SESSION):
            self.assertNotIn(token, text)

    def test_outside_families_are_never_refused(self) -> None:
        self.assertIsNone(self.decide("gh pr merge 5 --squash"))
        self.assertIsNone(self.decide("git push origin main"))


class Decision7FailOpen(GateCase):
    """Sticky-wicket PATCH: the gate fails open on its own error; every precheck
    mode's tool runs consume() itself, which still refuses."""

    def test_deadline_bounds_a_slow_resolver(self) -> None:
        """A slow `git rev-parse` hits the internal deadline, never the hook timeout."""
        timeouts = []

        def slow(*args, **kwargs):
            timeouts.append(kwargs.get("timeout"))
            time.sleep(0.05)
            raise subprocess.TimeoutExpired(args[0], kwargs.get("timeout"))
        started = time.monotonic()
        with mock.patch.object(R.subprocess, "run", side_effect=slow):
            self.assertIsNone(self.decide(FORCE_CMD))
        self.assertLess(time.monotonic() - started, R.DEADLINE_S + 1)
        self.assertEqual(self.rows()[-1]["outcome"], "error")
        # The resolver is handed the remaining deadline, never no timeout at all.
        self.assertTrue(timeouts)
        self.assertTrue(all(isinstance(s, (int, float)) and 0 < s <= R.DEADLINE_S for s in timeouts), timeouts)

    def test_an_error_fails_open_and_consume_still_refuses(self) -> None:
        with mock.patch.object(R, "fingerprint", side_effect=RuntimeError("boom")):
            self.assertIsNone(self.decide(FORCE_CMD))
        self.assertEqual(self.rows()[-1]["outcome"], "error")
        self.assertIsNotNone(R.consume(KILL, shlex.split(FORCE_CMD)[2:], cwd=self.repo))

    def test_a_broken_registry_never_denies_unrelated_bash(self) -> None:
        with mock.patch.object(R, "registry", side_effect=RuntimeError("cite drift")):
            for command in ("git status", "echo repo_hygiene", FORCE_CMD):
                self.assertIsNone(self.decide(command), command)

    def test_the_hook_fails_open_when_the_gate_cannot_load(self) -> None:
        payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": "grep repo_hygiene f"},
                              "session_id": SESSION, "cwd": str(self.repo)})
        sys.path.insert(0, str(FORGE / "tools" / "hooks"))
        import precheck_gate  # noqa: PLC0415
        out = io.StringIO()
        with mock.patch.dict(sys.modules, {"gate": None}), contextlib.redirect_stdout(out), \
                mock.patch.object(sys, "stdin", io.TextIOWrapper(io.BytesIO(payload.encode()))):
            self.assertEqual(precheck_gate.main(), 0)
        self.assertEqual(out.getvalue(), "")


class ReforgeToolSideBackstop(GateCase):
    """REFORGE (issuecomment-7002756185): the three formerly unbackstopped modes
    refuse their own real run without a receipt, however the command was spelled;
    the gate's spelling recognition is not load-bearing."""

    def test_no_mode_can_declare_no_backstop(self) -> None:
        import effects  # noqa: PLC0415
        self.assertFalse([m for _, m in effects.precheck_modes(R.registry()) if "backstop" in m])

    def test_a_blank_session_is_the_no_session_bucket_tool_side(self) -> None:
        """Survivors 2/X2: blanking CLAUDE_CODE_SESSION_ID never skips consume()."""
        argv = shlex.split(FORCE_CMD)[2:]
        for value in ("", "  "):
            with self.subTest(value=value), mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": value}):
                self.assertIsNotNone(R.consume(KILL, argv, cwd=self.repo))
        env = {k: v for k, v in os.environ.items() if k != "CLAUDE_CODE_SESSION_ID"}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertIsNotNone(R.consume(KILL, argv, cwd=self.repo))

    def test_consume_refuses_each_former_unbackstopped_mode_without_a_receipt(self) -> None:
        for tool, argv in (("tools/gh/repo_hygiene.py", ["--prune-worktrees"]),
                           ("tools/gh/check_lane3_ready.py", ["--mark-active", "--issue", "5"]),
                           ("tools/lane/lanes-claude", ["--session", "x"])):
            with self.subTest(tool=tool):
                self.assertIsNotNone(R.consume(tool, argv, cwd=self.repo))

    def test_repo_hygiene_refuses_a_real_prune_before_any_call(self) -> None:
        import repo_hygiene  # noqa: PLC0415
        with mock.patch.object(sys, "argv", ["repo_hygiene.py", "--prune-worktrees"]), \
                mock.patch.object(repo_hygiene.subprocess, "run", side_effect=AssertionError("no call")), \
                contextlib.redirect_stderr(io.StringIO()) as err, \
                mock.patch.dict(os.environ, {"HF_CALLER_CWD": str(self.repo)}):
            self.assertEqual(repo_hygiene.main(), 2)
        self.assertIn("no live precheck receipt", err.getvalue())

    def test_check_lane3_ready_refuses_mark_active_before_any_read(self) -> None:
        import check_lane3_ready  # noqa: PLC0415
        with mock.patch.object(check_lane3_ready, "fetch_comments", side_effect=AssertionError("no read")), \
                mock.patch.object(check_lane3_ready, "clear_marker"), \
                mock.patch.object(check_lane3_ready, "mark_active", side_effect=AssertionError("no mark")), \
                mock.patch.dict(os.environ, {"HF_CALLER_CWD": str(self.repo)}), \
                contextlib.redirect_stderr(io.StringIO()) as err, self.assertRaises(SystemExit):
            check_lane3_ready.main(["--mark-active", "--issue", "5"])
        self.assertIn("no live precheck receipt", err.getvalue())

    def test_the_bash_launcher_consumes_through_the_cli(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(R.main(["consume", "--tool", "tools/lane/lanes-claude", "--", "--session", "x"]), 1)
        R.record("tools/lane/lanes-claude", ["--session", "x", "--dry-run"], self.repo,
                 _argv0=str(FORGE / "tools/lane/lanes-claude"))
        with mock.patch.dict(os.environ, {"HF_CALLER_CWD": str(self.repo)}):
            self.assertEqual(R.main(["consume", "--tool", "tools/lane/lanes-claude", "--", "--session", "x"]), 0)

    def test_the_launcher_calls_consume_before_any_tmux_session(self) -> None:
        text = (FORGE / "tools/lane/lanes-claude").read_text()
        self.assertLess(text.index("gate_receipt.py\" consume --tool tools/lane/lanes-claude"),
                        text.index("tmux new-session"))


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


class RedirectDescriptorDigit(unittest.TestCase):
    """harmonic-forge#1461: a redirect's descriptor digit is not an argument."""

    BASE = "python3 tools/gh/l1_post.py --repo o/r --issue 7 --kind handoff"

    def argv(self, command: str) -> list[str]:
        calls, _others, ok = gate._calls_in(command)
        self.assertTrue(ok)
        self.assertEqual(len(calls), 1)
        return calls[0].argv

    def test_attached_digit_redirects_fingerprint_like_the_plain_call(self) -> None:
        plain = self.argv(self.BASE)
        for suffix in (" > log 2>&1", " 2>/dev/null", " 2>>err", " 1>out", " 0<in", " &>log"):
            with self.subTest(suffix=suffix):
                self.assertEqual(self.argv(self.BASE + suffix), plain)

    def test_a_spaced_digit_before_a_redirect_stays_an_argument(self) -> None:
        """Reforge #3 pass 1 (four lenses + cross-family): a spaced digit is an
        argument to bash, so it stays in the fingerprint."""
        for suffix in (" 2 >f", " 1461 > log 2>&1", " 7 >f"):
            with self.subTest(suffix=suffix):
                self.assertEqual(self.argv(self.BASE + suffix)[-1], suffix.split()[0])

    def test_quoted_escaped_and_ampersand_digits_stay_arguments(self) -> None:
        """Epoch 3 pass 2 + cross-family: bash keeps these digits."""
        for suffix in (" '2'>f", " \\2>f", " 2''>f", " 7&>f", " 7&>>f", " 10>f"):
            with self.subTest(suffix=suffix):
                self.assertEqual(len(self.argv(self.BASE + suffix)), len(self.argv(self.BASE)) + 1)

    def test_a_digit_not_directly_before_a_redirect_stays_an_argument(self) -> None:
        for suffix, kept in ((" 2 2>f", "2"), (" v2>f", "v2"), (" ٢>f", "٢"), (" note\\ 2>f", "note 2")):
            with self.subTest(suffix=suffix):
                self.assertEqual(self.argv(self.BASE + suffix)[-1], kept)


class RedirectSingleReading(unittest.TestCase):
    """harmonic-forge#1461, second sticky-wicket REFORGE: shell_parse marks the
    digit words before a redirect; the gate drops them from the raw argv before
    prefix stripping and reads that one argv. Only a digit attached to the
    operator is marked; a spaced digit is an argument."""

    BASE = RedirectDescriptorDigit.BASE
    CORPUS = (
        "", " > log 2>&1", " 2>/dev/null", " 2>>err", " 1>out", " 0<in", " &>log",
        " 2>&1>log", " 2<&0", " 2<>f", " 2>|f", " {fd}>f", " 2>&-", " 1>&2 2>&1",
        " 2>&1 | tee log", " 2>&1; true", " && true 2>&1", " 2>'q f'", ' 2>"$HOME/x"',
        " 2<(true)", " 2 >f", " '2'>f", " v2>f", " ٢>f", " note\\ 2>f",
        " # 2>f", " $((1>2))", " <<EOF\n2>x\nEOF",
    )

    @staticmethod
    def main_reading(command: str):
        """main's behavior: no digit word is ever dropped."""
        real = shell_parse.simple_commands

        def no_candidates(text):
            return [i._replace(fd_candidates=()) if isinstance(i, shell_parse.SimpleCommand) else i
                    for i in real(text)]
        return mock.patch.object(gate.shell_parse, "simple_commands", side_effect=no_candidates)

    def paths(self, command: str) -> list[str]:
        calls, _others, ok = gate._calls_in(command)
        self.assertTrue(ok)
        return [c.tool["path"] for c in calls]

    def test_every_reported_bypass_is_found_and_refused(self) -> None:
        """Epoch-2 survivors and the sticky-wicket's fourth case."""
        for command in (
                "python3 2>f tools/gh/kill_check.py run --issue 1 2>&1>log",
                "bash -c 2>/dev/null '" + FORCE_CMD + "' 2>&1>log",
                "bash -c 2>/dev/null '" + FORCE_CMD + "'",
                "env 2>f python3 tools/gh/kill_check.py run --issue 1 2>&1>x",
                "timeout 30 2>f python3 tools/gh/kill_check.py run --issue 1 2>&1>x",
                "2>f python3 tools/gh/kill_check.py run --issue 1",
                "if python3 2>f tools/gh/kill_check.py run --issue 1; then :; fi",
                FORCE_CMD + """ ; bash -c 2>/dev/null 'x "'"""):
            with self.subTest(command=command):
                # A later part that does not parse may leave ok False; the call
                # found before it is still gated.
                self.assertIn("tools/gh/kill_check.py", [c.tool["path"] for c in gate._calls_in(command)[0]])
                self.assertIsNotNone(gate.refuses(command, "/tmp", frozenset()))

    def test_a_spaced_prefix_argument_is_kept_and_the_call_found(self) -> None:
        """Pitch inspection's case: `timeout 5 >f` keeps its `5`, so the
        prefix stripper never eats `bash` and the inner call is found."""
        command = "timeout 5 >f bash -c '" + FORCE_CMD + "'"
        self.assertIn("tools/gh/kill_check.py", self.paths(command))
        self.assertIsNotNone(gate.refuses(command, "/tmp", frozenset()))

    def test_an_attached_descriptor_before_the_program_fingerprints_the_plain_call(self) -> None:
        """Reforge #3 pass 1 (correctness): these were refused as ambiguous
        even with a receipt; the executed argv is the plain call's."""
        plain = gate._calls_in(FORCE_CMD)[0][0].argv
        for prefix in ("2>f ", "env 2>f ", "x=1 2>f ", "timeout 30 2>f "):
            with self.subTest(prefix=prefix):
                calls = gate._calls_in(prefix + FORCE_CMD)[0]
                self.assertEqual([c.argv for c in calls], [plain])

    def test_a_spaced_digit_never_reuses_a_shorter_receipt(self) -> None:
        """Reforge #3 pass 1 (silent-bypass): `run 1461 7 >f` runs with `7`;
        its fingerprint must carry it, never match a receipt for `run 1461`."""
        calls = gate._calls_in("python3 tools/gh/kill_check.py run 1461 7 >f")[0]
        self.assertEqual(calls[0].argv, ["run", "1461", "7"])

    def test_a_nested_call_survives_a_later_unparseable_part(self) -> None:
        """Reforge #3 pass 1 (test-honesty): a call found inside `bash -c`
        before a part that does not parse is still gated."""
        command = """bash -c "python3 tools/gh/kill_check.py run --issue 1 ; bash -c 'x \\"'" """
        self.assertIn("tools/gh/kill_check.py", [c.tool["path"] for c in gate._calls_in(command)[0]])
        self.assertIsNotNone(gate.refuses(command, "/tmp", frozenset()))

    def test_a_redirected_shell_wrapper_is_one_call(self) -> None:
        plain = gate._calls_in("bash -c '" + FORCE_CMD + "'")[0]
        redirected = gate._calls_in("bash -c '" + FORCE_CMD + "' 2>/dev/null")[0]
        self.assertEqual(len(redirected), 1)
        self.assertEqual(redirected[0].argv, plain[0].argv)

    def test_an_unparseable_command_is_unparsed_as_on_main(self) -> None:
        self.assertFalse(gate._calls_in(self.BASE + " 'unterminated")[2])

    def test_the_corpus_finds_at_least_the_calls_main_finds(self) -> None:
        for suffix in self.CORPUS:
            command = self.BASE + suffix
            with self.subTest(suffix=suffix):
                with self.main_reading(command):
                    main_calls, _o, main_ok = gate._calls_in(command)
                calls, _others, ok = gate._calls_in(command)
                self.assertEqual(ok, main_ok)
                for call in main_calls:
                    self.assertIn(call.tool["path"], [c.tool["path"] for c in calls])

    def test_the_corpus_never_allows_what_main_refuses(self) -> None:
        for suffix in self.CORPUS:
            command = FORCE_CMD + suffix
            with self.subTest(suffix=suffix):
                with self.main_reading(command):
                    main_refused = gate.refuses(command, "/tmp", frozenset()) is not None
                if main_refused:
                    self.assertIsNotNone(gate.refuses(command, "/tmp", frozenset()))

    def test_a_redirect_inserted_anywhere_never_ungates_the_call(self) -> None:
        """Fuzz (sticky-wicket prescription): an attached ` 2>f` or ` 1>&2`
        inserted between any two words of a gated command, alone or with a
        trailing ` 2>&1>log`, is always found and refused."""
        words = shlex.split(FORCE_CMD)
        prefixes = ([], ["env"], ["timeout", "30"], ["nice", "-n", "5"], ["command"])
        for prefix in prefixes:
            base = prefix + words
            for at in range(len(base) + 1):
                for redirect in ("2>f", "1>&2", "0<in"):
                    for tail in ("", " 2>&1>log"):
                        command = " ".join(shlex.quote(w) for w in base[:at]) + " " + redirect \
                            + " " + " ".join(shlex.quote(w) for w in base[at:]) + tail
                        with self.subTest(command=command):
                            self.assertIsNotNone(gate.refuses(command.strip(), "/tmp", frozenset()))


class BashOracle(unittest.TestCase):
    """Sticky-wicket PATCH, epoch 3: real bash is the judge. For every spelling
    the gate's argv equals the argv bash passes, or keeps MORE words than bash
    (so no receipt matches and the call is refused). Dropping a word bash keeps
    is the only divergence that can reuse a receipt, and it fails here."""

    DIGITS = ("2", "7", "0", "10", "'2'", "\\2", "2''")
    OPERATORS = (">f", ">>f", ">|f", ">&2", "<in", "<>f", "&>f", "&>>f")

    def corpus(self):
        for digit in self.DIGITS:
            for op in self.OPERATORS:
                yield f"{digit}{op}"
                yield f"{digit} {op}"
        yield from ("2>&1", "1>f 2>&1", "{fd}>f", "2\\\n>f", "x;2>f", "2>f 7")

    def bash_argv(self, tail: str, tmp: str) -> list[str]:
        out = Path(tmp) / "argv"
        script = f'f(){{ printf "%s\\0" "$@" > {shlex.quote(str(out))}; }}; f run 1461 {tail}'
        subprocess.run(["bash", "-c", script], cwd=tmp, capture_output=True, timeout=10,
                       input="")
        return out.read_text().split("\0")[:-1] if out.exists() else []

    def test_the_gate_never_drops_a_word_bash_keeps(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "in").write_text("")
            for tail in self.corpus():
                with self.subTest(tail=tail):
                    expected = self.bash_argv(tail, tmp)
                    calls, _others, ok = gate._calls_in(f"python3 tools/gh/kill_check.py run 1461 {tail}")
                    if not ok or not calls or not expected:
                        continue  # unparsed or no call: main's behavior, never a new allow
                    got = calls[0].argv
                    self.assertTrue(got == expected or len(got) > len(expected),
                                    f"gate {got} vs bash {expected}")


class ShellParseFdCandidates(unittest.TestCase):
    def test_a_digit_before_a_redirect_is_a_candidate_and_argv_is_unchanged(self) -> None:
        (item,) = shell_parse.simple_commands("cmd a 2>f b 7 >g")
        self.assertEqual(item.argv, ["cmd", "a", "2", "b", "7"])
        self.assertEqual(item.fd_candidates, (2,))  # the spaced 7 is an argument

    def test_indices_count_from_after_stripped_reserved_words(self) -> None:
        item = shell_parse.simple_commands("if cmd 2>f x; then :; fi")[0]
        self.assertEqual(item.argv, ["cmd", "2", "x"])
        self.assertEqual(item.fd_candidates, (1,))

    def test_only_a_proven_descriptor_is_a_candidate(self) -> None:
        for command in ("cmd 10>f", "cmd '2'>f", "cmd \\2>f", "cmd 2''>f", "cmd 7&>f", "cmd 2 >f", "cmd x2>f"):
            with self.subTest(command=command):
                self.assertEqual(shell_parse.simple_commands(command)[0].fd_candidates, ())
        for command in ("cmd 2>f", "x;2>f", "2>f cmd", "cmd 0<in", "cmd 1>>f"):
            with self.subTest(command=command):
                self.assertTrue(shell_parse.simple_commands(command)[-1].fd_candidates)

    def test_the_field_defaults_to_empty(self) -> None:
        self.assertEqual(shell_parse.SimpleCommand(["a"], [], None, None).fd_candidates, ())

if __name__ == "__main__":
    unittest.main()
