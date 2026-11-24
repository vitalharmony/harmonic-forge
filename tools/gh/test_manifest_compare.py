#!/usr/bin/env python3
"""harmonic-forge#1539: a package.json compares by what npm installs from it;
every other manifest compares bytes."""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _manifest_compare import manifests_equal  # noqa: E402

BASE = {"name": "app", "version": "3.2.0", "dependencies": {"react": "^18.3.1"},
        "devDependencies": {"vite": "^5.4.0"}, "packageManager": "npm@10.8.0",
        "scripts": {"build": "vite build", "postinstall": "node setup.js"}}


def pkg(**changes) -> bytes:
    data = json.loads(json.dumps(BASE))
    for key, value in changes.items():
        if key == "scripts":
            data["scripts"].update(value)
        else:
            data[key] = value
    return json.dumps(data, indent=2).encode()


class ManifestsEqual(unittest.TestCase):
    def test_identical_bytes_equal(self) -> None:
        self.assertTrue(manifests_equal("frontend/package.json", b"not json\n", b"not json\n"))
        self.assertTrue(manifests_equal("frontend/package-lock.json", b"x", b"x"))

    def test_version_only_equal(self) -> None:
        self.assertTrue(manifests_equal("frontend/package.json", pkg(), pkg(version="3.2.1")))

    def test_non_install_script_equal(self) -> None:
        self.assertTrue(manifests_equal("package.json", pkg(), pkg(scripts={"build": "x"})))

    def test_dependency_key_differs(self) -> None:
        self.assertFalse(manifests_equal("frontend/package.json", pkg(),
                                         pkg(dependencies={"react": "^19.0.0"})))

    def test_package_manager_differs(self) -> None:
        self.assertFalse(manifests_equal("frontend/package.json", pkg(),
                                         pkg(packageManager="npm@11.0.0")))

    def test_os_differs(self) -> None:
        self.assertFalse(manifests_equal("frontend/package.json", pkg(), pkg(os=["linux"])))

    def test_lifecycle_script_differs(self) -> None:
        self.assertFalse(manifests_equal("frontend/package.json", pkg(),
                                         pkg(scripts={"postinstall": "node other.js"})))

    def test_lockfile_byte_differs(self) -> None:
        self.assertFalse(manifests_equal("frontend/package-lock.json", b'{"a": 1}', b'{"a":1}'))

    def test_requirements_byte_differs(self) -> None:
        self.assertFalse(manifests_equal("backend/requirements.txt", b"a==1\n", b"a==1 \n"))

    def test_invalid_json_differs(self) -> None:
        self.assertFalse(manifests_equal("frontend/package.json", pkg(), b"{broken"))
        self.assertFalse(manifests_equal("frontend/package.json", b"[]", b"[ ]"))
        self.assertFalse(manifests_equal("frontend/package.json", b"{broken", b"{broken2"))

    def test_missing_side_differs(self) -> None:
        self.assertFalse(manifests_equal("frontend/package.json", pkg(), None))
        self.assertTrue(manifests_equal("frontend/package.json", None, None))


if __name__ == "__main__":
    unittest.main()
