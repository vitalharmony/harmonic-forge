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

    def _mise(self, task, flags):
        template = textwrap.dedent("""\
            [tasks.__TASK__]
            description = "fake"
            usage = '''
            __DECL__
            '''
            run = "true"
            """)
        decl = "\n".join(f'flag "{f} <v>" help="x"' for f in flags)
        path = self.tmp_path / "mise.toml"
        path.write_text(
            template.replace("__TASK__", task).replace("__DECL__", decl),
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


class LiveScriptParsing(unittest.TestCase):
    def test_live_gh_issue_usage_block_parses(self):
        """Guards the real parse against argparse formatting changes."""
        real = Path(__file__).resolve().parent / "gh_issue.py"
        flags = wp.script_flags(real)
        self.assertTrue({"--repo", "--title", "--tier", "--milestone"} <= flags)


if __name__ == "__main__":
    unittest.main()
