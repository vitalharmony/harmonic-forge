#!/usr/bin/env python3
"""The onboarding hooks readers render the lane hook set for the root a lane
launch resolves, never for the checkout the report runs from
(harmonic-forge#1526 round 1 survivor 2), from one launch view for both the
set and the root (sticky-wicket B1), with the root pinned in every onboarding
fixture (sticky-wicket B2).

Hermetic: HOME and XDG_CONFIG_HOME are temporary, and the redirected root is a
copy of this checkout's `tools/hooks`.
"""
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

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import forge_onboard as fo  # noqa: E402
import instance  # noqa: E402
import manifest as mf  # noqa: E402
from test_forge_onboard import _hermetic_home  # noqa: E402

SET = HERE.parent / "lane" / "lane_hooks.json"
SCRIPT = "require_ci_plan.py"


class LaunchRootRendering(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()
        self.home = self.tmp / "home"
        self.copy = self.tmp / "forge-copy"
        shutil.copytree(fo._THIS_CHECKOUT / "tools" / "hooks", self.copy / "tools" / "hooks",
                        ignore=shutil.ignore_patterns("__pycache__"))
        self.config = self.home / ".config" / "harmonic-forge" / "instance.toml"
        self.config.parent.mkdir(parents=True)

    def environ(self, home_link: Path, config: str, **env: str):
        link = self.home / "harmonic-forge"
        if link.is_symlink():
            link.unlink()
        link.symlink_to(home_link)
        self.config.write_text('lane_hook_overlay = "operator"\n' + config, encoding="utf-8")
        base = {k: v for k, v in os.environ.items() if k != "HARMONIC_FORGE_ROOT"}
        return mock.patch.dict(os.environ, {**base, "HOME": str(self.home),
                                            "XDG_CONFIG_HOME": str(self.home / ".config"), **env},
                               clear=True)

    def project(self, name: str) -> mf.Project:
        """A project whose own copy of a set entry names `${HOME}/harmonic-forge`."""
        data = json.loads(SET.read_text(encoding="utf-8").replace("@FORGE_ROOT@", "${HOME}/harmonic-forge"))
        hooks = {event: [{**b, "hooks": [h for h in b["hooks"] if SCRIPT in h["command"]]}
                         for b in blocks if any(SCRIPT in h["command"] for h in b["hooks"])]
                 for event, blocks in data["hooks"].items()}
        hooks = {event: blocks for event, blocks in hooks.items() if blocks}
        checkout = self.tmp / name
        (checkout / ".claude").mkdir(parents=True)
        (checkout / ".claude" / "settings.json").write_text(json.dumps({"hooks": hooks}), encoding="utf-8")
        return mf.Project(name=name, prefix="Z", path=str(checkout))

    def test_a_home_linked_redirect_renders_home_relative_and_is_not_doubled(self):
        """The report's checkout is not what HOME links; the launch root is."""
        for label, config, env in (("env", "", {"HARMONIC_FORGE_ROOT": str(self.copy)}),
                                   ("instance.toml", f'forge_root = "{self.copy}"\n', {})):
            with self.subTest(source=label), self.environ(self.copy, config, **env):
                data, error = fo._lane_set()
                self.assertIsNone(error)
                commands = [h["command"] for blocks in data["hooks"].values() for b in blocks for h in b["hooks"]]
                self.assertTrue(all("${HOME}/harmonic-forge/tools/hooks/" in c for c in commands), commands[0])
                check = fo.check_hooks(self.project(f"linked-{label}"))
                self.assertEqual(check.status, fo.OK, check.detail)
                self.assertNotIn("runs twice", check.detail)

    def test_the_doubled_cli_defaults_to_the_launch_root(self):
        """`lane_hookset.py doubled` with no --forge-root is the same reader."""
        settings = self.tmp / "user.json"
        project = self.project("cli")
        shutil.copy(Path(project.path) / ".claude" / "settings.json", settings)
        with self.environ(self.copy, "", HARMONIC_FORGE_ROOT=str(self.copy)):
            proc = subprocess.run([sys.executable, "-I", "-E", str(SET.parent / "lane_hookset.py"),
                                   "doubled", "--user-settings", str(settings)],
                                  capture_output=True, text=True, timeout=60, env=dict(os.environ))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("REMOVABLE", proc.stdout)
        self.assertNotIn("DOUBLED", proc.stdout)

    def test_an_unlinked_redirect_reports_the_project_entries_that_run_twice(self):
        """HOME links the report's checkout, but the launch runs an unlinked
        root: the project's `${HOME}` entries then run beside the set's."""
        with self.environ(fo._THIS_CHECKOUT, "", HARMONIC_FORGE_ROOT=str(self.copy)):
            check = fo.check_hooks(self.project("unlinked"))
        self.assertEqual(check.status, fo.FAIL, check.detail)
        self.assertIn(f"{SCRIPT} runs twice", check.detail)

    def test_the_set_and_the_root_both_come_from_the_launch_view(self):
        """Sticky-wicket B1: the report reads the set the launcher passes (the
        platform checkout's), never this checkout's, with the same root."""
        launcher = self.copy
        (launcher / "tools" / "lane").mkdir()
        shutil.copy(SET.parent / "lane_hooks.operator.json", launcher / "tools" / "lane")
        data = json.loads(SET.read_text(encoding="utf-8"))
        handler = data["hooks"]["Stop"][0]["hooks"][0]
        handler["timeout"] = handler.get("timeout", 10) + 7  # differs by one handler
        (launcher / "tools" / "lane" / "lane_hooks.json").write_text(json.dumps(data), encoding="utf-8")
        with self.environ(launcher, ""), mock.patch.object(fo, "platform_source", return_value=launcher):
            got, error = fo._lane_set()
            hookset = fo._lane_hookset_module()
            want = hookset.load_effective(launcher / "tools" / "lane" / "lane_hooks.json", "operator",
                                          forge_root=launcher)
            here = hookset.load_effective(SET, "operator", forge_root=launcher)
        self.assertIsNone(error)
        self.assertEqual(got, want)
        self.assertNotEqual(got, here)


class HermeticRoot(unittest.TestCase):
    """Sticky-wicket B2 sentinel: `_hermetic_home` pins HARMONIC_FORGE_ROOT, so
    an exported one never reaches the onboarding suite."""

    def test_the_resolved_root_is_the_checkout_under_test(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(os.environ, {"HARMONIC_FORGE_ROOT": "/nonexistent/elsewhere"}):
            _hermetic_home(self, Path(tmp).resolve())
            self.assertEqual(instance.launch_view(fo.platform_source())[1], fo._THIS_CHECKOUT)
            self.assertIsNone(fo._lane_set()[1])
            self.doCleanups()


if __name__ == "__main__":
    unittest.main()
