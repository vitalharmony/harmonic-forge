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
        """The piped-gate fold is removed (outside the AC; R-0381). A gate piped into
        `tail`/`head` is denied since harmonic-forge#1368 (operator-agreed AC4 amendment)."""
        self.assertIsNone(hook.decide("gh run view 123 --log | grep -i error"))
        self.assertIsNotNone(hook.decide("python3 -m pytest tools/hooks -q | tail -5"))

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
                        "gh run view 123 --log | grep -i error",
                        "echo git push"]:
            with self.subTest(command=command):
                self.assertIsNone(hook.decide(command))
        # harmonic-forge#1368 (operator-agreed AC4 amendment): no longer allowed.
        self.assertIsNotNone(hook.decide("pytest -q | tail -5"))


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


class Preclose1366Pass2(unittest.TestCase):
    """harmonic-forge#1366 preclose pass 2 survivors (issuecomment-6881496159) and the
    sticky-wicket PATCH (issuecomment-6881508159)."""

    def test_s1_npm_exec_past_npm_value_flags(self):
        for command in ("npm exec --workspace w eslint && git push",
                        "npm --prefix x x eslint . && git push",
                        "npm --prefix exec exec tsc && git push"):
            with self.subTest(command=command):
                self.assertIsNotNone(hook.decide(command))

    def test_s2_wrapper_value_equal_to_the_program(self):
        self.assertIsNotNone(hook.decide("uvx --from ruff ruff check x && git push"))
        self.assertIsNone(hook.decide("uvx --from ruff ruff format x && git push"))

    def test_s3_help_and_version_are_not_gates(self):
        for command in ("ruff --version && git push", "ruff -h && git push",
                        "npx tsc --version && git push", "mypy --help && git push",
                        "eslint -V && git push"):
            with self.subTest(command=command):
                self.assertIsNone(hook.decide(command))

    def test_s3_a_path_named_like_a_help_flag_is_still_a_gate(self):
        self.assertIsNotNone(hook.decide("eslint src/--help && git push"))

    def test_s4_every_ruff_value_flag(self):
        for flag in sorted(hook._RUFF_VALUE_FLAGS):
            with self.subTest(flag=flag):
                self.assertIsNotNone(hook.decide(f"ruff {flag} v check x && git push"))

    def test_s4_every_npm_value_flag(self):
        for flag in sorted(hook._NPM_VALUE_FLAGS):
            with self.subTest(flag=flag):
                self.assertIsNone(hook.decide(f"npm {flag} test run dev && git push"))
                self.assertIsNotNone(hook.decide(f"npm {flag} v run lint && git push"))

    def test_s4_every_npm_alias(self):
        for alias in hook._NPM_TEST:
            with self.subTest(alias=alias):
                self.assertIsNotNone(hook.decide(f"npm {alias} && git push"))
        for alias in hook._NPM_RUN:
            with self.subTest(alias=alias):
                self.assertIsNotNone(hook.decide(f"npm {alias} lint && git push"))
        for alias in hook._NPM_EXEC:
            with self.subTest(alias=alias):
                self.assertIsNotNone(hook.decide(f"npm {alias} eslint && git push"))


class MaskedGate1368(unittest.TestCase):
    """harmonic-forge#1368: a gate piped into tail/head without pipefail."""

    def test_ac1_each_masked_gate_is_denied(self):
        for command, gate in (("mise run check 2>&1 | tail -3", "mise run check"),
                              ("pytest -q | tail -1", "pytest"),
                              ("mise run check | head -20", "mise run check"),
                              ("ruff check x |& tail -5", "ruff check")):
            with self.subTest(command=command):
                reason = hook.decide(command)
                self.assertIsNotNone(reason)
                self.assertIn(f"`{gate}`", reason)
                self.assertIn("harmonic-forge#1368", reason)

    def test_ac2_pipefail_does_not_exempt(self):
        """REFORGE (issuecomment-6882876419): there is no pipefail exemption."""
        for command in ("set -o pipefail; mise run check | tail -3",
                        "set -euo pipefail && pytest | tail -1"):
            with self.subTest(command=command):
                self.assertIsNotNone(hook.decide(command))

    def test_ac3_each_is_allowed(self):
        for command in ("mise run check > log 2>&1; tail -3 log",
                        "echo check | tail -1",
                        "git log | head -5",
                        "mise run check | tee log"):
            with self.subTest(command=command):
                self.assertIsNone(hook.decide(command))

    def test_a_later_truncator_anywhere_in_the_pipeline(self):
        self.assertIsNotNone(hook.decide("mise run check | tail -3 | cat"))

    def test_a_shell_c_string_is_read(self):
        self.assertIsNotNone(hook.decide('bash -c "pytest | tail -1"'))

    def test_pipefail_after_the_gate_does_not_exempt(self):
        self.assertIsNotNone(hook.decide("mise run check | tail -3; set -o pipefail"))

    def test_a_substitution_body_is_read(self):
        self.assertIsNotNone(hook.decide("x=$(pytest | tail -1); echo $x"))


class MaskedGate1368Pass1(unittest.TestCase):
    """harmonic-forge#1368 preclose pass 1 survivors (issuecomment-6882649150) and the
    sticky-wicket PATCH (issuecomment-6882662014)."""

    def test_s1_pipefail_turned_off_does_not_exempt(self):
        for command in ("set +o pipefail; pytest | tail -1",
                        "set -o pipefail; set +o pipefail; pytest | tail -1"):
            with self.subTest(command=command):
                self.assertIsNotNone(hook.decide(command))

    def test_s2_outer_pipefail_is_not_inherited_by_a_child_shell(self):
        self.assertIsNotNone(hook.decide('set -o pipefail; bash -c "pytest | tail -1"'))

    def test_s3_subshell_or_conditional_pipefail_does_not_exempt(self):
        for command in ("(set -o pipefail); pytest | tail -1",
                        "false && set -o pipefail; pytest | tail -1"):
            with self.subTest(command=command):
                self.assertIsNotNone(hook.decide(command))

    def test_s4_a_grouped_gate_is_denied(self):
        for command in ("{ pytest; } | tail -1", "(pytest) | tail -1"):
            with self.subTest(command=command):
                self.assertIsNotNone(hook.decide(command))

    def test_s5_a_grouped_truncator_is_denied(self):
        self.assertIsNotNone(hook.decide("pytest | (tail -1)"))

    def test_s6_a_tail_after_a_non_pipe_separator_is_not_piped(self):
        """Second REFORGE (issuecomment-6884035774, operator "1 yes"): the rule is
        order-only, so a gate, a later pipe and a later tail deny even when that tail
        reads a file. An accepted, fail-closed over-deny."""
        self.assertIsNotNone(hook.decide("mise run check | tee log; tail -1 log"))
        self.assertIsNone(hook.decide("mise run check > log 2>&1; tail -3 log"))


class MaskedGate1368Reforge(unittest.TestCase):
    """harmonic-forge#1368 REFORGE (issuecomment-6882876419): pass-2 survivors
    (issuecomment-6882868464) and the extended AC1."""

    def test_a_wrapped_gate_piped_into_tail_is_denied(self):
        for command, gate in (('bash -c "pytest" | tail -1', "pytest"),
                              ("eval pytest | tail -1", "pytest"),
                              ('sh -c "mise run check" | tail -3', "mise run check")):
            with self.subTest(command=command):
                reason = hook.decide(command)
                self.assertIsNotNone(reason)
                self.assertIn(f"`{gate}`", reason)

    def test_a_wrapped_truncator_is_denied(self):
        self.assertIsNotNone(hook.decide('pytest | bash -c "tail -1"'))

    def test_a_piped_set_never_exempts(self):
        self.assertIsNotNone(hook.decide("set -o pipefail | cat; pytest -q | tail -1"))

    def test_a_substitution_is_read_whatever_the_outer_shell_set(self):
        self.assertIsNotNone(hook.decide("set -o pipefail; x=$(pytest | tail -1)"))

    def test_a_wrapped_non_gate_is_allowed(self):
        self.assertIsNone(hook.decide('bash -c "echo hi" | tail -1'))

    def test_the_message_offers_no_pipefail_remedy(self):
        self.assertNotIn("pipefail", hook.decide("pytest | tail -1"))


class MaskedGate1368ReforgePass1(unittest.TestCase):
    """harmonic-forge#1368 reforge pass-1 survivors (issuecomment-6883817713) and the
    sticky-wicket PATCH (issuecomment-6883830423)."""

    def test_s1_a_truncator_anywhere_in_a_piped_group_or_script(self):
        for command in ("pytest | { head -5; tail -3; }", "pytest | (cat; tail -1)",
                        "pytest | (sed 1d; tail -1)", 'pytest | bash -c "sed 1d; tail -1"',
                        "pytest | bash -c 'cat | tail -1'"):
            with self.subTest(command=command):
                self.assertIsNotNone(hook.decide(command))

    def test_s2_backtick_and_quoted_substitutions_are_read(self):
        for command in ("x=`pytest | tail -1`", 'echo "$(pytest | tail -1)"'):
            with self.subTest(command=command):
                self.assertIsNotNone(hook.decide(command))

    def test_s3_a_parse_error_elsewhere_keeps_the_deny(self):
        for command in ("mise run check 2>&1 | tail -3  # don't lose the summary",
                        "pytest | tail -1; bash -c \"'\""):
            with self.subTest(command=command):
                self.assertIsNotNone(hook.decide(command))

    def test_s4_the_first_gate_stays_the_gate(self):
        self.assertIsNotNone(hook.decide("pytest | grep -v x | tail -1"))

    def test_a_comment_alone_does_not_deny(self):
        self.assertIsNone(hook.decide("echo don't # fine"))


class MaskedGate1368SecondReforge(unittest.TestCase):
    """harmonic-forge#1368 reforge pass-2 survivors (issuecomment-6884023933) under the
    order-only rule of the second REFORGE (issuecomment-6884035774)."""

    def test_a_line_break_after_the_pipe(self):
        for command in ("pytest |\n tail -1", "mise run check |\ntail -1"):
            with self.subTest(command=command):
                self.assertIsNotNone(hook.decide(command))

    def test_if_while_case_bodies(self):
        for command in ("pytest | if true; then tail -1; fi",
                        "pytest | while true; do tail -1; break; done",
                        "pytest | case x in x) tail -1;; esac"):
            with self.subTest(command=command):
                self.assertIsNotNone(hook.decide(command))

    def test_an_earlier_comment_that_does_not_parse(self):
        self.assertIsNotNone(hook.decide("# don't lose output\npytest | tail -1"))

    def test_a_pipe_recorded_only_on_the_reader(self):
        self.assertIsNotNone(hook.decide("{ pytest; } | tail -1"))

    def test_order_matters(self):
        for command in ("tail -3 log | grep x; pytest", "pytest > log; tail -3 log",
                        "git log | head -5; pytest",
                        # a pipe BEFORE the gate never counts, even with a tail after it
                        "git log | cat; pytest; tail -3 log"):
            with self.subTest(command=command):
                self.assertIsNone(hook.decide(command))

    def test_an_unparseable_script_is_read_as_raw_text(self):
        self.assertIsNotNone(hook.decide('bash -c "pytest | tail -1 # don\'t"'))


class MaskedGate1368FinalPass1(unittest.TestCase):
    """harmonic-forge#1368 final-reforge pass-1 survivors (issuecomment-6885144464),
    sticky-wicket PATCH (issuecomment-6885150729, correction 6885155066)."""

    def test_f1_a_gate_word_as_an_argument_is_not_a_gate(self):
        for command in ("git log --grep pytest | head -5 # don't",
                        "grep -rn ruff . | head  # it's"):
            with self.subTest(command=command):
                self.assertIsNone(hook.decide(command))
        self.assertIsNotNone(hook.decide("pytest | tail -1 # don't"))

    def test_f2_every_raw_gate_is_recognised(self):
        for gate, script in (("pytest", "pytest"), ("ruff", "ruff check x"),
                             ("mise run check", "mise run check"), ("mypy", "mypy ."),
                             ("tsc", "tsc -b"), ("eslint", "eslint ."), ("vitest", "vitest run"),
                             ("npm test", "npm test"), ("npm run", "npm run lint"),
                             ("gh pr checks", "gh pr checks 1"),
                             ("preclose_check.py", "python3 tools/gh/preclose_check.py --complete"),
                             ("l1-post", "l1-post x")):
            with self.subTest(gate=gate):
                reason = hook.decide(f'bash -c "{script} | tail -1 # don\'t"')
                self.assertIsNotNone(reason)
                self.assertIn(gate, reason)

    def test_f3_nested_wrappers_are_read(self):
        for command in ("bash -c \"bash -c 'pytest | tail -1'\"",
                        'bash -c "bash -c \'bash -c \\"pytest | tail -1\\"\'"'):
            with self.subTest(command=command):
                self.assertIsNotNone(hook.decide(command))

    def test_f4_the_raw_fallback_needs_a_pipe(self):
        self.assertIsNone(hook.decide('bash -c "mise run check > log; tail -3 log # don\'t"'))
        self.assertIsNotNone(hook.decide('bash -c "mise run check | tail -3 # don\'t"'))


if __name__ == "__main__":
    unittest.main()
