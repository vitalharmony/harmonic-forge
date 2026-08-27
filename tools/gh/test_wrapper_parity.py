#!/usr/bin/env python3
"""Tests for wrapper_parity (harmonic-forge#290).

The acceptance criterion that matters here is bidirectional: it is not enough
that the check passes on a correct wrapper, it must *fail* on a newly
unexposed flag. A parity check that only ever passes is the same class of
defect it exists to catch.

harmonic-forge#293: written with `unittest`, not pytest. This was the only
pytest-dependent file in a repo that declares no dependencies at all, and it
was the single thing standing between 336 passing tests and a zero-install
CI runner.

Run: python3 tools/gh/test_wrapper_parity.py
"""

import importlib.util
import json
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

_MODULE_PATH = Path(__file__).resolve().parent / "wrapper_parity.py"
_spec = importlib.util.spec_from_file_location("wrapper_parity", _MODULE_PATH)
assert _spec and _spec.loader
wp = importlib.util.module_from_spec(_spec)
sys.modules["wrapper_parity"] = wp
_spec.loader.exec_module(wp)


class _TmpDirCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _script(self, flags, name="fake.py"):
        """A real argparse script, so we exercise genuine --help output.

        Dedent the template *before* interpolating: multi-line substituted
        text shifts dedent's common-prefix calculation and silently corrupts
        the generated source.
        """
        template = textwrap.dedent("""\
            import argparse
            def main():
                p = argparse.ArgumentParser(description="fake")
            __ADDS__
                p.parse_args()
            if __name__ == "__main__":
                main()
            """)
        adds = "\n".join(f'    p.add_argument("{f}")' for f in flags)
        path = self.tmp_path / name
        path.write_text(template.replace("__ADDS__", adds), encoding="utf-8")
        return path

    def _mise(self, task, flags, run=None, forward=None):
        """`forward` (defaults to `flags`) controls which declared flags
        the fake `run` body actually passes through -- lets a test seed a
        declared-but-unforwarded flag by passing a `forward` subset."""
        template = textwrap.dedent("""\
            [tasks.__TASK__]
            description = "fake"
            usage = '''
            __DECL__
            '''
            run = __RUN__
            """)
        decl = "\n".join(f'flag "{f} <v>" help="x"' for f in flags)
        if run is None:
            fwd = flags if forward is None else forward
            run = "true " + " ".join(f'{f} "$v"' for f in fwd)
        path = self.tmp_path / "mise.toml"
        path.write_text(
            template.replace("__TASK__", task)
                    .replace("__DECL__", decl)
                    .replace("__RUN__", json.dumps(run)),
            encoding="utf-8",
        )
        return path


class ParityChecks(_TmpDirCase):
    def test_parity_holds_when_wrapper_exposes_everything(self):
        script = self._script(["--alpha", "--beta"])
        mise = self._mise("t", ["--alpha", "--beta"])
        self.assertEqual(wp.check(mise, "t", script, set()), [])

    def test_unexposed_flag_is_reported(self):
        """The failing direction -- the whole point of the check."""
        script = self._script(["--alpha", "--beta"])
        mise = self._mise("t", ["--alpha"])
        self.assertEqual(wp.check(mise, "t", script, set()), ["--beta"])

    def test_declared_omission_is_accepted(self):
        script = self._script(["--alpha", "--beta"])
        mise = self._mise("t", ["--alpha"])
        self.assertEqual(wp.check(mise, "t", script, {"--beta"}), [])

    def test_removing_a_flag_from_allow_list_reopens_the_finding(self):
        """Declaring an omission must not permanently silence it."""
        script = self._script(["--alpha", "--beta"])
        mise = self._mise("t", ["--alpha"])
        self.assertEqual(wp.check(mise, "t", script, {"--beta"}), [])
        self.assertEqual(wp.check(mise, "t", script, set()), ["--beta"])

    def test_allow_missing_accepts_bare_or_dashed_names(self):
        """argparse rejects a value starting with `--`, so both spellings work."""
        script = self._script(["--alpha", "--beta"])
        mise = self._mise("t", ["--alpha"])
        self.assertEqual(wp.check(mise, "t", script, {"beta"}), [])
        self.assertEqual(wp.check(mise, "t", script, {"--beta"}), [])

    def test_help_text_mentions_are_not_counted_as_flags(self):
        """Why the usage block is parsed instead of the options section.

        gh_issue.py's --milestone help cites --tier; --body-file's cites
        --body. Scraping the options section would invent flags and fail
        spuriously.
        """
        src = textwrap.dedent("""\
            import argparse
            def main():
                p = argparse.ArgumentParser(description="fake")
                p.add_argument("--alpha", help="prefer --ghost over --phantom here")
                p.parse_args()
            if __name__ == "__main__":
                main()
            """)
        script = self.tmp_path / "mentions.py"
        script.write_text(src, encoding="utf-8")
        self.assertEqual(wp.script_flags(script), {"--alpha"})

    def test_help_is_never_treated_as_a_wrapped_flag(self):
        script = self._script(["--alpha"])
        self.assertNotIn("--help", wp.script_flags(script))

    def test_missing_task_is_a_run_error_not_a_pass(self):
        """A check that cannot run must not look like a check that passed."""
        script = self._script(["--alpha"])
        mise = self._mise("other", ["--alpha"])
        with self.assertRaises(wp.ParityError):
            wp.check(mise, "t", script, set())

    def test_unrunnable_script_is_a_run_error(self):
        mise = self._mise("t", [])
        with self.assertRaises(wp.ParityError):
            wp.check(mise, "t", self.tmp_path / "does-not-exist.py", set())


class UnforwardedFlagChecks(_TmpDirCase):
    """harmonic-forge#368 item 3: usage-vs-script alone never reads `run`,
    so a declared-but-unforwarded flag reported OK. Known-answer test per
    the issue's own AC2."""

    def test_declared_and_forwarded_flag_is_not_reported(self):
        mise = self._mise("t", ["--alpha", "--beta"])
        self.assertEqual(wp.unforwarded_flags(mise, "t", set()), [])

    def test_declared_but_unforwarded_flag_is_reported(self):
        """The known-answer case: --beta is declared in usage but the run
        body only ever forwards --alpha."""
        mise = self._mise("t", ["--alpha", "--beta"], forward=["--alpha"])
        self.assertEqual(wp.unforwarded_flags(mise, "t", set()), ["--beta"])

    def test_allow_missing_silences_an_unforwarded_flag(self):
        mise = self._mise("t", ["--alpha", "--beta"], forward=["--alpha"])
        self.assertEqual(wp.unforwarded_flags(mise, "t", {"beta"}), [])

    def test_main_cli_reports_unforwarded_flags_and_exits_nonzero(self):
        script = self._script(["--alpha", "--beta"])
        mise = self._mise("t", ["--alpha", "--beta"], forward=["--alpha"])
        import io
        import unittest.mock
        argv = ["wrapper_parity.py", "--mise-toml", str(mise), "--task", "t",
                "--script", str(script)]
        stderr = io.StringIO()
        with unittest.mock.patch.object(sys, "argv", argv), \
             unittest.mock.patch.object(sys, "stderr", stderr):
            rc = wp.main()
        self.assertEqual(rc, 1)
        self.assertIn("never forwards", stderr.getvalue())

    def test_flag_mentioned_only_in_a_comment_is_not_counted_as_forwarded(self):
        """preclose-inspection finding: a flag named in an explanatory
        comment (this repo's own real style, e.g. '# harmonic-forge#290:
        --milestone was missing...') must not read as forwarded -- that
        is precisely backwards for the flags the checker's own incident
        history is about."""
        mise = self._mise(
            "t", ["--alpha", "--beta"],
            run='# harmonic-forge#999: --beta was the missing one here\n'
                'true --alpha "$v"',
        )
        self.assertEqual(wp.unforwarded_flags(mise, "t", set()), ["--beta"])

    def test_indented_comment_line_is_also_stripped(self):
        mise = self._mise(
            "t", ["--alpha", "--beta"],
            run='set -e\n  # note: prefer --beta over --alpha here\ntrue --alpha "$v"',
        )
        self.assertEqual(wp.unforwarded_flags(mise, "t", set()), ["--beta"])


class DiscoverWrapperTasks(_TmpDirCase):
    """harmonic-forge#368 AC1: the set of checked tasks must be discovered
    from mise.toml, not a hand-maintained allow-list of task names."""

    def _write_mise(self, body: str) -> Path:
        path = self.tmp_path / "mise.toml"
        path.write_text(body, encoding="utf-8")
        return path

    def test_discovers_task_invoking_relative_script_path(self):
        script = self.tmp_path / "scripts"
        script.mkdir()
        (script / "real.py").write_text("print('hi')\n", encoding="utf-8")
        mise = self._write_mise(
            '[tasks.t]\nrun = "python3 scripts/real.py \\"$@\\""\n'
        )
        pairs = wp.discover_wrapper_tasks(mise)
        self.assertEqual(pairs, [("t", (self.tmp_path / "scripts" / "real.py").resolve())])

    def test_shell_variable_invocation_is_not_discovered(self):
        """The self-reference guard: `python3 $PARITY ...` has no literal
        `.py` path token and must not register -- this is what stops the
        wrapper-parity task from discovering and looping into itself."""
        mise = self._write_mise(
            '[tasks.wrapper-parity]\nrun = "python3 $PARITY --task x"\n'
        )
        self.assertEqual(wp.discover_wrapper_tasks(mise), [])

    def test_nonexistent_script_path_is_skipped_not_raised(self):
        mise = self._write_mise(
            '[tasks.t]\nrun = "python3 scripts/does_not_exist.py"\n'
        )
        self.assertEqual(wp.discover_wrapper_tasks(mise), [])

    def test_multiple_tasks_each_discovered(self):
        script_dir = self.tmp_path / "scripts"
        script_dir.mkdir()
        (script_dir / "a.py").write_text("", encoding="utf-8")
        (script_dir / "b.py").write_text("", encoding="utf-8")
        mise = self._write_mise(
            '[tasks.one]\nrun = "python3 scripts/a.py"\n'
            '[tasks.two]\nrun = "python3 scripts/b.py"\n'
        )
        pairs = dict(wp.discover_wrapper_tasks(mise))
        self.assertEqual(set(pairs), {"one", "two"})

    def test_cli_discover_mode_prints_pairs_and_exits_zero(self):
        script_dir = self.tmp_path / "scripts"
        script_dir.mkdir()
        (script_dir / "a.py").write_text("", encoding="utf-8")
        mise = self._write_mise('[tasks.one]\nrun = "python3 scripts/a.py"\n')
        import io
        import unittest.mock
        argv = ["wrapper_parity.py", "--mise-toml", str(mise), "--discover"]
        stdout = io.StringIO()
        with unittest.mock.patch.object(sys, "argv", argv), \
             unittest.mock.patch.object(sys, "stdout", stdout):
            rc = wp.main()
        self.assertEqual(rc, 0)
        self.assertIn("one\t", stdout.getvalue())

    def test_precondition_then_real_script_registers_the_last_one(self):
        """preclose-inspection finding, live-reproduced against this repo's
        own containers-up task: a run body that checks a precondition
        script before the real one must register the LAST invocation, not
        the first."""
        script_dir = self.tmp_path / "scripts"
        script_dir.mkdir()
        (script_dir / "precondition.py").write_text("", encoding="utf-8")
        (script_dir / "real.py").write_text("", encoding="utf-8")
        mise = self._write_mise(
            '[tasks.t]\nrun = "python3 scripts/precondition.py\\npython3 scripts/real.py"\n'
        )
        pairs = dict(wp.discover_wrapper_tasks(mise))
        self.assertEqual(pairs["t"], (self.tmp_path / "scripts" / "real.py").resolve())

    def test_venv_interpreter_path_form_is_discovered(self):
        """preclose-inspection finding, live-reproduced against this
        repo's own graph-hygiene task: 'backend/.venv/bin/python
        scripts/x.py' (no literal 'python3' token) must still register."""
        script_dir = self.tmp_path / "scripts"
        script_dir.mkdir()
        (script_dir / "x.py").write_text("", encoding="utf-8")
        venv_dir = self.tmp_path / "backend" / ".venv" / "bin"
        venv_dir.mkdir(parents=True)
        (venv_dir / "python").write_text("", encoding="utf-8")
        mise = self._write_mise(
            '[tasks.t]\nrun = "backend/.venv/bin/python scripts/x.py"\n'
        )
        pairs = dict(wp.discover_wrapper_tasks(mise))
        self.assertEqual(pairs["t"], (self.tmp_path / "scripts" / "x.py").resolve())

    def test_script_path_mentioned_only_in_a_comment_is_not_discovered(self):
        mise = self._write_mise(
            '[tasks.t]\nrun = "# see python3 scripts/ghost.py for context\\ntrue"\n'
        )
        self.assertEqual(wp.discover_wrapper_tasks(mise), [])


class DiscoverExpectFlag(_TmpDirCase):
    """harmonic-forge#368 preclose finding: a broken/empty --discover must
    fail loudly (exit 2), not silently exit 0 having found nothing --
    reproduced live across three consumer repos via the unprotected
    `--discover | while read` pipe shape under `sh -c -o errexit` (no
    pipefail). --expect is the mechanism a consumer uses to assert its
    curated task list actually resolved."""

    def _write_mise(self, body: str) -> Path:
        path = self.tmp_path / "mise.toml"
        path.write_text(body, encoding="utf-8")
        return path

    def _run_discover(self, mise, expect=""):
        import io
        import unittest.mock
        argv = ["wrapper_parity.py", "--mise-toml", str(mise), "--discover"]
        if expect:
            argv += ["--expect", expect]
        stdout, stderr = io.StringIO(), io.StringIO()
        with unittest.mock.patch.object(sys, "argv", argv), \
             unittest.mock.patch.object(sys, "stdout", stdout), \
             unittest.mock.patch.object(sys, "stderr", stderr):
            rc = wp.main()
        return rc, stdout.getvalue(), stderr.getvalue()

    def test_expect_satisfied_exits_zero(self):
        script_dir = self.tmp_path / "scripts"
        script_dir.mkdir()
        (script_dir / "a.py").write_text("", encoding="utf-8")
        mise = self._write_mise('[tasks.one]\nrun = "python3 scripts/a.py"\n')
        rc, out, _ = self._run_discover(mise, expect="one")
        self.assertEqual(rc, 0)
        self.assertIn("one\t", out)

    def test_expect_missing_task_exits_two_not_zero(self):
        """The known-answer case for the swallowed-pipe-failure finding:
        an expected task absent from discovery must be a loud, distinct
        failure, not indistinguishable from a clean empty run."""
        mise = self._write_mise('[tasks.unrelated]\nrun = "true"\n')
        rc, out, err = self._run_discover(mise, expect="gh-new-issue")
        self.assertEqual(rc, 2)
        self.assertIn("gh-new-issue", err)
        self.assertEqual(out, "")

    def test_expect_with_no_flag_at_all_still_exits_zero_on_empty(self):
        """Backward compatible: a consumer that never passes --expect gets
        the old, permissive behavior (exit 0 on nothing found) -- --expect
        is opt-in per the finding's own recommendation that consumers
        adopt it, not a behavior change forced on every caller."""
        mise = self._write_mise('[tasks.unrelated]\nrun = "true"\n')
        rc, out, _ = self._run_discover(mise, expect="")
        self.assertEqual(rc, 0)
        self.assertEqual(out, "")


class LiveScriptParsing(unittest.TestCase):
    def test_live_gh_issue_usage_block_parses(self):
        """Guards the real parse against argparse formatting changes."""
        real = Path(__file__).resolve().parent / "gh_issue.py"
        flags = wp.script_flags(real)
        self.assertTrue({"--repo", "--title", "--tier", "--milestone"} <= flags)


if __name__ == "__main__":
    unittest.main()
