"""Tests for the te-runner driver (harmonic-forge#1055 AC3-AC10).

No network and no real `claude`: a fake `claude` edits the worktree, commits,
and prints canned stream-json; stub `gh` and `mise` stand in on PATH; the
"upstream" and the runner fork are bare repos in a temp directory.
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import te_run  # noqa: E402

REAL_PRECLOSE_FACT = te_run.preclose_fact

ISSUE = ("Body.\n\n## Acceptance criteria\n\n1. The widget returns 2. More text.\n"
         "2. Nothing else changes.\n\n## Notes\n")

FAKE_CLAUDE = textwrap.dedent("""\
    #!/usr/bin/env python3
    import json, os, subprocess, sys
    from pathlib import Path
    here = Path(__file__).resolve().parent
    prompt = sys.stdin.read()
    queue = (here / "queue_path").read_text()
    status = [i["status"] for i in json.loads(Path(queue).read_text())["items"]]
    with open(here / "observed.jsonl", "a") as log:
        log.write(json.dumps({"argv": sys.argv[1:], "cwd": os.getcwd(), "prompt": prompt,
                              "env": dict(os.environ), "statuses": status}) + "\\n")
    Path("widget.py").write_text("def widget():\\n    return %s\\n" % os.environ.get("FAKE_RETURN", "2"))
    subprocess.run(["git", "add", "-A"], check=True)
    subprocess.run(["git", "-c", "user.name=a", "-c", "user.email=a@a", "commit", "-q", "-m", "agent"],
                   check=True)
    if os.environ.get("FAKE_TAMPER_BUNDLE"):  # the agent rewrites Lane 1's tests on disk
        Path(os.environ["FAKE_TAMPER_BUNDLE"]).write_text(json.dumps([{"ac": "AC1", "test": ["true"]}]))
    drop = os.environ.get("FAKE_DROP_HOOK", "")
    print(json.dumps({"type": "system", "subtype": "init",
                      "apiKeySource": os.environ.get("FAKE_KEYSOURCE", "ANTHROPIC_API_KEY")}))
    for hook in ("SessionStart", "UserPromptSubmit", "Stop"):
        if hook != drop:
            for sub in ("hook_started", "hook_response"):
                print(json.dumps({"type": "system", "subtype": sub, "hook_event": hook,
                                  "hook_name": hook}))
    print(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                      "result": os.environ.get("FAKE_FINAL", "done"), "total_cost_usd": 0.2,
                      "permission_denials": []}))
""")

FAKE_GH = textwrap.dedent("""\
    #!/usr/bin/env python3
    import json, os, subprocess, sys
    from pathlib import Path
    here = Path(__file__).resolve().parent
    with open(here / "gh.jsonl", "a") as log:
        log.write(json.dumps(sys.argv[1:]) + "\\n")
    args = sys.argv[1:]
    if args[:2] == ["pr", "create"]:
        branch = args[args.index("--head") + 1].split(":", 1)[1]
        sha = subprocess.run(["git", "-C", os.environ["FAKE_FORK"], "rev-parse",
                              f"refs/heads/{branch}"], capture_output=True, text=True).stdout.strip()
        (here / "pr_head").write_text(sha)
        print("https://github.com/vitalharmony/harmonic-forge/pull/9999")
    elif args[:2] == ["pr", "comment"]:
        print("https://github.com/vitalharmony/harmonic-forge/pull/9999#issuecomment-77")
    elif args[:2] == ["pr", "view"]:
        print(json.dumps({"headRefOid": (here / "pr_head").read_text(),
                          "statusCheckRollup": [{"conclusion": "SUCCESS"}]}))
""")

FAKE_MISE = "#!/bin/sh\nexit ${FAKE_MISE_RC:-0}\n"


def private_scratch_root(case: unittest.TestCase) -> str:
    """A disk-backed scratch root of the test's own (_scratch refuses /tmp), so
    kill_runner never takes the shared scratch lock -- which a kill_check.py
    run of these very tests already holds."""
    base = Path.home() / ".cache" / "te-runner-test-scratch"
    base.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(dir=base))
    case.addCleanup(shutil.rmtree, root, True)
    return str(root)


def sh(*args: str, cwd: Path) -> str:
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def commit(repo: Path, message: str) -> None:
    sh("git", "add", "-A", cwd=repo)
    sh("git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", message, cwd=repo)


class Pipeline(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        upstream, self.fork = self.tmp / "upstream.git", self.tmp / "fork.git"
        for bare in (upstream, self.fork):
            sh("git", "init", "-q", "--bare", "-b", "main", str(bare), cwd=self.tmp)
        src = self.tmp / "src"
        src.mkdir()
        sh("git", "init", "-q", "-b", "main", cwd=src)
        (src / "widget.py").write_text("def widget():\n    return 1\n")
        commit(src, "base")
        sh("git", "remote", "add", "origin", str(upstream), cwd=src)
        sh("git", "push", "-q", "origin", "main", cwd=src)
        self.src = src
        bundle = self.tmp / "bundle"
        (bundle / "files" / "tests").mkdir(parents=True)
        (bundle / "files" / "tests" / "__init__.py").write_text("")
        (bundle / "files" / "tests" / "test_widget.py").write_text(
            "import unittest\nimport widget\n\n\nclass W(unittest.TestCase):\n"
            "    def test_two(self):\n        self.assertEqual(widget.widget(), 2)\n")
        (bundle / "checks.json").write_text(json.dumps(
            [{"ac": "AC1", "test": ["python3", "-m", "unittest", "discover", "-s", "tests",
                                    "-t", "."]}]))
        self.bundle = bundle
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        for name, text in (("claude", FAKE_CLAUDE), ("gh", FAKE_GH), ("mise", FAKE_MISE)):
            (self.bin / name).write_text(text)
            (self.bin / name).chmod(0o755)
        self.queue = self.tmp / "queue.json"
        (self.bin / "queue_path").write_text(str(self.queue))
        te_run.te_queue.add(self.queue, ["F7"])
        key = self.tmp / "anthropic-key"
        key.write_text("sk-runner\n")
        key.chmod(0o600)
        self.enter(mock.patch.dict(os.environ, {
            "PATH": f"{self.bin}:{os.environ['PATH']}", "FAKE_FORK": str(self.fork),
            "GH_TOKEN": "operator-token", "GITHUB_TOKEN": "operator-token",
            "HRSE_SCRATCH_ROOT": private_scratch_root(self)}))
        pd, tq = te_run.preclose_driver, te_run.te_queue
        for target, name, value in (
                (te_run, "SOURCE", src), (te_run, "WORKTREE_ROOT", self.tmp / "wt"),
                (te_run, "FORK_URL", str(self.fork)), (te_run, "CLAUDE", str(self.bin / "claude")),
                (pd, "RUNNER_KEY", key), (pd, "RUNNER_GH", self.tmp / "runner-gh")):
            self.enter(mock.patch.object(target, name, value))
        self.enter(mock.patch.object(te_run.deny_check, "boundary", return_value=[]))
        self.enter(mock.patch.object(tq, "first_failure", return_value=None))
        self.enter(mock.patch.object(tq, "gh_json", return_value={"body": ISSUE}))
        self.digest = tq.kill_runner.bundle_digest(bundle)
        self.enter(mock.patch.object(tq, "handoff",
                                     return_value=f"the handoff\nkill-bundle-sha256: {self.digest}\n"))
        self.enter(mock.patch.object(tq, "tier", return_value="standard"))
        self.enter(mock.patch.object(tq, "bundle_dir", return_value=bundle))
        self.enter(mock.patch.object(te_run, "kill_receipt", return_value=True))
        self.enter(mock.patch.object(te_run, "preclose_fact", return_value={
            "status": "complete", "survivors": 0,
            "provenance": "Red-team provenance: cross-family (codex) — 1/1"}))
        self.events = []
        self.enter(mock.patch.object(te_run, "emit_event",
                                     lambda *a: self.events.append(a)))
        (self.tmp / "wt").mkdir()

    def enter(self, patcher):
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        # Every git write must land in the throwaway repo, never the real one.
        real = Path(te_run.__file__).resolve().parent.parent.parent
        stray = sh("git", "branch", "--list", "fix/*-te-runner", cwd=real)
        self.assertEqual(stray, "", f"a test created {stray!r} in {real}")

    def drive(self, **env) -> int:
        with mock.patch.dict(os.environ, env):
            return te_run.run_queue(self.queue, "1", 5)

    def item(self, key: str = "F7") -> dict:
        return next(i for i in te_run.queue_state.load(self.queue)["items"] if i["id"] == key)

    def observed(self) -> list[dict]:
        path = self.bin / "observed.jsonl"
        return [json.loads(l) for l in path.read_text().splitlines()] if path.exists() else []

    def gh_calls(self) -> list[list[str]]:
        path = self.bin / "gh.jsonl"
        return [json.loads(l) for l in path.read_text().splitlines()] if path.exists() else []

    def fork_branches(self) -> str:
        return sh("git", "for-each-ref", "--format=%(refname)", "refs/heads", cwd=self.fork)

    # --- AC7 green path, set-aside path ------------------------------------------------

    def test_green_path_pushes_opens_pr_and_records_every_key(self):
        self.assertEqual(self.drive(), 0)
        item = self.item()
        self.assertEqual(item["status"], "done")
        self.assertEqual(item["verdict"]["state"], "green", item)
        for key in ("reason", "verdict", "facts", "ac_titles", "diff_stat", "head_sha", "branch",
                    "pr", "pushed_sha"):
            self.assertIn(key, item)
        self.assertEqual(item["pr"], "https://github.com/vitalharmony/harmonic-forge/pull/9999")
        self.assertEqual(item["pushed_sha"], item["head_sha"])
        self.assertEqual(item["facts"]["live"], {"pr_exists": True, "head_sha_matches": True,
                                                 "ci": "green"})
        self.assertEqual(item["facts"]["kill"], [{"ac": "AC1", "base_rc": 1, "head_rc": 0}])
        self.assertEqual(item["ac_titles"], ["The widget returns 2.", "Nothing else changes."])
        self.assertIn("refs/heads/fix/7-te-runner", self.fork_branches())
        create = next(c for c in self.gh_calls() if c[:2] == ["pr", "create"])
        self.assertEqual(create[create.index("--head") + 1], "vitalharmony-runner:fix/7-te-runner")
        title, body = create[create.index("--title") + 1], create[create.index("--body") + 1]
        self.assertTrue(title.endswith("(harmonic-forge#7)"))
        self.assertNotRegex(body.lower(), r"\b(close[sd]?|fix(e[sd])?|resolve[sd]?) #?\d")
        self.assertEqual(len(self.events), 1)

    def test_green_item_records_the_summary_link_and_live_read_time(self):
        def fact(issue, base, head, tier, env, out):
            out.mkdir(parents=True, exist_ok=True)
            (out / "summary.md").write_text("## Preclose pass\n")
            return {"status": "complete", "survivors": 0,
                    "provenance": "Red-team provenance: cross-family (codex) — 1/1"}
        with mock.patch.object(te_run, "preclose_fact", fact):
            self.assertEqual(self.drive(), 0)
        item = self.item()
        self.assertEqual(item["preclose_summary"],
                         "https://github.com/vitalharmony/harmonic-forge/pull/9999#issuecomment-77")
        self.assertRegex(item["live_read_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        comment = next(c for c in self.gh_calls() if c[:2] == ["pr", "comment"])
        self.assertIn("--repo", comment)

    def test_every_finished_item_carries_the_runs_id(self):
        te_run.te_queue.add(self.queue, ["F8"])
        with mock.patch.object(te_run.te_queue, "issue_number",
                               side_effect=lambda key: int(key[1:])):
            self.drive(FAKE_MISE_RC="1")
        state = te_run.queue_state.load(self.queue)
        self.assertRegex(state["run_id"], r"^run-\d{8}T\d{6}Z-[0-9a-f]{8}$")
        self.assertEqual({i["run_id"] for i in state["items"]}, {state["run_id"]})

    def test_a_run_that_works_nothing_still_replaces_the_run_id(self):
        state = te_run.queue_state.load(self.queue)
        state["run_id"] = "run-20200101T000000Z"
        te_run.queue_state.save(self.queue, state)
        with mock.patch.object(te_run.deny_check, "boundary", return_value=["push is True"]):
            self.assertEqual(self.drive(), 3)
        self.assertNotEqual(te_run.queue_state.load(self.queue)["run_id"], "run-20200101T000000Z")

    def test_two_runs_in_the_same_second_get_distinct_ids(self):
        fixed = te_run.dt.datetime(2026, 10, 20, 12, 0, 0, tzinfo=te_run.dt.timezone.utc)
        ids = []
        with mock.patch.object(te_run.deny_check, "boundary", return_value=["stop here"]), \
                mock.patch.object(te_run, "dt", wraps=te_run.dt) as fake_dt:
            fake_dt.datetime.now.return_value = fixed
            for _ in range(2):
                self.drive()
                ids.append(te_run.queue_state.load(self.queue)["run_id"])
        self.assertNotEqual(ids[0], ids[1])
        self.assertTrue(all(i.startswith("run-20261020T120000Z-") for i in ids), ids)

    def test_crash_recovered_item_is_stamped_with_the_new_run(self):
        state = te_run.queue_state.load(self.queue)
        state["items"][0].update({"status": "in_progress", "pushed_sha": "a" * 40,
                                  "branch": "fix/7-te-runner"})
        te_run.queue_state.save(self.queue, state)
        with mock.patch.object(te_run.deny_check, "boundary", return_value=["stop here"]):
            self.drive()
        state = te_run.queue_state.load(self.queue)
        self.assertEqual(state["items"][0]["status"], "blocked")
        self.assertEqual(state["items"][0]["run_id"], state["run_id"])

    def test_every_status_write_goes_through_set_status(self):
        source = Path(te_run.__file__).read_text()
        body = source.split("def _run_queue", 1)[1].split("\ndef main", 1)[0]
        self.assertNotRegex(body, r'\["status"\]\s*=|"status":')

    def test_set_aside_branch_is_never_pushed(self):
        self.assertEqual(self.drive(FAKE_MISE_RC="1"), 0)
        item = self.item()
        self.assertEqual((item["status"], item["reason"]), ("done", "check-fail"))
        self.assertEqual(self.fork_branches(), "")
        self.assertFalse([c for c in self.gh_calls() if c[:2] == ["pr", "create"]])
        self.assertEqual(item["pushed_sha"], "")

    def test_wrong_implementation_fails_the_kill_oracle(self):
        self.assertEqual(self.drive(FAKE_RETURN="3"), 0)
        self.assertEqual(self.item()["reason"], "kill-check-fail")
        self.assertEqual(self.fork_branches(), "")

    def test_self_flag_sets_aside(self):
        self.drive(FAKE_FINAL="partial\nTE-RUNNER-SELF-FLAG: could not test the hook")
        item = self.item()
        self.assertEqual(item["reason"], "self-flag")
        self.assertEqual(item["facts"]["self_flag"], "could not test the hook")

    def test_failed_kill_receipt_sets_aside(self):
        # Operator ruling: a kill_check.py result other than pass is kill-check-fail.
        with mock.patch.object(te_run, "kill_receipt", return_value=False):
            self.drive()
        self.assertEqual(self.item()["reason"], "kill-check-fail")
        self.assertEqual(self.fork_branches(), "")

    def test_preclose_driver_failure_sets_aside_and_is_not_retried(self):
        # A real stub driver behind preclose_fact: it writes a PASSING
        # preclose.json and still exits non-zero. Each item is set aside, and
        # the driver runs exactly once per item (operator ruling, #1059).
        driver = self.tmp / "driver.py"
        driver.write_text(textwrap.dedent("""\
            import json, os, sys
            from pathlib import Path
            with open(os.environ["FAKE_DRIVER_LOG"], "a") as log:
                log.write("run\\n")
            out = Path(sys.argv[sys.argv.index("--out") + 1])
            out.mkdir(parents=True, exist_ok=True)
            (out / "preclose.json").write_text(json.dumps({"status": "complete", "survivors": 0,
                "provenance": "Red-team provenance: cross-family (codex) — 1/1"}))
            sys.exit(int(os.environ["FAKE_DRIVER_RC"]))
        """))
        calls = self.tmp / "driver.log"
        for rc in ("2", "4"):
            calls.write_text("")
            self.setUp_queue_fresh()
            with mock.patch.object(te_run, "preclose_fact", REAL_PRECLOSE_FACT), \
                    mock.patch.object(te_run, "PRECLOSE_DRIVER", driver):
                self.drive(FAKE_DRIVER_RC=rc, FAKE_DRIVER_LOG=str(calls))
            self.assertEqual(calls.read_text().count("run"), 1, rc)
            item = self.item()
            self.assertEqual(item["reason"], "preclose-survivor", rc)
            self.assertEqual(item["facts"]["preclose"]["status"], f"driver exit {rc}")

    def setUp_queue_fresh(self):
        self.queue.unlink()
        te_run.te_queue.add(self.queue, ["F7"])
        for stale in (self.tmp / "wt").iterdir():
            shutil.rmtree(stale)
        sh("git", "worktree", "prune", cwd=self.src)
        sh("git", "branch", "-D", "--quiet", "fix/7-te-runner", cwd=self.src) \
            if "fix/7-te-runner" in sh("git", "branch", "--list", "fix/7-te-runner", cwd=self.src) \
            else None
        for ref in sh("git", "for-each-ref", "--format=%(refname)", "refs/heads", cwd=self.fork).split():
            sh("git", "update-ref", "-d", ref, cwd=self.fork)

    # --- AC3 harness-only writes, AC6 env ----------------------------------------------

    def test_agent_never_receives_queue_path(self):
        self.drive()
        [seen] = self.observed()
        blob = json.dumps({k: seen[k] for k in ("argv", "cwd", "prompt", "env")})
        self.assertNotIn(str(self.queue), blob)
        self.assertNotIn(self.queue.name, seen["prompt"])

    def test_harness_alone_writes_statuses(self):
        self.drive()
        [seen] = self.observed()
        self.assertEqual(seen["statuses"], ["in_progress"])  # set before the agent ran
        self.assertEqual(self.item()["status"], "done")

    def test_child_env_has_no_gh_token(self):
        self.drive()
        env = self.observed()[0]["env"]
        self.assertNotIn("GH_TOKEN", env)
        self.assertNotIn("GITHUB_TOKEN", env)
        self.assertEqual(env["GH_CONFIG_DIR"], str(self.tmp / "runner-gh"))
        self.assertEqual(env["ANTHROPIC_API_KEY"], "sk-runner")

    def test_missing_or_loose_key_file_refuses(self):
        key = te_run.preclose_driver.RUNNER_KEY
        key.chmod(0o644)
        self.assertEqual(te_run.main(["run", "--queue", str(self.queue),
                                      "--max-budget-usd", "1"]), 2)
        key.unlink()
        self.assertEqual(te_run.main(["run", "--queue", str(self.queue),
                                      "--max-budget-usd", "1"]), 2)
        self.assertEqual(self.observed(), [])
        self.assertEqual(self.item()["status"], "pending")

    def test_claude_argv(self):
        self.drive()
        argv = self.observed()[0]["argv"]
        for flag in ("--include-hook-events", "--verbose"):
            self.assertIn(flag, argv)
        for pair in (["--output-format", "stream-json"], ["--permission-mode", "dontAsk"],
                     ["--permission-prompts", "none"], ["--max-budget-usd", "1"],
                     ["--settings", str(te_run.SETTINGS)]):
            i = argv.index(pair[0])
            self.assertEqual(argv[i:i + 2], pair)
        self.assertNotIn("--bare", argv)
        self.assertEqual(Path(self.observed()[0]["cwd"]),
                         (self.tmp / "wt" / "harmonic-forge-7r-impl").resolve())

    def test_missing_settings_file_aborts(self):
        with mock.patch.object(te_run, "SETTINGS", self.tmp / "missing.json"):
            self.assertEqual(self.drive(), 3)
        self.assertEqual(self.item()["status"], "blocked")
        self.assertEqual(self.observed(), [])

    # --- AC5 G3 ------------------------------------------------------------------------

    def test_missing_stop_hook_event_aborts_the_run(self):
        te_run.te_queue.add(self.queue, ["F8"])
        self.assertEqual(self.drive(FAKE_DROP_HOOK="Stop"), 3)
        self.assertEqual(self.item()["status"], "blocked")
        self.assertIn("no Stop hook events", self.item()["note"])
        self.assertEqual(self.item("F8")["status"], "pending")  # the run stopped
        self.assertEqual(self.fork_branches(), "")

    def test_apikeysource_none_aborts_the_run(self):
        self.assertEqual(self.drive(FAKE_KEYSOURCE="none"), 3)
        self.assertIn("apiKeySource", self.item()["note"])
        self.assertEqual(self.fork_branches(), "")

    def test_every_required_hook_is_checked(self):
        for hook in te_run.REQUIRED_HOOKS:
            parsed = te_run.parse_stream("\n".join(
                [json.dumps({"type": "system", "subtype": "init", "apiKeySource": "x"}),
                 json.dumps({"type": "result"})]
                + [json.dumps({"type": "system", "subtype": s, "hook_event": h})
                   for h in te_run.REQUIRED_HOOKS if h != hook
                   for s in ("hook_started", "hook_response")]))
            self.assertEqual(te_run.g3_problems(parsed), [f"no {hook} hook events"])

    # --- AC3 serial order, AC4 never reuse, AC7 recovery -------------------------------

    def test_serial_order_one_item_at_a_time(self):
        te_run.te_queue.add(self.queue, ["F8"])
        with mock.patch.object(te_run.te_queue, "issue_number",
                               side_effect=lambda key: int(key[1:])):
            self.drive(FAKE_MISE_RC="1")
        seen = self.observed()
        self.assertEqual([s["prompt"].split()[1] for s in seen],
                         ["harmonic-forge#7", "harmonic-forge#8"])
        self.assertEqual([s["statuses"] for s in seen],
                         [["in_progress", "pending"], ["done", "in_progress"]])

    def test_existing_branch_blocks_the_item_and_is_never_reused(self):
        sh("git", "branch", "fix/7-te-runner", cwd=self.src)
        self.assertEqual(self.drive(), 3)
        self.assertEqual(self.item()["status"], "blocked")
        self.assertIn("never reused", self.item()["note"])
        self.assertEqual(self.observed(), [])

    def test_kill_after_push_is_reconciled_as_blocked_with_branch(self):
        state = te_run.queue_state.load(self.queue)
        state["items"][0].update({"status": "blocked", "pushed_sha": "a" * 40,
                                  "branch": "fix/7-te-runner",
                                  "note": "push to runner-fork failed: denied"})
        state["items"].append({"id": "F9", "kind": "te-issue", "status": "in_progress",
                               "note": ""})
        te_run.queue_state.save(self.queue, state)
        self.drive()
        self.assertEqual(self.item()["status"], "blocked")
        note = self.item()["note"]
        self.assertIn("fix/7-te-runner", note)
        self.assertIn("may have been pushed", note)  # an attempt, never claimed as a push
        self.assertIn("no PR was recorded", note)
        self.assertIn("push to runner-fork failed: denied", note)  # the run's own record kept
        self.drive()  # a second start keeps the note as it is
        self.assertEqual(self.item()["note"], note)
        self.assertEqual(self.item("F9")["status"], "blocked")
        self.assertIn("recovery", self.item("F9")["note"])

    def test_push_failure_after_pushed_sha_is_recorded(self):
        on_disk = []
        real_run = te_run.run

        def watching(argv, **kw):
            if argv[:2] == ["git", "push"]:  # what a kill at this instant would leave
                on_disk.append(self.item().get("pushed_sha"))
            return real_run(argv, **kw)
        with mock.patch.object(te_run, "FORK_URL", str(self.tmp / "no-such-fork.git")), \
                mock.patch.object(te_run, "run", watching):
            self.assertEqual(self.drive(), 3)
        item = self.item()
        self.assertEqual(item["status"], "blocked")
        self.assertEqual(len(on_disk), 1)
        self.assertTrue(on_disk[0], "pushed_sha was not saved before the push")

    def test_boundary_failure_aborts_before_any_item(self):
        with mock.patch.object(te_run.deny_check, "boundary", return_value=["push is True"]):
            self.assertEqual(self.drive(), 3)
        self.assertEqual(self.item()["status"], "pending")
        self.assertEqual(self.observed(), [])

    def test_bundle_rewritten_by_the_agent_never_reaches_the_oracle(self):
        # A wrong implementation (returns 3) plus a rewritten bundle whose test
        # always passes: the oracle runs the snapshot taken before the agent ran.
        self.drive(FAKE_RETURN="3", FAKE_TAMPER_BUNDLE=str(self.bundle / "checks.json"))
        item = self.item()
        self.assertEqual(item["reason"], "kill-check-fail")
        self.assertEqual(item["facts"]["kill"], [{"ac": "AC1", "base_rc": 1, "head_rc": 1}])
        self.assertEqual(self.fork_branches(), "")

    def test_bundle_that_no_longer_matches_the_pin_blocks_before_the_agent(self):
        (self.bundle / "files" / "tests" / "test_widget.py").write_text("x = 1\n")
        self.assertEqual(self.drive(), 3)
        self.assertIn("kill-bundle-sha256", self.item()["note"])
        self.assertEqual(self.observed(), [])

    def test_symlinked_checks_json_is_refused_before_the_agent(self):
        # checks.json as a symlink to a file the agent could rewrite after the
        # snapshot: refused outright, and nothing the agent writes ever runs.
        real = self.tmp / "agent-writable-checks.json"
        shutil.move(str(self.bundle / "checks.json"), real)
        (self.bundle / "checks.json").symlink_to(real)
        self.assertEqual(self.drive(), 3)
        item = self.item()
        self.assertEqual(item["status"], "blocked")
        self.assertIn("symlink", item["note"])
        self.assertIn("checks.json", item["note"])
        self.assertEqual(self.observed(), [])

    def test_non_finite_budget_is_refused(self):
        for budget in ("nan", "inf", "-inf", "0", "-1"):
            self.assertEqual(te_run.main(["run", "--queue", str(self.queue),
                                          f"--max-budget-usd={budget}"]), 2, budget)
        self.assertEqual(self.observed(), [])

    def test_queue_is_locked_while_a_run_holds_it(self):
        with te_run.te_queue.queue_lock(self.queue):
            with self.assertRaises(te_run.te_queue.QueueError):
                te_run.te_queue.add(self.queue, ["F8"])
            self.assertEqual(te_run.main(["run", "--queue", str(self.queue),
                                          "--max-budget-usd", "1"]), 2)
        self.assertEqual(self.item()["status"], "pending")
        te_run.te_queue.add(self.queue, ["F8"])  # free again once the holder ends

    def test_an_unexpected_error_blocks_the_item_never_in_progress(self):
        boom = UnicodeDecodeError("utf-8", b"\\xe9", 0, 1, "invalid continuation byte")
        with mock.patch.object(te_run, "deny_hits", side_effect=boom):
            self.assertEqual(self.drive(), 3)
        item = self.item()
        self.assertEqual(item["status"], "blocked")
        self.assertIn("invalid continuation byte", item["note"])

    def test_max_steps_bounds_the_run(self):
        te_run.te_queue.add(self.queue, ["F8"])
        with mock.patch.dict(os.environ, {"FAKE_MISE_RC": "1"}):
            te_run.run_queue(self.queue, "1", 1)
        self.assertEqual(self.item("F8")["status"], "pending")


class SafePath(unittest.TestCase):
    """Operator ruling on this issue: a head that shadows the test runner must
    still fail the kill oracle."""

    def test_head_shadowing_unittest_still_fails_the_kill_check(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        src = tmp / "src"
        src.mkdir()
        sh("git", "init", "-q", "-b", "main", cwd=src)
        (src / "widget.py").write_text("def widget():\n    return 1\n")
        commit(src, "base")
        base = sh("git", "rev-parse", "HEAD", cwd=src)
        (src / "unittest.py").write_text("raise SystemExit(0)\n")  # no fix, only a shadow
        commit(src, "head")
        head = sh("git", "rev-parse", "HEAD", cwd=src)
        bundle = tmp / "bundle"
        (bundle / "files" / "tests").mkdir(parents=True)
        (bundle / "files" / "tests" / "__init__.py").write_text("")
        (bundle / "files" / "tests" / "test_widget.py").write_text(
            "import unittest\nimport widget\n\n\nclass W(unittest.TestCase):\n"
            "    def test_two(self):\n        self.assertEqual(widget.widget(), 2)\n")
        (bundle / "checks.json").write_text(json.dumps(
            [{"ac": "AC1", "test": ["python3", "-m", "unittest", "discover", "-s", "tests",
                                    "-t", "."]}]))
        env = {**os.environ, "HRSE_SCRATCH_ROOT": private_scratch_root(self)}
        digest = te_run.te_queue.kill_runner.bundle_digest(bundle)
        with mock.patch.object(te_run, "SOURCE", src):
            kill = te_run.kill_facts(7, base, head, env, bundle, digest)
            self.assertEqual(te_run.kill_facts(7, base, head, env, bundle, "0" * 64), [],
                             "a bundle other than the pinned one must not count")
        self.assertEqual(len(kill), 1)
        self.assertNotEqual(kill[0]["head_rc"], 0, "the head's unittest.py shadowed the oracle")


class RealKillReceipt(unittest.TestCase):
    """The real kill_check.py path (operator ruling: python3 -P on every oracle
    run). A head that only adds a unittest.py shadow, with a wrong fix, must
    not earn a passing receipt."""

    def test_shadowing_head_never_earns_a_kill_check_receipt(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        src = tmp / "src"
        src.mkdir()
        sh("git", "init", "-q", "-b", "main", cwd=src)
        # Never fetched: kill_check.py only reads the URL to name the repo.
        sh("git", "remote", "add", "origin", "https://github.com/vitalharmony/harmonic-forge.git",
           cwd=src)
        (src / "widget.py").write_text("def widget():\n    return 1\n")
        commit(src, "base")
        base = sh("git", "rev-parse", "HEAD", cwd=src)
        (src / "widget.py").write_text("def widget():\n    return 3\n")
        (src / "unittest.py").write_text("raise SystemExit(0)\n")
        commit(src, "head")
        head = sh("git", "rev-parse", "HEAD", cwd=src)
        bundle = tmp / "bundle"
        (bundle / "files" / "tests").mkdir(parents=True)
        (bundle / "files" / "tests" / "__init__.py").write_text("")
        (bundle / "files" / "tests" / "test_widget.py").write_text(
            "import unittest\nimport widget\n\n\nclass W(unittest.TestCase):\n"
            "    def test_two(self):\n        self.assertEqual(widget.widget(), 2)\n")
        (bundle / "checks.json").write_text(json.dumps(
            [{"ac": "AC1", "test": ["python3", "-m", "unittest", "discover", "-s", "tests",
                                    "-t", "."]}]))
        scratch = tmp / "scratch"
        scratch.mkdir()
        # A private receipt store (HOME) and scratch root: never the operator's.
        env = {**os.environ, "HOME": str(tmp / "home"),
               "HRSE_SCRATCH_ROOT": private_scratch_root(self)}
        with mock.patch.object(te_run, "SOURCE", src):
            passed = te_run.kill_receipt(7, base, head, env, scratch, bundle)
        self.assertFalse(passed, "the unittest.py shadow earned a passing kill_check receipt")
        [path] = (tmp / "home" / ".claude" / "state" / "preclose").glob("*_7.kill.json")
        receipt = json.loads(path.read_text())
        # Failed for the right reason: the real test ran at the head and failed
        # (the shadow did not mask it), not because kill_check.py refused to run.
        self.assertEqual(receipt["status"], "fail", receipt)
        self.assertEqual([c["verdict"] for c in receipt["checks"]], ["broken-test"], receipt)


class KillCheckChecks(unittest.TestCase):
    def test_reverse_patch_of_a_non_utf8_file_is_byte_exact(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        src = tmp / "src"
        src.mkdir()
        sh("git", "init", "-q", "-b", "main", cwd=src)
        (src / "widget.py").write_text("x = 1\n")
        commit(src, "base")
        base = sh("git", "rev-parse", "HEAD", cwd=src)
        latin = "caf\xe9 na\xefve\n".encode("latin-1")
        (src / "notes.txt").write_bytes(latin)
        commit(src, "head")
        head = sh("git", "rev-parse", "HEAD", cwd=src)
        bundle = tmp / "bundle"
        (bundle / "files").mkdir(parents=True)
        (bundle / "files" / "t.py").write_text("x = 1\n")
        (bundle / "checks.json").write_text(json.dumps([{"ac": "AC1", "test": ["true"]}]))
        scratch = tmp / "scratch"
        scratch.mkdir()
        with mock.patch.object(te_run, "SOURCE", src):
            path = te_run.kill_check_checks(bundle, base, head, scratch)
        [check] = json.loads(path.read_text())
        sh("git", "apply", check["patch"], cwd=src)  # reverses the head exactly
        self.assertFalse((src / "notes.txt").exists())


    def test_checks_reverse_the_agents_change_and_overlay_the_bundle(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        src = tmp / "src"
        src.mkdir()
        sh("git", "init", "-q", "-b", "main", cwd=src)
        (src / "widget.py").write_text("def widget():\n    return 1\n")
        commit(src, "base")
        base = sh("git", "rev-parse", "HEAD", cwd=src)
        (src / "widget.py").write_text("def widget():\n    return 2\n")
        commit(src, "head")
        head = sh("git", "rev-parse", "HEAD", cwd=src)
        bundle = tmp / "bundle"
        (bundle / "files").mkdir(parents=True)
        (bundle / "files" / "t.py").write_text("x = 1\n")
        (bundle / "checks.json").write_text(json.dumps([{"ac": "AC1", "test": ["python3", "t.py"]}]))
        scratch = tmp / "scratch"
        scratch.mkdir()
        with mock.patch.object(te_run, "SOURCE", src):
            path = te_run.kill_check_checks(bundle, base, head, scratch)
        [check] = json.loads(path.read_text())
        self.assertEqual(check["test"][:2], ["sh", "-c"])
        self.assertIn(str(bundle / "files"), check["test"][2])
        self.assertEqual(check["test"][-2:], ["python3", "t.py"])
        sh("git", "apply", "--check", check["patch"], cwd=src)  # applies to the head
        sh("git", "apply", check["patch"], cwd=src)
        self.assertIn("return 1", (src / "widget.py").read_text())


if __name__ == "__main__":
    unittest.main()
