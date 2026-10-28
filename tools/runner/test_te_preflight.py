"""harmonic-forge#1112 AC9: the te-runner preflight runs every check and lists
every failure; a kill bundle whose base run is not a test failure aborts."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import preflight  # noqa: E402
import te_run  # noqa: E402

STUB_BWRAP = Path(__file__).resolve().parent / "testdata" / "stub_bwrap.py"


def sh(*args: str, cwd: Path) -> str:
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def commit(repo: Path, message: str) -> None:
    sh("git", "add", "-A", cwd=repo)
    sh("git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", message, cwd=repo)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        patcher = mock.patch.dict(os.environ, {"TE_RUNNER_BWRAP": str(STUB_BWRAP),
                                               "HARMONIC_FORGE_TESTING": "1"})
        patcher.start()
        self.addCleanup(patcher.stop)


class RunAll(Base):
    def test_every_failure_is_reported(self):
        def boom():
            raise RuntimeError("exploded")
        checks = [("a", lambda: ["a failed"]), ("b", lambda: ["b failed"]), ("c", boom),
                  ("d", lambda: []), ("e", lambda: ["e failed", "e again"])]
        record = preflight.run_all(checks, [("r", lambda: ["noted"])], self.tmp)
        self.assertEqual(record["failures"],
                         ["a failed", "b failed", "c: could not run: exploded",
                          "e failed", "e again"])
        self.assertEqual(record["reported"], ["noted"])
        self.assertEqual(json.loads((self.tmp / "preflight.json").read_text()), record)
        self.assertTrue(record["checks"]["d"]["ok"])


class KillBaselines(Base):
    def setUp(self):
        super().setUp()
        upstream = self.tmp / "upstream.git"
        sh("git", "init", "-q", "--bare", "-b", "main", str(upstream), cwd=self.tmp)
        self.src = self.tmp / "src"
        self.src.mkdir()
        sh("git", "init", "-q", "-b", "main", cwd=self.src)
        (self.src / "widget.py").write_text("def widget():\n    return 1\n")
        commit(self.src, "base")
        sh("git", "remote", "add", "origin", str(upstream), cwd=self.src)
        sh("git", "push", "-q", "origin", "main", cwd=self.src)
        self.base = sh("git", "rev-parse", "HEAD", cwd=self.src)

    def bundle(self, test_body: str) -> Path:
        bundle = self.tmp / "bundle"
        shutil.rmtree(bundle, ignore_errors=True)
        (bundle / "files" / "tests").mkdir(parents=True)
        (bundle / "files" / "tests" / "__init__.py").write_text("")
        (bundle / "files" / "tests" / "test_widget.py").write_text(
            "import unittest\n" + test_body + "\n\nclass W(unittest.TestCase):\n"
            "    def test_two(self):\n        self.assertEqual(widget.widget(), 2)\n")
        (bundle / "checks.json").write_text(json.dumps(
            [{"ac": "AC1", "test": ["python3", "-m", "unittest", "discover", "-s", "tests",
                                    "-t", "."]}]))
        return bundle

    def baselines(self, bundle: Path) -> list[str]:
        with mock.patch.object(preflight.te_queue, "bundle_dir", return_value=bundle):
            return preflight.kill_baselines([7], self.src, self.base, self.tmp)

    def test_a_test_failure_on_the_base_passes(self):
        self.assertEqual(self.baselines(self.bundle("import widget")), [])

    def test_an_import_error_on_the_base_fails(self):
        [failure] = self.baselines(self.bundle("import widget\nimport not_there_yet"))
        self.assertIn("broken-test", failure)

    def test_broken_test_baseline_aborts(self):
        # The whole preflight, as `te_run.py run` calls it: every other check
        # passes, the kill bundle's base run is an import error, so it fails.
        bundle = self.bundle("import widget\nimport not_there_yet")
        state = {"items": [{"id": "F7", "status": "pending", "note": ""}]}
        with mock.patch.object(te_run, "SOURCE", self.src), \
                mock.patch.object(te_run, "BASE_CHECK_ROOT", self.tmp / "base-check"), \
                mock.patch.object(preflight.te_queue, "bundle_dir", return_value=bundle), \
                mock.patch.object(preflight, "permission_probe", return_value=[]), \
                mock.patch.object(preflight, "codex_parity", return_value=[]), \
                mock.patch.object(preflight, "item_failures", return_value=[]), \
                mock.patch.object(te_run, "base_check",
                                  return_value={"sha": self.base, "failures": [], "ran": 1,
                                                "complete": True, "step_failures": []}):
            record = te_run.run_preflight(state, self.tmp / "run", {**os.environ},
                                          {**os.environ})
        self.assertTrue(record["failures"])
        self.assertTrue(any("broken-test" in f for f in record["failures"]), record)
        self.assertTrue((self.tmp / "run" / "preflight.json").is_file())

    def test_failed_origin_fetch_is_collected_and_every_check_still_runs(self):
        # Cross-family finding 1: a failed fetch is one refusal among the
        # others, never an abort before run_all writes its report.
        sh("git", "remote", "set-url", "origin", str(self.tmp / "missing.git"), cwd=self.src)
        state = {"items": [{"id": "F7", "status": "pending", "note": ""}]}
        with mock.patch.object(te_run, "SOURCE", self.src), \
                mock.patch.object(te_run, "BASE_CHECK_ROOT", self.tmp / "base-check"), \
                mock.patch.object(preflight, "permission_probe", return_value=[]), \
                mock.patch.object(preflight, "kill_baselines", return_value=[]), \
                mock.patch.object(preflight, "codex_parity", return_value=["codex differs"]), \
                mock.patch.object(preflight, "item_failures", return_value=["F7 item bad"]), \
                mock.patch.object(te_run, "base_check",
                                  return_value={"sha": self.base, "failures": [], "ran": 1,
                                                "complete": True, "step_failures": []}):
            record = te_run.run_preflight(state, self.tmp / "run", {**os.environ},
                                          {**os.environ})
        failures = record["failures"]
        self.assertTrue(any("fetch origin main failed" in f for f in failures), record)
        self.assertIn("codex differs", failures)
        self.assertIn("F7 item bad", failures)
        self.assertTrue((self.tmp / "run" / "preflight.json").is_file())


ISSUE_BODY = "## Acceptance criteria\n\n1. The widget returns 2.\n"

class ItemChecks(Base):
    """AC12: the preflight calls the pipeline's own per-item preconditions."""

    def setUp(self):
        super().setUp()
        self.src = self.tmp / "src"
        self.src.mkdir()
        sh("git", "init", "-q", "-b", "main", cwd=self.src)
        (self.src / "widget.py").write_text("def widget():\n    return 1\n")
        commit(self.src, "base")
        self.base = sh("git", "rev-parse", "HEAD", cwd=self.src)
        self.seams = {"first_failure": None, "bundle_pin": "a" * 64,
                      "snapshot_bundle": self.tmp / "snap", "body": ISSUE_BODY,
                      "handoff": f"the handoff\nkill-bundle-sha256: {'a' * 64}\n",
                      "open_prs": []}
        self.fork = self.tmp / "fork.git"
        sh("git", "init", "-q", "--bare", "-b", "main", str(self.fork), cwd=self.tmp)
        (self.tmp / "wt").mkdir()
        # The fixture repo has no sync_rules.py; linking has its own tests (#1127).
        import provision  # noqa: PLC0415
        patcher = mock.patch.object(provision, "provision_clone")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.origin = self.tmp / "origin.git"
        sh("git", "init", "-q", "--bare", "-b", "main", str(self.origin), cwd=self.tmp)
        sh("git", "remote", "add", "origin", str(self.origin), cwd=self.src)
        sh("git", "push", "-q", "origin", "main", cwd=self.src)
        # origin reads as the upstream, as a real source checkout's does, and
        # fetches from the local bare repo.
        sh("git", "config", f"url.{self.origin}.insteadOf", te_run.UPSTREAM_URL, cwd=self.src)
        sh("git", "config", "remote.origin.url", te_run.UPSTREAM_URL, cwd=self.src)

    def failures(self, **override) -> list[str]:
        seams = {**self.seams, **override}

        def seam(value):
            if isinstance(value, Exception):
                return {"side_effect": value}
            return {"return_value": value}
        with mock.patch.object(preflight.te_queue, "first_failure", **seam(seams["first_failure"])), \
                mock.patch.object(preflight.te_queue, "bundle_pin", **seam(seams["bundle_pin"])), \
                mock.patch.object(preflight.te_queue, "snapshot_bundle",
                                  **seam(seams["snapshot_bundle"])), \
                mock.patch.object(preflight.acceptance, "issue_body", **seam(seams["body"])), \
                mock.patch.object(te_run, "open_runner_prs", **seam(seams["open_prs"])), \
                mock.patch.object(preflight.te_queue, "handoff", **seam(seams["handoff"])), \
                mock.patch.object(te_run, "SOURCE", self.src), \
                mock.patch.object(te_run, "WORKTREE_ROOT", self.tmp / "wt"), \
                mock.patch.object(te_run, "FORK_URL", str(seams.get("fork", self.fork))):
            return preflight.item_failures(7, self.src, self.base, self.tmp / "item", {})

    def test_clean_item_has_no_failures(self):
        self.assertEqual(self.failures(), [])

    def test_no_acceptance_section_is_refused_before_spend(self):
        """harmonic-forge#1195 AC12: (e)'s acceptance step, via acceptance.py."""
        failures = self.failures(body="## Problem\n\nno criteria\n")
        self.assertTrue(any("F7 acceptance:" in f and "no acceptance criteria section" in f
                            for f in failures), failures)

    def test_empty_acceptance_section_is_refused_before_spend(self):
        failures = self.failures(body="## Acceptance criteria\n\nprose only\n\n## Next\n")
        self.assertTrue(any("F7 acceptance:" in f and "lists no criteria" in f
                            for f in failures), failures)

    def test_an_unreadable_issue_body_is_a_refusal(self):
        found = self.failures(body=preflight.sandbox.RunnerEnvError("gh down"))
        self.assertTrue(any("F7 acceptance:" in f and "gh down" in f for f in found), found)

    def test_kill_bundle_and_queue_refusals_are_both_listed(self):
        failures = self.failures(
            first_failure=("ineligible", "no tooling-exception label"),
            snapshot_bundle=preflight.te_queue.QueueError("the kill-check bundle no longer "
                                                          "matches the handoff's pin"))
        self.assertTrue(any("eligibility" in f and "tooling-exception" in f for f in failures),
                        failures)
        self.assertTrue(any("kill bundle" in f and "no longer matches" in f for f in failures),
                        failures)


    def test_earlier_attempt_traces_are_not_a_refusal(self):
        """harmonic-forge#1195: a re-run archives an earlier attempt instead of
        refusing, so its local and fork traces no longer fail the preflight."""
        sh("git", "branch", "fix/7-te-runner", cwd=self.src)
        sh("git", "update-ref", "refs/te-runner/7", "HEAD", cwd=self.src)
        sh("git", "push", "-q", str(self.fork), "HEAD:refs/heads/fix/7-te-runner", cwd=self.src)
        self.assertEqual(self.failures(), [])

    def test_an_open_runner_pr_is_reported_before_any_spend(self):
        failures = self.failures(open_prs=[{"number": 12, "title": "te-runner: x "
                                            "(harmonic-forge#7)", "headRefName": "fix/7-te-runner"}])
        self.assertTrue(any("item preconditions" in f and "runner PR #12" in f
                            for f in failures), failures)

    def test_an_unreadable_fork_is_reported(self):
        failures = self.failures(fork=self.tmp / "no-fork.git")
        self.assertTrue(any("cannot read the runner fork" in f for f in failures), failures)

    def test_failing_origin_fetch_is_listed_with_the_others(self):
        # Post-verdict gap 3: the origin fetch work_item needs is a
        # precondition; its failure is listed beside every other refusal.
        sh("git", "remote", "set-url", "origin", str(self.tmp / "missing.git"), cwd=self.src)
        failures = self.failures(open_prs=[{"number": 3, "title": "(harmonic-forge#7)"}])
        self.assertTrue(any("item preconditions" in f and "fetch origin main failed" in f
                            for f in failures), failures)
        self.assertTrue(any("item preconditions" in f and "runner PR #3" in f
                            for f in failures), failures)

    def test_handoff_queue_error_is_listed_and_later_guards_still_run(self):
        # Cross-family finding 2: _guard_bundle_pin's handoff() can raise
        # QueueError; it is one refusal, and the guards before and after it
        # still report theirs.
        sh("git", "remote", "set-url", "origin", str(self.tmp / "missing.git"), cwd=self.src)
        failures = self.failures(handoff=preflight.te_queue.QueueError("handoff unreadable"),
                                 open_prs=[{"number": 3, "title": "(harmonic-forge#7)"}])
        self.assertTrue(any("item preconditions" in f and "handoff unreadable" in f
                            for f in failures), failures)
        self.assertTrue(any("item preconditions" in f and "runner PR #3" in f
                            for f in failures), failures)
        self.assertTrue(any("item preconditions" in f and "fetch origin main failed" in f
                            for f in failures), failures)

    def test_failing_origin_fetch_raises_runner_env_error(self):
        sh("git", "remote", "set-url", "origin", str(self.tmp / "missing.git"), cwd=self.src)
        with self.assertRaises(preflight.sandbox.RunnerEnvError):
            te_run._guard_origin_fetch(7, {}, self.src)

    def test_new_blocked_in_item_preconditions_is_reported(self):
        # The AC12 pattern: a Blocked added to te_run.item_preconditions
        # reaches the preflight with no change to the preflight's code.
        def new_guard(issue, env, source):
            raise te_run.Blocked("a brand-new block")
        guards = (*te_run.ITEM_GUARDS, new_guard)
        with mock.patch.object(te_run, "ITEM_GUARDS", guards):
            failures = self.failures()
        self.assertTrue(any("item preconditions" in f and "a brand-new block" in f
                            for f in failures), failures)

class Pieces(unittest.TestCase):
    def test_parse_probe_lists_attempts_and_denials(self):
        lines = [{"type": "assistant", "message": {"content": [
                     {"type": "tool_use", "name": "Bash", "input": {"command": "mkdir -p x"}}]}},
                 {"type": "result", "permission_denials": [
                     {"tool_name": "Bash", "tool_input": {"command": "mkdir -p x"}}]}]
        attempted, denied, result = preflight.parse_probe("\n".join(map(json.dumps, lines)))
        self.assertEqual(attempted, ["mkdir -p x"])
        self.assertEqual(denied, ["mkdir -p x"])
        self.assertIsNotNone(result)

    def test_base_problems_name_failing_non_test_steps_and_a_crashed_run(self):
        self.assertEqual(preflight.base_problems({"sha": "b", "complete": True,
                                                  "step_failures": []}), [])
        found = preflight.base_problems({"sha": "b", "complete": True,
                                         "step_failures": ["check_cross_registry exited 1"]})
        self.assertEqual(found, ["base b: check_cross_registry exited 1"])
        self.assertTrue(preflight.base_problems({"sha": "b", "complete": False,
                                                 "step_failures": []}))

    def test_base_regression_is_reported_only_from_empty(self):
        self.assertTrue(preflight.base_regression({"sha": "b", "failures": ["x"]},
                                                  {"sha": "a", "failures": []}))
        self.assertEqual(preflight.base_regression({"sha": "b", "failures": ["x"]},
                                                   {"sha": "a", "failures": ["y"]}), [])
        self.assertEqual(preflight.base_regression({"sha": "b", "failures": ["x"]}, None), [])

    def test_probe_list_covers_the_ac5_commands(self):
        text = "\n".join(preflight.PROBE_COMMANDS)
        for cmd in ("mkdir", "rm ", "cp ", "mv ", "touch", "git checkout", "git switch",
                    "git restore", "git stash", "git mv", "git rm", "git reset", "git rev-parse",
                    "git branch", "mise tasks", "mise run check --help"):
            self.assertIn(cmd, text)


SENTINEL = preflight.SENTINEL_COMMAND
SENTINEL_HOOK_EVENT = {"type": "system", "subtype": "hook_response", "hook_event": "PreToolUse",
                       "output": '{"hookSpecificOutput": {"permissionDecision": "deny"}}'}


class ProbeFailure(Base):
    """Preclose survivor 5: a probe that attempted every command but failed
    proves nothing ran. harmonic-forge#1195: the sentinel must be denied by
    the settings' own hook."""

    def probe(self, result: dict, rc: int = 0, *, sentinel_denied: bool = True,
              hook_event: bool = True, extra_denied: tuple[str, ...] = ()) -> tuple[list, list]:
        commands = ["mkdir -p x"]
        denials = [{"tool_name": "Bash", "tool_input": {"command": c}}
                   for c in ((SENTINEL,) if sentinel_denied else ()) + extra_denied]
        lines = [{"type": "assistant", "message": {"content": [
                     {"type": "tool_use", "name": "Bash", "input": {"command": c}}
                     for c in (*commands, SENTINEL)]}},
                 *([SENTINEL_HOOK_EVENT] if hook_event else []),
                 {"type": "result", "permission_denials": denials, **result}]
        done = subprocess.CompletedProcess([], rc, "\n".join(map(json.dumps, lines)), "")
        with mock.patch.object(preflight, "_run", return_value=done) as ran:
            failures = preflight.permission_probe(commands, self.tmp, self.tmp / "t", {},
                                                  self.tmp / "settings.json", ())
        return failures, ran.call_args

    def test_a_clean_probe_passes(self):
        failures, call = self.probe({"subtype": "success", "is_error": False})
        self.assertEqual(failures, [])
        argv = call.args[0]
        # harmonic-forge#1195 rework: the probe launches exactly as the agent.
        base = te_run.launch_base(self.tmp / "settings.json")
        start = argv.index(te_run.CLAUDE)
        self.assertEqual(argv[start:start + len(base)], base)
        self.assertNotIn("--bare", argv)
        i = argv.index("--setting-sources")
        self.assertEqual(argv[i:i + 2], ["--setting-sources", ""])
        self.assertIn("--include-hook-events", argv)
        self.assertTrue(call.kwargs["stdin"].rstrip().endswith(f"`{SENTINEL}`"))

    def test_failure_text_names_the_agent_mode_not_bare(self):
        failures, _ = self.probe({"subtype": "success", "is_error": False},
                                 sentinel_denied=False)
        self.assertTrue(failures)
        self.assertFalse([f for f in failures if "--bare" in f], failures)
        self.assertTrue(any("agent's launch mode" in f for f in failures), failures)

    def test_sentinel_not_denied_fails(self):
        failures, _ = self.probe({"subtype": "success", "is_error": False},
                                 sentinel_denied=False)
        self.assertTrue(any("sentinel" in f and "not denied" in f for f in failures), failures)

    def test_no_hook_response_for_the_deny_fails(self):
        failures, _ = self.probe({"subtype": "success", "is_error": False}, hook_event=False)
        self.assertTrue(any("hook_response" in f for f in failures), failures)

    def test_any_other_denial_still_fails(self):
        failures, _ = self.probe({"subtype": "success", "is_error": False},
                                 extra_denied=("mkdir -p x",))
        self.assertEqual(failures, ["permission probe: denied `mkdir -p x`"])

    def test_is_error_with_no_denials_fails(self):
        failures, _ = self.probe({"subtype": "success", "is_error": True})
        self.assertTrue(any("is_error" in f for f in failures), failures)

    def test_non_zero_exit_or_error_subtype_fails(self):
        self.assertTrue(self.probe({"subtype": "success", "is_error": False}, rc=1)[0])
        failures, _ = self.probe({"subtype": "error_during_execution", "is_error": False})
        self.assertTrue(any("error_during_execution" in f for f in failures), failures)

    def test_settings_carry_the_sentinel_hook(self):
        """The committed settings deny exactly the sentinel through their hook."""
        settings = json.loads(te_run.SETTINGS.read_text())
        self.assertEqual(sorted(settings["hooks"]),
                         sorted(("SessionStart", "UserPromptSubmit", "Stop", "PreToolUse")))
        [entry] = settings["hooks"]["PreToolUse"]
        self.assertEqual(entry["matcher"], "Bash")
        hook = entry["hooks"][0]["command"]
        for command, denied in ((SENTINEL, True), ("git status", False), (SENTINEL + " ", False)):
            with self.subTest(command=command):
                out = subprocess.run(["sh", "-c", hook], capture_output=True, text=True,
                                     input=json.dumps({"tool_input": {"command": command}}))
                self.assertEqual(out.returncode, 0)
                if denied:
                    decision = json.loads(out.stdout)["hookSpecificOutput"]
                    self.assertEqual(decision["permissionDecision"], "deny")
                else:
                    self.assertEqual(out.stdout, "")
        for event in te_run.REQUIRED_HOOKS:
            self.assertEqual(settings["hooks"][event][0]["hooks"][0]["command"], "true")


if __name__ == "__main__":
    unittest.main()
