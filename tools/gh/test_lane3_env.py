"""harmonic-forge#1218: a sweep refuses until Lane 3's own environment is ready.

Every subprocess is stubbed; each kill check names the test that must fail."""

from __future__ import annotations

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
    """A subprocess stand-in: records each call, answers by the first match."""

    def __init__(self, answers: dict[str, int] | None = None):
        self.calls: list[tuple[list[str], str]] = []
        self.answers = answers or {}

    def __call__(self, cmd, cwd=None, **_kw):
        self.calls.append((list(cmd), cwd))
        joined = " ".join(cmd)
        for needle, rc in self.answers.items():
            if needle in joined:
                return _done(rc, f"{needle} -> {rc}")
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

    def preflight(self, run: str) -> None:
        (self.lane3 / "mise.toml").write_text(f'[tasks.lane3-preflight]\nrun = "{run}"\n')


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
        cmd, cwd = run.calls[0]
        self.assertEqual(cmd[1], str(self.lane3 / "tools" / "onboard" / "platform_link_report.py"))
        self.assertEqual(cmd[2:], ["--project", str(self.lane3)])
        self.assertEqual(cwd, str(self.lane3))

    def test_consuming_repo_runs_platform_copy_against_lane3(self):
        run = Recorder()
        self.check({}, run)
        cmd, _cwd = run.calls[0]
        self.assertEqual(cmd[1], str(self.platform / "tools" / "onboard" / "platform_link_report.py"))
        self.assertEqual(cmd[2:], ["--project", str(self.lane3)])

    def test_link_timeout_is_not_ok(self):
        def run(cmd, **_kw):
            raise subprocess.TimeoutExpired(cmd, 60)
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
        self.assertEqual(probe[0][:3], ["python3", "-I", "-c"])
        self.assertEqual(probe[1], str(self.lane3))

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
        self.preflight("true")
        run = Recorder()
        before = sorted(str(p) for p in self.lane3.rglob("*"))
        self.check({"1": "1. TC1\n Requires: py:json\n"}, run)
        for cmd, _cwd in run.calls:
            args = {Path(a).name for a in cmd[1:]}
            self.assertFalse(args & {"install", "hooks-install", "pip", "npm", "sync_rules.py", "ci"})
        self.assertEqual(sorted(str(p) for p in self.lane3.rglob("*")), before)


class PreflightTest(Lane3EnvCase):
    """AC5."""

    def test_failing_preflight_refuses(self):
        """Kill check 8: the hrse#2522 shape, Lane 3's own preflight failing."""
        self.preflight("false")
        run = Recorder({"lane3-preflight": 1})
        reasons = self.check({}, run)
        self.assertEqual(len(reasons), 1)
        self.assertIn("lane3-preflight", reasons[0])
        self.assertIn(str(self.lane3), reasons[0])
        self.assertEqual(run.calls[-1], (["mise", "run", "lane3-preflight"], str(self.lane3)))

    def test_passing_preflight_passes(self):
        self.preflight("true")
        self.assertEqual(self.check({}, Recorder()), [])

    def test_no_declared_preflight_is_not_run(self):
        (self.lane3 / "mise.toml").write_text('[tasks.other]\nrun = "true"\n')
        run = Recorder()
        self.check({}, run)
        self.assertFalse(any("lane3-preflight" in " ".join(c[0]) for c in run.calls))


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

    def test_both_sweep_call_sites_run_the_step(self):
        """Kill check 6: the step sits beside `validate_sweep` at both call
        sites, inside `_collect`, the path the dry run also takes."""
        source = Path(l1_post.__file__).read_text()
        self.assertEqual(source.count("validate_lane3_env(sweep_body, spec_once(), repo)"), 1)
        self.assertEqual(source.count("validate_lane3_env(body, spec_once(), repo)"), 1)


if __name__ == "__main__":
    unittest.main()
