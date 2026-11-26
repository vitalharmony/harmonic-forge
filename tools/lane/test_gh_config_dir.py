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

[accounts.vitalharmony]
lane_slots = true
"""
#: `gh api user` answers the slot's `login` file, only for its token if it has one (#1528).
STUB_GH = """#!/bin/sh
[ "$*" = "api user --jq .login" ] || exit 1
tok=$(awk '/oauth_token:/ {print $2; exit}' "$GH_CONFIG_DIR/hosts.yml" 2>/dev/null)
[ -z "$tok" ] || [ "$GH_TOKEN" = "$tok" ] || exit 1
cat "$GH_CONFIG_DIR/login" 2>/dev/null || exit 1
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
        self.slot("harmonicarchitect")
        self.bin = self.root / "bin"
        self.bin.mkdir()
        (self.bin / "gh").write_text(STUB_GH)
        (self.bin / "gh").chmod(0o755)

    def slot(self, name: str, login: str | None = None, token: str | None = None) -> Path:
        """A fixture slot whose stub `gh` login is `login` (default: the account)."""
        path = self.slots / name
        path.mkdir(parents=True, exist_ok=True)
        (path / "login").write_text((login or name.split("-lane")[0]) + "\n")
        if token:
            (path / "hosts.yml").write_text(f"github.com:\n    oauth_token: {token}\n")
        return path

    def launch(self, origin: str | None, extra_env: dict[str, str] | None = None) -> tuple[int, str, str]:
        repo = self.root / f"repo{len(list(self.root.iterdir()))}"
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        if origin is not None:
            subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", origin], check=True)
        env = {"PATH": f"{self.bin}:/usr/bin:/bin", "HOME": str(self.root),
               "GH_ACCT_HOME": str(self.slots), **(extra_env or {})}
        proc = subprocess.run(
            ["bash", "-c", f'target="{repo}"; source "{self.script}"; rc=$?; '
                           'echo "rc=$rc dir=${GH_CONFIG_DIR:-UNSET} tok=${GH_TOKEN:-UNSET}"'],
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

    def test_another_host_naming_a_registered_repo_refuses(self) -> None:
        for origin in ("https://notgithub.com/vitalharmony/hrse",
                       "https://evil.example/github.com/vitalharmony/hrse.git",
                       "https://github.com/vitalharmony/hrse.git.git"):
            with self.subTest(origin=origin):
                _, out, _ = self.launch(origin)
                self.assertIn("dir=UNSET", out)
                self.assertNotIn("rc=0", out)

    def test_a_module_planted_in_the_checkout_cannot_choose_the_account(self) -> None:
        repo = self.root / "planted"
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        subprocess.run(["git", "-C", str(repo), "remote", "add", "origin",
                        "https://github.com/some-unregistered-owner/x.git"], check=True)
        # `re` is often already imported at interpreter start; tomllib is
        # imported only when the manifest is read, so it proves isolation.
        for module in ("re.py", "tomllib.py"):
            # flush=True: os._exit skips stdio flushing, and an unflushed
            # payload would be refused as empty output, hiding the real test.
            (repo / module).write_text(
                "import os\nprint('harmonicarchitect', flush=True)\nos._exit(0)\n")
        proc = subprocess.run(
            ["bash", "-c", f'cd "{repo}"; target="{repo}"; source "{self.script}"; rc=$?; '
                           'echo "rc=$rc dir=${GH_CONFIG_DIR:-UNSET}"'],
            capture_output=True, text=True,
            env={"PATH": "/usr/bin:/bin", "HOME": str(self.root), "GH_ACCT_HOME": str(self.slots)})
        self.assertIn("dir=UNSET", proc.stdout)
        self.assertNotIn("rc=0", proc.stdout)

    def overlay_repo(self, repo_key: str, overlay: bool = True) -> Path:
        """A checkout of `repo_key` registered only by its own .claude/forge.toml."""
        owner, name = repo_key.split("/")
        repo = self.root / f"co-{owner}-{name}"
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        subprocess.run(["git", "-C", str(repo), "remote", "add", "origin",
                        f"https://github.com/{repo_key}.git"], check=True)
        if overlay:
            (repo / ".claude").mkdir()
            (repo / ".claude" / "forge.toml").write_text(
                f'[project]\nname = "{name}"\nprefix = "S"\nrepo = "{repo_key}"\n'
                f'account = "{owner}"\n')
        return repo

    def launch_at(self, repo: Path, **env: str) -> tuple[str, str]:
        """Source the script for `repo` from the fixture root, never `repo` (TC10)."""
        proc = subprocess.run(
            ["bash", "-c", f'target="{repo}"; source "{self.script}"; rc=$?; '
                           'echo "rc=$rc dir=${GH_CONFIG_DIR:-UNSET} tok=${GH_TOKEN:-UNSET}"'],
            capture_output=True, text=True, cwd=self.root,
            env={"PATH": f"{self.bin}:/usr/bin:/bin", "HOME": str(self.root),
                 "GH_ACCT_HOME": str(self.slots), **env})
        return proc.stdout.strip(), proc.stderr

    # harmonic-forge#1528 AC1 (and #1527 TC10): an account only the checkout's own
    # forge.toml declares launches on the ambient login from $target, not the cwd.
    def test_an_overlay_only_account_runs_ambient_with_one_line(self) -> None:
        repo = self.overlay_repo("acme/app")
        for lane in ("1", "2"):
            with self.subTest(lane=lane):
                out, err = self.launch_at(repo, LANE=lane, GH_TOKEN="x")
                self.assertEqual(out, "rc=0 dir=UNSET tok=x", err)
                self.assertEqual(len(err.strip().splitlines()), 1, err)
                self.assertTrue("'acme'" in err and "ambient" in err, err)

    def test_ac2_the_overlay_only_checkout_without_its_overlay_refuses(self) -> None:
        out, err = self.launch_at(self.overlay_repo("acme/app", overlay=False), LANE="1")
        self.assertTrue(out.startswith("rc=1 dir=UNSET"), out)
        self.assertIn("is not registered in projects.toml or .claude/forge.toml", err)

    # Pitch-inspection revision 1: a new repo under a projects.toml-declared account refuses.
    def test_an_overlay_only_repo_under_a_declared_account_refuses(self) -> None:
        self.slot("vitalharmony")
        for key in ("vitalharmony/scratch-app", "harmonicarchitect/new-app"):
            with self.subTest(key=key):
                out, err = self.launch_at(self.overlay_repo(key), GH_TOKEN="x")
                self.assertTrue(out.startswith("rc=1 dir=UNSET tok=x"), out)
                self.assertNotIn("ambient", err)

    def test_a_relative_slot_root_refuses(self) -> None:
        _, out, err = self.launch("https://github.com/kenekted/kenekted-ai.git",
                                  {"GH_ACCT_HOME": "slots"})
        self.assertIn("dir=UNSET", out)
        self.assertNotIn("rc=0", out)
        self.assertIn("not an absolute path", err)

    def test_a_registered_repo_whose_owner_is_not_the_account_resolves(self) -> None:
        for origin in ("https://github.com/kenekted/kenekted-ai.git",
                       "git@github.com:kenekted/kenekted-ai.git"):
            with self.subTest(origin=origin):
                _, out, _ = self.launch(origin, {"GH_TOKEN": "x"})
                self.assertEqual(out, f"rc=0 dir={self.slots / 'harmonicarchitect'} tok=UNSET")

    # AC3: every projects.toml account is strict.
    def test_a_declared_account_with_no_slot_refuses_naming_it(self) -> None:
        _, out, err = self.launch("https://github.com/LeasePAL-ML/LeasePAL-App-Prototype.git")
        self.assertTrue(out.startswith("rc=1 dir=UNSET"), out)
        self.assertTrue("'leasepal'" in err and "projects.toml" in err, err)

    def test_a_declared_account_on_a_valid_slot_launches(self) -> None:
        self.slot("leasepal")
        _, out, err = self.launch("https://github.com/LeasePAL-ML/LeasePAL-App-Prototype.git")
        self.assertEqual(out, f"rc=0 dir={self.slots / 'leasepal'} tok=UNSET", err)

    def test_a_mismatched_login_refuses_naming_sources_not_values(self) -> None:
        for origin, account in (("https://github.com/kenekted/kenekted-ai.git", "harmonicarchitect"),
                                ("https://github.com/LeasePAL-ML/LeasePAL-App-Prototype.git", "leasepal")):
            with self.subTest(account=account):
                slot = self.slot(account, login="someoneelse")
                _, out, err = self.launch(origin)
                self.assertTrue(out.startswith("rc=1 dir=UNSET"), out)
                self.assertIn("projects.toml", err)
                self.assertIn(str(slot), err)
                self.assertNotIn("someoneelse", err)

    def test_a_non_lane_launch_of_a_lane_slots_account_uses_the_account_slot(self) -> None:
        self.slot("vitalharmony")
        _, out, err = self.launch("https://github.com/vitalharmony/hrse.git")
        self.assertEqual(out, f"rc=0 dir={self.slots / 'vitalharmony'} tok=UNSET", err)

    # TC4: [accounts.vitalharmony] lane_slots = true, LANE=2: the lane slot and its token.
    def test_a_lane_slots_account_in_a_lane_exports_the_lane_slot_and_token(self) -> None:
        lane = self.slot("vitalharmony-lane2", token="lane2tok")
        _, out, err = self.launch("https://github.com/vitalharmony/hrse.git",
                                  {"LANE": "2", "GH_TOKEN": "inherited"})
        self.assertEqual(out, f"rc=0 dir={lane} tok=lane2tok", err)

    def test_a_lane_slot_with_no_token_refuses(self) -> None:
        self.slot("vitalharmony-lane2")
        _, out, err = self.launch("https://github.com/vitalharmony/hrse.git", {"LANE": "2"})
        self.assertTrue(out.startswith("rc=1 dir=UNSET"), out)
        self.assertIn("harmonic-forge#1396", err)

if __name__ == "__main__":
    unittest.main()
