"""A lane takes its name and pane title from its repo (harmonic-forge#1014).

AC1 label per workspace / unregistered checkout, AC2 the Claude session name
and passthrough precedence, AC3 the pinned tmux pane title, AC4 the launcher
no longer naming lanes, AC5 the teardown name sets. Stdlib unittest; the
launcher cases reuse test_lane_launchers' stubbed fixture tree.
"""
from __future__ import annotations

import os
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

LANE_DIR = Path(__file__).resolve().parent
ONBOARD = LANE_DIR.parent / "onboard"
sys.path.insert(0, str(LANE_DIR))
sys.path.insert(0, str(ONBOARD))

import lane_targets as lt  # noqa: E402
import manifest_identity  # noqa: E402
import manifest_labels as ml  # noqa: E402
from test_lane_launchers import _FixtureTree, _agent_args  # noqa: E402

REAL_MANIFEST = LANE_DIR.parent.parent / "projects.toml"

_STUB_TMUX = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "{log}"
"""


_PROTOCOL = (
    "[project.protocol]\n"
    'worktree_name = "{checkout}-lane{lane}"\n'
    'l1_post_task = "l1-post"\nlane_comment_task = "lane-comment"\n'
    'gate_checkout_task = "gate-checkout"\nlane3_begin_task = "lane3-begin"\n'
    'lane3_end_task = "lane3-end"\ngate_task = "check"\nruns_lane3 = true\n')


def _manifest(root: Path) -> tuple[Path, dict[str, Path]]:
    """A three-venture manifest whose checkouts are dirs under `root`."""
    dirs = {ws: root / name for ws, name in
            (("vh", "HRSE2"), ("leasepal", "LeasePAL"), ("kenekted", "kenekted-platform"))}
    body = '[workspace_labels]\nvh = "vh"\nleasepal = "lp"\nkenekted = "k"\n\n'
    for i, (ws, d) in enumerate(dirs.items()):
        d.mkdir()
        body += (f'[[project]]\nname = "p{i}"\nprefix = "{"ABC"[i]}"\n'
                 f'repo = "o/p{i}"\naccount = "a"\npath = "{d}"\nonboarded = true\nworkspace = "{ws}"\n'
                 + _PROTOCOL + "\n")
    path = root / "projects.toml"
    path.write_text(body)
    return path, dirs


class LabelTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        self.manifest, self.dirs = _manifest(self.root)

    def test_label_per_workspace(self):
        cases = (("vh", "lane2", "vh:lane2"), ("leasepal", "lane1", "lp:lane1"),
                 ("kenekted", "lane3", "k:lane3"), ("vh", "advisor", "vh:advisor"))
        for ws, lane, want in cases:
            with self.subTest(ws=ws, lane=lane):
                self.assertEqual(ml.session_label(self.dirs[ws], lane, self.manifest), (want, ""))
        # A subdirectory resolves the same as the checkout.
        sub = self.dirs["vh"] / "tools"
        sub.mkdir()
        self.assertEqual(ml.session_label(sub, "lane2", self.manifest)[0], "vh:lane2")

    def test_unregistered_checkout_gets_the_bare_lane_and_a_reason(self):
        stray = self.root / "stray"
        stray.mkdir()
        label, reason = ml.session_label(stray, "lane2", self.manifest)
        self.assertEqual(label, "lane2")
        self.assertIn("not in projects.toml", reason)

    def test_missing_label_falls_back_to_the_workspace_name(self):
        self.manifest.write_text(self.manifest.read_text().replace('leasepal = "lp"\n', ""))
        self.assertEqual(ml.session_label(self.dirs["leasepal"], "lane2", self.manifest)[0],
                         "leasepal:lane2")
        self.assertEqual(ml.session_label(self.dirs["vh"], "lane2", self.manifest)[0], "vh:lane2")

    def test_colliding_labels_are_a_manifest_error(self):
        self.manifest.write_text(self.manifest.read_text().replace('kenekted = "k"', 'kenekted = "vh"'))
        with self.assertRaises(manifest_identity.ManifestError):
            ml.workspace_labels(self.manifest)

    def test_every_manifest_workspace_has_a_label(self):
        table = tomllib.loads(REAL_MANIFEST.read_text())["workspace_labels"]
        self.assertLessEqual(set(manifest_identity.workspaces(REAL_MANIFEST)), set(table))
        self.assertEqual(table, {"vh": "vh", "leasepal": "lp", "kenekted": "k"})

    def test_teardown_names_include_labels(self):
        names = ml.lane_label_names(self.manifest)
        self.assertLessEqual({"vh:lane1", "lp:lane2", "k:lane3", "lane1", "lane2", "lane3"}, names)
        self.assertEqual(ml.advisor_label_names(self.manifest), {"vh:advisor", "lp:advisor", "k:advisor"})
        with mock.patch.dict(os.environ, {"FORGE_PROJECTS_MANIFEST": str(self.manifest)}):
            self.assertEqual(lt.advisor_names(),
                             {"Advisor", "Lane Advisor", "vh:advisor", "lp:advisor", "k:advisor"})
        # lanes-claude's teardown set is the legacy names plus both label sets.
        text = (LANE_DIR / "lanes-claude").read_text()
        self.assertIn("_LABELLED = lane_label_names()", text)
        self.assertIn('LANE_NAMES = {"Lane 1", "Lane 2", "Lane 3"} | _LABELLED | ADVISOR_SET', text)
        self.assertIn("ADVISOR_SET = advisor_names()", text)
        self.assertIn("advisor_name_set=ADVISOR_SET", text)

    def test_advisor_names_falls_back_to_the_literal(self):
        with mock.patch.dict(os.environ, {"FORGE_PROJECTS_MANIFEST": str(self.root / "absent.toml")}):
            self.assertEqual(lt.advisor_names(), {"Advisor", "Lane Advisor"})

    def test_cli_prints_label_and_exits_zero(self):
        import subprocess
        env = {**os.environ, "FORGE_PROJECTS_MANIFEST": str(self.manifest)}
        done = subprocess.run([sys.executable, str(ONBOARD / "manifest_labels.py"), "label",
                               str(self.dirs["kenekted"]), "lane1"],
                              capture_output=True, text=True, env=env)
        self.assertEqual((done.returncode, done.stdout, done.stderr), (0, "k:lane1\n", ""))
        done = subprocess.run([sys.executable, str(ONBOARD / "manifest_labels.py"), "label",
                               str(self.root), "lane1"], capture_output=True, text=True, env=env)
        self.assertEqual((done.returncode, done.stdout), (0, "lane1\n"))
        self.assertIn("not in projects.toml", done.stderr)


class LauncherTests(unittest.TestCase):
    """The wrappers themselves, in the stubbed fixture tree (unregistered: `laneN`)."""

    def setUp(self):
        self.tree = _FixtureTree().__enter__()
        self.addCleanup(self.tree.__exit__, None, None, None)
        self.tmux_log = self.tree.root / "tmux.log"
        stub = self.tree.stub_bin / "tmux"
        stub.write_text(_STUB_TMUX.format(log=self.tmux_log))
        stub.chmod(0o755)

    def _tmux_calls(self) -> list[str]:
        return self.tmux_log.read_text().splitlines() if self.tmux_log.exists() else []

    def test_claude_gets_the_label_as_its_session_name(self):
        for lane in ("1", "2", "3"):
            with self.subTest(lane=lane):
                cell = self.tree.run(lane, ["-p", "hi"], LANE_CLI="claude")
                self.assertTrue(cell["launched"], cell.get("stderr"))
                args = _agent_args(cell)
                self.assertEqual(args[-4:], ["--name", f"lane{lane}", "-p", "hi"])
                self.assertIn(f"lane{lane}: session label 'lane{lane}'", cell["stderr"])

    def test_codex_and_gemini_get_no_name(self):
        for agent in ("codex", "gemini"):
            with self.subTest(agent=agent):
                cell = self.tree.run("2", [], LANE_CLI=agent)
                self.assertTrue(cell["launched"], cell.get("stderr"))
                self.assertNotIn("lane2", _agent_args(cell))

    def test_passthrough_name_wins(self):
        for given in (["--name", "mine"], ["--name=mine"], ["-n", "mine"]):
            with self.subTest(given=given):
                cell = self.tree.run("2", given, LANE_CLI="claude")
                self.assertTrue(cell["launched"], cell.get("stderr"))
                args = _agent_args(cell)
                self.assertNotIn("lane2", args)
                self.assertEqual(args[-len(given):], given)

    def test_tmux_pins_pane_title(self):
        for agent in ("claude", "codex"):
            self.tmux_log.unlink(missing_ok=True)
            with self.subTest(agent=agent):
                cell = self.tree.run("2", [], LANE_CLI=agent, TMUX_PANE="%99", TMUX="/tmp/x,1,0")
                self.assertTrue(cell["launched"], cell.get("stderr"))
                self.assertEqual(self._tmux_calls(), [
                    "set-option -p -t %99 allow-set-title off",
                    "select-pane -t %99 -T lane2",
                ])

    def test_no_tmux_calls_without_tmux_pane(self):
        # The operator may be running the suite inside a lane pane: run_script
        # must strip TMUX_PANE, so the wrapper never touches tmux.
        out = self.tree.root / "capture.json"
        with mock.patch.dict(os.environ, {"TMUX_PANE": "%98", "TMUX": "/tmp/x,1,0",
                                          "LANE_LABEL": "leaked:lane9"}):
            done = self.tree.run_script("lane2", ["--agent", "claude"],
                                        LANE_CAPTURE_OUT=str(out))
        self.assertEqual(self._tmux_calls(), [], done.stderr)
        self.assertNotIn("leaked:lane9", out.read_text() if out.exists() else "")

    def test_refused_launch_never_pins(self):
        # lane_refresh refuses a dirty lane2 worktree before lane_identity runs.
        (self.tree.lane2 / "README.md").write_text("dirty\n")
        cell = self.tree.run("2", [], LANE_CLI="claude", TMUX_PANE="%97")
        self.assertFalse(cell["launched"])
        self.assertIn("refusing to start", cell["stderr"])
        self.assertEqual(self._tmux_calls(), [])

    def test_label_is_never_exported(self):
        text = (LANE_DIR / "_lane_identity.sh").read_text()
        self.assertNotIn("export LANE_LABEL", text)


class LanesClaudeTests(unittest.TestCase):
    def test_launcher_passes_no_lane_name(self):
        text = (LANE_DIR / "lanes-claude").read_text()
        self.assertNotIn("--name 'Lane", text)
        for n in (1, 2, 3):
            self.assertIn(f'"lane{n} --agent claude; rc=', text)
            self.assertNotIn(f'-T "L{n} · Claude"', text)
        self.assertNotIn("--name 'Advisor'", text)
        self.assertEqual(text.count("--name '$advisor_label'"), 2)
        self.assertEqual(text.count("pin_advisor_title\n"), 2)
        self.assertIn('allow-set-title off', text)


if __name__ == "__main__":
    unittest.main()
