#!/usr/bin/env python3
"""Every built hook target resolves under the forge root (harmonic-forge#1526
round 1 survivors 1, 4 and 5).

One rule: a stored command fullmatches one of two fixed templates (sticky-wicket
A1), never a regex search of shell text, expands `${HOME}` from `HOME` as the shell does (an unset
or empty `HOME` refuses), and refuses unless that path's realpath is a file
under the realpath of the root.

Hermetic: HOME is a temporary directory and each root is a copy of this
checkout's `tools/hooks`.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(HERE))
import lane_hookset as hs  # noqa: E402

SCRIPT = "block_blind_repeat.py"


def _set(command: str) -> dict:
    return {"schema": 1, "disableAllHooks": False, "hooks": {"PreToolUse": [
        {"matcher": "Bash", "hooks": [{"type": "command", "command": command,
                                       "onFailure": "block"}]}]}}  # harmonic-forge#1608 P5


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

    def forge(self, name: str) -> Path:
        shutil.copytree(ROOT / "tools" / "hooks", self.tmp / name / "tools" / "hooks",
                        ignore=shutil.ignore_patterns("__pycache__"))
        return self.tmp / name

    def build(self, root: Path, command: str) -> dict:
        path = self.tmp / "set.json"
        path.write_text(json.dumps(_set(command)), encoding="utf-8")
        return hs.build(path, root, "src-x", overlay="none")


class WholeQuotedPath(_Tmp):
    """Survivor 1: a path that only starts with a script name is refused."""

    def test_a_suffix_after_py_refuses_though_both_files_exist(self):
        root = self.forge("forge-x")
        for suffix in ("_disabled", "c", ".bak", "~"):
            (root / "tools" / "hooks" / f"{SCRIPT}{suffix}").write_text("")
            with self.subTest(suffix=suffix), self.assertRaisesRegex(hs.SetError, "stored command templates"):
                self.build(root, f'python3 "@FORGE_ROOT@/tools/hooks/{SCRIPT}{suffix}"')

    def test_an_unquoted_placeholder_refuses(self):
        root = self.forge("forge-x")
        for command in (f"python3 @FORGE_ROOT@/tools/hooks/{SCRIPT}",
                        f'python3 "@FORGE_ROOT@/tools/hooks/{SCRIPT}" @FORGE_ROOT@/x'):
            with self.subTest(command=command), self.assertRaises(hs.SetError):
                self.build(root, command)

    def test_only_the_two_templates_are_accepted(self):
        """Sticky-wicket A1: every stored command fullmatches one of the two
        fixed templates. Any other text naming the placeholder refuses."""
        root = self.forge("forge-x")
        (root / "tools" / "hooks" / f"{SCRIPT}.bak").write_text("")
        path = f"@FORGE_ROOT@/tools/hooks/{SCRIPT}"
        guarded = f'f="{path}"; [ -f "$f" ] && python3 "$f" || true'
        for command in (f'python3 "{path}"_disabled', f'python3 "{path}"x', f'python3 "{path}.bak"',
                        f"python3 {path}", f'python3 "{path}"; rm -rf x', f'{guarded}; rm -rf x',
                        f' python3 "{path}"', f'python3  "{path}"', f'f="{path}"x; [ -f "$f" ] && python3 "$f" || true'):
            with self.subTest(command=command), self.assertRaisesRegex(hs.SetError, "stored command templates"):
                self.build(root, command)
        for command in (f'python3 "{path}"', guarded):
            with self.subTest(command=command):
                self.assertTrue(self.build(root, command)["hooks"])

    def test_the_whole_path_builds(self):
        root = self.forge("forge-x")
        value = self.build(root, f'f="@FORGE_ROOT@/tools/hooks/{SCRIPT}"; [ -f "$f" ] && python3 "$f" || true')
        command = value["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        self.assertIn(f'"{root}/tools/hooks/{SCRIPT}"', command)


class ResolvesUnderTheRoot(_Tmp):
    """Survivor 4: a target whose realpath leaves the root is refused."""

    def test_a_symlinked_hooks_directory_refuses(self):
        other = self.forge("other-checkout")
        root = self.tmp / "forge-x"
        (root / "tools").mkdir(parents=True)
        (root / "tools" / "hooks").symlink_to(other / "tools" / "hooks")
        with self.assertRaisesRegex(hs.SetError, "do not resolve to a file under forge root") as raised:
            self.build(root, f'python3 "@FORGE_ROOT@/tools/hooks/{SCRIPT}"')
        self.assertIn(str(other / "tools" / "hooks" / SCRIPT), str(raised.exception))

    def test_a_symlinked_script_refuses(self):
        other = self.forge("other-checkout")
        root = self.forge("forge-x")
        target = root / "tools" / "hooks" / SCRIPT
        target.unlink()
        target.symlink_to(other / "tools" / "hooks" / SCRIPT)
        with self.assertRaisesRegex(hs.SetError, "do not resolve to a file under forge root"):
            self.build(root, f'python3 "@FORGE_ROOT@/tools/hooks/{SCRIPT}"')

    def test_a_home_link_to_the_root_is_under_it(self):
        root = self.forge("forge-x")
        (self.home / "harmonic-forge").symlink_to(root)
        value = self.build(root, f'python3 "@FORGE_ROOT@/tools/hooks/{SCRIPT}"')
        self.assertEqual(value["hooks"]["PreToolUse"][0]["hooks"][0]["command"],
                         f'python3 "${{HOME}}/harmonic-forge/tools/hooks/{SCRIPT}"')

    def test_the_committed_sets_build_under_a_copied_root(self):
        root = self.forge("forge-x")
        (root / "tools" / "lane").mkdir()
        for name in ("lane_hooks.json", "lane_hooks.operator.json"):
            shutil.copy(HERE / name, root / "tools" / "lane" / name)
        data = hs.load_effective(root / "tools" / "lane" / "lane_hooks.json", "operator", forge_root=root)
        self.assertTrue(list(hs.handlers(data["hooks"])))


class HomeAsTheShellSeesIt(_Tmp):
    """Survivor 5: Python never finds a HOME the shell would not expand."""

    def test_an_unset_or_empty_home_refuses(self):
        root = self.forge("forge-x")
        (self.home / "harmonic-forge").symlink_to(root)
        for home in (None, ""):
            env = {k: v for k, v in os.environ.items() if k != "HOME"}
            if home is not None:
                env["HOME"] = home
            with self.subTest(home=home), mock.patch.dict(os.environ, env, clear=True), \
                    self.assertRaisesRegex(hs.SetError, "HOME is unset or empty"):
                self.build(root, f'python3 "@FORGE_ROOT@/tools/hooks/{SCRIPT}"')


if __name__ == "__main__":
    unittest.main()
