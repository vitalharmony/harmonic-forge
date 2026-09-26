#!/usr/bin/env python3
"""Tests for validate_tooling_exception_labelled (harmonic-forge#778 AC1).

#769, #772 and #774 each said Tooling Exception in the body and never
carried the arming label; gh_issue.py catches this at filing time, this
catches it at handoff time -- a handoff posted onto an issue filed some
other way, before this check existed, or filed by hand.
"""
import importlib.util
import subprocess
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).parent


def load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


post = load("l1_post")


def _handoff(next_line: str) -> str:
    return (
        f"## Handoff: [Issue #778 — test]\n\n"
        f"**Scope:** test scope.\n"
        f"**Next:** {next_line}\n"
    )


class ValidateToolingExceptionLabelledTests(unittest.TestCase):
    def _labels_result(self, labels: list[str]) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess([], 0, "\n".join(labels), "")

    def test_no_tooling_exception_mention_never_calls_gh(self):
        with mock.patch.object(post, "run") as run_mock:
            post.validate_tooling_exception_labelled(
                _handoff("Implement #778."), "o/r", 778)
        run_mock.assert_not_called()

    def test_mention_with_the_label_present_passes(self):
        with mock.patch.object(post, "run",
                              return_value=self._labels_result(["bug", "tooling-exception"])):
            post.validate_tooling_exception_labelled(
                _handoff("Tooling Exception — Lane 1 implements."), "o/r", 778)

    def test_mention_without_the_label_refuses(self):
        with mock.patch.object(post, "run",
                              return_value=self._labels_result(["bug"])):
            with self.assertRaises(SystemExit) as caught:
                post.validate_tooling_exception_labelled(
                    _handoff("Tooling Exception — Lane 1 implements."), "o/r", 778)
        self.assertIn("tooling-exception", str(caught.exception))
        self.assertIn("--add-label tooling-exception", str(caught.exception))

    def test_case_insensitive_and_comma_phrasing_is_still_caught(self):
        with mock.patch.object(post, "run",
                              return_value=self._labels_result(["bug"])):
            with self.assertRaises(SystemExit):
                post.validate_tooling_exception_labelled(
                    _handoff("TOOLING, EXCEPTION: no Lane 2 trigger."), "o/r", 778)

    def test_hyphenated_phrasing_is_still_caught(self):
        """Preclose finding: `[\\s,]+` didn't include a hyphen, so
        "Tooling-Exception" -- a real phrasing, not a contrived one --
        bypassed the check entirely."""
        with mock.patch.object(post, "run",
                              return_value=self._labels_result(["bug"])):
            with self.assertRaises(SystemExit):
                post.validate_tooling_exception_labelled(
                    _handoff("Tooling-Exception -- Lane 1 implements."), "o/r", 778)

    def test_unreadable_labels_fails_loud_not_open(self):
        """Unlike the merge-time hook's fail-open style, a handoff-time
        check that cannot verify the label must not silently let a real
        gap through -- there is no downstream backstop as forgiving as
        this one's own next line."""
        with mock.patch.object(post, "run",
                              return_value=subprocess.CompletedProcess([], 1, "", "network down")):
            with self.assertRaises(SystemExit) as caught:
                post.validate_tooling_exception_labelled(
                    _handoff("Tooling Exception — Lane 1 implements."), "o/r", 778)
        self.assertIn("cannot read labels", str(caught.exception))

    def test_a_normal_handoff_with_no_mention_is_unaffected(self):
        with mock.patch.object(post, "run") as run_mock:
            post.validate_tooling_exception_labelled(
                _handoff("Implement #778 once the plan clears."), "o/r", 778)
        run_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
