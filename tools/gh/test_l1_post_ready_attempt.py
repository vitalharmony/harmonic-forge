#!/usr/bin/env python3
"""harmonic-forge#892 -- every `ready-for-l3` attempt that reaches
`post_kind()` writes one `ready-for-l3.attempt` telemetry event, posted or
refused, with a CI snapshot taken after the local check."""
import contextlib
import fcntl
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import l1_post as L  # noqa: E402

REPO = "vitalharmony/harmonic-forge"
SHA = "a" * 40
T0, T1 = "2026-01-01T00:00:00+00:00", "2026-01-01T00:02:30+00:00"
BODY = "## ready-for-l3 — F5\n\n**Target:** x\n**Verified:** y\n**Next:** z\n"


def _completed(stdout: str = "", returncode: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(["x"], returncode, stdout, "")


def _failed_check(output: str = "ok\nFAIL: test_thing (suite.Case)\nmore\n"):
    return lambda sha, branch: ([], (T0, T1), {"result": "fail", "output": output})


class StoreCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = Path(tmp.name) / "store"
        env = mock.patch.dict(os.environ, {"HARMONIC_FORGE_TELEMETRY_STORE": str(self.store)})
        env.start()
        self.addCleanup(env.stop)

    def events(self) -> list[dict]:
        return [json.loads(line) for part in self.store.glob("events/**/*.jsonl")
                for line in part.read_text().splitlines() if line.strip()]

    def refuse(self, kind: str = "ready-for-l3") -> tuple[str, str]:
        """Run post_kind, expect a refusal; return (refusal text, stderr)."""
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as ctx:
            L.post_kind(REPO, 5, kind, BODY, SHA, "br")
        return str(ctx.exception.code if ctx.exception.code is not None else ""), err.getvalue()


class RefusedCheckTests(StoreCase):
    def _run_failed(self, ci_state: str = "pending", output: str | None = None):
        calls = []
        def conclusion(repo, sha, required=None):
            calls.append((repo, sha))
            return (ci_state, "detail")
        check = _failed_check() if output is None else _failed_check(output)
        with mock.patch.object(L, "static_checks", side_effect=check), \
             mock.patch.object(L, "_cwd_repo_from_git", return_value="o/checkout"), \
             mock.patch.object(L.gate_ci, "ci_conclusion", side_effect=conclusion):
            refusal, err = self.refuse()
        return refusal, err, calls

    def test_tc1_a_failed_check_refuses_as_before_and_writes_one_event(self):
        refusal, err, calls = self._run_failed()
        self.assertIn("static verification failed:\nok\nFAIL: test_thing", refusal)
        events = self.events()
        self.assertEqual(len(events), 1, events)
        attrs = events[0]["attrs"]
        self.assertEqual(events[0]["event_type"], "ready-for-l3.attempt")
        self.assertEqual(events[0]["subject_id"], f"{REPO}#5@{SHA[:12]}")
        self.assertEqual((attrs["outcome"], attrs["local_check_result"], attrs["ci_state"]),
                         ("refused-check", "fail", "pending"))
        self.assertEqual(attrs["local_check_ms"], 150000)
        self.assertEqual(attrs["failing_step"], "test_thing (suite.Case)")
        self.assertFalse(attrs["ci_green_local_red"])
        self.assertEqual(len(calls), 1, "one CI snapshot, taken after the check")
        self.assertNotIn("rejected", err)

    def test_tc1_a_500_character_fail_line_is_still_written(self):
        self._run_failed(output="FAIL: test_" + "x" * 500 + "\n")
        events = self.events()
        self.assertEqual(len(events), 1)
        self.assertEqual(len(events[0]["attrs"]["failing_step"]), 200)

    def test_failing_step_skips_the_check_s_own_noise_lines(self):
        noise = ("[hygiene] 2 ERROR lines expected in this fixture\n"
                 "/home/u/x.py: ERROR is not a failure here\n"
                 "FAIL Check 8 — lesson aging (fixture)\n"
                 "FAIL: test_real_one (pkg.Case)\n")
        self._run_failed(output=noise)
        self.assertEqual(self.events()[0]["attrs"]["failing_step"], "test_real_one (pkg.Case)")

    def test_failing_step_falls_back_to_the_failed_task(self):
        self._run_failed(output="lots\n[ci-check] ERROR task failed\n")
        self.assertEqual(self.events()[0]["attrs"]["failing_step"], "task ci-check")

    def test_tc2_green_ci_with_a_failed_check_is_flagged(self):
        self._run_failed(ci_state="green")
        self.assertTrue(self.events()[0]["attrs"]["ci_green_local_red"])

    def test_tc3_the_refusal_snapshot_reads_the_checkout_repo_not_the_issue_repo(self):
        _, _, calls = self._run_failed()
        self.assertEqual(calls, [("o/checkout", SHA)])

    def test_a_snapshot_that_cannot_be_taken_reads_unknown(self):
        with mock.patch.object(L, "static_checks", side_effect=_failed_check()), \
             mock.patch.object(L, "_cwd_repo_from_git", side_effect=RuntimeError("no git")):
            self.refuse()
        self.assertEqual(self.events()[0]["attrs"]["ci_state"], "unknown")


class StaticChecksFailureTests(unittest.TestCase):
    """TC1: a failed check that ALSO dirtied the worktree refuses with its own
    output, never the dirty-worktree message."""

    def test_a_failed_check_that_dirtied_the_worktree_returns_its_own_output(self):
        tmp = tempfile.mkdtemp()
        def fake_run(*args, **kwargs):
            if args[:2] == ("mise", "run") and args[2] == "check":
                return _completed("FAIL: test_x\n", 1)
            if args[:3] == ("git", "status", "--porcelain"):
                return _completed(" M dirtied.py\n")
            if args[:3] == ("git", "rev-parse", "--show-toplevel"):
                return _completed(tmp + "\n")
            return _completed()
        with mock.patch.object(L, "run", side_effect=fake_run), \
             mock.patch.object(L, "resolve_sha", return_value=SHA), \
             mock.patch.object(L, "refresh_main", return_value="b" * 40), \
             mock.patch.object(L, "_source_repo_is_hrse", return_value=False), \
             mock.patch.object(L, "_private_check_tmp", return_value=(None, {})):
            checks, timing, check = L.static_checks(SHA, "br")
        self.assertEqual(check["result"], "fail")
        self.assertIn("FAIL: test_x", check["output"])
        self.assertEqual(len(timing), 2)


class PostedTests(StoreCase):
    def _fake_run(self, *args, **kwargs):
        joined = " ".join(args)
        if joined == "git remote get-url origin":
            return _completed("https://github.com/o/r.git\n")
        if "pulls?head=" in joined:
            return _completed(json.dumps([{"number": 2, "node_id": "PR_1"}]))
        if re.search(r"gh api repos/[^/]+/[^/]+/issues/\d+$", joined):
            return _completed(json.dumps({"node_id": "I_1"}))
        raise AssertionError(f"unexpected run() call: {args!r}")

    def _post(self) -> str:
        posted = {}
        def comment_body(repo, issue, body):
            posted["body"] = body
            return f"https://github.com/{repo}/issues/{issue}#issuecomment-9", 9
        with mock.patch.object(L, "static_checks",
                               return_value=(["mise-check"], (T0, T1), {"result": "pass"})), \
             mock.patch.object(L, "world_checks", return_value=([], [])), \
             mock.patch.object(L, "require_open_pr", return_value=(["pr-open"], [])), \
             mock.patch.object(L, "run", side_effect=self._fake_run), \
             mock.patch.object(L.gate_ci, "ci_conclusion", return_value=("green", "ok")), \
             mock.patch.object(L, "comment_body", side_effect=comment_body), \
             mock.patch.object(L, "write_receipt"):
            L.post_kind(REPO, 5, "ready-for-l3", BODY, SHA, "br")
        return posted["body"]

    def test_tc3_a_post_writes_one_posted_event_and_keeps_the_footer_order(self):
        body = self._post()
        events = self.events()
        self.assertEqual(len(events), 1, events)
        attrs = events[0]["attrs"]
        self.assertEqual((attrs["outcome"], attrs["local_check_result"], attrs["ci_state"]),
                         ("posted", "pass", "green"))
        end = re.search(r"local-check-end=([^;]+);", body).group(1)
        at = re.search(r"ci-snapshot-at=([^;]+);", body).group(1)
        self.assertLess(end, at, "ci-snapshot-at must still fall after local-check-end")

    def test_a_failure_after_the_comment_posted_is_recorded_as_posted(self):
        with mock.patch.object(L, "write_receipt", side_effect=lambda r: L.fail("receipt not written")):
            with mock.patch.object(L, "static_checks",
                                   return_value=(["mise-check"], (T0, T1), {"result": "pass"})), \
                 mock.patch.object(L, "world_checks", return_value=([], [])), \
                 mock.patch.object(L, "require_open_pr", return_value=(["pr-open"], [])), \
                 mock.patch.object(L, "run", side_effect=self._fake_run), \
                 mock.patch.object(L.gate_ci, "ci_conclusion", return_value=("green", "ok")), \
                 mock.patch.object(L, "comment_body", return_value=("https://x/9", 9)), \
                 self.assertRaises(SystemExit):
                L.post_kind(REPO, 5, "ready-for-l3", BODY, SHA, "br")
        self.assertEqual([e["attrs"]["outcome"] for e in self.events()], ["posted"])

    def test_tc5_a_held_partition_lock_delays_but_never_fails_the_post(self):
        self._post()  # creates the partition and its lock file
        # emit() derives event_id from a ts truncated to the second, so a second
        # attempt on the same SHA must land in a later second to be distinct.
        time.sleep(1.05)
        lock_path = next(self.store.glob("events/**/*.jsonl.lock"))
        holder = lock_path.open("a")
        fcntl.flock(holder.fileno(), fcntl.LOCK_EX)
        releaser = threading.Timer(0.5, lambda: (fcntl.flock(holder.fileno(), fcntl.LOCK_UN), holder.close()))
        releaser.start()
        started = time.monotonic()
        self._post()
        self.assertGreaterEqual(time.monotonic() - started, 0.4)
        self.assertEqual(len(self.events()), 2)


class RefusedOtherTests(StoreCase):
    def test_tc4_a_refusal_inside_post_kind_is_recorded_as_refused_other(self):
        def not_an_ancestor(sha, branch):
            L.fail("attested SHA is not based on current origin/main")
        with mock.patch.object(L, "static_checks", side_effect=not_an_ancestor):
            refusal, _ = self.refuse()
        self.assertIn("not based on current origin/main", refusal)
        events = self.events()
        self.assertEqual(len(events), 1, events)
        self.assertEqual((events[0]["attrs"]["outcome"], events[0]["attrs"]["local_check_result"]),
                         ("refused-other", "not-run"))

    def test_tc4_a_handoff_refusal_writes_no_event(self):
        with mock.patch.object(L, "world_checks", side_effect=lambda *a, **k: L.fail("overlap")):
            self.refuse(kind="handoff")
        self.assertEqual(self.events(), [])

    def test_tc4_a_refused_check_is_not_also_recorded_as_refused_other(self):
        with mock.patch.object(L, "static_checks", side_effect=_failed_check()), \
             mock.patch.object(L, "_cwd_repo_from_git", return_value="o/r"), \
             mock.patch.object(L.gate_ci, "ci_conclusion", return_value=("red", "x")):
            self.refuse()
        self.assertEqual([e["attrs"]["outcome"] for e in self.events()], ["refused-check"])


class EmitNeverChangesTheOutcomeTests(StoreCase):
    def test_tc5_an_unwritable_store_keeps_the_refusal_and_reports_on_stderr(self):
        blocker = self.store.parent / "a-file"
        blocker.write_text("not a directory")
        with mock.patch.dict(os.environ, {"HARMONIC_FORGE_TELEMETRY_STORE": str(blocker)}), \
             mock.patch.object(L, "static_checks", side_effect=_failed_check()), \
             mock.patch.object(L, "_cwd_repo_from_git", return_value="o/r"), \
             mock.patch.object(L.gate_ci, "ci_conclusion", return_value=("green", "x")):
            refusal, err = self.refuse()
        self.assertIn("static verification failed", refusal)
        self.assertIn("telemetry not written", err)

    def test_tc5_the_test_runner_forces_a_throwaway_store(self):
        source = (HERE.parent / "run_tests.py").read_text(encoding="utf-8")
        self.assertRegex(source, r'os\.environ\["HARMONIC_FORGE_TELEMETRY_STORE"\]\s*=')


if __name__ == "__main__":
    unittest.main()
