#!/usr/bin/env python3
"""harmonic-forge#795: l1-post's scratch worktree gets the three Keycloak keys
copied into its own `frontend/.env`, so a test importing the real
apiClient/authManager chain does not fail on missing env alone."""
from __future__ import annotations

import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import l1_post as L  # noqa: E402

SOURCE = ("# local\nVITE_APP_VERSION=2.8.34\nVITE_KEYCLOAK_URL=https://id.example/\n"
          "VITE_KEYCLOAK_REALM=hrse\nVITE_KEYCLOAK_WEB_CLIENT_ID=web\nOTHER_SECRET=nope\n")


class ScratchFrontendEnv(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo, self.scratch = Path(tmp.name) / "repo", Path(tmp.name) / "scratch"
        (self.repo / "frontend").mkdir(parents=True)
        self.scratch.mkdir()

    def source(self, text: str) -> None:
        (self.repo / "frontend" / ".env").write_text(text)

    def target(self) -> Path:
        return self.scratch / "frontend" / ".env"

    def test_tc1_the_three_keys_and_nothing_else_are_copied(self) -> None:
        self.source(SOURCE)
        self.assertIsNone(L._provision_scratch_frontend_env(self.repo, self.scratch))
        self.assertEqual(self.target().read_text(),
                         "VITE_KEYCLOAK_URL=https://id.example/\nVITE_KEYCLOAK_REALM=hrse\n"
                         "VITE_KEYCLOAK_WEB_CLIENT_ID=web\n")

    def test_the_copy_is_a_private_real_file_never_a_symlink(self) -> None:
        self.source(SOURCE)
        L._provision_scratch_frontend_env(self.repo, self.scratch)
        self.assertFalse(self.target().is_symlink())
        self.assertEqual(stat.S_IMODE(os.stat(self.target()).st_mode), 0o600)

    def test_tc3_a_missing_file_or_key_skips_with_a_named_reason_and_without_raising(self) -> None:
        self.assertIn("no value for VITE_KEYCLOAK_URL, VITE_KEYCLOAK_REALM, VITE_KEYCLOAK_WEB_CLIENT_ID",
                      L._provision_scratch_frontend_env(self.repo, self.scratch))
        self.source("VITE_KEYCLOAK_URL=x\nVITE_KEYCLOAK_REALM=y\n")
        self.assertIn("no value for VITE_KEYCLOAK_WEB_CLIENT_ID",
                      L._provision_scratch_frontend_env(self.repo, self.scratch))
        self.assertFalse(self.target().exists())

    def test_an_empty_value_is_not_a_value(self) -> None:
        """runtimeConfig.ts tests truthiness, so `KEY=` would still throw (preclose pass 1)."""
        self.source(SOURCE.replace("VITE_KEYCLOAK_URL=https://id.example/", "VITE_KEYCLOAK_URL="))
        self.assertIn("no value for VITE_KEYCLOAK_URL", L._provision_scratch_frontend_env(self.repo, self.scratch))
        self.assertFalse(self.target().exists())

    def test_an_undecodable_source_file_skips_instead_of_raising(self) -> None:
        (self.repo / "frontend" / ".env").write_bytes(b"VITE_KEYCLOAK_URL=caf\xe9\n")
        self.assertIn("no value", L._provision_scratch_frontend_env(self.repo, self.scratch))

    def test_an_existing_scratch_file_is_never_overwritten(self) -> None:
        self.source(SOURCE)
        self.target().parent.mkdir()
        self.target().write_text("MINE=1\n")
        self.assertIn("already exists", L._provision_scratch_frontend_env(self.repo, self.scratch))
        self.assertEqual(self.target().read_text(), "MINE=1\n")

    def test_the_ready_for_l3_scratch_build_calls_it_for_hrse(self) -> None:
        src = Path(L.__file__).read_text()
        block = src[src.index("for dependency_dir in HRSE_DEPENDENCY_DIRS:"):]
        block = block[:block.index("hooks-install")]
        self.assertIn("skipped = _provision_scratch_frontend_env(repo_root, scratch)", block)
        self.assertIn("not provisioned: {skipped}", block)


if __name__ == "__main__":
    unittest.main()
