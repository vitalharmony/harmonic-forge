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


class HookContentTests(unittest.TestCase):
    """harmonic-forge#547 preclose findings 2 and 3.

    `check_hooks` read only the event KEY NAMES and never a `command` string,
    which produced two failures with one root cause:

      * renaming or moving a platform hook script made every session in every
        consuming repo run a command that exits 2 and emits nothing — for
        `SessionStart` that is silent, indistinguishable from the pre-#547
        state the issue was filed about — while this tool reported green;
      * creating a `settings.json` containing a single `SessionStart` entry
        flipped a repo from FAIL to OK while it still carried zero PreToolUse
        guards, extinguishing a real guard-absence signal.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.script = self.root / "harmonic-forge" / "tools" / "hooks" / "belt_wakeup.py"
        self.script.parent.mkdir(parents=True)
        self.script.write_text("#!/usr/bin/env python3\n", encoding="utf-8")

    def _hooks(self, command: str, matcher: str = "startup|resume|clear|fork") -> dict:
        # Default is the FULL source set (harmonic-forge#560). This fixture used
        # to hard-code `startup|resume`, which is the shipped defect itself —
        # so every test built on it was asserting against a settings file that
        # could never wake a `/clear` session.
        return {"SessionStart": [{"matcher": matcher,
                                  "hooks": [{"type": "command", "command": command}]}]}

    def test_the_shipped_startup_resume_matcher_is_reported_as_a_gap(self) -> None:
        """harmonic-forge#560, stated as the check that would have caught it.

        `clear` is a distinct SessionStart source, so `startup|resume` never
        fired for `lane<N> /clear` — the launch the operator actually uses.
        Nothing errored; the hook simply never ran.
        """
        hooks = self._hooks("python3 belt_wakeup.py", matcher="startup|resume")
        self.assertEqual(fo.sessionstart_source_gaps(hooks), ["clear", "fork"])

    def test_full_coverage_reports_no_gap(self) -> None:
        self.assertEqual(
            fo.sessionstart_source_gaps(self._hooks("python3 belt_wakeup.py")), [])

    def test_a_repo_not_wiring_the_wakeup_at_all_is_not_a_source_gap(self) -> None:
        """Absent is a different finding from mis-matched, and conflating them
        would make this check fire on every repo that legitimately has no
        wake-up hook."""
        hooks = self._hooks("python3 something_else.py", matcher="startup")
        self.assertEqual(fo.sessionstart_source_gaps(hooks), [])

    def test_a_source_gap_fails_the_hooks_check(self) -> None:
        """It must FAIL, not warn. A wake-up that never fires for the launch
        path in daily use reads green while the session is unguarded."""
        checkout = self.root / "gaprepo"
        (checkout / ".claude").mkdir(parents=True)
        (checkout / ".claude" / "settings.json").write_text(
            json.dumps({"hooks": self._hooks(
                f'python3 "{self.root}/harmonic-forge/tools/hooks/belt_wakeup.py"',
                matcher="startup|resume")}),
            encoding="utf-8")
        check = fo.check_hooks(mf.Project(name="g", prefix="G", path=str(checkout)))
        self.assertEqual(check.status, fo.FAIL)
        self.assertIn("clear", check.detail)

    def test_resolvable_target_reports_nothing_missing(self) -> None:
        hooks = self._hooks(f'python3 "{self.root}/harmonic-forge/tools/hooks/belt_wakeup.py"')
        self.assertEqual(fo.unresolvable_hook_targets(hooks), [])

    def test_renamed_script_is_reported(self) -> None:
        """The exact scenario: rename the script, every repo silently no-ops."""
        hooks = self._hooks(
            f'python3 "{self.root}/harmonic-forge/tools/hooks/belt_wakeup_RENAMED.py"')
        missing = fo.unresolvable_hook_targets(hooks)
        self.assertEqual(len(missing), 1)
        self.assertIn("belt_wakeup_RENAMED.py", missing[0])

    def test_home_expansion_is_handled(self) -> None:
        with mock.patch.dict(os.environ, {"HOME": str(self.root)}):
            hooks = self._hooks(
                'python3 "${HOME}/harmonic-forge/tools/hooks/belt_wakeup.py"')
            self.assertEqual(fo.unresolvable_hook_targets(hooks), [])
            gone = self._hooks(
                'python3 "${HOME}/harmonic-forge/tools/hooks/nope.py"')
            self.assertEqual(len(fo.unresolvable_hook_targets(gone)), 1)

    def test_non_platform_commands_are_not_inspected(self) -> None:
        """A project's own hook is not this tool's business."""
        hooks = self._hooks("python3 ./scripts/my_own_hook.py")
        self.assertEqual(fo.unresolvable_hook_targets(hooks), [])

    def test_every_event_is_scanned_not_just_the_first(self) -> None:
        hooks = self._hooks(f'python3 "{self.root}/harmonic-forge/tools/hooks/belt_wakeup.py"')
        hooks["PreToolUse"] = [{"matcher": "Bash", "hooks": [
            {"type": "command",
             "command": f'python3 "{self.root}/harmonic-forge/tools/hooks/absent.py"'}]}]
        missing = fo.unresolvable_hook_targets(hooks)
        self.assertEqual(len(missing), 1)
        self.assertIn("absent.py", missing[0])

    def test_malformed_hook_entries_do_not_crash(self) -> None:
        for hooks in ({"SessionStart": "not-a-list"},
                      {"SessionStart": [None]},
                      {"SessionStart": [{"hooks": None}]},
                      {"SessionStart": [{"hooks": [{"command": 3}]}]}):
            with self.subTest(hooks=hooks):
                self.assertEqual(fo.unresolvable_hook_targets(hooks), [])

    def test_guard_count_distinguishes_a_lone_sessionstart(self) -> None:
        """Finding 3: one event must not read the same as a full guard set."""
        checkout = self.root / "repo"
        (checkout / ".claude").mkdir(parents=True)
        (checkout / ".claude" / "settings.json").write_text(
            json.dumps({"hooks": self._hooks(
                f'python3 "{self.root}/harmonic-forge/tools/hooks/belt_wakeup.py"')}),
            encoding="utf-8")
        project = mf.Project(name="x", prefix="X", path=str(checkout))
        check = fo.check_hooks(project)
        self.assertEqual(check.status, fo.OK)
        self.assertIn("0 PreToolUse matcher(s)", check.detail)

    def test_unresolvable_target_fails_the_check(self) -> None:
        checkout = self.root / "repo2"
        (checkout / ".claude").mkdir(parents=True)
        (checkout / ".claude" / "settings.json").write_text(
            json.dumps({"hooks": self._hooks(
                f'python3 "{self.root}/harmonic-forge/tools/hooks/gone.py"')}),
            encoding="utf-8")
        project = mf.Project(name="y", prefix="Y", path=str(checkout))
        check = fo.check_hooks(project)
        self.assertEqual(check.status, fo.FAIL)
        self.assertIn("gone.py", check.detail)


if __name__ == "__main__":
    unittest.main()


class SessionStartMatcherSemanticsTests(unittest.TestCase):
    """harmonic-forge#560 preclose. A matcher is a REGEX, not a pipe list.

    The first draft split on "|" and tested membership, which got the one
    common case right and every other case wrong.
    """

    def _wired(self, *matchers):
        return {"SessionStart": [
            {"matcher": m, "hooks": [{"command": "python3 belt_wakeup.py"}]}
            for m in matchers]}

    def test_coverage_accumulates_across_blocks(self):
        """Returning on the FIRST block naming the hook reported a split
        configuration as covering only half. Not hypothetical: this same change
        establishes the two-block shape by adding a `compact` block."""
        self.assertEqual(
            fo.sessionstart_source_gaps(self._wired("startup|resume", "clear|fork")),
            [])

    def test_an_omitted_matcher_matches_everything(self):
        self.assertEqual(fo.sessionstart_source_gaps(self._wired(None)), [])

    def test_a_catch_all_regex_matches_everything(self):
        self.assertEqual(fo.sessionstart_source_gaps(self._wired(".*")), [])

    def test_matching_is_unanchored_like_the_runtime(self):
        """Claude Code dispatches with `new RegExp(m).test(source)`, which is
        unanchored. A stricter check here would report a gap for a matcher that
        actually fires — a check disagreeing with the thing it checks is worse
        than no check."""
        self.assertEqual(fo.sessionstart_source_gaps(self._wired("start|resum|clea|for")),
                         [])

    def test_an_invalid_regex_covers_nothing(self):
        gaps = fo.sessionstart_source_gaps(self._wired("*[unclosed"))
        self.assertEqual(gaps, list(fo.WAKEUP_SOURCES))

    def test_a_malformed_sessionstart_block_does_not_raise(self):
        """It returned a Check on every other malformed input and raised
        AttributeError on this one, which escaped `main` and aborted every
        remaining project in the manifest."""
        self.assertEqual(fo.sessionstart_source_gaps({"SessionStart": ["oops"]}), [])
