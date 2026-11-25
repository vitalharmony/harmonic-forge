#!/usr/bin/env python3
"""Tests for the forge root in the lane hook set (harmonic-forge#1526): the
stored placeholder, its rendering (`spelling`, `render`), the build against a
root, the `root` subcommand, and `hook_targets`' basename rule.

Hermetic: HOME is a temporary directory and the forge checkout is a copy of
this one's `tools/hooks` and lane sets, at a path not named `harmonic-forge`
and not under that HOME. The launcher half is `test_lane_forge_root_launch.py`.
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
ROOT = HERE.parent.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "onboard"))
import lane_hookset as hs  # noqa: E402
from hook_targets import unresolvable_hook_targets  # noqa: E402

SET_FILE = HERE / "lane_hooks.json"
OP = "${HOME}/harmonic-forge"


class _Tmp(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()
        self.home = self.tmp / "home"
        self.home.mkdir()
        patcher = mock.patch.dict(os.environ, {"HOME": str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def copy(self, where: Path) -> Path:
        """A forge checkout at `where`: this checkout's hooks and lane sets."""
        shutil.copytree(ROOT / "tools" / "hooks", where / "tools" / "hooks",
                        ignore=shutil.ignore_patterns("__pycache__"))
        for name in ("lane_hooks.json", "lane_hooks.operator.json"):
            (where / "tools" / "lane").mkdir(parents=True, exist_ok=True)
            shutil.copy(HERE / name, where / "tools" / "lane" / name)
        return where

    def link(self, name: str, target: Path) -> None:
        (self.home / name).symlink_to(target)


def _commands(data: dict) -> list[str]:
    return [h["command"] for _e, _m, h in hs.handlers(data["hooks"])]


def _target(command: str) -> Path:
    """The script a rendered command runs, after the shell's expansion."""
    path = command.split('"')[1]
    return Path(os.path.expandvars(path))


class OutsideHome(_Tmp):
    """AC1 / Test Case 1: a checkout named `forge-x`, outside an empty HOME."""

    def test_every_command_targets_an_existing_file(self):
        forge = self.copy(self.tmp / "elsewhere" / "forge-x")
        core = hs.load_set(forge / "tools" / "lane" / "lane_hooks.json", forge_root=forge)
        effective = hs.load_effective(forge / "tools" / "lane" / "lane_hooks.json", "operator",
                                      forge_root=forge)
        for data, count in ((core, 47), (effective, 56)):
            commands = _commands(data)
            self.assertEqual(len(commands), count)
            for command in commands:
                with self.subTest(command=command):
                    self.assertTrue(_target(command).is_file(), command)
                    self.assertTrue(command.split('"')[1].startswith(f"{forge}/tools/hooks/"), command)


class Spelling(_Tmp):
    """Test Case 9 (N1): `$HOME`'s links decide the form, never the basename."""

    def test_a_home_link_renders_home_relative_whatever_the_checkout_is_named(self):
        forge = self.copy(self.tmp / "forge-x")
        self.link("harmonic-forge", forge)
        self.assertEqual(hs.spelling(forge), OP)
        for command in _commands(hs.load_set(forge / "tools" / "lane" / "lane_hooks.json", forge_root=forge)):
            self.assertIn(f"{OP}/tools/hooks/", command)
        self.link("aaa", forge)
        self.assertEqual(hs.spelling(forge), "${HOME}/aaa")

    def test_the_roots_own_name_is_preferred_among_links(self):
        forge = self.tmp / "x" / "harmonic-forge"
        forge.mkdir(parents=True)
        self.link("aaa", forge)
        self.link("harmonic-forge", forge)
        self.assertEqual(hs.spelling(forge), OP)

    def test_no_link_renders_the_absolute_root(self):
        forge = self.tmp / "forge-x"
        forge.mkdir()
        (self.home / "forge-x").mkdir()  # same name, not the same directory
        self.assertEqual(hs.spelling(forge), str(forge))

    def test_an_unlinked_root_outside_the_allowlist_refuses(self):
        for name in ("a b", "a$b", 'a"b', "a;b"):
            with self.subTest(name=name):
                forge = self.tmp / name
                forge.mkdir()
                with self.assertRaisesRegex(hs.SetError, "cannot be written into a hook command"):
                    hs.spelling(forge)
                self.link(f"ok{len(name)}{ord(name[1])}", forge)
                self.assertTrue(hs.spelling(forge).startswith("${HOME}/ok"))

    def test_render_replaces_only_the_placeholder_and_copies(self):
        stored = json.loads(SET_FILE.read_text(encoding="utf-8"))
        rendered = hs.render(stored, "/r")
        self.assertTrue(all(c.count("/r/tools/hooks/") == c.count("tools/hooks/") for c in _commands(rendered)))
        self.assertTrue(all(hs.PLACEHOLDER in c for c in _commands(stored)))
        self.assertEqual(json.dumps(rendered).replace("/r/tools", "@FORGE_ROOT@/tools"), json.dumps(stored))


class OperatorLayout(_Tmp):
    """AC4 / Test Case 4: `$HOME/harmonic-forge` links the checkout, so every
    command is the string forge's own settings already carry."""

    def test_the_operator_layout_doubles_no_project_entry(self):
        self.link("harmonic-forge", ROOT)
        settings = json.loads((ROOT / ".claude" / "settings.json").read_text(encoding="utf-8"))
        for data in (hs.load_set(SET_FILE, forge_root=ROOT),
                     hs.load_effective(SET_FILE, "operator", forge_root=ROOT)):
            self.assertEqual(hs.doubled(data, {"forge": settings})[0], [])
            self.assertTrue(all(f"{OP}/tools/hooks/" in c for c in _commands(data)))


class BuildRefuses(_Tmp):
    """AC2 at the builder: the root and its source are named."""

    def test_a_root_without_tools_hooks_refuses_naming_root_and_source(self):
        (self.tmp / "bare").mkdir()
        with self.assertRaisesRegex(hs.SetError, rf"forge root {self.tmp}/bare \(from src-x\) has no tools/hooks/"):
            hs.build(SET_FILE, self.tmp / "bare", "src-x", overlay="none")


class RootCommand(_Tmp):
    """AC2 / Test Case 2: `lane_hookset.py root`, as the launcher runs it."""

    def run_root(self, config: str | None, **env) -> subprocess.CompletedProcess:
        xdg = self.tmp / "xdg"
        if config is not None:
            (xdg / "harmonic-forge").mkdir(parents=True, exist_ok=True)
            (xdg / "harmonic-forge" / "instance.toml").write_text(config, encoding="utf-8")
        base = {k: v for k, v in os.environ.items() if k != "HARMONIC_FORGE_ROOT"}
        return subprocess.run(
            [sys.executable, "-I", "-E", str(HERE / "lane_hookset.py"), "root", "--launcher-checkout", str(ROOT)],
            capture_output=True, text=True, timeout=60,
            env={**base, "HOME": str(self.home), "XDG_CONFIG_HOME": str(xdg), **env})

    def test_a_forge_root_without_tools_hooks_refuses_naming_value_and_source(self):
        (self.tmp / "no-hooks").mkdir()
        proc = self.run_root(f'forge_root = "{self.tmp}/no-hooks"\n')
        self.assertEqual((proc.returncode, proc.stdout), (1, ""), proc.stderr)
        self.assertIn(f"{self.tmp}/no-hooks", proc.stderr)
        self.assertIn("instance.toml forge_root", proc.stderr)
        self.assertIn("has no tools/hooks/", proc.stderr)

    def test_a_relative_value_refuses_naming_it(self):
        for config, env, value in (('forge_root = "rel/x"\n', {}, "'rel/x'"),
                                   (None, {"HARMONIC_FORGE_ROOT": "rel/y"}, "'rel/y'")):
            with self.subTest(value=value):
                proc = self.run_root(config, **env)
                self.assertEqual(proc.returncode, 1)
                self.assertIn(value, proc.stderr)

    def test_each_source_prints_root_and_label(self):
        forge = self.copy(self.tmp / "declared")
        self.assertEqual(self.run_root(None).stdout, f"{ROOT}\tlauncher checkout\n")
        proc = self.run_root(f'forge_root = "{forge}"\n')
        self.assertEqual(proc.stdout, f"{forge}\t{self.tmp}/xdg/harmonic-forge/instance.toml forge_root\n")
        proc = self.run_root(None, HARMONIC_FORGE_ROOT=str(forge))
        self.assertEqual(proc.stdout, f"{forge}\tHARMONIC_FORGE_ROOT\n")


class HookTargetsBasename(_Tmp):
    """Test Case 7: a consumer entry is inspected whatever the checkout is named."""

    def test_a_missing_script_under_any_checkout_name_is_reported(self):
        forge = self.copy(self.tmp / "forge-x")
        gone = f'python3 "{forge}/tools/hooks/gone.py"'
        here = f'python3 "{forge}/tools/hooks/batch_gate.py"'
        hooks = {"Stop": [{"hooks": [{"type": "command", "command": c} for c in (gone, here)]}]}
        self.assertEqual(unresolvable_hook_targets(hooks), [f"{forge}/tools/hooks/gone.py"])


if __name__ == "__main__":
    unittest.main()
