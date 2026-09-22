"""Tests for gate_plan.py (harmonic-forge#708 AC3)."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import gate_plan

GOOD = {
    "policy_doc": "docs/GATE.md",
    "commands": [
        {"name": "lint", "cwd": "frontend", "command": "npm run lint"},
        {"name": "mypy", "cwd": "backend", "command": ".venv/bin/mypy app",
         "note": "cwd must be backend/"},
        {"name": "selinux", "cwd": ".", "command": "python3 x.py",
         "when_changed": ["podman-compose.yml"]},
    ],
}


class GatePlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        (self.root / ".claude").mkdir()
        (self.root / "backend").mkdir()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _write(self, config: object) -> None:
        (self.root / gate_plan.CONFIG).write_text(json.dumps(config))

    def test_cwd_resolves_against_the_checkout_not_a_fixed_path(self) -> None:
        self._write(GOOD)
        out = gate_plan.render(self.root, gate_plan.load(self.root))
        self.assertIn(f"working_dir: {self.root / 'backend'}", out)
        self.assertIn(f"working_dir: {self.root}\n", out)
        self.assertIn("  # cwd must be backend/", out)
        self.assertIn("only if changed: podman-compose.yml", out)
        self.assertIn(f"Read first, in full: {self.root / 'docs/GATE.md'}", out)

    def test_root_found_from_a_subdirectory(self) -> None:
        self._write(GOOD)
        self.assertEqual(gate_plan.find_root(self.root / "backend"), self.root)

    def test_missing_config_refuses(self) -> None:
        with self.assertRaises(gate_plan.ConfigError):
            gate_plan.find_root(self.root / "backend")

    def test_invalid_configs_refuse(self) -> None:
        bad = [
            {},
            {"commands": []},
            {"commands": [{"name": "x", "cwd": "/abs", "command": "y"}]},
            {"commands": [{"name": "x", "cwd": ".", "command": ""}]},
            {"commands": [{"name": "x", "cwd": ".", "command": "y", "extra": 1}]},
            {"commands": [{"name": "x", "cwd": ".", "command": "y"}], "surprise": 1},
        ]
        for config in bad:
            with self.subTest(config=config):
                self._write(config)
                with self.assertRaises(gate_plan.ConfigError):
                    gate_plan.load(self.root)


if __name__ == "__main__":
    unittest.main()
