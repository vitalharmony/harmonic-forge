#!/usr/bin/env python3
"""Tests for the forge root accessors in `instance.py` (harmonic-forge#1526):
`forge_root()`, `projects_root()` and the one precedence
`resolve_forge_root()` the lane launcher reads.

Hermetic: every call names its own `environ`, whose `XDG_CONFIG_HOME` is a
temporary directory, so the operator's `instance.toml` is never read.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import instance  # noqa: E402


class _Config(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()
        self.env = {"XDG_CONFIG_HOME": str(self.tmp / "config")}
        self.launcher = self.forge("launcher")

    def forge(self, name: str) -> Path:
        """A directory that passes the `tools/hooks/` guard."""
        (self.tmp / name / "tools" / "hooks").mkdir(parents=True, exist_ok=True)
        return self.tmp / name

    def declare(self, text: str) -> Path:
        path = instance.config_path(self.env)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path


class Accessors(_Config):

    def test_a_missing_file_or_key_is_none(self):
        self.assertIsNone(instance.forge_root(self.env))
        self.assertIsNone(instance.projects_root(self.env))
        self.declare('lane_hook_overlay = "operator"\n')
        self.assertIsNone(instance.forge_root(self.env))
        self.assertIsNone(instance.projects_root(self.env))

    def test_an_absolute_or_home_relative_value_is_read(self):
        self.declare(f'forge_root = "{self.tmp}/f"\nprojects_root = "~/projects"\n')
        self.assertEqual(instance.forge_root(self.env), self.tmp / "f")
        self.assertEqual(instance.projects_root(self.env), Path.home() / "projects")

    def test_any_other_value_refuses_naming_it(self):
        for key in ("forge_root", "projects_root"):
            for value in ('"relative/x"', '""', "3", "true", '"~user/x"'):
                with self.subTest(key=key, value=value):
                    path = self.declare(f"{key} = {value}\n")
                    with self.assertRaises(ValueError) as raised:
                        getattr(instance, key)(self.env)
                    self.assertIn(str(path), str(raised.exception))
                    self.assertIn(f"`{key}` must be an absolute path", str(raised.exception))

    def test_the_timezone_is_unchanged_beside_the_roots(self):
        self.declare(f'timezone = "Europe/Paris"\nforge_root = "{self.tmp}"\n')
        with mock.patch.dict("os.environ", self.env):
            zone, defaulted = instance.timezone()
        self.assertEqual((str(zone), defaulted), ("Europe/Paris", False))


class Precedence(_Config):

    def resolve(self, **env):
        return instance.resolve_forge_root(self.launcher, {**self.env, **env})

    def test_the_launcher_checkout_is_the_fallback(self):
        self.assertEqual(self.resolve(), (self.launcher, "launcher checkout"))
        self.assertEqual(self.resolve(HARMONIC_FORGE_ROOT=""), (self.launcher, "launcher checkout"))
        self.assertEqual(instance.LAUNCHER_CHECKOUT, "launcher checkout")

    def test_instance_toml_outranks_the_launcher_checkout(self):
        declared = self.forge("declared")
        path = self.declare(f'forge_root = "{declared}"\n')
        self.assertEqual(self.resolve(), (declared, f"{path} forge_root"))

    def test_the_environment_outranks_instance_toml(self):
        self.declare(f'forge_root = "{self.forge("declared")}"\n')
        env_root = self.forge("from-env")
        self.assertEqual(self.resolve(HARMONIC_FORGE_ROOT=str(env_root)),
                         (env_root, "HARMONIC_FORGE_ROOT"))

    def test_the_root_is_resolved(self):
        (self.tmp / "link").symlink_to(self.launcher)
        self.assertEqual(self.resolve(HARMONIC_FORGE_ROOT=str(self.tmp / "link"))[0], self.launcher)

    def test_a_root_without_tools_hooks_refuses_naming_root_and_source(self):
        (self.tmp / "bare").mkdir()
        path = self.declare(f'forge_root = "{self.tmp}/bare"\n')
        for env, source in (({}, f"{path} forge_root"),
                            ({"HARMONIC_FORGE_ROOT": str(self.tmp / "bare")}, "HARMONIC_FORGE_ROOT")):
            with self.subTest(source=source), self.assertRaises(ValueError) as raised:
                self.resolve(**env)
            self.assertIn(f"forge root {str(self.tmp / 'bare')!r} (from {source}; resolves to "
                          f"{str(self.tmp / 'bare')!r}) has no tools/hooks/", str(raised.exception))

    def test_the_refusal_names_the_value_as_written(self):
        """Round 1 survivor 6: the value the operator entered, not only its
        normalized form, with its source."""
        (self.tmp / "bare").mkdir()
        (self.tmp / "blink").symlink_to(self.tmp / "bare")
        written = f"{self.tmp}/x/../blink"
        path = self.declare(f'forge_root = "{written}"\n')
        for env, source in (({}, f"{path} forge_root"),
                            ({"HARMONIC_FORGE_ROOT": written}, "HARMONIC_FORGE_ROOT")):
            with self.subTest(source=source), self.assertRaises(ValueError) as raised:
                self.resolve(**env)
            self.assertIn(f"forge root {written!r} (from {source}; resolves to "
                          f"{str(self.tmp / 'bare')!r})", str(raised.exception))

    def test_a_relative_environment_root_refuses_naming_it(self):
        with self.assertRaisesRegex(ValueError, "HARMONIC_FORGE_ROOT must be an absolute path, got 'rel/x'"):
            self.resolve(HARMONIC_FORGE_ROOT="rel/x")

    def test_a_root_holding_a_tab_or_newline_refuses(self):
        for name in ("a\tb", "a\nb"):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "tab or newline"):
                self.resolve(HARMONIC_FORGE_ROOT=str(self.forge(name)))


if __name__ == "__main__":
    unittest.main()
