#!/usr/bin/env python3
"""Instance settings and the zone-literal check (harmonic-forge#1505)."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import check_zone_literals as C  # noqa: E402
import instance as I  # noqa: E402

REPO = HERE.parents[1]


class Accessor(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": self.tmp.name, "HOME": self.tmp.name})
        self.env.start()
        self.addCleanup(self.env.stop)

    def _write(self, text: str) -> None:
        path = Path(self.tmp.name) / "harmonic-forge" / "instance.toml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def test_a_missing_file_returns_the_default_reported_as_defaulted(self):
        self.assertEqual(I.timezone(), (ZoneInfo("America/Los_Angeles"), True))

    def test_a_missing_key_returns_the_default_reported_as_defaulted(self):
        self._write('other = "x"\n')
        self.assertEqual(I.timezone(), (ZoneInfo("America/Los_Angeles"), True))

    def test_a_valid_zone_is_returned(self):
        self._write('timezone = "America/New_York"\n')
        self.assertEqual(I.timezone(), (ZoneInfo("America/New_York"), False))

    def test_an_invalid_zone_raises_naming_the_value(self):
        self._write('timezone = "Mars/Olympus"\n')
        with self.assertRaises(ValueError) as raised:
            I.timezone()
        self.assertIn("Mars/Olympus", str(raised.exception))

    def test_an_empty_xdg_config_home_falls_back_to_dot_config(self):
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": "", "HOME": self.tmp.name}):
            self.assertEqual(I.config_path(), Path(self.tmp.name) / ".config" / "harmonic-forge" / "instance.toml")

    def test_the_settings_file_is_never_inside_the_repo_and_is_never_written(self):
        self.assertFalse(I.config_path().resolve().is_relative_to(REPO.resolve()))
        before = sorted(p for p in Path(self.tmp.name).rglob("*"))
        I.timezone()
        self.assertEqual(sorted(p for p in Path(self.tmp.name).rglob("*")), before)


class PathSafety(unittest.TestCase):
    def test_a_directory_at_the_settings_path_is_an_error_not_a_default(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": tmp}):
            (Path(tmp) / "harmonic-forge" / "instance.toml").mkdir(parents=True)
            with self.assertRaises(ValueError):
                I.timezone()

    def test_a_settings_path_inside_the_repo_is_refused(self):
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(REPO / "tools")}):
            with self.assertRaises(ValueError):
                I.config_path()

    def test_a_settings_path_inside_another_checkout_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / ".git").write_text("gitdir: elsewhere\n", encoding="utf-8")
            with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(Path(tmp) / "cfg")}):
                with self.assertRaises(ValueError):
                    I.config_path()

    def test_a_dangling_git_symlink_still_marks_a_checkout(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / ".git").symlink_to(Path(tmp) / "gone")
            with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(Path(tmp) / "cfg")}):
                with self.assertRaises(ValueError):
                    I.config_path()

    def test_a_dangling_symlink_is_an_error_not_a_default(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": tmp}):
            path = Path(tmp) / "harmonic-forge" / "instance.toml"
            path.parent.mkdir(parents=True)
            path.symlink_to(Path(tmp) / "gone.toml")
            with self.assertRaises(ValueError):
                I.timezone()

    def test_a_file_where_the_config_directory_should_be_is_an_error(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": tmp}):
            (Path(tmp) / "harmonic-forge").write_text("not a directory\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                I.timezone()

    def test_a_relative_xdg_config_home_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": "tools", "HOME": tmp}):
            self.assertEqual(I.config_path(), Path(tmp) / ".config" / "harmonic-forge" / "instance.toml")

    def test_reading_an_existing_file_leaves_it_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": tmp}):
            path = Path(tmp) / "harmonic-forge" / "instance.toml"
            path.parent.mkdir(parents=True)
            path.write_text('timezone = "Europe/Berlin"\n', encoding="utf-8")
            before = path.read_bytes()
            I.timezone()
            self.assertEqual(path.read_bytes(), before)


class ZoneLiteralCheck(unittest.TestCase):
    def _tree(self, files: dict[str, str]) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        for rel, text in files.items():
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_text(text, encoding="utf-8")
        return root

    def test_a_planted_literal_fails_in_every_spelling(self):
        for source in ('from zoneinfo import ZoneInfo\nZoneInfo("Europe/Paris")\n',
                       'from zoneinfo import ZoneInfo\nZoneInfo(key="Europe/Paris")\n',
                       'import zoneinfo\nzoneinfo.ZoneInfo("Europe/Paris")\n',
                       'from zoneinfo import ZoneInfo as Z\nZ("Europe/Paris")\n',
                       'import zoneinfo as zi\nzi.ZoneInfo("Europe/Paris")\n',
                       'from zoneinfo import ZoneInfo\nZoneInfo.no_cache("Europe/Paris")\n',
                       'from zoneinfo import ZoneInfo\nZoneInfo("Europe/" + "Paris")\n',
                       'from zoneinfo import ZoneInfo\nc = "Paris"\nZoneInfo(f"Europe/{c}")\n',
                       'from zoneinfo import ZoneInfo\nZoneInfo(*[], "Europe/Paris")\n',
                       'from zoneinfo import ZoneInfo\nZoneInfo(*["Europe/Paris"])\n'):
            with self.subTest(source=source):
                files, hits = C.scan(self._tree({"tools/x/y.py": source}))
                self.assertEqual(files, 1)
                self.assertEqual(len(hits), 1)
                self.assertTrue(hits[0].startswith("tools/x/y.py:"))

    def test_a_test_file_is_not_exempt_unless_named(self):
        source = 'from zoneinfo import ZoneInfo\nZoneInfo("Europe/Paris")\n'
        # A clean sibling keeps the parsed-file count above zero, so only the hit can fail this.
        tree = self._tree({"tools/x/test_b.py": source, "tools/x/clean.py": "x = 1\n"})
        self.assertEqual(C.scan(tree)[1], ["tools/x/test_b.py:2"])

    def test_a_scan_of_zero_files_fails(self):
        self.assertEqual(C.main(["--root", str(self._tree({"README.md": "x\n"}))]), 1)

    def test_a_tree_of_only_exempt_files_parses_zero_and_fails(self):
        self.assertEqual(C.main(["--root", str(self._tree({"tools/settings/instance.py": "x = 1\n"}))]), 1)

    def test_a_none_fallback_is_not_a_literal_zone(self):
        source = "from zoneinfo import ZoneInfo\ndef f(z):\n    return ZoneInfo(z or None)\n"
        self.assertEqual(C.main(["--root", str(self._tree({"tools/x/y.py": source}))]), 0)

    def test_documented_blind_spot_a_zone_held_in_a_constant_is_not_caught(self):
        # Pinned on purpose: the docstring names this as out of scope (a tripwire, not dataflow).
        source = "from zoneinfo import ZoneInfo\nTZ = 'Europe/Paris'\nZoneInfo(TZ)\n"
        self.assertEqual(C.main(["--root", str(self._tree({"tools/x/y.py": source}))]), 0)

    def test_the_docstring_states_the_rule_the_scan_enforces(self):
        # Post-verdict survivor: the stated rule must not drift from the code.
        doc = " ".join(C.__doc__.split())
        self.assertIn("anywhere in ANY of its arguments, starred and keyword ones included", doc)

    def test_the_real_tree_has_no_hardcoded_zone(self):
        self.assertEqual(C.main(["--root", str(REPO)]), 0)


if __name__ == "__main__":
    unittest.main()
