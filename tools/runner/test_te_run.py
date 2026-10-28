"""Tests for the te-runner driver (harmonic-forge#1055 AC3-AC10).

No network and no real `claude`: a fake `claude` edits the worktree, commits,
and prints canned stream-json; stub `gh` and `mise` stand in on PATH; the
"upstream" and the runner fork are bare repos in a temp directory.
"""
from __future__ import annotations

import ast
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
import warnings
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import te_run  # noqa: E402


def NO_CHECKS(issue: int) -> list[str]:  # noqa: N802 -- add-time checks have their own tests
    return []

ISSUE = ("Body.\n\n## Acceptance criteria\n\n1. The widget returns 2. More text.\n"
         "2. Nothing else changes.\n\n## Notes\n")

FAKE_CLAUDE = textwrap.dedent("""\
    #!/usr/bin/env python3
    import json, os, subprocess, sys
    from pathlib import Path
    here = Path(__file__).resolve().parent
    prompt = sys.stdin.read()
    try:  # under real bwrap the queue is outside the allowlist, so unreadable
        queue = (here / "queue_path").read_text()
        status = [i["status"] for i in json.loads(Path(queue).read_text())["items"]]
    except OSError:
        status = None
    try:  # best effort: under real bwrap the fake's own directory is read-only
        with open(here / "observed.jsonl", "a") as log:
            log.write(json.dumps({"argv": sys.argv[1:], "cwd": os.getcwd(), "prompt": prompt,
                                  "env": dict(os.environ), "statuses": status}) + "\\n")
    except OSError:
        pass
    Path("widget.py").write_text("def widget():\\n    return %s\\n" % os.environ.get("FAKE_RETURN", "2"))
    subprocess.run(["git", "add", "-A"], check=True)
    subprocess.run(["git", "-c", "user.name=a", "-c", "user.email=a@a", "commit", "-q", "-m", "agent"],
                   check=True)
    if os.environ.get("FAKE_PRE_PUSH_SENTINEL"):  # an agent-planted hook in its own clone
        hook = Path(".git/hooks/pre-push")
        hook.write_text("#!/bin/sh\\ntouch " + os.environ["FAKE_PRE_PUSH_SENTINEL"] + "\\n")
        hook.chmod(0o755)
    if os.environ.get("FAKE_UNTRACKED"):  # an uncommitted file left in the agent's clone
        Path(os.environ["FAKE_UNTRACKED"]).write_text("[tasks.check]\\nrun = 'true'\\n")
    if os.environ.get("FAKE_TAMPER_BUNDLE"):  # the agent rewrites Lane 1's tests on disk
        Path(os.environ["FAKE_TAMPER_BUNDLE"]).write_text(json.dumps([{"ac": "AC1", "test": ["true"]}]))
    if os.environ.get("FAKE_SLEEP_FOR") and os.environ["FAKE_SLEEP_FOR"] in prompt:
        # An agent that hangs, with a child of its own (harmonic-forge#1195 AC7).
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)",
                                  os.environ["FAKE_SLEEP_MARKER"]])
        Path("spawned.pid").write_text(str(child.pid))  # the clone is writable
        import time
        time.sleep(120)
    drop = os.environ.get("FAKE_DROP_HOOK", "")
    print(json.dumps({"type": "system", "subtype": "init",
                      "apiKeySource": os.environ.get("FAKE_KEYSOURCE", "ANTHROPIC_API_KEY")}))
    for hook in ("SessionStart", "UserPromptSubmit", "Stop"):
        if hook != drop:
            for sub in ("hook_started", "hook_response"):
                print(json.dumps({"type": "system", "subtype": sub, "hook_event": hook,
                                  "hook_name": hook}))
    print(json.dumps({"type": "result", "subtype": os.environ.get("FAKE_SUBTYPE", "success"),
                      "is_error": False,
                      "result": os.environ.get("FAKE_FINAL", "done"), "total_cost_usd": 0.2,
                      "permission_denials": []}))
""")

FAKE_GH = textwrap.dedent("""\
    #!/usr/bin/env python3
    import json, os, subprocess, sys
    from pathlib import Path
    here = Path(__file__).resolve().parent
    args = sys.argv[1:]
    try:  # best effort: under real bwrap the fake's own directory is read-only
        with open(here / "gh.jsonl", "a") as log:
            log.write(json.dumps(args) + "\\n")
        with open(here / "gh_env.jsonl", "a") as log:
            log.write(json.dumps({"argv": args, "GH_TOKEN": os.environ.get("GH_TOKEN")}) + "\\n")
    except OSError:
        pass
    if args[:2] == ["api", "user"]:  # the start-up probe (harmonic-forge#1107)
        sys.stderr.write(os.environ.get("FAKE_PROBE_STDERR", ""))
        print(os.environ.get("FAKE_LOGIN", "vitalharmony-runner"))
        sys.exit(int(os.environ.get("FAKE_PROBE_RC", "0")))
    if args[:2] == ["pr", "create"]:
        branch = args[args.index("--head") + 1].split(":", 1)[1]
        sha = subprocess.run(["git", "-C", os.environ["FAKE_FORK"], "rev-parse",
                              f"refs/heads/{branch}"], capture_output=True, text=True).stdout.strip()
        (here / "pr_head").write_text(sha)
        print("https://github.com/vitalharmony/harmonic-forge/pull/9999")
    elif args[:2] == ["pr", "list"]:  # the open-runner-PR guard (harmonic-forge#1195)
        print(os.environ.get("FAKE_OPEN_PRS", "[]"))
        sys.exit(int(os.environ.get("FAKE_PR_LIST_RC", "0")))
    elif args[:2] == ["pr", "view"]:
        print(json.dumps({"headRefOid": (here / "pr_head").read_text()}))
""")

FAKE_MISE = "#!/bin/sh\nprintf '%b' \"${FAKE_MISE_OUT:-}\"\nexit ${FAKE_MISE_RC:-0}\n"


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



STUB_BWRAP = Path(__file__).resolve().parent / "testdata" / "stub_bwrap.py"


def setUpModule():
    # The unit tests run the boundary's argv through a stand-in (harmonic-forge#1098);
    # real confinement has its own tests.
    os.environ["TE_RUNNER_BWRAP"] = str(STUB_BWRAP)
    os.environ["HARMONIC_FORGE_TESTING"] = "1"

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
        te_run.te_queue.add(self.queue, ["F7"], checks=NO_CHECKS)
        key = self.tmp / "anthropic-key"
        key.write_text("sk-runner\n")
        key.chmod(0o600)
        self.token = self.tmp / "gh-token"
        self.token.write_text("runner-token\n")
        self.token.chmod(0o600)
        self.enter(mock.patch.object(te_run, "RUNNER_GH_TOKEN", self.token, create=True))
        self.enter(mock.patch.dict(os.environ, {
            "PATH": f"{self.bin}:{os.environ['PATH']}", "FAKE_FORK": str(self.fork),
            "GH_TOKEN": "operator-token", "GITHUB_TOKEN": "operator-token",
            "HRSE_SCRATCH_ROOT": private_scratch_root(self),
            "STUB_BWRAP_LOG": str(self.tmp / "bwrap.jsonl")}))
        sb, tq = te_run.sandbox, te_run.te_queue
        for target, name, value in (
                (te_run, "SOURCE", src), (te_run, "WORKTREE_ROOT", self.tmp / "wt"),
                (te_run, "FORK_URL", str(self.fork)), (te_run, "CLAUDE", str(self.bin / "claude")),
                (sb, "RUNNER_KEY", key), (sb, "RUNNER_GH", self.tmp / "runner-gh"),
                (te_run, "ARCHIVE_ROOT", self.tmp / "archive")):
            self.enter(mock.patch.object(target, name, value))
        self.enter(mock.patch.object(te_run.deny_check, "boundary", return_value=[]))
        # The fixture repo has no sync_rules.py; linking has its own tests (#1127).
        self.provisioned = []
        self.enter(mock.patch.object(te_run.provision, "provision_clone",
                                     lambda path, objects: self.provisioned.append(path)))
        # Run evidence and the base-check cache stay in the fixture (#1112).
        self.enter(mock.patch.object(te_run, "RUNS_ROOT", self.tmp / "runs"))
        self.enter(mock.patch.object(te_run, "BASE_CHECK_ROOT", self.tmp / "base-check"))
        self.enter(mock.patch.object(te_run, "CHECK_TREE_ROOT", self.tmp / "check-trees"))
        # One test step, run through the stub mise (FAKE_MISE_OUT/FAKE_MISE_RC).
        self.enter(mock.patch.object(te_run, "CHECK_STEPS",
                                     (("run_tests", "python3 tools/run_tests.py"),)))
        self.preflight = mock.MagicMock(return_value={"failures": [], "reported": []})
        self.enter(mock.patch.object(te_run, "run_preflight", self.preflight))
        self.enter(mock.patch.object(tq, "first_failure", return_value=None))
        self.enter(mock.patch.object(tq, "gh_json", return_value={"body": ISSUE}))
        self.digest = tq.kill_runner.bundle_digest(bundle)
        self.enter(mock.patch.object(tq, "handoff",
                                     return_value=f"the handoff\nkill-bundle-sha256: {self.digest}\n"))
        self.enter(mock.patch.object(tq, "tier", return_value="standard"))
        self.enter(mock.patch.object(tq, "bundle_dir", return_value=bundle))
        # harmonic-forge#1147: the newest Lane 1 post and the runner tree, offline.
        self.enter(mock.patch.object(tq, "lane1_marker_id", return_value=11))
        self.enter(mock.patch.object(tq, "runner_tree", return_value="tree0"))
        self.enter(mock.patch.object(te_run, "kill_receipt", return_value=True))
        # CI through gate_ci's authorizing read only (harmonic-forge#1195 AC4).
        self.ci = ("green", "verify:success")
        self.ci_reads = []
        self.enter(mock.patch.object(te_run.gate_ci, "required_checks", return_value=set()))
        self.enter(mock.patch.object(te_run.gate_ci, "authorizing_conclusion",
                                     lambda repo, sha, required, run=None:
                                     self.ci_reads.append((repo, sha)) or self.ci))
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
        self.assertEqual(item["facts"]["live"], {"pr_exists": True, "head_sha_matches": True})
        self.assertNotIn("preclose", item["facts"])
        self.assertEqual(item["merge"], {"authorized": True, "ci": "green",
                                         "detail": "verify:success", "set_aside": False})
        self.assertEqual(item["attempt"], 1)
        self.assertEqual(self.ci_reads, [("vitalharmony/harmonic-forge", item["head_sha"])])
        self.assertFalse([c for c in self.gh_calls() if c[:2] == ["pr", "comment"]])
        self.assertEqual(item["facts"]["kill"], [{"ac": "AC1", "base_rc": 1, "head_rc": 0}])
        self.assertEqual(item["ac_titles"], ["The widget returns 2.", "Nothing else changes."])
        self.assertIn("refs/heads/fix/7-te-runner", self.fork_branches())
        create = next(c for c in self.gh_calls() if c[:2] == ["pr", "create"])
        self.assertEqual(create[create.index("--head") + 1], "vitalharmony-runner:fix/7-te-runner")
        title, body = create[create.index("--title") + 1], create[create.index("--body") + 1]
        self.assertTrue(title.endswith("(harmonic-forge#7)"))
        self.assertNotRegex(body.lower(), r"\b(close[sd]?|fix(e[sd])?|resolve[sd]?) #?\d")
        self.assertEqual(len(self.events), 1)

    def test_pending_ci_opens_the_pr_but_never_authorizes_merge(self):
        """harmonic-forge#1195 AC4."""
        self.ci = ("pending", "verify: queued")
        self.assertEqual(self.drive(), 0)
        item = self.item()
        self.assertEqual(item["pr"], "https://github.com/vitalharmony/harmonic-forge/pull/9999")
        self.assertEqual(item["verdict"]["state"], "green")
        self.assertFalse(item["merge"]["authorized"])
        self.assertEqual(item["merge"]["ci"], "pending")
        self.assertIn("queued", item["merge"]["detail"])
        self.assertRegex(item["live_read_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")

    def test_absent_or_unknown_ci_leaves_the_item_open_and_unauthorized(self):
        for state in ("absent", "unknown"):
            with self.subTest(state=state):
                self.setUp_queue_fresh()
                self.ci = (state, "no verify run")
                self.drive()
                item = self.item()
                self.assertEqual(item["verdict"]["state"], "green")
                self.assertFalse(item["merge"]["authorized"])

    def test_red_ci_sets_the_item_aside_as_check_fail(self):
        self.ci = ("red", "verify:failure")
        self.drive()
        item = self.item()
        self.assertEqual(item["pr"], "https://github.com/vitalharmony/harmonic-forge/pull/9999")
        self.assertEqual((item["verdict"]["state"], item["reason"]), ("set-aside", "check-fail"))
        self.assertIn("CI is red", item["note"])
        self.assertFalse(item["merge"]["authorized"])

    def test_ci_is_read_only_through_gate_ci(self):
        """AC4: no hand-rolled CI rollup is left in the runner."""
        runner = Path(te_run.__file__).resolve().parent
        for path in runner.glob("*.py"):
            if path.name.startswith("test_"):
                continue
            text = path.read_text()
            with self.subTest(path=path.name):
                self.assertNotIn("statusCheck" "Rollup", text)  # split: AC4 greps for it
                expected = 1 if path.name == "te_run.py" else 0
                self.assertEqual(text.count("gate_ci.authorizing_conclusion("), expected)

    def test_ci_state_passes_the_runner_env_to_gate_ci(self):
        seen = []

        def conclusion(repo, sha, required, run=None):
            seen.append(run(["gh", "api", "user"]))
            return "green", "ok"
        with mock.patch.object(te_run.gate_ci, "authorizing_conclusion", conclusion), \
                mock.patch.object(te_run, "run", return_value=subprocess.CompletedProcess(
                    [], 0, "out", "")) as ran:
            self.assertEqual(te_run.ci_state("a" * 40, {"GH_CONFIG_DIR": "/runner"}),
                             ("green", "ok"))
        self.assertEqual(seen, [(0, "out")])
        self.assertEqual(ran.call_args.kwargs["env"], {"GH_CONFIG_DIR": "/runner"})
    def test_every_finished_item_carries_the_runs_id(self):
        te_run.te_queue.add(self.queue, ["F8"], checks=NO_CHECKS)
        with mock.patch.object(te_run.te_queue, "issue_number",
                               side_effect=lambda key: int(key[1:])):
            self.drive(FAKE_MISE_RC="1")
        state = te_run.queue_state.load(self.queue)
        self.assertRegex(state["run_id"], r"^run-\d{8}T\d{6}Z-[0-9a-f]{8}$")
        self.assertEqual({i["run_id"] for i in state["items"]}, {state["run_id"]})

    def test_the_run_saves_its_invocation_id_beside_its_run_id(self):
        # Sticky-wicket patch 2: the report ties itself to the run that ended.
        self.drive(INVOCATION_ID="inv-123")
        self.assertEqual(te_run.queue_state.load(self.queue).get("invocation_id"), "inv-123")

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

    def bwrap_calls(self) -> list[list[str]]:
        path = self.tmp / "bwrap.jsonl"
        return [json.loads(l) for l in path.read_text().splitlines()] if path.exists() else []

    @staticmethod
    def wrapped_command(call: list[str]) -> list[str]:
        return call[call.index("--chdir") + 3:]

    def test_agent_runs_inside_the_boundary(self):
        self.drive()
        clone = str((self.tmp / "wt" / "harmonic-forge-7r-impl").resolve())
        agent = [c for c in self.bwrap_calls() if self.wrapped_command(c)[:1]
                 == [str(self.bin / "claude")]]
        self.assertEqual(len(agent), 1)
        call = agent[0]
        self.assertEqual(call[call.index("--chdir") + 1], clone)
        # The writable bind is the last mount, after every read-only one; only
        # the mise env follows it (harmonic-forge#1112).
        at = call.index("--setenv")
        self.assertEqual(call[at - 3:at], ["--bind", clone, clone])
        self.assertEqual(call[at - 6:at - 3], ["--ro-bind"] + [str(te_run.source_objects())] * 2)

    def test_boundary_is_an_allowlist_over_an_empty_home(self):
        self.drive()
        home = te_run.sandbox.HOME
        secrets = (home / ".local" / "share" / "te-runner",
                   home / ".config" / "te-runner" / "anthropic-key", home / ".claude" / "state")
        calls = self.bwrap_calls()
        self.assertTrue(calls)
        for call in calls:
            mounts = [(a, Path(call[i + 1])) for i, a in enumerate(call)
                      if a in ("--bind", "--ro-bind", "--tmpfs")]
            tmpfs = [p for a, p in mounts if a == "--tmpfs"]
            bound = [p for a, p in mounts if a != "--tmpfs"]
            self.assertIn(home, tmpfs)
            self.assertIn(Path("/tmp"), tmpfs)
            self.assertNotIn(Path("/"), bound)
            self.assertNotIn(home, bound)
            # Nothing bound exposes a secret, unless a later tmpfs covers it again.
            for secret in secrets:
                for i, (kind, path) in enumerate(mounts):
                    if kind != "--tmpfs" and secret.is_relative_to(path):
                        self.assertTrue(any(k == "--tmpfs" and secret.is_relative_to(p)
                                            for k, p in mounts[i + 1:]), (secret, call))

    def test_agent_check_and_kill_tests_run_inside_the_boundary(self):
        self.drive()
        commands = [self.wrapped_command(c) for c in self.bwrap_calls()]
        self.assertTrue(any(c[:5] == ["mise", "exec", "--", "sh", "-c"] for c in commands),
                        commands)
        # kill_runner's test commands (a subprocess of te_run) are wrapped too
        self.assertTrue(any(c[:3] == ["python3", "-m", "unittest"] for c in commands), commands)

    def test_system_binds_the_ca_store(self):
        # /etc/ssl/certs links into it; without it TLS fails inside (harmonic-forge#1107).
        self.assertIn("/var/lib/ca-certificates", te_run.sandbox.SYSTEM)

    def test_missing_bwrap_aborts_the_run(self):
        with mock.patch.dict(os.environ, {"TE_RUNNER_BWRAP": ""}), \
                mock.patch.object(te_run.sandbox.shutil, "which", return_value=None), \
                mock.patch.object(te_run, "log") as logged:
            self.assertEqual(self.drive(), 3)
        # The start-up probe needs the boundary too, so it fails closed before any item.
        self.assertIn("bwrap is not installed", " ".join(c.args[1] for c in logged.call_args_list))
        self.assertEqual(self.item()["status"], "pending")
        self.assertEqual(self.observed(), [])

    def test_fetched_head_is_pinned_by_a_ref(self):
        self.drive()
        pinned = sh("git", "rev-parse", "refs/te-runner/7", cwd=self.src)
        self.assertEqual(pinned, self.item()["head_sha"])

    def test_clone_pre_push_hook_never_runs(self):
        sentinel = self.tmp / "PRE_PUSH_RAN"
        self.assertEqual(self.drive(FAKE_PRE_PUSH_SENTINEL=str(sentinel)), 0)
        self.assertEqual(self.item()["verdict"]["state"], "green")
        self.assertIn("refs/heads/fix/7-te-runner", self.fork_branches())
        self.assertFalse(sentinel.exists(), "the agent's clone hook ran as the operator")

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

    def test_done_item_records_its_dependencies(self):
        # harmonic-forge#1147 AC1: a set-aside records what it depended on.
        with mock.patch.object(te_run, "kill_receipt", return_value=False):
            self.drive()
        last = self.item()["last_outcome"]
        self.assertEqual(last["reason"], "kill-check-fail")
        self.assertEqual(last["run_id"], self.item()["run_id"])
        self.assertEqual((last["lane1_marker_id"], last["bundle_digest"], last["runner_tree"]),
                         (11, self.digest, "tree0"))
        self.assertEqual(last["runner_source"], str(te_run.te_queue.SOURCE))

    def test_unchanged_item_is_never_set_aside_again(self):
        # harmonic-forge#1147 AC2a: an unchanged item never reaches work_item,
        # so its last_outcome keeps the real reason, not a tool-refusal.
        state = te_run.queue_state.load(self.queue)
        state["items"][0]["last_outcome"] = {
            "run_id": "run-old", "reason": "check-fail", "lane1_marker_id": 11,
            "bundle_digest": self.digest, "runner_tree": "tree0"}
        te_run.queue_state.save(self.queue, state)
        with mock.patch.object(te_run, "work_item") as work:
            self.drive()
        work.assert_not_called()
        item = self.item()
        self.assertEqual(item["status"], "pending")
        self.assertEqual(item["last_outcome"]["reason"], "check-fail")
        self.assertEqual(self.observed(), [])

    def test_dependencies_are_read_before_the_work(self):
        # Sticky-wicket PATCH: a Lane 1 post landing mid-run (12) is not
        # recorded as consumed; last_outcome keeps the marker the run used (11).
        with mock.patch.object(te_run, "kill_receipt", return_value=False), \
                mock.patch.object(te_run.te_queue, "lane1_marker_id", side_effect=[11, 12, 12]):
            self.drive()
        self.assertEqual(self.item()["last_outcome"]["lane1_marker_id"], 11)

    def test_an_unreadable_snapshot_passes_the_item_over_pending(self):
        with mock.patch.object(te_run.te_queue, "lane1_marker_id",
                               side_effect=te_run.te_queue.QueueError("cannot read")), \
                mock.patch.object(te_run, "work_item") as work:
            self.assertEqual(self.drive(), 0)
        work.assert_not_called()
        self.assertEqual(self.item()["status"], "pending")

    def test_an_unreadable_unchanged_check_passes_over_only_that_item(self):
        state = te_run.queue_state.load(self.queue)
        state["items"][0]["last_outcome"] = {
            "run_id": "run-old", "reason": "check-fail", "lane1_marker_id": 11,
            "bundle_digest": self.digest, "runner_tree": "tree0"}
        te_run.queue_state.save(self.queue, state)
        with mock.patch.object(te_run.te_queue, "lane1_marker_id",
                               side_effect=te_run.te_queue.QueueError("cannot read")), \
                mock.patch.object(te_run, "work_item") as work:
            self.assertEqual(self.drive(), 0)
        work.assert_not_called()
        self.assertEqual(self.item()["status"], "pending")

    def test_a_passed_over_item_uses_no_step(self):
        # Sticky-wicket PATCH 2: with one step, an undecidable F7 at the head
        # must not stop F8 from being worked.
        te_run.te_queue.add(self.queue, ["F8"], checks=NO_CHECKS)

        def marker(issue):
            if issue == 7:
                raise te_run.te_queue.QueueError("cannot read")
            return 11
        worked = []
        with mock.patch.object(te_run.te_queue, "issue_number",
                               side_effect=lambda key: int(key[1:])), \
                mock.patch.object(te_run.te_queue, "lane1_marker_id", side_effect=marker), \
                mock.patch.object(te_run, "work_item",
                                  side_effect=lambda item, *a: worked.append(item["id"])
                                  or (_ for _ in ()).throw(te_run.Blocked("stop"))):
            with mock.patch.dict(os.environ, {}):
                te_run.run_queue(self.queue, "1", 1)
        self.assertEqual(worked, ["F8"])
        self.assertEqual(self.item("F7")["status"], "pending")

    def test_a_green_run_clears_a_stale_last_outcome(self):
        state = te_run.queue_state.load(self.queue)
        state["items"][0]["last_outcome"] = {
            "run_id": "run-old", "reason": "check-fail", "lane1_marker_id": 10,
            "bundle_digest": self.digest, "runner_tree": "tree0"}
        te_run.queue_state.save(self.queue, state)
        self.drive()
        item = self.item()
        self.assertEqual(item["verdict"]["state"], "green")
        self.assertNotIn("last_outcome", item)

    def test_an_unreadable_bundle_at_snapshot_is_never_worked(self):
        with mock.patch.object(te_run.te_queue.kill_runner, "bundle_digest",
                               side_effect=te_run.te_queue.kill_runner.RunnerError("gone")), \
                mock.patch.object(te_run, "work_item") as work:
            self.drive()
        work.assert_not_called()
        self.assertEqual(self.item()["status"], "pending")

    def test_failed_receipt_keeps_kill_facts(self):
        # harmonic-forge#1147 AC6 (F1073, run 7): the receipt failed, but the
        # facts it judged stay on the item, so the report names each check
        # instead of "no kill checks ran".
        with mock.patch.object(te_run, "kill_receipt", return_value=False):
            self.drive()
        item = self.item()
        self.assertEqual(item["reason"], "kill-check-fail")
        self.assertEqual(item["facts"]["kill"], [{"ac": "AC1", "base_rc": 1, "head_rc": 0}])
        self.assertIs(item["facts"]["kill_receipt"], False)
        self.assertNotIn("no kill checks ran", item["note"])

    def test_every_failed_clause_is_listed(self):
        # AC8: the reducer lists every failed clause, with no model clause left.
        cases = ({"kill_receipt": False}, {"deny_hits": ["tools/hooks/x.py"]})
        for patched in cases:
            self.setUp_queue_fresh()
            with mock.patch.multiple(te_run, **{k: mock.MagicMock(return_value=v)
                                                for k, v in patched.items()}):
                self.drive()
            reasons = [r["reason"] for r in self.item()["verdict"]["reasons"]]
            self.assertNotIn("preclose-survivor", reasons, patched)
            self.assertTrue(reasons, patched)
    def test_agent_wall_timeout_sets_aside_as_budget_and_the_run_goes_on(self):
        """harmonic-forge#1195 AC7: a hung agent is killed with everything it
        started, set aside as `budget`, and the next item still runs."""
        te_run.te_queue.add(self.queue, ["F8"], checks=NO_CHECKS)
        marker = f"te-runner-timeout-stub-{os.getpid()}-{id(self)}"
        started = time.monotonic()
        with mock.patch.object(te_run, "AGENT_TIMEOUT_S", 8), \
                mock.patch.object(te_run.te_queue, "issue_number",
                                  side_effect=lambda key: int(key[1:])):
            self.drive(FAKE_SLEEP_FOR="harmonic-forge#7 ", FAKE_SLEEP_MARKER=marker)
        # A child left alive holds the agent's stdout open, and the runner would
        # wait on it (the child sleeps 120s): the kill must take the whole group.
        self.assertLess(time.monotonic() - started, 90, "the run waited on the agent's child")
        # Non-vacuous: the hung agent did start a child before it was killed.
        self.assertTrue((self.tmp / "wt" / "harmonic-forge-7r-impl" / "spawned.pid").is_file())
        first, second = self.item("F7"), self.item("F8")
        self.assertEqual((first["status"], first["reason"]), ("done", "budget"), first)
        self.assertEqual(first["note"], "agent wall timeout")
        self.assertEqual(first["verdict"]["state"], "set-aside")
        self.assertEqual(second["status"], "done", second)
        self.assertEqual(second["verdict"]["state"], "green", second)
        survivors = subprocess.run(["pgrep", "-f", marker], capture_output=True, text=True)
        self.assertEqual(survivors.stdout, "", "the agent's child outlived the timeout")
        self.assertEqual(survivors.returncode, 1)

    def test_error_max_turns_is_budget(self):
        self.drive(FAKE_SUBTYPE="error_max_turns")
        item = self.item()
        self.assertEqual(item["reason"], "budget")
        self.assertIn("--max-turns 60", item["note"])
        self.assertFalse(self.fork_branches())

    def test_error_max_budget_is_still_budget(self):
        self.drive(FAKE_SUBTYPE="error_max_budget_usd")
        self.assertEqual((self.item()["reason"], self.item()["note"]),
                         ("budget", "budget exceeded"))
    def setUp_queue_fresh(self):
        self.queue.unlink()
        te_run.te_queue.add(self.queue, ["F7"], checks=NO_CHECKS)
        for stale in (self.tmp / "wt").iterdir():
            shutil.rmtree(stale)
        sh("git", "worktree", "prune", cwd=self.src)
        sh("git", "branch", "-D", "--quiet", "fix/7-te-runner", cwd=self.src) \
            if "fix/7-te-runner" in sh("git", "branch", "--list", "fix/7-te-runner", cwd=self.src) \
            else None
        sh("git", "update-ref", "-d", "refs/te-runner/7", cwd=self.src)
        for ref in sh("git", "for-each-ref", "--format=%(refname)", "refs/te-runner-archive",
                      cwd=self.src).split():
            sh("git", "update-ref", "-d", ref, cwd=self.src)
        shutil.rmtree(self.tmp / "archive", ignore_errors=True)
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
        # harmonic-forge#1107 AC5: the agent carries the runner token; every
        # oracle, kill and PR env carries none.
        envs = []
        real_run = te_run.run

        def spy(argv, **kw):
            envs.append((argv, kw.get("env")))
            return real_run(argv, **kw)

        with mock.patch.object(te_run, "run", spy), \
                mock.patch.object(te_run, "kill_facts", wraps=te_run.kill_facts) as kills:
            self.assertEqual(self.drive(), 0)
        env = self.observed()[0]["env"]
        self.assertEqual(env["GH_TOKEN"], "runner-token")
        self.assertNotIn("GITHUB_TOKEN", env)
        self.assertEqual(env["GH_CONFIG_DIR"], str(self.tmp / "runner-gh"))
        self.assertEqual(env["ANTHROPIC_API_KEY"], "sk-runner")
        self.assertTrue(envs and kills.call_args_list)
        for argv, child in envs:  # env=None is the harness's own host git
            if child is not None:
                self.assertNotIn("GH_TOKEN", child, argv)
        for child in [c.args[3] for c in kills.call_args_list]:
            self.assertNotIn("GH_TOKEN", child)
        pr_calls = [c for c in self.gh_env() if c["argv"][:1] == ["pr"]]
        self.assertTrue(pr_calls)
        self.assertTrue(all(c["GH_TOKEN"] is None for c in pr_calls), pr_calls)

    def gh_env(self) -> list[dict]:
        path = self.bin / "gh_env.jsonl"
        return [json.loads(l) for l in path.read_text().splitlines()] if path.exists() else []

    def test_probe_runs_inside_the_agent_boundary_with_the_token(self):
        self.assertEqual(self.drive(), 0)
        [probe] = [c for c in self.gh_env() if c["argv"][:2] == ["api", "user"]]
        self.assertEqual(probe["GH_TOKEN"], "runner-token")
        wrapped = [json.loads(l) for l in (self.tmp / "bwrap.jsonl").read_text().splitlines()]
        self.assertTrue(any("api" in json.dumps(w) and "user" in json.dumps(w) for w in wrapped))

    def test_probe_wrong_login_or_failure_aborts_before_any_item(self):
        for env in ({"FAKE_LOGIN": "operator"},
                    {"FAKE_PROBE_RC": "1", "FAKE_PROBE_STDERR": "x509: unknown authority"}):
            with self.subTest(env=env), mock.patch.object(te_run, "log") as logged:
                self.assertEqual(self.drive(**env), 3)
                self.assertEqual(self.observed(), [])
                self.assertEqual(self.item()["status"], "pending")
                steps = [c.args[0] for c in logged.call_args_list]
                self.assertIn("probe", steps)
                if "FAKE_PROBE_STDERR" in env:
                    self.assertIn("x509", " ".join(c.args[1] for c in logged.call_args_list))

    def test_missing_loose_or_empty_token_file_aborts(self):
        for setup in (lambda: self.token.chmod(0o644), lambda: self.token.unlink(),
                      lambda: (self.token.write_text(""), self.token.chmod(0o600))):
            with self.subTest():
                if self.token.exists():
                    self.token.chmod(0o600)
                self.token.write_text("runner-token\n")
                self.token.chmod(0o600)
                setup()
                self.assertEqual(self.drive(), 3)
                self.assertEqual(self.observed(), [])
                self.assertEqual(self.gh_env(), [])
                self.assertEqual(self.item()["status"], "pending")

    def test_g3_abort_carries_the_agent_stderr_tail(self):
        noisy = self.bin / "claude"
        noisy.write_text(FAKE_CLAUDE.replace(
            "print(json.dumps({\"type\": \"result\"",
            "sys.stderr.write(''.join(f'line {i}\\n' for i in range(40)) + 'boom: TLS\\n')\n"
            "print(json.dumps({\"type\": \"result\"", 1))
        self.assertEqual(self.drive(FAKE_DROP_HOOK="Stop"), 3)
        note = self.item()["note"]
        self.assertIn("G3", note)
        self.assertIn("boom: TLS", note)
        self.assertNotIn("line 5\n", note)  # the tail only, 20 lines

    def test_nonzero_agent_exit_after_g3_is_logged_not_raised(self):
        rc1 = self.bin / "claude"
        rc1.write_text(FAKE_CLAUDE.rstrip("\n")
                       + "\nsys.stderr.write('boom: TLS\\n')\nsys.exit(1)\n")
        with mock.patch.object(te_run, "log") as logged:
            self.assertEqual(self.drive(), 0)
        self.assertEqual(self.item()["status"], "done")
        self.assertTrue(any(a[0] == "agent" and "claude exited 1" in a[1]
                            and "boom: TLS" in a[1]
                            for a in (c.args for c in logged.call_args_list)))

    def test_nonzero_agent_exit_with_empty_stderr_logs_the_bare_exit(self):
        rc1 = self.bin / "claude"
        rc1.write_text(FAKE_CLAUDE.rstrip("\n") + "\nsys.exit(1)\n")
        with mock.patch.object(te_run, "log") as logged:
            self.assertEqual(self.drive(), 0)
        self.assertIn(("agent", "claude exited 1"),
                      [c.args for c in logged.call_args_list])

    def test_agent_stderr_is_written_beside_the_stream_log(self):
        log_path = self.tmp / "s" / "stream.jsonl"
        log_path.parent.mkdir()
        done = subprocess.CompletedProcess([], 0, "", "warn: x\n")
        with mock.patch.object(te_run, "_agent_process", return_value=done), \
                mock.patch.object(te_run, "source_objects", return_value=self.tmp), \
                self.assertRaises(te_run.Abort):
            te_run.run_agent(self.tmp, "p", "1", {}, log_path, self.tmp / "t")
        self.assertEqual((self.tmp / "s" / "stream.jsonl.stderr").read_text(), "warn: x\n")

    def test_token_with_special_mode_bits_is_refused(self):
        self.token.chmod(0o1600)
        self.assertIsNone(te_run.runner_token())

    def test_non_utf8_agent_stderr_still_aborts_with_g3_and_writes_logs(self):
        bad = self.bin / "claude"
        bad.write_text("#!/usr/bin/env python3\nimport sys\n"
                       "sys.stdin.read()\nsys.stderr.buffer.write(b'\\xff\\xfe')\n")
        bad.chmod(0o755)
        log_path = self.tmp / "s2" / "stream.jsonl"
        log_path.parent.mkdir()
        with mock.patch.object(te_run, "claude_argv", return_value=[str(bad)]), \
                mock.patch.object(te_run, "source_objects", return_value=self.tmp), \
                self.assertRaises(te_run.Abort) as caught:
            te_run.run_agent(self.tmp, "p", "1", dict(os.environ), log_path, self.tmp / "t2")
        self.assertIn("G3", str(caught.exception))
        self.assertTrue((self.tmp / "s2" / "stream.jsonl.stderr").exists())

    def test_missing_or_loose_key_file_refuses(self):
        key = te_run.sandbox.RUNNER_KEY
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
        i = argv.index("--max-turns")  # harmonic-forge#1195 AC7
        self.assertEqual(argv[i:i + 2], ["--max-turns", "60"])
        self.assertNotIn("--bare", argv)
        i = argv.index("--setting-sources")  # harmonic-forge#1195 rework
        self.assertEqual(argv[i:i + 2], ["--setting-sources", ""])
        self.assertEqual(Path(self.observed()[0]["cwd"]),
                         (self.tmp / "wt" / "harmonic-forge-7r-impl").resolve())

    def test_missing_settings_file_aborts(self):
        with mock.patch.object(te_run, "SETTINGS", self.tmp / "missing.json"):
            self.assertEqual(self.drive(), 3)
        self.assertEqual(self.item()["status"], "blocked")
        self.assertEqual(self.observed(), [])

    # --- AC5 G3 ------------------------------------------------------------------------

    def test_missing_stop_hook_event_aborts_the_run(self):
        te_run.te_queue.add(self.queue, ["F8"], checks=NO_CHECKS)
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
        te_run.te_queue.add(self.queue, ["F8"], checks=NO_CHECKS)
        with mock.patch.object(te_run.te_queue, "issue_number",
                               side_effect=lambda key: int(key[1:])):
            self.drive(FAKE_MISE_RC="1")
        seen = self.observed()
        self.assertEqual([s["prompt"].split()[1] for s in seen],
                         ["harmonic-forge#7", "harmonic-forge#8"])
        self.assertEqual([s["statuses"] for s in seen],
                         [["in_progress", "pending"], ["done", "in_progress"]])

    def clone_path(self) -> Path:
        return self.tmp / "wt" / "harmonic-forge-7r-impl"

    def earlier_attempt(self) -> str:
        """Every local trace of attempt 1 plus its fork branch: a clone with a
        commit and an uncommitted file, the pinned ref and a local branch."""
        clone = self.clone_path()
        sh("git", "clone", "-q", "--shared", str(self.src), str(clone), cwd=self.tmp)
        sh("git", "checkout", "-q", "-b", "fix/7-te-runner", cwd=clone)
        (clone / "widget.py").write_text("def widget():\n    return 3\n")
        commit(clone, "attempt 1")
        (clone / "notes.txt").write_text("uncommitted\n")
        sh("git", "fetch", "-q", str(clone), "HEAD", cwd=self.src)
        head = sh("git", "rev-parse", "FETCH_HEAD", cwd=self.src)
        sh("git", "update-ref", "refs/te-runner/7", head, cwd=self.src)
        sh("git", "branch", "fix/7-te-runner", head, cwd=self.src)
        sh("git", "push", "-q", str(self.fork), f"{head}:refs/heads/fix/7-te-runner",
           cwd=self.src)
        return head

    def test_an_earlier_attempt_is_archived_and_attempt_2_runs(self):
        """harmonic-forge#1195 AC9."""
        old = self.earlier_attempt()
        self.assertEqual(self.drive(), 0)
        item = self.item()
        self.assertEqual((item["attempt"], item["branch"]), (2, "fix/7-te-runner-a2"))
        self.assertEqual(item["verdict"]["state"], "green", item)
        archive = self.tmp / "archive" / "7" / "1"
        self.assertIn(b"attempt 1", (archive / "commits.patch").read_bytes())
        self.assertIn(b"return 3", (archive / "commits.patch").read_bytes())
        self.assertIn(b"notes.txt", (archive / "status.txt").read_bytes())
        traces = json.loads((archive / "traces.json").read_text())
        self.assertEqual((traces["attempt"], traces["ref"]), (1, old))
        self.assertEqual(sh("git", "rev-parse", "refs/te-runner-archive/7/1", cwd=self.src), old)
        self.assertNotEqual(sh("git", "rev-parse", "refs/te-runner/7", cwd=self.src), old)
        self.assertEqual(sh("git", "branch", "--list", "fix/7-te-runner*", cwd=self.src), "")
        # The fork's attempt-1 branch is untouched; attempt 2 is pushed beside it.
        self.assertEqual(sh("git", "rev-parse", "refs/heads/fix/7-te-runner", cwd=self.fork), old)
        self.assertIn("refs/heads/fix/7-te-runner-a2", self.fork_branches())
        self.assertFalse(list((self.tmp / "archive" / "7").glob(".tmp-*")))
        create = next(c for c in self.gh_calls() if c[:2] == ["pr", "create"])
        self.assertEqual(create[create.index("--head") + 1],
                         "vitalharmony-runner:fix/7-te-runner-a2")

    def test_an_open_runner_pr_still_refuses_and_archives_nothing(self):
        self.earlier_attempt()
        open_pr = json.dumps([{"number": 41, "title": "te-runner: x (harmonic-forge#7)",
                               "headRefName": "fix/7-te-runner"}])
        self.assertEqual(self.drive(FAKE_OPEN_PRS=open_pr), 3)
        self.assertEqual(self.item()["status"], "blocked")
        self.assertIn("runner PR #41", self.item()["note"])
        self.assertTrue(self.clone_path().exists())
        self.assertFalse((self.tmp / "archive" / "7").exists())
        self.assertEqual(self.observed(), [])

    def test_another_issues_open_pr_does_not_refuse(self):
        open_pr = json.dumps([{"number": 5, "title": "te-runner: y (harmonic-forge#77)",
                               "headRefName": "fix/77-te-runner"}])
        self.assertEqual(self.drive(FAKE_OPEN_PRS=open_pr), 0)

    def test_unreadable_pr_list_blocks_the_item(self):
        self.assertEqual(self.drive(FAKE_PR_LIST_RC="1"), 3)
        self.assertIn("cannot list the runner's open PRs", self.item()["note"])
        self.assertEqual(self.observed(), [])

    def test_a_fork_branch_alone_moves_to_the_next_attempt(self):
        sh("git", "push", "-q", str(self.fork), "HEAD:refs/heads/fix/7-te-runner", cwd=self.src)
        before = sh("git", "rev-parse", "refs/heads/fix/7-te-runner", cwd=self.fork)
        self.assertEqual(self.drive(), 0)
        self.assertEqual(self.item()["branch"], "fix/7-te-runner-a2")
        self.assertEqual(sh("git", "rev-parse", "refs/heads/fix/7-te-runner", cwd=self.fork),
                         before)
        self.assertEqual(te_run.archived_attempts(7), 0)

    def test_a_fork_name_already_taken_increments_the_attempt(self):
        self.earlier_attempt()
        sh("git", "push", "-q", str(self.fork), "HEAD:refs/heads/fix/7-te-runner-a2",
           cwd=self.src)
        self.assertEqual(self.drive(), 0)
        self.assertEqual((self.item()["attempt"], self.item()["branch"]),
                         (3, "fix/7-te-runner-a3"))

    def test_a_crash_before_the_rename_never_advances_the_attempt(self):
        """AC9's crash rule: killed between the ref move and the clone removal."""
        old = self.earlier_attempt()
        real_rmtree = shutil.rmtree
        clone = self.clone_path()

        def crash(path, *args, **kw):
            if Path(path) == clone:
                raise KeyboardInterrupt("killed")
            return real_rmtree(path, *args, **kw)
        with mock.patch.object(te_run.shutil, "rmtree", crash), \
                self.assertRaises(KeyboardInterrupt):
            te_run.archive_previous_attempt(7, None, self.src)
        root = self.tmp / "archive" / "7"
        self.assertFalse((root / "1").exists())
        self.assertEqual(len(list(root.glob(".tmp-1-*"))), 1)
        self.assertEqual(te_run.archived_attempts(7), 0)
        self.assertEqual(sh("git", "rev-parse", "refs/te-runner-archive/7/1", cwd=self.src), old)
        # The next run resumes to the same, consistent attempt number.
        self.assertEqual(te_run.archive_previous_attempt(7, None, self.src),
                         (2, "fix/7-te-runner-a2"))
        self.assertTrue((root / "1" / "commits.patch").is_file())
        self.assertIn(b"return 3", (root / "1" / "commits.patch").read_bytes())
        self.assertFalse(list(root.glob(".tmp-*")))
        self.assertFalse(clone.exists())

    def test_a_crash_after_the_clone_removal_keeps_the_archive(self):
        self.earlier_attempt()
        with mock.patch.object(te_run.os, "rename", side_effect=KeyboardInterrupt("killed")), \
                self.assertRaises(KeyboardInterrupt):
            te_run.archive_previous_attempt(7, None, self.src)
        self.assertEqual(te_run.archived_attempts(7), 0)
        self.assertFalse(self.clone_path().exists())
        self.assertEqual(te_run.archive_previous_attempt(7, None, self.src)[0], 2)
        patch = self.tmp / "archive" / "7" / "1" / "commits.patch"
        self.assertIn(b"return 3", patch.read_bytes())

    def test_a_clone_with_work_and_an_empty_archive_is_refused(self):
        self.earlier_attempt()
        empty = subprocess.CompletedProcess([], 0, b"", b"")
        status = subprocess.CompletedProcess([], 0, b"?? notes.txt\n", b"")
        with mock.patch.object(te_run, "_clone_git",
                               side_effect=lambda clone, *a: status if a[0] == "status" else empty), \
                self.assertRaisesRegex(te_run.Blocked, "archive came out empty"):
            te_run.archive_previous_attempt(7, None, self.src)
        self.assertTrue(self.clone_path().exists())
        self.assertEqual(te_run.archived_attempts(7), 0)

    def test_archive_git_runs_inside_the_boundary(self):
        self.earlier_attempt()
        log = self.tmp / "bwrap.jsonl"
        log.write_text("")
        te_run.archive_previous_attempt(7, None, self.src)
        wrapped = [json.loads(l) for l in log.read_text().splitlines()]
        self.assertTrue(any("--no-pager" in w and "log" in w for w in wrapped), wrapped)
    def test_a_pinned_ref_alone_is_archived(self):
        sh("git", "update-ref", "refs/te-runner/7", "HEAD", cwd=self.src)
        self.assertEqual(self.drive(), 0)
        self.assertEqual(self.item()["branch"], "fix/7-te-runner-a2")
        self.assertTrue((self.tmp / "archive" / "7" / "1" / "traces.json").is_file())

    def test_unreadable_runner_fork_blocks_the_item(self):
        with mock.patch.object(te_run, "FORK_URL", str(self.tmp / "no-such-fork.git")):
            self.assertEqual(self.drive(), 3)
        self.assertIn("cannot read the runner fork", self.item()["note"])
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
                return subprocess.CompletedProcess(argv, 1, "", "denied")
            return real_run(argv, **kw)
        with mock.patch.object(te_run, "run", watching):
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
                te_run.te_queue.add(self.queue, ["F8"], checks=NO_CHECKS)
            self.assertEqual(te_run.main(["run", "--queue", str(self.queue),
                                          "--max-budget-usd", "1"]), 2)
        self.assertEqual(self.item()["status"], "pending")
        te_run.te_queue.add(self.queue, ["F8"], checks=NO_CHECKS)  # free again once the holder ends

    def test_an_unexpected_error_blocks_the_item_never_in_progress(self):
        boom = UnicodeDecodeError("utf-8", b"\\xe9", 0, 1, "invalid continuation byte")
        with mock.patch.object(te_run, "deny_hits", side_effect=boom):
            self.assertEqual(self.drive(), 3)
        item = self.item()
        self.assertEqual(item["status"], "blocked")
        self.assertIn("invalid continuation byte", item["note"])

    def test_max_steps_bounds_the_run(self):
        te_run.te_queue.add(self.queue, ["F8"], checks=NO_CHECKS)
        with mock.patch.dict(os.environ, {"FAKE_MISE_RC": "1"}):
            te_run.run_queue(self.queue, "1", 1)
        self.assertEqual(self.item("F8")["status"], "pending")


@unittest.skipUnless(shutil.which("bwrap"), "real bwrap is not installed")
class RealBoundary(unittest.TestCase):
    """Real bwrap, no stub (harmonic-forge#1098)."""

    def setUp(self):
        self.enter = lambda p: (p.start(), self.addCleanup(p.stop))
        self.enter(mock.patch.dict(os.environ, {"TE_RUNNER_BWRAP": ""}))
        # Under home, not /tmp: the boundary covers /tmp with a fresh tmpfs.
        base = Path.home() / ".cache" / "te-runner-test-scratch"
        base.mkdir(parents=True, exist_ok=True)
        self.tmp = Path(tempfile.mkdtemp(dir=base))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_real_bwrap_agent_cannot_write_trusted_state(self):
        marker = f"TE_RUNNER_LEAK_{os.getpid()}"
        home = Path.home()
        targets = [home / ".claude" / "state" / marker,
                   home / ".local" / "share" / "te-runner" / marker,
                   home / ".config" / "te-runner" / marker,
                   Path(te_run.SOURCE) / marker,
                   te_run.sandbox.TRUSTED_LINK.resolve() / marker,
                   te_run.sandbox.RUNNER_GH / marker]
        for target in targets:
            self.addCleanup(lambda t=target: t.unlink(missing_ok=True))
        # The agent profile, the one the agent runs under. Its read-only binds
        # put some of these parents back in view, so each one that exists on the
        # host is checked visible first: a write must fail as read-only, not
        # because the directory is missing.
        visible = [t.parent for t in targets[4:] if t.parent.is_dir()]
        script = "".join(f"test -d '{p}' && echo seen >> seen; " for p in visible)
        script += "; ".join(f"echo x > '{t}' 2>/dev/null" for t in targets) + "; echo ok > inside"
        subprocess.run(te_run.sandbox.wrap(["sh", "-c", script], self.tmp, profile="agent"),
                       check=True)
        self.assertTrue((self.tmp / "inside").exists())
        seen = (self.tmp / "seen").read_text().split() if visible else []
        self.assertEqual(len(seen), len(visible), "a bound trusted path was not in view")
        leaked = [str(t) for t in targets if t.exists()]
        self.assertEqual(leaked, [], "a write from inside the boundary reached a trusted path")

    def test_real_bwrap_agent_commit_reaches_the_harness(self):
        src = self.tmp / "src"
        src.mkdir()
        sh("git", "init", "-q", "-b", "main", cwd=src)
        (src / "a.txt").write_text("1\n")
        commit(src, "base")
        clone = self.tmp / "clone"
        sh("git", "clone", "-q", "--shared", "--no-checkout", str(src), str(clone), cwd=self.tmp)
        sh("git", "checkout", "-q", "-b", "fix/7-te-runner", "main", cwd=clone)
        script = ("echo 2 > a.txt && git add a.txt && "
                  "git -c user.name=a -c user.email=a@a commit -q -m agent")
        subprocess.run(te_run.sandbox.wrap(["sh", "-c", script], clone, profile="agent",
                                           inputs=(src / ".git" / "objects",)), check=True)
        sh("git", "fetch", "-q", str(clone), "HEAD", cwd=src)
        head = sh("git", "rev-parse", "FETCH_HEAD", cwd=src)
        self.assertNotEqual(head, sh("git", "rev-parse", "main", cwd=src))
        self.assertEqual(sh("git", "show", f"{head}:a.txt", cwd=src), "2")

    def fresh_claude_home(self) -> Path:
        """A `.claude/` with neither `state/` nor `projects/`, the CI runner's
        shape (harmonic-forge#1132), patched in for the real one."""
        claude = self.tmp / "home" / ".claude"
        claude.mkdir(parents=True)
        for name, value in (("CLAUDE_HOME", claude), ("CLAUDE_STATE", claude / "state"),
                            ("TRANSCRIPTS", claude / "projects")):
            self.enter(mock.patch.object(te_run.sandbox, name, value))
        return claude

    def test_real_bwrap_agent_runs_without_claude_mount_points(self):
        claude = self.fresh_claude_home()
        work = self.tmp / "work"
        work.mkdir()
        done = subprocess.run(te_run.sandbox.wrap(["true"], work, profile="agent"),
                              capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(sorted(p.name for p in claude.iterdir()), ["projects", "state"])
        self.assertEqual((claude / "state").stat().st_mode & 0o777, 0o700)

    def test_real_bwrap_agent_cannot_read_host_state(self):
        claude = self.fresh_claude_home()
        (claude / "state").mkdir(mode=0o700)
        (claude / "state" / "secret").write_text("host\n")
        work = self.tmp / "work"
        work.mkdir()
        script = f"ls -A '{claude / 'state'}' > listing"
        subprocess.run(te_run.sandbox.wrap(["sh", "-c", script], work, profile="agent"),
                       check=True)
        self.assertEqual((work / "listing").read_text(), "")


    def here(self, *args: str, cwd: Path) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(Path(te_run.__file__).resolve().parent
                                                   / "sandbox.py"), "here", *args],
                              cwd=cwd, capture_output=True, text=True)

    def test_real_bwrap_pinned_check_cannot_run_code_from_the_clone(self):
        # The agent's clone persists at a predictable path; a pinned check that
        # reaches for code there must fail inside the oracle boundary.
        clone = self.tmp / "harmonic-forge-7r-impl"
        clone.mkdir()
        (clone / "x.py").write_text("print('AGENT CODE RAN')\n")
        tree = self.tmp / "tree"
        tree.mkdir()
        done = self.here("--", "python3", str(clone / "x.py"), cwd=tree)
        self.assertNotEqual(done.returncode, 0)
        self.assertNotIn("AGENT CODE RAN", done.stdout)

    def test_real_bwrap_oracle_reads_no_operator_or_runner_state(self):
        tree = self.tmp / "tree"
        tree.mkdir()
        home = te_run.sandbox.HOME
        probes = [home / ".claude", home / ".claude.json", home / ".config" / "te-runner",
                  home / ".local" / "share" / "te-runner", Path(te_run.SOURCE)]
        script = "; ".join(f"test -e '{p}' && echo 'SEEN {p}'" for p in probes) + "; true"
        done = self.here("--", "sh", "-c", script, cwd=tree)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertNotIn("SEEN", done.stdout)

    def test_real_bwrap_oracle_sees_its_bundle_snapshot_under_tmp(self):
        # The snapshot lives in a /tmp scratch, which the boundary covers with a
        # tmpfs; the generated check names it as a read-only input.
        with tempfile.TemporaryDirectory(prefix="te-run-") as scratch_name:
            scratch = Path(scratch_name)
            src = self.tmp / "src"
            src.mkdir()
            sh("git", "init", "-q", "-b", "main", cwd=src)
            (src / "widget.py").write_text("x = 1\n")
            commit(src, "base")
            base = sh("git", "rev-parse", "HEAD", cwd=src)
            (src / "widget.py").write_text("x = 2\n")
            commit(src, "head")
            head = sh("git", "rev-parse", "HEAD", cwd=src)
            bundle = scratch / "bundle"
            (bundle / "files").mkdir(parents=True)
            (bundle / "files" / "t.py").write_text("print('BUNDLE TEST RAN')\n")
            (bundle / "checks.json").write_text(json.dumps([{"ac": "AC1",
                                                             "test": ["python3", "t.py"]}]))
            with mock.patch.object(te_run, "SOURCE", src):
                path = te_run.kill_check_checks(bundle, base, head, scratch)
            [check] = json.loads(path.read_text())
            tree = self.tmp / "tree"
            tree.mkdir()
            done = subprocess.run(check["test"], cwd=tree, capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("BUNDLE TEST RAN", done.stdout)


@unittest.skipUnless(shutil.which("bwrap"), "real bwrap is not installed")
class RealPipeline(Pipeline):
    """The whole pipeline under real bwrap: the agent must be able to commit
    inside the boundary and the harness must see that commit (#1098). Fixtures
    live under home, since the boundary covers /tmp with a fresh tmpfs."""

    def setUp(self):
        base = Path.home() / ".cache" / "te-runner-test-scratch"
        base.mkdir(parents=True, exist_ok=True)
        real_mkdtemp = tempfile.mkdtemp
        with mock.patch.object(tempfile, "mkdtemp", lambda *a, **k: real_mkdtemp(dir=base)):
            super().setUp()
        self.enter(mock.patch.dict(os.environ, {"TE_RUNNER_BWRAP": ""}))
        # The fake claude and mise live in the fixture, outside the allowlist:
        # they join the read-only toolchain, and nothing else of the fixture does.
        sb = te_run.sandbox
        self.enter(mock.patch.object(sb, "TOOLCHAIN", (*sb.TOOLCHAIN, self.bin)))

    def test_real_bwrap_agent_commit_reaches_the_harness(self):
        self.assertEqual(self.drive(), 0, self.item().get("note"))
        item = self.item()
        base = sh("git", "rev-parse", "origin/main", cwd=self.src)
        self.assertEqual(item["verdict"]["state"], "green", item.get("note"))
        self.assertNotEqual(item["head_sha"], base)
        self.assertIn("refs/heads/fix/7-te-runner", self.fork_branches())


# Only the green path runs under real bwrap; Pipeline's other tests use the stub.
for _name in [n for n in vars(Pipeline) if n.startswith("test_")]:
    setattr(RealPipeline, _name, unittest.skip("covered under the stub bwrap")(lambda self: None))

# harmonic-forge#1195: the timeout's process-tree kill and the archive's
# in-boundary git are what real bwrap changes, so both run under it too.
RealPipeline.test_real_bwrap_agent_wall_timeout_leaves_no_process = \
    Pipeline.test_agent_wall_timeout_sets_aside_as_budget_and_the_run_goes_on
RealPipeline.test_real_bwrap_earlier_attempt_is_archived = \
    Pipeline.test_an_earlier_attempt_is_archived_and_attempt_2_runs


def shadow_repo(case: unittest.TestCase) -> tuple[Path, str, str, Path]:
    """A repo whose head fixes `widget`, and a bundle whose check imports the
    repo (`import widget`) by path, `python3 -m unittest tests/test_widget.py`:
    the form F1008's checks use, which `-t .` discovery would not exercise."""
    tmp = Path(tempfile.mkdtemp())
    case.addCleanup(shutil.rmtree, tmp, True)
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
    (bundle / "files" / "tests").mkdir(parents=True)
    (bundle / "files" / "tests" / "__init__.py").write_text("")
    (bundle / "files" / "tests" / "test_widget.py").write_text(
        "import unittest\nimport widget\n\n\nclass W(unittest.TestCase):\n"
        "    def test_two(self):\n        self.assertEqual(widget.widget(), 2)\n")
    (bundle / "checks.json").write_text(json.dumps(
        [{"ac": "AC1", "test": ["python3", "-m", "unittest", "tests/test_widget.py"]}]))
    return src, base, head, bundle


class OracleImports(unittest.TestCase):
    """harmonic-forge#1112 AC2: no oracle process runs with PYTHONSAFEPATH, so a
    check that imports the repo works; shadowing is the deny check's."""

    def test_oracle_env_never_carries_pythonsafepath(self):
        env = te_run.oracle_env({"PYTHONSAFEPATH": "1", "PATH": "/usr/bin"})
        self.assertNotIn("PYTHONSAFEPATH", env)
        self.assertEqual(env["PATH"], "/usr/bin")

    def test_unittest_path_check_imports_the_repo(self):
        src, base, head, bundle = shadow_repo(self)
        env = {**os.environ, "HRSE_SCRATCH_ROOT": private_scratch_root(self),
               "PYTHONSAFEPATH": "1"}  # inherited: oracle_env must drop it
        digest = te_run.te_queue.kill_runner.bundle_digest(bundle)
        with mock.patch.object(te_run, "SOURCE", src):
            kill = te_run.kill_facts(7, base, head, env, bundle, digest)
            self.assertEqual(te_run.kill_facts(7, base, head, env, bundle, "0" * 64), [],
                             "a bundle other than the pinned one must not count")
        self.assertEqual(kill, [{"ac": "AC1", "base_rc": 1, "head_rc": 0}])

    def test_shadowing_head_is_a_deny_hit(self):
        src, base, _head, _bundle = shadow_repo(self)
        (src / "unittest.py").write_text("raise SystemExit(0)\n")
        commit(src, "shadow")
        head = sh("git", "rev-parse", "HEAD", cwd=src)
        cwd = os.getcwd()
        os.chdir(src)
        self.addCleanup(os.chdir, cwd)
        with mock.patch.object(te_run, "SOURCE", src):
            self.assertEqual(te_run.deny_hits(base, head), ["unittest.py"])


@unittest.skipUnless(shutil.which("bwrap"), "real bwrap is not installed")
class RealKillReceipt(unittest.TestCase):
    """The real kill_check.py path. A head that adds a `unittest.py` shadow is
    the deny check's to refuse (harmonic-forge#1112 AC2), not the oracle's:
    see `OracleImports.test_shadowing_head_is_a_deny_hit`."""

    def receipt(self, fix: str, shadow: bool) -> tuple[bool, dict]:
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
        (src / "widget.py").write_text(f"def widget():\n    return {fix}\n")
        if shadow:
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
        # Real bwrap (no stub): the bundle sits under /tmp, which the boundary
        # covers, so the check works only through its read-only input.
        env = {**os.environ, "HOME": str(tmp / "home"), "TE_RUNNER_BWRAP": "",
               "HRSE_SCRATCH_ROOT": private_scratch_root(self)}
        with mock.patch.object(te_run, "SOURCE", src):
            passed = te_run.kill_receipt(7, base, head, env, scratch, bundle)
        [path] = (tmp / "home" / ".claude" / "state" / "preclose").glob("*_7.kill.json")
        return passed, json.loads(path.read_text())

    def test_the_same_setup_with_a_real_fix_earns_a_receipt(self):
        # The positive control: the fixture itself works end to end, so the
        # shadow test below fails for the shadow, never for its setup.
        passed, receipt = self.receipt("2", shadow=False)
        self.assertTrue(passed, receipt)
        self.assertEqual(receipt["status"], "pass", receipt)



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
        # Wrapped by the trusted sandbox.py (absolute path, never the tree's copy).
        self.assertEqual(check["test"][1:8], [str(Path(te_run.__file__).resolve().parent
                                                  / "sandbox.py"), "here", "--ro", str(bundle),
                                              "--", "sh", "-c"])
        self.assertIn(str(bundle / "files"), check["test"][8])
        self.assertEqual(check["test"][-2:], ["python3", "t.py"])
        sh("git", "apply", "--check", check["patch"], cwd=src)  # applies to the head
        sh("git", "apply", check["patch"], cwd=src)
        self.assertIn("return 1", (src / "widget.py").read_text())


def pipeline_only(cls):
    """A Pipeline subclass that runs only its own tests, never Pipeline's again."""
    for name in [n for n in vars(Pipeline) if n.startswith("test_")]:
        if name not in vars(cls):
            setattr(cls, name, None)
    return cls


@unittest.skipUnless(shutil.which("bwrap") and shutil.which("mise"), "real bwrap or mise missing")
class MiseEnv(unittest.TestCase):
    """harmonic-forge#1112 AC1: mise must trust a fresh clone inside the boundary."""

    def test_check_runs_past_config_loading_in_a_fresh_clone(self):
        base = Path.home() / ".cache" / "te-runner-test-scratch"
        base.mkdir(parents=True, exist_ok=True)
        tmp = Path(tempfile.mkdtemp(dir=base))
        self.addCleanup(shutil.rmtree, tmp, True)
        src = tmp / "src"
        src.mkdir()
        sh("git", "init", "-q", "-b", "main", cwd=src)
        (src / "mise.toml").write_text('[tasks.check]\nrun = "echo MISE-CHECK-RAN"\n')
        commit(src, "base")
        clone = tmp / "clone"
        sh("git", "clone", "-q", "--shared", str(src), str(clone), cwd=tmp)
        with mock.patch.dict(os.environ, {"TE_RUNNER_BWRAP": ""}):
            argv = te_run.sandbox.wrap(["mise", "run", "check"], clone,
                                       inputs=(src / ".git" / "objects",))
        # The boundary's /tmp is a private tmpfs: an inherited TMPDIR (mise run
        # check sets one under /tmp) names a path that does not exist inside it.
        env = {k: v for k, v in os.environ.items() if k != "TMPDIR"}
        done = subprocess.run(argv, capture_output=True, text=True, env=env)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("MISE-CHECK-RAN", done.stdout)


@pipeline_only
class CloneOrigin(Pipeline):
    def test_origin_is_the_github_url(self):
        self.drive()
        clone = self.tmp / "wt" / "harmonic-forge-7r-impl"
        self.assertEqual(sh("git", "remote", "get-url", "origin", cwd=clone),
                         "https://github.com/vitalharmony/harmonic-forge.git")
        # Objects are still borrowed from SOURCE through alternates.
        self.assertTrue((clone / ".git" / "objects" / "info" / "alternates").is_file())


def checked(test_out: str = "", test_rc: int = 0, **steps: int) -> dict:
    """A run_steps-shaped result: the test step's output and rc, and any other
    steps' rcs."""
    rows = [{"name": "run_tests", "rc": test_rc}] + [{"name": k, "rc": v} for k, v in steps.items()]
    return {"steps": rows, "step_failures": [f"{k} exited {v}" for k, v in steps.items() if v],
            "test_rc": test_rc, **te_run.parse_check(test_out)}


@pipeline_only
class BaseRelative(Pipeline):
    """harmonic-forge#1112 AC4 and pitch changes A/B, reforged per step."""

    def test_a_failure_on_base_too_is_not_a_check_fail(self):
        out = "FAIL: test_host (test_lane.Host.test_host)\\nRan 5 tests in 0.1s\\n"
        self.drive(FAKE_MISE_RC="1", FAKE_MISE_OUT=out)
        item = self.item()
        self.assertEqual(item["facts"]["check_rc"], 0, item["facts"])
        self.assertEqual(item["facts"]["check_base_failures"],
                         ["test_host (test_lane.Host.test_host)"])
        self.assertEqual(item["facts"]["check_new_failures"], [])
        self.assertEqual(item["verdict"]["state"], "green", item["verdict"])
        cached = list((self.tmp / "base-check").glob("*/result.json"))
        self.assertEqual(len(cached), 1)

    def test_head_crash_before_tests_on_failing_base_is_a_fail(self):
        base = checked("ImportError: no module\n", 1)  # ran 0: only change A can catch it
        rc, new, _base = te_run.judge_check(checked("ImportError: no module\n", 1), base)
        self.assertNotEqual(rc, 0)
        self.assertTrue(new)

    def test_module_level_error_on_head_always_counts_as_new(self):
        test_id = "test_x (unittest.loader._FailedTest.test_x)"
        base = checked(f"ERROR: {test_id}\nRan 3 tests in 0.1s\n", 1)
        rc, new, _base = te_run.judge_check(checked(f"ERROR: {test_id}\nRan 3 tests in 0.1s\n",
                                                    1), base)
        self.assertNotEqual(rc, 0)
        self.assertIn(test_id, new)

    def test_head_running_fewer_tests_than_base_fails(self):
        base = checked("Ran 10 tests in 1s\n")
        rc, new, _base = te_run.judge_check(checked("Ran 4 tests in 1s\n"), base)
        self.assertNotEqual(rc, 0)
        self.assertTrue(new)

    def test_a_new_failure_on_the_head_is_a_check_fail(self):
        base = checked("FAIL: a (m.C.a)\nRan 2 tests in 1s\n", 1)
        head = checked("FAIL: a (m.C.a)\nFAIL: b (m.C.b)\nRan 2 tests in 1s\n", 1)
        rc, new, base_failures = te_run.judge_check(head, base)
        self.assertNotEqual(rc, 0)
        self.assertEqual(new, ["b (m.C.b)"])
        self.assertEqual(base_failures, ["a (m.C.a)"])

    def test_head_test_step_exiting_abnormally_is_a_fail(self):
        # Sticky-wicket patch 5: the base exits 1 with test_old failing; the
        # head exits 2 with the same failure listed and as many tests run. An
        # exit other than 0 or 1 is a crash, never a matching failure set.
        base = checked("FAIL: test_old (m.C.test_old)\nRan 2 tests in 1s\n", 1)
        head = checked("FAIL: test_old (m.C.test_old)\nRan 2 tests in 1s\n", 2)
        rc, new, _base = te_run.judge_check(head, base)
        self.assertNotEqual(rc, 0)
        self.assertIn("test step exited 2", new)

    def test_head_breaking_a_later_step_fails_on_a_failing_base(self):
        # Sticky-wicket kill check: the base fails one unittest, the head breaks
        # check_cross_registry. Every step runs, so the head must fail.
        base = checked("FAIL: a (m.C.a)\nRan 2 tests in 1s\n", 1, check_cross_registry=0)
        head = checked("FAIL: a (m.C.a)\nRan 2 tests in 1s\n", 1, check_cross_registry=1)
        rc, new, _base = te_run.judge_check(head, base)
        self.assertNotEqual(rc, 0)
        self.assertEqual(new, ["step check_cross_registry exited 1"])

    def test_a_step_failing_on_base_and_head_with_different_rc_fails(self):
        # Sticky-wicket kill check: no task-level name masks the head.
        base = checked("Ran 2 tests in 1s\n", 0, context_budget_ratchet=1)
        head = checked("Ran 2 tests in 1s\n", 0, context_budget_ratchet=2)
        rc, new, _base = te_run.judge_check(head, base)
        self.assertEqual(rc, 2)
        self.assertEqual(new, ["step context_budget_ratchet exited 2"])

    def test_every_step_runs_after_a_failing_one(self):
        steps = (("first", "exit 1"), ("run_tests", "echo 'Ran 1 test in 0.0s'"),
                 ("last", "exit 3"))
        with mock.patch.object(te_run, "CHECK_STEPS", steps), \
                mock.patch.object(te_run, "STEP_PREFIX", ()):
            sha = sh("git", "rev-parse", "HEAD", cwd=self.src)
            result = te_run.check_sha(sha, dict(os.environ), self.tmp / "ev")
        self.assertEqual([s["rc"] for s in result["steps"]], [1, 0, 3])
        self.assertEqual(result["step_failures"], ["first exited 1", "last exited 3"])
        self.assertFalse(result["complete"])  # rc 3 is a crash

    def test_non_test_step_failing_on_the_base_aborts_the_run(self):
        steps = (("run_tests", "echo 'Ran 1 test in 0.0s'"), ("lint", "exit 1"))
        with mock.patch.object(te_run, "CHECK_STEPS", steps), \
                mock.patch.object(te_run, "STEP_PREFIX", ()):
            self.assertEqual(self.drive(), 3)
        item = self.item()
        self.assertEqual(item["status"], "blocked")
        self.assertIn("lint exited 1", item["note"])

    def test_untracked_mise_local_toml_in_the_agent_clone_never_reaches_the_check(self):
        # Sticky-wicket kill check (survivor 6): the head check runs in a fresh
        # clone of the pinned head SHA, never the agent's working tree.
        steps = (("run_tests", "if [ -e mise.local.toml ]; then echo POISONED; exit 1; fi\n"
                               "echo 'Ran 1 test in 0.0s'"),)
        with mock.patch.object(te_run, "CHECK_STEPS", steps), \
                mock.patch.object(te_run, "STEP_PREFIX", ()):
            self.drive(FAKE_UNTRACKED="mise.local.toml")
        clone = self.tmp / "wt" / "harmonic-forge-7r-impl"
        self.assertTrue((clone / "mise.local.toml").is_file())
        item = self.item()
        self.assertEqual(item["facts"]["check_rc"], 0, item["facts"])
        self.assertEqual(item["verdict"]["state"], "green", item["verdict"])


class BaseCache(unittest.TestCase):
    """Sticky-wicket REFORGE step 5: lock, atomic write, never a crashed run."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        for target, name, value in ((te_run, "BASE_CHECK_ROOT", self.tmp / "base-check"),):
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_concurrent_base_checks_never_cache_a_crashed_run(self):
        import threading
        calls = []

        def crashed(sha, env, evidence, name="check"):
            calls.append(sha)
            return {"sha": sha, "complete": False, "steps": [{"name": "run_tests", "rc": 2}],
                    "step_failures": [], "test_rc": 2, "failures": [], "module_errors": [],
                    "ran": 0, "printed_ran": False}
        results = []
        with mock.patch.object(te_run, "check_sha", crashed):
            threads = [threading.Thread(target=lambda: results.append(te_run.base_check("b" * 40,
                                                                                        {})))
                       for _ in range(2)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        self.assertEqual(len(calls), 2)  # nothing cached, so the second ran it again
        self.assertFalse(list((self.tmp / "base-check").glob("*/result.json")))
        self.assertFalse(any(r["complete"] for r in results))

    def test_a_complete_run_is_cached_and_reused(self):
        calls = []

        def complete(sha, env, evidence, name="check"):
            calls.append(sha)
            return {"sha": sha, "complete": True, "steps": [], "step_failures": [],
                    "test_rc": 0, "failures": [], "module_errors": [], "ran": 1,
                    "printed_ran": True}
        with mock.patch.object(te_run, "check_sha", complete):
            te_run.base_check("c" * 40, {})
            te_run.base_check("c" * 40, {})
        self.assertEqual(len(calls), 1)
        self.assertTrue((self.tmp / "base-check" / ("c" * 40) / "result.json").is_file())


class CheckSteps(unittest.TestCase):
    """CHECK_STEPS cannot drift from mise.toml's `check` (with `ci-check` inlined)."""

    def test_check_steps_match_the_mise_check_task(self):
        import tomllib
        tasks = tomllib.loads((te_run.SOURCE / "mise.toml").read_text(encoding="utf-8"))["tasks"]

        def lines(task: str) -> list[str]:
            out = []
            for line in tasks[task]["run"].splitlines():
                line = line.strip()
                if not line or line.startswith("#") or line in ("set -e",
                                                                "head=$(git rev-parse HEAD)"):
                    continue
                out += lines(line.split()[-1]) if line.startswith("mise run ") else [line]
            return out
        ours = [l.strip() for _n, script in te_run.CHECK_STEPS for l in script.splitlines()]
        self.assertEqual(ours, lines("check"))
        self.assertIn(te_run.TEST_STEP, [n for n, _s in te_run.CHECK_STEPS])


class Settings(unittest.TestCase):
    def settings(self) -> dict:
        return json.loads(te_run.SETTINGS.read_text())["permissions"]

    def test_allow_list_has_the_required_commands(self):
        allow = self.settings()["allow"]
        for cmd in ("mkdir", "rm", "cp", "mv", "touch", "git checkout", "git switch",
                    "git restore", "git stash", "git mv", "git rm", "git reset", "git rev-parse",
                    "git branch", "mise tasks", "mise run"):
            self.assertIn(f"Bash({cmd}:*)", allow)
        for cmd in ("bash", "sh", "env", "eval", "xargs"):
            self.assertFalse([a for a in allow if a.startswith(f"Bash({cmd}")], cmd)
            self.assertIn(f"Bash({cmd}:*)", self.settings()["deny"])

    def test_mise_task_dirs_are_write_denied(self):
        deny = self.settings()["deny"]
        self.assertIn("Edit(./**/.mise/tasks/**)", deny)
        self.assertIn("Edit(./**/mise-tasks/**)", deny)
        for path in (".mise/tasks/check", "tools/mise-tasks/x.sh"):
            self.assertTrue(te_run.deny_check.denied(path), path)


    def test_mise_alternate_configs_are_denied_and_only_mise_toml_is_trusted(self):
        # Preclose survivor 2.
        for path in ("mise.local.toml", ".mise.toml", ".mise.local.toml", ".config/mise.toml",
                     ".config/mise/config.toml", "mise/config.toml", ".mise-tasks/check",
                     "mise/tasks/check", "sub/mise.local.toml"):
            self.assertTrue(te_run.deny_check.denied(path), path)
        clone = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, clone, True)
        argv = te_run.sandbox.boundary_argv(clone)
        at = argv.index("MISE_TRUSTED_CONFIG_PATHS")
        self.assertEqual(argv[at + 1], str(clone.resolve() / "mise.toml"))


class LaunchBase(unittest.TestCase):
    """harmonic-forge#1195 rework: one launch mode for the agent and the probe."""

    def test_base_loads_no_settings_file_and_passes_the_runner_settings(self):
        base = te_run.launch_base()
        self.assertEqual(base[:2], [te_run.CLAUDE, "-p"])
        i = base.index("--setting-sources")
        self.assertEqual(base[i:i + 2], ["--setting-sources", ""])
        j = base.index("--settings")
        self.assertEqual(base[j:j + 2], ["--settings", str(te_run.SETTINGS)])
        self.assertLess(i, j)

    def test_base_never_contains_bare(self):
        for settings in (None, Path("/x/settings.json")):
            self.assertNotIn("--bare", te_run.launch_base(settings))
        self.assertNotIn("--bare", te_run.claude_argv("1"))

    def test_agent_argv_starts_from_the_base(self):
        base = te_run.launch_base()
        argv = te_run.claude_argv("2")
        self.assertEqual(argv[:len(base)], base)
        self.assertEqual(argv[len(base):], ["--max-budget-usd", "2", "--max-turns", "60"])

    def test_a_settings_path_replaces_only_the_settings_value(self):
        default, other = te_run.launch_base(), te_run.launch_base(Path("/x/s.json"))
        j = default.index("--settings")
        self.assertEqual(other[j + 1], "/x/s.json")
        self.assertEqual(default[:j + 1] + default[j + 2:], other[:j + 1] + other[j + 2:])


class Prompt(unittest.TestCase):
    def test_prompt_names_harness_owned_steps_and_refusal_policy(self):
        text = te_run.agent_prompt(7, "BODY", "HANDOFF")
        for phrase in ("`mkdir`", "`git checkout`", "`mise tasks`",
                       "A refusal is never a reason to stop",
                       "commit what exists",
                       "The harness runs the kill checks, `mise run check` and the deny check",
                       "cannot see them",
                       "`os.environ`", "never as a command prefix",
                       "`pytest` is not installed", "`python3 -m unittest`",
                       "`python3 tools/run_tests.py`",
                       "deployed or live measurement", "not a reason to self-flag",
                       te_run.SELF_FLAG, "BODY", "HANDOFF"):
            self.assertIn(phrase, text)
        self.assertNotIn("following the Lane 1 handoff below exactly", text)

    def test_prompt_recommends_no_path_the_boundary_refuses(self):
        """preclose pass 1: the prompt sent the agent to `.claude/cache/`, which
        te-runner.settings.json's Edit deny refuses, so following it set the item aside."""
        # Preclose pass 2 (sticky-wicket PATCH): assert the behavior, not two
        # literals. The prohibition must be present, and once that one sentence
        # is cut out, no file path may be left recommended.
        text = te_run.agent_prompt(7, "BODY", "HANDOFF").split("## Issue")[0]
        prohibition = ("never as a script file (no scratch file anywhere: `.claude/` is "
                       "refused and a file outside the checkout is too)")
        self.assertIn(prohibition, text)
        rest = text.replace(prohibition, "")
        # `tools/run_tests.py` is a command the agent runs inside the checkout,
        # not a file it is told to write.
        rest = rest.replace("`python3 tools/run_tests.py`", "")
        found = re.findall(r"[\w./-]*\.(?:py|sh|txt)\b|(?<!\w)/tmp\b|\.claude/\S+", rest)
        self.assertEqual(found, [], f"the prompt recommends a path: {found}")

    def test_prompt_states_the_one_command_per_call_shape(self):
        """harmonic-forge#1137 AC1: each refused shape is named, with its alternative."""
        text = te_run.agent_prompt(7, "BODY", "HANDOFF")
        for phrase in ("already the checkout", "exactly one of the commands above",
                       "`cd`", "`git -C`", "`&&`", "`;`", "`|`", "`>`", "heredoc (`<<`)",
                       "`python3 -c '...'`", "never as a script file",
                       "`python3 -m unittest discover -s <dir> -t <dir>`",
                       "sets your item aside"):
            self.assertIn(phrase, text)

    def test_prompt_names_only_tools_the_session_has(self):
        """harmonic-forge#1137 AC2: the tool set is the recorded session's `init` event,
        not a hand-typed list; Grep and Glob were named but absent in run 9d10b3e1."""
        init = json.loads((Path(__file__).parent / "testdata"
                           / "agent_init_9d10b3e1.json").read_text())
        observed = set(init["tools"])
        text = te_run.agent_prompt(7, "BODY", "HANDOFF")
        # The whole instruction text, not only the opening list (preclose pass 1:
        # a tool restored in the refusal-fallback sentence passed a list-only scan).
        # The issue and handoff are appended verbatim and are not instructions.
        instructions = text.split("## Issue")[0]
        named = set(re.findall(r"\b(Read|Grep|Glob|Edit|Write|NotebookEdit|WebFetch|WebSearch)\b",
                               instructions))
        self.assertTrue(named)
        self.assertLessEqual(named, observed, f"prompt names tools the session lacks: "
                                              f"{sorted(named - observed)}")


@pipeline_only
class Evidence(Pipeline):
    """harmonic-forge#1112 AC7."""

    def test_item_artifacts_survive_the_item(self):
        self.drive()
        state = te_run.queue_state.load(self.queue)
        item_dir = self.tmp / "runs" / state["run_id"] / "7"
        for name in ("stream.jsonl", "stream.jsonl.stderr", "check-run_tests.stdout",
                     "check-run_tests.stderr",
                     "kill_runner.stdout", "kill_runner.stderr", "bundle/checks.json"):
            self.assertTrue((item_dir / name).is_file(), name)
        self.assertTrue((item_dir / "transcripts").is_dir())

    def test_only_the_newest_runs_are_kept(self):
        runs = self.tmp / "runs"
        for n in range(12):
            (runs / f"run-20200101T0000{n:02d}Z-00").mkdir(parents=True)
        self.drive()
        kept = sorted(p.name for p in runs.iterdir())
        self.assertEqual(len(kept), te_run.KEEP_RUNS)
        self.assertEqual(kept[-1], te_run.queue_state.load(self.queue)["run_id"])

    def test_failed_preflight_aborts_before_any_item(self):
        self.preflight.return_value = {"failures": ["x"], "reported": []}
        self.assertEqual(self.drive(), 3)
        self.assertEqual(self.item()["status"], "pending")
        self.assertEqual(self.observed(), [])


class Unit(unittest.TestCase):
    def test_npm_global_precedes_usr_local(self):
        unit = (Path(te_run.__file__).resolve().parent / "systemd"
                / "forge-te-runner.service").read_text()
        [line] = [l for l in unit.splitlines() if l.startswith("Environment=PATH=")]
        parts = line.split("=", 2)[2].split(":")
        self.assertIn("%h/.npm-global/bin", parts)
        self.assertLess(parts.index("%h/.npm-global/bin"), parts.index("/usr/local/bin"))

    def stop_post(self, saved: dict, invocation: str, exit_status: str = "0") -> tuple[Path, Path]:
        """The unit's own ExecStopPost line, run as systemd would (%h expanded,
        $$ unescaped), over a queue holding `saved`; (home, data dir)."""
        unit = (Path(te_run.__file__).resolve().parent / "systemd"
                / "forge-te-runner.service").read_text()
        lines = [l.split("=", 1)[1] for l in unit.splitlines() if l.startswith("ExecStopPost=")]
        self.assertEqual(len(lines), 1, "the unit has no single ExecStopPost= line")
        home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, home, True)
        (home / "harmonic-forge").symlink_to(Path(te_run.__file__).resolve().parents[2])
        data = home / ".local" / "share" / "te-runner"
        data.mkdir(parents=True)
        te_run.queue_state.save(data / "queue.json", {**saved, "items": []})
        (data / "ntfy-topic").write_text("topic\n")
        fake = home / "bin"
        fake.mkdir()
        (fake / "curl").write_text(f"#!/bin/sh\necho \"$*\" >> {home}/curl.log\n")
        (fake / "curl").chmod(0o755)
        command = lines[0].replace("%h", str(home)).replace("$$", "$") \
            .replace("/usr/bin/python3", sys.executable)
        argv = shlex.split(command)
        done = subprocess.run(argv, env={**os.environ, "HOME": str(home),
                                         "PATH": f"{fake}:{os.environ['PATH']}",
                                         "INVOCATION_ID": invocation, "EXIT_STATUS": exit_status},
                              capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        return home, data

    def test_finished_run_renders_report_and_notifies_once(self):
        # AC13: the ExecStopPost line renders <run-id>.md and notifies once.
        home, data = self.stop_post({"run_id": "20261021T000000Z-abcd",
                                     "invocation_id": "inv-n"}, "inv-n")
        self.assertTrue((data / "reports" / "20261021T000000Z-abcd.md").is_file())
        self.assertEqual(len((home / "curl.log").read_text().splitlines()), 1)

    def test_run_that_ended_before_it_started_never_reports_the_last_run(self):
        # Sticky-wicket patch 2: run N+1 exits before _run_queue saves a new
        # run id (an empty budget, a held queue lock). Its ExecStopPost must
        # not render or notify run N's report as this run's result.
        home, data = self.stop_post({"run_id": "20261021T000000Z-abcd",
                                     "invocation_id": "inv-n"}, "inv-n-plus-1", "2")
        self.assertFalse((data / "reports" / "20261021T000000Z-abcd.md").exists())
        [report] = list((data / "reports").glob("*.md"))
        self.assertIn("run ended before it started: exit 2", report.read_text())
        [sent] = (home / "curl.log").read_text().splitlines()
        self.assertIn("run ended before it started: exit 2", sent)


# --- harmonic-forge#1127: every clone gets its platform links ---------------------------

def _scratch(case: unittest.TestCase) -> Path:
    base = Path.home() / ".cache" / "te-runner-test-scratch"
    base.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(dir=base))
    case.addCleanup(shutil.rmtree, tmp, True)
    return tmp


def _clone_repo(tmp: Path) -> Path:
    """A `--shared` clone of this repo's HEAD, as the runner makes one."""
    clone = tmp / "clone"
    head = sh("git", "rev-parse", "HEAD", cwd=te_run.SOURCE).strip()
    sh("git", "clone", "-q", "--shared", "--no-checkout", str(te_run.SOURCE), str(clone),
       cwd=tmp)
    sh("git", "checkout", "-q", "--detach", head, cwd=clone)
    return clone


class ProvisionClone(unittest.TestCase):
    @unittest.skipUnless(shutil.which("bwrap"), "real bwrap is not installed")
    def test_links_a_fresh_forge_clone(self):
        clone = _clone_repo(_scratch(self))
        with mock.patch.dict(os.environ, {"TE_RUNNER_BWRAP": ""}):
            te_run.provision.provision_clone(clone, te_run.source_objects())
        report = subprocess.run(
            ["python3", "tools/onboard/platform_link_report.py", "--project", "."],
            cwd=clone, capture_output=True, text=True)
        self.assertEqual(report.returncode, 0, report.stdout + report.stderr)
        self.assertEqual(sh("git", "config", "core.hooksPath", cwd=clone).strip(), ".githooks")

    def test_every_clone_site_provisions(self):
        missing, sites = [], 0
        for path in sorted(te_run.HERE.glob("*.py")):
            if path.name.startswith("test_"):
                continue
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", SyntaxWarning)
                tree = ast.parse(path.read_text())
            for fn in ast.walk(tree):
                if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                clone_line, provision_lines = None, []
                for node in ast.walk(fn):
                    if not isinstance(node, ast.Call):
                        continue
                    name = (node.func.id if isinstance(node.func, ast.Name)
                            else getattr(node.func, "attr", ""))
                    if name == "git" and any(isinstance(a, ast.Constant) and a.value == "clone"
                                             for a in node.args):
                        clone_line = min(clone_line or node.lineno, node.lineno)
                    elif name == "provision_clone":
                        provision_lines.append(node.lineno)
                if clone_line is None:
                    continue
                sites += 1
                if not any(line > clone_line for line in provision_lines):
                    missing.append(f"{path.stem}.{fn.name}")
        self.assertGreaterEqual(sites, 4)
        self.assertEqual(missing, [])

    @unittest.skipUnless(shutil.which("bwrap") and shutil.which("mise"),
                         "real bwrap or mise missing")
    def test_check_sha_on_unchanged_head_has_no_link_failure(self):
        tmp = _scratch(self)
        head = sh("git", "rev-parse", "HEAD", cwd=te_run.SOURCE).strip()
        step = next(s for s in te_run.CHECK_STEPS if s[0] == "platform_link_report")
        with mock.patch.dict(os.environ, {"TE_RUNNER_BWRAP": ""}), \
                mock.patch.object(te_run, "CHECK_TREE_ROOT", tmp / "trees"), \
                mock.patch.object(te_run, "CHECK_STEPS", (step,)):
            result = te_run.check_sha(head, dict(os.environ), None)
        self.assertEqual(result["step_failures"], [])

    def test_failed_provision_is_a_step_failure_not_abort(self):
        tmp = _scratch(self)
        head = sh("git", "rev-parse", "HEAD", cwd=te_run.SOURCE).strip()

        def fail(path, objects):
            raise te_run.provision.ProvisionError("verify exited 1")

        with mock.patch.object(te_run, "CHECK_TREE_ROOT", tmp / "trees"), \
                mock.patch.object(te_run, "BASE_CHECK_ROOT", tmp / "base"), \
                mock.patch.object(te_run.provision, "provision_clone", fail):
            result = te_run.check_sha(head, dict(os.environ), None)
            cached = te_run.base_check(head, dict(os.environ))
        self.assertEqual(result["step_failures"], ["provision_clone: verify exited 1"])
        self.assertFalse(result["complete"])
        self.assertEqual(cached["step_failures"], ["provision_clone: verify exited 1"])
        self.assertFalse((tmp / "base" / head / "result.json").exists())

    def test_both_linker_calls_go_through_the_boundary(self):
        path = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, path, True)
        sh("git", "init", "-q", cwd=path)
        (path / "sync_rules.py").write_text("")
        wrapped = []

        def wrap(argv, writable, transcripts=None, **kw):
            wrapped.append((list(argv), Path(writable), kw.get("inputs")))
            return ["true"]

        with mock.patch.object(te_run.provision.sandbox, "wrap", wrap):
            te_run.provision.provision_clone(path, Path("/objects"))
        self.assertEqual([w[0] for w in wrapped],
                         [["python3", "sync_rules.py", "--project", "."],
                          ["python3", "sync_rules.py", "--verify", "--project", "."]])
        self.assertTrue(all(w[1] == path and w[2] == (Path("/objects"),) for w in wrapped))

    def test_failed_verify_raises(self):
        path = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, path, True)
        sh("git", "init", "-q", cwd=path)
        (path / "sync_rules.py").write_text("")
        with mock.patch.object(te_run.provision.sandbox, "wrap",
                               lambda argv, *a, **k: ["false"]):
            with self.assertRaises(te_run.provision.ProvisionError):
                te_run.provision.provision_clone(path, Path("/objects"))

    def test_hung_linker_raises(self):
        path = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, path, True)
        sh("git", "init", "-q", cwd=path)
        (path / "sync_rules.py").write_text("")
        with mock.patch.object(te_run.provision.sandbox, "wrap",
                               lambda argv, *a, **k: ["sleep", "30"]), \
                mock.patch.object(te_run.provision, "LINK_TIMEOUT", 0.2):
            with self.assertRaisesRegex(te_run.provision.ProvisionError, "timed out"):
                te_run.provision.provision_clone(path, Path("/objects"))


@pipeline_only
class ProvisionCleanup(Pipeline):
    def test_item_clone_removed_on_provision_error(self):
        def fail_item(path, objects):
            if path.name.endswith("r-impl"):
                raise te_run.provision.ProvisionError("verify failed")

        clone = self.tmp / "wt" / "harmonic-forge-7r-impl"
        with mock.patch.object(te_run.provision, "provision_clone", fail_item):
            self.assertEqual(self.drive(), 3)
        self.assertEqual(self.item()["status"], "blocked")
        self.assertFalse(clone.exists())
        self.assertEqual(self.observed(), [])


if __name__ == "__main__":
    unittest.main()
