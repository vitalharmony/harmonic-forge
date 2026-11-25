"""harmonic-forge#894 slice 6 step 2: gate_ci's S0 reader path (C3) and the
`pending` predicate for a required job not yet created (C4)."""
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "onboard"))
import gate_ci  # noqa: E402
import manifest_identity  # noqa: E402

REPO = "acme/widget"
SHA = "a" * 40
RUNS = f"repos/{REPO}/actions/runs?head_sha={SHA}&per_page=100"


def jobs_path(run_id, flt="latest"):
    return f"repos/{REPO}/actions/runs/{run_id}/jobs?per_page=100&filter={flt}"


class Reader:
    """A stub S0 reader: path -> (status, body), recording every call."""

    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def __call__(self, path):
        self.calls.append(path)
        status, payload = self.pages.get(path, (404, None))
        return status, (None if payload is None else json.dumps(payload))


def wf(run_id, status="completed", event="pull_request", conclusion="success", name="CI",
       path=".github/workflows/ci.yml"):
    return {"id": run_id, "name": name, "status": status, "event": event,
            "conclusion": conclusion, "path": path}


def job(name, status="completed", conclusion="success"):
    return {"name": name, "status": status, "conclusion": conclusion}


def pages(runs, jobs_by_run):
    out = {RUNS: (200, {"total_count": len(runs), "workflow_runs": runs})}
    for run_id, jobs in jobs_by_run.items():
        out[jobs_path(run_id)] = (200, {"total_count": len(jobs), "jobs": jobs})
    return out


class PendingPredicate(unittest.TestCase):
    """C4: each case is its own kill check."""

    def judge(self, runs, jobs_by_run):
        return gate_ci.ci_conclusion(REPO, SHA, required={"verify"},
                                     reader=Reader(pages(runs, jobs_by_run)))

    def test_an_unfinished_pull_request_run_with_no_verify_job_reads_pending(self):
        state, detail = self.judge([wf(1, status="in_progress", conclusion=None)],
                                   {1: [job("frontend", status="in_progress", conclusion=None)]})
        self.assertEqual(state, "pending", detail)
        self.assertIn("verify", detail)

    def test_a_completed_run_set_without_verify_reads_absent(self):
        state, _ = self.judge([wf(1)], {1: [job("frontend")]})
        self.assertEqual(state, "absent")

    def test_only_a_pull_request_target_run_in_flight_reads_absent(self):
        state, _ = self.judge(
            [wf(1), wf(2, status="in_progress", event="pull_request_target",
                       conclusion=None, name="ui-approval")],
            {1: [job("frontend")], 2: [job("ui-approval", status="in_progress", conclusion=None)]})
        self.assertEqual(state, "absent")

    def test_a_red_job_with_verify_not_yet_created_is_not_pending(self):
        state, _ = self.judge([wf(1, status="in_progress", conclusion=None)],
                              {1: [job("frontend", conclusion="failure"),
                                   job("backend", status="in_progress", conclusion=None)]})
        self.assertNotEqual(state, "pending")

    def test_an_unrelated_in_flight_workflow_never_makes_verify_pending(self):
        """PATCH A4 (C4 condition 2): only verify's producer, ci.yml, counts."""
        state, _ = self.judge([wf(1), wf(2, status="in_progress", conclusion=None,
                                         name="docs", path=".github/workflows/docs.yml")],
                              {1: [job("frontend")], 2: [job("build", status="in_progress",
                                                             conclusion=None)]})
        self.assertEqual(state, "absent")

    def test_a_jobless_unfinished_run_reads_pending(self):
        state, _ = self.judge([wf(1, status="queued", conclusion=None)], {1: []})
        self.assertEqual(state, "pending")

    def test_the_default_reader_carries_the_run_event_too(self):
        def run(cmd):
            path = cmd[3] if cmd[2] == "--paginate" else cmd[2]
            if "actions/runs?" in path:
                self.assertIn("event", cmd[-1])
                return 0, json.dumps(wf(1, status="in_progress", conclusion=None)) + "\n"
            return 0, json.dumps(job("frontend", status="in_progress", conclusion=None)) + "\n"
        self.assertEqual(gate_ci.ci_conclusion(REPO, SHA, run=run, required={"verify"})[0],
                         "pending")


class S0Reader(unittest.TestCase):
    """C3: every read through the reader; anything but 200/304-with-body is unknown."""

    def test_a_green_snapshot_reads_green_through_the_reader(self):
        reader = Reader(pages([wf(1)], {1: [job("verify")]}))
        self.assertEqual(gate_ci.ci_conclusion(REPO, SHA, required={"verify"},
                                               reader=reader)[0], "green")
        self.assertEqual(reader.calls, [RUNS, jobs_path(1)])

    def test_a_404_or_403_reads_unknown_never_absent(self):
        # A real error answer carries a JSON body; an empty run list in it must
        # never read as `absent` (C3: only 200, or 304 with a body, counts).
        empty = {"total_count": 0, "workflow_runs": []}
        for status in (404, 403, 500, None):
            reader = Reader({RUNS: (status, empty)})
            self.assertEqual(gate_ci.ci_conclusion(REPO, SHA, required={"verify"},
                                                   reader=reader)[0], "unknown", status)

    def test_a_304_without_a_body_reads_unknown(self):
        reader = Reader({RUNS: (304, None)})
        self.assertEqual(gate_ci.ci_conclusion(REPO, SHA, reader=reader)[0], "unknown")

    def test_a_page_short_of_its_total_count_reads_unknown(self):
        p = pages([wf(1)], {1: [job("verify")]})
        p[RUNS] = (200, {"total_count": 101, "workflow_runs": [wf(1)]})
        self.assertEqual(gate_ci.ci_conclusion(REPO, SHA, reader=Reader(p))[0], "unknown")
        p = pages([wf(1)], {1: [job("verify")]})
        p[jobs_path(1)] = (200, {"total_count": 2, "jobs": [job("verify")]})
        self.assertEqual(gate_ci.ci_conclusion(REPO, SHA, reader=Reader(p))[0], "unknown")

    def test_a_reader_exception_reads_unknown(self):
        def boom(path):
            raise RuntimeError("network")
        self.assertEqual(gate_ci.ci_conclusion(REPO, SHA, reader=boom)[0], "unknown")

    def test_a_manifest_error_reads_unknown(self):
        reader = gate_ci.s0_reader(REPO)
        with mock.patch.object(manifest_identity, "account_for",
                               side_effect=manifest_identity.ManifestError("nope")):
            self.assertEqual(reader(RUNS), (None, None))
            self.assertEqual(gate_ci.ci_conclusion(REPO, SHA, reader=reader)[0], "unknown")

    def test_status_records_go_through_the_reader(self):
        p = pages([wf(1)], {1: [job("frontend")]})
        p[f"repos/{REPO}/commits/{SHA}/statuses?per_page=100"] = (200, [
            {"context": "verify", "state": "success",
             "creator": {"login": gate_ci.STATUS_CREATOR}, "updated_at": "2026-11-24T00:00:00Z"}])
        reader = Reader(p)
        self.assertEqual(gate_ci.authorizing_conclusion(REPO, SHA, {"verify"},
                                                        reader=reader)[0], "green")
        self.assertIn(f"repos/{REPO}/commits/{SHA}/statuses?per_page=100", reader.calls)

    def test_a_full_status_page_reads_unknown(self):
        p = pages([wf(1)], {1: [job("frontend")]})
        p[f"repos/{REPO}/commits/{SHA}/statuses?per_page=100"] = (200, [
            {"context": f"c{i}", "state": "success", "creator": {"login": "x"}}
            for i in range(100)])
        self.assertEqual(gate_ci.authorizing_conclusion(REPO, SHA, {"verify"},
                                                        reader=Reader(p))[0], "unknown")

    def test_every_attempt_read_goes_through_the_reader(self):
        p = {RUNS: (200, {"total_count": 1, "workflow_runs": [wf(1)]}),
             jobs_path(1, "all"): (200, {"total_count": 1,
                                         "jobs": [job("verify", conclusion="skipped")]})}
        reader = Reader(p)
        self.assertTrue(gate_ci._required_checks_all_skipped(REPO, SHA, {"verify"},
                                                             reader=reader))
        self.assertEqual(reader.calls, [RUNS, jobs_path(1, "all")])
        p[RUNS] = (403, None)
        self.assertFalse(gate_ci._required_checks_all_skipped(REPO, SHA, {"verify"},
                                                              reader=Reader(p)))


class NoPrAckRelease(unittest.TestCase):
    """harmonic-forge#894 PATCH A1: the one no-PR rule needs an eligible state,
    main's tip, and no open PR containing the SHA; a failed lookup refuses."""

    def rule(self, state="absent", tip=True, prs=(), skipped=False):
        with mock.patch.object(gate_ci, "sha_is_current_main_tip", return_value=tip), \
             mock.patch.object(gate_ci, "open_prs_containing",
                               return_value=None if prs is None else list(prs)), \
             mock.patch.object(gate_ci, "_required_checks_all_skipped", return_value=skipped):
            return gate_ci.no_pr_ack_release(REPO, SHA, {"verify"}, state)

    def test_absent_on_main_tip_with_no_open_pr_releases(self):
        self.assertTrue(self.rule())
        self.assertTrue(self.rule(state="unknown", skipped=True))

    def test_off_tip_an_open_pr_or_a_failed_lookup_refuses(self):
        self.assertFalse(self.rule(tip=False))
        self.assertFalse(self.rule(tip=None))
        self.assertFalse(self.rule(prs=[{"number": 1}]))
        self.assertFalse(self.rule(prs=None))

    def test_pending_red_and_unskipped_unknown_refuse(self):
        for state in ("pending", "red", "green"):
            self.assertFalse(self.rule(state=state))
        self.assertFalse(self.rule(state="unknown", skipped=False))


if __name__ == "__main__":
    unittest.main()
