#!/usr/bin/env python3
"""Tests for block_data_migration_close.py (hrse#859).

Every evasion the first implementation's review found is a test here.
"""
import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import block_data_migration_close as hook  # noqa: E402

HOOK = Path(__file__).resolve().parent / "block_data_migration_close.py"
REPO = "vitalharmony/hrse"


def bash(command: str) -> dict:
    return {"tool_name": "Bash", "tool_input": {"command": command}}


def run_hook(payload: dict) -> dict:
    result = subprocess.run(
        [sys.executable, str(HOOK)], input=json.dumps(payload),
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout or "{}")


class FindTargetsTests(unittest.TestCase):
    def assert_target(self, command: str, expected: tuple[str | None, str]) -> None:
        self.assertEqual(hook.find_close_targets(command), [expected])

    def test_issue_close_with_repo(self) -> None:
        self.assert_target(f"gh issue close 849 --repo {REPO}", (REPO, "849"))

    def test_issue_close_short_repo_flag(self) -> None:
        """-R is gh's documented short form; the first version missed it."""
        self.assert_target(f"gh issue close 849 -R {REPO}", (REPO, "849"))

    def test_issue_close_repo_equals_form(self) -> None:
        self.assert_target(f"gh issue close 849 --repo={REPO}", (REPO, "849"))

    def test_comment_containing_a_number_does_not_hijack(self) -> None:
        """The live bypass found in review: --comment 'closes 219 rows'."""
        self.assert_target(
            f"gh issue close 849 --repo {REPO} --comment 'closes 219 rows'",
            (REPO, "849"),
        )

    def test_quoted_issue_number(self) -> None:
        self.assert_target(f"gh issue close '#849' --repo {REPO}", (REPO, "849"))

    def test_repo_omitted_yields_none(self) -> None:
        self.assert_target("gh issue close 849", (None, "849"))

    def test_flag_value_is_not_mistaken_for_the_issue(self) -> None:
        self.assert_target(
            f"gh issue close --repo {REPO} --comment 219 849", (REPO, "849"),
        )

    def test_api_patch_state_closed(self) -> None:
        self.assert_target(
            f"gh api -X PATCH repos/{REPO}/issues/849 -f state=closed",
            (REPO, "849"),
        )

    def test_api_method_long_flag(self) -> None:
        """--method PATCH is documented gh syntax; first version missed it."""
        self.assert_target(
            f"gh api --method PATCH repos/{REPO}/issues/849 -f state=closed",
            (REPO, "849"),
        )

    def test_api_quoted_state_value(self) -> None:
        self.assert_target(
            f"gh api -X PATCH repos/{REPO}/issues/849 -f state='closed'",
            (REPO, "849"),
        )

    def test_api_flag_order_reversed(self) -> None:
        self.assert_target(
            f"gh api -f state=closed -X PATCH repos/{REPO}/issues/849",
            (REPO, "849"),
        )

    def test_compound_command_returns_every_target(self) -> None:
        got = hook.find_close_targets(
            f"gh issue close 849 --repo {REPO}; gh issue close 850 --repo {REPO}"
        )
        self.assertEqual(got, [(REPO, "849"), (REPO, "850")])

    def test_state_open_is_an_issue_mutation_target(self) -> None:
        """Changed by harmonic-forge#1498 (operator ruling 2026-11-23): corpus
        fixture row `migration` / `gh api -X PATCH .../issues/849 -f
        state=open`, classified `intended`. gh_argv binds every issue REST
        mutation it cannot prove state-free, so the label check runs."""
        self.assertEqual(hook.find_close_targets(
            f"gh api -X PATCH repos/{REPO}/issues/849 -f state=open"), [(REPO, "849")])

    def test_reopen_is_not_a_close(self) -> None:
        self.assertEqual(hook.find_close_targets("gh issue reopen 849"), [])

    def test_unrelated_commands(self) -> None:
        for command in ("git status", "gh issue view 849", "echo gh issue close 1"):
            with self.subTest(command=command):
                self.assertEqual(hook.find_close_targets(command), [])

    def test_unbalanced_quotes_fail_open(self) -> None:
        self.assertIsNone(hook.find_close_targets("gh issue close 849 --comment 'x"))


class DecisionTests(unittest.TestCase):
    CMD = f"gh api -X PATCH repos/{REPO}/issues/849 -f state=closed"

    def _decide(self, command: str, labels: set[str] | None,
                repo: str | None = REPO) -> dict:
        with mock.patch.object(hook, "resolve_repo", return_value=repo), \
             mock.patch.object(hook, "labels_for", return_value=labels), \
             mock.patch("sys.stdin", mock.MagicMock()), \
             mock.patch("json.load", return_value=bash(command)), \
             mock.patch("builtins.print") as printed:
            hook.main()
        return json.loads(printed.call_args[0][0])

    def _denied(self, payload: dict) -> bool:
        return (payload.get("hookSpecificOutput") or {}).get(
            "permissionDecision") == "deny"

    def test_labelled_without_evidence_denies(self) -> None:
        self.assertTrue(self._denied(
            self._decide(self.CMD, labels={hook.LABEL})))

    def test_deny_sets_permission_decision_reason(self) -> None:
        """The model reads permissionDecisionReason, not systemMessage."""
        payload = self._decide(self.CMD, labels={hook.LABEL})
        reason = payload["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn(hook.EXECUTED_LABEL, reason)
        self.assertIn("849", reason)

    def test_labelled_with_evidence_allows(self) -> None:
        self.assertFalse(self._denied(
            self._decide(self.CMD, labels={hook.LABEL, hook.EXECUTED_LABEL})))

    def test_unlabelled_allows(self) -> None:
        self.assertFalse(self._denied(
            self._decide(self.CMD, labels=set())))

    def test_unresolvable_repo_allows(self) -> None:
        self.assertFalse(self._denied(
            self._decide(self.CMD, labels={hook.LABEL}, repo=None)))

    def test_unreadable_labels_fail_open(self) -> None:
        self.assertFalse(self._denied(self._decide(self.CMD, labels=None)))

    def test_abandoned_label_allows(self) -> None:
        self.assertFalse(self._denied(self._decide(
            self.CMD, labels={hook.LABEL, hook.ABANDONED_LABEL})))

    def test_docs_naming_the_label_do_not_grant_it(self) -> None:
        """The whole point of the reforge: prose cannot be a credential."""
        doc = f"Apply {hook.EXECUTED_LABEL} after running it."
        self.assertTrue(self._denied(self._decide(
            f"gh issue close 849 -R {REPO} --comment '{doc}'",
            labels={hook.LABEL})))

    def test_bare_close_is_gated_once_repo_resolves(self) -> None:
        """The most common form must NOT be fail-open any more."""
        self.assertTrue(self._denied(
            self._decide("gh issue close 849", labels={hook.LABEL})))


class ProcessTests(unittest.TestCase):
    def test_non_bash_allowed(self) -> None:
        self.assertEqual(run_hook({"tool_name": "Read"}), {})

    def test_malformed_payload_allowed(self) -> None:
        result = subprocess.run([sys.executable, str(HOOK)], input="not json",
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout or "{}"), {})

    def test_unrelated_bash_allowed(self) -> None:
        self.assertEqual(run_hook(bash("git status")), {})



class ReviewRegressionTests(unittest.TestCase):
    """Every defect the two independent reviews found, as a test."""

    def test_semicolon_inside_quoted_comment_still_parses(self) -> None:
        """Second review: private regex segmenter split inside quotes."""
        self.assertEqual(
            hook.find_close_targets(
                f'gh issue close 849 -R {REPO} --comment "done; verified"'),
            [(REPO, "849")])

    def test_pipe_inside_quoted_comment_still_parses(self) -> None:
        self.assertEqual(
            hook.find_close_targets(
                f'gh issue close 849 -R {REPO} --comment "a | b"'),
            [(REPO, "849")])

    def test_issue_url_form(self) -> None:
        self.assertEqual(
            hook.find_close_targets(
                "gh issue close https://github.com/vitalharmony/hrse/issues/849"),
            [(REPO, "849")])

    def test_env_prefixed_invocation(self) -> None:
        self.assertEqual(
            hook.find_close_targets(f"env gh issue close 849 -R {REPO}"),
            [(REPO, "849")])

    def test_var_assignment_prefixed_invocation(self) -> None:
        self.assertEqual(
            hook.find_close_targets(f"GH_TOKEN=x gh issue close 849 -R {REPO}"),
            [(REPO, "849")])

    def test_main_threads_payload_cwd_into_repo_resolution(self) -> None:
        """A prior round resolved against the hook's own cwd, not the
        command's, and checked the wrong repository's issue #N. Nothing
        else in this suite guards that fix."""
        seen: dict[str, str | None] = {}

        def spy(explicit: str | None, cwd: str | None = None) -> str | None:
            seen["cwd"] = cwd
            return REPO

        payload = {"tool_name": "Bash", "cwd": "/some/where",
                   "tool_input": {"command": "gh issue close 849"}}
        with mock.patch.object(hook, "resolve_repo", side_effect=spy), \
             mock.patch.object(hook, "labels_for", return_value=set()), \
             mock.patch("sys.stdin", mock.MagicMock()), \
             mock.patch("json.load", return_value=payload), \
             mock.patch("builtins.print"):
            hook.main()
        self.assertEqual(seen["cwd"], "/some/where")

    @staticmethod
    def _run(command: str, labels: set[str] | None) -> dict:
        with mock.patch.object(hook, "resolve_repo", return_value=REPO), \
             mock.patch.object(hook, "labels_for", return_value=labels), \
             mock.patch("sys.stdin", mock.MagicMock()), \
             mock.patch("json.load", return_value=bash(command)), \
             mock.patch("builtins.print") as printed:
            hook.main()
        return json.loads(printed.call_args[0][0])

    @staticmethod
    def _is_deny(payload: dict) -> bool:
        return (payload.get("hookSpecificOutput") or {}).get(
            "permissionDecision") == "deny"

    def test_label_matching_is_exact_not_substring(self) -> None:
        cmd = f"gh issue close 849 -R {REPO}"
        for near in ("not-migration-executed", "migration-executed-soon"):
            with self.subTest(label=near):
                self.assertTrue(self._is_deny(
                    self._run(cmd, {hook.LABEL, near})))

    def test_executed_label_allows(self) -> None:
        self.assertFalse(self._is_deny(self._run(
            f"gh issue close 849 -R {REPO}", {hook.LABEL, hook.EXECUTED_LABEL})))

    def test_unreadable_target_does_not_suppress_a_later_deny(self) -> None:
        """continue, not return: the second target must still be judged."""
        calls = iter([None, {hook.LABEL}])
        with mock.patch.object(hook, "resolve_repo", return_value=REPO), \
             mock.patch.object(hook, "labels_for", side_effect=lambda *_: next(calls)), \
             mock.patch("sys.stdin", mock.MagicMock()), \
             mock.patch("json.load", return_value=bash(
                 f"gh issue close 111 -R {REPO}; gh issue close 849 -R {REPO}")), \
             mock.patch("builtins.print") as printed:
            hook.main()
        self.assertTrue(self._is_deny(json.loads(printed.call_args[0][0])))

    def test_labels_for_returns_none_on_failure(self) -> None:
        with mock.patch.object(hook, "_gh", return_value=None):
            self.assertIsNone(hook.labels_for("o/r", "849"))

    def test_labels_for_parses_names(self) -> None:
        with mock.patch.object(hook, "_gh", return_value="tech-debt\ndata-migration\n"):
            self.assertEqual(hook.labels_for("o/r", "849"),
                             {"tech-debt", "data-migration"})


def _no_read(*_args, **_kwargs):
    raise AssertionError("a repo or label read ran")


class AdapterAdoptionTests(unittest.TestCase):
    """harmonic-forge#1498 reforge: one structural cut (a literal gh call or
    not), the adapter's grammar for the call, and no word list."""

    @staticmethod
    def _main(command: str, labels: set[str] | None = None, reads=None) -> dict:
        resolve = {"side_effect": reads} if reads else {"return_value": REPO}
        label = {"side_effect": reads} if reads else {"return_value": labels}
        with mock.patch.object(hook, "resolve_repo", **resolve), \
             mock.patch.object(hook, "labels_for", **label), \
             mock.patch("sys.stdin", mock.MagicMock()), \
             mock.patch("json.load", return_value=bash(command)), \
             mock.patch("builtins.print") as printed:
            hook.main()
        return json.loads(printed.call_args[0][0])

    def test_r0423_commands_naming_gh_are_allowed_without_a_read(self) -> None:
        """Containment-only segments are not-a-close here, as on main; the
        shim is the authority (close_gate.shim_verdict)."""
        for command in ("grep -rn 'gh issue close' tools/", "git commit -m 'route gh api reads'",
                        "git grep -n 'gh api'", "which gh", "$PYTHON -m pytest",
                        "bash -c 'gh issue close 849'", "G=gh; $G issue close 849"):
            with self.subTest(command=command):
                self.assertEqual(hook.find_close_targets(command), [])
                self.assertEqual(self._main(command, reads=_no_read), {})

    def test_an_undecided_literal_gh_close_is_denied_before_any_read(self) -> None:
        for command in ('gh issue close "$N"', "gh issue close 849 --bogus",
                        f"gh -R {REPO} issue close 849", "gh issue close"):
            with self.subTest(command=command):
                self.assertEqual(hook.find_close_targets(command), [(None, "")])
                reason = self._main(command, reads=_no_read)["hookSpecificOutput"]
                self.assertEqual(reason["permissionDecision"], "deny")
                self.assertIn("harmonic-forge#1498", reason["permissionDecisionReason"])

    def test_the_deny_names_the_adapter_reason(self) -> None:
        reasons: list[str] = []
        hook.find_close_targets("gh issue close 849 --bogus", reasons)
        self.assertEqual(reasons, ["unknown flag: --bogus"])
        self.assertIn("unknown flag: --bogus", self._main("gh issue close 849 --bogus", reads=_no_read)[
            "hookSpecificOutput"]["permissionDecisionReason"])

    def test_a_literal_gh_close_whose_comment_mentions_gh_is_still_bound(self) -> None:
        """Never looser than main: the old reader bound this, so the text hook
        must too, although gh_segment's containment calls it unprovable."""
        self.assertEqual(hook.find_close_targets(
            f"gh issue close 849 -R {REPO} --comment 'checked with gh api'"), [(REPO, "849")])

    def test_a_literal_gh_close_behind_a_gh_mentioning_prefix_is_still_a_close(self) -> None:
        """Reforged pass 1 survivor 1: main stripped `NAME=value` and `env`
        before reading gh, so a gh word in an assignment value or a second
        `env` must not turn a literal gh close into not-a-close."""
        for command, want in (
                ("GH_CONFIG_DIR=/home/u/.config/gh gh issue close 849", [(None, "849")]),
                (f"TOOL=gh gh issue close 849 -R {REPO}", [(REPO, "849")]),
                (f"env env gh issue close 849 -R {REPO}", [(REPO, "849")]),
                ("FOO=bar env gh issue close 849", [(None, "849")]),
                (f"GH_CONFIG_DIR=~/.config/gh gh api -X PATCH repos/{REPO}/issues/849 -f state=closed",
                 [(REPO, "849")]),
                ("PAGER=gh gh issue view 849", [])):
            with self.subTest(command=command):
                self.assertEqual(hook.find_close_targets(command), want)

    def test_a_bound_pr_merge_is_not_a_close(self) -> None:
        self.assertEqual(hook.find_close_targets(f"gh pr merge 5 --repo {REPO} --squash"), [])

    def test_inline_gh_repo_binds_the_command(self) -> None:
        """Corpus row `bound_inline_repo` (old-defect): gh reads GH_REPO."""
        self.assertEqual(hook.find_close_targets("GH_REPO=a/b gh issue close 12"), [("a/b", "12")])

    def test_the_hook_process_gh_repo_never_binds_the_command(self) -> None:
        with mock.patch.dict("os.environ", {"GH_REPO": "other/repo"}):
            self.assertEqual(hook.find_close_targets("gh issue close 849"), [(None, "849")])
            self.assertEqual(hook.find_close_targets(f"gh issue close 849 -R {REPO}"), [(REPO, "849")])


class DecideTargetStrictTests(unittest.TestCase):
    """The shim's call (close_gate.shim_verdict): strict reads, strict repo."""

    def test_strict_bare_close_asks_the_real_gh_not_the_checkout_registry(self) -> None:
        import close_gate
        seen: list[tuple] = []

        def fake_gh(*args, cwd=None, repo=None):
            seen.append(args)
            return "o/mig\n" if args[:2] == ("repo", "view") else "data-migration\n"
        with mock.patch.object(hook, "_repo_from_checkout", return_value="o/safe"), \
             mock.patch.object(hook, "_gh", side_effect=fake_gh), close_gate.strict("/bin/true"):
            denied = hook.decide_target(None, "849", "/x", strict_repo=True)
        self.assertIn("o/mig#849", denied)
        self.assertIn(("api", "repos/o/mig/issues/849", "--jq", ".labels[].name"), seen)

    def test_strict_unresolvable_repo_refuses(self) -> None:
        with mock.patch.object(hook, "_gh", return_value=""):
            self.assertIn("could not resolve", hook.decide_target(None, "849", "/x", strict_repo=True))
        with mock.patch.object(hook, "resolve_repo", return_value=None):
            self.assertIsNone(hook.decide_target(None, "849", "/x"))

    def test_strict_failed_label_read_raises(self) -> None:
        import close_gate
        with mock.patch.object(hook, "_run_gh", side_effect=OSError("down")), \
             close_gate.strict("/bin/true"):
            with self.assertRaises(close_gate.GateReadError):
                hook.decide_target("o/r", "849", "/x", strict_repo=True)
        with mock.patch.object(hook, "_run_gh", side_effect=OSError("down")):
            self.assertIsNone(hook.decide_target("o/r", "849", "/x"))


MAIN_TABLE = Path(__file__).resolve().parent / "testdata" / "migration_main_178ba5f.json"
#: sha256 of main's `_strip_invocation_prefix` source at 178ba5f (K7); the git
#: comparison below runs too wherever the commit is present (CI clones shallow).
MAIN_STRIP_SHA256 = "1d85e3f90910881a944c7053c662eee57721c96d79a600318464243e5e141a6b"
GRID_PREFIXES = ("", "env ", "env env ", "1=x ", "env 1=x ", "A=gh ", "GH_CONFIG_DIR=~/.config/gh ",
                 "X=$(true) ")
GRID_WORDS = ("gh", "/usr/bin/gh", "${B:-/usr/bin}/gh", '"$HOME/.local/bin/gh"', '"$BIN/gh"')
GRID_BODIES = ("issue close 849 -R o/r", "api -X PATCH repos/o/r/issues/849 -f state=closed")


def grid() -> list[str]:
    """K1's generated commands; migration_main_table.py freezes main's output for each."""
    return [p + w + " " + b for p in GRID_PREFIXES for w in GRID_WORDS for b in GRID_BODIES]


class StickyWicketTripwireTests(unittest.TestCase):
    """harmonic-forge#1498 sticky-wicket PATCH: recognition is main's predicate
    verbatim (K1, K5, K6, K7); an inline GH_REPO/GH_HOST expansion is undecided
    (K2, K3); an undecided call on the restored path is denied (K4)."""

    def test_k1_recognition_is_never_narrower_than_frozen_main(self) -> None:
        """Generated grid, compared with main's own output (migration_main_table.py
        runs 178ba5f from `git archive`; the commands reach it by being recorded here)."""
        table = json.loads(MAIN_TABLE.read_text(encoding="utf-8"))
        for command in grid():
            with self.subTest(command=command):
                got = hook.find_close_targets(command)
                self.assertIn(command, table, "regenerate: python3 tools/hooks/testdata/migration_main_table.py")
                self.assertTrue(table[command], "the grid row must be a close on main")
                self.assertTrue(got, "main named a target; the hook must not read not-a-close")

    def test_k2_an_inline_gh_repo_expansion_is_denied_before_any_read(self) -> None:
        for command in ("GH_REPO=$OWNER/hrse gh issue close 849", 'GH_REPO="o/$NAME" gh issue close 849',
                        "GH_REPO=`x` gh issue close 849", "GH_HOST=$H gh issue close 849 -R o/r"):
            with self.subTest(command=command):
                self.assertEqual(hook.find_close_targets(command), [(None, "")])
                reason = AdapterAdoptionTests._main(command, reads=_no_read)["hookSpecificOutput"]
                self.assertEqual(reason["permissionDecision"], "deny")

    def test_k3_the_expansion_guard_names_every_env_var_gh_argv_reads(self) -> None:
        import re
        source = (Path(__file__).resolve().parent / "gh_argv.py").read_text(encoding="utf-8")
        read = set(re.findall(r"""env\.get\(\s*["']([^"']+)["']""", source))
        self.assertEqual(read, {"GH_REPO", "GH_HOST"})
        self.assertEqual(set(hook._GH_ENV), read)

    def test_k4_an_undecided_call_on_the_restored_path_is_denied(self) -> None:
        for command in ("env 1=x gh issue close 849 --bogus", '"$BIN/gh" issue close 849 --bogus'):
            with self.subTest(command=command):
                self.assertEqual(hook.find_close_targets(command), [(None, "")])
                reason = AdapterAdoptionTests._main(command, reads=_no_read)["hookSpecificOutput"]
                self.assertEqual(reason["permissionDecision"], "deny")

    def test_k5_a_gha_wrapper_close_is_bound(self) -> None:
        self.assertEqual(hook.find_close_targets("gha vh issue close 849 -R o/r"), [("o/r", "849")])
        self.assertEqual(hook.find_close_targets("gh-as vh gh issue close 849 -R o/r"), [("o/r", "849")])

    def test_k6_r0423_rows_are_allowed(self) -> None:
        for command in ("grep -rn 'gh issue close' tools/", "git commit -m 'gh issue close 849'",
                        "git grep -n 'gh api'", "which gh", "$PYTHON -m pytest", "echo gh issue close 1"):
            with self.subTest(command=command):
                self.assertEqual(hook.find_close_targets(command), [])

    def test_k7_the_recognizer_is_main_verbatim(self) -> None:
        import hashlib
        import inspect
        source = inspect.getsource(hook._strip_invocation_prefix)
        self.assertEqual(hashlib.sha256(source.encode()).hexdigest(), MAIN_STRIP_SHA256)
        self.assertEqual(hook.ENV_ASSIGNMENT.pattern, r"^\w+=")
        main = subprocess.run(["git", "-C", str(HOOK.parent), "show",
                               "178ba5f:tools/hooks/block_data_migration_close.py"],
                              capture_output=True, text=True)
        if main.returncode == 0:
            self.assertIn(source, main.stdout)
            self.assertIn('ENV_ASSIGNMENT = re.compile(r"^\\w+=")', main.stdout)


if __name__ == "__main__":
    unittest.main()
