#!/usr/bin/env python3
"""Tests for the pre-close check planner (hrse#1208).

Several of these exist because the pre-close panel found the first version's
tests asserting the wrong properties -- notably a no-PASS check that grepped
source text with a syntactic heuristic and could not see an f-string, and a
one-pass suite that patched away the very cwd-dependence that made the guard
evadable. Both now run the real entry point and assert on real output.
"""

import argparse
import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).parent


def load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


preclose = load("preclose_check")


def git(*args: str, cwd: Path) -> None:
    subprocess.run(("git", *args), cwd=cwd, check=True, capture_output=True)


ANCHORED = {"anchor": "scripts/x.py:12", "scenario": "input Y crashes at line 12",
            "mechanism": "re-parsing shell text"}
CROSS = "Red-team provenance: cross-family (codex / gpt-x) — 2 of 2 assumption(s) checked"
NOT_TRIGGERED = "Red-team provenance: in-family only (claude-opus-5) — cross-family branch not triggered"
FALLBACK = "Red-team provenance: in-family fallback (claude-opus-5) — status process-error"


class ScratchRepo(unittest.TestCase):
    """A real git repo, because the defects found live in git interaction."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name)
        git("init", "-q", "-b", "main", cwd=self.repo)
        git("config", "user.email", "t@t.test", cwd=self.repo)
        git("config", "user.name", "t", cwd=self.repo)
        git("remote", "add", "origin", "https://github.com/vitalharmony/hrse.git", cwd=self.repo)
        # Real repos gitignore this; without it the tool's own receipt
        # dirties the tree and the next run refuses on its own artifact.
        (self.repo / ".gitignore").write_text(".claude/cache/\n")
        (self.repo / "seed.txt").write_text("seed\n")
        git("add", "-A", cwd=self.repo)
        git("commit", "-qm", "seed", cwd=self.repo)
        git("branch", "base", cwd=self.repo)
        self.cwd = os.getcwd()
        os.chdir(self.repo)
        self.addCleanup(os.chdir, self.cwd)
        # harmonic-forge#701: labels are computed by the provenance tool, never
        # typed. A stand-in reads a structurally valid envelope.
        tool = self.repo.parent / f"{self.repo.name}-provenance.py"
        tool.write_text(
            "import sys\n"
            "if '--not-triggered' in sys.argv:\n"
            f"    print({NOT_TRIGGERED!r})\n"
            "else:\n"
            "    import json\n"
            "    print(json.load(open(sys.argv[sys.argv.index('--envelope') + 1]))['label'])\n")
        self.addCleanup(lambda: tool.unlink(missing_ok=True))
        patcher = patch.object(preclose, "PROVENANCE_TOOL", tool)
        patcher.start()
        self.addCleanup(patcher.stop)
        # harmonic-forge#778: receipt_dir() is now user-level (Path.home()),
        # not repo-anchored -- point HOME at this scratch repo's own tree
        # (never patch receipt_dir()/Path.home() directly: OnePassTests
        # deliberately runs the real function to prove it does NOT vary with
        # cwd, and a mocked-out home would hide that the same way a mocked
        # repo_root() once hid the pre-#778 cwd bug this class was written
        # against). This still isolates every test from the real operator's
        # ~/.claude/state/preclose/.
        self.home_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.home_tmp.cleanup)
        home_patcher = patch.dict(os.environ, {"HOME": self.home_tmp.name})
        home_patcher.start()
        self.addCleanup(home_patcher.stop)
        # Existing planner tests exercise their own concerns; F846's receipt
        # behavior is tested separately with the real checker.
        kill_patcher = patch.object(preclose, "kill_receipt_ok", return_value=True)
        kill_patcher.start()
        self.addCleanup(kill_patcher.stop)

    def commit(self, relpath: str, body: str = "x\n") -> None:
        target = self.repo / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body)
        git("add", "-A", cwd=self.repo)
        git("commit", "-qm", f"add {relpath}", cwd=self.repo)

    def findings_file(self, findings: list) -> str:
        path = Path(tempfile.mkdtemp()) / "findings.json"
        self.addCleanup(lambda: path.unlink(missing_ok=True))
        path.write_text(json.dumps(findings))
        return str(path)

    def complete(self, findings: list | None = None, envelope_label: str | None = CROSS,
                 not_triggered: bool = False, cross_family: bool = False,
                 force: bool = False) -> str:
        """harmonic-forge#701: a completion carries the panel's findings and an
        envelope (or --not-triggered); the label is computed from it. Default:
        a silent panel, cross-family taken."""
        findings = [] if findings is None else findings
        envelope = None
        if not not_triggered:
            envelope = self.findings_file([])[:-len("findings.json")] + "envelope.txt"
            status = "process-error" if envelope_label == FALLBACK else "ok"
            body = {"status": status, "label": envelope_label or ""}
            if status == "ok":
                body["report"] = {"assumptions": [{"verdict": "confirmed"}]}
                body.update({"family": "codex", "posture": "verify", "exit_code": 0,
                             "caller_family": "claude", "target_family": "codex",
                             "native": [{"type": "thread.started"}, {"type": "item.completed", "item": {"type": "agent_message"}}]})
            else:
                body["exit_code"] = 1
            Path(envelope).write_text(json.dumps(body))
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            preclose.complete(_Args(repo="vitalharmony/hrse", issue=1208, base="base", head="HEAD",
                                    findings=self.findings_file(findings), envelope=envelope,
                                    not_triggered=not_triggered, cross_family=cross_family,
                                    force=force))
        return buffer.getvalue()

    def plan(self, **overrides) -> str:
        args = {"repo": "vitalharmony/hrse", "issue": 1208, "base": "base", "head": "HEAD",
                "tier": None, "force": False, "allow_dirty": False, "allow_repo_mismatch": False}
        args.update(overrides)
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            preclose.plan(_Args(**args))
        return buffer.getvalue()


class _Args:
    def __init__(self, **kwargs):
        self.allow_repo_mismatch = False  # harmonic-forge#704: complete() now shares plan()'s guard
        # harmonic-forge#889: every pass records its cost; tests that are not
        # about cost get a measured pass, and a cross-family time with an envelope.
        self.panel_tokens, self.panel_ms, self.cost_unavailable = "1000", "2000", None
        self.cross_family_ms = "300" if kwargs.get("envelope") else None
        self.__dict__.update(kwargs)


class MechanismContractTests(ScratchRepo):
    def test_complete_names_every_survivor_missing_a_mechanism(self) -> None:
        self.commit("scripts/a.py")
        self.plan(tier="fast")
        missing = [{"anchor": "a.py:1", "scenario": "a"},
                   {"anchor": "b.py:2", "scenario": "b", "mechanism": "   "},
                   {"anchor": "c.py:3", "scenario": "c", "mechanism": []},
                   {"anchor": "d.py:4", "scenario": "d", "mechanism": 0},
                   {"anchor": "e.py:5", "scenario": "e", "mechanism": ["x"]},
                   {"anchor": "f.py:6", "scenario": "f", "mechanism": 1}]
        with self.assertRaises(SystemExit) as refused:
            self.complete(missing, not_triggered=True)
        self.assertIn("a.py:1", str(refused.exception))
        self.assertIn("b.py:2", str(refused.exception))
        self.assertIn("c.py:3", str(refused.exception))
        self.assertIn("d.py:4", str(refused.exception))
        self.assertIn("e.py:5", str(refused.exception))
        self.assertIn("f.py:6", str(refused.exception))

    def test_dismissed_finding_without_a_mechanism_is_accepted(self) -> None:
        self.commit("scripts/a.py")
        self.plan(tier="fast")
        dismissed = {"anchor": "a.py:1", "scenario": "a", "dismissed": "not in diff"}
        self.complete([dismissed])

    def test_gate_does_not_require_a_mechanism(self) -> None:
        path = self.findings_file([{"anchor": "a.py:1", "scenario": "a"}])
        preclose.gate(_Args(repo="vitalharmony/hrse", issue=1208, findings=path,
                            cross_family=False, base="base", head="HEAD"))


class PanelSizingTests(unittest.TestCase):
    def test_hook_implementation_is_high_blast(self) -> None:
        self.assertTrue(preclose.blast_radius(["tools/hooks/block_irreversible_ops.py"]))

    def test_hook_wiring_is_high_blast(self) -> None:
        """The motivating incident edits wiring, not implementation."""
        for path in (".claude/settings.json", ".claude/settings.local.json",
                     ".codex/hooks.json", "agents/preclose-inspection.md",
                     ".githooks/pre-commit", "scripts/gate_codex_tool.py"):
            self.assertTrue(preclose.blast_radius([path]), path)

    def test_high_blast_beats_tier(self) -> None:
        size, why = preclose.panel_size(preclose.blast_radius(["tools/hooks/x.py"]), "fast")
        self.assertEqual(size, len(preclose.LENSES))
        self.assertIn("blast radius", why)

    def test_ordinary_tooling_is_not_high_blast(self) -> None:
        self.assertFalse(preclose.blast_radius(["docs/ONTOLOGY.md", "scripts/1-one-off.py"]))

    def test_tier_scales_an_ordinary_change(self) -> None:
        self.assertEqual(preclose.panel_size([], "fast")[0], 1)
        self.assertEqual(preclose.panel_size([], "standard")[0], 3)
        self.assertEqual(preclose.panel_size([], "deep")[0], 5)

    def test_unset_tier_defaults_to_standard_not_to_skipping(self) -> None:
        size, why = preclose.panel_size([], None)
        self.assertEqual(size, 3)
        self.assertIn("unset", why)

    def test_lenses_are_distinct(self) -> None:
        self.assertEqual(len(set(preclose.LENSES)), len(preclose.LENSES))


class RepoNormalizationTests(unittest.TestCase):
    def test_spellings_collapse_to_one_key(self) -> None:
        """Otherwise every spelling mints its own receipt and re-arms one-pass."""
        for spelling in ("vitalharmony/hrse", "vitalharmony/HRSE",
                         "https://github.com/vitalharmony/hrse",
                         "https://github.com/vitalharmony/hrse.git",
                         "git@github.com:vitalharmony/hrse.git"):
            self.assertEqual(preclose.normalize_repo(spelling), "vitalharmony/hrse", spelling)

    def test_a_bare_name_is_refused(self) -> None:
        with self.assertRaises(SystemExit):
            preclose.normalize_repo("hrse")


class RequiredRepoFlagTests(unittest.TestCase):
    """harmonic-forge#704 preclose finding: every other test bypasses argparse
    by constructing _Args directly, so nothing asserted --repo is required --
    reverting the default entirely left all of them green."""

    def test_missing_repo_is_an_argument_error_not_a_default(self) -> None:
        result = subprocess.run(
            (sys.executable, str(ROOT / "preclose_check.py"), "--issue", "1"),
            text=True, capture_output=True, check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--repo", result.stderr)
        self.assertIn("required", result.stderr.lower())


class DiffReadingTests(ScratchRepo):
    def test_rename_out_of_a_high_blast_directory_is_still_seen(self) -> None:
        """Removing a guard from tools/hooks/ must still read as high blast."""
        self.commit("tools/hooks/guard.py")
        git("branch", "-f", "base", "HEAD", cwd=self.repo)
        git("mv", "tools/hooks/guard.py", "attic_guard.py", cwd=self.repo)
        git("commit", "-qm", "move", cwd=self.repo)
        files = preclose.changed_files("base", "HEAD")
        self.assertIn("tools/hooks/guard.py", files)
        self.assertTrue(preclose.blast_radius(files))

    def test_non_ascii_path_is_not_returned_quoted(self) -> None:
        self.commit("tools/hooks/café.py")
        files = preclose.changed_files("base", "HEAD")
        self.assertIn("tools/hooks/café.py", files)
        self.assertTrue(preclose.blast_radius(files))

    def test_untracked_high_blast_file_blocks_planning(self) -> None:
        self.commit("scripts/ordinary.py")
        (self.repo / "tools" / "hooks").mkdir(parents=True, exist_ok=True)
        (self.repo / "tools" / "hooks" / "sneaky.py").write_text("x\n")
        with self.assertRaises(SystemExit) as caught:
            self.plan(tier="fast")
        self.assertIn("uncommitted", str(caught.exception))

    def test_allow_dirty_proceeds_on_the_committed_diff(self) -> None:
        self.commit("scripts/ordinary.py")
        (self.repo / "stray.txt").write_text("x\n")
        self.assertIn("refuters:", self.plan(tier="fast", allow_dirty=True))


class RepoMismatchTests(ScratchRepo):
    def test_repo_flag_must_match_the_checkout(self) -> None:
        self.commit("scripts/ordinary.py")
        with self.assertRaises(SystemExit) as caught:
            self.plan(repo="vitalharmony/harmonic-forge", tier="fast")
        self.assertIn("origin", str(caught.exception))

    def test_mismatch_is_overridable_deliberately(self) -> None:
        self.commit("scripts/ordinary.py")
        self.assertIn("refuters:", self.plan(repo="vitalharmony/harmonic-forge",
                                             tier="fast", allow_repo_mismatch=True))

    def test_complete_also_refuses_a_mismatched_origin(self) -> None:
        """harmonic-forge#704 preclose finding: complete() originally had no
        origin check at all, so running the printed --complete command from
        the wrong checkout minted a receipt for a repo that was never
        reviewed, leaving the actually-reviewed repo's one-pass rule
        silently disarmed."""
        self.commit("scripts/ordinary.py")
        with self.assertRaises(SystemExit) as caught:
            preclose.complete(_Args(repo="vitalharmony/harmonic-forge", issue=1208, head="HEAD"))
        self.assertIn("origin", str(caught.exception))

    def test_complete_also_refuses_an_unresolvable_head(self) -> None:
        """harmonic-forge#704 preclose finding: an unresolvable --head must
        not mint a 'status: complete' receipt with an empty reviewed_sha
        that no later check_one_pass call could ever match."""
        self.commit("scripts/ordinary.py")
        with self.assertRaises(SystemExit):
            preclose.complete(_Args(repo="vitalharmony/hrse", issue=1208, head="not-a-real-ref"))

    def test_no_origin_remote_is_refused_not_treated_as_no_objection(self) -> None:
        """harmonic-forge#704 preclose finding: `if actual and ...` skipped the
        whole guard when origin_repo() returned None (a fork clone using
        `upstream`, a worktree off a bare/mirror clone, a renamed-remote CI
        checkout) -- unreachable while --repo defaulted to vitalharmony/hrse
        and the script lived only inside HRSE2, but AC4's required --repo
        plus one platform copy invoked from arbitrary repos makes this
        ordinary traffic. Cannot-decide must refuse, not pass."""
        self.commit("scripts/ordinary.py")
        git("remote", "remove", "origin", cwd=self.repo)
        git("remote", "add", "upstream", "https://github.com/vitalharmony/cymagraph-infra.git", cwd=self.repo)
        with self.assertRaises(SystemExit) as caught:
            self.plan(tier="fast")
        self.assertIn("no `origin` remote", str(caught.exception))

    def test_no_origin_remote_is_overridable_deliberately(self) -> None:
        self.commit("scripts/ordinary.py")
        git("remote", "remove", "origin", cwd=self.repo)
        self.assertIn("refuters:", self.plan(tier="fast", allow_repo_mismatch=True))

    def test_unlisted_repo_is_refused_by_the_manifest(self) -> None:
        self.commit("scripts/ordinary.py")
        with self.assertRaises(SystemExit) as caught:
            self.plan(repo="example/unlisted", tier="fast", allow_repo_mismatch=True)
        self.assertIn("projects.toml", str(caught.exception))

    def test_not_onboarded_repo_is_refused_by_the_manifest(self) -> None:
        self.commit("scripts/ordinary.py")
        manifest = self.repo / "projects.toml"
        manifest.write_text("""
[[project]]
name = "future"
prefix = "X"
repo = "example/future"
account = "vitalharmony"
onboarded = false
[project.protocol]
worktree_name = "{checkout}-lane{lane}"
l1_post_task = "l1-post"
lane_comment_task = "lane-comment"
gate_checkout_task = "gate-checkout"
lane3_begin_task = "lane3-begin"
lane3_end_task = "lane3-end"
gate_task = "check"
runs_lane3 = true
""")
        with patch.dict(os.environ, {"FORGE_PROJECTS_MANIFEST": str(manifest)}):
            with self.assertRaises(SystemExit) as caught:
                self.plan(repo="example/future", tier="fast", allow_repo_mismatch=True)
        self.assertIn("onboarded = false", str(caught.exception))

    def test_complete_also_refuses_a_not_onboarded_repo(self) -> None:
        self.commit("scripts/ordinary.py")
        manifest = self.repo / "projects.toml"
        manifest.write_text("""
[[project]]
name = "future"
prefix = "X"
repo = "example/future"
account = "vitalharmony"
onboarded = false
[project.protocol]
worktree_name = "{checkout}-lane{lane}"
l1_post_task = "l1-post"
lane_comment_task = "lane-comment"
gate_checkout_task = "gate-checkout"
lane3_begin_task = "lane3-begin"
lane3_end_task = "lane3-end"
gate_task = "check"
runs_lane3 = true
""")
        args = _Args(repo="example/future", issue=1208, head="HEAD",
                     allow_repo_mismatch=True)
        with patch.dict(os.environ, {"FORGE_PROJECTS_MANIFEST": str(manifest)}):
            with self.assertRaises(SystemExit) as caught:
                preclose.complete(args)
        self.assertIn("onboarded = false", str(caught.exception))


class OnePassTests(ScratchRepo):
    """Deliberately NOT patching `receipt_dir()`/`Path.home()` directly --
    only `HOME` (via `ScratchRepo.setUp`) -- so the real function runs and
    proves what it actually does. Patching it to a fixed tmpdir is what hid
    the cwd-dependence the original panel found; harmonic-forge#778 moved
    the receipt from repo-anchored to user-level for exactly that reason."""

    def head(self) -> str:
        return subprocess.run(("git", "rev-parse", "HEAD"), cwd=self.repo,
                              capture_output=True, text=True).stdout.strip()

    def test_planning_alone_does_not_consume_the_pass(self) -> None:
        """An interrupted session must not record a review that never ran."""
        self.commit("scripts/ordinary.py")
        self.plan(tier="fast")
        self.assertIn("refuters:", self.plan(tier="fast"))

    def test_a_completed_pass_on_the_same_diff_is_refused(self) -> None:
        self.commit("scripts/ordinary.py")
        self.plan(tier="fast")
        self.complete()
        with self.assertRaises(SystemExit) as caught:
            self.plan(tier="fast")
        self.assertIn("escalate", str(caught.exception).lower())

    def test_a_revised_diff_re_arms_the_check(self) -> None:
        """The correct response to a finding must not be gated behind --force."""
        self.commit("scripts/ordinary.py")
        self.plan(tier="fast")
        self.complete()
        self.commit("scripts/revised.py")
        self.assertIn("refuters:", self.plan(tier="fast"))

    def test_receipt_survives_a_change_of_working_directory(self) -> None:
        """Reproduced by four refuters: `cd scripts` defeated the whole guard."""
        self.commit("scripts/ordinary.py")
        self.plan(tier="fast")
        self.complete()
        subdir = self.repo / "scripts"
        os.chdir(subdir)
        with self.assertRaises(SystemExit):
            self.plan(tier="fast")

    def test_force_overrides_a_completed_receipt(self) -> None:
        self.commit("scripts/ordinary.py")
        self.plan(tier="fast")
        self.complete()
        self.assertIn("refuters:", self.plan(tier="fast", force=True))

    def test_a_corrupt_receipt_re_reviews_rather_than_crashing_or_refusing(self) -> None:
        self.commit("scripts/ordinary.py")
        self.plan(tier="fast")
        preclose.receipt_path("vitalharmony/hrse", 1208).write_text("")
        self.assertIn("refuters:", self.plan(tier="fast"))

    def test_receipt_is_written_atomically_with_no_temp_left_behind(self) -> None:
        self.commit("scripts/ordinary.py")
        self.plan(tier="fast")
        self.assertEqual(list(preclose.receipt_dir().glob("*.tmp")), [])

    def _block_receipt_dir(self) -> Path:
        """F783: make the real receipt_dir() unwritable by occupying its
        parent with a file -- HOME only, never patching receipt_dir() (see
        this class's docstring)."""
        state = Path(os.environ["HOME"]) / ".claude" / "state"
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text("blocked\n")
        return preclose.receipt_dir()

    def test_complete_fails_fast_when_receipt_dir_is_unwritable(self) -> None:
        """F783: an inaccessible receipt store must fail before any work."""
        blocked = self._block_receipt_dir()
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as caught:
            self.complete()
        self.assertEqual(caught.exception.code, 2)
        self.assertIn(str(blocked), err.getvalue())
        self.assertIn("AGENT_LANE_ADD_DIR", err.getvalue())

    @unittest.skipIf(os.geteuid() == 0, "root ignores directory mode bits")
    def test_complete_on_a_present_but_write_denied_dir_leaves_nothing(self) -> None:
        """F783 preclose finding: the Codex sandbox case is a directory that
        exists but refuses writes (EACCES/EROFS), not a missing one. The probe
        must catch it and leave no file of any name behind."""
        store = preclose.receipt_dir()
        store.mkdir(parents=True)
        store.chmod(0o500)
        self.addCleanup(store.chmod, 0o700)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
            self.complete()
        self.assertEqual(caught.exception.code, 2)
        self.assertEqual(list(store.iterdir()), [])

    def test_plan_refuses_before_printing_a_panel(self) -> None:
        """F783 preclose finding (4 of 5 refuters): plan is the first step,
        and its stdout is the instruction to spawn the panel."""
        self.commit("scripts/ordinary.py")
        self._block_receipt_dir()
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()), \
                self.assertRaises(SystemExit) as caught:
            preclose.plan(_Args(repo="vitalharmony/hrse", issue=1208, base="base", head="HEAD",
                                tier="fast", force=False, allow_dirty=False))
        self.assertEqual(caught.exception.code, 2)
        self.assertEqual(out.getvalue(), "")

    def test_gate_fails_before_processing_when_receipt_dir_is_unwritable(self) -> None:
        """F783: --gate refuses before reading findings or calling a reviewer
        (a missing findings file would otherwise be the first error)."""
        blocked = self._block_receipt_dir()
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as caught:
            preclose.gate(_Args(repo="not/onboarded", issue=1208, findings="missing.json"))
        self.assertEqual(caught.exception.code, 2)
        self.assertIn(str(blocked), err.getvalue())

    def test_writes_go_to_the_user_level_store_not_the_repo(self) -> None:
        """harmonic-forge#778 AC3: the receipt must outlive the worktree that
        wrote it, so it has to live somewhere a disposable Lane 1 impl
        worktree's own deletion can't take it with it."""
        self.commit("scripts/ordinary.py")
        self.plan(tier="fast")
        self.complete()
        self.assertTrue(
            str(preclose.receipt_dir()).startswith(str(Path(self.home_tmp.name))),
            f"receipt_dir() must resolve under HOME, got {preclose.receipt_dir()}")
        self.assertFalse(
            (self.repo / ".claude" / "cache" / "preclose").exists(),
            "nothing should write to the old repo-anchored location any more")

    def test_a_legacy_repo_anchored_receipt_is_still_found(self) -> None:
        """One release's fallback (harmonic-forge#778): a receipt written
        just before this change, under the old repo-anchored path, must
        still be found -- never a second write target, only a read
        fallback."""
        self.commit("scripts/ordinary.py")
        legacy_dir = preclose.legacy_receipt_dir()
        legacy_dir.mkdir(parents=True, exist_ok=True)
        legacy_path = legacy_dir / "vitalharmony_hrse_1208.json"
        legacy_path.write_text(json.dumps({
            "repo": "vitalharmony/hrse", "issue": 1208,
            "reviewed_sha": self.head(), "refuters": 3, "status": "complete",
        }))
        found = preclose.find_receipt("vitalharmony/hrse", 1208)
        self.assertIsNotNone(found)
        self.assertEqual(found["reviewed_sha"], self.head())

    def test_a_user_level_receipt_is_preferred_over_a_legacy_one(self) -> None:
        self.commit("scripts/ordinary.py")
        legacy_dir = preclose.legacy_receipt_dir()
        legacy_dir.mkdir(parents=True, exist_ok=True)
        (legacy_dir / "vitalharmony_hrse_1208.json").write_text(json.dumps({
            "reviewed_sha": "old-sha", "refuters": 1, "status": "complete"}))
        preclose.receipt_dir().mkdir(parents=True, exist_ok=True)
        preclose.receipt_path("vitalharmony/hrse", 1208).write_text(json.dumps({
            "reviewed_sha": "new-sha", "refuters": 2, "status": "complete"}))
        found = preclose.find_receipt("vitalharmony/hrse", 1208)
        self.assertEqual(found["reviewed_sha"], "new-sha")

    def test_completion_records_the_reviewed_sha_and_status(self) -> None:
        self.commit("scripts/ordinary.py")
        self.plan(tier="fast")
        self.complete()
        stored = json.loads(preclose.receipt_path("vitalharmony/hrse", 1208).read_text())
        self.assertEqual(stored["status"], "complete")
        self.assertEqual(stored["reviewed_sha"], self.head())


class OutputInvariantTests(ScratchRepo):
    """Assert on real captured stdout. The first version grepped source text
    with a startswith() heuristic that an f-string slipped straight past."""

    def test_no_pass_verdict_in_plan_output(self) -> None:
        self.commit("scripts/ordinary.py")
        self.assertNotRegex(self.plan(tier="fast"), r"\bPASS\b")

    def test_no_pass_verdict_in_completion_output(self) -> None:
        self.commit("scripts/ordinary.py")
        self.plan(tier="fast")
        out = self.complete()
        self.assertTrue(out, "the completion output must be captured, or this test is vacuous")
        self.assertNotRegex(out, r"\bPASS\b")

    def test_plan_output_states_it_is_not_a_gate(self) -> None:
        """The half of the AC that carries the design decision."""
        self.commit("scripts/ordinary.py")
        self.assertIn("not a Lane 3 gate", self.plan(tier="fast"))

    def test_completion_output_states_it_is_not_a_gate(self) -> None:
        self.commit("scripts/ordinary.py")
        self.plan(tier="fast")
        self.assertIn("not a Lane 3 gate", self.complete())



class CrossFamilyGateTests(unittest.TestCase):
    """harmonic-forge#701: the gate is evaluated after the panel, and silence
    triggers while findings do not."""

    def test_zero_survivors_triggers(self) -> None:
        required, why = preclose.cross_family_gate(0, [], False)
        self.assertTrue(required)
        self.assertIn("silence", why)

    def test_survivors_present_do_not_trigger(self) -> None:
        self.assertFalse(preclose.cross_family_gate(2, [], False)[0])

    def test_blast_radius_triggers_regardless_of_survivors(self) -> None:
        reasons = preclose.blast_radius(["tools/hooks/x.py"])
        self.assertTrue(preclose.cross_family_gate(5, reasons, False)[0])

    def test_operator_request_triggers(self) -> None:
        self.assertTrue(preclose.cross_family_gate(5, [], True)[0])

    def test_tier_is_not_an_input(self) -> None:
        """The issue forbids Tier as the gate (harmonic-forge#257)."""
        import inspect
        self.assertNotIn("tier", inspect.signature(preclose.cross_family_gate).parameters)

    def test_ten_unanchored_findings_are_zero_survivors_and_trigger(self) -> None:
        """AC4: survivors, not generations."""
        guesses = [{"anchor": "somewhere in the hook", "scenario": "might break"}] * 5 + \
                  [{"anchor": "scripts/x.py:3", "scenario": ""}] * 5
        survivors = preclose.surviving_findings(guesses)
        self.assertEqual(survivors, [])
        self.assertTrue(preclose.cross_family_gate(len(survivors), [], False)[0])

    def test_an_anchored_finding_with_a_scenario_survives(self) -> None:
        self.assertEqual(len(preclose.surviving_findings([ANCHORED, {"anchor": "a.py:1-4", "scenario": "s"}])), 2)


class CrossFamilyReceiptTests(ScratchRepo):
    """AC3/AC5/AC6 on the real entry points."""

    def test_one_pass_with_both_halves_writes_one_receipt_against_one_sha(self) -> None:
        self.commit("scripts/tool.py")
        self.plan()
        self.complete([])                                  # silent -> cross-family taken
        receipts = list(preclose.receipt_dir().glob("*.json"))
        self.assertEqual(len(receipts), 1)
        stored = json.loads(receipts[0].read_text())
        head = subprocess.run(("git", "rev-parse", "HEAD"), cwd=self.repo, text=True,
                              capture_output=True).stdout.strip()
        self.assertEqual(stored["reviewed_sha"], head)
        self.assertTrue(stored["cross_family_required"])
        self.assertIn("cross-family (", stored["provenance"])
        with self.assertRaises(SystemExit):                # still ONE pass per diff
            self.plan()

    def test_not_triggered_path_still_records_provenance(self) -> None:
        self.commit("scripts/tool.py")
        self.plan()
        self.complete([ANCHORED], not_triggered=True)
        stored = json.loads(next(preclose.receipt_dir().glob("*.json")).read_text())
        self.assertFalse(stored["cross_family_required"])
        self.assertEqual(stored["provenance"], NOT_TRIGGERED)

    def test_required_branch_refuses_a_same_family_label(self) -> None:
        """AC5: a same-family pass cannot be recorded as two-family, or vice versa."""
        self.commit("scripts/tool.py")
        self.plan()
        with self.assertRaises(SystemExit) as caught:
            self.complete([], not_triggered=True)
        self.assertIn("required", str(caught.exception))

    def test_not_triggered_refuses_a_cross_family_label(self) -> None:
        self.commit("scripts/tool.py")
        self.plan()
        with self.assertRaises(SystemExit):
            self.complete([ANCHORED])

    def test_receipt_lookup_is_case_insensitive_to_the_repo_slug(self) -> None:
        """Preclose finding: `receipt_path()` keyed the filesystem name on the
        `repo` string verbatim. `preclose_check.py --repo` always goes
        through the manifest's fixed casing, but a merge-time reader can
        resolve `repo` from a raw `--repo` flag or `gh repo view`'s own
        casing -- a mismatch there silently keyed two different receipt
        files for the same repo, producing a permanent deny loop."""
        self.commit("scripts/tool.py")
        self.plan()
        self.complete([])
        self.assertIsNotNone(preclose.find_receipt("VitalHarmony/HRSE", 1208))
        self.assertIsNotNone(preclose.find_receipt("vitalharmony/hrse", 1208))

    def test_unresolvable_fake_sha_is_refused_not_written(self) -> None:
        """Preclose finding (chief): a string that merely LOOKS like a full
        40-hex SHA is a valid `git rev-parse` argument -- it is echoed back
        verbatim with exit 0 even when no such object exists. Without a
        real existence check, `--complete --head <fake sha>` minted a
        'complete' receipt for a commit that was never read, authorizing a
        merge with zero refuters and no diff review."""
        self.commit("scripts/tool.py")
        fake_sha = "d" * 40
        with self.assertRaises(SystemExit) as caught:
            preclose._require_repo_and_head(
                "vitalharmony/hrse",
                _Args(repo="vitalharmony/hrse", issue=1208, base="base", head=fake_sha,
                      tier=None, force=False, allow_dirty=False, allow_repo_mismatch=False))
        self.assertIn("does not resolve to a commit", str(caught.exception))

    def test_fallback_label_is_accepted_when_the_call_could_not_run(self) -> None:
        """AC6: loud, non-fatal, never relabelled."""
        self.commit("scripts/tool.py")
        self.plan()
        out = self.complete([], envelope_label=FALLBACK)
        self.assertIn("in-family fallback", out)

    def test_success_envelope_without_native_execution_trace_is_refused(self) -> None:
        path = self.repo / "forged-envelope.json"
        path.write_text(json.dumps({"status": "ok", "family": "codex", "posture": "verify",
                                    "exit_code": 0,
                                    "report": {"assumptions": [{"verdict": "confirmed", "evidence": "invented"}]}}))
        with self.assertRaises(SystemExit):
            preclose.require_recorded_envelope(str(path))

    def test_claude_verify_requires_exact_init_and_executed_read(self) -> None:
        path = self.repo / "claude-envelope.json"
        envelope = {"family": "claude", "caller_family": "codex", "target_family": "claude",
                    "verify_model": "claude-opus-5-5", "posture": "verify", "status": "ok", "exit_code": 0,
                    "report": {"assumptions": [{"verdict": "confirmed", "evidence": "Read x"}]},
                    "native": [
                        {"type": "system", "subtype": "init", "tools": ["Glob", "Grep", "Read"], "mcp_servers": [], "model": "claude-opus-5-5"},
                        {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "read-1", "name": "Read"}]}},
                        {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "read-1"}]}},
                        {"type": "result", "subtype": "success", "result": "{}"},
                    ]}
        path.write_text(json.dumps(envelope))
        preclose.require_recorded_envelope(str(path))
        envelope["native"][0]["tools"].append("Bash")
        path.write_text(json.dumps(envelope))
        with self.assertRaises(SystemExit):
            preclose.require_recorded_envelope(str(path))

    def test_same_family_envelope_is_refused(self) -> None:
        path = self.repo / "same-family-envelope.json"
        path.write_text(json.dumps({"status": "ok", "family": "codex", "caller_family": "codex",
                                    "target_family": "codex", "posture": "verify", "exit_code": 0,
                                    "report": {"assumptions": [{"verdict": "confirmed"}]},
                                    "native": [{"type": "thread.started"}, {"type": "item.completed", "item": {"type": "agent_message"}}]}))
        with self.assertRaises(SystemExit):
            preclose.require_recorded_envelope(str(path))

    def test_high_blast_diff_requires_the_branch_even_with_survivors(self) -> None:
        self.commit("tools/hooks/guard.py")
        self.plan()
        with self.assertRaises(SystemExit):
            self.complete([ANCHORED], not_triggered=True)

    def test_complete_without_findings_or_provenance_is_refused(self) -> None:
        self.commit("scripts/tool.py")
        self.plan()
        with self.assertRaises(SystemExit):
            preclose.complete(_Args(repo="vitalharmony/hrse", issue=1208, base="base", head="HEAD",
                                    findings=None, envelope=None, not_triggered=False,
                                    cross_family=False, force=False))

    def test_the_recorded_label_is_the_tools_output_not_typed(self) -> None:
        """Preclose finding B: there is no flag to type a label; what is recorded
        is exactly what the provenance tool printed for the envelope."""
        self.commit("scripts/tool.py")
        self.plan()
        self.complete([], envelope_label=CROSS + " [from envelope]")
        stored = json.loads(next(preclose.receipt_dir().glob("*.json")).read_text())
        self.assertEqual(stored["provenance"], CROSS + " [from envelope]")
        self.assertFalse(hasattr(preclose, "PROVENANCE_FLAG"))
        import argparse, inspect
        self.assertNotIn("--provenance", inspect.getsource(preclose.main))

    def test_an_envelope_that_classifies_as_in_family_only_cannot_satisfy_a_required_branch(self) -> None:
        self.commit("scripts/tool.py")
        self.plan()
        with self.assertRaises(SystemExit):
            self.complete([], envelope_label=NOT_TRIGGERED)

    def test_a_second_complete_on_the_same_sha_cannot_relabel_the_pass(self) -> None:
        """Preclose finding C."""
        self.commit("scripts/tool.py")
        self.plan()
        self.complete([])
        with self.assertRaises(SystemExit) as caught:
            self.complete([ANCHORED], not_triggered=True)
        self.assertIn("already covers", str(caught.exception))

    def test_dismissed_findings_are_not_survivors(self) -> None:
        """Preclose finding A: a pass that dismissed every finding is silence."""
        dismissed = dict(ANCHORED, dismissed="predates this change")
        self.assertEqual(preclose.surviving_findings([dismissed] * 3), [])
        self.commit("scripts/tool.py")
        self.plan()
        with self.assertRaises(SystemExit):              # required, so not-triggered is refused
            self.complete([dismissed], not_triggered=True)


class OwnModelCallSiteTests(unittest.TestCase):
    """harmonic-forge#848 preclose finding 5: `--own-model` reaches
    `compute_provenance` from both entry points, not just from the helper."""

    class _Stop(Exception):
        pass

    def _capture(self) -> MagicMock:
        return MagicMock(side_effect=self._Stop)

    def test_complete_forwards_own_model(self) -> None:
        fake = self._capture()
        args = argparse.Namespace(repo="vitalharmony/harmonic-forge", issue=848, findings="f.json",
                                  base="origin/main", head="HEAD", force=False, envelope=None,
                                  not_triggered=True, own_model="gpt-6-sol",
                                  panel_tokens="1", panel_ms="1")
        # receipt_lock is stubbed: the real one flocks the shared per-issue
        # lock, which a concurrent kill-check run on this issue holds.
        with patch.object(preclose, "require_writable"), \
             patch.object(preclose, "receipt_lock", return_value=contextlib.nullcontext()), \
             patch.object(preclose, "registered_repo", return_value=args.repo), \
             patch.object(preclose, "_require_repo_and_head", return_value="a" * 40), \
             patch.object(preclose, "local_patch_id", return_value="p"), \
             patch.object(preclose, "check_pass_cap"), \
             patch.object(preclose, "load_findings", return_value=[]), \
             patch.object(preclose, "require_mechanisms"), \
             patch.object(preclose, "gate_decision", return_value=(False, "", 0, None)), \
             patch.object(preclose, "compute_provenance", fake), \
             self.assertRaises(self._Stop):
            preclose.complete(args)
        self.assertEqual(fake.call_args.args[2], "gpt-6-sol")

    def test_post_verdict_forwards_own_model(self) -> None:
        fake = self._capture()
        args = argparse.Namespace(repo="vitalharmony/harmonic-forge", issue=848, base="b",
                                  head="HEAD", main="origin/main", envelope="e.jsonl",
                                  own_model="claude-opus-5-5")
        passes = preclose.preclose_passes
        with patch.object(preclose, "require_writable"), \
             patch.object(preclose, "receipt_lock", return_value=contextlib.nullcontext()), \
             patch.object(preclose, "registered_repo", return_value=args.repo), \
             patch.object(preclose, "_require_repo_and_head", return_value="a" * 40), \
             patch.object(preclose, "run", return_value=MagicMock(stdout="b" * 40)), \
             patch.object(preclose, "find_receipt", return_value={}), \
             patch.object(passes, "current", return_value=[{}] * passes.MAX_PASSES), \
             patch.object(passes, "cap_message", return_value=passes.STICKY_WICKET), \
             patch.object(passes, "reviewed_head", return_value="b" * 40), \
             patch.object(passes, "reviewed_patch_id", return_value="p"), \
             patch.object(preclose, "local_patch_id", return_value="p"), \
             patch.object(preclose, "require_recorded_envelope"), \
             patch.object(preclose, "compute_provenance", fake), \
             self.assertRaises(self._Stop):
            preclose.post_verdict(args)
        self.assertEqual(fake.call_args.args[2], "claude-opus-5-5")



class OwnModelTests(unittest.TestCase):
    """harmonic-forge#848 AC8: the calling session's model reaches the label."""

    def test_compute_provenance_forwards_own_model(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tool = Path(tmp) / "prov.py"
            tool.write_text("import sys\nprint(' '.join(sys.argv[1:]))\n")
            with patch.object(preclose, "PROVENANCE_TOOL", tool):
                label = preclose.compute_provenance(None, True, "gpt-6-sol")
        self.assertIn("--own-model gpt-6-sol", label)

    def test_compute_provenance_omits_own_model_when_absent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tool = Path(tmp) / "prov.py"
            tool.write_text("import sys\nprint(' '.join(sys.argv[1:]) or 'x')\n")
            with patch.object(preclose, "PROVENANCE_TOOL", tool):
                label = preclose.compute_provenance(None, True)
        self.assertNotIn("--own-model", label)

    def test_not_triggered_without_own_model_is_a_parser_error(self) -> None:
        """Preclose finding 2: the label names the session's family on the
        not-triggered branch too, so --own-model is required there as well."""
        argv = ["preclose_check.py", "--repo", "vitalharmony/harmonic-forge", "--issue", "848",
                "--complete", "--findings", "/dev/null", "--not-triggered"]
        # complete() is stubbed so a parser that wrongly lets this through
        # fails fast here instead of running a real pass.
        with patch.object(sys, "argv", argv), \
             patch.object(preclose, "complete", return_value=0), \
             patch("sys.stderr", new_callable=io.StringIO) as err, \
             self.assertRaises(SystemExit) as done:
            preclose.main()
        self.assertEqual(done.exception.code, 2)
        self.assertIn("--own-model", err.getvalue())

    @staticmethod
    def _parses(command: str) -> int:
        """Feed a printed preclose_check.py command through main()'s own parser
        with every `<placeholder>` filled in; complete/post_verdict/cluster_verdict are stubbed,
        so the exit code is the parser's verdict alone."""
        import re as _re
        import shlex
        filled = _re.sub(r"<[^>]*>", "X", command)
        tokens = shlex.split(filled)
        argv = ["preclose_check.py"] + tokens[tokens.index(next(t for t in tokens
                                                                if t.endswith("preclose_check.py"))) + 1:]
        with patch.object(sys, "argv", argv), \
             patch.object(preclose, "complete", return_value=0), \
             patch.object(preclose, "post_verdict", return_value=0), \
             patch.object(preclose, "cluster_verdict", return_value=0), \
             patch("sys.stderr", new_callable=io.StringIO), \
             patch("sys.stdout", new_callable=io.StringIO):
            try:
                preclose.main()
            except SystemExit as done:
                return done.code or 0
        return 0

    def test_gate_hint_carries_own_model(self) -> None:
        """Pass-2 survivor 5: the command --gate actually PRINTS must parse --
        captured from stdout, never read from the source text."""
        for required in (True, False):
            with self.subTest(required=required):
                out = io.StringIO()
                with patch.object(preclose, "require_writable"), \
                     patch.object(preclose, "registered_repo", return_value="vitalharmony/harmonic-forge"), \
                     patch.object(preclose, "gate_decision", return_value=(required, "why", 1, [])), \
                     patch("sys.stdout", out):
                    preclose.gate(_Args(repo="vitalharmony/harmonic-forge", issue=848,
                                        findings="f.json", cross_family=False))
                line = next(l for l in out.getvalue().splitlines() if "--complete" in l)
                self.assertIn("--own-model", line)
                self.assertEqual(self._parses(line), 0, line)

    def test_post_verdict_hint_parses(self) -> None:
        """Pass-2 survivor 4: the sticky-wicket remediation command must run."""
        import preclose_passes
        text = preclose_passes.POST_VERDICT_REQUIRED.format(repo="vitalharmony/harmonic-forge", issue=848)
        # Parsed exactly as printed: the hint must name --repo and --issue itself.
        command = text[text.index("preclose_check.py --repo"):text.index(". It never")]
        self.assertEqual(self._parses("python3 " + command), 0, command)

    def test_every_command_hint_parses_as_printed(self) -> None:
        """harmonic-forge#852 preclose: every runnable hint, not only the one
        a test happened to cover, parses exactly as printed; and no other
        module-level string in preclose_passes carries a runnable command."""
        import preclose_passes
        for hint in preclose_passes.COMMAND_HINTS:
            text = hint.format(repo="vitalharmony/harmonic-forge", issue=848, mechanisms="m")
            start = text.index("preclose_check.py --")
            end = min(i for i in (text.find(" (", start), text.find(". ", start)) if i != -1)
            with self.subTest(hint=text[:60]):
                self.assertEqual(self._parses("python3 " + text[start:end]), 0, text[start:end])
        # Scope: module-level constants of preclose_passes only. An inline
        # f-string hint built elsewhere (e.g. preclose_check.py's own prints)
        # is not seen here; those are covered by their own printed-output
        # tests (test_gate_hint_carries_own_model). Known gap, not widened in #852.
        strays = [name for name, value in vars(preclose_passes).items()
                  if isinstance(value, str) and "preclose_check.py --" in value
                  and value not in preclose_passes.COMMAND_HINTS]
        self.assertEqual(strays, [])

    def test_envelope_without_own_model_is_a_parser_error(self) -> None:
        argv = ["preclose_check.py", "--repo", "vitalharmony/harmonic-forge", "--issue", "848",
                "--complete", "--findings", "/dev/null", "--envelope", "/dev/null"]
        with patch.object(sys, "argv", argv), \
             patch.object(preclose, "complete", return_value=0), \
             patch("sys.stderr", new_callable=io.StringIO) as err, \
             self.assertRaises(SystemExit) as done:
            preclose.main()
        self.assertEqual(done.exception.code, 2)
        self.assertIn("--own-model", err.getvalue())

if __name__ == "__main__":
    unittest.main()
