"""harmonic-forge#1381 AC3: closing a blocker reports its open dependents."""
from __future__ import annotations

import io
import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import batch_auth  # noqa: E402
import report_open_dependents_on_close as hook  # noqa: E402

CLOSE = {"tool_name": "Bash",
         "tool_input": {"command": "gh issue close 1290 --repo vitalharmony/harmonic-forge"}}


def _runner(stdout: str = "", returncode: int = 0):
    def run(cmd, **_kw):
        return subprocess.CompletedProcess(cmd, returncode, stdout=stdout, stderr="")
    return run


def _envelope(payload: dict, landed=True, runner=None) -> dict | None:
    """Run main() and return the JSON it printed, or None when it printed nothing."""
    buffer = io.StringIO()
    with mock.patch.object(batch_auth, "action_landed", return_value=landed), \
         mock.patch.object(hook.subprocess, "run", side_effect=runner or _runner()), \
         mock.patch("sys.stdin", io.StringIO(json.dumps(payload))), \
         mock.patch("sys.stdout", buffer):
        hook.main()
    text = buffer.getvalue().strip()
    return json.loads(text) if text else None


class ReportsOpenDependents(unittest.TestCase):
    def test_each_open_dependent_is_one_owed_line_in_additional_context(self):
        out = _envelope(CLOSE, runner=_runner("vitalharmony/harmonic-forge#1291\n"
                                              "vitalharmony/harmonic-forge#1292\n"))
        context = out["hookSpecificOutput"]["additionalContext"].splitlines()
        self.assertEqual(out["hookSpecificOutput"]["hookEventName"], "PostToolUse")
        self.assertEqual(len(context), 2)
        self.assertTrue(all(line.startswith("handoff now owed: ") for line in context))
        self.assertIn("vitalharmony/harmonic-forge#1292", context[1])

    def test_the_read_asks_only_for_open_dependents(self):
        seen = []

        def run(cmd, **_kw):
            seen.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        _envelope(CLOSE, runner=run)
        jq = seen[0][seen[0].index("--jq") + 1]
        self.assertIn('select(.state == "open"', jq)
        self.assertTrue(any(a.endswith("issues/1290/dependencies/blocking") for a in seen[0]))

    def test_a_blocker_with_no_dependents_prints_nothing(self):
        self.assertIsNone(_envelope(CLOSE, runner=_runner("")))

    def test_an_unreadable_read_prints_unknown_never_silence(self):
        out = _envelope(CLOSE, runner=_runner("", returncode=1))
        self.assertIn("dependents UNKNOWN", out["hookSpecificOutput"]["additionalContext"])

    def test_a_close_that_did_not_land_prints_nothing(self):
        self.assertIsNone(_envelope(CLOSE, landed=False,
                                    runner=_runner("vitalharmony/harmonic-forge#1291\n")))

    def test_an_unreadable_landing_check_prints_unknown(self):
        """Preclose pass 1: None means 'could not tell', never 'did not land'."""
        out = _envelope(CLOSE, landed=None, runner=_runner("vitalharmony/harmonic-forge#1291\n"))
        self.assertIn("dependents UNKNOWN", out["hookSpecificOutput"]["additionalContext"])

    def test_a_landing_check_that_raises_prints_unknown_and_keeps_other_closes(self):
        payload = {"tool_name": "Bash", "tool_input": {"command":
                   "gh issue close 1290 --repo vitalharmony/harmonic-forge && "
                   "gh issue close 1291 --repo vitalharmony/harmonic-forge"}}
        calls = iter([True, subprocess.TimeoutExpired("gh", 30)])

        def landed(*_a):
            value = next(calls)
            if isinstance(value, Exception):
                raise value
            return value
        buffer = io.StringIO()
        with mock.patch.object(batch_auth, "action_landed", side_effect=landed), \
             mock.patch.object(hook.subprocess, "run", side_effect=_runner("vitalharmony/harmonic-forge#9\n")), \
             mock.patch("sys.stdin", io.StringIO(json.dumps(payload))), \
             mock.patch("sys.stdout", buffer):
            hook.main()
        context = json.loads(buffer.getvalue())["hookSpecificOutput"]["additionalContext"]
        self.assertIn("handoff now owed: vitalharmony/harmonic-forge#9", context)
        self.assertIn("dependents UNKNOWN", context)

    def test_close_forms_with_no_bound_repo_print_unknown(self):
        """Preclose pass 1 (cross-family): --comment, --repo= and a URL target."""
        for command in ("gh issue close 1290 --repo vitalharmony/harmonic-forge --comment done",
                        "gh issue close 1290 --repo=vitalharmony/harmonic-forge",
                        "gh issue close https://github.com/vitalharmony/harmonic-forge/issues/1290"):
            with self.subTest(command=command):
                payload = {"tool_name": "Bash", "tool_input": {"command": command}}
                out = _envelope(payload, runner=_runner("vitalharmony/harmonic-forge#1291\n"))
                self.assertIsNotNone(out, command)
                context = out["hookSpecificOutput"]["additionalContext"]
                self.assertTrue("dependents UNKNOWN" in context or "handoff now owed" in context)

    def test_a_dependent_still_blocked_by_another_open_issue_is_not_owed(self):
        """Sticky-wicket reforge: the filter keeps only dependents whose blockers
        have all closed. Run the registered jq filter over two dependents."""
        seen = []

        def run(cmd, **_kw):
            seen.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        _envelope(CLOSE, runner=run)
        jq = seen[0][seen[0].index("--jq") + 1]
        sample = json.dumps([
            {"number": 1291, "state": "open", "repository_url": "https://api.github.com/repos/o/r",
             "issue_dependencies_summary": {"blocked_by": 0}},
            {"number": 1292, "state": "open", "repository_url": "https://api.github.com/repos/o/r",
             "issue_dependencies_summary": {"blocked_by": 1}},
            {"number": 1293, "state": "closed", "repository_url": "https://api.github.com/repos/o/r",
             "issue_dependencies_summary": {"blocked_by": 0}}])
        out = subprocess.run(["jq", "-r", jq], input=sample, capture_output=True, text=True, check=True)
        self.assertEqual(out.stdout.split(), ["o/r#1291"])

    def test_a_spent_budget_reports_unknown_for_the_remaining_closes(self):
        """Sticky-wicket reforge: several closes never overrun the hook timeout;
        once the budget is gone each remaining close says so."""
        payload = {"tool_name": "Bash", "tool_input": {"command":
                   "gh issue close 10 --repo vitalharmony/harmonic-forge && "
                   "gh issue close 11 --repo vitalharmony/harmonic-forge"}}
        now = [0.0]

        def landed(*_a):
            now[0] += 25.0  # a slow landing read
            return True

        def run(cmd, **kw):
            now[0] += kw.get("timeout", 0)
            return subprocess.CompletedProcess(cmd, 0, stdout="vitalharmony/harmonic-forge#9\n", stderr="")
        with mock.patch.object(batch_auth, "action_landed", side_effect=landed):
            result = hook.lines(payload, runner=run, clock=lambda: now[0])
        self.assertTrue(any("time budget spent" in line for line in result), result)
        self.assertLessEqual(now[0], hook.HOOK_TIMEOUT)

    def test_no_landing_read_starts_without_room_for_its_timeout(self):
        """A landing read starts only when LANDING_TIMEOUT of budget is left;
        the close it skips reports the spent budget instead of reading."""
        payload = {"tool_name": "Bash", "tool_input": {"command":
                   "gh issue close 10 --repo vitalharmony/harmonic-forge && "
                   "gh issue close 11 --repo vitalharmony/harmonic-forge"}}
        now = [0.0]
        calls = []

        def landed(*args):
            calls.append(args)
            now[0] += hook.BUDGET_SECONDS - hook.LANDING_TIMEOUT + 1
            return True

        with mock.patch.object(batch_auth, "action_landed", side_effect=landed):
            result = hook._closed(payload, deadline=hook.BUDGET_SECONDS, clock=lambda: now[0])
        self.assertEqual(len(calls), 1, calls)
        self.assertIn(("vitalharmony/harmonic-forge", "11", "time budget spent"), result)

    def test_a_close_inside_shell_control_flow_prints_the_catch_all(self):
        payload = {"tool_name": "Bash", "tool_input": {"command":
                   "if true; then gh issue close 1290 --repo vitalharmony/harmonic-forge; fi"}}
        out = _envelope(payload, runner=_runner("vitalharmony/harmonic-forge#1291\n"))
        self.assertIsNotNone(out)
        self.assertIn("run repo_hygiene", out["hookSpecificOutput"]["additionalContext"])

    def test_a_parsed_close_does_not_silence_an_unparsed_one_in_control_flow(self):
        """Pass 1 cluster (correctness): one parsed close must not hide the other."""
        payload = {"tool_name": "Bash", "tool_input": {"command":
                   "gh issue close 1290 --repo vitalharmony/harmonic-forge && "
                   "if true; then gh issue close 1291 --repo vitalharmony/harmonic-forge; fi"}}
        out = _envelope(payload, runner=_runner(""))
        self.assertIsNotNone(out)
        self.assertIn("run repo_hygiene", out["hookSpecificOutput"]["additionalContext"])

    def test_a_parsed_close_does_not_silence_xargs_closes(self):
        """Pass 1 cluster (fail-direction): closes fed through xargs still report."""
        payload = {"tool_name": "Bash", "tool_input": {"command":
                   "gh issue close 5 --repo vitalharmony/hrse && "
                   "echo 6 7 | xargs -n1 gh issue close --repo vitalharmony/hrse"}}
        out = _envelope(payload, runner=_runner(""))
        self.assertIsNotNone(out)
        self.assertIn("run repo_hygiene", out["hookSpecificOutput"]["additionalContext"])

    def _lines(self, command, dependents=""):
        payload = {"tool_name": "Bash", "tool_input": {"command": command}}
        with mock.patch.object(batch_auth, "action_landed", return_value=True):
            return hook.lines(payload, runner=_runner(dependents))

    def test_pass2_a_flag_between_issue_and_close_does_not_cancel_an_eval_close(self):
        """Pass 2 finding 1, verbatim."""
        result = self._lines('gh issue -R vitalharmony/harmonic-forge close 1290 && '
                             'eval "gh issue close 1291 -R vitalharmony/harmonic-forge"',
                             "vitalharmony/harmonic-forge#1292\n")
        self.assertIn(hook.CATCH_ALL, result)
        self.assertIn("handoff now owed: vitalharmony/harmonic-forge#1292 "
                      "(its blocker vitalharmony/harmonic-forge#1290 closed)", result)

    def test_pass2_a_quoted_state_closed_does_not_cancel_an_xargs_close(self):
        """Pass 2 finding 2, verbatim."""
        result = self._lines('gh api -X PATCH repos/o/r/issues/5 -f state="closed"; '
                             'echo 6 | xargs gh issue close -R o/r', "o/r#7\n")
        self.assertIn(hook.CATCH_ALL, result)
        self.assertIn("handoff now owed: o/r#7 (its blocker o/r#5 closed)", result)

    def test_pass2_a_graphql_close_issue_prints_the_catch_all(self):
        """Pass 2 finding 3, verbatim."""
        result = self._lines("gh api graphql -f query='mutation{closeIssue(input:{issueId:\"X\"}){issue{id}}}'")
        self.assertEqual(result, [hook.CATCH_ALL])

    def test_a_parsed_close_the_regex_cannot_see_does_not_cancel_an_unparsed_one(self):
        """Pins per-segment judgment over count subtraction: shell unquoting turns
        `st"ate=closed"` into `state=closed`, so the classifier parses the PATCH
        while the raw-text regex misses it; counts alone (1 mention, 1 parsed)
        would call the xargs close covered."""
        result = self._lines('gh api -X PATCH repos/o/r/issues/5 -f st"ate=closed"; '
                             'echo 6 | xargs gh issue close -R o/r')
        self.assertIn(hook.CATCH_ALL, result)

    def test_a_close_the_segmenter_drops_still_prints_the_catch_all(self):
        """The coverage guard compares the regex with itself."""
        with mock.patch.object(hook, "command_segments", return_value=[]):
            result = self._lines("gh issue close 1291 --repo vitalharmony/harmonic-forge")
        self.assertIn(hook.CATCH_ALL, result)

    def test_a_fully_parsed_command_prints_no_catch_all(self):
        payload = {"tool_name": "Bash", "tool_input": {"command":
                   "gh issue close 10 --repo vitalharmony/harmonic-forge && "
                   "gh issue close 11 --repo vitalharmony/harmonic-forge"}}
        self.assertIsNone(_envelope(payload, runner=_runner("")))

    def test_the_budget_is_checked_before_repo_resolution(self):
        """Pass 1 cross-family finding 6: a slow repo lookup counts against the budget."""
        payload = {"tool_name": "Bash", "tool_input": {"command":
                   "gh issue close 10 && gh issue close 11"}}
        now = [0.0]
        resolves = []

        def resolve(*args):
            resolves.append(args)
            now[0] += hook.BUDGET_SECONDS - hook.LANDING_TIMEOUT + 1
            return "vitalharmony/harmonic-forge"

        import record_tasklist_post
        with mock.patch.object(record_tasklist_post, "_advisory_repo", side_effect=resolve), \
             mock.patch.object(batch_auth, "action_landed", return_value=True):
            result = hook._closed(payload, deadline=hook.BUDGET_SECONDS, clock=lambda: now[0])
        self.assertEqual(len(resolves), 1, resolves)
        self.assertIn(("?", "11", "time budget spent"), result)

    def test_a_slow_repo_lookup_skips_its_own_landing_read(self):
        """The budget is checked again after repo resolution: a lookup that spent
        it never starts a landing read it has no room to finish."""
        payload = {"tool_name": "Bash", "tool_input": {"command": "gh issue close 10"}}
        now = [0.0]

        def resolve(*_a):
            now[0] += hook.BUDGET_SECONDS - hook.LANDING_TIMEOUT + 1
            return "vitalharmony/harmonic-forge"

        import record_tasklist_post
        with mock.patch.object(record_tasklist_post, "_advisory_repo", side_effect=resolve), \
             mock.patch.object(batch_auth, "action_landed", return_value=True) as landed:
            result = hook._closed(payload, deadline=hook.BUDGET_SECONDS, clock=lambda: now[0])
        landed.assert_not_called()
        self.assertEqual(result, [("vitalharmony/harmonic-forge", "10", "time budget spent")])

    def test_the_dependents_read_timeout_shrinks_with_the_budget(self):
        """Pass 1 cross-family finding 7: the read never gets more than what is left."""
        now = [0.0]
        timeouts = []

        def landed(*_a):
            now[0] += hook.BUDGET_SECONDS - 5
            return True

        def run(cmd, **kw):
            timeouts.append(kw.get("timeout"))
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        with mock.patch.object(batch_auth, "action_landed", side_effect=landed), \
             mock.patch.object(hook, "LANDING_TIMEOUT", 0):
            hook.lines(CLOSE, runner=run, clock=lambda: now[0])
        self.assertEqual(len(timeouts), 1, timeouts)
        self.assertLessEqual(timeouts[0], 5)

    def test_an_internal_error_prints_the_catch_all(self):
        buffer = io.StringIO()
        with mock.patch.object(hook, "lines", side_effect=RuntimeError("boom")), \
             mock.patch("sys.stdin", io.StringIO(json.dumps(CLOSE))), \
             mock.patch("sys.stdout", buffer):
            hook.main()
        self.assertIn("run repo_hygiene", buffer.getvalue())

    def test_the_read_paginates(self):
        seen = []

        def run(cmd, **_kw):
            seen.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        _envelope(CLOSE, runner=run)
        self.assertIn("--paginate", seen[0])

    def test_the_rest_close_form_is_covered(self):
        payload = {"tool_name": "Bash", "tool_input": {"command":
                   "gh api -X PATCH repos/vitalharmony/harmonic-forge/issues/1290 "
                   "-f state=closed -f state_reason=completed"}}
        out = _envelope(payload, runner=_runner("vitalharmony/harmonic-forge#1291\n"))
        self.assertIn("1291", out["hookSpecificOutput"]["additionalContext"])

    def test_a_non_close_command_prints_nothing_and_reads_nothing(self):
        payload = {"tool_name": "Bash", "tool_input": {"command": "git status"}}
        self.assertIsNone(_envelope(payload, runner=lambda *a, **k: self.fail("no read")))


if __name__ == "__main__":
    unittest.main()
