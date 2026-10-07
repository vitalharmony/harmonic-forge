"""harmonic-forge#659 AC2 -- `belt_plan.py` is the one source of the arming calls.

harmonic-forge#917 AC1 -- the Monitor command carries the workspace of the
checkout `belt_plan.py` runs from, a lane worktree resolves to its main
checkout's workspace, a per-issue worktree resolves through git, and a
directory outside every onboarded checkout is refused.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "belt_plan.py"
REPO = HERE.parent.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "gh"))
import belt_plan  # noqa: E402
import watch_lane_posts  # noqa: E402

PROTOCOL = (
    '[project.protocol]\nworktree_name = "{checkout}-lane{lane}"\n'
    'l1_post_task = "l1-post"\nlane_comment_task = "lane-comment"\n'
    'gate_checkout_task = "gate-checkout"\nlane3_begin_task = "lane3-begin"\n'
    'lane3_end_task = "lane3-end"\nruns_lane3 = true\n')


def _run(lane, cwd=REPO, env_extra=None):
    env = {k: v for k, v in os.environ.items() if k != "LANE"}
    if lane is not None:
        env["LANE"] = lane
    env.update(env_extra or {})
    return subprocess.run([sys.executable, str(SCRIPT)], env=env, cwd=str(cwd),
                          capture_output=True, text=True)


def _entry(lane, workspace):
    return next(e for e in watch_lane_posts.CANONICAL_BELTS[lane]
                if e["workspace"] == workspace)


def _git(*args):
    subprocess.run(["git", *args], check=True, capture_output=True, text=True)


class _Fixture:
    """Three onboarded checkouts in three workspaces, a lane worktree, a per-issue
    git worktree and an unregistered directory. Hermetic: the real checkout this
    test runs from (a CI runner path, say) is registered nowhere."""

    def __init__(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = self.root = Path(self._tmp.name)
        self.checkouts = {}
        rows = []
        for name, ws, prefix in (("alpha", "vh", "A"), ("beta", "leasepal", "B"),
                                 ("gamma", "kenekted", "C")):
            checkout = root / name
            checkout.mkdir()
            _git("-C", str(checkout), "init", "-q", "-b", "main")
            _git("-C", str(checkout), "-c", "user.name=t", "-c", "user.email=t@t",
                 "commit", "-q", "--allow-empty", "-m", "init")
            self.checkouts[name] = checkout
            rows.append(f'[[project]]\nname = "{name}"\nprefix = "{prefix}"\n'
                        f'repo = "o/{name}"\naccount = "vitalharmony"\n'
                        f'path = "{checkout}"\nonboarded = true\nworkspace = "{ws}"\n'
                        + PROTOCOL)
        self.lane2 = root / "alpha-lane2"
        self.lane2.mkdir()
        self.impl = root / "impl" / "beta-12-impl"
        _git("-C", str(self.checkouts["beta"]), "worktree", "add", "-q", "--detach",
             str(self.impl))
        self.outside = root / "outside"
        self.outside.mkdir()
        manifest = root / "projects.toml"
        manifest.write_text("\n".join(rows), encoding="utf-8")
        self.env = {"FORGE_PROJECTS_MANIFEST": str(manifest)}

    def cleanup(self):
        self._tmp.cleanup()


FIXTURE: "_Fixture | None" = None


def setUpModule():
    global FIXTURE
    FIXTURE = _Fixture()


def tearDownModule():
    FIXTURE.cleanup()


def _run_vh(lane):
    """`belt_plan.py` from the fixture's `vh` checkout."""
    return _run(lane, cwd=FIXTURE.checkouts["alpha"], env_extra=FIXTURE.env)


class PlanOutput(unittest.TestCase):
    def test_monitor_command_is_built_from_the_table(self):
        for lane in ("1", "2", "3"):
            with self.subTest(lane=lane):
                out = _run_vh(lane)
                self.assertEqual(out.returncode, 0, out.stderr)
                plan = json.loads(out.stdout)
                expected = ("python3 ~/harmonic-forge/tools/gh/watch_lane_posts.py "
                            + " ".join(_entry(lane, "vh")["argv"]))
                self.assertEqual(plan["monitor"], {
                    "command": expected,
                    "description": f"Lane {lane} belt (vh)",
                    "timeout_ms": 1800000,
                })

    def test_command_follows_a_table_change_rather_than_a_retyped_copy(self):
        """Mutating the table must change the output; a retyped string would not."""
        entry = _entry("2", "vh")
        original = entry["argv"]
        try:
            entry["argv"] = ["--sentinel-flag"]
            self.assertTrue(belt_plan.canonical_calls("2", "vh")["monitor"]["command"]
                            .endswith("watch_lane_posts.py --sentinel-flag"))
        finally:
            entry["argv"] = original

    def test_loop_is_the_literal_invocation(self):
        plan = json.loads(_run_vh("3").stdout)
        self.assertEqual(plan["loop"], {"skill": "loop",
                                        "args": "10m proactively find work to do"})

    def test_report_template_names_both_ids(self):
        report = json.loads(_run_vh("1").stdout)["report"]
        self.assertIn("monitor task id", report)
        self.assertIn("loop job id", report)

    def test_no_lane_3_sweep_in_the_plan(self):
        self.assertNotIn("--sweep-for", _run_vh("3").stdout)


class WorkspaceScoping(unittest.TestCase):
    """harmonic-forge#917 AC1, against a fixture manifest of three workspaces."""

    def _command(self, cwd):
        out = _run("2", cwd=cwd, env_extra=FIXTURE.env)
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout)["monitor"]["command"]

    def test_each_checkout_prints_its_own_workspace(self):
        commands = {name: self._command(path) for name, path in FIXTURE.checkouts.items()}
        self.assertTrue(commands["alpha"].endswith("--workspace vh"), commands["alpha"])
        self.assertTrue(commands["beta"].endswith("--workspace leasepal"), commands["beta"])
        self.assertTrue(commands["gamma"].endswith("--workspace kenekted"), commands["gamma"])
        self.assertEqual(len(set(commands.values())), 3)

    def test_a_lane_worktree_prints_its_main_checkouts_workspace(self):
        self.assertTrue(self._command(FIXTURE.lane2)
                        .endswith("--workspace vh"))

    def test_a_per_issue_worktree_resolves_through_git(self):
        self.assertTrue(self._command(FIXTURE.impl).endswith("--workspace leasepal"))

    def test_outside_every_onboarded_checkout_is_refused(self):
        out = _run("2", cwd=FIXTURE.outside, env_extra=FIXTURE.env)
        self.assertEqual(out.returncode, 2)
        self.assertEqual(out.stdout, "")
        self.assertIn("no workspace", out.stderr)


class RefusesWithoutALane(unittest.TestCase):
    def test_invalid_or_missing_lane_exits_2(self):
        for lane in (None, "", "0", "4", "lane3", "l3"):
            with self.subTest(lane=lane):
                out = _run(lane)
                self.assertEqual(out.returncode, 2)
                self.assertEqual(out.stdout, "")
                self.assertIn("not 1, 2 or 3", out.stderr)

    def test_canonical_calls_raises_for_an_invalid_lane_or_workspace(self):
        with self.assertRaises(KeyError):
            belt_plan.canonical_calls("4", "vh")
        with self.assertRaises(KeyError):
            belt_plan.canonical_calls("2", "no-such-workspace")


if __name__ == "__main__":
    unittest.main()
