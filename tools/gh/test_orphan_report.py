"""hrse#2218: the report-only orphan hook never changes a post and says one line per skip."""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import orphan_report as orp  # noqa: E402


def _repo(declared: bool) -> Path:
    root = Path(tempfile.mkdtemp())
    (root / "mise.toml").write_text(f"{orp.DECLARATION if declared else '[tasks.other]'}\nrun = 'true'\n")
    return root


class Runner:
    def __init__(self, root: Path, mise=None):
        self.root, self.mise, self.calls = root, mise, []

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        if argv[0] == "git":
            return subprocess.CompletedProcess(argv, 0, f"{self.root}\n", "")
        if isinstance(self.mise, Exception):
            raise self.mise
        return self.mise


class OrphanReport(unittest.TestCase):
    def run_report(self, runner, capsys_lines=None):
        import contextlib
        import io
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            found = orp.report(runner=runner)
        return found, out.getvalue()

    def test_a_repo_without_the_task_is_skipped_in_one_line_and_mise_never_runs(self):
        runner = Runner(_repo(declared=False))
        found, out = self.run_report(runner)
        self.assertFalse(found)
        self.assertEqual(len(out.strip().splitlines()), 1)
        self.assertFalse([c for c in runner.calls if c[0] == "mise"])

    def test_a_clean_run_says_so(self):
        found, out = self.run_report(Runner(_repo(True), subprocess.CompletedProcess([], 0, "", "")))
        self.assertFalse(found)
        self.assertIn("no orphaned test graphs", out)

    def test_exit_zero_notes_are_printed_not_discarded_and_do_not_report_an_orphan(self):
        note = "test graph with no readable holder, not reaped (operator decides): graph-zz-9\n"
        found, out = self.run_report(Runner(_repo(True), subprocess.CompletedProcess([], 0, "", note)))
        self.assertFalse(found)
        self.assertIn("[test-graph-orphans] test graph with no readable holder, not reaped "
                      "(operator decides): graph-zz-9", out)

    def test_more_than_twenty_lines_say_how_many_earlier_ones_were_cut(self):
        notes = "".join(f"note graph-{i}\n" for i in range(25))
        for code, stdout_err in ((0, notes), (1, notes)):
            with self.subTest(exit=code):
                _, out = self.run_report(Runner(_repo(True), subprocess.CompletedProcess([], code, "", stdout_err)))
                self.assertIn("(5 earlier line(s) not shown", out)
                self.assertIn("note graph-24", out)
                self.assertNotIn("note graph-0\n", out)

    def test_exit_two_is_a_could_not_check_not_an_orphan(self):
        done = subprocess.CompletedProcess([], 2, "", "graph_fixture orphans: ERROR -- no podman\n")
        found, out = self.run_report(Runner(_repo(True), done))
        self.assertFalse(found)
        self.assertIn("ERROR -- no podman", out)

    def test_orphans_are_printed_under_the_heading_and_reported_true(self):
        done = subprocess.CompletedProcess([], 1, "", "orphan test graph (holder dead): hrse-test-master-x\n")
        found, out = self.run_report(Runner(_repo(True), done))
        self.assertTrue(found)
        self.assertIn("[test-graph-orphans] orphan test graph (holder dead): hrse-test-master-x", out)

    def test_every_failure_is_one_line_and_never_raises(self):
        for exc in (FileNotFoundError(2, "no mise", "mise"), subprocess.TimeoutExpired(["mise"], 20),
                    RuntimeError("boom")):
            with self.subTest(exc=type(exc).__name__):
                found, out = self.run_report(Runner(_repo(True), exc))
                self.assertFalse(found)
                self.assertEqual(len(out.strip().splitlines()), 1)
                self.assertIn("skipped", out)

    def test_each_failure_names_its_own_cause(self):
        cases = ((FileNotFoundError(2, "no mise", "mise"), "mise is not installed"),
                 (subprocess.TimeoutExpired(["mise"], 20), "timed out after 20s"),
                 (RuntimeError("boom"), "could not run (RuntimeError)"))
        for exc, wanted in cases:
            with self.subTest(exc=type(exc).__name__):
                _, out = self.run_report(Runner(_repo(True), exc))
                self.assertIn(wanted, out)

    def test_a_non_git_directory_is_skipped(self):
        def not_git(argv, **kw):
            return subprocess.CompletedProcess(argv, 128, "", "fatal: not a git repository")
        found, out = self.run_report(not_git)
        self.assertFalse(found)
        self.assertIn("not in a git checkout", out)

    def test_mise_runs_with_stdin_closed_and_a_timeout_and_never_trusts_a_config(self):
        seen = {}

        def capture(argv, **kw):
            if argv[0] == "mise":
                seen.update(kw)
                seen["argv"] = argv
                return subprocess.CompletedProcess(argv, 0, "", "")
            return subprocess.CompletedProcess(argv, 0, f"{_repo(True)}\n", "")
        self.run_report(capture)
        self.assertEqual(seen["argv"], ["mise", "run", "-q", orp.TASK])
        self.assertIs(seen["stdin"], subprocess.DEVNULL)
        self.assertEqual(seen["timeout"], orp.TIMEOUT_S)
        self.assertNotIn("env", seen, "MISE_YES must never be injected")


class WiredAtTheFinishLines(unittest.TestCase):
    """The hook is called at Lane 1's ready-for-l3 and Lane 2's completion, and nowhere else."""

    def _l2(self, kind):
        import unittest.mock as m
        import l2_post as lp
        argv = ["l2_post.py", "post", "--repo", "vitalharmony/hrse", "--issue", "1921",
                "--kind", kind, "--status", "s", "--change", "c", "--next", "n",
                "--receipts", "[]", "--narrative-file", "/dev/null"]
        with m.patch.object(sys, "argv", argv), \
             m.patch.object(lp, "lock_blocks", return_value=False), \
             m.patch.object(lp, "load_receipts", return_value=[]), \
             m.patch.object(lp, "post", return_value={"comment_id": 1, "url": "u"}), \
             m.patch.object(lp.belt_candidates, "record_candidate"), \
             m.patch.object(lp.orphan_report, "report") as report:
            lp.main()
        return report

    def test_a_completion_reports_once(self):
        self._l2("completion").assert_called_once_with()

    def test_other_l2_kinds_do_not_report(self):
        for kind in ("plan", "blocked", "finding"):
            with self.subTest(kind=kind):
                self._l2(kind).assert_not_called()

    def _l1(self, kind):
        import unittest.mock as m
        import l1_post as l1
        stop = RuntimeError("stop after the hook")
        with m.patch.object(l1.orphan_report, "report") as report, \
             m.patch.object(l1, "refresh_main", side_effect=stop), \
             m.patch.object(l1, "world_checks", side_effect=stop):
            with self.assertRaises(RuntimeError):
                l1.post_kind("vitalharmony/hrse", 1, kind, "body", "a" * 40, "feat/1-x")
        return report

    def test_a_ready_for_l3_reports_before_its_long_check(self):
        self._l1("ready-for-l3").assert_called_once_with()

    def test_other_l1_kinds_do_not_report(self):
        for kind in ("handoff", "sweep", "rework"):
            with self.subTest(kind=kind):
                self._l1(kind).assert_not_called()


if __name__ == "__main__":
    unittest.main()
