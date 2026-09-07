#!/usr/bin/env python3
"""Tests for `forge_onboard.py` (harmonic-forge#498).

Hermetic: every test builds a synthetic repo tree and a synthetic
`CLAUDE_CONFIG_DIR` under a temp dir. Nothing reads the operator's real
checkouts or settings — harmonic-forge#500's tests did and turned CI red on
`main` (#505).
"""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import forge_onboard as fo  # noqa: E402
import manifest as mf  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.home = self.root / "home"
        (self.home / ".claude").mkdir(parents=True)
        self.store = self.root / "store"
        (self.store / ".git").mkdir(parents=True)
        self.set_settings({"autoMemoryDirectory": str(self.store)})
        self._env = mock.patch.dict(
            os.environ, {"CLAUDE_CONFIG_DIR": str(self.home / ".claude")})
        self._env.start()
        self.addCleanup(self._env.stop)
        self.addCleanup(self._tmp.cleanup)

    def set_settings(self, data: dict) -> None:
        (self.home / ".claude" / "settings.json").write_text(
            json.dumps(data), encoding="utf-8")

    def make_repo(self, name: str = "thing", *, git: bool = True,
                  worktrees: bool = True, rules: bool = True,
                  claude_md: bool = True, hooks: bool = True) -> Path:
        repo = self.root / name
        repo.mkdir(parents=True, exist_ok=True)
        if git:
            (repo / ".git").mkdir(exist_ok=True)
        if claude_md:
            (repo / "CLAUDE.md").write_text("x", encoding="utf-8")
        if rules:
            rules_dir = repo / ".claude" / "rules"
            rules_dir.mkdir(parents=True, exist_ok=True)
            target = self.root / "platform_rule.md"
            target.write_text("r", encoding="utf-8")
            (rules_dir / "linked.md").symlink_to(target)
        if hooks:
            settings = repo / ".claude" / "settings.json"
            settings.parent.mkdir(parents=True, exist_ok=True)
            settings.write_text(json.dumps({"hooks": {"PreToolUse": []}}),
                                encoding="utf-8")
        if worktrees:
            for n in (2, 3):
                (repo.parent / f"{name}-lane{n}").mkdir(exist_ok=True)
        return repo

    def project(self, checkout: Path | None, **kw) -> mf.Project:
        fields = {"name": "thing", "prefix": "H", "repo": "o/thing",
                  "account": "vitalharmony", "path": str(checkout) if checkout else None,
                  "board_owner": "vitalharmony", "board_number": "1"}
        fields.update(kw)
        return mf.Project(**fields)

    def statuses(self, project: mf.Project) -> dict[str, str]:
        with mock.patch.object(fo, "check_prefix_agreement", return_value=[]), \
                mock.patch.object(fo, "prefixes", return_value={"H": "thing"}):
            return {c.name: c.status for c in fo.verify(project)}


class VerifyTests(Base):
    def test_a_fully_set_up_repo_is_all_green(self) -> None:
        got = self.statuses(self.project(self.make_repo()))
        self.assertNotIn(fo.FAIL, got.values(), got)

    def test_each_missing_piece_fails_its_own_check_only(self) -> None:
        cases = {
            "lane worktrees": {"worktrees": False},
            "directives": {"rules": False},
            "entrypoint": {"claude_md": False},
            "hooks": {"hooks": False},
        }
        for name, kwargs in cases.items():
            with self.subTest(check=name):
                repo = self.make_repo(f"r-{name.split()[0]}", **kwargs)
                got = self.statuses(self.project(repo))
                self.assertEqual(got[name], fo.FAIL, got)
                others = [k for k, v in got.items() if v == fo.FAIL and k != name]
                self.assertEqual(others, [], f"{name} leaked into {others}")

    def test_a_projected_repo_skips_rather_than_fails(self) -> None:
        """SKIP exists so "not applicable" never reads as a pass or a failure.
        A projected repo has no checkout, so it has no worktrees to be
        missing — reporting FAIL would make the manifest permanently red."""
        got = self.statuses(self.project(None, repo=None))
        self.assertNotIn(fo.FAIL, got.values(), got)
        for name in ("checkout", "lane worktrees", "directives", "hooks"):
            self.assertEqual(got[name], fo.SKIP)

    def test_a_settings_file_with_no_hooks_block_fails(self) -> None:
        """AC2 names hooks DRIFT as the expected real-world gap, and the only
        hooks test exercised the missing-FILE branch. Mutating `if not hooks:`
        to `if 0:` left the suite green: `hooks=None` then fell through the
        `isinstance(..., dict)` guard to `ok — 0 event(s)`."""
        repo = self.make_repo("nohooks", hooks=False)
        settings = repo / ".claude" / "settings.json"
        settings.parent.mkdir(parents=True, exist_ok=True)
        settings.write_text(json.dumps({"permissions": {}}), encoding="utf-8")
        got = self.statuses(self.project(repo))
        self.assertEqual(got["hooks"], fo.FAIL)

    def test_an_empty_hooks_block_fails(self) -> None:
        repo = self.make_repo("emptyhooks", hooks=False)
        settings = repo / ".claude" / "settings.json"
        settings.parent.mkdir(parents=True, exist_ok=True)
        settings.write_text(json.dumps({"hooks": {}}), encoding="utf-8")
        self.assertEqual(self.statuses(self.project(repo))["hooks"], fo.FAIL)

    def test_a_dangling_rules_symlink_fails(self) -> None:
        repo = self.make_repo("dangle")
        target = self.root / "platform_rule.md"
        target.unlink()
        self.assertEqual(self.statuses(self.project(repo))["directives"], fo.FAIL)

    def test_an_empty_rules_dir_fails(self) -> None:
        """`sync_rules.py` never ran. A present-but-empty directory read as
        success under a bare `is_dir()` check."""
        repo = self.make_repo("empty", rules=False)
        (repo / ".claude" / "rules").mkdir(parents=True)
        self.assertEqual(self.statuses(self.project(repo))["directives"], fo.FAIL)

    def test_the_platform_repo_skips_the_directives_check(self) -> None:
        """It carries `rules/`, not `.claude/rules/` — it IS the source.
        Detected structurally, because this module runs from whatever worktree
        it was checked out into, so a path comparison against PLATFORM_ROOT
        says "not the platform" every time it is gated."""
        repo = self.make_repo("platform", rules=False)
        (repo / "3-lane-protocol.md").write_text("x", encoding="utf-8")
        (repo / "sync_rules.py").write_text("x", encoding="utf-8")
        self.assertTrue(fo._is_platform(repo))
        self.assertEqual(self.statuses(self.project(repo))["directives"], fo.SKIP)

    def test_a_non_platform_repo_is_not_mistaken_for_one(self) -> None:
        self.assertFalse(fo._is_platform(self.make_repo("ordinary")))


class MemoryCheckTests(Base):
    def test_unset_store_fails(self) -> None:
        self.set_settings({})
        self.assertEqual(fo.check_memory(self.project(None)).status, fo.FAIL)

    def test_missing_store_directory_fails(self) -> None:
        self.set_settings({"autoMemoryDirectory": str(self.root / "nope")})
        self.assertEqual(fo.check_memory(self.project(None)).status, fo.FAIL)

    def test_an_untracked_store_fails(self) -> None:
        """The store is shared across lanes; untracked means unshareable."""
        plain = self.root / "plain"
        plain.mkdir()
        self.set_settings({"autoMemoryDirectory": str(plain)})
        self.assertEqual(fo.check_memory(self.project(None)).status, fo.FAIL)

    def test_unparseable_settings_fails_rather_than_raises(self) -> None:
        (self.home / ".claude" / "settings.json").write_text("{", encoding="utf-8")
        self.assertEqual(fo.check_memory(self.project(None)).status, fo.FAIL)


class BoardCheckTests(Base):
    def test_no_board_by_design_skips(self) -> None:
        """A repo with no board is a decision, not a gap."""
        project = self.project(None, board_owner=None, board_number=None)
        self.assertEqual(fo.check_board(project).status, fo.SKIP)


class ExitCodeTests(Base):
    """0 = green, 1 = a check failed, 2 = could not run. A tool returning
    non-zero for both "found a problem" and "could not look" leaves a gate
    unable to tell a finding from its own misconfiguration."""

    def manifest_for(self, repo: Path | None) -> Path:
        body = textwrap.dedent(f"""
            [[project]]
            name = "thing"
            prefix = "H"
            repo = "o/thing"
            account = "vitalharmony"
            board_owner = "vitalharmony"
            board_number = "1"
        """)
        if repo is not None:
            body += f'path = "{repo}"\n'
        path = self.root / "projects.toml"
        path.write_text(body, encoding="utf-8")
        return path

    def run_main(self, argv: list[str]) -> tuple[int, str]:
        out = io.StringIO()
        with mock.patch.object(sys, "stdout", out), \
                mock.patch.object(sys, "stderr", io.StringIO()), \
                mock.patch.object(fo, "check_prefix_agreement", return_value=[]):
            code = fo.main(argv)
        return code, out.getvalue()

    def test_green_exits_zero(self) -> None:
        path = self.manifest_for(self.make_repo())
        code, text = self.run_main(["--manifest", str(path)])
        self.assertEqual(code, 0, text)

    def test_a_failing_check_exits_one(self) -> None:
        path = self.manifest_for(self.make_repo("broken", hooks=False))
        code, _ = self.run_main(["--manifest", str(path)])
        self.assertEqual(code, 1)

    def test_a_bad_manifest_exits_two_not_one(self) -> None:
        bad = self.root / "bad.toml"
        bad.write_text("# no entries\n", encoding="utf-8")
        self.assertEqual(self.run_main(["--manifest", str(bad)])[0], 2)

    def test_an_unknown_project_name_exits_two(self) -> None:
        path = self.manifest_for(self.make_repo())
        self.assertEqual(
            self.run_main(["--manifest", str(path), "--project", "nope"])[0], 2)

    def test_project_filters_to_one_entry(self) -> None:
        path = self.manifest_for(self.make_repo())
        _code, text = self.run_main(["--manifest", str(path), "--project", "thing"])
        self.assertIn("1 project(s)", text)


class ApplyTests(Base):
    def test_dry_run_changes_nothing(self) -> None:
        repo = self.make_repo("dry", worktrees=False)
        before = sorted(p.name for p in self.root.iterdir())
        with mock.patch.object(fo, "_run", return_value=(0, "")):
            fo.apply(self.project(repo), dry_run=True)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), before)

    def test_apply_is_idempotent_on_existing_worktrees(self) -> None:
        """A second run must not re-create what is there — the whole point of
        an onboarding command you can run at any time."""
        repo = self.make_repo("idem")
        with mock.patch.object(fo, "_run", return_value=(0, "")) as run:
            checks = fo.apply(self.project(repo), dry_run=True)
        worktree_checks = [c for c in checks if c.name.startswith("worktree")]
        self.assertEqual([c.detail for c in worktree_checks],
                         ["already present", "already present"])
        run.assert_not_called()

    def test_a_failed_worktree_creation_is_reported_not_swallowed(self) -> None:
        """Asserted on the `worktree <name>` checks SPECIFICALLY.

        The old assertion was `any(FAIL and "boom" in detail)` over the whole
        list — satisfied by the `directives` check, because `_run` is patched
        globally and the `sync_rules.py` call returns the same tuple. Making
        the worktree branch unconditionally OK left the suite green.
        """
        repo = self.make_repo("failing", worktrees=False)
        with mock.patch.object(fo, "_run", return_value=(128, "fatal: boom")):
            checks = fo.apply(self.project(repo))
        worktree_checks = [c for c in checks if c.name.startswith("worktree ")]
        self.assertEqual(len(worktree_checks), 2, checks)
        for check in worktree_checks:
            self.assertEqual(check.status, fo.FAIL, check)
            self.assertIn("boom", check.detail)

    def test_a_successful_worktree_creation_says_created(self) -> None:
        """The other side of that branch, so neither outcome is unconstrained."""
        repo = self.make_repo("succeeding", worktrees=False)
        with mock.patch.object(fo, "_run", return_value=(0, "")):
            checks = fo.apply(self.project(repo))
        worktree_checks = [c for c in checks if c.name.startswith("worktree ")]
        self.assertEqual([c.status for c in worktree_checks], [fo.OK, fo.OK])
        self.assertEqual([c.detail for c in worktree_checks], ["created", "created"])

    def test_apply_skips_a_repo_with_no_checkout(self) -> None:
        checks = fo.apply(self.project(None, repo=None))
        self.assertEqual([c.status for c in checks], [fo.SKIP])


if __name__ == "__main__":
    unittest.main()
