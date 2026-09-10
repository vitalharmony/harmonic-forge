#!/usr/bin/env python3
"""Tests for report_red_main.py (harmonic-forge#566).

`run=`/`sha=` are injected everywhere -- no test reaches `gh api`.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import report_red_main as m  # noqa: E402


def green_run(cmd):
    joined = " ".join(cmd)
    if "commits/main" in joined:
        return 0, "deadbeef00000000000000000000000000000000\n"
    if "protection" in joined:
        return 0, "null"
    if "check-runs" in joined:
        return 0, json.dumps({"name": "verify", "status": "completed",
                              "conclusion": "success"}) + "\n"
    return 1, "unexpected call"


def red_run(cmd):
    joined = " ".join(cmd)
    if "commits/main" in joined:
        return 0, "cafebabe00000000000000000000000000000000\n"
    if "protection" in joined:
        return 0, "null"
    if "check-runs" in joined:
        return 0, json.dumps({"name": "verify", "status": "completed",
                              "conclusion": "failure"}) + "\n"
    return 1, "unexpected call"


def unreachable_run(cmd):
    return 1, "connection refused"


def unprotected_dependabot_noise_run(cmd):
    """No branch protection (protection -> 404-shaped null), and the only
    check run on the commit is an unrelated failing Dependabot job --
    live-reproduced against vitalharmony/cymagraph-infra's real main tip."""
    joined = " ".join(cmd)
    if "commits/main" in joined:
        return 0, "b581e1f500000000000000000000000000000000\n"
    if "protection" in joined:
        return 0, "null"
    if "check-runs" in joined:
        return 0, json.dumps({"name": "Dependabot", "status": "completed",
                              "conclusion": "failure"}) + "\n"
    return 1, "unexpected call"


class CheckRepoTests(unittest.TestCase):
    def test_a_green_main_reports_nothing(self):
        self.assertIsNone(m.check_repo("vitalharmony/hrse", run=green_run))

    def test_a_red_main_reports_sha_and_detail(self):
        result = m.check_repo("vitalharmony/hrse", run=red_run)
        self.assertIsNotNone(result)
        sha, detail = result
        self.assertTrue(sha.startswith("cafebabe"))
        self.assertIn("verify:failure", detail)

    def test_an_unreachable_api_reports_nothing_not_red(self):
        """AC5: unreachable GitHub must never read as a red main."""
        self.assertIsNone(m.check_repo("vitalharmony/hrse", run=unreachable_run))

    def test_a_missing_sha_reports_nothing(self):
        def no_sha_run(cmd):
            return 1, "not found"
        self.assertIsNone(m.check_repo("vitalharmony/hrse", run=no_sha_run))

    def test_an_unprotected_branch_does_not_read_dependabot_noise_as_red(self):
        """Preclose finding, live-reproduced against cymagraph-infra's real
        main tip: an unprotected branch must not fall back to treating
        EVERY check run as required. Without DEFAULT_REQUIRED_CHECKS, an
        unrelated failing Dependabot run reads as a red main."""
        self.assertIsNone(m.check_repo("vitalharmony/cymagraph-infra",
                                       run=unprotected_dependabot_noise_run))


class CheckAllTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.state_path = Path(self.tmpdir.name) / "main-ci-status.json"

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_ac1_a_red_repo_is_reported(self):
        reports = m.check_all({"vitalharmony/hrse": "H"}, run=red_run,
                              state_path=self.state_path)
        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0]["repo"], "vitalharmony/hrse")

    def test_ac2_the_report_names_the_merge_commit_and_failing_checks(self):
        reports = m.check_all({"vitalharmony/hrse": "H"}, run=red_run,
                              state_path=self.state_path)
        self.assertTrue(reports[0]["sha"].startswith("cafebabe"))
        self.assertIn("verify:failure", reports[0]["detail"])

    def test_a_green_repo_is_never_reported(self):
        reports = m.check_all({"vitalharmony/hrse": "H"}, run=green_run,
                              state_path=self.state_path)
        self.assertEqual(reports, [])

    def test_ac3_an_unchanged_red_does_not_repeat(self):
        first = m.check_all({"vitalharmony/hrse": "H"}, run=red_run,
                            state_path=self.state_path)
        second = m.check_all({"vitalharmony/hrse": "H"}, run=red_run,
                             state_path=self.state_path)
        self.assertEqual(len(first), 1)
        self.assertEqual(second, [])

    def test_ac3_a_new_red_sha_after_a_prior_red_reports_again(self):
        m.check_all({"vitalharmony/hrse": "H"}, run=red_run,
                    state_path=self.state_path)

        def different_red_run(cmd):
            joined = " ".join(cmd)
            if "commits/main" in joined:
                return 0, "1111111100000000000000000000000000000000\n"
            if "protection" in joined:
                return 0, "null"
            if "check-runs" in joined:
                return 0, json.dumps({"name": "verify", "status": "completed",
                                      "conclusion": "failure"}) + "\n"
            return 1, "unexpected"

        second = m.check_all({"vitalharmony/hrse": "H"}, run=different_red_run,
                             state_path=self.state_path)
        self.assertEqual(len(second), 1)
        self.assertTrue(second[0]["sha"].startswith("1111111"))

    def test_ac3_a_red_that_recovers_then_reports_again_if_it_goes_red(self):
        m.check_all({"vitalharmony/hrse": "H"}, run=red_run,
                    state_path=self.state_path)
        recovered = m.check_all({"vitalharmony/hrse": "H"}, run=green_run,
                                state_path=self.state_path)
        self.assertEqual(recovered, [])
        # Same red sha as before, but the repo had gone green in between --
        # a real re-occurrence, not a stale duplicate.
        again = m.check_all({"vitalharmony/hrse": "H"}, run=red_run,
                            state_path=self.state_path)
        self.assertEqual(len(again), 1)

    def test_ac3_a_transient_unknown_between_two_reds_does_not_re_report(self):
        """Preclose finding: a non-terminal reading (unknown/pending/absent)
        must never overwrite the stored 'red' memory -- otherwise the NEXT
        call reads the still-red commit as new and reports it a second
        time, which is exactly the duplicate AC3 forbids."""
        first = m.check_all({"vitalharmony/hrse": "H"}, run=red_run,
                            state_path=self.state_path)
        self.assertEqual(len(first), 1)

        def sha_resolves_but_checks_unknown(cmd):
            joined = " ".join(cmd)
            if "commits/main" in joined:
                return 0, "cafebabe00000000000000000000000000000000\n"
            if "protection" in joined:
                return 0, "null"
            if "check-runs" in joined:
                return 1, "rate limited"  # ci_conclusion() -> "unknown"
            return 1, "unexpected call"

        blip = m.check_all({"vitalharmony/hrse": "H"},
                           run=sha_resolves_but_checks_unknown,
                           state_path=self.state_path)
        self.assertEqual(blip, [])

        third = m.check_all({"vitalharmony/hrse": "H"}, run=red_run,
                            state_path=self.state_path)
        self.assertEqual(third, [], "the transient blip must not have erased "
                                    "the stored red, causing a re-report")

    def test_ac5_an_unreachable_repo_among_others_does_not_break_the_rest(self):
        def mixed_run(cmd):
            return unreachable_run(cmd) if "hrse" in " ".join(cmd) else red_run(cmd)

        reports = m.check_all(
            {"vitalharmony/hrse": "H", "vitalharmony/harmonic-forge": "F"},
            run=mixed_run, state_path=self.state_path)
        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0]["repo"], "vitalharmony/harmonic-forge")

    def test_a_corrupt_state_file_is_treated_as_empty_not_fatal(self):
        self.state_path.write_text("not json", encoding="utf-8")
        reports = m.check_all({"vitalharmony/hrse": "H"}, run=red_run,
                              state_path=self.state_path)
        self.assertEqual(len(reports), 1)


class HandleTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.state_path = Path(self.tmpdir.name) / "main-ci-status.json"

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_ac4_no_report_produces_no_output(self):
        import unittest.mock as mock
        with mock.patch.object(m, "check_all", return_value=[]):
            self.assertEqual(m.handle(state_path=self.state_path), {})

    def test_a_report_produces_sessionstart_additional_context(self):
        import unittest.mock as mock
        with mock.patch.object(m, "check_all", return_value=[
            {"repo": "vitalharmony/hrse", "sha": "cafebabe00", "detail": "verify:failure"}
        ]):
            out = m.handle(state_path=self.state_path)
        self.assertEqual(out["hookSpecificOutput"]["hookEventName"], "SessionStart")
        self.assertIn("vitalharmony/hrse", out["hookSpecificOutput"]["additionalContext"])
        self.assertIn("cafebabe", out["hookSpecificOutput"]["additionalContext"])

    def test_an_internal_exception_never_escapes_handle(self):
        """A SessionStart hook must never fail a session start over this
        feature (AC5's spirit, extended to the whole handler)."""
        import unittest.mock as mock
        with mock.patch.object(m, "check_all", side_effect=RuntimeError("boom")):
            self.assertEqual(m.handle(state_path=self.state_path), {})

    def test_ac4_never_gates_reverts_or_fixes(self):
        """Static check on actual `run([...])` call sites, not prose: every
        list literal passed to `run`/`_run` must be a read-only `gh api`
        call. Scoped to `["...]` argv literals so this doesn't false-positive
        on the module's own docstring discussing merges and reverts in
        prose."""
        import ast

        tree = ast.parse(Path(m.__file__).read_text(encoding="utf-8"))
        mutating = {"POST", "PATCH", "DELETE", "PUT"}
        found_any_call = False
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for arg in node.args:
                if not isinstance(arg, ast.List):
                    continue
                strings = [elt.value for elt in arg.elts
                          if isinstance(elt, ast.Constant) and isinstance(elt.value, str)]
                if strings and strings[0] == "gh":
                    found_any_call = True
                    self.assertFalse(
                        any(s in mutating for s in strings),
                        f"mutating gh invocation found: {strings}")
                    self.assertNotIn("merge", strings)
                    self.assertNotIn("close", strings)
        self.assertTrue(found_any_call, "expected at least one gh argv literal")


if __name__ == "__main__":
    unittest.main()
