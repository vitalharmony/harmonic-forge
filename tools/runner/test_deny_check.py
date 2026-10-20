"""harmonic-forge#1053: the te-runner deny manifest, its rendered settings, the
final-diff check and the fork-boundary check."""
from __future__ import annotations

import contextlib
import io
import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import deny_check

FORGE = deny_check.HERE.parents[1]


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                          text=True).stdout.strip()


def tracked() -> list[str]:
    return git(FORGE, "ls-files").splitlines()


def run(argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = deny_check.main(argv)
    return rc, out.getvalue(), err.getvalue()


class ManifestCoverage(unittest.TestCase):
    def setUp(self):
        self.globs = [g for g in map(deny_check.glob_regex, deny_check.globs()) if g]
        self.paths = tracked()

    def glob_hit(self, path: str) -> bool:
        return any(g.search(path) for g in self.globs)

    def test_high_blast_patterns_shape_is_pattern_reason_pairs(self):
        for entry in deny_check.HIGH_BLAST_PATTERNS:
            pattern, why = entry
            self.assertTrue(hasattr(pattern, "search"))
            self.assertIsInstance(why, str)

    def test_every_high_blast_match_is_covered_by_a_glob(self):
        for pattern, _why in deny_check.HIGH_BLAST_PATTERNS:
            for path in self.paths:
                if pattern.search(path):
                    self.assertTrue(self.glob_hit(path), f"{path} ({pattern.pattern}) has no glob")

    def test_globs_and_diff_check_agree_over_the_tree(self):
        for path in self.paths:
            self.assertEqual(self.glob_hit(path), deny_check.denied(path), path)

    def test_every_r0371_surface_is_denied(self):
        surfaces = {
            "AE / ready-for-l3 posting": "tools/gh/l1_post.py",
            "batch_auth merge grant": "tools/hooks/batch_auth.py",
            "preclose-inspected label gate": "tools/hooks/block_missing_preclose_inspection.py",
            "model-tier gate": "tools/hooks/model_tier_gate.py",
            "auto-AE": "tools/gh/_auto_ae.py",
            "review waiver": "tools/gh/imc_waiver.py",
            "PreToolUse deny hooks": ".claude/settings.json",
        }
        for surface, path in surfaces.items():
            self.assertIn(path, self.paths, f"{surface}: {path} is not tracked")
            self.assertTrue(self.glob_hit(path), f"{surface}: no glob denies {path}")
            self.assertTrue(deny_check.denied(path), f"{surface}: diff check misses {path}")

    def test_named_g2_paths_are_denied(self):
        for path in ("tools/lane/lane1", "tools/gh/post_lane1_issue.py", "tools/gh/preclose_check.py",
                     "tools/gh/pitch_receipt.py", "tools/gh/kill_check.py", "tools/gh/sticky_receipt.py",
                     "tools/gh/_standing_grant.py", "tools/gh/l2_post.py", "tools/gh/batch_x.py",
                     "rules/universal-agent.md", "3-lane-protocol.md", "agents/x.md",
                     ".codex/hooks.json", "tools/runner/deny_manifest.json", "mise.toml"):
            self.assertTrue(deny_check.denied(path), path)
        self.assertIn("~/.local/share/te-runner/kill-bundles/**", deny_check.globs())

    def test_synthetic_high_blast_paths_are_covered_by_globs(self):
        # Paths the tree does not hold yet, so the ls-files test cannot see them.
        for path in ("scripts/migrations/0001_init.py", "scripts/migrate_x.py",
                     "x/scripts/data_migration/a.py", "conftest.py", "tools/x/conftest.py",
                     "pytest.ini", "a/tox.ini", "setup.cfg", "pyproject.toml",
                     "sitecustomize.py", "tools/x/usercustomize.py", "x/evil.pth",
                     "sitecustomize/__init__.py", "x/usercustomize/__init__.py",
                     "lib/sitecustomize.cpython-313-x86_64-linux-gnu.so"):
            self.assertTrue(deny_check.denied(path), path)
            self.assertTrue(self.glob_hit(path), f"{path} has no glob")

    def test_ordinary_tooling_is_not_denied(self):
        for path in ("tools/telemetry/emit.py", "tools/gh/gh_issue.py", "docs/x.md",
                     "tools/x/conftest_helper.py", "docs/pyproject.toml.md",
                     "tools/x/sitecustomize_notes.md"):
            self.assertFalse(deny_check.denied(path), path)


class Settings(unittest.TestCase):
    def test_committed_settings_equal_render(self):
        self.assertEqual(deny_check.SETTINGS.read_text(), deny_check.render_settings())

    def test_settings_shape(self):
        perms = json.loads(deny_check.render_settings())["permissions"]
        self.assertEqual(perms["allow"], list(deny_check.ALLOW))
        for cmd in ("gh pr merge", "gh issue close", "gh issue edit", "gh label", "gh api",
                    "git push", "git remote"):
            self.assertIn(f"Bash({cmd}:*)", perms["deny"])
        for glob in deny_check.globs():
            self.assertIn(f"Edit({glob})", perms["deny"])
        self.assertFalse([d for d in perms["deny"] if d.startswith("Write(")])


class Diff(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = Path(tmp.name)
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "config", "user.name", "t")
        git(self.repo, "config", "user.email", "t@example.test")
        for rel in ("tools/hooks/h.py", "tools/telemetry/a.py", "docs/d.md"):
            (self.repo / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.repo / rel).write_text("x\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "base")
        self.base = git(self.repo, "rev-parse", "HEAD")
        cwd = os.getcwd()
        os.chdir(self.repo)
        self.addCleanup(os.chdir, cwd)

    def commit(self) -> str:
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "head")
        return git(self.repo, "rev-parse", "HEAD")

    def check(self, head: str) -> tuple[int, list]:
        rc, out, err = run(["diff", "--base", self.base, "--head", head])
        return rc, (json.loads(out)["hits"] if out else err)

    def test_clean_diff_exits_zero(self):
        (self.repo / "tools/telemetry/a.py").write_text("y\n")
        self.assertEqual(self.check(self.commit()), (0, []))

    def test_edit_in_denied_path_is_a_hit(self):
        (self.repo / "tools/hooks/h.py").write_text("y\n")
        self.assertEqual(self.check(self.commit()), (3, ["tools/hooks/h.py"]))

    def test_rename_out_of_denied_path_is_a_hit(self):
        git(self.repo, "mv", "tools/hooks/h.py", "tools/telemetry/h.py")
        rc, hits = self.check(self.commit())
        self.assertEqual(rc, 3)
        self.assertIn("tools/hooks/h.py", hits)

    def test_rename_into_denied_path_is_a_hit(self):
        (self.repo / "rules").mkdir()
        git(self.repo, "mv", "tools/telemetry/a.py", "rules/a.py")
        rc, hits = self.check(self.commit())
        self.assertEqual(rc, 3)
        self.assertIn("rules/a.py", hits)

    def test_deleting_a_denied_file_is_a_hit(self):
        git(self.repo, "rm", "-q", "tools/hooks/h.py")
        self.assertEqual(self.check(self.commit()), (3, ["tools/hooks/h.py"]))

    def test_mode_change_on_denied_file_is_a_hit(self):
        path = self.repo / "tools/hooks/h.py"
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        self.assertEqual(self.check(self.commit()), (3, ["tools/hooks/h.py"]))

    def test_symlink_into_denied_path_is_a_hit(self):
        (self.repo / "docs/link.py").symlink_to("../tools/hooks/h.py")
        self.assertEqual(self.check(self.commit()), (3, ["docs/link.py"]))

    def test_symlink_out_of_repo_is_a_hit(self):
        (self.repo / "docs/out").symlink_to("/etc/passwd")
        self.assertEqual(self.check(self.commit()), (3, ["docs/out"]))

    def test_symlink_to_a_denied_directory_is_a_hit(self):
        for name, target in (("hk", "tools/hooks"), ("r", "../rules"), ("c", ".claude")):
            (self.repo / "docs" / name).symlink_to(target)
        rc, hits = self.check(self.commit())
        self.assertEqual((rc, hits), (3, ["docs/c", "docs/hk", "docs/r"]))

    def test_any_new_symlink_is_a_hit(self):
        (self.repo / "docs/plain").symlink_to("d.md")
        self.assertEqual(self.check(self.commit()), (3, ["docs/plain"]))

    def test_bad_ref_exits_two(self):
        self.assertEqual(run(["diff", "--base", self.base, "--head", "nope"])[0], 2)

    def test_paths_subcommand(self):
        rc, out, _ = run(["paths", "docs/d.md", "./tools/hooks/x.py"])
        self.assertEqual((rc, json.loads(out)["hits"]), (3, ["./tools/hooks/x.py"]))
        rc, out, _ = run(["paths", "docs/d.md"])
        self.assertEqual((rc, json.loads(out)["hits"]), (0, []))

    def test_paths_refuses_non_canonical_spellings(self):
        for spelling in ("/home/u/wt/rules/x.md", "/rules/x.md", "tools/gh/../../rules/x.md",
                         "tools//hooks/a.py", "tools/./hooks/a.py", "rules\\x.md", "rules/", ""):
            rc, out, err = run(["paths", spelling])
            self.assertEqual((rc, out), (2, ""), spelling)
            self.assertIn("not a canonical", err, spelling)

    def test_paths_denies_a_repo_prefixed_spelling(self):
        for spelling in ("harmonic-forge/rules/x.md", "harmonic-forge/tools/gh/batch_auth.py",
                         "harmonic-forge/tools/runner/deny_check.py",
                         "harmonic-forge/3-lane-protocol.md"):
            rc, out, _ = run(["paths", spelling])
            self.assertEqual((rc, json.loads(out)["hits"]), (3, [spelling]), spelling)


class MalformedManifest(unittest.TestCase):
    def test_malformed_manifest_exits_two(self):
        for bad in ({"groups": [{"why": "x", "globs": [], "regexes": ["("]}]},
                    {"groups": [{"why": "x"}]}, {}):
            with mock.patch.object(deny_check, "manifest", return_value=bad):
                for argv in (["paths", "docs/x.md"], ["render-settings"]):
                    rc, out, err = run(argv)
                    self.assertEqual(rc, 2, (bad, argv, out))
                    self.assertIn("deny-check:", err)


class Boundary(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        env = mock.patch.dict(os.environ, {"PATH": f"{self.dir}:{os.environ['PATH']}",
                                           "HOME": str(self.dir), "GH_TOKEN": "leak",
                                           "GITHUB_TOKEN": "leak"})
        env.start()
        self.addCleanup(env.stop)

    def fake_gh(self, login: object, perms: object) -> None:
        user = json.dumps({"login": login} if login is not None else {})
        repo = json.dumps({"permissions": perms})
        script = self.dir / "gh"
        script.write_text(
            "#!/bin/sh\n"
            f"env > {self.dir}/env.txt\n"
            f"case \"$2\" in user) echo '{user}' ;; *) echo '{repo}' ;; esac\n")
        script.chmod(0o755)

    def test_boundary_holds(self):
        self.fake_gh("vitalharmony-runner", {"push": False, "admin": False})
        rc, out, _ = run(["boundary"])
        self.assertEqual(rc, 0, out)
        env = (self.dir / "env.txt").read_text()
        # Exact names: CI sets MISE_GITHUB_TOKEN, which is not the gh token.
        names = {line.split("=", 1)[0] for line in env.splitlines()}
        self.assertNotIn("GH_TOKEN", names)
        self.assertNotIn("GITHUB_TOKEN", names)
        self.assertIn(f"GH_CONFIG_DIR={self.dir}/.config/te-runner/gh", env)

    def test_boundary_refuses_push_true(self):
        self.fake_gh("vitalharmony-runner", {"push": True, "admin": False})
        self.assertNotEqual(run(["boundary"])[0], 0)

    def test_boundary_refuses_admin_true(self):
        self.fake_gh("vitalharmony-runner", {"push": False, "admin": True})
        self.assertNotEqual(run(["boundary"])[0], 0)

    def test_boundary_refuses_wrong_login(self):
        self.fake_gh("vitalharmony", {"push": False, "admin": False})
        self.assertNotEqual(run(["boundary"])[0], 0)

    def test_boundary_refuses_missing_login(self):
        self.fake_gh(None, {"push": False, "admin": False})
        self.assertNotEqual(run(["boundary"])[0], 0)


if __name__ == "__main__":
    unittest.main()
