#!/usr/bin/env python3
"""harmonic-forge#1117: the mise wrappers over `tools/gh/` forward verbatim.

Each test runs the REAL task blocks from this repo's `mise.toml`, copied into
a scratch project with `tools/gh/` pointed at this checkout's scripts, and
calls only `--help` or an unknown flag, so argparse exits before any script
touches GitHub. A wrapper that re-declares flags fails these the way the
drift did in production: mise rejects the argument before the script runs.
"""
from __future__ import annotations

import importlib.util
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
sys.path.insert(0, str(HERE))
import _caller_path  # noqa: E402

_spec = importlib.util.spec_from_file_location("wrapper_parity", HERE / "wrapper_parity.py")
wp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wp)

# l2-run keeps its shape (its command follows a second `--`); see
# [tasks.wrapper-parity] in mise.toml.
EXEMPT = {"l2-run"}
THROWAWAY = "--zz-1117-throwaway"


def verbatim_tasks() -> list[str]:
    return sorted(name for name, script in wp.discover_wrapper_tasks(REPO / "mise.toml")
                  if script.parent.name == "gh" and script.parent.parent.name == "tools"
                  and name not in EXEMPT
                  and name != "wrapper-parity" and "transport-test" not in name)


def _block(name: str) -> str:
    text = (REPO / "mise.toml").read_text()
    start = re.search(r"(?m)^\[tasks\." + re.escape(name) + r"\]\n", text)
    end = re.search(r"(?m)^\[[A-Za-z]", text[start.end():])
    return text[start.start(): start.end() + end.start() if end else len(text)]


@unittest.skipUnless(shutil.which("mise"), "mise not installed")
class Passthrough(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="wrapper-passthrough-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        subprocess.run(["git", "remote", "add", "origin",
                        "https://github.com/vitalharmony/harmonic-forge.git"],
                       cwd=self.root, check=True)
        (self.root / "tools").mkdir()
        (self.root / "tools" / "gh").symlink_to(HERE)
        blocks = "\n".join(_block(name) for name in verbatim_tasks())
        (self.root / "mise.toml").write_text(blocks)
        self.env = {**os.environ, "GH_REPO": "vitalharmony/harmonic-forge",
                    "HARMONIC_FORGE_ROOT": str(REPO),
                    "MISE_TRUSTED_CONFIG_PATHS": str(self.root)}
        for key in ("MISE_TASK_NAME", "MISE_ORIGINAL_CWD", "MISE_PROJECT_ROOT",
                    "MISE_CONFIG_ROOT"):
            self.env.pop(key, None)

    def mise(self, *argv: str) -> subprocess.CompletedProcess:
        return subprocess.run(["mise", "run", "-q", *argv], cwd=self.root, env=self.env,
                              capture_output=True, text=True, timeout=120)

    def test_every_wrapper_is_covered(self):
        self.assertGreaterEqual(len(verbatim_tasks()), 12)

    def test_unknown_script_flag_reaches_script(self):
        # AC1: the script's argparse answers (its usage line, exit 2), so the
        # flag reached it; a per-flag wrapper makes mise refuse it first.
        for task in verbatim_tasks():
            with self.subTest(task=task):
                done = self.mise(task, THROWAWAY, "x")
                self.assertEqual(done.returncode, 2, done.stderr)
                self.assertRegex(done.stderr, r"usage: \w+\.py")
                self.assertNotIn("mise ERROR", done.stderr.split("[")[0])

    def test_wrapper_help_is_script_help(self):
        # AC3: the flag list a session reads is the one that will be parsed.
        for task in verbatim_tasks():
            with self.subTest(task=task):
                done = self.mise(task, "--", "--help")
                self.assertEqual(done.returncode, 0, done.stderr)
                self.assertRegex(done.stdout, r"usage: \w+\.py")

    def test_multi_word_value_arrives_whole(self):
        done = self.mise("post-comment", "--issue", "1", "--file", "f", THROWAWAY, "a b 'c'")
        self.assertIn(f"unrecognized arguments: {THROWAWAY} a b 'c'", done.stderr)

    def test_post_comment_body_refused_via_wrapper(self):
        # harmonic-forge#266, enforced in the script now (operator ruling).
        done = self.mise("post-comment", "--issue", "1", "--body", "x")
        self.assertEqual(done.returncode, 2)
        self.assertIn("--body is refused through `mise run post-comment`", done.stderr)


class CallerPath(unittest.TestCase):
    def test_relative_file_resolves_against_caller(self):
        # AC2: a relative path is read from where the caller ran mise.
        with tempfile.TemporaryDirectory() as caller:
            os.environ["MISE_ORIGINAL_CWD"] = caller
            try:
                self.assertEqual(_caller_path.caller_path(Path("body.md")),
                                 Path(caller) / "body.md")
                self.assertEqual(_caller_path.caller_path("body.md"),
                                 str(Path(caller) / "body.md"))
            finally:
                del os.environ["MISE_ORIGINAL_CWD"]

    def test_unchanged_outside_mise_and_for_absolute_or_stdin(self):
        env = os.environ.pop("MISE_ORIGINAL_CWD", None)
        try:
            self.assertEqual(_caller_path.caller_path(Path("body.md")), Path("body.md"))
        finally:
            if env is not None:
                os.environ["MISE_ORIGINAL_CWD"] = env
        os.environ["MISE_ORIGINAL_CWD"] = "/caller"
        try:
            for value in ("/abs/body.md", "-", None, Path("/abs")):
                self.assertEqual(_caller_path.caller_path(value), value)
        finally:
            if env is None:
                del os.environ["MISE_ORIGINAL_CWD"]
            else:
                os.environ["MISE_ORIGINAL_CWD"] = env

    def test_every_script_resolves_its_file_flags(self):
        expected = {
            "l1_post.py": ("file", "ae_file", "sweep_file"),
            "post_lane_discussion.py": ("file",),
            "post_lane1_issue.py": ("body_file",),
            "post_comment.py": ("file",),
            "gh_issue.py": ("body_file",),
            "l2_post.py": ("narrative_file", "real_run_file", "receipts"),
        }
        for script, names in expected.items():
            with self.subTest(script=script):
                call = "_caller_path.resolve_args(args, " + ", ".join(map(repr, names)) + ")"
                self.assertIn(call, (HERE / script).read_text())


class Shape(unittest.TestCase):
    def test_repo_wrappers_forward_verbatim(self):
        self.assertEqual(wp.shape_violations(REPO / "mise.toml", EXEMPT), [])

    def test_reintroduced_flag_line_fails(self):
        # AC4: a per-flag block on any tools/gh wrapper is reported.
        with tempfile.TemporaryDirectory() as tmp:
            text = (REPO / "mise.toml").read_text().replace(
                '[tasks.l2-snapshot]\n', '[tasks.l2-snapshot]\n', 1)
            text = re.sub(r'(\[tasks\.l2-snapshot\][^\[]*?usage = \'\'\'\n)',
                          r'\1flag "--issue <issue>"\n', text, count=1)
            mt = Path(tmp) / "mise.toml"
            mt.write_text(text)
            (Path(tmp) / "tools").mkdir()
            (Path(tmp) / "tools" / "gh").symlink_to(HERE)
            problems = wp.shape_violations(mt, EXEMPT)
        self.assertTrue(any(p.startswith("l2-snapshot: usage re-declares --issue")
                            for p in problems), problems)

    def test_shape_expect_names_a_missing_task(self):
        # A wrapper that drops out of discovery is a loud failure (exit 2),
        # never "nothing to check".
        done = subprocess.run([sys.executable, str(HERE / "wrapper_parity.py"),
                               "--mise-toml", str(REPO / "mise.toml"), "--shape",
                               "--exempt", "l2-run", "--expect", "l1-post,no-such-task"],
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 2, done.stderr)
        self.assertIn("no-such-task", done.stderr)


if __name__ == "__main__":
    unittest.main()
