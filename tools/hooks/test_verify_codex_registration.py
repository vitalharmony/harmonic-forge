#!/usr/bin/env python3
"""Tests for verify_codex_registration.py (harmonic-forge#778 AC2, AC6).
Run: python3 tools/hooks/test_verify_codex_registration.py"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))
import verify_codex_registration as vcr


def _write(tmp: Path, hooks: list[str]) -> Path:
    for hook in hooks:
        (tmp / hook).write_text("# stub\n", encoding="utf-8")
    path = tmp / "hooks.json"
    config = {
        "hooks": {
            "PreToolUse": [
                {"matcher": "^Bash$", "hooks": [
                    {"type": "command",
                     "command": f'python3 "{tmp / h}"'}
                    for h in hooks
                ]}
            ]
        }
    }
    path.write_text(json.dumps(config), encoding="utf-8")
    return path


class VerifyTests(unittest.TestCase):
    def test_all_three_registered_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(Path(tmp), [
                "block_missing_preclose_inspection.py", "batch_gate.py",
                "block_closing_keywords.py", "model_tier_gate.py",
            ])
            ok, missing = vcr.verify(path)
        self.assertTrue(ok)
        self.assertEqual(missing, [])

    def test_missing_one_fails_and_names_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(Path(tmp), [
                "block_missing_preclose_inspection.py", "batch_gate.py",
            ])
            ok, missing = vcr.verify(path)
        self.assertFalse(ok)
        self.assertEqual(missing, ["block_closing_keywords.py"])

    def test_missing_all_three_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(Path(tmp), ["model_tier_gate.py"])
            ok, missing = vcr.verify(path)
        self.assertFalse(ok)
        self.assertEqual(set(missing), set(vcr.REQUIRED_HOOKS))

    def test_missing_file_fails_loud_not_silently(self):
        """A missing/unreadable hooks.json must read as every hook missing,
        never as 'nothing to check' -- the same fail-direction as the
        settings.json read in batch_auth.verify_registration."""
        ok, missing = vcr.verify(Path("/nonexistent/hooks.json"))
        self.assertFalse(ok)
        self.assertEqual(set(missing), set(vcr.REQUIRED_HOOKS))

    def test_unparseable_json_fails_loud(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "hooks.json"
            path.write_text("not json", encoding="utf-8")
            ok, missing = vcr.verify(path)
        self.assertFalse(ok)
        self.assertEqual(set(missing), set(vcr.REQUIRED_HOOKS))

    def test_hook_in_a_non_bash_matcher_does_not_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "hooks.json"
            for hook in vcr.REQUIRED_HOOKS:
                (Path(tmp) / hook).write_text("# stub\n")
            path.write_text(json.dumps({"hooks": {"PreToolUse": [
                {"matcher": "^Bash$", "hooks": [
                    {"type": "command", "command": f"python3 {Path(tmp) / 'batch_gate.py'}"}]},
                {"matcher": "^apply_patch$", "hooks": [
                    {"type": "command",
                     "command": f"python3 {Path(tmp) / 'block_missing_preclose_inspection.py'}"},
                    {"type": "command",
                     "command": f"python3 {Path(tmp) / 'block_closing_keywords.py'}"}]},
            ]}}), encoding="utf-8")
            ok, missing = vcr.verify(path)
        self.assertFalse(ok)
        self.assertEqual(set(missing), {"block_missing_preclose_inspection.py", "block_closing_keywords.py"})

    def test_filename_in_echo_is_not_registration(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "hooks.json"
            path.write_text(json.dumps({"hooks": {"PreToolUse": [{"hooks": [
                {"type": "command", "command": "echo block_missing_preclose_inspection.py"},
                {"type": "command", "command": "python3 batch_gate.py"},
                {"type": "command", "command": "python3 block_closing_keywords.py"},
            ]}]}}), encoding="utf-8")
            ok, missing = vcr.verify(path)
        self.assertFalse(ok)
        self.assertEqual(set(missing), set(vcr.REQUIRED_HOOKS))

    def test_shell_wrapped_python_is_not_registration(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for hook in vcr.REQUIRED_HOOKS:
                (root / hook).write_text("# stub\n")
            path = root / "hooks.json"
            path.write_text(json.dumps({"hooks": {"PreToolUse": [{"matcher": "^Bash$", "hooks": [
                {"command": f"python3 {root / 'block_missing_preclose_inspection.py'} || true"},
                {"command": f"python3 {root / 'batch_gate.py'}"},
                {"command": f"python3 {root / 'block_closing_keywords.py'}"},
            ]}]}}))
            ok, missing = vcr.verify(path)
        self.assertFalse(ok)
        self.assertEqual(missing, ["block_missing_preclose_inspection.py"])


class MainTests(unittest.TestCase):
    """`main()`'s two real targets (`~/harmonic-forge/.codex/hooks.json` and
    `~/Harmonic_Projects/HRSE2/.codex/hooks.json`) are both faked under a
    temp `HOME`, built directly at their final paths rather than via
    `_write` + rename, to keep each case's setup traceable."""

    def test_main_returns_nonzero_when_any_target_is_missing_hooks(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            forge_dir = tmp_path / "harmonic-forge" / ".codex"
            hrse2_dir = tmp_path / "Harmonic_Projects" / "HRSE2" / ".codex"
            forge_dir.mkdir(parents=True)
            hrse2_dir.mkdir(parents=True)
            _write(forge_dir, list(vcr.REQUIRED_HOOKS))
            _write(hrse2_dir, ["model_tier_gate.py"])  # missing all three
            with patch("verify_codex_registration.Path.home", return_value=tmp_path):
                self.assertEqual(vcr.main(), 1)

    def test_main_returns_zero_when_both_targets_are_fully_wired(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            forge_dir = tmp_path / "harmonic-forge" / ".codex"
            hrse2_dir = tmp_path / "Harmonic_Projects" / "HRSE2" / ".codex"
            forge_dir.mkdir(parents=True)
            hrse2_dir.mkdir(parents=True)
            _write(forge_dir, list(vcr.REQUIRED_HOOKS))
            _write(hrse2_dir, list(vcr.REQUIRED_HOOKS))
            with patch("verify_codex_registration.Path.home", return_value=tmp_path):
                self.assertEqual(vcr.main(), 0)


if __name__ == "__main__":
    unittest.main()
