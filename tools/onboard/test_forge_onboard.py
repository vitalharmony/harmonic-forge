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
import subprocess
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
                  claude_md: bool = True, hooks: bool = True,
                  lane_task_names: tuple[str, ...] | None = None) -> Path:
        repo = self.root / name
        repo.mkdir(parents=True, exist_ok=True)
        if git:
            (repo / ".git").mkdir(exist_ok=True)
        # A correctly-onboarded repo by default (harmonic-forge#730): these
        # fixtures exist to exercise the OTHER checks, and a missing task layer
        # would make every one of them report a lane-tasks failure as noise.
        # Pass `lane_task_names` to build a repo that is deliberately short.
        names = (("l1-post", "lane-comment", "gate-checkout", "lane3-begin", "lane3-end")
                 if lane_task_names is None else lane_task_names)
        (repo / "mise.toml").write_text(
            "".join(f'[tasks.{n}]\nrun = "true"\n\n' for n in names), encoding="utf-8")
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
                  "board_owner": "vitalharmony", "board_number": "1",
                  "protocol": mf.Protocol(
                      worktree_name="{checkout}-lane{lane}",
                      l1_post_task="l1-post", lane_comment_task="lane-comment",
                      gate_checkout_task="gate-checkout", lane3_begin_task="lane3-begin",
                      lane3_end_task="lane3-end", runs_lane3=True,
                      # These synthetic repos have no gate-adapter.json, and
                      # check_gate_adapter now refuses silence -- declare it,
                      # exactly as a real no-graph repo does.
                      needs_gate_adapter=False)}
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

    def test_missing_protocol_fails_its_own_check(self) -> None:
        got = self.statuses(self.project(self.make_repo("noprotocol"), protocol=None))
        self.assertEqual(got["protocol"], fo.FAIL)

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
            onboarded = true
        """)
        if repo is not None:
            body += f'path = "{repo}"\n'
        body += textwrap.dedent("""
            [project.protocol]
            worktree_name = "{checkout}-lane{lane}"
            l1_post_task = "l1-post"
            lane_comment_task = "lane-comment"
            gate_checkout_task = "gate-checkout"
            lane3_begin_task = "lane3-begin"
            lane3_end_task = "lane3-end"
            runs_lane3 = true
            needs_gate_adapter = false
        """)
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


class AdvanceStaleWorktreeTests(unittest.TestCase):
    """harmonic-forge#560 preclose follow-up: nothing ever advanced a worktree.

    `apply()` created missing worktrees and stopped there, so a hook fix merged
    to `main` reached the main checkout only — and the lane launchers run in the
    worktrees. Nine lane worktrees kept the broken matcher through the merge.

    The three refusals below are each a lesson, not a precaution.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self._git("init", "-q", "-b", "main")
        (self.repo / "f.txt").write_text("x")
        self._git("add", "-A")
        self._git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "one")

    def _git(self, *args, cwd=None):
        return subprocess.run(["git", "-C", str(cwd or self.repo), *args],
                              capture_output=True, text=True)

    def _worktree(self, name):
        path = self.root / name
        self._git("worktree", "add", "-q", "--detach", str(path), "HEAD")
        return path

    def test_a_clean_detached_worktree_is_safe(self):
        ok, why = fo._worktree_is_safe_to_advance(self._worktree("wt"))
        self.assertTrue(ok, why)

    def test_uncommitted_changes_refuse(self):
        """`git checkout` carries uncommitted tracked changes ACROSS the
        switch rather than isolating them, so advancing would not merely risk
        someone's work — it would silently relocate it."""
        path = self._worktree("dirty")
        (path / "f.txt").write_text("changed")
        ok, why = fo._worktree_is_safe_to_advance(path)
        self.assertFalse(ok)
        self.assertIn("uncommitted", why)

    def test_a_worktree_on_a_branch_refuses(self):
        """A worktree on a branch is a branch OWNER. Advancing it moves that
        ref out from under whoever holds it."""
        path = self.root / "onbranch"
        self._git("worktree", "add", "-q", "-b", "someones-work", str(path), "HEAD")
        ok, why = fo._worktree_is_safe_to_advance(path)
        self.assertFalse(ok)
        self.assertIn("not detached", why)

    def test_a_live_lane3_gate_refuses(self):
        """hrse#1757, exactly. A checkout landing on a worktree mid-gate is the
        incident that issue was filed for — and the Lane 1 session that caused
        it was doing precisely this: advancing lane worktrees after a merge."""
        path = self._worktree("gating")
        git_dir = Path(self._git("rev-parse", "--absolute-git-dir", cwd=path).stdout.strip())
        (git_dir / "LANE3_ACTIVE").write_text(f"owner_pid={os.getpid()}\n")
        ok, why = fo._worktree_is_safe_to_advance(path)
        self.assertFalse(ok)
        self.assertIn("live pid", why)

    def test_a_dead_owner_does_not_block(self):
        """The dead-owner branch is deliberate and must stay: refusing on any
        marker at all would deadlock every worktree whose gate ended without
        cleanup, which is the failure hrse#1757's own file documents."""
        path = self._worktree("stalegate")
        git_dir = Path(self._git("rev-parse", "--absolute-git-dir", cwd=path).stdout.strip())
        (git_dir / "LANE3_ACTIVE").write_text("owner_pid=999999999\n")
        ok, why = fo._worktree_is_safe_to_advance(path)
        self.assertTrue(ok, why)

    def test_a_malformed_marker_does_not_block(self):
        path = self._worktree("badmarker")
        git_dir = Path(self._git("rev-parse", "--absolute-git-dir", cwd=path).stdout.strip())
        (git_dir / "LANE3_ACTIVE").write_text("garbage\n")
        self.assertTrue(fo._worktree_is_safe_to_advance(path)[0])

    def test_reconciliation_uses_declared_path_and_ignores_disabled_lane3(self):
        lanes = self.root / "lanes"
        lanes.mkdir()
        lane2 = lanes / "lane2-repo"
        lane3 = lanes / "lane3-repo"
        for path in (lane2, lane3):
            self._git("worktree", "add", "-q", "--detach", str(path), "HEAD")
            settings = path / ".claude" / "settings.json"
            settings.parent.mkdir(parents=True)
            settings.write_text(json.dumps({"hooks": {"SessionStart": [{
                "matcher": "startup|resume",
                "hooks": [{"command": "python3 belt_wakeup.py"}],
            }]}}))
        project = mf.Project(
            name="repo", prefix="R", path=str(self.repo), worktree_dir=str(lanes),
            protocol=mf.Protocol(
                worktree_name="lane{lane}-{checkout}", l1_post_task="l1-post",
                lane_comment_task="lane-comment", gate_checkout_task="gate-checkout",
                lane3_begin_task="lane3-begin", lane3_end_task="lane3-end",
                runs_lane3=False))

        self.assertEqual(fo.stale_worktree_hook_gaps(project), [lane2.name])
        with mock.patch.object(fo, "_worktree_is_safe_to_advance",
                               return_value=(True, "safe")) as safety:
            checks = fo.advance_stale_worktrees(project, dry_run=True)
        safety.assert_called_once_with(lane2)
        self.assertEqual([check.name for check in checks], [f"worktree {lane2.name}"])


class WorktreeCommitCurrencyTests(unittest.TestCase):
    """harmonic-forge#746: the existing worktree check measured hook drift,
    not commit drift, so a worktree 90 commits behind origin/main read
    identically to a current one. Each test here is the live incident's own
    state, reproduced hermetically."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self._git("init", "-q", "-b", "main")
        (self.repo / "f.txt").write_text("x")
        self._git("add", "-A")
        self._git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "one")
        # A real `origin` remote, so `origin/main` exists to compare against
        # -- `worktree_commit_currency` reads that ref directly, never a
        # local `main` branch, matching how `check_worktrees` fetches it.
        self.origin = self.root / "origin.git"
        self._git("init", "-q", "--bare", str(self.origin))
        self._git("remote", "add", "origin", str(self.origin))
        self._git("push", "-q", "origin", "main")

    def _git(self, *args, cwd=None):
        return subprocess.run(["git", "-C", str(cwd or self.repo), *args],
                              capture_output=True, text=True)

    def _worktree(self, name):
        path = self.root / name
        self._git("worktree", "add", "-q", "--detach", str(path), "HEAD")
        return path

    def _commit_to_main(self, n=1):
        for i in range(n):
            (self.repo / "f.txt").write_text(f"x{i}")
            self._git("add", "-A")
            self._git("-c", "user.email=t@t", "-c", "user.name=t",
                      "commit", "-qm", f"advance {i}")
        self._git("push", "-q", "origin", "main")

    def test_current_worktree_reports_current(self):
        path = self._worktree("wt")
        self._git("fetch", "-q", "origin", "main", cwd=path)
        currency = fo.worktree_commit_currency(path)
        self.assertEqual(currency.state, "current")
        self.assertFalse(currency.failing)

    def test_worktree_behind_the_threshold_is_not_stale(self):
        path = self._worktree("wt")
        self._commit_to_main(fo.STALE_WORKTREE_BEHIND_THRESHOLD)
        self._git("fetch", "-q", "origin", "main", cwd=path)
        currency = fo.worktree_commit_currency(path)
        self.assertEqual(currency.state, "current", currency.detail)
        self.assertFalse(currency.failing)

    def test_worktree_past_the_threshold_is_stale(self):
        """The live incident: 90 commits behind, and the existing check read
        `ok` because it only ever compared settings.json."""
        path = self._worktree("wt")
        self._commit_to_main(fo.STALE_WORKTREE_BEHIND_THRESHOLD + 1)
        self._git("fetch", "-q", "origin", "main", cwd=path)
        currency = fo.worktree_commit_currency(path)
        self.assertEqual(currency.state, "stale")
        self.assertTrue(currency.failing)
        self.assertIn(str(fo.STALE_WORKTREE_BEHIND_THRESHOLD + 1), currency.detail)

    def test_a_worktree_on_an_unmerged_branch_is_reported_as_branch_not_stale(self):
        """AC5: HRSE2-lane3 at 1459ef2c with PR #2022 open was the live
        case -- behind-main is the wrong predicate for an unmerged feature
        branch, and advancing it would discard in-flight Lane 2 work. The
        behind-count is still reported, per AC2's "every lane worktree"."""
        path = self.root / "onbranch"
        self._git("worktree", "add", "-q", "-b", "someones-work", str(path), "HEAD")
        (path / "own.txt").write_text("z")
        self._git("add", "-A", cwd=path)
        self._git("-c", "user.email=t@t", "-c", "user.name=t",
                  "commit", "-qm", "in-flight work", cwd=path)
        self._commit_to_main(fo.STALE_WORKTREE_BEHIND_THRESHOLD + 50)
        self._git("fetch", "-q", "origin", "main", cwd=path)
        currency = fo.worktree_commit_currency(path)
        self.assertEqual(currency.state, "branch")
        self.assertFalse(currency.failing)
        self.assertIn("someones-work", currency.detail)
        self.assertIn(str(fo.STALE_WORKTREE_BEHIND_THRESHOLD + 50), currency.detail)

    def test_a_worktree_on_a_merged_branch_is_still_scored_as_stale(self):
        """Preclose finding: the original branch short-circuit exempted
        EVERY branch worktree from measurement, not just genuinely unmerged
        ones -- a worktree left on a fully-merged (or never-diverged) local
        branch reported no behind-count and never failed, which is exactly
        the 90-behind-and-green failure mode this issue exists to fix, one
        layer down."""
        path = self.root / "onbranch"
        self._git("worktree", "add", "-q", "-b", "stale-branch", str(path), "HEAD")
        self._commit_to_main(fo.STALE_WORKTREE_BEHIND_THRESHOLD + 1)
        self._git("fetch", "-q", "origin", "main", cwd=path)
        currency = fo.worktree_commit_currency(path)
        self.assertEqual(currency.state, "stale")
        self.assertTrue(currency.failing)
        self.assertIn(str(fo.STALE_WORKTREE_BEHIND_THRESHOLD + 1), currency.detail)

    def test_a_detached_orphan_commit_is_reported_never_silently_kept(self):
        """AC3: harmonic-forge-lane3 was detached with 2 commits no branch
        contained -- a superseded local copy, not lost work, but the check
        must say so rather than reading it as merely 'ahead'."""
        path = self._worktree("orphan")
        (path / "local.txt").write_text("y")
        self._git("add", "-A", cwd=path)
        self._git("-c", "user.email=t@t", "-c", "user.name=t",
                  "commit", "-qm", "local-only", cwd=path)
        self._git("fetch", "-q", "origin", "main", cwd=path)
        currency = fo.worktree_commit_currency(path)
        self.assertEqual(currency.state, "detached-orphan")
        self.assertTrue(currency.failing)
        self.assertIn("no branch contains HEAD", currency.detail)

    def test_dirty_worktree_is_reported_and_never_conflated_with_stale(self):
        """AC4: `_worktree_is_safe_to_advance` already refuses to advance a
        dirty worktree; this check must not blur that into a currency verdict
        that would read as 'safe, just old'."""
        path = self._worktree("dirty")
        (path / "f.txt").write_text("changed")
        self._git("fetch", "-q", "origin", "main", cwd=path)
        currency = fo.worktree_commit_currency(path)
        self.assertEqual(currency.state, "dirty")
        self.assertFalse(currency.failing)
        self.assertIn("uncommitted", currency.detail)

    def test_a_dirty_stale_worktree_still_fails_dirty_does_not_mask_stale(self):
        """Preclose finding, the sharpest one: a first version returned
        `"dirty"` as a TERMINAL state before the stale check ever ran, so
        the live incident's own shape -- a shared lane worktree 90 behind
        with one stray uncommitted edit left in it -- read `ok`."""
        path = self._worktree("dirtystale")
        self._commit_to_main(fo.STALE_WORKTREE_BEHIND_THRESHOLD + 1)
        self._git("fetch", "-q", "origin", "main", cwd=path)
        (path / "f.txt").write_text("changed")
        currency = fo.worktree_commit_currency(path)
        self.assertEqual(currency.state, "stale")
        self.assertTrue(currency.failing)
        self.assertIn("uncommitted", currency.detail)
        self.assertIn(str(fo.STALE_WORKTREE_BEHIND_THRESHOLD + 1), currency.detail)

    def test_a_non_git_path_is_no_git_and_does_not_fail_the_check(self):
        """The predecessor check only tested existence, and many synthetic
        test fixtures elsewhere in this suite are a bare directory, not a
        real git worktree. This must report "no-git" and never fail --
        `test_a_fully_set_up_repo_is_all_green` depends on it -- and stay
        distinct from "unreadable", which IS a git worktree that broke."""
        path = self.root / "not-a-worktree"
        path.mkdir()
        currency = fo.worktree_commit_currency(path)
        self.assertEqual(currency.state, "no-git")
        self.assertFalse(currency.failing)

    def test_a_broken_real_worktree_is_unreadable_and_fails(self):
        """Preclose finding: a REAL worktree whose gitdir link is broken
        (pruned while the directory survived, or a `.git` file pointing at a
        removed admin dir) must not read identically to a synthetic
        no-git fixture -- "could not measure" is not "healthy"."""
        path = self._worktree("brokengit")
        git_file = path / ".git"
        self.assertTrue(git_file.is_file())  # a worktree's .git is a gitdir pointer file
        git_file.write_text("gitdir: /nonexistent/path\n")
        currency = fo.worktree_commit_currency(path)
        self.assertEqual(currency.state, "unreadable")
        self.assertTrue(currency.failing)

    def test_a_failed_fetch_fails_the_check_rather_than_reading_stale_data(self):
        """Preclose finding: the fetch's exit code was discarded, so a
        network/auth failure left every worktree scored against whatever
        `origin/main` ref already happened to be on disk -- in the live
        incident, a ref pinned 90 commits stale computed "0 behind" and
        reported current. `check_worktrees` must fail loudly instead."""
        lane2 = self._worktree("repo-lane2")
        lane3 = self._worktree("repo-lane3")
        project = mf.Project(
            name="repo", prefix="R", path=str(self.repo), worktree_dir=str(self.root),
            protocol=mf.Protocol(
                worktree_name="{checkout}-lane{lane}", l1_post_task="l1-post",
                lane_comment_task="lane-comment", gate_checkout_task="gate-checkout",
                lane3_begin_task="lane3-begin", lane3_end_task="lane3-end",
                runs_lane3=True))
        self.assertEqual(set(project.worktrees), {lane2, lane3})
        real_run = fo._run

        def failing_fetch(cmd):
            if "fetch" in cmd:
                return (1, "fatal: could not read from remote")
            return real_run(cmd)

        with mock.patch.object(fo, "_run", side_effect=failing_fetch):
            check = fo.check_worktrees(project)
        self.assertEqual(check.status, fo.FAIL, check.detail)
        self.assertIn("could not fetch", check.detail)

    def test_check_worktrees_fails_on_a_stale_lane_worktree(self):
        lane2 = self._worktree("repo-lane2")
        lane3 = self._worktree("repo-lane3")
        self._commit_to_main(fo.STALE_WORKTREE_BEHIND_THRESHOLD + 1)
        project = mf.Project(
            name="repo", prefix="R", path=str(self.repo), worktree_dir=str(self.root),
            protocol=mf.Protocol(
                worktree_name="{checkout}-lane{lane}", l1_post_task="l1-post",
                lane_comment_task="lane-comment", gate_checkout_task="gate-checkout",
                lane3_begin_task="lane3-begin", lane3_end_task="lane3-end",
                runs_lane3=True))
        self.assertEqual(set(project.worktrees), {lane2, lane3})
        check = fo.check_worktrees(project)
        self.assertEqual(check.status, fo.FAIL, check.detail)
        self.assertIn(f"{fo.STALE_WORKTREE_BEHIND_THRESHOLD + 1} behind", check.detail)

    def test_check_worktrees_passes_on_current_lane_worktrees(self):
        self._worktree("repo-lane2")
        self._worktree("repo-lane3")
        project = mf.Project(
            name="repo", prefix="R", path=str(self.repo), worktree_dir=str(self.root),
            protocol=mf.Protocol(
                worktree_name="{checkout}-lane{lane}", l1_post_task="l1-post",
                lane_comment_task="lane-comment", gate_checkout_task="gate-checkout",
                lane3_begin_task="lane3-begin", lane3_end_task="lane3-end",
                runs_lane3=True))
        check = fo.check_worktrees(project)
        self.assertEqual(check.status, fo.OK, check.detail)

    def test_release_worktree_is_out_of_scope_not_measured(self):
        """AC6: a `-release` worktree is not named by `Project.worktrees` at
        all, so it never reaches `worktree_commit_currency` -- excluded by
        construction, not by a silent gap."""
        self._worktree("repo-lane2")
        self._worktree("repo-lane3")
        release = self._worktree("repo-release")
        self._commit_to_main(fo.STALE_WORKTREE_BEHIND_THRESHOLD + 90)
        project = mf.Project(
            name="repo", prefix="R", path=str(self.repo), worktree_dir=str(self.root),
            protocol=mf.Protocol(
                worktree_name="{checkout}-lane{lane}", l1_post_task="l1-post",
                lane_comment_task="lane-comment", gate_checkout_task="gate-checkout",
                lane3_begin_task="lane3-begin", lane3_end_task="lane3-end",
                runs_lane3=True))
        self.assertNotIn(release, project.worktrees)


class LaneTaskCheckTests(Base):
    """harmonic-forge#730 — the check that makes the false-claim class unrepeatable.

    `projects.toml` claimed `onboarded = true` / `runs_lane3 = true` for two
    repos with almost no lane task layer. `check_protocol` passed them both: it
    only ever confirmed the block existed, never that the task names inside it
    resolve.
    """

    def test_a_declared_task_missing_from_mise_toml_fails_and_is_named(self) -> None:
        repo = self.make_repo(lane_task_names=("l1-post", "lane-comment", "gate-checkout"))
        check = fo.check_lane_tasks(self.project(repo))
        self.assertEqual(check.status, fo.FAIL)
        # Named, not merely counted -- "some tasks missing" sends the reader
        # back to diff two files by hand.
        self.assertIn("lane3-begin", check.detail)
        self.assertIn("lane3-end", check.detail)
        self.assertIn("lane3_begin_task", check.detail)

    def test_all_five_present_passes(self) -> None:
        check = fo.check_lane_tasks(self.project(self.make_repo()))
        self.assertEqual(check.status, fo.OK)
        self.assertIn("5", check.detail)

    def test_the_check_reads_the_DECLARED_name_not_a_hardcoded_one(self) -> None:
        """A repo free to rename its tasks is why this reads the manifest."""
        repo = self.make_repo(lane_task_names=("l1-post", "lane-comment", "gate-checkout",
                                               "lane3-begin", "session-close"))
        renamed = self.project(repo, protocol=mf.Protocol(
            worktree_name="{checkout}-lane{lane}", l1_post_task="l1-post",
            lane_comment_task="lane-comment", gate_checkout_task="gate-checkout",
            lane3_begin_task="lane3-begin", lane3_end_task="session-close",
            runs_lane3=True, needs_gate_adapter=False))
        self.assertEqual(fo.check_lane_tasks(renamed).status, fo.OK)
        # ...and the conventional name is then the one that is missing.
        self.assertEqual(fo.check_lane_tasks(self.project(repo)).status, fo.FAIL)

    def test_a_richer_task_body_still_passes(self) -> None:
        """hrse's lane3-begin stamps a pid, runs a port preflight and takes a
        scheduler lease. A body-match check would have failed it on day one."""
        repo = self.make_repo()
        mise = repo / "mise.toml"
        mise.write_text(mise.read_text(encoding="utf-8").replace(
            '[tasks.lane3-begin]\nrun = "true"',
            '[tasks.lane3-begin]\nrun = "echo stamp pid; echo take lease; echo preflight"'),
            encoding="utf-8")
        self.assertEqual(fo.check_lane_tasks(self.project(repo)).status, fo.OK)

    def test_no_mise_toml_at_all_fails(self) -> None:
        repo = self.make_repo()
        (repo / "mise.toml").unlink()
        self.assertEqual(fo.check_lane_tasks(self.project(repo)).status, fo.FAIL)


class GateAdapterDeclarationTests(Base):
    """DJC 2: a Lane 3 repo has a manifest or says it needs none. Silence fails."""

    def _protocol(self, **kw):
        base = dict(worktree_name="{checkout}-lane{lane}", l1_post_task="l1-post",
                    lane_comment_task="lane-comment", gate_checkout_task="gate-checkout",
                    lane3_begin_task="lane3-begin", lane3_end_task="lane3-end",
                    runs_lane3=True)
        base.update(kw)
        return mf.Protocol(**base)

    def test_silence_fails(self) -> None:
        project = self.project(self.make_repo(), protocol=self._protocol())
        check = fo.check_gate_adapter(project)
        self.assertEqual(check.status, fo.FAIL)
        self.assertIn("needs_gate_adapter", check.detail)

    def test_an_explicit_declaration_passes(self) -> None:
        project = self.project(self.make_repo(),
                               protocol=self._protocol(needs_gate_adapter=False))
        self.assertEqual(fo.check_gate_adapter(project).status, fo.OK)

    def test_a_real_manifest_passes_without_any_declaration(self) -> None:
        repo = self.make_repo()
        adapter = repo / ".claude" / "gate-adapter.json"
        adapter.parent.mkdir(parents=True, exist_ok=True)
        adapter.write_text("{}", encoding="utf-8")
        project = self.project(repo, protocol=self._protocol())
        self.assertEqual(fo.check_gate_adapter(project).status, fo.OK)

    def test_a_non_lane3_repo_is_skipped_not_failed(self) -> None:
        project = self.project(self.make_repo(), protocol=self._protocol(runs_lane3=False))
        self.assertEqual(fo.check_gate_adapter(project).status, fo.SKIP)


class LaneTaskGeneratorTests(Base):
    """The generator writes a FLOOR, and writing it twice changes nothing."""

    def test_apply_adds_only_what_is_missing(self) -> None:
        repo = self.make_repo(lane_task_names=("gate-checkout",))
        before = (repo / "mise.toml").read_text(encoding="utf-8")
        done = fo.apply_lane_tasks(self.project(repo))
        after = (repo / "mise.toml").read_text(encoding="utf-8")
        self.assertEqual(done[0].status, fo.OK)
        # The repo's own gate-checkout is untouched: exactly one remains.
        self.assertEqual(after.count("[tasks.gate-checkout]"), 1)
        self.assertIn('run = "true"', after)      # ...and it is still theirs
        for name in ("l1-post", "lane-comment", "lane3-begin", "lane3-end"):
            self.assertIn(f"[tasks.{name}]", after)
        self.assertNotEqual(before, after)

    def test_apply_is_idempotent(self) -> None:
        repo = self.make_repo(lane_task_names=())
        fo.apply_lane_tasks(self.project(repo))
        once = (repo / "mise.toml").read_text(encoding="utf-8")
        second = fo.apply_lane_tasks(self.project(repo))
        twice = (repo / "mise.toml").read_text(encoding="utf-8")
        self.assertEqual(once, twice, "a second --apply changed the file")
        self.assertEqual(second[0].detail, "already present")

    def test_the_generated_text_is_identical_across_repos(self) -> None:
        """AC1: sourced from the shared mechanism, not copy-pasted per repo."""
        first, second = self.make_repo("one", lane_task_names=()), self.make_repo("two", lane_task_names=())
        for repo in (first, second):
            fo.apply_lane_tasks(self.project(repo))
        a = (first / "mise.toml").read_text(encoding="utf-8")
        b = (second / "mise.toml").read_text(encoding="utf-8")
        self.assertEqual(a, b)

    def test_the_generated_block_is_valid_toml(self) -> None:
        import tomllib  # noqa: PLC0415
        repo = self.make_repo(lane_task_names=())
        fo.apply_lane_tasks(self.project(repo))
        parsed = tomllib.loads((repo / "mise.toml").read_text(encoding="utf-8"))
        self.assertEqual(sorted(parsed["tasks"]),
                         ["gate-checkout", "l1-post", "lane-comment", "lane3-begin", "lane3-end"])

    def test_no_generated_task_carries_a_repo_relative_platform_path(self) -> None:
        """The one non-portable line this issue found: a repo-relative
        `tools/worktree/check_worktree_busy.py` resolves only in the platform."""
        import lane_tasks as lt  # noqa: PLC0415
        block = lt.render()
        self.assertIn("HARMONIC_FORGE_ROOT", block)
        self.assertNotIn('python3 tools/worktree/', block)
