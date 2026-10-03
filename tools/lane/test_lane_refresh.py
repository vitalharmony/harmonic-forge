"""harmonic-forge#761 AC9: `_lane_refresh.sh` via lane1/lane2, and the
`lane_launch_notice.py` SessionStart hook.

Lane 3's refresh path is covered by `test_lane_launchers.Lane3RefreshesAtLaunch`.
Every case runs against a disposable fixture tree whose `origin` is a local
bare repository, so nothing touches the network.
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_lane_launchers as tll  # noqa: E402

# Referenced through the module, not imported by name: a TestCase class bound
# in this namespace would be collected and run a second time here.
_FixtureTree = tll._FixtureTree
_advance = tll.Lane3RefreshesAtLaunch._advance_origin_from_elsewhere

HOOK = Path(__file__).resolve().parent.parent / "hooks" / "lane_launch_notice.py"
_spec = importlib.util.spec_from_file_location("lane_launch_notice", HOOK)
notice = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(notice)

KEYS = "LANE_REFRESH_STATUS,LANE_REFRESH_FROM,LANE_REFRESH_TO"


def _git(path, *args):
    return subprocess.run(["git", *args], cwd=path, check=True,
                          capture_output=True, text=True).stdout.strip()


def _run(tree, lane, args=(), **env):
    return tree.run(lane, list(args), LANE_CLI=env.pop("agent", "claude"),
                    LANE_CAPTURE_EXTRA_ENV=KEYS,
                    LANE_REFRESH_LOG_DIR=str(tree.root / "refresh-log"), **env)


class Lane1BranchMode(unittest.TestCase):
    """TC3, TC9: lane1 fast-forwards a clean `main`, and otherwise warns and
    starts without moving anything."""

    def test_clean_and_behind_fast_forwards_main(self):
        with _FixtureTree() as tree:
            remote_sha = _advance(tree)
            cell = _run(tree, "1")
            self.assertTrue(cell["launched"], cell.get("stderr"))
            self.assertEqual(_git(tree.main, "rev-parse", "HEAD"), remote_sha)
            self.assertEqual(_git(tree.main, "symbolic-ref", "HEAD"), "refs/heads/main")
            self.assertEqual(cell["extra_env"]["LANE_REFRESH_STATUS"], "updated")

    def test_dirty_warns_starts_and_moves_nothing(self):
        with _FixtureTree() as tree:
            before = _git(tree.main, "rev-parse", "HEAD")
            _advance(tree)
            (tree.main / "README.md").write_text("local edit\n")
            cell = _run(tree, "1")
            self.assertTrue(cell["launched"], cell.get("stderr"))
            self.assertEqual(_git(tree.main, "rev-parse", "HEAD"), before)
            self.assertEqual(cell["extra_env"]["LANE_REFRESH_STATUS"], "skipped-dirty")
            self.assertIn("README.md", cell["stderr"])

    def test_off_main_warns_starts_and_moves_nothing(self):
        with _FixtureTree() as tree:
            _git(tree.main, "checkout", "-q", "-b", "side")
            before = _git(tree.main, "rev-parse", "HEAD")
            _advance(tree)
            cell = _run(tree, "1")
            self.assertTrue(cell["launched"], cell.get("stderr"))
            self.assertEqual(_git(tree.main, "rev-parse", "HEAD"), before)
            self.assertEqual(cell["extra_env"]["LANE_REFRESH_STATUS"], "skipped-diverged")

    def test_detached_main_checkout_is_never_moved(self):
        """TC9: `merge --ff-only` would succeed on a detached HEAD and move it
        silently; the helper must refuse that case up front."""
        with _FixtureTree() as tree:
            _git(tree.main, "checkout", "-q", "--detach")
            before = _git(tree.main, "rev-parse", "HEAD")
            _advance(tree)
            cell = _run(tree, "1")
            self.assertTrue(cell["launched"], cell.get("stderr"))
            self.assertEqual(_git(tree.main, "rev-parse", "HEAD"), before)
            self.assertEqual(cell["extra_env"]["LANE_REFRESH_STATUS"], "skipped-diverged")

    def test_local_commits_ahead_record_current(self):
        with _FixtureTree() as tree:
            (tree.main / "LOCAL.md").write_text("local\n")
            _git(tree.main, "add", "LOCAL.md")
            _git(tree.main, "commit", "-q", "-m", "local")
            before = _git(tree.main, "rev-parse", "HEAD")
            cell = _run(tree, "1")
            self.assertTrue(cell["launched"], cell.get("stderr"))
            self.assertEqual(_git(tree.main, "rev-parse", "HEAD"), before)
            self.assertEqual(cell["extra_env"]["LANE_REFRESH_STATUS"], "current")

    def test_diverged_warns_starts_and_moves_nothing(self):
        with _FixtureTree() as tree:
            (tree.main / "LOCAL.md").write_text("local\n")
            _git(tree.main, "add", "LOCAL.md")
            _git(tree.main, "commit", "-q", "-m", "local")
            before = _git(tree.main, "rev-parse", "HEAD")
            _advance(tree)
            cell = _run(tree, "1")
            self.assertTrue(cell["launched"], cell.get("stderr"))
            self.assertEqual(_git(tree.main, "rev-parse", "HEAD"), before)
            self.assertEqual(cell["extra_env"]["LANE_REFRESH_STATUS"], "skipped-diverged")

    def test_unreachable_origin_warns_and_starts(self):
        """TC4 (lane1 path)."""
        with _FixtureTree() as tree:
            _git(tree.main, "remote", "set-url", "origin",
                 str(tree.root / "does-not-exist.git"))
            cell = _run(tree, "1")
            self.assertTrue(cell["launched"], cell.get("stderr"))
            self.assertEqual(cell["extra_env"]["LANE_REFRESH_STATUS"], "fetch-failed")

    def test_restart_reminder_prints_for_every_agent(self):
        """TC11."""
        with _FixtureTree() as tree:
            for agent in ("claude", "codex", "gemini"):
                with self.subTest(agent=agent):
                    cell = _run(tree, "1", agent=agent)
                    self.assertTrue(cell["launched"], cell.get("stderr"))
                    self.assertIn("mise run restart --no-bump --no-git", cell["stderr"])


class Lane2DetachedMode(unittest.TestCase):
    def test_behind_is_updated_and_logged(self):
        """TC1 (lane2 path)."""
        with _FixtureTree() as tree:
            remote_sha = _advance(tree)
            cell = _run(tree, "2")
            self.assertTrue(cell["launched"], cell.get("stderr"))
            self.assertEqual(_git(tree.lane2, "rev-parse", "HEAD"), remote_sha)
            self.assertEqual(cell["extra_env"]["LANE_REFRESH_STATUS"], "updated")
            log = (tree.root / "refresh-log" / "refresh.log").read_text().splitlines()
            self.assertEqual(len(log), 1)
            self.assertIn("\tlane2\t", log[0])

    def test_tracked_changes_refuse_and_name_the_file(self):
        """TC2 (lane2 path)."""
        with _FixtureTree() as tree:
            before = _git(tree.lane2, "rev-parse", "HEAD")
            _advance(tree)
            (tree.lane2 / "README.md").write_text("local edit\n")
            cell = _run(tree, "2")
            self.assertFalse(cell["launched"])
            self.assertIn("README.md", cell["stderr"])
            self.assertEqual(_git(tree.lane2, "rev-parse", "HEAD"), before)

    def test_unreachable_origin_refuses(self):
        """TC4 (lane2 path)."""
        with _FixtureTree() as tree:
            _git(tree.main, "remote", "set-url", "origin",
                 str(tree.root / "does-not-exist.git"))
            cell = _run(tree, "2")
            self.assertFalse(cell["launched"])
            self.assertIn("could not determine or reach origin/main", cell["stderr"])

    def test_current_is_silent(self):
        with _FixtureTree() as tree:
            cell = _run(tree, "2")
            self.assertTrue(cell["launched"], cell.get("stderr"))
            self.assertEqual(cell["extra_env"]["LANE_REFRESH_STATUS"], "current")
            self.assertNotIn("lane2: checkout", cell["stderr"])



class PrecloseFindings(unittest.TestCase):
    """harmonic-forge#761 preclose: each finding's scenario, driven through
    the real launchers."""

    KEYS3 = KEYS + ",LANE_REFRESH_DETAIL,LANE_REFRESH_ENV"

    def _run(self, tree, lane, args=()):
        return tree.run(lane, list(args), LANE_CLI="claude",
                        LANE_CAPTURE_EXTRA_ENV=self.KEYS3,
                        LANE_REFRESH_LOG_DIR=str(tree.root / "refresh-log"))

    def test_a_pushed_branch_is_detached_and_named(self):
        """H1: no silent detach -- the branch is named in the record."""
        with _FixtureTree() as tree:
            _git(tree.lane3, "checkout", "-q", "-b", "gated")
            _git(tree.lane3, "push", "-q", "origin", "gated")
            remote_sha = _advance(tree)
            cell = self._run(tree, "3")
            self.assertTrue(cell["launched"], cell.get("stderr"))
            self.assertEqual(_git(tree.lane3, "rev-parse", "HEAD"), remote_sha)
            self.assertIn("gated", cell["extra_env"]["LANE_REFRESH_DETAIL"])
            self.assertIn("gated", cell["stderr"])

    def test_a_branch_with_unpushed_commits_refuses_and_stays(self):
        """H1: never detach away from work no remote has."""
        for lane, wt in (("2", "lane2"), ("3", "lane3")):
            with self.subTest(lane=lane), _FixtureTree() as tree:
                path = getattr(tree, wt)
                _git(path, "checkout", "-q", "-b", "local-work")
                (path / "WORK.md").write_text("w\n")
                _git(path, "add", "WORK.md")
                _git(path, "-c", "user.email=w@example.invalid", "-c", "user.name=W",
                     "commit", "-q", "-m", "work")
                before = _git(path, "rev-parse", "HEAD")
                _advance(tree)
                cell = self._run(tree, lane)
                self.assertFalse(cell["launched"])
                self.assertIn("local-work", cell["stderr"])
                self.assertEqual(_git(path, "rev-parse", "HEAD"), before)
                self.assertEqual(_git(path, "symbolic-ref", "--short", "HEAD"), "local-work")

    def test_a_failed_checkout_is_named_not_called_a_fetch_failure(self):
        """M1: an untracked file in the way of origin/main's new file."""
        for lane, wt in (("2", "lane2"), ("3", "lane3")):
            with self.subTest(lane=lane), _FixtureTree() as tree:
                _advance(tree)  # adds ELSEWHERE.md on origin/main
                (getattr(tree, wt) / "ELSEWHERE.md").write_text("in the way\n")
                cell = self._run(tree, lane)
                self.assertFalse(cell["launched"])
                self.assertIn("checkout --detach", cell["stderr"])
                self.assertIn("ELSEWHERE.md", cell["stderr"])
                self.assertNotIn("could not determine or reach", cell["stderr"])
                self.assertNotIn("cannot determine whether", cell["stderr"])

    def test_a_busy_worktree_is_not_moved(self):
        """M3: a live process with its cwd in the worktree."""
        with _FixtureTree() as tree:
            before = _git(tree.lane3, "rev-parse", "HEAD")
            _advance(tree)
            proc = subprocess.Popen(["sleep", "60"], cwd=tree.lane3)
            try:
                cell = self._run(tree, "3")
            finally:
                proc.kill(); proc.wait()
            self.assertTrue(cell["launched"], cell.get("stderr"))
            self.assertEqual(_git(tree.lane3, "rev-parse", "HEAD"), before)
            self.assertEqual(cell["extra_env"]["LANE_REFRESH_STATUS"], "skipped-busy")
            self.assertIn("NOT updated", cell["stderr"])

    def test_a_real_env_file_is_never_replaced(self):
        """M2, superseded by the harmonic-forge#875 sticky-wicket patch: a
        real file is refused and left byte-unchanged, not kept aside."""
        with _FixtureTree(with_backend_env=True) as tree:
            (tree.lane3 / "backend").mkdir()
            (tree.lane3 / "backend" / ".env").write_text("KEY=only-copy\n")
            cell = self._run(tree, "3")
            self.assertFalse(cell["launched"])
            self.assertFalse((tree.lane3 / "backend" / ".env").is_symlink())
            self.assertEqual((tree.lane3 / "backend" / ".env").read_text(), "KEY=only-copy\n")
            self.assertEqual(list((tree.lane3 / "backend").glob(".env.pre-relink-*")), [])

    def test_lane3_notice_never_claims_current_unless_current(self):
        """H2."""
        for status in ("ack-stale", "skipped-busy", "skipped-dirty",
                       "skipped-diverged", "fetch-failed", "checkout-failed", ""):
            with self.subTest(status=status):
                text = notice.build_notice({"LANE": "3", "LANE_REFRESH_STATUS": status,
                                            "LANE_REFRESH_FROM": "a" * 40})
                self.assertNotIn("was current at launch", text)
                self.assertIn("Gate report must state", text)
        self.assertIn("was current at launch",
                      notice.build_notice({"LANE": "3", "LANE_REFRESH_STATUS": "current"}))

class LaunchNotice(unittest.TestCase):
    """TC6, TC7."""

    def test_no_lane_emits_nothing(self):
        self.assertIsNone(notice.build_notice({"LANE_REFRESH_STATUS": "updated"}))

    def test_lane1_always_carries_the_restart_line(self):
        for status in ("updated", "current", "skipped-dirty", "fetch-failed", ""):
            with self.subTest(status=status):
                text = notice.build_notice({"LANE": "1", "LANE_REFRESH_STATUS": status})
                self.assertIn("Run `mise run restart --no-bump --no-git`", text)

    def _repo_with_change(self, path: str) -> tuple[str, str, str]:
        import tempfile
        d = tempfile.mkdtemp()
        _git(d, "init", "-q", "-b", "main")
        _git(d, "-c", "user.email=n@example.invalid", "-c", "user.name=N",
             "commit", "-q", "--allow-empty", "-m", "a")
        a = _git(d, "rev-parse", "HEAD")
        target = Path(d) / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x\n")
        _git(d, "add", path)
        _git(d, "-c", "user.email=n@example.invalid", "-c", "user.name=N",
             "commit", "-q", "-m", "b")
        return d, a, _git(d, "rev-parse", "HEAD")

    def test_lane1_code_changed_note_only_for_backend_or_frontend(self):
        import os
        for path, expected in (("backend/app/x.py", True), ("frontend/src/x.ts", True),
                               ("docs/x.md", False)):
            with self.subTest(path=path):
                d, a, b = self._repo_with_change(path)
                cwd = os.getcwd()
                os.chdir(d)
                try:
                    text = notice.build_notice({"LANE": "1", "LANE_REFRESH_STATUS": "updated",
                                                "LANE_REFRESH_FROM": a, "LANE_REFRESH_TO": b})
                finally:
                    os.chdir(cwd)
                self.assertEqual("backend/frontend code changed" in text, expected)

    def test_lane3_states_the_outcome_for_the_gate_report(self):
        text = notice.build_notice({"LANE": "3", "LANE_REFRESH_STATUS": "updated",
                                    "LANE_REFRESH_FROM": "a" * 40, "LANE_REFRESH_TO": "b" * 40})
        self.assertIn("Gate report must state", text)
        self.assertIn("updated at launch", text)
        self.assertIn("was current",
                      notice.build_notice({"LANE": "3", "LANE_REFRESH_STATUS": "current"}))

    def test_hook_emits_session_start_json(self):
        import json
        import os
        env = {k: v for k, v in os.environ.items() if not k.startswith("LANE")}
        env.update({"LANE": "1", "LANE_REFRESH_STATUS": "current"})
        out = subprocess.run([sys.executable, str(HOOK)], env=env,
                             capture_output=True, text=True, check=True).stdout
        payload = json.loads(out)
        self.assertEqual(payload["hookSpecificOutput"]["hookEventName"], "SessionStart")
        self.assertIn("mise run restart", payload["hookSpecificOutput"]["additionalContext"])

    def test_hook_is_silent_outside_a_lane(self):
        import os
        env = {k: v for k, v in os.environ.items() if not k.startswith("LANE")}
        out = subprocess.run([sys.executable, str(HOOK)], env=env,
                             capture_output=True, text=True, check=True).stdout
        self.assertEqual(out, "")


# ---------------------------------------------------------------------------
# harmonic-forge#875 -- a declared Lane 3 env provisioner replaces the relink
# ---------------------------------------------------------------------------
ENV_KEYS = KEYS + ",LANE_REFRESH_ENV"
DECLARED = "lane3-env-provision"
ACCESSOR = Path(__file__).resolve().parent.parent / "onboard" / "lane3_env_task.py"


def _run3(tree, args=()):
    return tree.run("3", list(args), LANE_CLI="claude",
                    LANE_CAPTURE_EXTRA_ENV=ENV_KEYS,
                    LANE_REFRESH_LOG_DIR=str(tree.root / "refresh-log"))


def _env_record(tree) -> list[str]:
    """The env field (8th) of every refresh.log record."""
    log = tree.root / "refresh-log" / "refresh.log"
    return [line.split("\t")[7] for line in log.read_text().splitlines()]


def _run_root(call: str) -> Path:
    """The `--root` a recorded `mise run <task> -- --root <dir>` targeted."""
    argv = call.split("|", 2)[2]
    return Path(argv.split(" -- --root ", 1)[1]).resolve()


def _noisy_python(tree) -> None:
    """A `python3` first on PATH that writes a diagnostic to stderr and then
    runs the real interpreter -- a broken `.pth`, a DeprecationWarning, a
    shim notice: stderr on a zero exit (preclose survivor 2)."""
    stub = tree.stub_bin / "python3"
    stub.write_text("#!/usr/bin/env bash\n"
                    "echo 'Error processing line 1 of /site/broken.pth' >&2\n"
                    f'exec "{sys.executable}" "$@"\n')
    stub.chmod(0o755)


class Lane3DeclaredEnvProvisioner(unittest.TestCase):
    """AC1/AC2/AC4. Every case runs the launcher from a fixture forge root
    whose projects.toml names the fixture checkout, with a stub `mise` first
    on PATH that knows only the tasks a checkout's mise.toml defines; without
    both, a case would only ever exercise the relink path and pass
    vacuously."""

    def _lane3_real_env(self, tree, text="KEY=own-copy\n"):
        (tree.lane3 / "backend").mkdir(exist_ok=True)
        (tree.lane3 / "backend" / ".env").write_text(text)

    def _declare(self, tree, exit_code=0):
        tree.write_manifest(lane3_env_task=DECLARED)
        tree.define_tasks(tree.main, DECLARED)
        tree.write_stub_mise(exit_code)

    def test_a_declared_provisioner_runs_instead_of_relinking(self):
        with _FixtureTree(with_backend_env=True) as tree:
            self._declare(tree)
            self._lane3_real_env(tree)
            cell = _run3(tree)
            self.assertTrue(cell["launched"], cell.get("stderr"))
            env_file = tree.lane3 / "backend" / ".env"
            self.assertFalse(env_file.is_symlink())
            self.assertEqual(env_file.read_text(), "SYNTHETIC=provisioned\n")
            self.assertEqual(list((tree.lane3 / "backend").glob(".env.pre-relink-*")), [])
            calls = tree.mise_calls()
            self.assertEqual(len(calls), 1)
            cwd, lane, argv = calls[0].split("|")
            # Run from the main checkout's definition, targeting the gate
            # worktree (preclose survivor 1).
            self.assertEqual(Path(cwd).resolve(), tree.main.resolve())
            self.assertEqual(lane, "unset")  # run with LANE unset
            self.assertTrue(argv.startswith(f"run {DECLARED} -- --root "), argv)
            self.assertEqual(_run_root(calls[0]), tree.lane3.resolve())
            self.assertEqual(cell["extra_env"]["LANE_REFRESH_ENV"], "provisioned")
            self.assertEqual(_env_record(tree), ["provisioned"])

    def test_ack_stale_still_provisions(self):
        with _FixtureTree(with_backend_env=True) as tree:
            self._declare(tree)
            cell = _run3(tree, ["--ack-stale", "deliberate"])
            self.assertTrue(cell["launched"], cell.get("stderr"))
            self.assertEqual(cell["extra_env"]["LANE_REFRESH_ENV"], "provisioned")
            self.assertFalse((tree.lane3 / "backend" / ".env").is_symlink())

    def test_a_task_missing_at_the_gate_worktrees_ref_still_provisions(self):
        """Preclose survivor 1: the gate worktree sits on a ref that predates
        the task (`--ack-stale` on an older target, `skipped-busy`, or forge
        merged first). Its own mise config LACKS the task, so running the task
        there fails as unknown -- the launcher must neither relink nor refuse,
        because the main checkout's definition provisions it."""
        with _FixtureTree(with_backend_env=True) as tree:
            self._declare(tree)
            tree.define_tasks(tree.lane3, "pc-up")  # the older ref: no DECLARED
            self._lane3_real_env(tree)
            cell = _run3(tree, ["--ack-stale", "gating an older target"])
            self.assertTrue(cell["launched"], cell.get("stderr"))
            env_file = tree.lane3 / "backend" / ".env"
            self.assertFalse(env_file.is_symlink())
            self.assertEqual(env_file.read_text(), "SYNTHETIC=provisioned\n")
            calls = tree.mise_calls()
            self.assertEqual(len(calls), 1)
            self.assertEqual(Path(calls[0].split("|")[0]).resolve(), tree.main.resolve())
            self.assertEqual(_env_record(tree), ["provisioned"])

    def test_a_task_the_main_checkout_does_not_define_refuses_with_a_real_remedy(self):
        """Not defined is told apart from ran-and-failed: no `mise run` is
        attempted, nothing is relinked, and the remedy names updating the
        main checkout -- not `mise trust`, which cannot clear it."""
        with _FixtureTree(with_backend_env=True) as tree:
            tree.write_manifest(lane3_env_task=DECLARED)
            tree.define_tasks(tree.main, "pc-up")
            tree.write_stub_mise(0)
            self._lane3_real_env(tree)
            cell = _run3(tree)
            self.assertFalse(cell["launched"])
            self.assertIn("is not defined in the main checkout", cell["stderr"])
            self.assertIn("pull --ff-only", cell["stderr"])
            self.assertNotIn("mise trust", cell["stderr"])
            env_file = tree.lane3 / "backend" / ".env"
            self.assertFalse(env_file.is_symlink())
            self.assertEqual(env_file.read_text(), "KEY=own-copy\n")
            self.assertEqual(tree.mise_calls(), [])
            self.assertEqual(_env_record(tree), ["provision-failed"])

    def test_a_manifest_lookup_failure_refuses_the_launch(self):
        with _FixtureTree(with_backend_env=True) as tree:
            tree.write_manifest(raw="this is [ not valid toml\n")
            tree.define_tasks(tree.main, DECLARED)
            tree.write_stub_mise(0)
            self._lane3_real_env(tree)
            cell = _run3(tree)
            self.assertFalse(cell["launched"])
            self.assertIn("cannot determine whether this project declares", cell["stderr"])
            self.assertIn("projects.toml", cell["stderr"])
            env_file = tree.lane3 / "backend" / ".env"
            self.assertFalse(env_file.is_symlink())
            self.assertEqual(env_file.read_text(), "KEY=own-copy\n")
            self.assertEqual(tree.mise_calls(), [])
            self.assertEqual(_env_record(tree), ["provision-failed"])

    def test_an_exported_manifest_override_cannot_steer_the_lookup(self):
        """Preclose survivor 4: FORGE_PROJECTS_MANIFEST exported at a valid
        manifest that lacks the key must not make a declared project relink.
        The launcher reads only its own forge checkout's projects.toml."""
        with _FixtureTree(with_backend_env=True) as tree:
            self._declare(tree)
            stale = tree.root / "stale-projects.toml"
            tree.write_manifest(at=stale)  # same project, no lane3_env_task
            self._lane3_real_env(tree)
            cell = tree.run("3", [], LANE_CLI="claude",
                            LANE_CAPTURE_EXTRA_ENV=ENV_KEYS,
                            LANE_REFRESH_LOG_DIR=str(tree.root / "refresh-log"),
                            FORGE_PROJECTS_MANIFEST=str(stale))
            self.assertTrue(cell["launched"], cell.get("stderr"))
            env_file = tree.lane3 / "backend" / ".env"
            self.assertFalse(env_file.is_symlink())
            self.assertEqual(env_file.read_text(), "SYNTHETIC=provisioned\n")
            self.assertEqual(list((tree.lane3 / "backend").glob(".env.pre-relink-*")), [])
            self.assertEqual(_env_record(tree), ["provisioned"])

    def test_stderr_noise_on_an_undeclared_project_still_relinks(self):
        """Preclose survivor 2, AC2: python3 exits 0 but writes to stderr. The
        noise is not a task name, so a project declaring nothing relinks."""
        with _FixtureTree(with_backend_env=True) as tree:
            _noisy_python(tree)
            tll._drift_lane3_env(tree)
            cell = _run3(tree)
            self.assertTrue(cell["launched"], cell.get("stderr"))
            link = tree.lane3 / "backend" / ".env"
            self.assertTrue(link.is_symlink())
            self.assertEqual(tree.mise_calls(), [])
            self.assertEqual(_env_record(tree), ["relinked"])

    def test_stderr_noise_on_a_declared_project_yields_exactly_the_task(self):
        with _FixtureTree(with_backend_env=True) as tree:
            self._declare(tree)
            _noisy_python(tree)
            cell = _run3(tree)
            self.assertTrue(cell["launched"], cell.get("stderr"))
            calls = tree.mise_calls()
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0].split("|", 2)[2],
                             f"run {DECLARED} -- --root {_run_root(calls[0])}")
            self.assertEqual(_env_record(tree), ["provisioned"])

    def test_a_checkout_matching_no_project_refuses_the_launch(self):
        with _FixtureTree(with_backend_env=True) as tree:
            tree.write_manifest(raw=tree.manifest.read_text().replace(
                str(tree.main), str(tree.root / "elsewhere")))
            self._lane3_real_env(tree)
            cell = _run3(tree)
            self.assertFalse(cell["launched"])
            self.assertIn("no project in", cell["stderr"])
            self.assertFalse((tree.lane3 / "backend" / ".env").is_symlink())

    def test_a_failed_provisioner_refuses_the_launch_with_remediation(self):
        with _FixtureTree(with_backend_env=True) as tree:
            self._declare(tree, exit_code=1)
            self._lane3_real_env(tree)
            cell = _run3(tree)
            self.assertFalse(cell["launched"])
            self.assertIn(f"the declared Lane 3 env task 'mise run {DECLARED}' failed",
                          cell["stderr"])
            self.assertIn("read the task's output above", cell["stderr"])
            self.assertIn("relaunch lane3", cell["stderr"])
            self.assertFalse((tree.lane3 / "backend" / ".env").is_symlink())
            self.assertEqual(len(tree.mise_calls()), 1)
            self.assertEqual(_env_record(tree), ["provision-failed"])

    def test_a_project_without_the_key_is_still_relinked(self):
        with _FixtureTree(with_backend_env=True) as tree:
            tll._drift_lane3_env(tree)
            cell = _run3(tree)
            self.assertTrue(cell["launched"], cell.get("stderr"))
            link = tree.lane3 / "backend" / ".env"
            self.assertTrue(link.is_symlink())
            self.assertEqual(link.resolve(), (tree.main / "backend" / ".env").resolve())
            self.assertEqual(tree.mise_calls(), [])
            self.assertEqual(cell["extra_env"]["LANE_REFRESH_ENV"], "relinked")
            self.assertEqual(_env_record(tree), ["relinked"])

    def test_a_project_with_no_backend_env_is_untouched_and_logged_na(self):
        with _FixtureTree() as tree:
            cell = _run3(tree)
            self.assertTrue(cell["launched"], cell.get("stderr"))
            self.assertEqual(cell["extra_env"]["LANE_REFRESH_ENV"], "n/a")
            self.assertEqual(_env_record(tree), ["n/a"])

    def test_a_stale_refusal_is_still_logged_exactly_once(self):
        with _FixtureTree(with_backend_env=True) as tree:
            subprocess.run(["git", "remote", "set-url", "origin",
                            str(tree.root / "does-not-exist.git")],
                           cwd=tree.main, check=True, capture_output=True)
            cell = _run3(tree)
            self.assertFalse(cell["launched"])
            log = (tree.root / "refresh-log" / "refresh.log").read_text().splitlines()
            self.assertEqual(len(log), 1)
            self.assertIn("\tfetch-failed\t", log[0])

    def test_lane1_and_lane2_records_carry_an_na_env_field(self):
        with _FixtureTree() as tree:
            _run(tree, "2")
            self.assertEqual(_env_record(tree), ["n/a"])


class Lane3ProvisionDeclaredEnv(unittest.TestCase):
    """`lane3-provision` (R-0189) takes the same three-way branch."""

    def _declare(self, tree, exit_code=0):
        tree.write_manifest(lane3_env_task=DECLARED)
        tree.define_tasks(tree.main, DECLARED)
        tree.write_stub_mise(exit_code)

    def test_provision_runs_the_declared_task(self):
        with _FixtureTree(with_backend_env=True) as tree:
            self._declare(tree)
            proc = tree.run_script("lane3-provision")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertFalse((tree.lane3 / "backend" / ".env").is_symlink())
            calls = tree.mise_calls()
            self.assertEqual(len(calls), 1)
            self.assertEqual(_run_root(calls[0]), tree.lane3.resolve())

    def test_provision_repairs_a_gate_worktree_whose_ref_lacks_the_task(self):
        """The remedy `lane3` prints must genuinely clear the state: the gate
        worktree's own mise config lacks the task, and provisioning works."""
        with _FixtureTree(with_backend_env=True) as tree:
            self._declare(tree)
            tree.define_tasks(tree.lane3, "pc-up")
            proc = tree.run_script("lane3-provision")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual((tree.lane3 / "backend" / ".env").read_text(),
                             "SYNTHETIC=provisioned\n")

    def test_provision_refuses_a_failed_task(self):
        with _FixtureTree(with_backend_env=True) as tree:
            self._declare(tree, exit_code=1)
            proc = tree.run_script("lane3-provision")
            self.assertEqual(proc.returncode, 1)
            self.assertIn("task-failed", proc.stderr)
            self.assertIn("re-run lane3-provision", proc.stderr)
            self.assertFalse((tree.lane3 / "backend" / ".env").exists())

    def test_provision_refuses_an_undefined_task(self):
        with _FixtureTree(with_backend_env=True) as tree:
            tree.write_manifest(lane3_env_task=DECLARED)
            tree.write_stub_mise(0)
            proc = tree.run_script("lane3-provision")
            self.assertEqual(proc.returncode, 1)
            self.assertIn("undefined", proc.stderr)
            self.assertIn("pull --ff-only", proc.stderr)
            self.assertFalse((tree.lane3 / "backend" / ".env").exists())
            self.assertEqual(tree.mise_calls(), [])

    def test_provision_refuses_a_failed_lookup(self):
        with _FixtureTree(with_backend_env=True) as tree:
            tree.write_manifest(raw="not [ toml\n")
            proc = tree.run_script("lane3-provision")
            self.assertEqual(proc.returncode, 1)
            self.assertIn("cannot determine", proc.stderr)
            self.assertFalse((tree.lane3 / "backend" / ".env").exists())

    def test_provision_still_relinks_without_the_key(self):
        with _FixtureTree(with_backend_env=True) as tree:
            proc = tree.run_script("lane3-provision")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertTrue((tree.lane3 / "backend" / ".env").is_symlink())
            self.assertEqual(tree.mise_calls(), [])

    def test_provision_ignores_an_exported_manifest_override(self):
        with _FixtureTree(with_backend_env=True) as tree:
            self._declare(tree)
            stale = tree.root / "stale-projects.toml"
            tree.write_manifest(at=stale)
            proc = tree.run_script("lane3-provision", FORGE_PROJECTS_MANIFEST=str(stale))
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertFalse((tree.lane3 / "backend" / ".env").is_symlink())


def _sha256(path: Path) -> str:
    import hashlib
    return hashlib.sha256(path.read_bytes()).hexdigest()


class Lane3OwnedEnvRefusal(unittest.TestCase):
    """harmonic-forge#875 sticky-wicket patch. The declaration is absent from
    the consulted manifest (the survivor's premise), but the gate worktree
    holds a real, non-symlink backend/.env: both launchers refuse and leave
    it byte-unchanged. AC2 still holds for an absent or symlinked target."""

    OWNED = "NEO4J_URI=bolt://localhost:7699\nNEO4J_PASSWORD=disposable\n"

    def _owned(self, tree) -> Path:
        (tree.lane3 / "backend").mkdir(exist_ok=True)
        env_file = tree.lane3 / "backend" / ".env"
        env_file.write_text(self.OWNED)
        return env_file

    def _assert_untouched(self, env_file, before):
        self.assertTrue(env_file.is_file())
        self.assertFalse(env_file.is_symlink())
        self.assertEqual(_sha256(env_file), before)
        self.assertEqual(list(env_file.parent.glob(".env.pre-relink-*")), [])

    def test_lane3_refuses_an_owned_env_without_a_declaration(self):
        with _FixtureTree(with_backend_env=True) as tree:
            env_file = self._owned(tree)
            before = _sha256(env_file)
            cell = _run3(tree)
            self.assertFalse(cell["launched"])
            self.assertNotEqual(cell["returncode"], 0)
            self.assertIn(str(env_file), cell["stderr"])
            self.assertIn("left untouched", cell["stderr"])
            self._assert_untouched(env_file, before)
            self.assertEqual(_env_record(tree), ["owned-unexpected"])
            self.assertEqual(tree.mise_calls(), [])

    def test_provision_refuses_an_owned_env_without_a_declaration(self):
        with _FixtureTree(with_backend_env=True) as tree:
            env_file = self._owned(tree)
            before = _sha256(env_file)
            proc = tree.run_script("lane3-provision")
            self.assertEqual(proc.returncode, 1)
            self.assertIn(str(env_file), proc.stderr)
            self.assertIn("left untouched", proc.stderr)
            self._assert_untouched(env_file, before)

    def test_an_absent_or_symlinked_target_is_still_relinked(self):
        for shape in ("absent", "symlink"):
            for runner in ("lane3", "lane3-provision"):
                with self.subTest(shape=shape, runner=runner), \
                        _FixtureTree(with_backend_env=True) as tree:
                    main_env = (tree.main / "backend" / ".env").resolve()
                    if shape == "symlink":
                        tll._drift_lane3_env(tree)
                    if runner == "lane3":
                        cell = _run3(tree)
                        self.assertTrue(cell["launched"], cell.get("stderr"))
                        self.assertEqual(cell["extra_env"]["LANE_REFRESH_ENV"], "relinked")
                    else:
                        proc = tree.run_script("lane3-provision")
                        self.assertEqual(proc.returncode, 0, proc.stderr)
                    link = tree.lane3 / "backend" / ".env"
                    self.assertTrue(link.is_symlink())
                    self.assertEqual(link.resolve(), main_env)

    def test_owned_unexpected_is_rendered(self):
        text = notice.build_notice({"LANE": "3", "LANE_REFRESH_STATUS": "current",
                                    "LANE_REFRESH_ENV": "owned-unexpected"})
        self.assertIn("left it untouched", text)


class Lane3EnvTaskAccessor(unittest.TestCase):
    """The three outcomes the launchers branch on, from the fixture forge
    root's copy of the accessor (it reads only its own forge's manifest)."""

    def _call(self, tree, checkout, **env_overrides):
        import os
        env = dict(os.environ)
        env.pop("FORGE_PROJECTS_MANIFEST", None)
        env.update(env_overrides)
        accessor = tree.forge / "tools" / "onboard" / "lane3_env_task.py"
        return subprocess.run([sys.executable, str(accessor), str(checkout)],
                              env=env, capture_output=True, text=True)

    def test_declared_prints_the_task(self):
        with _FixtureTree(with_backend_env=True) as tree:
            tree.write_manifest(lane3_env_task=DECLARED)
            proc = self._call(tree, tree.main)
            self.assertEqual((proc.returncode, proc.stdout.strip()), (0, DECLARED))

    def test_undeclared_prints_nothing(self):
        with _FixtureTree(with_backend_env=True) as tree:
            proc = self._call(tree, tree.main)
            self.assertEqual((proc.returncode, proc.stdout), (0, ""))

    def test_the_manifest_override_is_not_honored(self):
        with _FixtureTree(with_backend_env=True) as tree:
            tree.write_manifest(lane3_env_task=DECLARED)
            stale = tree.root / "stale-projects.toml"
            tree.write_manifest(at=stale)
            proc = self._call(tree, tree.main, FORGE_PROJECTS_MANIFEST=str(stale))
            self.assertEqual((proc.returncode, proc.stdout.strip()), (0, DECLARED))

    def test_every_lookup_failure_exits_2(self):
        with _FixtureTree(with_backend_env=True) as tree:
            for label, raw, checkout in (
                    ("unparseable", "not [ toml\n", tree.main),
                    ("no match", None, tree.root / "elsewhere"),
                    ("empty task", "EMPTY", tree.main)):
                with self.subTest(label):
                    if raw == "EMPTY":
                        tree.write_manifest(lane3_env_task="")
                    elif raw is not None:
                        tree.write_manifest(raw=raw)
                    else:
                        tree.write_manifest()
                    proc = self._call(tree, checkout)
                    self.assertEqual(proc.returncode, 2, proc.stderr)
                    self.assertEqual(proc.stdout, "")
                    self.assertIn("lookup failed", proc.stderr)

    def test_the_real_manifest_declares_hrse_and_nothing_else(self):
        sys.path.insert(0, str(ACCESSOR.parent))
        import manifest
        declared = {p.name: p.protocol.lane3_env_task for p in manifest.load(
            Path(__file__).resolve().parents[2] / "projects.toml")
            if p.protocol and p.protocol.lane3_env_task}
        self.assertEqual(declared, {"hrse": DECLARED})


class Lane3EnvNotice(unittest.TestCase):
    def test_provisioned_is_rendered(self):
        text = notice.build_notice({"LANE": "3", "LANE_REFRESH_STATUS": "current",
                                    "LANE_REFRESH_ENV": "provisioned"})
        self.assertIn("provisioned by this project's declared Lane 3 env task", text)
        self.assertNotIn("was relinked", text)

    def test_provision_failed_is_rendered(self):
        text = notice.build_notice({"LANE": "3", "LANE_REFRESH_STATUS": "current",
                                    "LANE_REFRESH_ENV": "provision-failed"})
        self.assertIn("provisioning FAILED", text)


if __name__ == "__main__":
    unittest.main()
