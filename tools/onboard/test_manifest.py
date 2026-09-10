#!/usr/bin/env python3
"""Tests for the onboarded-repo manifest (harmonic-forge#498).

Every test writes its own manifest into a temp dir. The live `projects.toml` is
read by exactly two tests, both of which assert a property that must hold for
the *shipped* file (the board map matches what the consumers had hardcoded, and
the prefixes agree with `lane-shorthand.md`) — those are the regressions this
change could actually cause, and a fixture cannot catch either.
"""
from __future__ import annotations

import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import manifest as mf  # noqa: E402

LIVE = HERE.parents[1] / "projects.toml"


def write(body: str) -> Path:
    tmp = tempfile.mkdtemp()
    path = Path(tmp) / "projects.toml"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


MINIMAL = """
    [[project]]
    name = "alpha"
    prefix = "A"
"""


class LoadTests(unittest.TestCase):
    def test_a_minimal_entry_loads(self) -> None:
        projects = mf.load(write(MINIMAL))
        self.assertEqual([p.name for p in projects], ["alpha"])
        self.assertIsNone(projects[0].repo)
        self.assertIsNone(projects[0].checkout)

    def test_declaration_order_is_preserved(self) -> None:
        path = write("""
            [[project]]
            name = "zulu"
            prefix = "Z"

            [[project]]
            name = "alpha"
            prefix = "A"
        """)
        self.assertEqual([p.name for p in mf.load(path)], ["zulu", "alpha"])


class FailLoudlyTests(unittest.TestCase):
    """A loader that returned [] on a bad manifest would make every consumer
    behave as if nothing were onboarded — `gh_issue.py` treats "no board" as a
    legitimate no-op and the sweep would audit nothing and report clean. Both
    look like success."""

    def assertRaisesManifest(self, body: str, needle: str) -> None:
        with self.assertRaises(mf.ManifestError) as ctx:
            mf.load(write(body))
        self.assertIn(needle, str(ctx.exception))

    def test_missing_file(self) -> None:
        with self.assertRaises(mf.ManifestError):
            mf.load(Path("/nonexistent/projects.toml"))

    def test_unparseable_toml(self) -> None:
        self.assertRaisesManifest("[[project]\nname =", "cannot read")

    def test_no_entries(self) -> None:
        self.assertRaisesManifest("# nothing here\n", "declares no [[project]]")

    def test_missing_required_key(self) -> None:
        self.assertRaisesManifest('[[project]]\nprefix = "A"\n', "missing `name`")
        self.assertRaisesManifest('[[project]]\nname = "a"\n', "missing `prefix`")

    def test_an_unknown_key_is_a_typo_not_a_shrug(self) -> None:
        """`board_num = 3` parses, loads, and leaves the project boardless with
        no complaint — the silent drift this file exists to end."""
        self.assertRaisesManifest(
            '[[project]]\nname = "a"\nprefix = "A"\nboard_num = "3"\n',
            "unknown key(s): board_num")

    def test_duplicate_prefix(self) -> None:
        self.assertRaisesManifest("""
            [[project]]
            name = "alpha"
            prefix = "A"

            [[project]]
            name = "anvil"
            prefix = "A"
        """, "claimed by both")

    def test_duplicate_repo(self) -> None:
        self.assertRaisesManifest("""
            [[project]]
            name = "a"
            prefix = "A"
            repo = "o/r"

            [[project]]
            name = "b"
            prefix = "B"
            repo = "o/r"
        """, "declared twice")

    def test_a_multi_character_prefix_is_rejected(self) -> None:
        self.assertRaisesManifest('[[project]]\nname = "a"\nprefix = "AB"\n',
                                  "must be a single letter")

    def test_a_malformed_repo_is_rejected(self) -> None:
        self.assertRaisesManifest(
            '[[project]]\nname = "a"\nprefix = "A"\nrepo = "not-owner-slash-name"\n',
            "is not owner/name")

    def test_onboarded_without_a_repo_is_rejected(self) -> None:
        self.assertRaisesManifest(
            '[[project]]\nname = "a"\nprefix = "A"\nonboarded = true\n',
            "onboarded but declares no repo")


class WorktreeTests(unittest.TestCase):
    def test_worktrees_default_to_the_checkouts_parent(self) -> None:
        path = write("""
            [[project]]
            name = "a"
            prefix = "A"
            path = "/srv/code/thing"
        """)
        self.assertEqual([str(p) for p in mf.load(path)[0].worktrees],
                         ["/srv/code/thing-lane2", "/srv/code/thing-lane3"])

    def test_worktree_dir_overrides_the_default(self) -> None:
        """harmonic-forge's checkout is `~/harmonic-forge` while its lane
        worktrees are in `~/Harmonic_Projects/`. That is not derivable from the
        checkout path, and inferring it reported two existing worktrees as
        missing."""
        path = write("""
            [[project]]
            name = "a"
            prefix = "A"
            path = "/srv/thing"
            worktree_dir = "/elsewhere"
        """)
        self.assertEqual([str(p) for p in mf.load(path)[0].worktrees],
                         ["/elsewhere/thing-lane2", "/elsewhere/thing-lane3"])

    def test_worktrees_are_named_from_the_directory_not_the_manifest_name(self) -> None:
        """HRSE2's directory is `HRSE2`; its manifest name is `hrse`. The lane
        launchers resolve by path, so naming from `name` yields worktrees
        nothing finds."""
        path = write("""
            [[project]]
            name = "hrse"
            prefix = "H"
            path = "/x/HRSE2"
        """)
        self.assertTrue(str(mf.load(path)[0].worktrees[0]).endswith("HRSE2-lane2"))

    def test_a_projected_repo_has_no_worktrees(self) -> None:
        self.assertEqual(mf.load(write(MINIMAL))[0].worktrees, [])


class ViewTests(unittest.TestCase):
    BODY = """
        [[project]]
        name = "has-board"
        prefix = "H"
        repo = "o/hb"
        account = "vitalharmony"
        board_owner = "vitalharmony"
        board_number = "1"

        [[project]]
        name = "no-board"
        prefix = "N"
        repo = "o/nb"
        account = "vitalharmony"

        [[project]]
        name = "other-account"
        prefix = "K"
        repo = "o/oa"
        account = "someoneelse"

        [[project]]
        name = "projected"
        prefix = "P"
    """

    def test_by_repo_excludes_projected_entries(self) -> None:
        """A caller keyed on repo cannot act on a repo that does not exist."""
        self.assertEqual(sorted(mf.by_repo(write(self.BODY))), ["o/hb", "o/nb", "o/oa"])

    def test_repo_boards_excludes_boardless_repos(self) -> None:
        self.assertEqual(mf.repo_boards(write(self.BODY)),
                         {"o/hb": ("vitalharmony", "1")})

    def test_sweep_excludes_other_accounts(self) -> None:
        """ke'nekted is a separate account with separate credentials, and a
        vitalharmony-authed query against it returns EMPTY rather than
        erroring — sweeping it would report a clean repo."""
        self.assertEqual(mf.sweep_repos(write(self.BODY)), ["o/hb", "o/nb"])

    def test_sweep_is_not_filtered_by_onboarded(self) -> None:
        """A `onboarded = false` repo IS in the sweep. Stranded work is worth
        finding whether or not the lane apparatus is installed, and filtering
        here would silently drop a covered repo.

        This used to cite openclaw-projects as the live example; it was
        onboarded on 2026-09-10, so the example is now the fixture below rather
        than a shipped entry. The property is unchanged -- and it is why
        onboarding openclaw changed no sweep behavior."""
        path = write("""
            [[project]]
            name = "not-onboarded"
            prefix = "N"
            repo = "o/n"
            account = "vitalharmony"
            onboarded = false
        """)
        self.assertEqual(mf.sweep_repos(path), ["o/n"])

    def test_prefixes_include_projected_entries(self) -> None:
        """The point of listing a projected repo is to RESERVE its letter."""
        self.assertIn("P", mf.prefixes(write(self.BODY)))


class LiveManifestTests(unittest.TestCase):
    """Two properties that must hold for the shipped file. A fixture cannot
    catch either, because both are about this change not altering behavior."""

    def test_the_board_map_matches_what_the_consumers_hardcoded(self) -> None:
        """`gh_issue.py` and `repo_hygiene.py` now read the manifest instead of
        each keeping a copy. If the manifest disagrees with what those copies
        said, issues start filing onto the wrong board — silently, which is the
        failure harmonic-forge#107 fixed once already."""
        self.assertEqual(mf.repo_boards(LIVE), {
            "vitalharmony/hrse": ("vitalharmony", "1"),
            "vitalharmony/harmonic-forge": ("vitalharmony", "3"),
            "vitalharmony/cymagraph-infra": ("vitalharmony", "1"),
            # #4 since 2026-09-10 -- its own venture, see the manifest header.
            "vitalharmony/openclaw-projects": ("vitalharmony", "4"),
        })

    def test_the_sweep_list_matches_the_live_hygiene_task(self) -> None:
        self.assertEqual(mf.sweep_repos(LIVE), [
            "vitalharmony/hrse", "vitalharmony/harmonic-forge",
            "vitalharmony/cymagraph-infra", "vitalharmony/openclaw-projects",
        ])

    def test_the_manifest_agrees_with_lane_shorthand(self) -> None:
        """`expand_lane_shorthand.py` parses the doc table at runtime and is
        deliberately NOT rewired to the manifest — the doc is prose a human
        reads and the parser already treats it as the source. So the manifest
        is CHECKED against it. A letter in one and not the other is how
        `L2B F496` reaches the wrong repo."""
        self.assertEqual(mf.check_prefix_agreement(LIVE), [])

    def test_that_agreement_check_can_actually_fail(self) -> None:
        """Otherwise the assertion above is a check that always passes."""
        path = write("""
            [[project]]
            name = "invented"
            prefix = "Q"
        """)
        findings = mf.check_prefix_agreement(path)
        self.assertTrue(any(f.startswith("Q:") for f in findings), findings)


if __name__ == "__main__":
    unittest.main()


class SweepFallbackConsumerTests(unittest.TestCase):
    """`sweep_repos()` must have a real consumer, or AC3 is half done.

    It did not: `repo_hygiene.py` still declared `--repo` with `default=[]` and
    the four repos were still spelled literally in hrse's mise.toml and in
    .github/workflows/repo-hygiene.yml. The function existed, was tested, and
    nothing called it.
    """

    def test_repo_hygiene_falls_back_to_the_manifest(self) -> None:
        source = (HERE.parents[0] / "gh" / "repo_hygiene.py").read_text(encoding="utf-8")
        self.assertIn("sweep_repos", source,
                      "repo_hygiene.py must consume the manifest, not a private list")

    def test_the_manifest_is_opted_into_explicitly_never_inferred(self) -> None:
        """An earlier draft fell back to the manifest whenever `--repo` was
        absent. That turned every `--checkout`-only invocation into a live
        four-repo network sweep — including this repo's own
        `test_truly_clean_run_still_prints_clean`, which hung the suite for
        minutes with no output. Opt-in, always."""
        source = (HERE.parents[0] / "gh" / "repo_hygiene.py").read_text(encoding="utf-8")
        self.assertIn("--manifest-repos", source)
        self.assertIn("if args.manifest_repos:", source)
        self.assertNotIn("if not args.repo:\n        # Fall back", source)

    def test_a_checkout_only_run_still_needs_no_network(self) -> None:
        """The regression above, asserted against the parser rather than the
        source text."""
        import subprocess

        script = HERE.parents[0] / "gh" / "repo_hygiene.py"
        result = subprocess.run(
            [sys.executable, str(script), "--help"],
            capture_output=True, text=True, timeout=30)
        self.assertIn("--manifest-repos", result.stdout)
