"""The Lane 1 pre-post check runs under a private temp root."""
from __future__ import annotations

import ast
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import l1_post as L  # noqa: E402

SOURCE = Path(L.__file__).read_text(encoding="utf-8")


def _fn(name: str) -> ast.FunctionDef:
    return next(n for n in ast.walk(ast.parse(SOURCE))
                if isinstance(n, ast.FunctionDef) and n.name == name)


class PrivateCheckTmpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root, self.env = L._private_check_tmp()
        self.addCleanup(shutil.rmtree, self.root, True)

    def test_root_is_outside_the_system_temp_root(self) -> None:
        system = Path(tempfile.gettempdir()).resolve()
        self.assertNotIn(system, self.root.resolve().parents)
        self.assertNotEqual(system, self.root.resolve())

    def test_env_points_every_temp_variable_at_the_private_root(self) -> None:
        for name in ("TMPDIR", "TMP", "TEMP"):
            self.assertEqual(self.env[name], str(self.root))

    def test_each_call_gets_a_fresh_directory(self) -> None:
        other, _ = L._private_check_tmp()
        self.addCleanup(shutil.rmtree, other, True)
        self.assertNotEqual(other, self.root)


class StrayGitBehaviorTests(unittest.TestCase):
    PROBE = ("import tempfile, pathlib; d = pathlib.Path(tempfile.mkdtemp()); "
             "print(any((p / '.git').exists() for p in d.parents))")

    def _probe(self, env: dict[str, str]) -> str:
        import subprocess
        out = subprocess.run([sys.executable, "-c", self.PROBE], env=env,
                             capture_output=True, text=True, check=True)
        return out.stdout.strip()

    def test_stray_git_in_ambient_temp_root_does_not_reach_the_check(self) -> None:
        import os
        from unittest import mock
        with tempfile.TemporaryDirectory() as ambient:
            (Path(ambient) / ".git").mkdir()
            with mock.patch.dict(os.environ, {"TMPDIR": ambient, "TMP": ambient, "TEMP": ambient}):
                self.assertEqual(self._probe(dict(os.environ)), "True")
                root, env = L._private_check_tmp()
                self.addCleanup(shutil.rmtree, root, True)
                self.assertEqual(self._probe(env), "False")


class StaticChecksWiringTests(unittest.TestCase):
    body = ast.unparse(_fn("static_checks"))

    def test_the_check_runs_with_the_private_env(self) -> None:
        self.assertIn("run('mise', 'run', 'check', cwd=scratch, env=check_env)", self.body)

    def test_the_private_root_is_removed_in_finally(self) -> None:
        tree = _fn("static_checks")
        finals = [ast.unparse(n) for t in ast.walk(tree) if isinstance(t, ast.Try)
                  for n in t.finalbody]
        self.assertTrue(any("shutil.rmtree(check_tmp" in f for f in finals))

    def test_check_tmp_exists_before_the_try(self) -> None:
        self.assertLess(self.body.index("check_tmp: Path | None = None"),
                        self.body.index("try:"))


if __name__ == "__main__":
    unittest.main()
