"""harmonic-forge#865: a test process never writes the operator's real belt
candidate store, however the test was launched.

In-process, `unittest` being imported marks a test; a writer a test spawns as a
subprocess is marked by the inherited `HARMONIC_FORGE_TESTING=1`. Every test
here points both `REAL_CANDIDATES_DIR` and `DEFAULT_CANDIDATES_DIR` at a temp
directory standing in for the real store, so nothing touches ~/.claude.
"""
import io
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import belt_candidates as bc  # noqa: E402

GH_DIR = Path(__file__).resolve().parent


class RealStoreFixture(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.real = Path(tmp.name) / "real-store"
        self.real.mkdir()
        for name in ("REAL_CANDIDATES_DIR", "DEFAULT_CANDIDATES_DIR"):
            patcher = mock.patch.object(bc, name, self.real)
            patcher.start()
            self.addCleanup(patcher.stop)
        # Sticky-wicket PATCH: run_tests.main() exports the env marker into its
        # own process, so without this every in-process test would pass on the
        # env half and none would exercise the `unittest`-loaded half.
        env = mock.patch.dict(os.environ, {}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop(bc.TESTING_ENV, None)

    def entries(self):
        return sorted(p.name for p in self.real.glob("*.json"))


class InProcessGuardTests(RealStoreFixture):
    def test_a_record_under_test_writes_nothing_to_the_real_store(self):
        err = io.StringIO()
        with mock.patch.object(sys, "stderr", err):
            bc.record_candidate("vitalharmony/hrse", 1, "handoff", "l1")
        self.assertEqual(self.entries(), [])
        self.assertIn("not writing the real candidate store", err.getvalue())

    def test_an_explicit_other_base_dir_still_writes(self):
        with tempfile.TemporaryDirectory() as other:
            bc.record_candidate("vitalharmony/hrse", 1, "handoff", "l1", base_dir=Path(other))
            self.assertEqual(len(list(Path(other).glob("*.json"))), 1)
        self.assertEqual(self.entries(), [])

    def test_retire_under_test_never_marks_the_real_store(self):
        path = self.real / "vitalharmony__hrse__2.json"
        path.write_text('{"repo": "vitalharmony/hrse", "issue": 2, "kind": "handoff", '
                        '"posted_by": "l1", "posted_at": "2026-10-01T00:00:00Z"}')
        before = path.read_text()
        with mock.patch.object(sys, "stderr", io.StringIO()):
            marked = bc.retire_candidate("vitalharmony/hrse", 2,
                                         read_before=datetime(2026, 10, 2, tzinfo=timezone.utc))
        self.assertFalse(marked)
        self.assertEqual(path.read_text(), before)

    def test_the_age_prune_under_test_leaves_the_real_store_and_reads_normally(self):
        old = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
        path = self.real / "vitalharmony__hrse__3.json"
        path.write_text('{"repo": "vitalharmony/hrse", "issue": 3, "kind": "handoff", '
                        f'"posted_by": "l1", "posted_at": "{old}"}}')
        result = bc.read_candidates(["vitalharmony/hrse"], "l2", queue_kinds={"l2": ("handoff",)},
                                    queue_posters={"l2": ("l1",)}, prune=True)
        self.assertEqual(result, set())
        self.assertTrue(path.exists())

    def test_the_incident_path_post_lane_discussion_main_writes_nothing(self):
        """The 2026-10-02 incident, replayed: `post_lane_discussion.main()`
        driven in-process with `--issue 2095`, as
        test_post_lane_discussion_kinds.py does, with the store at its default."""
        import post_lane_discussion as P

        def fake_comment_body(repo, issue, text):
            return f"https://github.com/{repo}/issues/{issue}#issuecomment-1", 1

        body = ("## Lane 3 Gate Results — H2095 — PASS\n\n**Verdict:** PASS\n"
                "**Head-SHA:** af35ca95\n**Finding:** none.\n**Next:** merge.\n"
                "\n| TC | Verdict |\n|---|---|\n| TC1 | PASS |\n")
        err = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            file = Path(tmp) / "body.md"
            file.write_text(body)
            results = Path(tmp) / "results.json"  # harmonic-forge#893
            results.write_text('{"1": "pass"}')
            argv = ["post_lane_discussion.py", "--issue", "2095", "--file", str(file),
                    "--kind", "gate-result", "--ack-no-pr-required", "replay",
                    "--tc-results", str(results)]
            with mock.patch.object(sys, "argv", argv), \
                 mock.patch.dict("os.environ", {"LANE": "1"}), \
                 mock.patch.object(P, "check_gate_result", return_value=(True, "[GATE] ok")), \
                 mock.patch.object(P, "require_round_approval", return_value=None), \
                 mock.patch.object(P, "comment_body", fake_comment_body), \
                 mock.patch.object(sys, "stderr", err):
                P.main()
        self.assertEqual(self.entries(), [])
        self.assertIn("not writing the real candidate store", err.getvalue())


class EachMarkerAloneRefusesTests(RealStoreFixture):
    """Each marker is pinned by a test that is red when that marker alone is
    removed, and the no-marker case keeps the guard from always refusing."""

    @staticmethod
    def _without_unittest():
        return {k: v for k, v in sys.modules.items() if k.split(".")[0] != "unittest"}

    def test_the_sys_modules_marker_alone_refuses(self):
        self.assertNotIn(bc.TESTING_ENV, os.environ)
        self.assertIn("unittest", sys.modules)
        self.assertTrue(bc._refuses_real_store(self.real))

    def test_the_env_marker_alone_refuses(self):
        with mock.patch.dict(os.environ, {bc.TESTING_ENV: "1"}), \
             mock.patch.dict(sys.modules, self._without_unittest(), clear=True):
            self.assertNotIn("unittest", sys.modules)
            self.assertTrue(bc._refuses_real_store(self.real))

    def test_neither_marker_permits_the_write(self):
        with mock.patch.dict(sys.modules, self._without_unittest(), clear=True):
            self.assertNotIn(bc.TESTING_ENV, os.environ)
            self.assertFalse(bc._refuses_real_store(self.real))


class SubprocessGuardTests(unittest.TestCase):
    """A writer launched as a subprocess has its own `sys.modules`; the flag it
    inherits is what marks it. HOME points the child's real store at a temp dir."""

    SNIPPET = ("import sys; sys.path.insert(0, %r); import belt_candidates as bc; "
               "bc.record_candidate('vitalharmony/hrse', 7, 'handoff', 'l1'); "
               "print('unittest' in sys.modules)")

    def run_child(self, flag: bool):
        with tempfile.TemporaryDirectory() as home:
            env = {k: v for k, v in os.environ.items() if k != bc.TESTING_ENV}
            env["HOME"] = home
            if flag:
                env[bc.TESTING_ENV] = "1"
            out = subprocess.run([sys.executable, "-c", self.SNIPPET % str(GH_DIR)],
                                 env=env, capture_output=True, text=True, check=True)
            store = Path(home) / ".claude" / "state" / "belt" / "candidates"
            return out.stdout.strip(), sorted(p.name for p in store.glob("*.json"))

    def test_a_production_shaped_child_writes(self):
        loaded, entries = self.run_child(flag=False)
        self.assertEqual(loaded, "False")
        self.assertEqual(entries, ["vitalharmony__hrse__7.json"])

    def test_a_flagged_child_writes_nothing(self):
        _, entries = self.run_child(flag=True)
        self.assertEqual(entries, [])


class RunnersSetTheFlagTests(unittest.TestCase):
    """Each runner's real code path exports `HARMONIC_FORGE_TESTING=1` (read
    from the environment it leaves or passes on, never from its source text)."""

    def test_run_tests_main_exports_the_flag(self):
        sys.path.insert(0, str(GH_DIR.parent))
        import run_tests
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(bc.TESTING_ENV, None)
            with mock.patch.object(run_tests, "_test_files", return_value=[]), \
                 mock.patch.object(sys, "stderr", io.StringIO()):
                self.assertEqual(run_tests.main(), 2)  # returns early, after the export
            self.assertEqual(os.environ.get(bc.TESTING_ENV), "1")

    def test_run_lane1_transport_tests_main_exports_the_flag(self):
        import run_lane1_transport_tests as transport
        result = mock.Mock(wasSuccessful=mock.Mock(return_value=True), testsRun=0)
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(bc.TESTING_ENV, None)
            with mock.patch.object(unittest.TestLoader, "discover", return_value=unittest.TestSuite()), \
                 mock.patch.object(unittest.TextTestRunner, "run", return_value=result), \
                 mock.patch.object(sys, "stdout", io.StringIO()):
                transport.main()
            self.assertEqual(os.environ.get(bc.TESTING_ENV), "1")

    def test_kill_check_passes_the_flag_to_the_test_command(self):
        import kill_check
        seen = []

        def fake_run(argv, *, cwd, env, timeout):
            seen.append(env.get(bc.TESTING_ENV))
            return mock.Mock(returncode=1)
        with tempfile.TemporaryDirectory() as parent, \
             mock.patch.object(kill_check, "materialize"), \
             mock.patch.object(kill_check, "run_command", side_effect=fake_run), \
             mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(bc.TESTING_ENV, None)
            kill_check.one_check({"test": ["true"], "patch_text": ""}, sha="a" * 40, origin="o",
                                 repo="vitalharmony/harmonic-forge", parent=Path(parent), timeout=5)
        self.assertTrue(seen)
        self.assertEqual(set(seen), {"1"})


if __name__ == "__main__":
    unittest.main()
