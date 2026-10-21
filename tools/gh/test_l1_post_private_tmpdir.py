"""The Lane 1 pre-post check runs under a private temp root."""
from __future__ import annotations

import ast
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import l1_post as L  # noqa: E402
from run_tests import REAL_TMPDIR_ENV, real_tmpdir  # noqa: E402

SOURCE = Path(L.__file__).read_text(encoding="utf-8")


def _fn(name: str) -> ast.FunctionDef:
    return next(n for n in ast.walk(ast.parse(SOURCE))
                if isinstance(n, ast.FunctionDef) and n.name == name)


def _system_temp_root() -> str:
    # harmonic-forge#1114: the real tmpfs root, not the contained per-run root.
    return real_tmpdir()


def _assert_outside(root: Path, system: str) -> None:
    """Raise AssertionError if root is, or is under, the temp root ``system``."""
    ref = Path(system).resolve()
    resolved = root.resolve()
    assert ref != resolved, f"{root} is the temp root {ref}"
    assert ref not in resolved.parents, f"{root} is under the temp root {ref}"


class PrivateCheckTmpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root, self.env = L._private_check_tmp()
        self.addCleanup(shutil.rmtree, self.root, True)

    def test_root_is_outside_the_system_temp_root(self) -> None:
        _assert_outside(self.root, _system_temp_root())

    def test_env_points_every_temp_variable_at_the_private_root(self) -> None:
        for name in ("TMPDIR", "TMP", "TEMP"):
            self.assertEqual(self.env[name], str(self.root))

    def test_each_call_gets_a_fresh_directory(self) -> None:
        other, _ = L._private_check_tmp()
        self.addCleanup(shutil.rmtree, other, True)
        self.assertNotEqual(other, self.root)


class RealTmpdirReferenceTests(unittest.TestCase):
    """harmonic-forge#1114: under a contained run gettempdir() is the per-run root."""

    def test_reference_is_the_recorded_real_root_not_the_contained_one(self) -> None:
        import os
        from unittest import mock
        with tempfile.TemporaryDirectory() as real, tempfile.TemporaryDirectory() as contained:
            with mock.patch.dict(os.environ, {REAL_TMPDIR_ENV: real}), \
                    mock.patch.object(tempfile, "tempdir", contained):
                self.assertEqual(real_tmpdir(), real)

    def test_a_check_root_under_the_real_root_is_caught_in_a_contained_run(self) -> None:
        import os
        from unittest import mock
        with tempfile.TemporaryDirectory() as real:
            contained = Path(real) / "hf-test-run-x"
            contained.mkdir()
            with mock.patch.dict(os.environ, {REAL_TMPDIR_ENV: real}), \
                    mock.patch.object(tempfile, "tempdir", str(contained)):
                root, _ = L._private_check_tmp(Path(real) / "l1-post-check")
                self.addCleanup(shutil.rmtree, root, True)
                # The recorded real root catches it; the contained root does not.
                with self.assertRaises(AssertionError):
                    _assert_outside(root, _system_temp_root())
                _assert_outside(root, tempfile.gettempdir())

    def test_falls_back_to_gettempdir_when_unset(self) -> None:
        import os
        from unittest import mock
        with mock.patch.dict(os.environ):
            os.environ.pop(REAL_TMPDIR_ENV, None)
            self.assertEqual(real_tmpdir(), tempfile.gettempdir())


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
        # harmonic-forge#949: the private root lives inside the scratch holder,
        # which the holder's own exit removes (and the next run reaps if killed).
        self.assertTrue(any("holder_cm.__exit__" in f for f in finals))

    def test_the_private_root_is_inside_the_scratch_holder(self) -> None:
        self.assertIn("_private_check_tmp(holder)", self.body)


if __name__ == "__main__":
    unittest.main()
