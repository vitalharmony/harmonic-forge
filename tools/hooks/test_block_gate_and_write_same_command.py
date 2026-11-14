"""Tests for block_gate_and_write_same_command (harmonic-forge#1337)."""
from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import block_gate_and_write_same_command as hook  # noqa: E402

HOOK = Path(__file__).resolve().parent / "block_gate_and_write_same_command.py"


class AC1Denied(unittest.TestCase):
    CASES = [
        "mise run check && mise run commit -m x",
        "gh pr checks 1322; gh pr merge 1322",
        "pytest -q | tail -3 && git push",
        "x=$(pytest -q) ; git push",
        'x="$(pytest -q)" ; git push',
        "python3 tools/hooks/preclose_check.py --complete && gh pr merge 1",
        "mise run check-full; mise run restart",
        "gh run watch 5 && git -C /tmp/r commit -m y",
        "python3 -m pytest tests && git push origin main",
        "gh -R o/r pr checks 9 && gh -R o/r pr merge 9",
    ]

    def test_each_is_denied_and_names_both_segments(self):
        for command in self.CASES:
            with self.subTest(command=command):
                reason = hook.decide(command)
                self.assertIsNotNone(reason)
                gate, write = hook.find(command)
                self.assertIn(f"`{gate}`", reason)
                self.assertIn(f"`{write}`", reason)


class AC2Allowed(unittest.TestCase):
    CASES = [
        "mise run check",
        "mise run commit",
        "git commit -m x",
        "mise run check; mise run restart --no-bump --no-git",
        'git commit -m "make pytest pass"',
        'gh issue comment 5 --body "gh pr merge after checks"',
        "git log --grep pytest",
        'echo "x && git push',
        "gh pr checks 5",
        "gh pr merge 5 --squash",
    ]

    def test_each_is_allowed(self):
        for command in self.CASES:
            with self.subTest(command=command):
                self.assertIsNone(hook.decide(command))

    def test_restart_without_no_git_is_a_write(self):
        self.assertEqual(hook.write_of(["mise", "run", "restart"]), "mise run restart")
        self.assertIsNone(hook.write_of(["mise", "run", "restart", "--no-bump", "--no-git"]))


class Pass1Survivors(unittest.TestCase):
    """harmonic-forge#1337 preclose pass 1 (issuecomment-6840160842)."""

    def test_shell_c_and_eval_strings_are_parsed(self):
        for command in ['bash -c "mise run check && git push"',
                        "sh -c 'pytest -q; git commit -m x'",
                        'bash -lc "pytest && git push"',
                        'eval "pytest; git push"']:
            with self.subTest(command=command):
                self.assertIsNotNone(hook.decide(command))

    def test_uv_run_prefix_and_mise_alias_and_flags_are_seen_through(self):
        for command in ["uv run pytest && git push", "uv run -- pytest; git push",
                        "mise r check && git push", "mise -C x run check && git push",
                        "mise --cd=x run check; mise r commit"]:
            with self.subTest(command=command):
                self.assertIsNotNone(hook.decide(command))

    def test_a_write_before_its_gate_is_allowed(self):
        for command in ["git push -u origin feat/x && gh pr checks 12 --watch",
                        "gh pr merge 5 --squash && gh run watch 9",
                        "git push && gh run view 1"]:
            with self.subTest(command=command):
                self.assertIsNone(hook.decide(command))

    def test_read_only_piped_gates_are_allowed(self):
        """The piped-gate fold is removed (outside the AC; R-0381)."""
        for command in ["python3 -m pytest tools/hooks -q | tail -5",
                        "gh run view 123 --log | grep -i error"]:
            with self.subTest(command=command):
                self.assertIsNone(hook.decide(command))

    def test_here_doc_body_is_a_known_residual(self):
        """Operator ruling 2026-11-12, option A: a here-doc fed to a shell is not parsed."""
        self.assertIsNone(hook.decide("bash <<X\npytest\ngit push\nX"))


class Pass2Survivors(unittest.TestCase):
    """Every pass-2 survivor (issuecomment-6840533545) is denied under word-presence
    classification (sticky-wicket pass-2 REFORGE)."""
    DENIED = [
        "mise run preclose-check -- --repo vitalharmony/harmonic-forge --issue 1337 --complete && gh pr merge 1",
        "python3 -I -m pytest -q && git push",
        "python3 -X dev -m pytest && git push",
        "pytest; git --git-dir .git commit -m x",
        "pytest; git --work-tree /tmp/w commit -m x",
        "pytest && exec git push",
        "sudo pytest; git push",
        "2>/dev/null pytest && git push",
        'env X=1 bash -c "pytest && git push"',
        "uv run --with x pytest && git push",
        "mise --cd /tmp/x run check && mise run commit",
    ]

    def test_each_is_denied(self):
        for command in self.DENIED:
            with self.subTest(command=command):
                self.assertIsNotNone(hook.decide(command))

    def test_ordinary_commands_stay_allowed(self):
        for command in ["git log --grep pytest", 'git commit -m "make pytest pass"',
                        "git push && gh pr checks 12 --watch",
                        "pytest -q | tail -5", "gh run view 123 --log | grep -i error",
                        "echo git push"]:
            with self.subTest(command=command):
                self.assertIsNone(hook.decide(command))


class ReforgePass1Survivors(unittest.TestCase):
    """Reforge pass 1 (issuecomment-6840875026), sticky-wicket PATCH
    (issuecomment-6840890733): the gate is the program, never a data word."""

    def test_a_gate_word_as_data_is_allowed(self):
        for command in ["echo pytest && git push", "pip show pytest; git commit -m x",
                        "gh pr view 12 --json checks && gh pr merge 12",
                        "gh issue view 3 --json title && gh pr merge 3"]:
            with self.subTest(command=command):
                self.assertIsNone(hook.decide(command))

    def test_mise_exec_runs_its_command_as_the_program(self):
        for command in ["mise exec -- pytest -q && git push", "mise x -- pytest && git push",
                        "mise exec python@3.13 -- pytest && git commit -m x"]:
            with self.subTest(command=command):
                self.assertIsNotNone(hook.decide(command))

    def test_gh_value_flag_is_skipped(self):
        self.assertIsNotNone(hook.decide("gh --repo o/r pr checks 9 && gh -R o/r pr merge 9"))


class ReforgePass2Survivors(unittest.TestCase):
    """Reforge pass 2 (issuecomment-6840993694), sticky-wicket PATCH
    (issuecomment-6841112555): a write is the subcommand, never a data word."""

    def test_a_write_word_as_data_is_allowed(self):
        for command in ["gh run view 9 --log-failed; git log --oneline --grep push",
                        "pytest -q && git stash push",
                        "gh pr checks 1 && git cat-file commit HEAD",
                        "mise run check && mise run gh-new-issue --title commit",
                        "mise run check && mise run gh-new-issue --title check"]:
            with self.subTest(command=command):
                self.assertIsNone(hook.decide(command))

    def test_uv_value_flags_do_not_hide_pytest(self):
        for command in ["uv run --project backend pytest -q && git push",
                        "uv run --directory backend pytest && git push",
                        "pytest; git -C /tmp/r -c user.name=x commit -m y"]:
            with self.subTest(command=command):
                self.assertIsNotNone(hook.decide(command))


class AC3Replay(unittest.TestCase):
    """protocol-failures.jsonl 281 and 287 record the shape, not the literal text:
    281 printed a failing verify result and merged in one command (F1244, PR 1322);
    287 committed the hrse half in the command that printed a failed check (F1319)."""

    def test_281_check_then_merge(self):
        self.assertIsNotNone(hook.decide(
            "gh pr checks 1322 --repo vitalharmony/harmonic-forge; "
            "gh pr merge 1322 --repo vitalharmony/harmonic-forge --squash"))

    def test_287_check_then_commit(self):
        self.assertIsNotNone(hook.decide(
            "mise run check 2>&1 | tail -20; mise run commit --message 'hrse half'"))


class Wire(unittest.TestCase):
    def run_hook(self, payload: dict) -> dict:
        out = subprocess.run([sys.executable, str(HOOK)], input=json.dumps(payload),
                             capture_output=True, text=True, check=True)
        return json.loads(out.stdout)

    def test_deny_shape(self):
        out = self.run_hook({"tool_name": "Bash",
                             "tool_input": {"command": "mise run check && git push"}})
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_allow_and_non_bash(self):
        self.assertEqual(self.run_hook({"tool_name": "Bash", "tool_input": {"command": "ls"}}), {})
        self.assertEqual(self.run_hook({"tool_name": "Read", "tool_input": {}}), {})

    def test_bad_json_allows(self):
        out = subprocess.run([sys.executable, str(HOOK)], input="not json",
                             capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(out.stdout), {})


class L1PostDryRunGate(unittest.TestCase):
    """harmonic-forge#1362: an `l1-post` dry run is a gate and a real post a write."""

    SESSION_CMD = ("mise run l1-post --issue 2674 --kind ready-for-l3 --sha a --branch b --file f "
                   "--dry-run 2>&1 | tail -2 && mise run l1-post --issue 2674 --kind ready-for-l3 "
                   "--sha a --branch b --file f 2>&1 | tail -2")

    def test_the_sessions_dry_run_then_post_is_denied_naming_both(self):
        reason = hook.decide(self.SESSION_CMD)
        self.assertIsNotNone(reason)
        self.assertIn("mise run l1-post --dry-run", reason)
        self.assertIn("`mise run l1-post`", reason)

    def test_a_dry_run_alone_and_a_post_alone_are_allowed(self):
        self.assertIsNone(hook.decide("mise run l1-post --issue 1 --dry-run"))
        self.assertIsNone(hook.decide("mise run l1-post --issue 1"))

    def test_the_script_form_is_denied(self):
        self.assertIsNotNone(hook.decide(
            "python3 tools/gh/l1_post.py --issue 1 --dry-run && python3 tools/gh/l1_post.py --issue 1"))

    def test_a_post_before_a_dry_run_is_allowed(self):
        self.assertIsNone(hook.decide(
            "mise run l1-post --issue 1 && mise run l1-post --issue 2 --dry-run"))

    def test_an_abbreviated_dry_run_flag_is_a_gate(self):
        self.assertIsNotNone(hook.decide(
            "mise run l1-post --issue 1 --dry | tail -2 && mise run l1-post --issue 1"))

    def test_a_path_named_like_the_flag_is_not_a_dry_run(self):
        """Preclose pass 1, survivor 3: `/tmp/--dry-run` is a file path, not the flag."""
        self.assertIsNotNone(hook.decide(
            "mise run l1-post --issue 1 --dry-run && mise run l1-post --issue 1 --file /tmp/--dry-run"))

    def test_a_post_through_mise_exec_is_a_write(self):
        """Pass 2, survivor 5: write_of unwraps `mise exec --` as gate_of does."""
        self.assertIsNotNone(hook.decide(
            "mise run l1-post --issue 1 --dry-run && mise exec -- python3 tools/gh/l1_post.py --issue 1"))

    def test_a_dry_run_gates_every_listed_write(self):
        self.assertIsNotNone(hook.decide("mise run l1-post --issue 1 --dry-run && git push origin x"))



class Linters1366(unittest.TestCase):
    """harmonic-forge#1366: linters, type checkers, test runners and the backend test tasks."""

    def test_ac1_ruff_piped_into_a_commit_is_denied_naming_ruff(self):
        reason = hook.decide("ruff check x.py | tail -3 && mise run commit --message m")
        self.assertIsNotNone(reason)
        self.assertIn("`ruff check`", reason)

    def test_ac2_each_is_denied(self):
        for command in ("mypy app | tail -1 && git push origin HEAD",
                        "uv run ruff check x && git push",
                        "npm --prefix frontend run lint && git commit -m x",
                        "npx tsc -b && git commit -m x",
                        "npx eslint . | tail && git push",
                        "npx vitest run && gh pr merge 1",
                        "npm run lint && git commit -m x",
                        "npm run-script typecheck && git push",
                        "npm test && git push",
                        "ruff && git commit -m x"):
            with self.subTest(command=command):
                self.assertIsNotNone(hook.decide(command))

    def test_ac3_each_is_allowed(self):
        for command in ("ruff format x.py && git commit -m x",
                        "echo ruff && git commit -m x",
                        "pip show mypy && git push",
                        'git commit -m "run eslint"',
                        "npm run dev && git commit -m x",
                        "npm install && git push"):
            with self.subTest(command=command):
                self.assertIsNone(hook.decide(command))

    def test_ac4_write_then_gate_is_allowed(self):
        self.assertIsNone(hook.decide("git push && npx tsc -b"))

    def test_ac5_backend_test_tasks_are_gates(self):
        for task in ("test-backend", "test-backend-unit", "test-backend-integration"):
            with self.subTest(task=task):
                self.assertIsNotNone(hook.decide(f"mise run {task} | tail -1 && git push origin HEAD"))


class Preclose1366Pass1(unittest.TestCase):
    """harmonic-forge#1366 preclose pass 1 survivors (issuecomment-6881346638)."""

    def test_s1_npm_option_after_run_keeps_the_gate(self):
        for command, gate in (("npm run --silent lint && git push", "npm run lint"),
                              ("npm run -s test && git push", "npm run test"),
                              ("npm run-script --if-present typecheck && git push",
                               "npm run typecheck")):
            with self.subTest(command=command):
                reason = hook.decide(command)
                self.assertIsNotNone(reason)
                self.assertIn(f"`{gate}`", reason)

    def test_s2_ruff_value_flag_before_the_subcommand(self):
        self.assertIsNotNone(hook.decide("ruff --config r.toml check . && git push"))
        self.assertIsNone(hook.decide("ruff --config check format . && git push"))

    def test_s3_test_as_a_value_or_package_is_not_npm_test(self):
        for command in ("npm --prefix test run dev && git push",
                        "npm --prefix test install && git push",
                        "npm install test && git push"):
            with self.subTest(command=command):
                self.assertIsNone(hook.decide(command))

    def test_s4_uvx_and_npm_exec_are_unwrapped(self):
        for command in ("uvx ruff check x && git push",
                        "npm exec eslint . && git push",
                        "npm exec -- tsc --noEmit && git push",
                        "npm x vitest run && git push"):
            with self.subTest(command=command):
                self.assertIsNotNone(hook.decide(command))
        self.assertIsNone(hook.decide("npm exec cowsay hi && git push"))

    def test_s5_npm_run_build_is_a_gate(self):
        reason = hook.decide("npm run build && git commit -m x")
        self.assertIsNotNone(reason)
        self.assertIn("`npm run build`", reason)


if __name__ == "__main__":
    unittest.main()
