#!/usr/bin/env python3
"""Tests for the pre-close check planner (hrse#1208).

Several of these exist because the pre-close panel found the first version's
tests asserting the wrong properties -- notably a no-PASS check that grepped
source text with a syntactic heuristic and could not see an f-string, and a
one-pass suite that patched away the very cwd-dependence that made the guard
evadable. Both now run the real entry point and assert on real output.
"""

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
from unittest.mock import patch

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

    def commit(self, relpath: str, body: str = "x\n") -> None:
        target = self.repo / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body)
        git("add", "-A", cwd=self.repo)
        git("commit", "-qm", f"add {relpath}", cwd=self.repo)

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
        self.__dict__.update(kwargs)


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


class OnePassTests(ScratchRepo):
    """Deliberately NOT patching the receipt location -- patching it to a fixed
    tmpdir is what hid the cwd-dependence the panel found."""

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
        preclose.complete(_Args(repo="vitalharmony/hrse", issue=1208, head="HEAD"))
        with self.assertRaises(SystemExit) as caught:
            self.plan(tier="fast")
        self.assertIn("escalate", str(caught.exception).lower())

    def test_a_revised_diff_re_arms_the_check(self) -> None:
        """The correct response to a finding must not be gated behind --force."""
        self.commit("scripts/ordinary.py")
        self.plan(tier="fast")
        preclose.complete(_Args(repo="vitalharmony/hrse", issue=1208, head="HEAD"))
        self.commit("scripts/revised.py")
        self.assertIn("refuters:", self.plan(tier="fast"))

    def test_receipt_survives_a_change_of_working_directory(self) -> None:
        """Reproduced by four refuters: `cd scripts` defeated the whole guard."""
        self.commit("scripts/ordinary.py")
        self.plan(tier="fast")
        preclose.complete(_Args(repo="vitalharmony/hrse", issue=1208, head="HEAD"))
        subdir = self.repo / "scripts"
        os.chdir(subdir)
        with self.assertRaises(SystemExit):
            self.plan(tier="fast")

    def test_force_overrides_a_completed_receipt(self) -> None:
        self.commit("scripts/ordinary.py")
        self.plan(tier="fast")
        preclose.complete(_Args(repo="vitalharmony/hrse", issue=1208, head="HEAD"))
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

    def test_completion_records_the_reviewed_sha_and_status(self) -> None:
        self.commit("scripts/ordinary.py")
        self.plan(tier="fast")
        preclose.complete(_Args(repo="vitalharmony/hrse", issue=1208, head="HEAD"))
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
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            preclose.complete(_Args(repo="vitalharmony/hrse", issue=1208, head="HEAD"))
        self.assertNotRegex(buffer.getvalue(), r"\bPASS\b")

    def test_plan_output_states_it_is_not_a_gate(self) -> None:
        """The half of the AC that carries the design decision."""
        self.commit("scripts/ordinary.py")
        self.assertIn("not a Lane 3 gate", self.plan(tier="fast"))

    def test_completion_output_states_it_is_not_a_gate(self) -> None:
        self.commit("scripts/ordinary.py")
        self.plan(tier="fast")
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            preclose.complete(_Args(repo="vitalharmony/hrse", issue=1208, head="HEAD"))
        self.assertIn("not a Lane 3 gate", buffer.getvalue())


if __name__ == "__main__":
    unittest.main()
