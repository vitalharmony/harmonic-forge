#!/usr/bin/env python3
"""harmonic-forge#1500: the private gh close/merge recognizer check.

Every plant runs against a temp git root holding a copy of the two adapter files
plus the planted modules, through the check's `main(root=...)`, never the
worktree.
"""
from __future__ import annotations

import contextlib
import io
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(HERE))

import check_gh_recognizers as check  # noqa: E402


class Root:
    """A temp git root: the two adapters copied in, `files` written and tracked."""

    def __init__(self, files: dict[str, str], adapters=check.ADAPTERS):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name)
        subprocess.run(["git", "init", "-q", str(self.path)], check=True)
        for adapter in adapters:
            (self.path / adapter).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(REPO / adapter, self.path / adapter)
        for rel, body in files.items():
            (self.path / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.path / rel).write_text(body, encoding="utf-8")
        subprocess.run(["git", "-C", str(self.path), "add", "-A"], check=True)

    def run(self) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = check.main([], root=self.path)
        return code, out.getvalue()

    def close(self):
        self.dir.cleanup()


def run_root(files: dict[str, str], **kw) -> tuple[int, str]:
    root = Root(files, **kw)
    try:
        return root.run()
    finally:
        root.close()


def hook(name: str) -> str:
    return f"tools/hooks/{name}.py"


class TC1EachRuleFails(unittest.TestCase):
    """AC2: each plant fails the check and names its path, line and rule."""

    PLANTS = {
        "a": ("G1", "import re\ndef f(t):\n    return t[1:3] == [\"issue\", \"close\"]\n", 3),
        "b": ("G3", "import gh_argv_adapters as A\ndef _parse_issue_close(t):\n    return A.close_target(t)\n", 2),
        "c": ("G1b", "def f(t):\n    return t[2] == \"merge\"\n", 2),
        "d": ("G2", "def f(t):\n    return _has_field(t, \"state=closed\")\n", 2),
        "e": ("G1", "import re\nPAT = re.compile(r\"gh pr merge \\d+\")\n", 2),
    }

    def test_each_plant_fails_naming_path_line_and_rule(self):
        for key, (rule, body, line) in self.PLANTS.items():
            with self.subTest(plant=key):
                code, out = run_root({hook("zz_plant"): body})
                self.assertEqual(code, 1, out)
                self.assertIn(f"VIOLATION tools/hooks/zz_plant.py:{line}:", out)
                self.assertRegex(out, rf"zz_plant\.py:{line}: \S+ \[{rule}\]")


class TC3NeverGreenOnNothing(unittest.TestCase):
    """AC3: an empty scan, a moved scan root and an unparseable file are never clean."""

    def test_an_empty_root_exits_1(self):
        code, out = run_root({})
        self.assertEqual(code, 1, out)
        self.assertNotIn("clean", out)

    def test_a_root_without_the_adapter_exits_1(self):
        code, out = run_root({hook("ok"): "X = 1\n"}, adapters=(check.ADAPTERS[0],))
        self.assertEqual(code, 1, out)
        self.assertIn("scan root moved", out)

    def test_a_syntax_error_exits_2_and_is_not_clean(self):
        code, out = run_root({hook("ok"): "X = 1\n", hook("broken"): "def f(:\n"})
        self.assertEqual(code, 2, out)
        self.assertIn("UNMEASURED tools/hooks/broken.py", out)
        self.assertNotIn("clean", out)


class TC6NoFalsePositives(unittest.TestCase):
    """Prose, docstrings and action labels are never hits."""

    def test_prose_docstrings_and_labels_are_clean(self):
        body = (
            'def f(t):\n'
            '    """tokens[1:3] == ["issue","close"] and t[2] == "merge" and state=closed"""\n'
            '    pr, repo = 5, "o/r"\n'
            '    return f"gh pr merge {pr} --repo {repo} state=closed"\n'
            'def g(record):\n'
            '    return record.get("gh pr merge")\n'
        )
        code, out = run_root({hook("prose"): body})
        self.assertEqual(code, 0, out)
        self.assertIn("clean: 1 files scanned", out)


if __name__ == "__main__":
    unittest.main()
