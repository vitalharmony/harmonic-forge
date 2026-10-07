"""harmonic-forge#918 -- `ci_plan.py`: the gate's steps, the constraints map, and a
receipt written only by a successful run. `mise` is replaced by a fake
`task_info`; everything else (git, the search, the receipt) is real."""
import contextlib
import io
import os
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import ci_plan  # noqa: E402

TASKS = {
    "check": {"run": ["set -e\npython3 scripts/run_gate_steps.py --task check-steps"],
              "depends": []},
    "check-steps": {"run": ["cd frontend && npm run lint", "mise run docs-check"],
                    "depends": []},
    "ci-check": {"run": "flutter analyze\nflutter test", "depends": ["setup"]},
    "setup": {"run": ["flutter pub get"], "depends": []},
}


def fake_info(task, cwd):
    if task not in TASKS:
        raise RuntimeError(f"mise task {task!r} not found")
    return TASKS[task]


def git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True,
                   env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})


def write(root, rel, text):
    path = Path(root) / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class ExpandTests(unittest.TestCase):
    def test_indirection_is_followed_and_set_e_dropped(self):
        with mock.patch.object(ci_plan, "task_info", fake_info):
            self.assertEqual(ci_plan.expand("check", Path(".")),
                             ["cd frontend && npm run lint", "mise run docs-check"])

    def test_depends_come_first_and_a_string_run_splits_nothing(self):
        with mock.patch.object(ci_plan, "task_info", fake_info):
            self.assertEqual(ci_plan.expand("ci-check", Path(".")),
                             ["flutter pub get", "flutter analyze ; flutter test"])

    def test_a_cycle_terminates(self):
        loop = {"a": {"run": ["echo a"], "depends": ["a"]}}
        with mock.patch.object(ci_plan, "task_info", lambda t, c: loop[t]):
            self.assertEqual(ci_plan.expand("a", Path(".")), ["echo a"])


class ConstraintsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        git(self.root, "init", "-q", "-b", "main")
        write(self.root, "README.md", "x")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "base")
        git(self.root, "update-ref", "refs/remotes/origin/main", "HEAD")

    def tearDown(self):
        self.tmp.cleanup()

    def _commit(self):
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "work")

    def test_python_source_reader_dart_and_php_tests_are_found(self):
        write(self.root, "backend/app/services/foo.py", "x = 1\n")
        write(self.root, "backend/tests/conftest.py", "# guard\n")
        write(self.root, "backend/tests/unit/test_reads.py",
              "text = Path('app/services/foo.py').read_text()\n")
        write(self.root, "app/test/foo_test.dart", "void main() { foo(); }\n")
        write(self.root, "web/tests/FooTest.php", "<?php // exercises foo\n")
        write(self.root, "web/tests/TestCase.php", "<?php\n")
        write(self.root, "backend/tests/unit/test_other.py", "unrelated = 1\n")
        self._commit()
        out = "\n".join(ci_plan.constraints(self.root, ["backend/app/services/foo.py"]))
        self.assertIn("backend/tests/unit/test_reads.py", out)
        self.assertIn("app/test/foo_test.dart", out)
        self.assertIn("web/tests/FooTest.php", out)
        self.assertIn("setup: backend/tests/conftest.py", out)
        self.assertIn("setup: web/tests/TestCase.php", out)
        self.assertNotIn("test_other.py", out)

    def test_a_stoplisted_stem_produces_no_noise(self):
        write(self.root, "src/utils.py", "x = 1\n")
        write(self.root, "tests/test_a.py", "import utils\n")
        self._commit()
        self.assertEqual(ci_plan.constraints(self.root, ["src/utils.py"]), [])

    def test_the_search_is_exact_word_not_substring(self):
        write(self.root, "src/foo.py", "x = 1\n")
        write(self.root, "tests/test_noise.py", "foobar = 1  # food, not the module\n")
        self._commit()
        self.assertEqual(ci_plan.constraints(self.root, ["src/foo.py"]), [])

    def test_a_dotted_module_path_matches_an_import(self):
        write(self.root, "pkg/services/widget_service.py", "x = 1\n")
        write(self.root, "tests/test_imp.py", "from services import widget_service\n")
        self._commit()
        out = "\n".join(ci_plan.constraints(self.root, ["pkg/services/widget_service.py"]))
        self.assertIn("tests/test_imp.py", out)

    def test_changed_files_include_uncommitted_work(self):
        write(self.root, "a.py", "x = 1\n")
        self.assertEqual(ci_plan.changed_files(self.root), [])  # untracked, not in diff
        git(self.root, "add", "a.py")
        self.assertEqual(ci_plan.changed_files(self.root), ["a.py"])


class MainTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        git(self.root, "init", "-q", "-b", "main")
        write(self.root, "README.md", "x")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "base")
        git(self.root, "update-ref", "refs/remotes/origin/main", "HEAD")
        self.receipts = self.root / "receipts"
        self.project = types.SimpleNamespace(
            name="demo", protocol=types.SimpleNamespace(gate_task="check"))
        self.env = {ci_plan.RECEIPT_DIR_ENV: str(self.receipts),
                    "CLAUDE_CODE_SESSION_ID": "sess-1"}

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, resolve=None, env=None, argv=()):
        cwd = os.getcwd()
        os.chdir(self.root)
        out, err = io.StringIO(), io.StringIO()
        try:
            with mock.patch.dict(os.environ, env or self.env), \
                    mock.patch.object(ci_plan, "task_info", fake_info), \
                    mock.patch.object(ci_plan, "resolve_project",
                                      resolve or (lambda c: self.project)), \
                    contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = ci_plan.main(list(argv))
        finally:
            os.chdir(cwd)
        return code, out.getvalue(), err.getvalue()

    def test_success_prints_numbered_steps_and_writes_the_receipt(self):
        code, out, _ = self._run()
        self.assertEqual(code, 0)
        self.assertIn("  1. cd frontend && npm run lint", out)
        self.assertIn("  2. mise run docs-check", out)
        self.assertTrue((self.receipts / "sess-1.json").is_file())

    def test_a_failed_run_writes_no_receipt(self):
        def unresolved(cwd):
            raise ci_plan.ManifestError("not inside any registered checkout")
        code, _, err = self._run(resolve=unresolved)
        self.assertEqual(code, 1)
        self.assertIn("no receipt written", err)
        self.assertFalse(self.receipts.exists())

    def test_a_missing_gate_task_is_a_failure_not_a_receipt(self):
        self.project.protocol.gate_task = "absent"
        code, _, _ = self._run()
        self.assertEqual(code, 1)
        self.assertFalse(self.receipts.exists())

    def test_no_session_id_prints_the_plan_and_writes_nothing(self):
        env = {ci_plan.RECEIPT_DIR_ENV: str(self.receipts), "CLAUDE_CODE_SESSION_ID": ""}
        code, out, _ = self._run(env=env)
        self.assertEqual(code, 0)
        self.assertIn("no receipt written (Codex lane)", out)
        self.assertFalse(self.receipts.exists())

    def test_a_path_argument_joins_the_constraints_map(self):
        write(self.root, "src/widget.py", "x = 1\n")
        write(self.root, "tests/test_widget.py", "import widget\n")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "w")
        git(self.root, "update-ref", "refs/remotes/origin/main", "HEAD")
        _, out, _ = self._run(argv=["src/widget.py"])
        self.assertIn("tests/test_widget.py", out)

    def test_an_unsafe_session_id_writes_no_receipt(self):
        self.assertIsNone(ci_plan.receipt_path("../escape"))
        self.assertIsNone(ci_plan.receipt_path(""))


if __name__ == "__main__":
    unittest.main()
