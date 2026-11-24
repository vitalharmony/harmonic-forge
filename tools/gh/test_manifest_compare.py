#!/usr/bin/env python3
"""harmonic-forge#1539 (reforged, issuecomment-7121562087): a package.json is
equal only when it differs in top-level `version` alone and no install step
runs; every other manifest compares bytes."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _manifest_compare import has_gyp, manifests_equal  # noqa: E402

BASE = {"name": "app", "version": "3.2.0", "dependencies": {"react": "^18.3.1"},
        "devDependencies": {"vite": "^5.4.0"}, "packageManager": "npm@10.8.0",
        "overrides": {"demo": "1.0.0", "demo@^1": "1.1.0"},
        "scripts": {"build": "vite build", "dev": "vite"}}
PKG = "frontend/package.json"


def pkg(**changes) -> bytes:
    data = json.loads(json.dumps(BASE))
    for key, value in changes.items():
        if key == "scripts":
            data["scripts"].update(value)
        else:
            data[key] = value
    return json.dumps(data, indent=2).encode()


def eq(ours: bytes | None, theirs: bytes | None, name: str = PKG) -> bool:
    return manifests_equal(name, ours, theirs, gyp=False)


class VersionOnly(unittest.TestCase):
    def test_identical_bytes_equal(self) -> None:
        self.assertTrue(eq(b"not json\n", b"not json\n"))
        self.assertTrue(eq(b"x", b"x", "frontend/package-lock.json"))

    def test_version_only_equal(self) -> None:
        self.assertTrue(eq(pkg(), pkg(version="3.2.1")))

    def test_bump_version_rewrite_equal(self) -> None:
        # HRSE2 scripts/bump_version.py: json.load, set version,
        # json.dump(indent=2), then a trailing newline.
        before = json.dumps(BASE, indent=2).encode() + b"\n"
        data = json.loads(before)
        data["version"] = "3.2.1"
        after = (json.dumps(data, indent=2) + "\n").encode()
        self.assertNotEqual(before, after)
        self.assertTrue(eq(before, after))

    def test_whitespace_only_equal(self) -> None:
        self.assertTrue(eq(pkg(), json.dumps(BASE).encode()))


class AnythingElseDiffers(unittest.TestCase):
    def test_dependency_key_differs(self) -> None:
        self.assertFalse(eq(pkg(), pkg(dependencies={"react": "^19.0.0"})))

    def test_unknown_added_key_differs(self) -> None:
        for key, value in (("devEngines", {"runtime": {"name": "node"}}),
                           ("libc", ["glibc"]), ("gypfile", False),
                           ("peerDependenciesMeta", {"a": {"optional": True}}),
                           ("acceptDependencies", {"a": "^2"}), ("someFutureKey", 1)):
            with self.subTest(key=key):
                self.assertFalse(eq(pkg(), pkg(**{key: value})))

    def test_non_install_script_change_differs(self) -> None:
        self.assertFalse(eq(pkg(), pkg(scripts={"build": "x"})))

    def test_reordered_overrides_differ(self) -> None:
        reordered = {"demo@^1": "1.1.0", "demo": "1.0.0"}
        self.assertFalse(eq(pkg(), pkg(overrides=reordered)))

    def test_duplicate_key_differs(self) -> None:
        once = b'{"version": "1", "dependencies": {"a": "1"}}'
        twice = b'{"version": "2", "dependencies": {"a": "1"}, "dependencies": {"a": "1"}}'
        self.assertFalse(eq(once, twice))

    def test_nested_version_is_not_ignored(self) -> None:
        self.assertFalse(eq(pkg(), pkg(dependencies={"react": "^18.3.1", "version": "1"})))


class InstallStepsFailClosed(unittest.TestCase):
    def test_version_bump_with_any_install_script_differs(self) -> None:
        for script in ("preinstall", "install", "postinstall", "prepare", "preprepare",
                       "postprepare", "prepublish", "dependencies"):
            with self.subTest(script=script):
                ours = pkg(scripts={script: "node gen.js"})
                theirs = pkg(version="3.2.1", scripts={script: "node gen.js"})
                self.assertFalse(eq(ours, theirs))

    def test_non_object_scripts_with_bump_differs(self) -> None:
        self.assertFalse(eq(json.dumps({**BASE, "scripts": "x"}).encode(),
                            json.dumps({**BASE, "scripts": "x", "version": "9"}).encode()))

    def test_binding_gyp_differs(self) -> None:
        self.assertFalse(manifests_equal(PKG, pkg(), pkg(version="3.2.1"), gyp=True))

    def test_gyp_defaults_closed(self) -> None:
        self.assertFalse(manifests_equal(PKG, pkg(), pkg(version="3.2.1")))

    def test_has_gyp_reads_either_root(self) -> None:
        with tempfile.TemporaryDirectory() as one, tempfile.TemporaryDirectory() as two:
            self.assertFalse(has_gyp(PKG, Path(one), Path(two)))
            (Path(two) / "frontend").mkdir()
            (Path(two) / "frontend" / "binding.gyp").write_text("{}")
            self.assertTrue(has_gyp(PKG, Path(one), Path(two)))


class BytesAndParsing(unittest.TestCase):
    def test_lockfile_byte_differs(self) -> None:
        self.assertFalse(eq(b'{"a": 1}', b'{"a":1}', "frontend/package-lock.json"))

    def test_requirements_byte_differs(self) -> None:
        self.assertFalse(eq(b"a==1\n", b"a==1 \n", "backend/requirements.txt"))

    def test_invalid_json_differs(self) -> None:
        self.assertFalse(eq(pkg(), b"{broken"))
        self.assertFalse(eq(b"{broken", b"{broken2"))

    def test_json_type_is_exact(self) -> None:
        for ours, theirs in (("true", "1"), ("false", "0"), ("1", "1.0"),
                             ("{}", "[]"), ('{"a": 1}', '[["a", 1]]')):
            with self.subTest(ours=ours, theirs=theirs):
                a = f'{{"version": "1", "bundleDependencies": {ours}}}'.encode()
                b = f'{{"version": "2", "bundleDependencies": {theirs}}}'.encode()
                self.assertFalse(eq(a, b))

    def test_non_strict_json_differs(self) -> None:
        self.assertFalse(eq(b'{"version": "1", "x": NaN}', b'{"version": "2", "x": NaN}'))
        self.assertFalse(eq(b'{"version": "1", "x": Infinity}', b'{"version": "2", "x": Infinity}'))
        utf16 = '{"version": "1"}'.encode("utf-16")
        self.assertFalse(eq(utf16, '{"version": "2"}'.encode("utf-16")))

    def test_non_object_differs(self) -> None:
        self.assertFalse(eq(b"[]", b"[ ]"))
        self.assertFalse(eq(b'"a"', b' "a"'))

    def test_missing_side_differs(self) -> None:
        self.assertFalse(eq(pkg(), None))
        self.assertTrue(eq(None, None))


if __name__ == "__main__":
    unittest.main()
