"""Focused immutable-target tests for F326's Gemini Lane 3 binding."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

MODULE_DIR = Path(__file__).parents[1] / "gemini" / "lane3-context"
sys.path.insert(0, str(MODULE_DIR))
import lane3_target_binding as binding


class AttestedTargetTests(unittest.TestCase):
    def test_provider_no_ae_signal_performs_no_git_target_access(self) -> None:
        unavailable = subprocess.CompletedProcess(
            args=[], returncode=3, stdout="",
            stderr="[FETCH-L1-CONTEXT] target metadata unavailable: no current AE",
        )
        with patch.object(binding.subprocess, "run", return_value=unavailable) as run:
            with self.assertRaisesRegex(binding.NoAttestedTarget, "no current AE"):
                binding.resolve_target(
                    Path("/trusted/provider.py"), "vitalharmony/harmonic-forge", "326",
                    Path("/registered/forge-lane3"),
                )
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.args[0][0], "python3")
        self.assertIn("--target-metadata", run.call_args.args[0])

    def test_provider_metadata_is_exact_and_commit_must_exist(self) -> None:
        sha = "a" * 40
        responses = [json.dumps({"repository": "vitalharmony/harmonic-forge", "sha": sha}), ""]
        with patch.object(binding, "_run", side_effect=responses) as run:
            target = binding.resolve_target(
                Path("/trusted/provider.py"), "vitalharmony/harmonic-forge", "326",
                Path("/registered/forge-lane3"),
            )
        self.assertEqual(target.sha, sha)
        self.assertEqual(run.call_args_list[0].args[:2], ("python3", "/trusted/provider.py"))
        self.assertIn("--target-metadata", run.call_args_list[0].args)
        self.assertEqual(run.call_args_list[1].args[:3], ("git", "cat-file", "-e"))

    def test_provider_cannot_widen_repository_or_metadata_shape(self) -> None:
        bad = (
            {"repository": "someone/fork", "sha": "a" * 40},
            {"repository": "vitalharmony/harmonic-forge", "sha": "a" * 40, "branch": "main"},
        )
        for metadata in bad:
            with self.subTest(metadata=metadata), \
                 patch.object(binding, "_run", return_value=json.dumps(metadata)):
                with self.assertRaisesRegex(RuntimeError, "wrong canonical|widened"):
                    binding.resolve_target(
                        Path("/trusted/provider.py"), "vitalharmony/harmonic-forge", "326",
                        Path("/registered/forge-lane3"),
                    )

    def test_reads_attested_object_instead_of_stale_head(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            self._git(repo, "init")
            self._git(repo, "config", "user.email", "lane3@example.invalid")
            self._git(repo, "config", "user.name", "Lane 3 Fixture")
            file = repo / "target.txt"
            file.write_text("attested\n")
            self._git(repo, "add", "target.txt")
            self._git(repo, "commit", "-m", "attested")
            attested_sha = self._git(repo, "rev-parse", "HEAD").strip()
            file.write_text("stale-head\n")
            self._git(repo, "commit", "-am", "stale head")

            target = binding.AttestedTarget(
                "vitalharmony/harmonic-forge", attested_sha, repo,
            )
            self.assertEqual(binding.read_file(target, "target.txt"), "attested\n")
            self.assertEqual(binding.list_files(target, "*.txt"), ["target.txt"])
            self.assertIn("target.txt:1:attested", binding.search_text(target, "attested", "*.txt"))
            self.assertNotIn("stale-head", binding.search_text(target, "attested", "*.txt"))

    def test_traversal_and_model_supplied_revs_are_rejected(self) -> None:
        target = binding.AttestedTarget(
            "vitalharmony/harmonic-forge", "a" * 40, Path("/registered/forge-lane3"),
        )
        for path in ("../secret", "/etc/passwd", "HEAD:README.md", "a/./b"):
            with self.subTest(path=path), self.assertRaisesRegex(RuntimeError, "relative Git path"):
                binding.read_file(target, path)

    @staticmethod
    def _git(repo: Path, *args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=repo, text=True, capture_output=True, check=True,
        ).stdout


if __name__ == "__main__":
    unittest.main()
