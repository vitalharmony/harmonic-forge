"""harmonic-forge#1218: a sweep refuses until Lane 3's own environment is ready.

Every subprocess is stubbed; each kill check names the test that must fail."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _lane3_env  # noqa: E402
import l1_post  # noqa: E402


def _done(rc: int, out: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], rc, stdout=out, stderr="")


class Recorder:
    """A subprocess stand-in: records each call, answers by the first match.

    `mise tasks ls --json` answers with `tasks` (no tasks by default), the way
    mise lists what it resolves in the worktree."""

    def __init__(self, answers: dict[str, int] | None = None, tasks: list[str] | None = None,
                 tasks_out: str | None = None):
        self.calls: list[tuple[list[str], str, dict]] = []
        self.answers = answers or {}
        self.tasks_out = tasks_out if tasks_out is not None else json.dumps(
            [{"name": n} for n in (tasks or [])])

    def __call__(self, cmd, cwd=None, **kw):
        self.calls.append((list(cmd), cwd, kw))
        joined = " ".join(cmd)
        for needle, rc in self.answers.items():
            if needle in joined:
                return _done(rc, f"{needle} -> {rc}")
        if "tasks ls" in joined:
            return _done(0, self.tasks_out)
        return _done(0)


def _present(name: str) -> str:
    return f"/usr/bin/{name}"


def _absent(_name: str) -> None:
    return None


class Lane3EnvCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.lane3 = root / "Repo-lane3"
        self.platform = root / "forge"
        for base in (self.lane3, self.platform):
            (base / "tools" / "onboard").mkdir(parents=True)
            (base / "tools" / "onboard" / "platform_link_report.py").write_text("")
        self.root = root

    def check(self, blocks, run, repo="vitalharmony/hrse", which=_present, lane3=None):
        return _lane3_env.check(repo, blocks, lane3=lane3 or self.lane3, run=run,
                                which=which, platform=lambda: self.platform)



class LinksTest(Lane3EnvCase):
    """AC1."""

    def test_links_failing_refuses_naming_worktree_and_repair(self):
        """Kill check 1: a `links_ok` that always says ok would let this pass."""
        reasons = self.check({}, Recorder({"platform_link_report": 1}))
        self.assertEqual(len(reasons), 1)
        self.assertIn(str(self.lane3), reasons[0])
        self.assertIn("mise run hooks-install", reasons[0])

    def test_links_ok_passes(self):
        self.assertEqual(self.check({}, Recorder()), [])

    def test_forge_runs_lane3s_own_link_report(self):
        """Kill check 2: forge's report is Lane 3's own copy, never the platform checkout's."""
        run = Recorder()
        self.check({}, run, repo=_lane3_env.FORGE_REPO)
        cmd, cwd, _kw = run.calls[0]
        self.assertEqual(cmd[1:3], ["-B", str(self.lane3 / "tools" / "onboard" / "platform_link_report.py")])
        self.assertEqual(cmd[3:], ["--project", str(self.lane3)])
        self.assertEqual(cwd, str(self.lane3))

    def test_consuming_repo_runs_platform_copy_against_lane3(self):
        run = Recorder()
        self.check({}, run)
        cmd, _cwd, _kw = run.calls[0]
        self.assertEqual(cmd[1:3], ["-B", str(self.platform / "tools" / "onboard" / "platform_link_report.py")])
        self.assertEqual(cmd[3:], ["--project", str(self.lane3)])

    def test_link_timeout_is_not_ok(self):
        recorder = Recorder()

        def run(cmd, **kw):
            if "platform_link_report" in " ".join(cmd):
                raise subprocess.TimeoutExpired(cmd, 60)
            return recorder(cmd, **kw)
        reasons = self.check({}, run)
        self.assertTrue(reasons and "timed out" in reasons[0])

    def test_missing_worktree_refuses(self):
        reasons = self.check({}, Recorder(), lane3=self.root / "absent-lane3")
        self.assertTrue(reasons and "does not exist" in reasons[0])

    def test_repo_without_lane3_is_not_checked(self):
        projects = {"o/r": SimpleNamespace(protocol=SimpleNamespace(runs_lane3=False), worktrees=[])}
        self.assertIsNone(_lane3_env.lane3_dir("o/r", projects))
        self.assertIsNone(_lane3_env.lane3_dir("o/unknown", projects))

    def test_lane3_dir_picks_the_lane3_worktree(self):
        wts = [self.root / "R-lane2", self.root / "R-lane3"]
        projects = {"o/r": SimpleNamespace(protocol=SimpleNamespace(runs_lane3=True), worktrees=wts)}
        self.assertEqual(_lane3_env.lane3_dir("o/r", projects), wts[1])

    def test_lane3_dir_selects_by_lane_not_name_suffix(self):
        """Preclose pass 1 #5: a template naming the worktree `-gate3` is still Lane 3's."""
        wts = [self.root / "R-gate2", self.root / "R-gate3"]
        projects = {"o/r": SimpleNamespace(protocol=SimpleNamespace(runs_lane3=True), worktrees=wts)}
        self.assertEqual(_lane3_env.lane3_dir("o/r", projects), wts[1])

    def test_lane3_repo_with_no_lane3_worktree_refuses(self):
        projects = {"o/r": SimpleNamespace(protocol=SimpleNamespace(runs_lane3=True), worktrees=[])}
        with self.assertRaisesRegex(_lane3_env.Lane3EnvError, "no Lane 3 worktree"):
            _lane3_env.lane3_dir("o/r", projects)


class RequiresTest(Lane3EnvCase):
    """AC2."""

    def test_missing_bin_refuses_naming_case_and_token(self):
        """Kill check 3."""
        blocks = {"1": "1. TC1 — ready.\n   Requires: bin:no-such-tool\n"}
        reasons = self.check(blocks, Recorder(), which=_absent)
        self.assertEqual(reasons, [f"Lane 3 ({self.lane3}) lacks TC1: bin:no-such-tool"])

    def test_missing_py_refuses_and_imports_isolated_from_lane3(self):
        """Kill check 4: the import runs `-I` under Lane 3's interpreter, in Lane 3."""
        blocks = {"2": "2. TC2 — ready.\n   Requires: py:no_such_module\n"}
        run = Recorder({"import no_such_module": 1})
        reasons = self.check(blocks, run)
        self.assertEqual(reasons, [f"Lane 3 ({self.lane3}) lacks TC2: py:no_such_module"])
        probe = [c for c in run.calls if "import no_such_module" in " ".join(c[0])][0]
        # pass 2 #2: -I ignores PYTHONDONTWRITEBYTECODE, so -B is explicit.
        self.assertEqual(probe[0][:4], ["python3", "-I", "-B", "-c"])
        self.assertEqual(probe[1], str(self.lane3))

    def test_probe_runs_lane3s_venv_interpreter(self):
        """Pass 2 #1: the probe itself, not only `interpreter()`, uses Lane 3's venv."""
        venv = self.lane3 / "backend" / ".venv" / "bin"
        venv.mkdir(parents=True)
        (venv / "python3").write_text("")
        run = Recorder()
        _lane3_env.probe_requires({"1": [("py", "json")]}, self.lane3, run=run)
        self.assertEqual(run.calls[0][0][0], str(venv / "python3"))

    def test_probe_falls_back_to_python3_without_a_venv(self):
        run = Recorder()
        _lane3_env.probe_requires({"1": [("py", "json")]}, self.lane3, run=run)
        self.assertEqual(run.calls[0][0][0], "python3")

    def test_lane3_venv_interpreter_is_preferred(self):
        venv = self.lane3 / "backend" / ".venv" / "bin"
        venv.mkdir(parents=True)
        (venv / "python3").write_text("")
        self.assertEqual(_lane3_env.interpreter(self.lane3), str(venv / "python3"))

    def test_present_requirements_pass(self):
        blocks = {"1": "1. TC1\n   Requires: bin:git; py:json\n"}
        self.assertEqual(self.check(blocks, Recorder()), [])

    def test_malformed_requires_refuses(self):
        """Kill check 5."""
        for line in ("Requires: git", "Requires: bin:git, py:json", "Requires: npm:vite",
                     "Requires: bin:", "Requires: bin:$(rm -rf x)"):
            with self.subTest(line=line), self.assertRaisesRegex(_lane3_env.Lane3EnvError, "malformed"):
                _lane3_env.requires({"1": f"1. TC1\n   {line}\n"})

    def test_bulleted_requires_is_probed(self):
        """Preclose pass 1 #1: a list-item `Requires:` is read, not skipped."""
        for line in ("  - Requires: bin:no-such-tool", "  * **Requires:** bin:no-such-tool"):
            with self.subTest(line=line):
                reasons = self.check({"1": f"- TC1 — x\n{line}\n"}, Recorder(), which=_absent)
                self.assertEqual(reasons, [f"Lane 3 ({self.lane3}) lacks TC1: bin:no-such-tool"])

    def test_inline_requires_refuses_as_unattributed(self):
        """Preclose pass 1 #1: `Requires:` text the line grammar cannot attribute refuses."""
        with self.assertRaisesRegex(_lane3_env.Lane3EnvError, "unattributed"):
            _lane3_env.requires({"1": "TC1 — x. Requires: bin:no-such-tool\n"})

    def test_two_requires_lines_in_one_case_refuses(self):
        with self.assertRaisesRegex(_lane3_env.Lane3EnvError, "more than one"):
            _lane3_env.requires({"1": "1. TC1\n Requires: bin:git\n Requires: py:json\n"})

    def test_malformed_requires_refuses_even_without_lane3(self):
        with patch.object(_lane3_env, "lane3_dir", return_value=None), \
                self.assertRaises(_lane3_env.Lane3EnvError):
            _lane3_env.check("o/none", {"1": "1. TC1\n Requires: junk\n"}, run=Recorder())

    def test_tier_p_requires_is_attributed_to_its_own_case(self):
        """Kill check 7: a Tier P sweep's P2 line belongs to P2/TC2, not P1."""
        sweep = ("## Gate-readiness sweep — H1\n\n**Readiness:** x\n**Blockers:** none.\n"
                 "**Next:** y\n\nWrite tier: P\n\n### Per-case readiness\n"
                 "P1 — ready.\n   Requires: bin:git\n"
                 "P2 — ready.\n   Requires: bin:no-such-tool\n")
        with patch.object(l1_post, "_tier_p_ids", return_value={"tc1", "tc2"}):
            blocks = l1_post.sweep_case_blocks(sweep, tier_p=True)
        reasons = self.check(blocks, Recorder(),
                             which=lambda n: None if n == "no-such-tool" else "/x")
        self.assertEqual(reasons, [f"Lane 3 ({self.lane3}) lacks TC2: bin:no-such-tool"])


class ReadOnlyTest(Lane3EnvCase):
    """AC3."""

    def test_checks_are_read_only(self):
        run = Recorder(tasks=["lane3-preflight"])
        before = sorted(str(p) for p in self.lane3.rglob("*"))
        # The caller's own environment must not supply the guard (preclose
        # pass 1 #7): kill_check.py exports it, which hid its absence here.
        with patch.dict("os.environ"):
            os.environ.pop("PYTHONDONTWRITEBYTECODE", None)
            self.check({"1": "1. TC1\n Requires: py:json\n"}, run)
        for cmd, _cwd, _kw in run.calls:
            args = {Path(a).name for a in cmd[1:]}
            self.assertFalse(args & {"install", "hooks-install", "pip", "npm", "sync_rules.py", "ci"})
        for cmd, _cwd, kw in run.calls:
            if "python" in Path(cmd[0]).name or cmd[0] == sys.executable:
                # preclose pass 1 #7: probes never write bytecode into Lane 3.
                self.assertEqual(kw.get("env", {}).get("PYTHONDONTWRITEBYTECODE"), "1", cmd)
        self.assertEqual(sorted(str(p) for p in self.lane3.rglob("*")), before)


class PreflightTest(Lane3EnvCase):
    """AC5."""

    def test_failing_preflight_refuses(self):
        """Kill check 8: the hrse#2522 shape, Lane 3's own preflight failing."""
        run = Recorder({"run lane3-preflight": 1}, tasks=["lane3-preflight"])
        reasons = self.check({}, run)
        self.assertEqual(len(reasons), 1)
        self.assertIn("lane3-preflight", reasons[0])
        self.assertIn(str(self.lane3), reasons[0])
        self.assertEqual(run.calls[-1][:2], (["mise", "run", "lane3-preflight"], str(self.lane3)))

    def test_passing_preflight_passes(self):
        self.assertEqual(self.check({}, Recorder(tasks=["lane3-preflight"])), [])

    def test_preflight_found_wherever_mise_resolves_it(self):
        """Preclose pass 1 #6: declared in `.mise.toml`, not `mise.toml`, it is
        still found, because mise itself is asked, in Lane 3's worktree."""
        (self.lane3 / ".mise.toml").write_text('[tasks.lane3-preflight]\nrun = "false"\n')
        run = Recorder({"run lane3-preflight": 1}, tasks=["lane3-preflight"])
        reasons = self.check({}, run)
        self.assertTrue(reasons and "lane3-preflight" in reasons[0])
        ls = [c for c in run.calls if "tasks ls" in " ".join(c[0])][0]
        self.assertEqual(ls[1], str(self.lane3))

    def test_preflight_found_by_alias(self):
        """Pass 2 #3: a task exposing lane3-preflight only as an alias is found and run."""
        out = json.dumps([{"name": "gate-preflight", "aliases": ["lane3-preflight"]}])
        run = Recorder({"run lane3-preflight": 1}, tasks_out=out)
        reasons = self.check({}, run)
        self.assertTrue(reasons and "lane3-preflight" in reasons[0])

    def test_unreadable_mise_config_refuses(self):
        """Preclose pass 1 #3: a config mise cannot read refuses, never reads as
        "not declared"."""
        for run in (Recorder({"tasks ls": 1}), Recorder(tasks_out="not json")):
            with self.subTest(run=run), self.assertRaises(_lane3_env.Lane3EnvError):
                self.check({}, run)

    def test_no_declared_preflight_is_not_run(self):
        run = Recorder(tasks=["other"])
        self.check({}, run)
        self.assertFalse(any(c[0][:2] == ["mise", "run"] for c in run.calls))


class WiringTest(unittest.TestCase):
    """l1_post wiring; AC4 (the dry run takes the same collected steps)."""

    def test_validate_lane3_env_refuses_through_fail(self):
        with patch.object(_lane3_env, "check", return_value=["Lane 3 not ready"]), \
                self.assertRaisesRegex(SystemExit, "Lane 3's environment is not ready"):
            l1_post.validate_lane3_env("## Gate-readiness sweep — H1\n", "", "vitalharmony/hrse")

    def test_validate_lane3_env_malformed_requires_refuses(self):
        with patch.object(_lane3_env, "check", side_effect=_lane3_env.Lane3EnvError("TC1: malformed")), \
                self.assertRaisesRegex(SystemExit, "malformed"):
            l1_post.validate_lane3_env("x", "", "vitalharmony/hrse")

    def test_requires_outside_any_case_refuses(self):
        """Preclose pass 1 #2: a `Requires:` line the per-case split drops refuses."""
        sweep = "## Gate-readiness sweep — H1\n\nRequires: bin:no-such-tool\n\n1. TC1 — ready.\n"
        with patch.object(_lane3_env, "check", return_value=[]), \
                self.assertRaisesRegex(SystemExit, "outside any case block"):
            l1_post.validate_lane3_env(sweep, "", "vitalharmony/hrse")



class FenceTest(Lane3EnvCase):
    """harmonic-forge#1225 AC2: `fence:` paths go through the hook's own predicate."""

    OUTSIDE = "~/Harmonic_Projects/HRSE2-lane3/backend/x.json"
    INSIDE = "~/Harmonic_Projects/testplan/h1/x.json"

    def setUp(self) -> None:
        super().setUp()
        prior = os.environ.pop("LANE", None)
        self.addCleanup(lambda: os.environ.__setitem__("LANE", prior) if prior is not None
                        else os.environ.pop("LANE", None))

    def blocks(self, path):
        return {"3": f"3. TC3 — ready.\n   Requires: fence:{path}\n"}

    def test_outside_path_refuses_naming_case_and_path(self):
        """Kill checks 2 and 3: LANE is absent here, so a pass needs the LANE=3 call."""
        reasons = self.check(self.blocks(self.OUTSIDE), Recorder())
        self.assertIn(f"TC3: fence:{self.OUTSIDE} is outside Lane 3's write targets", reasons)

    def test_testplan_path_passes(self):
        self.assertEqual(self.check(self.blocks(self.INSIDE), Recorder()), [])

    def test_refuses_on_every_return_path(self):
        want = f"TC3: fence:{self.OUTSIDE} is outside Lane 3's write targets"
        missing = self.check(self.blocks(self.OUTSIDE), Recorder(), lane3=self.root / "absent")
        self.assertIn(want, missing)
        with patch.object(_lane3_env, "lane3_dir", return_value=None):
            none = _lane3_env.check("o/r", self.blocks(self.OUTSIDE), run=Recorder())
        self.assertEqual(none, [want])

    def test_lane_restored_absent_and_set(self):
        self.check(self.blocks(self.OUTSIDE), Recorder())
        self.assertNotIn("LANE", os.environ)
        os.environ["LANE"] = "1"
        self.check(self.blocks(self.OUTSIDE), Recorder())
        self.assertEqual(os.environ["LANE"], "1")

    def test_lane_restored_when_predicate_raises(self):
        os.environ["LANE"] = "1"
        import block_lane1_status_claims as hook
        with patch.object(hook, "lane3_write_outside_testplan", side_effect=RuntimeError):
            with self.assertRaises(RuntimeError):
                self.check(self.blocks(self.OUTSIDE), Recorder())
        self.assertEqual(os.environ["LANE"], "1")

    def test_relative_path_refuses(self):
        """Kill check 4."""
        with self.assertRaisesRegex(_lane3_env.Lane3EnvError, "TC3: fence path 'rel/x' is relative"):
            self.check(self.blocks("rel/x"), Recorder())

    def test_fence_is_not_probed_as_an_import(self):
        run = Recorder()
        self.check(self.blocks(self.INSIDE), run)
        self.assertFalse([c for c in run.calls if "import " in " ".join(c[0])])

if __name__ == "__main__":
    unittest.main()
