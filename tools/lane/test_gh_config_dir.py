#!/usr/bin/env python3
"""harmonic-forge#1507: _gh_config_dir.sh resolves the lane's gh account from the
manifest, never from URL text, and refuses anything it cannot resolve.

Each case sources a COPY of the script inside a fixture forge root (tools/lane,
tools/onboard and its own projects.toml), the same place the real script reads
its manifest from, so no case reads the live manifest or a real gh slot."""
from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

LANE_DIR = Path(__file__).resolve().parent
MANIFEST = """\
[[project]]
name = "hrse"
prefix = "H"
repo = "vitalharmony/hrse"
account = "vitalharmony"
path = "/nonexistent/hrse"
onboarded = false
workspace = "vh"

[[project]]
name = "kenekted-ai"
prefix = "Y"
repo = "kenekted/kenekted-ai"
account = "harmonicarchitect"
path = "/nonexistent/kenekted-ai"
onboarded = false
workspace = "ka"

[[project]]
name = "leasepal"
prefix = "P"
repo = "LeasePAL-ML/LeasePAL-App-Prototype"
account = "leasepal"
path = "/nonexistent/leasepal"
onboarded = false
workspace = "leasepal"
"""


class GhConfigDir(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        forge = self.root / "forge"
        (forge / "tools" / "lane").mkdir(parents=True)
        shutil.copy(LANE_DIR / "_gh_config_dir.sh", forge / "tools" / "lane")
        shutil.copytree(LANE_DIR.parent / "onboard", forge / "tools" / "onboard",
                        ignore=shutil.ignore_patterns("__pycache__"))
        (forge / "projects.toml").write_text(MANIFEST)
        self.script = forge / "tools" / "lane" / "_gh_config_dir.sh"
        self.slots = self.root / "slots"
        (self.slots / "harmonicarchitect").mkdir(parents=True)

    def launch(self, origin: str | None, extra_env: dict[str, str] | None = None) -> tuple[int, str, str]:
        repo = self.root / f"repo{len(list(self.root.iterdir()))}"
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        if origin is not None:
            subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", origin], check=True)
        env = {"PATH": "/usr/bin:/bin", "HOME": str(self.root), "GH_ACCT_HOME": str(self.slots),
               **(extra_env or {})}
        proc = subprocess.run(
            ["bash", "-c", f'target="{repo}"; source "{self.script}"; rc=$?; '
                           'echo "rc=$rc dir=${GH_CONFIG_DIR:-UNSET}"'],
            capture_output=True, text=True, env=env)
        return proc.returncode, proc.stdout.strip(), proc.stderr

    # AC1
    def test_an_unregistered_origin_refuses_naming_both_files(self) -> None:
        _, out, err = self.launch("https://github.com/some-unregistered-owner/x.git")
        self.assertIn("dir=UNSET", out)
        self.assertNotIn("rc=0", out)
        self.assertIn("some-unregistered-owner/x", err)
        self.assertIn("projects.toml", err)
        self.assertIn(".claude/forge.toml", err)

    def test_no_origin_refuses(self) -> None:
        _, out, err = self.launch(None)
        self.assertIn("dir=UNSET", out)
        self.assertNotIn("rc=0", out)
        self.assertIn("'none'", err)

    def test_an_owner_substring_lookalike_refuses(self) -> None:
        _, out, err = self.launch("https://github.com/evil/vitalharmony-x.git")
        self.assertIn("dir=UNSET", out)
        self.assertNotIn("rc=0", out)
        self.assertIn("evil/vitalharmony-x", err)

    def test_a_planted_manifest_override_is_ignored(self) -> None:
        planted = self.root / "planted.toml"
        planted.write_text(MANIFEST.replace("vitalharmony/hrse", "evil/x"))
        _, out, _ = self.launch("https://github.com/evil/x.git",
                                {"FORGE_PROJECTS_MANIFEST": str(planted)})
        self.assertIn("dir=UNSET", out)
        self.assertNotIn("rc=0", out)

    # AC2
    def test_a_registered_repo_whose_owner_is_not_the_account_resolves(self) -> None:
        for origin in ("https://github.com/kenekted/kenekted-ai.git",
                       "git@github.com:kenekted/kenekted-ai.git"):
            with self.subTest(origin=origin):
                _, out, _ = self.launch(origin)
                self.assertEqual(out, f"rc=0 dir={self.slots / 'harmonicarchitect'}")

    def test_an_account_with_no_lane_slot_refuses_naming_it(self) -> None:
        _, out, err = self.launch("https://github.com/LeasePAL-ML/LeasePAL-App-Prototype.git")
        self.assertIn("dir=UNSET", out)
        self.assertNotIn("rc=0", out)
        self.assertIn("'leasepal'", err)

    # AC3
    def test_a_vitalharmony_non_lane_launch_uses_the_account_slot(self) -> None:
        _, out, _ = self.launch("https://github.com/vitalharmony/hrse.git")
        self.assertEqual(out, f"rc=0 dir={self.slots / 'vitalharmony'}")


if __name__ == "__main__":
    unittest.main()
