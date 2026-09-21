#!/usr/bin/env python3
"""Unit tests for watch_lane_posts.py (harmonic-forge#442) -- pure parsing
logic only, no live gh/API calls. Fixtures are real comment bodies from
hrse#1530 (trimmed), not invented shapes."""
import argparse
import datetime as dt
import io
import json
import os
import re
import sys
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import belt_candidates
import watch_lane_posts
from belt_mechanics import SeenSet, Watermarks, query_since
from watch_lane_posts import (
    QUEUE_KINDS,
    _BRANCH_ISSUE_RE,
    _classify,
    branch_ahead_lines,
    branch_ahead_without_completion,
    discover_from_worktree,
    discover_l1_sweep,
    discover_l3_unanswered_verdicts,
    discover_queue,
    drop_closed_targets,
    RootNotARepo,
    cycle_is_quiet,
    enumerate_repo_roots,
    enumerate_worktrees,
    l1_sweep_cycle,
    l3_verdict_sweep_cycle,
    list_open_issues,
    next_poll_interval,
    sleep_before_next_poll,
    MONITOR_LIFETIME_S,
    report_resolution,
    resolve_worktree,
)

#: `skills/belt-and-suspenders/DESIGN.md`, two directories above `tools/gh/`
#: (the same relative path the skill's own docstring names, harmonic-
#: forge#570). harmonic-forge#659 moved the skill's former body, which these
#: doc-sync tests read, from `SKILL.md` to `DESIGN.md`; `SKILL.md` itself is now
#: a short "run belt_plan.py" procedure, asserted in
#: `skills/belt-and-suspenders/test_skill_text.py`.
_SKILL_MD = Path(__file__).resolve().parent.parent.parent / "skills" / "belt-and-suspenders" / "DESIGN.md"


class ClassifyTests(unittest.TestCase):
    def test_l1_post_marker_wins_regardless_of_heading(self):
        body = ("## Handoff: hrse#1530 — some title\n\nbody text\n\n"
                "<!-- l1-post v1; kind=handoff; posted-by=LANE-unset -->")
        self.assertEqual(_classify(body), ("l1", "handoff"))

    def test_l1_ready_for_l3_marker_with_extra_fields(self):
        body = ("## Ready for Lane 3 — hrse#1530\n\nbody\n\n"
                "<!-- l1-post v1; kind=ready-for-l3; sha=abc123; checks=x,y -->")
        self.assertEqual(_classify(body), ("l1", "ready-for-l3"))

    def test_l2_heading_no_marker(self):
        body = "## L2D — receipt-backed status (harmonic-forge#371)\n\nsome narrative"
        self.assertEqual(_classify(body), ("l2", "## L2D — receipt-backed status (harmonic-forge#371)"))

    def test_l2_plan_heading(self):
        body = "## L2P — receipt-backed status (harmonic-forge#371)\n\nplan text"
        lane, _ = _classify(body)
        self.assertEqual(lane, "l2")

    def test_l3_heading_spelled_out_not_a_short_code(self):
        body = "## Lane 3 Test Spec — hrse#1530 (NULL-tolerant sync predicate)\n\nspec text"
        lane, _ = _classify(body)
        self.assertEqual(lane, "l3")

    def test_plain_comment_is_unclassified(self):
        self.assertIsNone(_classify("just a plain chat comment, no heading, no marker"))

    def test_unrelated_heading_is_unclassified(self):
        self.assertIsNone(_classify("## Some other heading entirely\n\ntext"))

    def test_l2_marker_reads_as_l2_not_l1(self):
        """harmonic-forge#583 AC2: once `l2_post.py` stamps the same
        `l1-post` marker Lane 1 uses, the marker's mere presence must no
        longer imply Lane 1 -- `posted-by=LANE2` has to route this to `l2`,
        or a real Lane 2 post silently reads as a Lane 1 one."""
        body = ("## L2D — receipt-backed status (harmonic-forge#371)\n\ntext\n\n"
                "<!-- l1-post v1; kind=completion; posted-by=LANE2 -->")
        self.assertEqual(_classify(body), ("l2", "completion"))

    def test_l3_marker_reads_as_l3(self):
        body = ("## Lane 3 Gate Results\n\ntext\n\n"
                "<!-- l1-post v1; kind=gate-result; posted-by=LANE3 -->")
        self.assertEqual(_classify(body), ("l3", "gate-result"))

    def test_marker_with_no_posted_by_still_defaults_to_l1(self):
        """Backward compatibility (AC5): every marker minted before
        harmonic-forge#583 carries no `posted-by` at all -- this is Lane
        1's entire historical corpus, and it must keep reading as `l1`."""
        body = "note\n\n<!-- l1-post v1; kind=discussion -->"
        self.assertEqual(_classify(body), ("l1", "discussion"))

    def test_corpus_l1_post_via_composed_l2_body_reads_as_l2(self):
        """harmonic-forge#583 AC3: feed a REAL `l2_post.py`-composed body
        (not a synthetic string that avoids the awkward cases) through
        `_classify` and assert lane=`l2` for every kind."""
        import l2_post as lp
        for kind in ("plan", "completion", "blocked", "finding"):
            body = lp.compose_body(kind, [], "a real narrative")
            lane, detail = _classify(body)
            self.assertEqual(lane, "l2", f"kind={kind}")
            self.assertEqual(detail, kind)

    def test_corpus_hrse1676_l2p_heading_still_reads_as_l2(self):
        """harmonic-forge#583 AC3/AC8, real corpus example named in the
        issue: hrse#1676's actual `## L2P` comment, posted before
        `l2_post.py` stamped a marker at all. Must still classify as `l2`
        -- the heading fallback, not a marker -- and this test asserts it
        rather than assuming the earlier `test_l2_plan_heading` covers the
        AC (that test predates #583 and was not written against this AC)."""
        body = "## L2P — receipt-backed status (harmonic-forge#371)\n\nplan narrative"
        self.assertEqual(_classify(body)[0], "l2")

    def test_corpus_hrse1754_lane2_completion_prose_heading(self):
        """harmonic-forge#583 AC3, the second real corpus example named in
        the issue: hrse#1754's `## Lane 2 completion:` comment did not match
        `_L2_HEADING_RE` at all (a prose heading, not a short code) and was
        "surfaced only because it happened to carry an l1-post marker from
        a different path, and the watcher labelled it l1" -- the exact
        misclassification this issue's AC2 exists to fix by making the
        marker's `posted-by` field authoritative over a blind kind=X ->
        lane=l1 assumption. A body carrying that heading AND a genuine
        `posted-by=LANE2` marker must classify as `l2`, not `l1`."""
        body = ("## Lane 2 completion: hrse#1754\n\ntext\n\n"
                "<!-- l1-post v1; kind=completion; posted-by=LANE2 -->")
        self.assertEqual(_classify(body), ("l2", "completion"))
        # And the same heading with NO marker at all (a hand-typed post
        # bypassing l2_post.py entirely) is still unclassified by heading
        # alone -- this module doesn't attempt prose-heading matching, and
        # #583 does not claim to fix that half; only the marker-carrying
        # case (the one that actually happened) is in scope here.
        headless = "## Lane 2 completion: hrse#1754\n\ntext, no marker at all"
        self.assertIsNone(_classify(headless))


class RetiredTokenMarkingTests(unittest.TestCase):
    """harmonic-forge#609. `L2P` stopped being emitted at #583, but historical
    comments carry it and the belt renders a comment's first line straight into
    the lane's task display -- where it reads as current. An operator saw
    exactly that and corrected Lane 1 by hand."""

    def test_a_retired_token_is_marked_not_reproduced_bare(self):
        self.assertEqual(
            watch_lane_posts._mark_retired_tokens(
                "## L2P — receipt-backed status (harmonic-forge#371)"),
            "## L2P [retired -> L2S or L2D] — receipt-backed status "
            "(harmonic-forge#371)")

    def test_the_quote_is_marked_not_falsified(self):
        """The comment is an accurate record of what was posted. The token
        must still be visible, not silently rewritten to L2S."""
        marked = watch_lane_posts._mark_retired_tokens("## L2P — x")
        self.assertIn("L2P", marked)

    def test_live_tokens_are_untouched(self):
        for headline in ("## L2S — plan", "## L2D — done", "## L2B — blocked",
                         "## L3S — spec", "## Lane 3 Gate Update"):
            with self.subTest(headline=headline):
                self.assertEqual(watch_lane_posts._mark_retired_tokens(headline),
                                 headline)

    def test_classify_marks_a_historical_l2p_heading(self):
        """End to end through the path that actually feeds the display."""
        lane, detail = watch_lane_posts._classify(
            "## L2P — receipt-backed status (harmonic-forge#371)\n\nbody")
        self.assertEqual(lane, "l2")
        self.assertIn("[retired -> L2S or L2D]", detail)

    def test_the_marking_reads_the_shared_registry(self):
        """Driven from `retired_artifacts.RETIRED_ARTIFACTS`, not a second
        list -- a second list is the drift this repo keeps paying for."""
        from retired_artifacts import RETIRED_ARTIFACTS
        self.assertIn("L2P", RETIRED_ARTIFACTS)
        self.assertIn("L2S", RETIRED_ARTIFACTS["L2P"])
        self.assertIn("L2D", RETIRED_ARTIFACTS["L2P"])


class PrefixMapIsDerivedTests(unittest.TestCase):
    """harmonic-forge#605 preclose finding. Three independent copies of the
    prefix map existed -- this module's `_PREFIX_REPO`, its `_BRANCH_ISSUE_RE`
    character class, and `batch_auth.REPO_PREFIXES` -- and no test tied any of
    them together. Onboarding openclaw showed the cost: `O` reached the
    manifest and `lane-shorthand.md` while the belt still read `[hHfFiI]`, so
    a branch named `l2/o12-fix` resolved to nothing."""

    def test_every_manifest_repo_has_a_prefix_the_belt_can_resolve(self):
        import manifest as onboard_manifest
        for prefix, repo in onboard_manifest.prefix_repos().items():
            with self.subTest(repo=repo):
                self.assertEqual(watch_lane_posts._PREFIX_REPO.get(prefix), repo)

    def test_the_branch_regex_accepts_every_manifest_prefix(self):
        """The regex class and the map must not be able to disagree."""
        for prefix in watch_lane_posts._PREFIX_REPO:
            for letter in (prefix.lower(), prefix.upper()):
                with self.subTest(letter=letter):
                    match = _BRANCH_ISSUE_RE.search(f"l2/{letter}12-fix")
                    self.assertIsNotNone(match, f"{letter}12 does not resolve")
                    self.assertEqual(match.group("num"), "12")

    def test_openclaw_specifically_resolves(self):
        """The concrete case the finding named, in the branch shapes actually
        used live (`__gate__/h1343-tc5` exists in the HRSE2 checkout today)."""
        for branch in ("l2/o12-fix", "__gate__/o12-tc1", "o12"):
            with self.subTest(branch=branch):
                match = _BRANCH_ISSUE_RE.search(branch)
                self.assertIsNotNone(match)
                self.assertEqual(match.group("prefix"), "o")
        self.assertEqual(watch_lane_posts._PREFIX_REPO["o"],
                         "vitalharmony/openclaw-projects")

    def test_batch_auth_agrees_with_the_manifest(self):
        """The third copy. `BATCH O12` resolving against a stale table is
        silent, which is why this is asserted rather than reviewed."""
        import manifest as onboard_manifest
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))
        import batch_auth
        expected = {p.repo: p.prefix for p in onboard_manifest.load()
                    if p.repo and (p.account or "vitalharmony") == "vitalharmony"}
        self.assertEqual(batch_auth.REPO_PREFIXES, expected)


class BranchIssueRegexTests(unittest.TestCase):
    """Real branch names observed live across this repo's worktrees,
    2026-09-03 -- not invented shapes."""

    def _num_prefix(self, branch):
        m = _BRANCH_ISSUE_RE.search(branch)
        if not m:
            return None
        return m.group("prefix"), m.group("num")

    def test_l2_h_prefixed(self):
        self.assertEqual(self._num_prefix("l2/h1530-null-tolerant-sync-predicate"),
                         ("h", "1530"))

    def test_h_prefixed_no_lane_segment(self):
        self.assertEqual(self._num_prefix("h1522/tier-group-rename"), ("h", "1522"))

    def test_bare_digits_no_letter_prefix(self):
        self.assertEqual(self._num_prefix("fix/1498-workflow-secrets-context"),
                         (None, "1498"))

    def test_f_prefixed_cross_repo_subject(self):
        self.assertEqual(self._num_prefix("l2/f433-drift-check-patch-id"), ("f", "433"))

    def test_bare_digits_spike_branch(self):
        self.assertEqual(self._num_prefix("spike/733-plan"), (None, "733"))

    def test_no_issue_number_returns_none(self):
        self.assertIsNone(self._num_prefix("docs/priorities-reconcile-sep3"))
        self.assertIsNone(self._num_prefix("docs/transaction-log-regen-sep3"))


class DiscoverFromWorktreeTests(unittest.TestCase):
    """End-to-end against real temporary git repos -- no network, no gh."""

    def _repo(self, remote_url: str, branch: str) -> str:
        tmp = tempfile.mkdtemp()
        subprocess.run(["git", "init", "-q", "-b", branch, tmp], check=True)
        subprocess.run(["git", "-C", tmp, "remote", "add", "origin", remote_url], check=True)
        subprocess.run(["git", "-C", tmp, "commit", "-q", "--allow-empty", "-m", "x"],
                       check=True, env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
                                        "PATH": __import__("os").environ.get("PATH", "")})
        return tmp

    def test_h_prefix_overrides_worktree_repo(self):
        """A hrse-repo worktree hosting a branch about a forge issue must
        resolve to harmonic-forge -- the real F433 case."""
        repo = self._repo("git@github.com:vitalharmony/hrse.git",
                          "l2/f433-drift-check-patch-id")
        self.assertEqual(discover_from_worktree(repo),
                         ("vitalharmony/harmonic-forge", 433))

    def test_unprefixed_number_uses_worktree_own_repo(self):
        repo = self._repo("https://github.com/vitalharmony/hrse.git",
                          "fix/1498-workflow-secrets-context")
        self.assertEqual(discover_from_worktree(repo), ("vitalharmony/hrse", 1498))

    def test_branch_with_no_issue_number_is_none(self):
        repo = self._repo("https://github.com/vitalharmony/hrse.git",
                          "docs/priorities-reconcile-sep3")
        self.assertIsNone(discover_from_worktree(repo))

    def test_non_git_path_is_none(self):
        tmp = tempfile.mkdtemp()
        self.assertIsNone(discover_from_worktree(tmp))


class ResolveWorktreeReasonTests(unittest.TestCase):
    """harmonic-forge#570 AC6 -- an unresolved worktree carries a reason,
    not just a `None`, so a lane between issues (detached HEAD, its normal
    resting state per hrse#570's own cross-lane evidence) is not silently
    read as 'nothing to watch'."""

    def _repo(self, remote_url: str, branch: str | None = None) -> str:
        tmp = tempfile.mkdtemp()
        args = ["git", "init", "-q"]
        if branch:
            args += ["-b", branch]
        args.append(tmp)
        subprocess.run(args, check=True)
        subprocess.run(["git", "-C", tmp, "remote", "add", "origin", remote_url], check=True)
        subprocess.run(["git", "-C", tmp, "commit", "-q", "--allow-empty", "-m", "x"],
                       check=True, env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
                                        "PATH": __import__("os").environ.get("PATH", "")})
        return tmp

    def test_resolved_pair_reports_resolved(self):
        repo = self._repo("https://github.com/vitalharmony/hrse.git",
                          "fix/1498-workflow-secrets-context")
        pair, reason = resolve_worktree(repo)
        self.assertEqual(pair, ("vitalharmony/hrse", 1498))
        self.assertEqual(reason, "resolved")

    def test_detached_head_names_the_reason(self):
        repo = self._repo("https://github.com/vitalharmony/hrse.git", branch="main")
        subprocess.run(["git", "-C", repo, "checkout", "-q", "--detach"], check=True)
        pair, reason = resolve_worktree(repo)
        self.assertIsNone(pair)
        self.assertIn("detached HEAD", reason)

    def test_branch_with_no_issue_number_names_the_reason(self):
        repo = self._repo("https://github.com/vitalharmony/hrse.git",
                          "docs/priorities-reconcile-sep3")
        pair, reason = resolve_worktree(repo)
        self.assertIsNone(pair)
        self.assertIn("names no issue number", reason)

    def test_non_git_path_names_the_reason(self):
        tmp = tempfile.mkdtemp()
        pair, reason = resolve_worktree(tmp)
        self.assertIsNone(pair)
        self.assertIn("not a git worktree", reason)


class ReportResolutionTests(unittest.TestCase):
    """`report_resolution` is the fail-loud surface AC6 asks for -- asserted
    against its actual stderr output, not just the return value."""

    def _detached(self) -> str:
        tmp = tempfile.mkdtemp()
        subprocess.run(["git", "init", "-q", "-b", "main", tmp], check=True)
        subprocess.run(["git", "-C", tmp, "remote", "add", "origin",
                       "https://github.com/vitalharmony/hrse.git"], check=True)
        subprocess.run(["git", "-C", tmp, "commit", "-q", "--allow-empty", "-m", "x"],
                       check=True, env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
                                        "PATH": __import__("os").environ.get("PATH", "")})
        subprocess.run(["git", "-C", tmp, "checkout", "-q", "--detach"], check=True)
        return tmp

    def test_zero_resolved_is_stated_loudly(self):
        import io
        from contextlib import redirect_stderr
        buf = io.StringIO()
        with redirect_stderr(buf):
            resolutions = report_resolution([self._detached()])
        self.assertEqual(len(resolutions), 1)
        self.assertIsNone(resolutions[0][1])
        out = buf.getvalue()
        self.assertIn("0/1 resolved", out)
        self.assertIn("ZERO of 1 --worktrees target(s) resolved", out)

    def test_empty_worktree_list_prints_nothing(self):
        import io
        from contextlib import redirect_stderr
        buf = io.StringIO()
        with redirect_stderr(buf):
            resolutions = report_resolution([])
        self.assertEqual(resolutions, [])
        self.assertEqual(buf.getvalue(), "")


def _fake_completed(stdout, returncode=0):
    result = subprocess.CompletedProcess(args=[], returncode=returncode)
    result.stdout = stdout
    result.stderr = ""
    return result


class DiscoverQueueTests(unittest.TestCase):
    """`discover_queue` never touches a real network; every `gh` call is
    mocked. Real marker/heading shapes throughout, matching `_classify`'s
    own fixtures."""

    def _l1(self, kind):
        return f"## Some post\n\n<!-- l1-post v1; kind={kind}; posted-by=LANE1 -->"

    def _mock_gh(self, comments: dict):
        """`comments`: {issue: [comment bodies, in order]}.

        harmonic-forge#518: every call now goes through
        `belt_mechanics.gh_as`, so the argv shape is
        `["gh-as", <account>, "gh", ...]`.

        harmonic-forge#686: there is no longer a `search_results` half. The
        candidate set is an argument to `discover_queue`, supplied by the
        caller from what the belt already holds, so a `search/issues` call
        reaching this mock is itself the regression — it fails loudly below
        rather than being served, which is what makes these tests evidence
        for AC1 and not merely compatible with it.
        """
        def run(argv, **kwargs):
            self.assertEqual(argv[0], "gh-as",
                             f"every call must be account-scoped: {argv}")
            gh_argv = argv[3:]  # strip ["gh-as", <account>, "gh"]
            self.assertNotIn(
                "search/issues", gh_argv,
                "harmonic-forge#686: no belt path may issue an "
                f"issue-number-free account-wide scan: {argv}")
            if "api" in gh_argv and any("/comments" in a for a in gh_argv):
                # harmonic-forge#570 preclose finding: the comments fetch
                # must be an explicit GET and must paginate -- assert both
                # rather than just tolerating whichever shape shows up, so a
                # regression back to an implicit POST or a single page fails
                # this test instead of silently passing.
                self.assertIn("-X", gh_argv, f"comments fetch must be explicit GET: {gh_argv}")
                self.assertEqual(gh_argv[gh_argv.index("-X") + 1], "GET")
                self.assertIn("--paginate", gh_argv, f"comments fetch must paginate: {gh_argv}")
                path = next(a for a in gh_argv if a.startswith("repos/") and "/comments" in a)
                issue = int(path.rsplit("/", 2)[-2])
                bodies = comments.get(issue, [])
                return _fake_completed(json.dumps([{"body": b} for b in bodies]))
            if "api" in gh_argv and "--jq" in gh_argv and ".labels[].name" in gh_argv:
                # harmonic-forge#686 preclose finding: `_issue_labels`'
                # single-issue label lookup. Bounded (one issue's own label
                # list, no pagination needed) -- distinct from the comments
                # fetch above, which lists an unbounded, growing collection.
                return _fake_completed("")
            raise AssertionError(f"unexpected gh call: {argv}")
        return run

    def test_issue_with_matching_marker_as_latest_comment_is_queued(self):
        with patch("belt_mechanics.subprocess.run",
                  side_effect=self._mock_gh(
                      comments={1530: [self._l1("handoff"), self._l1("ready-for-l3")]},
                  )):
            self.assertEqual(
                discover_queue("vitalharmony/hrse", "l3", {1530})[0],
                {1530: "ready-for-l3"})

    def test_issue_superseded_by_a_later_comment_is_not_queued(self):
        """The self-clearing property: once Lane 3 (or anyone) posts after
        the marker, the issue drops out with no separate bookkeeping."""
        with patch("belt_mechanics.subprocess.run",
                  side_effect=self._mock_gh(
                      comments={1530: [self._l1("ready-for-l3"),
                                       "## Lane 3 Gate Results — PASS"]},
                  )):
            self.assertEqual(
                discover_queue("vitalharmony/hrse", "l3", {1530})[0], {})

    def test_no_candidates_yields_empty_queue(self):
        """harmonic-forge#686: an empty candidate set (nothing the caller
        already holds names this repo) short-circuits with no `gh` call at
        all -- there is no search fallback to fall back to."""
        with patch("belt_mechanics.subprocess.run",
                  side_effect=self._mock_gh(comments={})):
            self.assertEqual(
                discover_queue("vitalharmony/hrse", "l3", set())[0], {})

    def test_issue_found_by_two_kind_searches_is_deduplicated(self):
        """A candidate is only a candidate; the real classification decides
        the kind. An issue that could match two kinds (e.g. it once carried
        an `ae` marker, later superseded by `ready-for-l3`) must appear
        once, with whichever kind is actually latest."""
        with patch("belt_mechanics.subprocess.run",
                  side_effect=self._mock_gh(
                      comments={1530: [self._l1("ae"), self._l1("ready-for-l3")]},
                  )):
            queue, _ok = discover_queue("vitalharmony/hrse", "l3", {1530})
            self.assertEqual(queue, {1530: "ready-for-l3"})

    def test_two_real_currently_queued_issues_hrse1058_and_1531(self):
        """Live shape observed 2026-09-03: two separate issues, each with
        its own ready-for-l3 comment, no cross-contamination."""
        with patch("belt_mechanics.subprocess.run",
                  side_effect=self._mock_gh(
                      comments={
                          1058: [self._l1("handoff"), "## L2P", "## L2D",
                                 self._l1("ready-for-l3")],
                          1531: [self._l1("handoff"), "## L2P", "## L2D",
                                 self._l1("ready-for-l3")],
                      },
                  )):
            queue, _ok = discover_queue("vitalharmony/hrse", "l3", {1058, 1531})
            self.assertEqual(queue, {1058: "ready-for-l3", 1531: "ready-for-l3"})

    def test_ae_and_sweep_marker_is_queued_for_l3(self):
        """harmonic-forge#579 AC2 -- live reproduction: `kind=ae-and-sweep`
        is a real, valid Lane 1 choice with 9 live markers already on
        vitalharmony/hrse. Before that fix, `QUEUE_KINDS["l3"]` did not
        contain the string at all, so `discover_queue` printed nothing for a
        real, actionable AE."""
        with patch("belt_mechanics.subprocess.run",
                  side_effect=self._mock_gh(
                      comments={1725: [self._l1("ae-and-sweep")]},
                  )):
            queue, _ok = discover_queue("vitalharmony/hrse", "l3", {1725})
            self.assertEqual(queue, {1725: "ae-and-sweep"})

    def test_l2_finding_after_ready_for_l3_does_not_drop_the_issue(self):
        """harmonic-forge#580 AC1 -- live reproduction: a `## L2 Finding`
        comment posted after `ready-for-l3` must not change the issue's
        Lane 3 queue membership. Before the fix, the finding became
        `last_kind`, failed the `last_kind[0] == 'l1'` check, and silently
        dropped a genuinely queued issue out of the belt."""
        with patch("belt_mechanics.subprocess.run",
                  side_effect=self._mock_gh(
                      comments={571: [
                          self._l1("ready-for-l3"),
                          "## L2 Finding — receipt-backed finding (harmonic-forge#571)",
                      ]},
                  )):
            queue, _ok = discover_queue("vitalharmony/hrse", "l3", {571})
            self.assertEqual(queue, {571: "ready-for-l3"})

    def test_l2_finding_does_not_resurrect_a_superseded_issue(self):
        """The finding-skip must not go too far the other direction: an
        issue genuinely superseded by a real status transition (not a
        finding) must still drop, finding present or not. Uses an `l3`-
        classified superseding comment, which never reaches the `l2`-only
        skip predicate at all -- a coarser check than the one below."""
        with patch("belt_mechanics.subprocess.run",
                  side_effect=self._mock_gh(
                      comments={571: [
                          self._l1("ready-for-l3"),
                          "## L2 Finding — receipt-backed finding (harmonic-forge#571)",
                          "## Lane 3 Gate Results — PASS",
                      ]},
                  )):
            queue, _ok = discover_queue("vitalharmony/hrse", "l3", {571})
            self.assertEqual(queue, {})

    def test_a_real_l2_status_transition_still_supersedes_after_a_finding(self):
        """harmonic-forge#580 preclose finding: the previous test's
        superseding comment was `l3`-classified, which never reaches the
        `l2`-only skip predicate (`classified[0] == "l2" and
        _L2_FINDING_RE.match(...)`) at all -- so it could not distinguish
        the correct fix from an over-broad one that skips EVERY `l2`
        heading (including real `L2P`/`L2D`/`L2B` transitions), not just
        `L2 Finding`. This uses a genuine `## L2D` status comment (no
        finding at all) as the superseding event: it must still end
        `discover_queue`'s "last classified" walk and drop the issue out
        of Lane 2's own queue. Reproduced live: mutating the skip predicate
        from `_L2_FINDING_RE` to the broader `_L2_HEADING_RE` makes this
        test fail while leaving every other test in this file green."""
        with patch("belt_mechanics.subprocess.run",
                  side_effect=self._mock_gh(
                      comments={571: [
                          self._l1("handoff"),
                          "## L2D — receipt-backed status (harmonic-forge#371)",
                      ]},
                  )):
            queue, _ok = discover_queue("vitalharmony/hrse", "l2", {571})
            self.assertEqual(queue, {})

    def test_a_real_marker_carrying_finding_still_does_not_drop_the_issue(self):
        """harmonic-forge#583 AC1 regression: `l2_post.py` now stamps a
        marker on `finding` too (previously it fell back to the heading
        exclusively), so `_classify` returns `("l2", "finding")` for it
        instead of `("l2", "## L2 Finding — ...")`. The finding-skip
        predicate (`_is_l2_finding`) must recognize BOTH shapes -- this
        uses a real `l2_post.compose_body("finding", ...)` output, not a
        hand-typed heading, so a regression that only widened the heading
        regex and forgot the marker-kind branch would be caught here."""
        import l2_post as lp
        finding_body = lp.compose_body("finding", [], "diagnosed a defect")
        with patch("belt_mechanics.subprocess.run",
                  side_effect=self._mock_gh(
                      comments={571: [self._l1("ready-for-l3"), finding_body]},
                  )):
            queue, _ok = discover_queue("vitalharmony/hrse", "l3", {571})
            self.assertEqual(queue, {571: "ready-for-l3"})


class BranchAheadWithoutCompletionTests(unittest.TestCase):
    """harmonic-forge#583 AC4. `_run_git` and `_fetch_all_comments` are the
    only I/O boundaries -- mocked directly rather than through a real repo,
    matching this file's existing convention (`DiscoverQueueTests` mocks at
    the `subprocess.run` layer for the same reason)."""

    def test_no_commits_ahead_reports_nothing(self):
        with patch("watch_lane_posts._run_git",
                  side_effect=lambda wt, *a: {"merge-base": "abc",
                                              "rev-list": "0"}[a[0]]), \
             patch("watch_lane_posts._fetch_all_comments", return_value=[]):
            self.assertIsNone(
                branch_ahead_without_completion("/wt", "o/r", 1))

    def test_ahead_with_no_completion_posted_reports(self):
        with patch("watch_lane_posts._run_git") as run_git, \
             patch("watch_lane_posts._fetch_all_comments", return_value=[]):
            run_git.side_effect = lambda wt, *a: (
                "abc" if a[0] == "merge-base" else
                "3" if a[0] == "rev-list" else "fix/1-x")
            report = branch_ahead_without_completion("/wt", "o/r", 1)
            self.assertIsNotNone(report)
            self.assertIn("o/r#1", report)
            self.assertIn("3 commit", report)
            self.assertIn("no completion posted", report)

    def test_ahead_but_completion_already_posted_reports_nothing(self):
        """The steady, correct state: a completion was posted and the
        branch is ahead because of it -- not a gap."""
        import l2_post as lp
        completion_body = lp.compose_body("completion", [], "clean", lead={
            "Status": "done", "Change": "x", "Next": "ready for review"})
        with patch("watch_lane_posts._run_git") as run_git, \
             patch("watch_lane_posts._fetch_all_comments",
                  return_value=[{"body": completion_body}]):
            run_git.side_effect = lambda wt, *a: (
                "abc" if a[0] == "merge-base" else
                "3" if a[0] == "rev-list" else "fix/1-x")
            self.assertIsNone(
                branch_ahead_without_completion("/wt", "o/r", 1))

    def test_ahead_but_completion_posted_via_legacy_heading_reports_nothing(self):
        """AC5: a completion posted before harmonic-forge#583 landed (no
        marker, heading only) must be recognized too."""
        with patch("watch_lane_posts._run_git") as run_git, \
             patch("watch_lane_posts._fetch_all_comments",
                  return_value=[{"body": "## L2D — receipt-backed status "
                                          "(harmonic-forge#371)\n\ntext"}]):
            run_git.side_effect = lambda wt, *a: (
                "abc" if a[0] == "merge-base" else
                "3" if a[0] == "rev-list" else "fix/1-x")
            self.assertIsNone(
                branch_ahead_without_completion("/wt", "o/r", 1))

    def test_no_origin_main_reports_nothing_rather_than_raising(self):
        with patch("watch_lane_posts._run_git", return_value=None):
            self.assertIsNone(
                branch_ahead_without_completion("/wt", "o/r", 1))

    def test_failed_comment_fetch_reports_nothing_not_a_false_positive(self):
        """harmonic-forge#583 preclose finding: `_fetch_all_comments`
        returns `None` (not `[]`) on a failed fetch. Silently treating that
        as "no completion" would turn "I could not check" into a false
        positive claim that a real completion doesn't exist -- worse than
        staying silent for one cycle, since the next successful poll
        re-checks from scratch."""
        with patch("watch_lane_posts._run_git") as run_git, \
             patch("watch_lane_posts._fetch_all_comments", return_value=None):
            run_git.side_effect = lambda wt, *a: (
                "abc" if a[0] == "merge-base" else
                "3" if a[0] == "rev-list" else "fix/1-x")
            self.assertIsNone(
                branch_ahead_without_completion("/wt", "o/r", 1))

    def test_finding_after_completion_does_not_reassert_the_gap(self):
        """harmonic-forge#583 preclose finding: a `finding` (harmonic-
        forge#571 -- a defect report, never a status transition) posted
        after a real completion must not un-classify it. `_is_l2_finding`
        already protects `discover_queue` from exactly this; it must
        protect this function's own "latest l2 event" walk too."""
        import l2_post as lp
        completion_body = lp.compose_body("completion", [], "clean", lead={
            "Status": "done", "Change": "x", "Next": "ready for review"})
        finding_body = lp.compose_body("finding", [], "an unrelated defect note")
        with patch("watch_lane_posts._run_git") as run_git, \
             patch("watch_lane_posts._fetch_all_comments",
                  return_value=[{"body": completion_body}, {"body": finding_body}]):
            run_git.side_effect = lambda wt, *a: (
                "abc" if a[0] == "merge-base" else
                "3" if a[0] == "rev-list" else "fix/1-x")
            self.assertIsNone(
                branch_ahead_without_completion("/wt", "o/r", 1))


class BranchAheadLinesTests(unittest.TestCase):
    """harmonic-forge#583 preclose finding: `branch_ahead_lines` (the
    factored-out per-cycle wiring `main()` calls) must be reachable with NO
    `--watch` argument at all -- it takes no `watch` parameter by design,
    which is the direct proof that the belt-and-suspenders `--worktrees
    ... --watch l1` command (the one real Lane 2 belt invocation) actually
    reaches this feature."""

    def test_reports_and_updates_state_with_no_watch_concept_involved(self):
        with patch("watch_lane_posts._run_git") as run_git, \
             patch("watch_lane_posts._fetch_all_comments", return_value=[]):
            run_git.side_effect = lambda wt, *a: (
                "abc" if a[0] == "merge-base" else
                "3" if a[0] == "rev-list" else "fix/1-x")
            resolutions = [("/wt", ("o/r", 1), "resolved")]
            last_ahead: dict[str, str | None] = {}
            lines = branch_ahead_lines(resolutions, last_ahead)
            self.assertEqual(len(lines), 1)
            self.assertIn("no completion posted", lines[0])
            self.assertEqual(last_ahead["/wt"], lines[0])

    def test_unresolved_worktree_is_dropped_from_state_and_silent(self):
        last_ahead = {"/wt": "a stale prior report"}
        lines = branch_ahead_lines([("/wt", None, "detached HEAD")], last_ahead)
        self.assertEqual(lines, [])
        self.assertNotIn("/wt", last_ahead)

    def test_unchanged_report_is_not_reprinted(self):
        with patch("watch_lane_posts._run_git") as run_git, \
             patch("watch_lane_posts._fetch_all_comments", return_value=[]):
            run_git.side_effect = lambda wt, *a: (
                "abc" if a[0] == "merge-base" else
                "3" if a[0] == "rev-list" else "fix/1-x")
            resolutions = [("/wt", ("o/r", 1), "resolved")]
            last_ahead: dict[str, str | None] = {}
            first = branch_ahead_lines(resolutions, last_ahead)
            second = branch_ahead_lines(resolutions, last_ahead)
            self.assertEqual(len(first), 1)
            self.assertEqual(second, [])


class ClassifyFencedBlockTests(unittest.TestCase):
    """harmonic-forge#583 preclose finding / this file's own R-0334
    (`rules/lane-shorthand.md`): a marker quoted as evidence inside a
    fenced code block must never be read as a real transition -- now
    load-bearing for LANE attribution, not just `kind`, since `posted-by`
    controls which lane a comment is credited to."""

    def test_marker_quoted_inside_a_fence_is_not_read_as_a_real_transition(self):
        body = (
            "Reviewing the belt's own docstring, which quotes its marker "
            "shape as an example:\n\n"
            "```\n"
            "<!-- l1-post v1; kind=completion; posted-by=LANE2 -->\n"
            "```\n\n"
            "No real transition happened here."
        )
        self.assertIsNone(_classify(body))

    def test_a_real_marker_outside_any_fence_still_classifies(self):
        body = ("## L2D — receipt-backed status (harmonic-forge#371)\n\ntext\n\n"
                "<!-- l1-post v1; kind=completion; posted-by=LANE2 -->")
        self.assertEqual(_classify(body), ("l2", "completion"))

    def test_quoted_marker_in_a_fence_does_not_silence_ac4s_gap_detector(self):
        """The concrete failure mode: a Lane 1 review comment pasting Lane
        2's completion footer as evidence must not make
        `branch_ahead_without_completion` believe a completion exists."""
        quoting_body = {"body": (
            "Evidence review:\n\n```\n"
            "<!-- l1-post v1; kind=completion; posted-by=LANE2 -->\n```\n"
        )}
        with patch("watch_lane_posts._run_git") as run_git, \
             patch("watch_lane_posts._fetch_all_comments", return_value=[quoting_body]):
            run_git.side_effect = lambda wt, *a: (
                "abc" if a[0] == "merge-base" else
                "3" if a[0] == "rev-list" else "fix/1-x")
            report = branch_ahead_without_completion("/wt", "o/r", 1)
            self.assertIsNotNone(report)
            self.assertIn("no completion posted", report)


class L1SweepTests(unittest.TestCase):
    """harmonic-forge#570 AC1/AC8 -- Lane 1's repo-wide newest-marker sweep,
    given a runnable form. Every `gh` call mocked, no network."""

    def _mock_gh(self, open_issues: list[int] | Exception, comments: dict,
                 issue_states: dict | None = None):
        issue_states = issue_states or {}

        def run(argv, **kwargs):
            self.assertEqual(argv[0], "gh-as")
            gh_argv = argv[3:]
            if any(a == "repos/vitalharmony/hrse/issues" for a in gh_argv):
                if isinstance(open_issues, Exception):
                    raise open_issues
                # harmonic-forge#570 preclose finding: `gh api` promotes a
                # call with `-f` params to POST unless told otherwise, and a
                # POST here is issue *creation* -- assert the explicit GET
                # so a regression back to an implicit POST fails this test
                # rather than silently returning nothing in production.
                self.assertIn("-X", gh_argv, f"issues list must be explicit GET: {gh_argv}")
                self.assertEqual(gh_argv[gh_argv.index("-X") + 1], "GET")
                return _fake_completed("\n".join(str(n) for n in open_issues))
            if "api" in gh_argv:
                # A single-issue state check (`_issue_is_open`,
                # harmonic-forge#579 AC4) has no `/comments` suffix -- a
                # comments-list call does. Distinguish on that before
                # falling through to the single-issue-state branch.
                comments_paths = [a for a in gh_argv if a.startswith("repos/") and "/comments" in a]
                if comments_paths:
                    path = comments_paths[0]
                    issue = int(path.rsplit("/", 2)[-2])
                    bodies = comments.get(issue, [])
                    return _fake_completed(json.dumps([{"body": b} for b in bodies]))
                single_issue_paths = [a for a in gh_argv
                                       if a.startswith("repos/") and "/issues/" in a]
                if single_issue_paths:
                    issue = int(single_issue_paths[0].rsplit("/", 1)[-1])
                    state = issue_states.get(issue, "open")
                    return _fake_completed(state)
            raise AssertionError(f"unexpected gh call: {argv}")
        return run

    def test_list_open_issues_parses_numbers(self):
        with patch("belt_mechanics.subprocess.run",
                  side_effect=self._mock_gh([12, 34, 56], {})):
            self.assertEqual(list_open_issues("vitalharmony/hrse"), [12, 34, 56])

    def test_list_open_issues_passes_since_as_query_param(self):
        captured = []

        def run(argv, **kwargs):
            captured.append(argv[3:])
            return _fake_completed("")
        with patch("belt_mechanics.subprocess.run", side_effect=run):
            list_open_issues("vitalharmony/hrse", since="2026-09-01T00:00:00Z")
        self.assertIn("-f", captured[0])
        self.assertIn("since=2026-09-01T00:00:00Z", captured[0])

    def test_list_open_issues_returns_none_on_fetch_failure(self):
        """harmonic-forge#579 AC1: `None` (fetch failed) is distinguishable
        from `[]` (fetch succeeded, genuinely zero open issues)."""
        with patch("belt_mechanics.subprocess.run",
                  side_effect=self._mock_gh(RuntimeError("502"), {})):
            self.assertIsNone(list_open_issues("vitalharmony/hrse"))

    def test_l1_sweep_still_checks_a_stale_already_queued_issue(self):
        """harmonic-forge#570 preclose finding: `since` must not drop an
        issue that stopped receiving updates but was never resolved -- it
        must stay in the candidate set via `extra_issues`."""
        with patch("belt_mechanics.subprocess.run",
                  side_effect=self._mock_gh(
                      [],  # nothing NEW since the watermark
                      {1530: ["## L2D — receipt-backed status (harmonic-forge#371)"]},
                  )):
            queue, fetch_ok = discover_l1_sweep(
                "vitalharmony/hrse", since="2026-09-01T00:00:00Z", extra_issues=[1530])
            self.assertEqual(list(queue), [1530])
            self.assertTrue(fetch_ok)

    def test_issue_whose_newest_comment_is_lane2_needs_l1(self):
        with patch("belt_mechanics.subprocess.run",
                  side_effect=self._mock_gh(
                      [1530],
                      {1530: ["## L2D — receipt-backed status (harmonic-forge#371)"]},
                  )):
            queue, fetch_ok = discover_l1_sweep("vitalharmony/hrse")
            self.assertEqual(list(queue), [1530])
            lane, detail = queue[1530]
            self.assertEqual(lane, "l2")
            self.assertTrue(fetch_ok)

    def test_issue_whose_newest_comment_is_lane1s_own_is_not_queued(self):
        body = "## Some post\n\n<!-- l1-post v1; kind=discussion; posted-by=LANE1 -->"
        with patch("belt_mechanics.subprocess.run",
                  side_effect=self._mock_gh([1530], {1530: [body]})):
            queue, _fetch_ok = discover_l1_sweep("vitalharmony/hrse")
            self.assertEqual(queue, {})

    def test_issue_with_no_classified_comment_carries_no_ball(self):
        with patch("belt_mechanics.subprocess.run",
                  side_effect=self._mock_gh([1530], {1530: ["just chat, no heading"]})):
            queue, _fetch_ok = discover_l1_sweep("vitalharmony/hrse")
            self.assertEqual(queue, {})

    def test_issue_with_zero_comments_carries_no_ball(self):
        with patch("belt_mechanics.subprocess.run",
                  side_effect=self._mock_gh([1530], {})):
            queue, _fetch_ok = discover_l1_sweep("vitalharmony/hrse")
            self.assertEqual(queue, {})

    def test_fetch_failure_reports_fetch_ok_false(self):
        """harmonic-forge#579 AC1 reproduction: a failed `list_open_issues`
        call must surface `fetch_ok=False` so the caller (`main()`) knows
        not to advance its watermark. `extra_issues` (already-queued issues)
        must still be checked even when the fresh fetch failed."""
        with patch("belt_mechanics.subprocess.run",
                  side_effect=self._mock_gh(
                      RuntimeError("secondary rate limit"),
                      {1530: ["## L2D — receipt-backed status (harmonic-forge#371)"]},
                  )):
            queue, fetch_ok = discover_l1_sweep(
                "vitalharmony/hrse", since="2026-09-01T00:00:00Z", extra_issues=[1530])
            self.assertFalse(fetch_ok)
            self.assertEqual(list(queue), [1530])

    def test_closed_extra_issue_is_dropped_from_the_queue(self):
        """harmonic-forge#579 AC4 reproduction: an issue only present via
        `extra_issues` (not returned by the fresh `state=open` fetch) that
        has since been closed must not be re-queued."""
        with patch("belt_mechanics.subprocess.run",
                  side_effect=self._mock_gh(
                      [],
                      {1530: ["## L2D — receipt-backed status (harmonic-forge#371)"]},
                      issue_states={1530: "closed"},
                  )):
            queue, _fetch_ok = discover_l1_sweep(
                "vitalharmony/hrse", since="2026-09-01T00:00:00Z", extra_issues=[1530])
            self.assertEqual(queue, {})

    def test_open_extra_issue_is_kept_in_the_queue(self):
        """The open-state re-check (AC4) must not falsely drop a still-open
        stale issue -- only a genuinely closed one."""
        with patch("belt_mechanics.subprocess.run",
                  side_effect=self._mock_gh(
                      [],
                      {1530: ["## L2D — receipt-backed status (harmonic-forge#371)"]},
                      issue_states={1530: "open"},
                  )):
            queue, _fetch_ok = discover_l1_sweep(
                "vitalharmony/hrse", since="2026-09-01T00:00:00Z", extra_issues=[1530])
            self.assertEqual(list(queue), [1530])

    def test_comment_fetch_failure_falls_back_to_previous_classification(self):
        """harmonic-forge#579 preclose finding: the open-state re-check
        alone was not enough -- `_fetch_all_comments` failing separately
        for the same issue dropped it right back out. A `Mapping`
        `extra_issues` carrying the previous `(lane, detail)` must be used
        as the fallback when this cycle's comment fetch for that issue
        fails, so a transient outage does not read as `left-queue-for-l1`."""
        def run(argv, **kwargs):
            self.assertEqual(argv[0], "gh-as")
            gh_argv = argv[3:]
            if any(a == "repos/vitalharmony/hrse/issues" for a in gh_argv):
                return _fake_completed("")  # nothing new since the watermark
            if "api" in gh_argv and any("/comments" in a for a in gh_argv):
                raise RuntimeError("secondary rate limit")
            raise AssertionError(f"unexpected gh call: {argv}")
        with patch("belt_mechanics.subprocess.run", side_effect=run):
            queue, fetch_ok = discover_l1_sweep(
                "vitalharmony/hrse", since="2026-09-01T00:00:00Z",
                extra_issues={1530: ("l2", "## L2D — receipt-backed status (harmonic-forge#371)")},
            )
            self.assertTrue(fetch_ok)
            self.assertEqual(queue, {1530: ("l2", "## L2D — receipt-backed status (harmonic-forge#371)")})

    def test_comment_fetch_failure_with_no_previous_value_is_excluded(self):
        """A freshly-discovered issue (no prior classification to fall back
        to) whose comment fetch fails is excluded, same as before this
        fix -- not a regression, just no `TypeError` from iterating `None`."""
        def run(argv, **kwargs):
            gh_argv = argv[3:]
            if any(a == "repos/vitalharmony/hrse/issues" for a in gh_argv):
                return _fake_completed("1530")
            if "api" in gh_argv and any("/comments" in a for a in gh_argv):
                raise RuntimeError("502")
            raise AssertionError(f"unexpected gh call: {argv}")
        with patch("belt_mechanics.subprocess.run", side_effect=run):
            queue, fetch_ok = discover_l1_sweep("vitalharmony/hrse")
            self.assertTrue(fetch_ok)
            self.assertEqual(queue, {})


class L1SweepCycleTests(unittest.TestCase):
    """harmonic-forge#579 AC1, preclose finding: `main()`'s own watermark
    gate (`if fetch_ok: l1_since = now`) was unexercised by any test --
    reverting it to unconditional left the full suite green. Factored into
    `l1_sweep_cycle` specifically so this is directly testable."""

    def _mock_gh(self, open_issues_or_exc):
        def run(argv, **kwargs):
            gh_argv = argv[3:]
            if any(a == "repos/vitalharmony/hrse/issues" for a in gh_argv):
                if isinstance(open_issues_or_exc, Exception):
                    raise open_issues_or_exc
                return _fake_completed("\n".join(str(n) for n in open_issues_or_exc))
            if "api" in gh_argv and any("/comments" in a for a in gh_argv):
                return _fake_completed("[]")
            raise AssertionError(f"unexpected gh call: {argv}")
        return run

    def test_successful_cycle_advances_the_watermark(self):
        with patch("belt_mechanics.subprocess.run", side_effect=self._mock_gh([])):
            _queue, new_since = l1_sweep_cycle(
                "vitalharmony/hrse", "2026-09-01T00:00:00Z", {}, "2026-09-09T00:00:00Z")
        self.assertEqual(new_since, "2026-09-09T00:00:00Z")

    def test_failed_cycle_does_not_advance_the_watermark(self):
        """The exact reproduction AC1 names: a failed fetch must leave
        `l1_since` where it was, not silently narrow the next window."""
        with patch("belt_mechanics.subprocess.run",
                  side_effect=self._mock_gh(RuntimeError("502"))):
            _queue, new_since = l1_sweep_cycle(
                "vitalharmony/hrse", "2026-09-01T00:00:00Z", {}, "2026-09-09T00:00:00Z")
        self.assertEqual(new_since, "2026-09-01T00:00:00Z")

    def test_last_queue_string_form_round_trips_through_the_cycle(self):
        """`main()`'s `last_queue` is `{issue: "lane:detail"}` -- confirm
        `l1_sweep_cycle` splits it back into the tuple form
        `discover_l1_sweep` expects, using it as the stale-issue fallback."""
        def run(argv, **kwargs):
            gh_argv = argv[3:]
            if any(a == "repos/vitalharmony/hrse/issues" for a in gh_argv):
                return _fake_completed("")
            if "api" in gh_argv and any("/comments" in a for a in gh_argv):
                raise RuntimeError("rate limit")
            raise AssertionError(f"unexpected gh call: {argv}")
        with patch("belt_mechanics.subprocess.run", side_effect=run):
            queue, _new_since = l1_sweep_cycle(
                "vitalharmony/hrse", "2026-09-01T00:00:00Z",
                {1530: "l2:## L2D — receipt-backed status (harmonic-forge#371)"},
                "2026-09-09T00:00:00Z",
            )
        self.assertEqual(queue, {1530: ("l2", "## L2D — receipt-backed status (harmonic-forge#371)")})


def _gate_result(verdict: str) -> str:
    return (f"## Lane 3 Gate Results — hrse#0000\n\n**Verdict:** {verdict}\n\n"
            "<!-- l1-post v1; kind=gate-result; posted-by=LANE3; body-sha256=x -->")


class L3UnansweredVerdictTests(unittest.TestCase):
    """harmonic-forge#629, Check C -- the regression hrse#1771 produced live:
    a FAIL gate-result got a real Lane 1 ruling, posted as `kind=discussion`,
    that neither Check A/B nor `discover_queue` ever surfaced because a
    `discussion` marker carries no queue membership."""

    def _mock_gh(self, open_issues, comments: dict, issue_states: dict | None = None):
        """`comments`: {issue: [(body, created_at), ...]}, oldest first --
        matches the real GitHub REST ordering `_fetch_all_comments` relies
        on."""
        issue_states = issue_states or {}

        def run(argv, **kwargs):
            gh_argv = argv[3:]
            if any(a == "repos/vitalharmony/hrse/issues" for a in gh_argv):
                if isinstance(open_issues, Exception):
                    raise open_issues
                return _fake_completed("\n".join(str(n) for n in open_issues))
            if "api" in gh_argv:
                comments_paths = [a for a in gh_argv if a.startswith("repos/") and "/comments" in a]
                if comments_paths:
                    issue = int(comments_paths[0].rsplit("/", 2)[-2])
                    rows = comments.get(issue, [])
                    return _fake_completed(json.dumps(
                        [{"body": b, "created_at": c} for b, c in rows]))
                single_issue_paths = [a for a in gh_argv
                                       if a.startswith("repos/") and "/issues/" in a]
                if single_issue_paths:
                    issue = int(single_issue_paths[0].rsplit("/", 1)[-1])
                    return _fake_completed(issue_states.get(issue, "open"))
            raise AssertionError(f"unexpected gh call: {argv}")
        return run

    def test_a_fail_verdict_with_a_later_reply_is_watched(self):
        with patch("belt_mechanics.subprocess.run", side_effect=self._mock_gh(
                [1771], {1771: [
                    (_gate_result("FAIL"), "2026-09-11T01:34:14Z"),
                    ("## L1 ruling\n\nsome text", "2026-09-11T03:35:20Z"),
                ]})):
            watching, fetch_ok = discover_l3_unanswered_verdicts("vitalharmony/hrse")
        self.assertEqual(watching, {1771: "FAIL"})
        self.assertTrue(fetch_ok)

    def test_a_blocked_verdict_with_a_later_reply_is_watched(self):
        with patch("belt_mechanics.subprocess.run", side_effect=self._mock_gh(
                [1792], {1792: [
                    (_gate_result("BLOCKED"), "2026-09-11T00:41:39Z"),
                    ("some later reply, no marker at all", "2026-09-11T00:50:00Z"),
                ]})):
            watching, _fetch_ok = discover_l3_unanswered_verdicts("vitalharmony/hrse")
        self.assertEqual(watching, {1792: "BLOCKED"})

    def test_a_pass_verdict_never_fires_even_with_a_later_reply(self):
        with patch("belt_mechanics.subprocess.run", side_effect=self._mock_gh(
                [1663], {1663: [
                    (_gate_result("PASS"), "2026-09-11T01:21:10Z"),
                    ("merged", "2026-09-11T01:21:30Z"),
                ]})):
            watching, _fetch_ok = discover_l3_unanswered_verdicts("vitalharmony/hrse")
        self.assertEqual(watching, {})

    def test_a_fail_verdict_with_no_later_comment_does_not_fire_yet(self):
        with patch("belt_mechanics.subprocess.run", side_effect=self._mock_gh(
                [1771], {1771: [(_gate_result("FAIL"), "2026-09-11T01:34:14Z")]})):
            watching, _fetch_ok = discover_l3_unanswered_verdicts("vitalharmony/hrse")
        self.assertEqual(watching, {})

    def test_an_issue_with_no_gate_result_at_all_is_excluded(self):
        with patch("belt_mechanics.subprocess.run", side_effect=self._mock_gh(
                [1530], {1530: [("just chat, no marker", "2026-09-11T00:00:00Z")]})):
            watching, _fetch_ok = discover_l3_unanswered_verdicts("vitalharmony/hrse")
        self.assertEqual(watching, {})

    def test_only_the_last_gate_result_matters_a_prior_fail_superseded_by_a_pass_does_not_fire(self):
        with patch("belt_mechanics.subprocess.run", side_effect=self._mock_gh(
                [1000], {1000: [
                    (_gate_result("FAIL"), "2026-09-01T00:00:00Z"),
                    ("fix pushed", "2026-09-01T01:00:00Z"),
                    (_gate_result("PASS"), "2026-09-01T02:00:00Z"),
                    ("merged", "2026-09-01T03:00:00Z"),
                ]})):
            watching, _fetch_ok = discover_l3_unanswered_verdicts("vitalharmony/hrse")
        self.assertEqual(watching, {})

    def test_the_reply_kind_is_irrelevant_a_discussion_marker_still_fires(self):
        """The regression's own shape: the reply carries `kind=discussion`,
        which `discover_queue` would never treat as queue membership -- but
        Check C does not classify the reply at all, only its timestamp."""
        with patch("belt_mechanics.subprocess.run", side_effect=self._mock_gh(
                [1771], {1771: [
                    (_gate_result("FAIL"), "2026-09-11T01:34:14Z"),
                    ("## L1 ruling\n\n<!-- l1-post v1; kind=discussion; posted-by=LANE1 -->",
                     "2026-09-11T03:35:20Z"),
                ]})):
            watching, _fetch_ok = discover_l3_unanswered_verdicts("vitalharmony/hrse")
        self.assertEqual(watching, {1771: "FAIL"})

    def test_fetch_failure_reports_fetch_ok_false(self):
        with patch("belt_mechanics.subprocess.run",
                  side_effect=self._mock_gh(RuntimeError("502"), {})):
            watching, fetch_ok = discover_l3_unanswered_verdicts(
                "vitalharmony/hrse", since="2026-09-01T00:00:00Z", extra_issues=[1771])
            self.assertFalse(fetch_ok)

    def test_extra_issues_keeps_a_stale_watched_issue_in_the_candidate_set(self):
        with patch("belt_mechanics.subprocess.run", side_effect=self._mock_gh(
                [],  # nothing NEW since the watermark
                {1771: [
                    (_gate_result("FAIL"), "2026-09-11T01:34:14Z"),
                    ("a reply", "2026-09-11T03:35:20Z"),
                ]})):
            watching, fetch_ok = discover_l3_unanswered_verdicts(
                "vitalharmony/hrse", since="2026-09-11T02:00:00Z", extra_issues=[1771])
        self.assertEqual(watching, {1771: "FAIL"})
        self.assertTrue(fetch_ok)

    def test_a_closed_extra_issue_is_dropped(self):
        with patch("belt_mechanics.subprocess.run", side_effect=self._mock_gh(
                [], {1771: [
                    (_gate_result("FAIL"), "2026-09-11T01:34:14Z"),
                    ("a reply", "2026-09-11T03:35:20Z"),
                ]}, issue_states={1771: "closed"})):
            watching, _fetch_ok = discover_l3_unanswered_verdicts(
                "vitalharmony/hrse", since="2026-09-11T02:00:00Z", extra_issues=[1771])
        self.assertEqual(watching, {})


class L3VerdictSweepCycleTests(unittest.TestCase):
    """Mirrors `L1SweepCycleTests` -- the watermark discipline is identical."""

    def _mock_gh(self, open_issues_or_exc):
        def run(argv, **kwargs):
            gh_argv = argv[3:]
            if any(a == "repos/vitalharmony/hrse/issues" for a in gh_argv):
                if isinstance(open_issues_or_exc, Exception):
                    raise open_issues_or_exc
                return _fake_completed("\n".join(str(n) for n in open_issues_or_exc))
            if "api" in gh_argv and any("/comments" in a for a in gh_argv):
                return _fake_completed("[]")
            raise AssertionError(f"unexpected gh call: {argv}")
        return run

    def test_successful_cycle_advances_the_watermark(self):
        with patch("belt_mechanics.subprocess.run", side_effect=self._mock_gh([])):
            _watching, new_since = l3_verdict_sweep_cycle(
                "vitalharmony/hrse", "2026-09-01T00:00:00Z", {}, "2026-09-09T00:00:00Z")
        self.assertEqual(new_since, "2026-09-09T00:00:00Z")

    def test_failed_cycle_does_not_advance_the_watermark(self):
        with patch("belt_mechanics.subprocess.run",
                  side_effect=self._mock_gh(RuntimeError("502"))):
            _watching, new_since = l3_verdict_sweep_cycle(
                "vitalharmony/hrse", "2026-09-01T00:00:00Z", {}, "2026-09-09T00:00:00Z")
        self.assertEqual(new_since, "2026-09-01T00:00:00Z")


class QueueCycleL3SweepTests(unittest.TestCase):
    """`queue_cycle(..., sweep=True)` dispatches to Check C when `lane ==
    "l3"`, producing the `needs-l1-response` line shape -- distinct from
    `needs-l1` (Check A's own l1-sweep line), so an operator or a Monitor
    parsing belt output can tell the two backstops apart."""

    def _mock_gh(self, comments: dict):
        def run(argv, **kwargs):
            gh_argv = argv[3:]
            if any(a == "repos/vitalharmony/hrse/issues" for a in gh_argv):
                return _fake_completed("\n".join(str(n) for n in comments))
            if "api" in gh_argv:
                comments_paths = [a for a in gh_argv if a.startswith("repos/") and "/comments" in a]
                if comments_paths:
                    issue = int(comments_paths[0].rsplit("/", 2)[-2])
                    rows = comments.get(issue, [])
                    return _fake_completed(json.dumps(
                        [{"body": b, "created_at": c} for b, c in rows]))
            raise AssertionError(f"unexpected gh call: {argv}")
        return run

    def test_needs_l1_response_line_is_emitted_for_a_new_fail_verdict(self):
        with patch("belt_mechanics.subprocess.run", side_effect=self._mock_gh(
                {1771: [
                    (_gate_result("FAIL"), "2026-09-11T01:34:14Z"),
                    ("## L1 ruling\n\n<!-- l1-post v1; kind=discussion; posted-by=LANE1 -->",
                     "2026-09-11T03:35:20Z"),
                ]})):
            queue, lines, ok_repos = watch_lane_posts.queue_cycle(
                ["vitalharmony/hrse"], "l3", {}, {}, "2026-09-11T04:00:00Z", sweep=True)
        self.assertEqual(queue[("vitalharmony/hrse", 1771)], "l3verdict:FAIL")
        self.assertIn("vitalharmony/hrse#1771 needs-l1-response last-verdict=FAIL", lines)
        self.assertIn("vitalharmony/hrse", ok_repos)

    def test_an_already_known_verdict_produces_no_duplicate_line(self):
        """Self-clearing, same as every other queue: a verdict already
        reported on a previous cycle does not re-print every tick."""
        with patch("belt_mechanics.subprocess.run", side_effect=self._mock_gh(
                {1771: [
                    (_gate_result("FAIL"), "2026-09-11T01:34:14Z"),
                    ("a reply", "2026-09-11T03:35:20Z"),
                ]})):
            _queue, lines, _ok = watch_lane_posts.queue_cycle(
                ["vitalharmony/hrse"], "l3",
                {("vitalharmony/hrse", 1771): "l3verdict:FAIL"}, {},
                "2026-09-11T04:00:00Z", sweep=True)
        self.assertEqual(lines, [])


class BeltSkillDocSyncTests(unittest.TestCase):
    """harmonic-forge#579 AC3, preclose finding: the doc-vs-code drift this
    AC exists to fix was previously checkable only by eyeballing --
    `skills/belt-and-suspenders/test_skill_text.py` deliberately excludes
    the "Fires on" line from its own assertions (see its `_prose_only`),
    and that file is not wired into `tools/run_tests.py`'s `TEST_DIRS`
    (pre-existing gap, out of scope here) regardless. This asserts the
    property mechanically, from a file `mise run test` does collect."""

    def test_lane_2_fires_on_line_matches_queue_kinds_l2_exactly(self):
        text = _SKILL_MD.read_text(encoding="utf-8")
        match = re.search(r"^Fires on (.+?)\.\s", text, re.MULTILINE)
        self.assertIsNotNone(match, "SKILL.md's Lane 2 'Fires on ...' line was not found")
        listed = set(re.findall(r"`([\w-]+)`", match.group(1)))
        self.assertEqual(listed, set(QUEUE_KINDS["l2"]),
                         "SKILL.md's Lane 2 'Fires on ...' line must list exactly "
                         "QUEUE_KINDS['l2'], no more and no less")


class EnumerateWorktreesTests(unittest.TestCase):
    """harmonic-forge#590: Lane 1's belt is worktrees-first, and the worktree
    set is not static, so it is read from git rather than hardcoded."""

    _PORCELAIN = (
        "worktree /home/u/harmonic-forge\n"
        "HEAD abc123\n"
        "branch refs/heads/main\n"
        "\n"
        "worktree /tmp/hrse2-1676-impl\n"
        "HEAD def456\n"
        "branch refs/heads/feat/1676-backfill\n"
        "\n"
        "worktree /home/u/HRSE2-lane3\n"
        "HEAD 789abc\n"
        "detached\n"
        "\n"
    )

    def test_parses_every_worktree_path(self):
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=self._PORCELAIN, stderr="")
        with patch("watch_lane_posts.subprocess.run", return_value=completed):
            self.assertEqual(
                enumerate_worktrees(),
                ["/home/u/harmonic-forge", "/tmp/hrse2-1676-impl",
                 "/home/u/HRSE2-lane3"])

    def test_ephemeral_impl_worktree_is_included(self):
        """The whole point: a `/tmp/<repo>-<issue>-impl` checkout that no
        hardcoded --worktrees list could have named is discovered."""
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=self._PORCELAIN, stderr="")
        with patch("watch_lane_posts.subprocess.run", return_value=completed):
            self.assertIn("/tmp/hrse2-1676-impl", enumerate_worktrees())

    def test_returns_empty_on_git_failure_rather_than_raising(self):
        for exc in (subprocess.CalledProcessError(128, "git"),
                    subprocess.TimeoutExpired("git", 15),
                    OSError("git not found")):
            with self.subTest(exc=type(exc).__name__):
                with patch("watch_lane_posts.subprocess.run", side_effect=exc):
                    self.assertEqual(enumerate_worktrees(), [])

    def test_not_a_repo_yields_no_targets(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(enumerate_worktrees(cwd=tmp), [])


class BeltLane1IsWorktreesFirstTests(unittest.TestCase):
    """harmonic-forge#590 AC1/AC2: SKILL.md's Lane 1 belt watches the live
    worktrees. harmonic-forge#640 retired the repo-wide sweep for Lane 1
    entirely (operator ruling: worktrees/queued-plans discover, gh only
    enriches) -- it is no longer a suspenders backstop either. Asserted
    mechanically because the regression that produced #590 was a prose edit
    that read plausibly."""

    def _lane1_belt_command(self) -> str:
        text = _SKILL_MD.read_text(encoding="utf-8")
        match = re.search(r"^- \*\*Lane 1\*\*.*?```\n(.*?)```",
                          text, re.MULTILINE | re.DOTALL)
        self.assertIsNotNone(match, "SKILL.md's Lane 1 belt command block was not found")
        return match.group(1)

    def test_lane1_belt_arms_all_worktrees(self):
        self.assertIn("--all-worktrees", self._lane1_belt_command())

    def test_lane1_belt_does_not_arm_the_repo_wide_sweep(self):
        """#590's property, restated for #618's flag split.

        The forbidden thing is the UNBOUNDED sweep (`--sweep-for l1`), not
        `--queue-for l1` -- which since #618 means the same bounded thing for
        Lane 1 that it always meant for Lane 2 and Lane 3, and which Lane 1's
        belt now legitimately arms to see a plan awaiting PROCEED."""
        self.assertNotIn("--sweep-for", self._lane1_belt_command(),
                         "the repo-wide sweep is the suspenders' backstop "
                         "(harmonic-forge#590); arming it as the belt is the "
                         "regression that issue fixed")

    def test_lane1_belt_arms_its_bounded_inbound_queue(self):
        """#618. A Plan-First issue has no worktree until Lane 1 approves the
        plan, so a worktrees-only belt cannot see the most time-sensitive thing
        Lane 1 owes. Four plans stalled exactly there."""
        self.assertIn("--queue-for l1", self._lane1_belt_command())

    #: Tokens after `--all-worktrees` that are repo roots: anything up to the
    #: next flag. `\S+` is NOT enough and was the shipped defect (#594 preclose
    #: finding) -- it matches `--watch` and `l2`, so the bare CWD-dependent
    #: form this test exists to forbid satisfied a two-group match and passed.
    _ROOTS_RE = re.compile(r"--all-worktrees((?:\s+(?!-)\S+)*)")

    def _lane1_repo_roots(self) -> list[str]:
        match = self._ROOTS_RE.search(self._lane1_belt_command())
        self.assertIsNotNone(match, "the Lane 1 belt must arm --all-worktrees")
        return match.group(1).split()

    def test_lane1_belt_does_not_seed_roots_through_worktrees(self):
        """AC3: --worktrees names worktrees to watch, nothing else."""
        self.assertNotIn("--worktrees", self._lane1_belt_command())

    def test_lane1_belt_carries_no_run_it_from_here_caveat(self):
        """AC1: a command that needs a caveat is not a command a skill can
        arm. If there is nothing to warn about, there is no warning."""
        text = _SKILL_MD.read_text(encoding="utf-8")
        lane1 = text.split("- **Lane 1**", 1)[1].split("- **Lane 2**", 1)[0]
        self.assertNotIn("Run it from the HRSE2 checkout", lane1)

    def test_no_armed_command_anywhere_arms_lane1s_repo_wide_sweep(self):
        """harmonic-forge#640 preclose finding 1: scoping this to "the
        suspenders section" left every heading before it unchecked -- the
        exact retired command re-armed immediately above `## The suspenders`
        (or anywhere else in the file outside a historical mention) passed
        both doc-sync suites green under the old, narrower scoping. Checked
        FILE-WIDE, not section-scoped: the operator ruled the unbounded sweep
        out for Lane 1 entirely, not just out of one section."""
        text = _SKILL_MD.read_text(encoding="utf-8")
        armed = [line for line in text.splitlines()
                 if "watch_lane_posts.py" in line and "--sweep-for l1" in line]
        self.assertFalse(armed,
                         "harmonic-forge#640 retired the repo-wide sweep for "
                         "Lane 1; no command arming it may remain anywhere in "
                         f"SKILL.md, found: {armed!r}")

    def test_discover_l1_sweep_is_kept_not_deleted(self):
        """harmonic-forge#640: retired from Lane 1's own practice, not deleted
        as a tool -- Lane 3's Check C reuses its shape (`discover_l3_
        unanswered_verdicts`), so the function and the doc's mention of it
        both survive."""
        self.assertTrue(callable(discover_l1_sweep))
        self.assertIn("discover_l1_sweep", _SKILL_MD.read_text(encoding="utf-8"))


class DropClosedTargetsTests(unittest.TestCase):
    """harmonic-forge#590 preclose finding 1: an abandoned `/tmp/*-impl`
    checkout for a merged issue reads as "1 commit ahead with no completion
    posted" forever, because main took the work as a squash merge."""

    def setUp(self):
        watch_lane_posts._CLOSED_SEEN.clear()

    def _rows(self):
        return [("/tmp/hf-568-impl", ("vitalharmony/harmonic-forge", 568), "resolved"),
                ("/tmp/hrse2-1780-impl", ("vitalharmony/hrse", 1780), "resolved"),
                ("/home/u/HRSE2-lane3", None, "detached HEAD with no branch name")]

    def test_closed_issue_target_is_demoted_and_named(self):
        with patch("watch_lane_posts._issue_is_open",
                   side_effect=lambda repo, issue: issue != 568):
            kept = drop_closed_targets(self._rows())
        by_path = {path: (pair, reason) for path, pair, reason in kept}
        self.assertIsNone(by_path["/tmp/hf-568-impl"][0])
        self.assertIn("closed", by_path["/tmp/hf-568-impl"][1])
        self.assertIn("abandoned worktree", by_path["/tmp/hf-568-impl"][1])

    def test_open_issue_target_survives_untouched(self):
        with patch("watch_lane_posts._issue_is_open", return_value=True):
            kept = drop_closed_targets(self._rows())
        self.assertEqual(kept, self._rows())

    def test_already_unresolved_row_is_passed_through_not_re_asked(self):
        with patch("watch_lane_posts._issue_is_open", return_value=True) as is_open:
            drop_closed_targets([("/home/u/HRSE2-lane3", None, "detached HEAD")])
        is_open.assert_not_called()

    def test_closed_verdict_is_cached_so_the_belt_does_not_re_ask_every_cycle(self):
        rows = [("/tmp/hf-568-impl", ("vitalharmony/harmonic-forge", 568), "resolved")]
        with patch("watch_lane_posts._issue_is_open", return_value=False) as is_open:
            drop_closed_targets(rows)
            drop_closed_targets(rows)
            drop_closed_targets(rows)
        self.assertEqual(is_open.call_count, 1)


class EnumerateRepoRootsTests(unittest.TestCase):
    """harmonic-forge#590 preclose finding 3 and harmonic-forge#594: roots are
    NAMED, a root contributing nothing is never silent, and two spellings of
    one repository collapse to one identity."""

    def test_two_distinct_repos_are_unioned(self):
        with patch("watch_lane_posts._git_common_dir",
                   side_effect=["/a/.git", "/b/.git"]), \
             patch("watch_lane_posts.enumerate_worktrees",
                   side_effect=[["/a", "/tmp/a-1-impl"], ["/b"]]):
            self.assertEqual(enumerate_repo_roots(["/a", "/b"]),
                             ["/a", "/b", "/tmp/a-1-impl"])

    def test_no_roots_means_the_repo_containing_cwd(self):
        """The bare flag keeps its #590 meaning."""
        with patch("watch_lane_posts._git_common_dir",
                   return_value="/a/.git") as common, \
             patch("watch_lane_posts.enumerate_worktrees", return_value=["/a"]):
            self.assertEqual(enumerate_repo_roots([]), ["/a"])
        common.assert_called_once_with(None)

    def test_named_roots_do_not_also_enumerate_cwd(self):
        """AC1: the result must not depend on where the command was run."""
        with patch("watch_lane_posts._git_common_dir",
                   return_value="/a/.git") as common, \
             patch("watch_lane_posts.enumerate_worktrees", return_value=["/a"]):
            enumerate_repo_roots(["/a"])
        self.assertEqual([c.args[0] for c in common.call_args_list], ["/a"],
                         "CWD must not be enumerated when roots are named -- "
                         "that is the cwd dependence #594 removes")

    def test_two_spellings_of_one_repo_collapse_and_are_reported(self):
        """AC4: `~/harmonic-forge` is a symlink to the real checkout."""
        with patch("watch_lane_posts._git_common_dir", return_value="/real/.git"), \
             patch("watch_lane_posts.enumerate_worktrees", return_value=["/real"]), \
             patch("sys.stderr", new_callable=io.StringIO) as err:
            self.assertEqual(
                enumerate_repo_roots(["/home/u/hf", "/home/u/Projects/hf"]), ["/real"])
        self.assertIn("same repository", err.getvalue())

    def test_same_repo_is_enumerated_once_not_twice(self):
        with patch("watch_lane_posts._git_common_dir", return_value="/a/.git"), \
             patch("watch_lane_posts.enumerate_worktrees",
                   return_value=["/a"]) as enum:
            enumerate_repo_roots(["/a", "/a-sibling"])
        self.assertEqual(enum.call_count, 1)

    def test_named_root_that_is_not_a_repo_raises(self):
        """AC5: an asserted root contributing nothing is a typo, and arming a
        narrower belt than was asked for is what this protocol refuses."""
        with patch("watch_lane_posts._git_common_dir",
                   side_effect=["/a/.git", None]), \
             patch("watch_lane_posts.enumerate_worktrees", return_value=["/a"]):
            with self.assertRaises(RootNotARepo) as caught:
                enumerate_repo_roots(["/a", "/not/a/repo"])
        self.assertIn("/not/a/repo", str(caught.exception))

    def test_cwd_that_is_not_a_repo_only_warns(self):
        """Nobody asserted CWD was a repo, so it is not a typo -- warn, and
        say what to do instead."""
        with patch("watch_lane_posts._git_common_dir", return_value=None), \
             patch("sys.stderr", new_callable=io.StringIO) as err:
            self.assertEqual(enumerate_repo_roots([]), [])
        self.assertIn("not a git repo", err.getvalue())
        self.assertIn("--all-worktrees <path>", err.getvalue())

    def test_each_root_reports_its_own_contribution(self):
        """An aggregate count cannot show that one root added zero."""
        with patch("watch_lane_posts._git_common_dir",
                   side_effect=["/a/.git", "/b/.git"]), \
             patch("watch_lane_posts.enumerate_worktrees",
                   side_effect=[["/a", "/tmp/a-1-impl"], []]), \
             patch("sys.stderr", new_callable=io.StringIO) as err:
            enumerate_repo_roots(["/a", "/b"])
        self.assertIn("root /a: 2 worktree(s)", err.getvalue())
        self.assertIn("root /b: 0 worktree(s)", err.getvalue())


class GitCommonDirTests(unittest.TestCase):
    """AC4: repo identity must survive a symlinked path."""

    def test_symlinked_repo_resolves_to_the_real_common_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            real = Path(tmp) / "real"
            (real / ".git").mkdir(parents=True)
            link = Path(tmp) / "link"
            link.symlink_to(real)
            completed = subprocess.CompletedProcess(
                args=[], returncode=0, stdout=f"{link}/.git\n", stderr="")
            with patch("watch_lane_posts.subprocess.run", return_value=completed):
                self.assertEqual(watch_lane_posts._git_common_dir(str(link)),
                                 str(real / ".git"))

    def test_not_a_repo_is_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(watch_lane_posts._git_common_dir(tmp))


class BeltNeverSilentDocTests(unittest.TestCase):
    """harmonic-forge#590 preclose finding 4: the never-silent guarantee
    attributed Lane 1's belt to `--queue-for`, a branch it no longer enters."""

    def test_guarantee_does_not_claim_lane1_uses_queue_for_above(self):
        text = _SKILL_MD.read_text(encoding="utf-8")
        para = re.search(r"\*\*A monitor that never printed.*?\n\n",
                         text, re.DOTALL)
        self.assertIsNotNone(para, "the never-silent guarantee paragraph was not found")
        self.assertNotIn("every Lane 1 and Lane 3 command above", para.group(0),
                         "Lane 1's belt is worktrees-first and never enters the "
                         "--queue-for branch this sentence points at")

    def test_guarantee_covers_lane1s_belt_under_worktrees(self):
        text = _SKILL_MD.read_text(encoding="utf-8")
        para = re.search(r"\*\*A monitor that never printed.*?\n\n",
                         text, re.DOTALL)
        self.assertIn("Lane 1's belt", para.group(0))


class EveryLaneBeltDerivesItsRepoSetTests(unittest.TestCase):
    """harmonic-forge#596. Asserted PER LANE because #590/#594 fixed Lane 1 and
    left Lanes 2 and 3 carrying the same defects -- a guard written for one lane
    proves nothing about the other two, which is how that shipped.

    The property is DERIVATION, not a list. An earlier cut of this issue named
    four repos in the commands and pinned them with a test; that fixed the
    instance and made the wrong answer permanent, against R-0122's standing
    requirement that the repo set be derived. The manifest is the source."""

    _LANES = ("Lane 1", "Lane 2", "Lane 3")

    def _lane_block(self, lane: str) -> str:
        text = _SKILL_MD.read_text(encoding="utf-8")
        after = text.split(f"- **{lane}**", 1)
        self.assertEqual(len(after), 2, f"{lane}'s section was not found")
        for terminator in ("- **Lane 2**", "- **Lane 3**", "**A monitor that never"):
            if terminator in after[1]:
                return after[1].split(terminator, 1)[0]
        return after[1]

    def _commands(self, lane: str) -> list[str]:
        return re.findall(r"```\n(.*?)```", self._lane_block(lane), re.DOTALL)

    def test_every_lane_derives_its_repo_set_from_the_manifest(self):
        for lane in self._LANES:
            with self.subTest(lane=lane):
                joined = " ".join(self._commands(lane))
                self.assertIn("--account-repos", joined,
                              f"{lane} must derive its repo set (R-0122), not list it")

    def test_no_lane_command_hardcodes_a_repo(self):
        """The failure this replaces: naming repos fixed the instance and left
        'which repos?' a question someone answers correctly forever."""
        for lane in self._LANES:
            with self.subTest(lane=lane):
                joined = " ".join(self._commands(lane))
                self.assertNotIn("--repo vitalharmony/", joined)
                for hardcoded in ("~/Harmonic_Projects/HRSE2", "~/harmonic-forge ",
                                  "cymagraph-infra", "openclaw-projects"):
                    self.assertNotIn(hardcoded, joined,
                                     f"{lane} hardcodes {hardcoded!r}")

    def test_lane_2_keeps_queue_discovery_as_well_as_worktrees(self):
        """#596 preclose finding 1. Lane 2's inbound handoff has NO worktree --
        Lane 2 creates it only after picking the issue up -- so worktrees-only
        made every inbound handoff invisible, which is the belt's whole job for
        this lane. Lane 1's belt can be worktrees-only; that asymmetry is
        load-bearing and does not transfer."""
        joined = " ".join(self._commands("Lane 2"))
        self.assertIn("--all-worktrees", joined)
        self.assertIn("--queue-for l2", joined)

    def test_every_lane_command_is_a_single_unwrapped_line(self):
        """A backslash-continued command is not copy-pasteable, and these are
        armed verbatim by the skill."""
        for lane in self._LANES:
            with self.subTest(lane=lane):
                for cmd in self._commands(lane):
                    self.assertNotIn("\\\n", cmd)

    def test_no_lane_command_names_a_static_worktree_path(self):
        """A hardcoded worktree list is what #590 removed; it goes stale the
        moment an ephemeral /tmp checkout appears, and silently."""
        for lane in self._LANES:
            with self.subTest(lane=lane):
                for cmd in self._commands(lane):
                    self.assertNotIn("--worktrees ", cmd)

    def test_the_suspenders_section_arms_no_separate_lane1_command(self):
        """harmonic-forge#640: Check 2 used to arm its own `--sweep-for l1`
        command outside any lane bullet, so the per-lane guard above never
        reached it -- that command is retired, and Check 2 now points back at
        the belt's own `--all-worktrees --queue-for l1` (already covered by
        `test_every_lane_derives_its_repo_set_from_the_manifest`) rather than
        repeating a second, unbounded one."""
        text = _SKILL_MD.read_text(encoding="utf-8")
        suspenders_section = text.split("## The suspenders", 1)[1].split("## Role: Lane 1", 1)[0]
        self.assertNotIn("--sweep-for l1", suspenders_section,
                         "the repo-wide sweep is retired for Lane 1; no command "
                         "arming it should remain in the suspenders section")


class DiscoverQueueFailsClosedPerIssueTests(unittest.TestCase):
    """harmonic-forge#602 — found by an out-of-family (Codex/gpt-5.6-sol)
    review of the design assessment, after four in-family passes missed it.

    `_fetch_all_comments` returns None on a FAILED fetch. `discover_queue`
    consumed that as `or ()` and still reported `fetch_ok=True`, so
    `queue_cycle` counted the repo as reporting and retracted the issue with
    `left-queue-for-<lane>` -- telling the lane the ball moved on because one
    comment fetch hit a rate limit."""

    def test_one_issue_comment_fetch_failure_marks_the_repo_unreliable(self):
        with patch("watch_lane_posts._fetch_all_comments", return_value=None):
            queued, fetch_ok = discover_queue("vitalharmony/hrse", "l3", {1530})
        self.assertFalse(fetch_ok, "a failed comment fetch must not report success")
        self.assertEqual(queued, {})

    def test_a_genuinely_empty_comment_list_still_reports_success(self):
        """`[]` means zero comments and must stay distinguishable from None --
        otherwise the fix trades a false retraction for a stuck queue."""
        with patch("watch_lane_posts._fetch_all_comments", return_value=[]):
            queued, fetch_ok = discover_queue("vitalharmony/hrse", "l3", {1530})
        self.assertTrue(fetch_ok)
        self.assertEqual(queued, {})

    def test_queue_cycle_does_not_retract_on_an_issue_level_failure(self):
        """The end-to-end property, with `discover_queue` NOT mocked.

        harmonic-forge#602 preclose finding: the first version of this test
        patched `discover_queue` -- the unit under change -- so it passed with
        the entire fix reverted. It verified `queue_cycle`'s carry-forward
        against a value the test itself supplied. Patching one level lower, at
        `_fetch_all_comments`, is what connects the two halves."""
        last = {("vitalharmony/hrse", 1530): "ready-for-l3"}
        with patch("watch_lane_posts._fetch_all_comments", return_value=None):
            queue, lines, ok = watch_lane_posts.queue_cycle(
                ["vitalharmony/hrse"], "l3", last, {}, "2026-09-10T00:00:00Z",
                candidate_pairs={("vitalharmony/hrse", 1530)})
        self.assertEqual(queue, last, "the prior queue must carry forward")
        self.assertEqual(ok, set(), "a failed repo must not be counted as reporting")
        self.assertFalse([line for line in lines if "left-queue" in line],
                         f"a transient failure must not retract: {lines!r}")

    def test_a_still_queued_issue_is_not_retracted_and_a_checked_gone_one_is(self):
        """The fix must not buy safety by never retracting anything: an
        issue this cycle actually CHECKED (its number is in candidate_pairs)
        and found superseded still retracts."""
        last = {("vitalharmony/hrse", 1530): "ready-for-l3",
                ("vitalharmony/hrse", 1600): "ready-for-l3"}
        queued_body = "body\n<!-- l1-post v1; kind=ready-for-l3; posted-by=LANE-unset -->"
        superseded = [
            {"body": queued_body},
            {"body": "## Lane 3 Gate Results — PASS"},
        ]
        def fake_comments(repo, issue):
            return [{"body": queued_body}] if issue == 1530 else superseded
        with patch("watch_lane_posts._fetch_all_comments", side_effect=fake_comments):
            queue, lines, ok = watch_lane_posts.queue_cycle(
                ["vitalharmony/hrse"], "l3", last, {}, "2026-09-10T00:00:00Z",
                candidate_pairs={("vitalharmony/hrse", 1530),
                                 ("vitalharmony/hrse", 1600)})
        self.assertEqual(ok, {"vitalharmony/hrse"})
        self.assertIn(("vitalharmony/hrse", 1530), queue)
        self.assertNotIn(("vitalharmony/hrse", 1600), queue)
        self.assertIn("vitalharmony/hrse#1600 left-queue-for-l3", lines)

    def test_a_queued_issue_whose_candidate_disappears_carries_forward_not_retracted(self):
        """harmonic-forge#686 preclose finding 2. 1600 was queued last cycle
        but its worktree (or --issues) no longer names it this cycle -- it
        is simply not in `candidate_pairs`, so `discover_queue` never looked
        at it. That must read as "unknown", the same as a failed fetch, not
        as "verified gone" -- a worktree disappearing must not silently
        retract a still-live marker."""
        last = {("vitalharmony/hrse", 1530): "ready-for-l3",
                ("vitalharmony/hrse", 1600): "ready-for-l3"}
        body = "body\n<!-- l1-post v1; kind=ready-for-l3; posted-by=LANE-unset -->"
        with patch("watch_lane_posts._fetch_all_comments",
                   return_value=[{"body": body}]):
            queue, lines, ok = watch_lane_posts.queue_cycle(
                ["vitalharmony/hrse"], "l3", last, {}, "2026-09-10T00:00:00Z",
                candidate_pairs={("vitalharmony/hrse", 1530)})
        self.assertEqual(ok, {"vitalharmony/hrse"})
        self.assertIn(("vitalharmony/hrse", 1530), queue)
        self.assertIn(("vitalharmony/hrse", 1600), queue,
                       "an uninspected candidate must carry forward, not vanish")
        self.assertFalse([l for l in lines if "left-queue" in l],
                          f"an uninspected candidate must not retract: {lines!r}")


class RecordedOnlyCandidateCarryForwardTests(unittest.TestCase):
    """harmonic-forge#691 preclose finding 2. The worktree-scan carry-forward
    above (`test_a_queued_issue_whose_candidate_disappears_carries_forward_
    not_retracted`) is correct for a candidate set derived from a partial
    scan. It is WRONG for a belt whose candidate set is `read_queue_
    candidates` alone (Lane 3's canonical belt: `--queue-for l3`, no
    `--worktrees`/`--issues`/`--all-worktrees`) -- there, that source is a
    full re-read of the candidates directory every tick, so an issue's
    absence from it IS the decisive "no longer queue-eligible" signal, not
    an artifact of a partial scan. `recorded_only=True` is the caller's
    (arm-time) declaration of that property; this reproduces the reporter's
    exact 3-tick repro end to end, `discover_queue` unmocked."""

    _READY = "body\n<!-- l1-post v1; kind=ready-for-l3; posted-by=LANE-unset -->"

    def test_ineligible_recorded_candidate_leaves_queue_not_carried_forward(self):
        """Tick 1: 1530 is a candidate (ready-for-l3 recorded) and checks out
        live -> queued. Tick 2: the file behind it was overwritten with an
        ineligible kind (Lane 3 posted its own gate-result) -- simulated
        here by the issue simply no longer appearing in `candidate_pairs`,
        exactly what `read_queue_candidates` would now return. With
        `recorded_only=True` this must NOT carry forward; it must leave the
        queue."""
        last: dict = {}
        with patch("watch_lane_posts._fetch_all_comments",
                   return_value=[{"body": self._READY}]):
            queue1, lines1, ok1 = watch_lane_posts.queue_cycle(
                ["vitalharmony/hrse"], "l3", last, {}, "2026-09-10T00:00:00Z",
                candidate_pairs={("vitalharmony/hrse", 1530)},
                recorded_only=True)
        self.assertEqual(ok1, {"vitalharmony/hrse"})
        self.assertIn(("vitalharmony/hrse", 1530), queue1)
        self.assertIn("vitalharmony/hrse#1530 queued-for-l3 kind=ready-for-l3", lines1)

        # Tick 2: 1530 no longer a recorded candidate at all (file overwritten
        # with a non-queue-eligible kind, e.g. Lane 3's own gate-result).
        with patch("watch_lane_posts._fetch_all_comments",
                   return_value=[{"body": self._READY}]):
            queue2, lines2, ok2 = watch_lane_posts.queue_cycle(
                ["vitalharmony/hrse"], "l3", queue1, {}, "2026-09-10T00:05:00Z",
                candidate_pairs=set(),
                recorded_only=True)
        self.assertEqual(ok2, {"vitalharmony/hrse"})
        self.assertNotIn(("vitalharmony/hrse", 1530), queue2,
                          "a recorded-only belt must not carry an ineligible "
                          "candidate forward indefinitely")
        self.assertIn("vitalharmony/hrse#1530 left-queue-for-l3", lines2)

        # Tick 3: a genuine SECOND ready-for-l3 is recorded on the same
        # issue (e.g. after a fix-and-retest cycle). It must be queued and
        # emit a line -- not suppressed as a duplicate of the stale marker
        # that was correctly dropped at tick 2.
        with patch("watch_lane_posts._fetch_all_comments",
                   return_value=[{"body": self._READY}]):
            queue3, lines3, ok3 = watch_lane_posts.queue_cycle(
                ["vitalharmony/hrse"], "l3", queue2, {}, "2026-09-10T00:10:00Z",
                candidate_pairs={("vitalharmony/hrse", 1530)},
                recorded_only=True)
        self.assertEqual(ok3, {"vitalharmony/hrse"})
        self.assertIn(("vitalharmony/hrse", 1530), queue3)
        self.assertIn("vitalharmony/hrse#1530 queued-for-l3 kind=ready-for-l3", lines3,
                       "a genuine re-post must not be suppressed as a duplicate "
                       "of a marker that was already dropped from the queue")

    def test_default_recorded_only_false_preserves_worktree_ambiguity(self):
        """`recorded_only` defaults to `False` -- every existing worktree/
        `--issues`-derived belt keeps carrying an uninspected candidate
        forward exactly as before (see the sibling test above this class,
        `test_a_queued_issue_whose_candidate_disappears_carries_forward_
        not_retracted`, which asserts the old behavior with no
        `recorded_only` argument at all)."""
        last = {("vitalharmony/hrse", 1600): "ready-for-l3"}
        with patch("watch_lane_posts._fetch_all_comments",
                   return_value=[{"body": self._READY}]):
            queue, lines, ok = watch_lane_posts.queue_cycle(
                ["vitalharmony/hrse"], "l3", last, {}, "2026-09-10T00:00:00Z",
                candidate_pairs=set())
        self.assertIn(("vitalharmony/hrse", 1600), queue,
                       "recorded_only=False (the default) must still carry "
                       "an uninspected worktree-sourced candidate forward")
        self.assertFalse([l for l in lines if "left-queue" in l])


class CommentWatchCycleTests(unittest.TestCase):
    """harmonic-forge#599 preclose finding: every behavior AC1/AC3/AC4/AC6 name
    lived inside `while True:` with no seam, so six mutations of it -- including
    advance-on-failure, the exact regression AC6 names -- left the suite green.
    These drive `comment_watch_cycle` directly."""

    NOW = "2026-09-10T12:00:00Z"
    HANDOFF = ("## Handoff\n\n<!-- l1-post v1; kind=handoff; posted-by=LANE1 -->")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.wm = Watermarks(root / "wm")
        self.seen = SeenSet(root / "seen.tsv")
        self.primed = set()
        self.target = [("vitalharmony/hrse", 1530)]
        self.key = watch_lane_posts._wm_key("vitalharmony/hrse", 1530)

    def tearDown(self):
        self.tmp.cleanup()

    def _cycle(self, comments, now=None, allow_priming=True, promote=True,
                fetch=None):
        deferred = []
        with patch("watch_lane_posts._fetch_comments",
                   **({"side_effect": fetch} if fetch else {"return_value": comments})):
            lines, _fetch_failed = watch_lane_posts.comment_watch_cycle(
                self.target, {"l1"}, now or self.NOW,
                self.wm, self.seen, self.primed, allow_priming=allow_priming,
                deferred_advances=deferred)
        if promote:
            # What `main()` does (harmonic-forge#697): the real delivery path.
            watch_lane_posts.deliver_comment_lines(
                lines, self.seen, self.wm, deferred, out=io.StringIO())
        return lines

    @staticmethod
    def _announced(lines):
        """Live announcements only -- not the AC4 'PRIMED at first arm' line."""
        return [l for l in lines if "PRIMED at first arm" not in l]

    def test_a_failed_fetch_reports_fetch_failed_true(self):
        """harmonic-forge#638 preclose finding 2 -- 'I do not know' must never
        read as 'nothing found' for backoff purposes."""
        with patch("watch_lane_posts._fetch_comments", return_value=None):
            _lines, fetch_failed = watch_lane_posts.comment_watch_cycle(
                self.target, {"l1"}, self.NOW, self.wm, self.seen, self.primed)
        self.assertTrue(fetch_failed)

    def test_a_successful_fetch_reports_fetch_failed_false(self):
        with patch("watch_lane_posts._fetch_comments", return_value=[]):
            _lines, fetch_failed = watch_lane_posts.comment_watch_cycle(
                self.target, {"l1"}, self.NOW, self.wm, self.seen, self.primed)
        self.assertFalse(fetch_failed)

    def test_a_failed_fetch_does_not_advance_the_watermark(self):
        """AC1/AC6, and mutation M1 -- the exact regression this issue is about."""
        self.wm.advance("vitalharmony", self.key,
                        watch_lane_posts._parse_iso("2026-09-10T10:00:00Z"))
        before = self.wm.get("vitalharmony", self.key)
        self._cycle(None)
        self.assertEqual(self.wm.get("vitalharmony", self.key), before,
                         "a failed fetch must hold the watermark")

    def test_a_successful_fetch_advances_the_watermark(self):
        self._cycle([])
        self.assertEqual(self.wm.get("vitalharmony", self.key),
                         watch_lane_posts._parse_iso(self.NOW))

    def test_the_missed_window_is_re_read_next_cycle(self):
        """AC6's second half: after a failure, the next query still starts from
        the held watermark, so the comment that was missed is fetched again."""
        self.wm.advance("vitalharmony", self.key,
                        watch_lane_posts._parse_iso("2026-09-10T10:00:00Z"))
        self._cycle(None)
        seen_since = {}
        def capture(repo, issue, since):
            seen_since["since"] = since
            return []
        with patch("watch_lane_posts._fetch_comments", side_effect=capture):
            watch_lane_posts.comment_watch_cycle(
                self.target, {"l1"}, self.NOW, self.wm, self.seen, self.primed)
        self.assertLessEqual(seen_since["since"], "2026-09-10T10:00:00Z",
                             "the re-read must cover the window the failure missed")

    def test_the_query_applies_overlap(self):
        """AC3, mutation M2."""
        self.wm.advance("vitalharmony", self.key,
                        watch_lane_posts._parse_iso("2026-09-10T11:59:00Z"))
        got = {}
        def capture(repo, issue, since):
            got["since"] = since
            return []
        with patch("watch_lane_posts._fetch_comments", side_effect=capture):
            watch_lane_posts.comment_watch_cycle(
                self.target, {"l1"}, self.NOW, self.wm, self.seen, self.primed)
        self.assertLess(got["since"], "2026-09-10T11:59:00Z",
                        "the query must start before the watermark (overlap)")

    def test_first_cycle_primes_and_does_not_announce(self):
        """AC4, mutation M5."""
        lines = self._cycle([{"id": "1", "body": self.HANDOFF}])
        self.assertEqual(self._announced(lines), [])
        self.assertEqual(self.seen.status("1"), SeenSet.PRIMED)

    def test_second_cycle_announces(self):
        """Mutation M4 -- priming must not be permanent."""
        self._cycle([{"id": "1", "body": self.HANDOFF}])
        lines = self._cycle([{"id": "2", "body": self.HANDOFF}])
        self.assertEqual(len(lines), 1)
        self.assertIn("vitalharmony/hrse#1530", lines[0])
        self.assertEqual(self.seen.status("2"), SeenSet.EMITTED)

    def test_an_already_seen_comment_is_not_re_announced(self):
        """Mutations M3/M6 -- what makes overlap affordable."""
        self._cycle([{"id": "1", "body": self.HANDOFF}])
        self._cycle([{"id": "2", "body": self.HANDOFF}])
        again = self._cycle([{"id": "2", "body": self.HANDOFF}])
        self.assertEqual(again, [], "overlap re-delivers; the seen-set dedups")

    def test_priming_is_per_target_so_a_failed_first_fetch_still_primes(self):
        """Preclose finding: a scalar `priming` cleared once per cycle meant a
        target whose first fetch failed never primed, then replayed its whole
        overlap window as new -- priming inverted into what it prevents."""
        self._cycle(None)                       # cycle 1 fails for this target
        self.assertNotIn("vitalharmony/hrse#1530", self.primed)
        lines = self._cycle([{"id": "1", "body": self.HANDOFF}])
        self.assertEqual(self._announced(lines), [],
                         "the first SUCCESSFUL cycle must still prime")

    def test_what_priming_suppressed_is_named_not_counted(self):
        """The overlap window reaches backwards into live work, so priming can
        swallow a handoff posted moments before arming. A count cannot tell the
        operator that happened."""
        with patch("sys.stderr", new_callable=io.StringIO) as err:
            self._cycle([{"id": "1", "body": self.HANDOFF}])
        out = err.getvalue()
        self.assertIn("SUPPRESSED", out)
        self.assertIn("vitalharmony/hrse#1530", out)
        self.assertIn("delete", out, "the operator needs the recovery path")

    # --- harmonic-forge#697 ------------------------------------------------

    def _new_process(self):
        """A fresh process on the same on-disk state: what a Monitor re-arm is."""
        self.seen = SeenSet(self.seen.path)
        self.primed = set()
        return watch_lane_posts.priming_allowed(self.seen)

    def test_rearm_emits_a_post_that_landed_between_runs(self):
        """AC1/AC3: the hrse#1948 spec, primed and never emitted at 20:21:16Z."""
        self._cycle([{"id": "1", "body": self.HANDOFF}])       # run 1, first arm
        allow = self._new_process()                            # run 2, re-arm
        self.assertFalse(allow, "a re-arm must never prime")
        lines = self._cycle([{"id": "1", "body": self.HANDOFF},
                             {"id": "2", "body": self.HANDOFF}],
                            allow_priming=allow)
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("vitalharmony/hrse#1530", lines[0])
        self.assertEqual(self.seen.status("2"), SeenSet.EMITTED)

    def test_a_quiet_first_arm_does_not_make_the_next_arm_prime(self):
        """Preclose finding 1: a first arm that saw no comment leaves the
        seen-set empty, so `priming_allowed` is True again on the re-arm.
        The target's watermark is what stops it priming."""
        self._cycle([])                                        # quiet first arm
        allow = self._new_process()
        self.assertTrue(allow, "the seen-set alone cannot tell")
        lines = self._cycle([{"id": "2", "body": self.HANDOFF}], allow_priming=allow)
        self.assertEqual(len(self._announced(lines)), 1, lines)
        self.assertEqual(self.seen.status("2"), SeenSet.EMITTED)

    def test_a_pending_emit_survives_a_late_rearm(self):
        """Preclose finding 2: the watermark advanced before the flush, so a
        re-arm more than ~11 minutes later queried past the PENDING comment."""
        self._cycle([{"id": "1", "body": self.HANDOFF}])       # primes, mark=12:00
        self._cycle([{"id": "2", "body": self.HANDOFF,
                      "created_at": "2026-09-10T12:04:00Z"}],
                    now="2026-09-10T12:05:00Z", allow_priming=False,
                    promote=False)                             # killed before flush
        allow = self._new_process()
        got = {}
        def capture(repo, issue, since):
            got["since"] = since
            return [{"id": "2", "body": self.HANDOFF}]
        lines = self._cycle(None, now="2026-09-10T12:30:00Z",
                            allow_priming=allow, fetch=capture)
        self.assertLessEqual(got["since"], "2026-09-10T12:04:00Z",
                             "the late re-arm must still reach the pending comment")
        self.assertEqual(len(lines), 1, lines)
        self.assertEqual(self.seen.status("2"), SeenSet.EMITTED)

    def test_deliver_prints_before_recording(self):
        """Preclose finding 3: the print-promote-advance order lives in one
        tested function, not in `main()`'s untested loop."""
        self.seen.add("5", SeenSet.PENDING)
        deferred = [("vitalharmony", self.key,
                     watch_lane_posts._parse_iso("2026-09-10T12:10:00Z"))]
        class Killed(Exception):
            pass
        class DyingOut:
            def write(self, _):
                raise Killed()
            def flush(self):
                pass
        with self.assertRaises(Killed):
            watch_lane_posts.deliver_comment_lines(
                ["x"], self.seen, self.wm, deferred, out=DyingOut())
        self.assertEqual(self.seen.status("5"), SeenSet.PENDING)
        self.assertIsNone(self.wm.get("vitalharmony", self.key))
        out = io.StringIO()
        watch_lane_posts.deliver_comment_lines(["x"], self.seen, self.wm, deferred, out=out)
        self.assertEqual(out.getvalue(), "x\n")
        self.assertEqual(self.seen.status("5"), SeenSet.EMITTED)
        self.assertIsNotNone(self.wm.get("vitalharmony", self.key))

    def test_main_delivers_through_the_tested_function(self):
        """Preclose finding 3: pin `main()` to the tested seams."""
        import inspect
        src = inspect.getsource(watch_lane_posts.main)
        self.assertIn("allow_priming=allow_priming", src)
        self.assertIn("deferred_advances=deferred_advances", src)
        self.assertIn("deliver_comment_lines(comment_lines", src)

    def test_a_run_killed_after_emit_re_emits_on_next_arm(self):
        """AC2/AC3: the hrse#1902 spec, recorded emitted and never delivered."""
        self._cycle([{"id": "1", "body": self.HANDOFF}])
        allow = self._new_process()
        self._cycle([{"id": "2", "body": self.HANDOFF}],
                    allow_priming=allow, promote=False)        # killed before flush
        allow = self._new_process()
        self.assertEqual(self.seen.status("2"), SeenSet.PENDING)
        lines = self._cycle([{"id": "2", "body": self.HANDOFF}], allow_priming=allow)
        self.assertEqual(len(lines), 1, "an unconfirmed emit must be re-emitted")
        self.assertEqual(self.seen.status("2"), SeenSet.EMITTED)

    def test_a_delivered_emit_is_not_re_emitted_on_next_arm(self):
        self._cycle([{"id": "1", "body": self.HANDOFF}])
        allow = self._new_process()
        self._cycle([{"id": "2", "body": self.HANDOFF}], allow_priming=allow)
        allow = self._new_process()
        lines = self._cycle([{"id": "2", "body": self.HANDOFF}], allow_priming=allow)
        self.assertEqual(lines, [])

    def test_first_arm_suppression_is_named_on_stdout(self):
        """AC4: stderr goes to a file the lane never reads."""
        lines = self._cycle([{"id": "1", "body": self.HANDOFF}])
        primed = [l for l in lines if "PRIMED at first arm" in l]
        self.assertEqual(len(primed), 1, lines)
        self.assertIn("vitalharmony/hrse#1530", primed[0])
        self.assertIn("l1", primed[0])

    def test_priming_allowed_only_on_an_empty_seen_set(self):
        self.assertTrue(watch_lane_posts.priming_allowed(self.seen))
        self.seen.add("9", SeenSet.PRIMED)
        self.assertFalse(watch_lane_posts.priming_allowed(self.seen))


class Lane1InboundQueueTests(unittest.TestCase):
    """harmonic-forge#618. Lane 1's inbound is handed UP by Lane 2, which is the
    reverse of every other lane's, and the code hardcoded the downward
    direction."""

    PLAN = "## Plan — H1383\n\n<!-- l1-post v1; kind=plan; posted-by=LANE2 -->"
    L1 = "## L1 review\n\n<!-- l1-post v1; kind=discussion; posted-by=LANE1 -->"
    DISC = "## Plan — H1\n\n<!-- l1-post v1; kind=discussion; posted-by=LANE2 -->"
    HANDOFF = "## Handoff\n\n<!-- l1-post v1; kind=handoff; posted-by=LANE1 -->"

    def _queue(self, bodies, lane="l1"):
        with patch("watch_lane_posts._fetch_all_comments",
                   return_value=[{"body": b} for b in bodies]):
            return discover_queue("vitalharmony/hrse", lane, {1383})[0]

    def test_a_lane2_plan_queues_to_lane1(self):
        self.assertEqual(self._queue([self.PLAN]), {1383: "plan"})

    def test_it_clears_once_lane1_answers(self):
        """Self-clearing, the same way every other lane's queue is."""
        self.assertEqual(self._queue([self.PLAN, self.L1]), {})

    def test_a_plan_posted_as_discussion_does_not_queue(self):
        """Why the four stalled. `discussion` is deliberately not queue-eligible
        (63 issues measured, none actionable), which is why harmonic-forge#618's
        real fix is the guard that makes a plan carry `kind=plan` at the source."""
        self.assertEqual(self._queue([self.DISC]), {})

    def test_posters_are_directional(self):
        """The hardcoded `last_kind[0] == "l1"` is correct for lanes 2 and 3 --
        Lane 1 hands work DOWN -- and structurally wrong for Lane 1. Adding
        QUEUE_KINDS["l1"] alone changed nothing; this is what made it work."""
        self.assertEqual(watch_lane_posts.QUEUE_POSTERS["l1"], ("l2", "l3"))
        self.assertEqual(watch_lane_posts.QUEUE_POSTERS["l2"], ("l1",))

    def test_lane1_never_queues_its_own_marker(self):
        """"Already acted" is still expressed by Lane 1's own marker being
        newest -- it must not queue work to itself."""
        self.assertNotIn("l1", watch_lane_posts.QUEUE_POSTERS["l1"])

    def test_lane2_and_lane3_are_unchanged(self):
        self.assertEqual(self._queue([self.HANDOFF], "l2"), {1383: "handoff"})

    def test_discussion_is_not_a_lane1_queue_kind(self):
        """Adding it would reintroduce the 63-issue noise on the lane with the
        least capacity to absorb it."""
        self.assertNotIn("discussion", watch_lane_posts.QUEUE_KINDS["l1"])


class SweepFlagIsSeparateTests(unittest.TestCase):
    """harmonic-forge#618. `--queue-for l1` used to route to the UNBOUNDED
    repo-wide sweep, so it meant something categorically different from
    `--queue-for l2` -- an inconsistency that was itself a trap."""

    def test_queue_for_means_the_same_thing_for_every_lane(self):
        for lane in ("l1", "l2", "l3"):
            with self.subTest(lane=lane):
                self.assertIn(lane, watch_lane_posts.QUEUE_KINDS)
                self.assertIn(lane, watch_lane_posts.QUEUE_POSTERS)

    def test_sweep_true_reaches_the_unbounded_sweep(self):
        """harmonic-forge#618 preclose finding: dropping `sweep=` at the call
        site made `--sweep-for l1` route to the BOUNDED queue, so the suspenders
        silently covered the same narrow set as the belt -- and the whole suite
        stayed green. This is the mutation the flag exists to prevent."""
        with patch("watch_lane_posts.l1_sweep_cycle",
                   return_value=({1: ("l2", "x")}, "2026-09-10T12:00:00Z")) as sweep, \
             patch("watch_lane_posts.discover_queue",
                   return_value=({}, True)) as bounded:
            watch_lane_posts.queue_cycle(["o/r"], "l1", {}, {},
                                         "2026-09-10T12:00:00Z", sweep=True)
        sweep.assert_called_once()
        bounded.assert_not_called()

    def test_sweep_false_reaches_the_bounded_queue(self):
        with patch("watch_lane_posts.l1_sweep_cycle") as sweep, \
             patch("watch_lane_posts.discover_queue",
                   return_value=({}, True)) as bounded:
            watch_lane_posts.queue_cycle(["o/r"], "l1", {}, {},
                                         "2026-09-10T12:00:00Z")
        bounded.assert_called_once()
        sweep.assert_not_called()

    # `test_main_passes_sweep_true_for_the_sweep_flag` was removed by
    # harmonic-forge#659: with `--sweep-for l1` (#640) and `--sweep-for l3`
    # (#659) both refused at parse time, no invocation of `main()` reaches
    # `queue_cycle(..., sweep=True)` any more. The refusal itself is asserted
    # in `SweepForL1IsRetiredTests` / `SweepForL3IsRetiredTests` below.

    def test_main_passes_sweep_false_for_the_queue_flag(self):
        seen = {}
        def capture(*a, **kw):
            seen["sweep"] = kw.get("sweep", False)
            raise KeyboardInterrupt
        # LANE=3's plain belt entry (--queue-for, no --all-worktrees) --
        # avoids a real `--all-worktrees` enumeration of every worktree on
        # this machine, which is what made this test (LANE=1's canonical
        # argv, previously used here) take ~15s of real I/O.
        argv = ["watch_lane_posts.py",
                *watch_lane_posts.CANONICAL_BELTS["3"][0]["argv"]]
        with patch.object(sys, "argv", argv), \
             patch.dict(os.environ, {"LANE": "3"}), \
             patch("watch_lane_posts.assert_identity"), \
             patch("watch_lane_posts._check_git_staleness"), \
             patch("watch_lane_posts._acquire_belt_lock"), \
             patch("watch_lane_posts.queue_cycle", side_effect=capture), \
             patch("watch_lane_posts.time.sleep"):
            with self.assertRaises(KeyboardInterrupt):
                watch_lane_posts.main()
        self.assertFalse(seen["sweep"])

    def test_arming_both_the_belt_and_the_sweep_is_refused(self):
        """Collapsing two deliberately independent mechanisms into one process
        is harmonic-forge#590's regression.

        In-process, not a subprocess: `assert_identity` runs before argument
        validation and shells out to `gh-as`, which does not exist on CI. The
        subprocess form of this test passed locally and failed in CI with
        `FileNotFoundError: 'gh-as'` -- a test that only runs on one machine."""
        with patch.object(sys, "argv", ["watch_lane_posts.py", "--queue-for", "l1",
                                        "--sweep-for", "l1", "--repo", "o/r",
                                        "--watch", "l2"]), \
             patch("watch_lane_posts.assert_identity"), \
             patch("sys.stderr", new_callable=io.StringIO) as err:
            with self.assertRaises(SystemExit) as caught:
                watch_lane_posts.main()
        self.assertEqual(caught.exception.code, 2)
        self.assertIn("belt and the suspenders", err.getvalue())


class SweepForL1IsRetiredTests(unittest.TestCase):
    """harmonic-forge#640 AC6, added on the live issue thread after preclose
    found a doc-only fix leaves `--sweep-for l1` fully callable: a session
    that finds the flag in an old transcript or a stale SKILL.md copy could
    still run the retired mechanism. Refused at parse time instead. `l3` was
    a separate mechanism sharing the flag; it is now retired too
    (harmonic-forge#659, see `SweepForL3IsRetiredTests`)."""

    def test_sweep_for_l1_is_refused_at_parse_time(self):
        """Mutation-checked per the issue thread's own instruction: with this
        guard reverted, `--sweep-for l1` must run rather than refuse."""
        with patch.object(sys, "argv", ["watch_lane_posts.py", "--sweep-for", "l1",
                                        "--repo", "o/r", "--watch", "l2"]), \
             patch("watch_lane_posts.assert_identity"), \
             patch("sys.stderr", new_callable=io.StringIO) as err:
            with self.assertRaises(SystemExit) as caught:
                watch_lane_posts.main()
        self.assertEqual(caught.exception.code, 2)
        self.assertIn("RETIRED", err.getvalue())
        self.assertIn("harmonic-forge#640", err.getvalue())
        self.assertIn("--queue-for l1", err.getvalue())



class SweepForL3IsRetiredTests(unittest.TestCase):
    """harmonic-forge#659 AC1, operator ruling: the Lane 3 repo-wide sweep
    exhausted the shared REST budget twice on 2026-09-14 and is retired
    exactly as `--sweep-for l1` was (#640) -- refused at parse time, before
    any canonical-table check, lock, or poll."""

    def test_sweep_for_l3_is_refused_at_parse_time(self):
        """With the guard reverted, the former canonical sweep argv under
        LANE=3 must no longer refuse this way."""
        argv = ["watch_lane_posts.py", "--sweep-for", "l3",
                "--account-repos", "vitalharmony", "--interval", "300"]
        with patch.object(sys, "argv", argv), \
             patch.dict(os.environ, {"LANE": "3"}), \
             patch("watch_lane_posts.assert_identity"), \
             patch("watch_lane_posts._check_git_staleness"), \
             patch("watch_lane_posts._acquire_belt_lock"), \
             patch("watch_lane_posts.queue_cycle") as cycle, \
             patch("watch_lane_posts.time.sleep"), \
             patch("sys.stderr", new_callable=io.StringIO) as err:
            with self.assertRaises(SystemExit) as caught:
                watch_lane_posts.main()
        self.assertEqual(caught.exception.code, 2)
        self.assertIn("--sweep-for l3 is RETIRED", err.getvalue())
        self.assertIn("harmonic-forge#659", err.getvalue())
        cycle.assert_not_called()

    def test_lane3_table_holds_only_the_queue_belt(self):
        entries = watch_lane_posts.CANONICAL_BELTS["3"]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["lock"], "belt-lane3.lock")
        self.assertNotIn("--sweep-for", entries[0]["argv"])


class BeltDedupMechanicTests(unittest.TestCase):
    """harmonic-forge#599. `SKILL.md` declares dedup as one mechanic with three
    parts -- watermark, overlap, seen-set -- and says "Do not simplify it back."
    The comment-watch path had none of them: one in-memory `since`, advanced
    unconditionally after a fetch that swallowed failures to `[]`."""

    def test_fetch_comments_returns_none_on_failure_not_empty(self):
        """`[]` means zero comments; failure must be distinguishable, or the
        caller cannot know whether to hold its watermark."""
        with patch("watch_lane_posts.gh_as", side_effect=RuntimeError("rate limit")):
            self.assertIsNone(watch_lane_posts._fetch_comments("r", 1, "s"))

    def test_fetch_comments_returns_none_on_unparseable_body(self):
        with patch("watch_lane_posts.gh_as", return_value="not json"):
            self.assertIsNone(watch_lane_posts._fetch_comments("r", 1, "s"))

    def test_a_genuinely_empty_result_is_still_a_list(self):
        with patch("watch_lane_posts.gh_as", return_value="[]"):
            self.assertEqual(watch_lane_posts._fetch_comments("r", 1, "s"), [])

    def test_overlap_reads_from_before_the_watermark(self):
        """AC3. `query_since` returns `min(watermark, now - K)`, so the seam
        between cycles is re-read rather than assumed."""
        now = watch_lane_posts._parse_iso("2026-09-10T12:00:00Z")
        mark = watch_lane_posts._parse_iso("2026-09-10T11:59:00Z")
        self.assertLess(query_since(mark, watch_lane_posts._OVERLAP_MINUTES, now),
                        mark, "the query must start BEFORE the watermark")

    def test_overlap_never_skips_a_stalled_target(self):
        """A watermark older than the overlap floor wins, so a target unread
        for an hour is re-read from where it stopped, not from now-K."""
        now = watch_lane_posts._parse_iso("2026-09-10T12:00:00Z")
        stale = watch_lane_posts._parse_iso("2026-09-10T11:00:00Z")
        self.assertEqual(
            query_since(stale, watch_lane_posts._OVERLAP_MINUTES, now), stale)

    def test_watermark_key_survives_a_slash_in_the_repo_name(self):
        """`Watermarks` builds a filename from this key."""
        key = watch_lane_posts._wm_key("vitalharmony/hrse", 1530)
        self.assertNotIn("/", key)
        self.assertIn("1530", key)

    def test_watermark_is_per_target_not_per_repo(self):
        """Two issues in one repo fail independently; a shared marker would let
        one issue's failure advance past another's unread window -- the same
        argument `Watermarks` makes one level up about accounts vs repos."""
        self.assertNotEqual(watch_lane_posts._wm_key("o/r", 1),
                            watch_lane_posts._wm_key("o/r", 2))

    def test_seen_set_distinguishes_primed_from_emitted(self):
        """AC4. A bare-id file could not say afterwards whether a comment was
        reported or suppressed at arm."""
        with tempfile.TemporaryDirectory() as tmp:
            s = SeenSet(Path(tmp) / "seen.tsv")
            s.prime(["1", "2"])
            s.add("3", SeenSet.EMITTED)
            self.assertEqual(s.status("1"), SeenSet.PRIMED)
            self.assertEqual(s.status("3"), SeenSet.EMITTED)

    def test_the_belt_imports_the_mechanics_the_skill_declares_mandatory(self):
        """The finding itself: the module imported four names from
        `belt_mechanics` and used none of `Watermarks`, `SeenSet` or
        `query_since`, under documentation asserting all three are required."""
        for name in ("Watermarks", "SeenSet", "query_since"):
            self.assertTrue(hasattr(watch_lane_posts, name),
                            f"{name} is declared mandatory and is not imported")


class QueueKeyIsRepoQualifiedTests(unittest.TestCase):
    """harmonic-forge#596 AC2: hrse#570 and harmonic-forge#570 both exist. A
    queue keyed on the bare issue number lets one evict the other."""

    def test_two_repos_same_issue_number_both_survive(self):
        with patch("watch_lane_posts.discover_queue",
                   side_effect=[{570: "handoff"}, {570: "handoff"}]):
            queue = {}
            for repo in ("vitalharmony/hrse", "vitalharmony/harmonic-forge"):
                for issue, kind in watch_lane_posts.discover_queue(repo, "l3").items():
                    queue[(repo, issue)] = kind
        self.assertEqual(len(queue), 2)
        self.assertEqual(sorted(queue),
                         [("vitalharmony/harmonic-forge", 570),
                          ("vitalharmony/hrse", 570)])


class NextPollIntervalTests(unittest.TestCase):
    """harmonic-forge#638 AC1/AC2/AC3."""

    def test_a_zero_streak_returns_the_base_interval_unchanged(self) -> None:
        """AC1: the cycle that just found something (or the very first
        cycle) polls at the armed cadence, not a backed-off one."""
        for base in (60, 90, 300, 600):
            with self.subTest(base=base):
                self.assertEqual(next_poll_interval(base, 0), base)

    def test_a_negative_streak_is_treated_the_same_as_zero(self) -> None:
        """Defensive: no caller should ever construct one, but a function
        this central to AC2's guarantee must not behave strangely on an
        out-of-domain input either."""
        self.assertEqual(next_poll_interval(60, -1), 60)

    def test_the_interval_roughly_doubles_each_further_quiet_cycle(self) -> None:
        base = 60
        prev = next_poll_interval(base, 1)
        for streak in range(2, 4):
            current = next_poll_interval(base, streak)
            self.assertGreater(current, prev, f"streak={streak}")
            prev = current

    def test_growth_caps_at_ten_times_the_base_interval(self) -> None:
        """AC1's 'capped' half -- sustained quiet must not grow unbounded."""
        for base in (60, 300, 600):
            with self.subTest(base=base):
                self.assertEqual(next_poll_interval(base, 1000), base * 10)

    def test_never_returns_anything_but_a_positive_finite_interval(self) -> None:
        """AC2: there is no quiet-streak length, however long, that makes
        this function express 'stop polling'. Swept across a wide range of
        streak lengths and bases -- the guarantee is about EVERY input, not
        one example."""
        for base in (30, 60, 90, 300, 600, 3600):
            for streak in (0, 1, 2, 5, 10, 50, 1000, 10_000):
                value = next_poll_interval(base, streak)
                self.assertIsInstance(value, int)
                self.assertGreater(value, 0)
                self.assertLessEqual(value, base * 10)

    def test_the_cap_scales_with_each_lanes_own_base_preserving_urgency_order(self) -> None:
        """AC3: a shared global cap would let a backed-off sweep (600s base)
        converge to the SAME interval as a backed-off Lane 3 watcher (60s
        base), erasing the urgency difference the armed intervals encode.
        The cap must scale with base instead, so Lane 3 stays strictly more
        frequent than the sweep at every backoff level, not just at the
        base."""
        lane3_capped = next_poll_interval(60, 1000)
        sweep_capped = next_poll_interval(600, 1000)
        self.assertLess(lane3_capped, sweep_capped)
        self.assertEqual(sweep_capped / lane3_capped, 10)


class CycleIsQuietTests(unittest.TestCase):
    """harmonic-forge#638 preclose findings 1/2/3.

    Finding 1: dedup makes an unchanged, still-queued item look "quiet" --
    `queue_cycle` only emits a line on a queue-marker CHANGE, not on every
    cycle a queued item remains unpicked. A non-empty `queue` in queue-for/
    sweep-for mode must never read as quiet, printed line or not.

    Finding 2: a failed fetch (repo-fetch half via `ok_repos` short of
    `repos`, or comment-fetch half via `comment_fetch_failed`) must never
    read as quiet either -- "I do not know" is not "nothing found".

    Finding 3: none of the original 9 tests exercised the loop's own
    reset-trigger logic -- the preclose demonstrated live that replacing
    `quiet_streak = 0 if cycle_emitted else quiet_streak + 1` with
    `quiet_streak = quiet_streak + 1` (backoff never resets, an outright
    AC1 violation) still left the full suite green. These tests drive
    `cycle_is_quiet` itself, the function that decision now routes through,
    so that exact mutation is caught: it would report `quiet=True` on a
    cycle carrying real queued work or a failed fetch, which every test
    below asserts must be `False`.
    """

    def test_printed_line_is_never_quiet(self):
        self.assertFalse(cycle_is_quiet(True, {}, "l1", set(), [], False))

    def test_no_mode_no_queue_no_failure_is_quiet(self):
        """The ordinary quiet case: comment-watch-only mode, nothing printed,
        nothing failed."""
        self.assertTrue(cycle_is_quiet(False, {}, None, set(), [], False))

    def test_nonempty_queue_is_never_quiet_even_with_no_printed_line(self):
        """Finding 1: a queued item that didn't change this cycle prints
        nothing, but real work is still outstanding."""
        queue = {("vitalharmony/hrse", 1530): "l1:handoff"}
        self.assertFalse(cycle_is_quiet(False, queue, "l1", {"vitalharmony/hrse"},
                                         ["vitalharmony/hrse"], False))

    def test_a_repo_that_failed_to_report_is_never_quiet(self):
        """Finding 1 (fetch half): `ok_repos` short of `repos` means at least
        one repo's own queue_cycle fetch failed -- unknown, not empty."""
        self.assertFalse(cycle_is_quiet(False, {}, "l1", set(),
                                         ["vitalharmony/hrse"], False))

    def test_comment_fetch_failure_is_never_quiet(self):
        """Finding 2."""
        self.assertFalse(cycle_is_quiet(False, {}, None, set(), [], True))

    def test_empty_queue_with_mode_and_all_repos_reporting_is_quiet(self):
        """A queue-for/sweep-for mode that genuinely found nothing, from
        every repo it asked, is quiet."""
        self.assertTrue(cycle_is_quiet(False, {}, "l1", {"vitalharmony/hrse"},
                                        ["vitalharmony/hrse"], False))

    def test_the_exact_mutation_the_preclose_demonstrated_is_caught(self):
        """Reproduces the preclose's own live check: with a non-empty queue
        and no printed line, the loop's reset condition must fire. Simulates
        `quiet_streak = 0 if not cycle_is_quiet(...) else quiet_streak + 1`
        across three cycles of sustained real work sitting in queue -- a
        mutation that always increments (backoff never resets) would let
        `quiet_streak` grow unboundedly here; the correct behavior holds it
        at 0 throughout."""
        queue = {("vitalharmony/hrse", 1530): "l1:handoff"}
        quiet_streak = 0
        for _ in range(3):
            quiet = cycle_is_quiet(False, queue, "l1", {"vitalharmony/hrse"},
                                    ["vitalharmony/hrse"], False)
            quiet_streak = 0 if not quiet else quiet_streak + 1
        self.assertEqual(quiet_streak, 0,
                          "real outstanding work must hold the backoff at 0")


class BeltNeverPausesDocSyncTests(unittest.TestCase):
    """harmonic-forge#638 AC6: SKILL.md states plainly that the belt does
    not pause, and why -- doc-SYNC against the actual constant, not a
    number that could silently drift from the code it describes."""

    def test_skill_md_states_the_belt_never_pauses(self) -> None:
        text = _SKILL_MD.read_text(encoding="utf-8")
        self.assertIn("belt never pauses", text.lower())

    def test_skill_md_names_the_real_cap_multiplier(self) -> None:
        """Doc-sync: the "10x" figure in the doc must match
        `_BACKOFF_CAP_MULTIPLIER`, not a hand-typed number that can drift
        the moment the constant changes."""
        from watch_lane_posts import _BACKOFF_CAP_MULTIPLIER

        text = _SKILL_MD.read_text(encoding="utf-8")
        self.assertIn(f"{int(_BACKOFF_CAP_MULTIPLIER)}x", text)

    def test_skill_md_documents_the_suspenders_never_stop_guarantee(self) -> None:
        text = _SKILL_MD.read_text(encoding="utf-8")
        self.assertIn("Never call `stop`", text)


def _with_interval(argv: list[str], value: str) -> list[str]:
    argv = list(argv)
    argv[argv.index("--interval") + 1] = value
    return argv


class CanonicalBeltEnforcementTests(unittest.TestCase):
    """harmonic-forge#651 AC1/AC2. Every invocation reaches `main()`'s
    `while True:` loop (there is no one-shot/test-exempt mode), so every
    invocation is gated by `_enforce_canonical_belt` against
    `CANONICAL_BELTS[LANE]` -- compared as PARSED argparse values (`--watch`
    order-insensitive), never as a raw-argv string, so `--interval=300`,
    `--int 300`, or a reordered `--watch` cannot slip past.
    `_check_git_staleness` and `_acquire_belt_lock` are patched out in every
    case: this class is about the argv/LANE gate alone."""

    def _run(self, argv: list[str], env: dict[str, str] | None):
        patches = [
            patch.object(sys, "argv", ["watch_lane_posts.py", *argv]),
            patch("watch_lane_posts.assert_identity"),
            patch("watch_lane_posts._check_git_staleness"),
            patch("watch_lane_posts._acquire_belt_lock"),
            patch("watch_lane_posts.queue_cycle", side_effect=KeyboardInterrupt),
            patch("watch_lane_posts.time.sleep"),
        ]
        err = io.StringIO()
        patches.append(patch("sys.stderr", err))
        saved_lane = os.environ.get("LANE")
        try:
            if env is None:
                os.environ.pop("LANE", None)
            else:
                os.environ.update(env)
            for p in patches:
                p.start()
            try:
                watch_lane_posts.main()
                outcome = None
            except SystemExit as exc:
                outcome = exc.code
            except KeyboardInterrupt:
                outcome = "looped"
            finally:
                for p in reversed(patches):
                    p.stop()
        finally:
            if saved_lane is None:
                os.environ.pop("LANE", None)
            else:
                os.environ["LANE"] = saved_lane
        return outcome, err.getvalue()

    def test_dropping_queue_for_l1_is_refused(self):
        argv = [a for a in watch_lane_posts.CANONICAL_BELTS["1"][0]["argv"]
                if a not in ("--queue-for", "l1")]
        outcome, err = self._run(argv, {"LANE": "1"})
        self.assertEqual(outcome, 2)
        self.assertIn("canonical command", err)

    def test_interval_60_is_refused(self):
        argv = _with_interval(watch_lane_posts.CANONICAL_BELTS["1"][0]["argv"], "60")
        outcome, err = self._run(argv, {"LANE": "1"})
        self.assertEqual(outcome, 2)
        self.assertIn("canonical command", err)

    def test_an_extra_unexpected_watch_value_is_refused(self):
        """Lane 1's canonical entry already watches l2 and l3 -- adding a
        third (l1, watching itself) must still be caught, proving the
        comparison is an exact-set match, not merely `>=` the required set."""
        argv = [*watch_lane_posts.CANONICAL_BELTS["1"][0]["argv"], "--watch", "l1"]
        outcome, err = self._run(argv, {"LANE": "1"})
        self.assertEqual(outcome, 2)
        self.assertIn("canonical command", err)

    def test_canonical_argv_in_a_different_order_is_accepted(self):
        """The comparison is over PARSED values (`vars(args)`, `--watch`
        sorted), not the raw token sequence -- so a reordering of the exact
        same canonical flags must be accepted, not refused."""
        canonical = watch_lane_posts.CANONICAL_BELTS["1"][0]["argv"]
        # Swap the two --watch pairs and move --interval 300 to the front.
        reordered = ["--interval", "300", "--all-worktrees", "--account-repos",
                     "vitalharmony", "--watch", "l3", "--watch", "l2",
                     "--queue-for", "l1", "--deadline-seconds",
                     str(watch_lane_posts.MONITOR_LIFETIME_S)]
        self.assertEqual(sorted(canonical), sorted(reordered),
                         "test fixture drifted from CANONICAL_BELTS['1']")
        outcome, _err = self._run(reordered, {"LANE": "1"})
        self.assertEqual(outcome, "looped",
                         "a reordered-but-identical canonical command must "
                         "reach the poll loop, not be refused")

    def test_lane_env_var_unset_is_refused(self):
        argv = watch_lane_posts.CANONICAL_BELTS["1"][0]["argv"]
        outcome, err = self._run(argv, None)
        self.assertEqual(outcome, 2)
        self.assertIn("LANE=1|2|3", err)

    def test_allow_abbrev_is_disabled(self):
        """AC1: `allow_abbrev=False` on the parser -- a glued/abbreviated
        form of a canonical flag must not silently parse as if spelled out,
        which would let it slip past the exact-match comparison undetected."""
        parser = argparse.ArgumentParser(allow_abbrev=False)
        # Build the real parser the way main() does, minus running main()
        # itself -- easiest done by asserting on the module-level behavior:
        # an abbreviated `--int` for `--interval` must be REJECTED by argparse
        # itself (unrecognized argument), proving abbreviation is off.
        argv = ["--all-worktrees", "--account-repos", "vitalharmony",
                "--queue-for", "l1", "--watch", "l2", "--watch", "l3",
                "--int", "300"]
        outcome, err = self._run(argv, {"LANE": "1"})
        self.assertEqual(outcome, 2)
        self.assertIn("unrecognized", err.lower())


class BeltLockTests(unittest.TestCase):
    """harmonic-forge#651 AC2. `_acquire_belt_lock` takes an exclusive,
    non-blocking `flock` -- a second instance while the first still holds it
    must exit 4, never block."""

    def test_second_attempt_while_first_holds_the_lock_exits_4(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock_dir = Path(tmp)
            lock_path = lock_dir / "belt-lane1.lock"
            marker = lock_dir / "acquired"
            holder_script = (
                "import fcntl, json, os, sys, time\n"
                "fh = open(sys.argv[1], 'a+')\n"
                "fcntl.flock(fh.fileno(), fcntl.LOCK_EX)\n"
                "fh.write(json.dumps({'pid': os.getpid(), 'start': 'test', "
                "'session_pid': None}))\n"
                "fh.flush()\n"
                "open(sys.argv[2], 'w').close()\n"
                "time.sleep(10)\n"
            )
            proc = subprocess.Popen(
                [sys.executable, "-c", holder_script, str(lock_path), str(marker)])
            try:
                deadline = time.time() + 5
                while not marker.exists():
                    if time.time() > deadline:
                        self.fail("holder subprocess never acquired the lock")
                    time.sleep(0.05)
                with patch("watch_lane_posts.BELT_LOCK_DIR", lock_dir), \
                     patch("watch_lane_posts._nearest_session_pid",
                          return_value=None):
                    with self.assertRaises(SystemExit) as caught:
                        watch_lane_posts._acquire_belt_lock("belt-lane1.lock")
                self.assertEqual(caught.exception.code, 4)
            finally:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)


class GitStalenessTests(unittest.TestCase):
    """harmonic-forge#651 AC3. Local-git-only staleness check: two temp
    repos, one standing in for `origin/main`, the local one deliberately one
    commit behind it (reproduced by advancing origin from a THIRD clone --
    resetting the local repo back a commit would just discard the same
    commit object, not exercise a real fetch of new history)."""

    def _run_git(self, args, cwd):
        result = subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                                text=True, timeout=15)
        self.assertEqual(result.returncode, 0,
                         f"git {args} failed: {result.stderr}")
        return result.stdout.strip()

    def _init_repos(self, root: Path) -> Path:
        origin = root / "origin.git"
        origin.mkdir()
        self._run_git(["init", "--bare", "-b", "main"], cwd=origin)

        seed = root / "seed"
        seed.mkdir()
        self._run_git(["init", "-b", "main"], cwd=seed)
        self._run_git(["config", "user.email", "t@example.invalid"], cwd=seed)
        self._run_git(["config", "user.name", "T"], cwd=seed)
        (seed / "f.txt").write_text("a\n")
        self._run_git(["add", "f.txt"], cwd=seed)
        self._run_git(["commit", "-q", "-m", "A"], cwd=seed)
        self._run_git(["remote", "add", "origin", str(origin)], cwd=seed)
        self._run_git(["push", "-q", "origin", "main"], cwd=seed)

        local = root / "local"
        self._run_git(["clone", "-q", str(origin), str(local)], cwd=root)

        advancer = root / "advancer"
        self._run_git(["clone", "-q", str(origin), str(advancer)], cwd=root)
        self._run_git(["config", "user.email", "t@example.invalid"], cwd=advancer)
        self._run_git(["config", "user.name", "T"], cwd=advancer)
        (advancer / "f.txt").write_text("b\n")
        self._run_git(["commit", "-aq", "-m", "B"], cwd=advancer)
        self._run_git(["push", "-q", "origin", "main"], cwd=advancer)

        (local / "tools" / "gh").mkdir(parents=True)
        return local

    def test_refuses_when_head_is_behind_origin_main(self):
        with tempfile.TemporaryDirectory() as tmp:
            local = self._init_repos(Path(tmp))
            fake_file = local / "tools" / "gh" / "watch_lane_posts.py"
            head = self._run_git(["rev-parse", "HEAD"], cwd=local)
            with patch.object(watch_lane_posts, "__file__", str(fake_file)):
                parser = argparse.ArgumentParser()
                with self.assertRaises(SystemExit) as caught:
                    watch_lane_posts._check_git_staleness(parser)
            self.assertEqual(caught.exception.code, 2)

    def test_fails_open_on_a_broken_git_invocation(self):
        """AC3: any git error must warn and continue, never block the belt."""
        with tempfile.TemporaryDirectory() as tmp:
            not_a_repo = Path(tmp) / "tools" / "gh"
            not_a_repo.mkdir(parents=True)
            fake_file = not_a_repo / "watch_lane_posts.py"
            with patch.object(watch_lane_posts, "__file__", str(fake_file)), \
                 patch("sys.stderr", new_callable=io.StringIO) as err:
                parser = argparse.ArgumentParser()
                watch_lane_posts._check_git_staleness(parser)  # must not raise
            self.assertIn("staleness check failed", err.getvalue())


if __name__ == "__main__":
    unittest.main()



class QueueNoiseFilterTests(unittest.TestCase):
    """Operator ruling 2026-09-14 (harmonic-forge#663)."""

    def test_l2_and_l3_exclude_epic_and_tooling_exception(self):
        from watch_lane_posts import queue_qualifiers
        for lane in ("l2", "l3"):
            excluded = queue_qualifiers("vitalharmony/hrse", lane)
            self.assertIn("epic", excluded)
            self.assertIn("tooling-exception", excluded)
            self.assertNotIn("milestone", excluded)

    def test_l1_keeps_tooling_exception_issues(self):
        from watch_lane_posts import queue_qualifiers
        excluded = queue_qualifiers("vitalharmony/hrse", "l1")
        self.assertIn("epic", excluded)
        self.assertNotIn("tooling-exception", excluded)

    def test_discover_queue_actually_calls_queue_qualifiers(self):
        """harmonic-forge#686 preclose finding: `discover_queue` used to
        reimplement this filter inline from the constants directly, leaving
        `queue_qualifiers` an untested orphan the two tests above exercised
        in isolation -- a change to one could silently stop matching the
        other. Patching `queue_qualifiers` itself and asserting it was
        actually called (not just that its constants happen to still agree)
        is what ties them back together."""
        calls = []
        def spy(repo, lane):
            calls.append((repo, lane))
            return frozenset({"epic"})
        with patch("watch_lane_posts.queue_qualifiers", side_effect=spy), \
             patch("watch_lane_posts._issue_labels", return_value=set()), \
             patch("watch_lane_posts._fetch_all_comments", return_value=[]):
            discover_queue("vitalharmony/hrse", "l3", {1530})
        self.assertIn(("vitalharmony/hrse", "l3"), calls)

    # `test_discover_queue_passes_qualifiers_to_search` was removed by
    # harmonic-forge#686: `discover_queue` no longer searches, so there are
    # no qualifiers to pass to one. The filter `queue_qualifiers` used to
    # express as a search qualifier is restored below as a per-issue label
    # check instead -- `DiscoverQueueLabelFilterTests`.


class DiscoverQueueLabelFilterTests(unittest.TestCase):
    """harmonic-forge#686 preclose finding: `_search_candidates` applied
    `queue_qualifiers`' filter as a search qualifier; removing the search
    left the filter defined but never called, so an epic or a Lane-1-owned
    Tooling Exception issue could land on Lane 2/3's queue the moment a
    worktree or `--issues` handed its number in as a candidate."""

    HANDOFF = "## Handoff\n\n<!-- l1-post v1; kind=handoff; posted-by=LANE1 -->"

    def test_an_epic_labeled_candidate_is_never_queued(self):
        with patch("watch_lane_posts._issue_labels", return_value={"epic"}), \
             patch("watch_lane_posts._fetch_all_comments",
                   return_value=[{"body": self.HANDOFF}]):
            queue, ok = discover_queue("vitalharmony/hrse", "l2", {1})
        self.assertTrue(ok)
        self.assertEqual(queue, {})

    def test_a_tooling_exception_candidate_is_never_queued_for_l2_or_l3(self):
        with patch("watch_lane_posts._issue_labels",
                   return_value={"tooling-exception"}), \
             patch("watch_lane_posts._fetch_all_comments",
                   return_value=[{"body": self.HANDOFF}]):
            for lane in ("l2", "l3"):
                with self.subTest(lane=lane):
                    queue, ok = discover_queue("vitalharmony/hrse", lane, {1})
                    self.assertTrue(ok)
                    self.assertEqual(queue, {})

    def test_a_tooling_exception_candidate_still_queues_for_l1(self):
        """Lane 1 owns Tooling Exception issues -- the filter is asymmetric,
        matching `queue_qualifiers`."""
        with patch("watch_lane_posts._issue_labels",
                   return_value={"tooling-exception"}), \
             patch("watch_lane_posts._fetch_all_comments",
                   return_value=[{"body":
                       "## Plan\n\n<!-- l1-post v1; kind=plan; posted-by=LANE2 -->"}]):
            queue, ok = discover_queue("vitalharmony/hrse", "l1", {1})
        self.assertTrue(ok)
        self.assertEqual(queue, {1: "plan"})

    def test_a_label_fetch_failure_does_not_drop_the_repo(self):
        """Fail open on the filter itself (distinct from a comment-fetch
        failure, which still fails the repo per `DiscoverQueueFailsClosed
        PerIssueTests`): an unfetchable label set must not make real,
        classifiable work disappear."""
        with patch("watch_lane_posts._issue_labels", return_value=None), \
             patch("watch_lane_posts._fetch_all_comments",
                   return_value=[{"body": self.HANDOFF}]):
            queue, ok = discover_queue("vitalharmony/hrse", "l2", {1})
        self.assertTrue(ok)
        self.assertEqual(queue, {1: "handoff"})


class DeadlineAwareSleep(unittest.TestCase):
    """harmonic-forge#680. The belt went blind for ~20 minutes of every quiet
    30-minute window and missed a real Lane 3 spec. Two independent causes."""

    BASE = 300

    def test_the_sleep_never_exceeds_one_base_interval(self):
        """NC1/AC1. A post made at any point must be emitted within one base
        interval, so a 600s sleep at a 300s base fails by construction — the
        FIRST doubling, long before any cap is reached."""
        for streak in range(0, 12):
            with self.subTest(quiet_streak=streak):
                self.assertLessEqual(
                    sleep_before_next_poll(self.BASE, streak, 1800, 0), self.BASE)

    def test_no_sleep_ends_after_the_deadline(self):
        """AC2/TC3, asserted directly rather than inferred from a poll count."""
        deadline = 1800
        now = 0.0
        while True:
            sleep_for = sleep_before_next_poll(self.BASE, 5, deadline, now)
            if sleep_for is None:
                break
            self.assertLessEqual(now + sleep_for, deadline,
                                 f"a sleep starting at {now} ends past {deadline}")
            now += sleep_for

    def test_the_window_poll_count_is_measured_not_derived(self):
        """AC3/NC2. **Seven, and the spec's estimate of six was one low.**

        NC2 worked the schedule to "6 polls per window"; walking the actual
        implementation gives 7, because the final clamped sleep lands one more
        poll inside the reserve before the window ends. Asserting the measured
        number rather than the quoted one is the whole point of AC3 — a count
        derived from a schedule is the same species of claim as the 3000s cap
        that was never reachable.
        """
        now, polls = 0.0, 0
        while True:
            polls += 1
            sleep_for = sleep_before_next_poll(self.BASE, polls, MONITOR_LIFETIME_S, now)
            if sleep_for is None:
                break
            now += sleep_for
        self.assertEqual(polls, 7)
        self.assertGreater(polls, 2, "2 polls per window was the defect")

    def test_the_loop_stops_rather_than_sleeping_into_the_kill(self):
        self.assertIsNone(sleep_before_next_poll(self.BASE, 1, 1800, 1799))
        self.assertIsNone(sleep_before_next_poll(self.BASE, 1, 1800, 1800))

    def test_an_absent_deadline_keeps_the_previous_behaviour(self):
        """A belt started by hand, or an older invocation, must not crash."""
        self.assertEqual(sleep_before_next_poll(self.BASE, 7, None, 0), self.BASE)

    def test_next_poll_interval_is_kept_and_still_honours_its_own_contract(self):
        """NC1 keeps the function rather than deleting it: its AC2 property is
        worth preserving as an invariant, and a lane armed at a longer base
        could use it with headroom. It simply no longer decides the sleep."""
        for streak in range(0, 40):
            self.assertGreater(next_poll_interval(600, streak), 0)


class CapAndLifetimeAgree(unittest.TestCase):
    """AC2 — the RELATIONSHIP is asserted, not two constants that happen to
    agree today. This is the test that fails if either value moves alone."""

    def test_every_canonical_belt_declares_the_shared_lifetime(self):
        from watch_lane_posts import CANONICAL_BELTS

        for lane, entries in CANONICAL_BELTS.items():
            for entry in entries:
                argv = entry["argv"]
                with self.subTest(lane=lane):
                    self.assertIn("--deadline-seconds", argv)
                    value = argv[argv.index("--deadline-seconds") + 1]
                    self.assertEqual(int(value), MONITOR_LIFETIME_S)

    def test_the_monitor_timeout_is_derived_from_the_same_constant(self):
        """NC3's premise: one number, not two. `belt_plan` must not declare
        its own lifetime."""
        import importlib.util
        from pathlib import Path as _Path

        spec = importlib.util.spec_from_file_location(
            "belt_plan_f680",
            _Path(__file__).resolve().parents[1] / "lane" / "belt_plan.py")
        belt_plan = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(belt_plan)
        self.assertEqual(belt_plan.MONITOR_TIMEOUT_MS, MONITOR_LIFETIME_S * 1000)

    def test_no_canonical_sleep_can_outlive_the_declared_lifetime(self):
        """The two numbers the original defect set against each other, checked
        together: with a 300s base inside MONITOR_LIFETIME_S, every scheduled
        sleep fits."""
        now = 0.0
        while True:
            sleep_for = sleep_before_next_poll(300, 9, MONITOR_LIFETIME_S, now)
            if sleep_for is None:
                break
            self.assertLessEqual(now + sleep_for, MONITOR_LIFETIME_S)
            now += sleep_for


class RecordLineRefsTests(unittest.TestCase):
    """harmonic-forge#685 preclose finding 3. `queue_cycle` and
    `branch_ahead_lines` are the two emit sites that print a `<repo>#<issue>
    ...` row to stdout with no matching `tick.record_match`/`record_emit`
    call beside it -- every AE/sweep/queued-for-l3/branch-ahead event they
    surface was invisible to the tick log even on a belt that otherwise
    ticked correctly."""

    def _tick(self):
        from belt_mechanics import TickLog
        return TickLog(path=Path(self.tmp.name) / "ticks.jsonl", lane="l3",
                        trigger="belt")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_queued_for_line_is_recorded_bare(self):
        tick = self._tick()
        watch_lane_posts._record_line_refs(
            tick, ["vitalharmony/hrse#1530 queued-for-l3 kind=ready-for-l3"])
        self.assertEqual([e["id"] for e in tick.matched], ["hrse#1530"])
        self.assertEqual([e["id"] for e in tick.emitted], ["hrse#1530"])

    def test_a_left_queue_retraction_is_not_recorded(self):
        """A retraction names an issue that dropped out -- there is no
        marker to record, unlike a genuine detection."""
        tick = self._tick()
        watch_lane_posts._record_line_refs(
            tick, ["vitalharmony/hrse#1600 left-queue-for-l3"])
        self.assertEqual(tick.matched, [])
        self.assertEqual(tick.emitted, [])

    def test_a_branch_ahead_line_is_recorded_bare(self):
        tick = self._tick()
        watch_lane_posts._record_line_refs(
            tick, ["vitalharmony/hrse#1530 branch fix/1530-x is 2 commits "
                   "ahead of origin/main with no completion posted"])
        self.assertEqual([e["id"] for e in tick.matched], ["hrse#1530"])

    def test_none_tick_is_a_silent_no_op(self):
        """The belt runs with `tick=None` outside an armed belt (e.g. a
        `--repo/--issues` one-shot); recording must not require one."""
        watch_lane_posts._record_line_refs(None, ["vitalharmony/hrse#1 queued-for-l3 kind=ae"])  # no raise


class ReadQueueCandidatesTests(unittest.TestCase):
    """harmonic-forge#691 (rescoped). The no-worktree-yet replacement for
    #686's removed scan: `l1_post.py`/`l2_post.py`/`post_lane_discussion.py`
    write via the shared `belt_candidates` module, this reads -- no GitHub
    call on this side, ever. `read_queue_candidates` is now a thin wrapper
    over `belt_candidates.read_candidates` bound to this repo's own
    `QUEUE_KINDS`/`QUEUE_POSTERS`, so it takes `lane` and filters by
    kind/poster eligibility (AC2'), not just repo and age."""

    def _record(self, base, repo, issue, kind, posted_by):
        belt_candidates.record_candidate(repo, issue, kind, posted_by, base_dir=base)

    def test_a_recent_eligible_entry_is_returned(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            self._record(base, "vitalharmony/hrse", 1921, "handoff", "l1")
            got = watch_lane_posts.read_queue_candidates(
                ["vitalharmony/hrse"], "l2",
                now=dt.datetime(2026, 9, 18, 12, tzinfo=dt.timezone.utc), base_dir=base)
        self.assertEqual(got, {("vitalharmony/hrse", 1921)})

    def test_an_entry_for_a_repo_not_in_the_manifest_is_excluded(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            self._record(base, "vitalharmony/other", 1, "handoff", "l1")
            got = watch_lane_posts.read_queue_candidates(
                ["vitalharmony/hrse"], "l2",
                now=dt.datetime(2026, 9, 18, 12, tzinfo=dt.timezone.utc), base_dir=base)
        self.assertEqual(got, set())

    def test_an_entry_older_than_the_max_age_is_excluded(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            self._record(base, "vitalharmony/hrse", 1, "handoff", "l1")
            got = watch_lane_posts.read_queue_candidates(
                ["vitalharmony/hrse"], "l2",
                now=dt.datetime(2026, 10, 18, tzinfo=dt.timezone.utc), base_dir=base)
        self.assertEqual(got, set())

    def test_a_kind_ineligible_for_the_requesting_lane_is_excluded(self):
        """AC2': the reader returns a candidate only when its newest entry
        is queue-eligible for the requesting lane -- `discussion` is not in
        `QUEUE_KINDS` for any lane."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            self._record(base, "vitalharmony/hrse", 1, "discussion", "l1")
            got = watch_lane_posts.read_queue_candidates(
                ["vitalharmony/hrse"], "l2",
                now=dt.datetime(2026, 9, 18, 12, tzinfo=dt.timezone.utc), base_dir=base)
        self.assertEqual(got, set())

    def test_lane3_is_populated_by_l1_post_kinds_ac7(self):
        """AC7': Lane 3 is included deliberately -- its
        ready-for-l3/ae/sweep/ae-and-sweep kinds are already
        `l1_post.py`-posted (posted_by='l1'), which `QUEUE_POSTERS['l3']`
        already accepts."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            self._record(base, "vitalharmony/hrse", 1530, "ready-for-l3", "l1")
            got = watch_lane_posts.read_queue_candidates(
                ["vitalharmony/hrse"], "l3",
                now=dt.datetime(2026, 9, 18, 12, tzinfo=dt.timezone.utc), base_dir=base)
        self.assertEqual(got, {("vitalharmony/hrse", 1530)})

    def test_a_missing_directory_returns_empty_not_an_error(self):
        got = watch_lane_posts.read_queue_candidates(
            ["vitalharmony/hrse"], "l2", base_dir=Path("/nonexistent/dir"))
        self.assertEqual(got, set())

    def test_a_malformed_file_is_skipped_not_fatal(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            base.mkdir(parents=True, exist_ok=True)
            (base / "vitalharmony__hrse__99.json").write_text("not json at all", encoding="utf-8")
            self._record(base, "vitalharmony/hrse", 5, "handoff", "l1")
            got = watch_lane_posts.read_queue_candidates(
                ["vitalharmony/hrse"], "l2",
                now=dt.datetime(2026, 9, 18, 12, tzinfo=dt.timezone.utc), base_dir=base)
        self.assertEqual(got, {("vitalharmony/hrse", 5)})

    def test_no_gh_call_is_ever_made(self):
        """The whole point: this reads local files, never GitHub."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            self._record(base, "vitalharmony/hrse", 1, "handoff", "l1")
            with patch("belt_mechanics.subprocess.run",
                      side_effect=AssertionError("no gh call expected")):
                got = watch_lane_posts.read_queue_candidates(
                    ["vitalharmony/hrse"], "l2",
                    now=dt.datetime(2026, 9, 18, 12, tzinfo=dt.timezone.utc), base_dir=base)
        self.assertEqual(got, {("vitalharmony/hrse", 1)})
