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
    "scripted": {"run": ["set -euo pipefail\n# a comment, not a command\nnpm run lint\n\n"
                         "docker run --rm \\\n  img cmd\nset -e\n# another\nnpm test"],
                 "depends": []},
    "setup": {"run": ["flutter pub get"], "depends": []},
}


def fake_info(task, cwd):
    if task == "nomise":
        raise FileNotFoundError("[Errno 2] No such file or directory: 'mise'")
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

    def test_depends_come_first_and_a_string_run_splits_per_line(self):
        with mock.patch.object(ci_plan, "task_info", fake_info):
            self.assertEqual(ci_plan.expand("ci-check", Path(".")),
                             ["flutter pub get", "flutter analyze", "flutter test"])

    def test_comments_set_options_and_blank_lines_are_not_steps_and_continuations_join(self):
        with mock.patch.object(ci_plan, "task_info", fake_info):
            self.assertEqual(ci_plan.expand("scripted", Path(".")),
                             ["npm run lint", "docker run --rm img cmd", "npm test"])

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

    def test_a_stoplisted_stem_still_matches_its_dotted_module(self):
        write(self.root, "backend/app/main.py", "x = 1\n")
        write(self.root, "backend/tests/test_boot.py", "from app.main import create\n")
        self._commit()
        out = "\n".join(ci_plan.constraints(self.root, ["backend/app/main.py"]))
        self.assertIn("backend/tests/test_boot.py", out)

    def test_strong_matches_are_never_truncated_only_weak_ones(self):
        write(self.root, "src/widget.py", "x = 1\n")
        for n in range(20):
            write(self.root, f"tests/test_strong_{n:02d}.py", "import src.widget\n")
        for n in range(20):
            write(self.root, f"tests/test_weak_{n:02d}.py", "a widget appears\n")
        write(self.root, "tests/conftest.py", "# setup\n")
        self._commit()
        out = ci_plan.constraints(self.root, ["src/widget.py"])
        shown = [line for line in out if "test_strong_" in line]
        self.assertEqual(len(shown), 20)
        self.assertEqual(len([line for line in out if "test_weak_" in line]), 8)
        self.assertTrue(any("12 more weaker (bare-word)" in line for line in out))
        self.assertIn("    setup: tests/conftest.py", out)

    def test_a_broken_search_is_an_error_not_an_empty_map(self):
        write(self.root, "src/widget.py", "x = 1\n")
        self._commit()
        with mock.patch("ci_plan_constraints.subprocess.run") as run:
            run.return_value = types.SimpleNamespace(returncode=2, stdout="", stderr="boom")
            with self.assertRaises(RuntimeError):
                ci_plan.constraints(self.root, ["src/widget.py"])

    def test_a_dotted_module_path_matches_an_import(self):
        write(self.root, "pkg/services/widget_service.py", "x = 1\n")
        write(self.root, "tests/test_imp.py", "from services import widget_service\n")
        self._commit()
        out = "\n".join(ci_plan.constraints(self.root, ["pkg/services/widget_service.py"]))
        self.assertIn("tests/test_imp.py", out)

    def test_changed_files_include_staged_modified_and_untracked_work(self):
        write(self.root, "new_untracked.py", "x = 1\n")
        write(self.root, "README.md", "changed\n")
        write(self.root, "staged.py", "y = 2\n")
        git(self.root, "add", "staged.py")
        write(self.root, ".gitignore", "ignored.log\n")
        write(self.root, "ignored.log", "noise\n")
        got = ci_plan.changed_files(self.root)
        self.assertEqual(sorted(got), [".gitignore", "README.md", "new_untracked.py", "staged.py"])

    def test_data_file_stems_are_not_searched_and_strong_matches_rank_first(self):
        write(self.root, "projects.toml", "x = 1\n")
        for n in range(15):
            write(self.root, f"tests/test_noise_{n:02d}.py", "the projects we run\n")
        write(self.root, "tests/test_zz_manifest.py", "LIVE = 'projects.toml'\n")
        self._commit()
        out = ci_plan.constraints(self.root, ["projects.toml"])
        self.assertEqual(out[1].split(":")[0].strip(), "tests/test_zz_manifest.py")
        self.assertFalse(any("test_noise" in line for line in out))

    def test_weak_bare_word_matches_are_ranked_below_strong_ones_and_truncated_loudly(self):
        write(self.root, "src/widget.py", "x = 1\n")
        for n in range(15):
            write(self.root, f"tests/test_a_{n:02d}.py", "a widget appears\n")
        write(self.root, "tests/test_zz_strong.py", "text = open('src/widget.py').read()\n")
        self._commit()
        out = ci_plan.constraints(self.root, ["src/widget.py"])
        self.assertEqual(out[1].split(":")[0].strip(), "tests/test_zz_strong.py")
        self.assertTrue(any("more weaker (bare-word)" in line for line in out))


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

    def test_a_run_outside_any_git_checkout_writes_no_receipt(self):
        nongit = Path(tempfile.mkdtemp())
        cwd = os.getcwd()
        os.chdir(nongit)
        err = io.StringIO()
        try:
            with mock.patch.dict(os.environ, self.env), contextlib.redirect_stderr(err):
                code = ci_plan.main([])
        finally:
            os.chdir(cwd)
        self.assertEqual(code, 1)
        self.assertIn("no receipt written", err.getvalue())
        self.assertFalse(self.receipts.exists())

    def test_nothing_that_degrades_the_plan_withholds_the_receipt(self):
        """Only the reading is enforced: an unregistered checkout, a missing
        merge base and a failing search are warnings, never a lockout."""
        def unresolved(cwd):
            raise ci_plan.ManifestError("not inside any registered checkout")
        code, out, _ = self._run(resolve=unresolved)
        self.assertEqual((code, "no registered project" in out), (0, True))
        self.assertTrue((self.receipts / "sess-1.json").is_file())
        (self.receipts / "sess-1.json").unlink()
        with mock.patch.object(ci_plan, "changed_files", side_effect=RuntimeError("no merge base")):
            code, out, _ = self._run()
        self.assertEqual((code, "no merge base" in out), (0, True))
        self.assertTrue((self.receipts / "sess-1.json").is_file())
        (self.receipts / "sess-1.json").unlink()
        with mock.patch.object(ci_plan, "constraints", side_effect=RuntimeError("git grep failed")):
            code, out, _ = self._run()
        self.assertEqual((code, "constraints search failed" in out), (0, True))
        self.assertTrue((self.receipts / "sess-1.json").is_file())

    def test_a_project_with_no_protocol_still_gets_a_receipt(self):
        self.project.protocol = None
        code, out, _ = self._run()
        self.assertEqual((code, "no protocol.gate_task" in out), (0, True))
        self.assertTrue((self.receipts / "sess-1.json").is_file())

    def test_mise_missing_is_a_warning_and_still_gets_a_receipt(self):
        self.project.protocol.gate_task = "nomise"
        code, out, _ = self._run()
        self.assertEqual((code, "could not be read here" in out), (0, True))
        self.assertTrue((self.receipts / "sess-1.json").is_file())

    def test_a_missing_gate_task_is_reported_not_fatal_and_still_gets_a_receipt(self):
        """A lane must not be locked out of its edits because the declared gate task
        does not exist in this checkout: there is nothing to read."""
        self.project.protocol.gate_task = "absent"
        code, out, _ = self._run()
        self.assertEqual(code, 0)
        self.assertIn("could not be read here", out)
        self.assertTrue((self.receipts / "sess-1.json").is_file())

    def test_the_session_flag_overrides_the_environment_id(self):
        code, _, _ = self._run(argv=["--session", "payload-id"])
        self.assertEqual(code, 0)
        self.assertTrue((self.receipts / "payload-id.json").is_file())
        self.assertFalse((self.receipts / "sess-1.json").exists())

    def test_an_unwritable_receipt_dir_is_a_warning_not_a_traceback(self):
        blocker = self.root / "blocker"
        blocker.write_text("a file, so mkdir beneath it fails", encoding="utf-8")
        env = {ci_plan.RECEIPT_DIR_ENV: str(blocker / "sub"), "CLAUDE_CODE_SESSION_ID": "s"}
        code, out, _ = self._run(env=env)
        self.assertEqual(code, 0)
        self.assertIn("WARNING: the receipt could not be written", out)

    def test_a_receipt_certifies_only_the_checkout_and_branch_it_was_written_for(self):
        self._run()
        with mock.patch.dict(os.environ, {ci_plan.RECEIPT_DIR_ENV: str(self.receipts)}):
            self.assertTrue(ci_plan.has_receipt("sess-1", self.root))
            git(self.root, "checkout", "-q", "-b", "other-branch")
            self.assertFalse(ci_plan.has_receipt("sess-1", self.root))
            other = Path(tempfile.mkdtemp())
            git(other, "init", "-q", "-b", "main")
            self.assertFalse(ci_plan.has_receipt("sess-1", other))

    def test_a_path_argument_relative_to_a_subdirectory_is_converted_not_duplicated(self):
        write(self.root, "backend/app/services/foo.py", "x = 1\n")
        write(self.root, "backend/tests/test_foo.py", "from app.services import foo\n")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "w")
        git(self.root, "update-ref", "refs/remotes/origin/main", "HEAD")
        cwd = os.getcwd()
        os.chdir(self.root / "backend")
        try:
            self.assertEqual(ci_plan.root_relative("app/services/foo.py", Path.cwd(), self.root),
                             "backend/app/services/foo.py")
            self.assertEqual(ci_plan.root_relative("backend/app/services/foo.py", Path.cwd(),
                                                   self.root), "backend/app/services/foo.py")
            with mock.patch.dict(os.environ, self.env), \
                    mock.patch.object(ci_plan, "task_info", fake_info), \
                    mock.patch.object(ci_plan, "resolve_project", lambda c: self.project), \
                    contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(ci_plan.main(["app/services/foo.py"]), 0)
        finally:
            os.chdir(cwd)
        text = out.getvalue()
        self.assertEqual(text.count("backend/app/services/foo.py"), 1)
        self.assertNotIn("\n  app/services/foo.py", text)

    def test_run_from_a_subdirectory_maps_the_same_files_as_from_the_root(self):
        write(self.root, "src/widget.py", "x = 1\n")
        write(self.root, "tests/test_widget.py", "import widget\n")
        sub = self.root / "docs" / "deep"
        sub.mkdir(parents=True)
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "w")
        git(self.root, "update-ref", "refs/remotes/origin/main", "HEAD")
        cwd = os.getcwd()
        os.chdir(sub)
        try:
            with mock.patch.dict(os.environ, self.env), \
                    mock.patch.object(ci_plan, "task_info", fake_info), \
                    mock.patch.object(ci_plan, "resolve_project", lambda c: self.project), \
                    contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(ci_plan.main(["src/widget.py"]), 0)
        finally:
            os.chdir(cwd)
        self.assertIn("tests/test_widget.py", out.getvalue())

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


class ReceiptStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.patch = mock.patch.dict(os.environ, {ci_plan.RECEIPT_DIR_ENV: str(self.root / "r")})
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def _checkout(self, name):
        path = self.root / name
        path.mkdir()
        git(path, "init", "-q", "-b", "main")
        git(path, "commit", "-q", "--allow-empty", "-m", "b")
        return path

    def test_concurrent_runs_in_one_session_keep_every_checkout(self):
        checkouts = [self._checkout(f"c{n}") for n in range(8)]
        script = ("import sys; sys.path.insert(0, %r); import ci_plan; from pathlib import Path; "
                  "ci_plan.write_receipt(Path(sys.argv[1]), 'sess')" % str(HERE))
        procs = [subprocess.Popen([sys.executable, "-c", script, str(c)], env=os.environ.copy())
                 for c in checkouts]
        self.assertEqual([p.wait() for p in procs], [0] * 8)
        self.assertEqual([ci_plan.has_receipt("sess", c) for c in checkouts], [True] * 8)

    def test_a_corrupt_receipt_is_replaced_whole_never_left_partial(self):
        a, b = self._checkout("a"), self._checkout("b")
        ci_plan.write_receipt(a, "sess")
        path = ci_plan.receipt_path("sess")
        path.write_text('{"reads": {"/x": ', encoding="utf-8")  # a killed write
        self.assertFalse(ci_plan.has_receipt("sess", a))
        ci_plan.write_receipt(b, "sess")
        self.assertTrue(ci_plan.has_receipt("sess", b))
        self.assertFalse(list(path.parent.glob("*.tmp")))  # no temp left behind

    def test_the_per_issue_worktree_fallback_resolves_the_main_checkout(self):
        """A per-issue worktree is registered nowhere; only the git-common-dir
        fallback finds its project. No patching of resolve_project."""
        main = self._checkout("main-checkout")
        manifest = self.root / "projects.toml"
        manifest.write_text(f'[[project]]\nname = "demo"\nprefix = "D"\npath = "{main}"\n',
                            encoding="utf-8")
        worktree = self.root / "wt-impl"
        git(main, "worktree", "add", "-q", "-b", "tooling/x", str(worktree))
        with mock.patch.dict(os.environ, {"FORGE_PROJECTS_MANIFEST": str(manifest)}):
            self.assertEqual(ci_plan.resolve_project(worktree).name, "demo")
            outside = self._checkout("unregistered")
            with self.assertRaises(ci_plan.ManifestError):
                ci_plan.resolve_project(outside)


if __name__ == "__main__":
    unittest.main()
