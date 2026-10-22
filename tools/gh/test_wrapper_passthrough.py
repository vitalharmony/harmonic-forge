#!/usr/bin/env python3
"""harmonic-forge#1117: the mise wrappers over `tools/gh/` forward verbatim.

Each test runs the REAL task blocks from this repo's `mise.toml`, copied into
a scratch project with `tools/gh/` pointed at this checkout's scripts, and
calls only `--help` or an unknown flag, so argparse exits before any script
touches GitHub. A wrapper that re-declares flags fails these the way the
drift did in production: mise rejects the argument before the script runs.
"""
from __future__ import annotations

import argparse
import ast
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
    def setUp(self):
        self._saved = {k: os.environ.pop(k, None) for k in ("HF_CALLER_CWD", "MISE_ORIGINAL_CWD")}
        self.addCleanup(self._restore)

    def _restore(self):
        for k, v in self._saved.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v

    def test_relative_file_resolves_against_caller(self):
        # AC2: a relative path is read from where the caller ran mise.
        with tempfile.TemporaryDirectory() as caller:
            os.environ["HF_CALLER_CWD"] = caller
            self.assertEqual(_caller_path.caller_path(Path("body.md")), Path(caller) / "body.md")
            self.assertEqual(_caller_path.caller_path("body.md"), str(Path(caller) / "body.md"))

    def test_unchanged_without_the_variable_and_for_absolute_or_stdin(self):
        self.assertEqual(_caller_path.caller_path(Path("body.md")), Path("body.md"))
        os.environ["HF_CALLER_CWD"] = "/caller"
        for value in ("/abs/body.md", "-", None, Path("/abs")):
            self.assertEqual(_caller_path.caller_path(value), value)

    def test_inherited_mise_original_cwd_is_ignored(self):
        # Preclose survivor: every process inside any mise task inherits
        # MISE_ORIGINAL_CWD, so it must never decide where a file is read.
        os.environ["MISE_ORIGINAL_CWD"] = "/some/subdir"
        args = argparse.Namespace(file="body.md")
        _caller_path.resolve_args(args, "file")
        self.assertEqual(args.file, "body.md")

    def test_resolve_args_consumes_the_variable(self):
        # One-shot: a child the script starts must not inherit it.
        os.environ["HF_CALLER_CWD"] = "/caller"
        args = argparse.Namespace(file="body.md", files=["a", "/b"])
        _caller_path.resolve_args(args, "file", "files")
        self.assertEqual(args.file, "/caller/body.md")
        self.assertEqual(args.files, ["/caller/a", "/b"])
        self.assertNotIn("HF_CALLER_CWD", os.environ)


class RepoOverride(unittest.TestCase):
    """Preclose survivor: a wrapper pins --repo "$GH_REPO" first, and argparse
    keeps the last value, so a caller's own --repo silently retargeted the
    write. The script refuses it under the wrapper."""

    def _refused(self, task_env, argv):
        parser = argparse.ArgumentParser()
        saved = os.environ.pop("MISE_TASK_NAME", None)
        try:
            if task_env:
                os.environ["MISE_TASK_NAME"] = task_env
            with self.assertRaises(SystemExit) as caught:
                _caller_path.refuse_repo_override(parser, "post-comment", argv)
                raise SystemExit(0)
            return caught.exception.code != 0
        finally:
            os.environ.pop("MISE_TASK_NAME", None)
            if saved is not None:
                os.environ["MISE_TASK_NAME"] = saved

    def test_second_repo_refused_under_the_wrapper(self):
        for argv in (["--repo", "a/b", "--issue", "1", "--repo", "c/d"],
                     ["--repo", "a/b", "--repo=c/d"],
                     ["--repo", "a/b", "--rep", "c/d"]):
            with self.subTest(argv=argv):
                self.assertTrue(self._refused("post-comment", argv))

    def test_single_repo_and_direct_calls_allowed(self):
        self.assertFalse(self._refused("post-comment", ["--repo", "a/b", "--issue", "1"]))
        self.assertFalse(self._refused(None, ["--repo", "a/b", "--repo", "c/d"]))

    def test_pinned_scripts_call_the_refusal(self):
        for script, task in (("post_comment.py", "post-comment"), ("gh_issue.py", "gh-new-issue")):
            with self.subTest(script=script):
                self.assertIn(f"refuse_repo_override(parser, {task!r})", (HERE / script).read_text())
        self.assertIn("refuse_repo_override(parser, task)", (HERE / "l2_post.py").read_text())

    def test_wrapper_sets_the_one_shot_variable(self):
        # Every script that calls resolve_args, read from the source rather than
        # a hand-kept list (post-verdict finding: kill_check and preclose_check
        # resolved nothing because no list named them).
        text = (REPO / "mise.toml").read_text()
        callers = sorted(p.stem for p in HERE.glob("*.py")
                         if not p.name.startswith("test_") and p.name != "_caller_path.py"
                         and "_caller_path.resolve_args(" in p.read_text())
        self.assertGreaterEqual(len(callers), 8)
        for script in callers:
            with self.subTest(script=script):
                self.assertRegex(text, r"""eval HF_CALLER_CWD='"\$MISE_ORIGINAL_CWD"' python3 \S*tools/gh/"""
                                 + script + r"\.py")

    def test_every_path_argument_is_resolved(self):
        # Pass-2 survivor: resolve_args covered only --file in some scripts, so
        # other file-path arguments the verbatim wrapper newly reaches
        # (--tc-classes, --tc-results) were read relative to the tools
        # worktree. Read every wrapper-reached script's parser, not a
        # hand-kept list: any type=Path argument, or one whose flag names a
        # file or receipts, must be in that script's resolve_args call.
        # This checkout's copy of each script a verbatim wrapper runs
        # (discovery expands the forge-root prefix to the main checkout).
        tasks = set(verbatim_tasks())
        scripts = {HERE / script.name
                   for name, script in wp.discover_wrapper_tasks(REPO / "mise.toml")
                   if name in tasks}
        self.assertGreaterEqual(len(scripts), 6)
        for script in sorted(scripts):
            tree = ast.parse(script.read_text())
            wanted, resolved = set(), set()
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                attr = getattr(node.func, "attr", "")
                if attr == "add_argument":
                    flags = [a.value for a in node.args if isinstance(a, ast.Constant)
                             and isinstance(a.value, str) and a.value.startswith("--")]
                    if not flags:
                        continue
                    kw = {k.arg: k.value for k in node.keywords}
                    typ = kw.get("type")
                    is_path = getattr(typ, "id", None) == "Path" or any(
                        re.search(r"file|receipts|findings|envelope|checks|brief", f) for f in flags)
                    if is_path:
                        dest = kw.get("dest")
                        wanted.add(dest.value if isinstance(dest, ast.Constant)
                                   else flags[0][2:].replace("-", "_"))
                elif attr == "resolve_args":
                    resolved |= {a.value for a in node.args[1:] if isinstance(a, ast.Constant)}
            with self.subTest(script=script.name):
                self.assertEqual(sorted(wanted - resolved), [],
                                 f"{script.name}: path arguments not passed to resolve_args")


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

    def test_short_alias_flag_line_fails(self):
        # Pass-2 survivor: `flag "-d --dry-run"` used to slip past --shape, and
        # mise then consumed --dry-run, so the caller's dry run became a real post.
        with tempfile.TemporaryDirectory() as tmp:
            text = re.sub(r'(\[tasks\.l1-post\][^\[]*?usage = \'\'\'\n)',
                          r'\1flag "-d --dry-run"\n', (REPO / "mise.toml").read_text(), count=1)
            mt = Path(tmp) / "mise.toml"
            mt.write_text(text)
            (Path(tmp) / "tools").mkdir()
            (Path(tmp) / "tools" / "gh").symlink_to(HERE)
            problems = wp.shape_violations(mt, EXEMPT)
        self.assertTrue(any(p.startswith("l1-post: usage re-declares -d --dry-run")
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
