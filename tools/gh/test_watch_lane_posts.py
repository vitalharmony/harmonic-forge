#!/usr/bin/env python3
"""Unit tests for watch_lane_posts.py (harmonic-forge#442) -- pure parsing
logic only, no live gh/API calls. Fixtures are real comment bodies from
hrse#1530 (trimmed), not invented shapes."""
import io
import json
import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import watch_lane_posts
from watch_lane_posts import (
    QUEUE_KINDS,
    _BRANCH_ISSUE_RE,
    _classify,
    branch_ahead_lines,
    branch_ahead_without_completion,
    discover_from_worktree,
    discover_l1_sweep,
    discover_queue,
    drop_closed_targets,
    RootNotARepo,
    enumerate_repo_roots,
    enumerate_worktrees,
    l1_sweep_cycle,
    list_open_issues,
    report_resolution,
    resolve_worktree,
)

#: `skills/belt-and-suspenders/SKILL.md`, two directories above `tools/gh/`
#: (the same relative path the skill's own docstring names, harmonic-
#: forge#570).
_SKILL_MD = Path(__file__).resolve().parent.parent.parent / "skills" / "belt-and-suspenders" / "SKILL.md"


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

    def _mock_gh(self, search_results: dict, comments: dict):
        """`search_results`: {kind: [issue numbers]}. `comments`: {issue:
        [comment bodies, in order]}.

        harmonic-forge#518: every call now goes through
        `belt_mechanics.gh_as`, so the argv shape is
        `["gh-as", <account>, "gh", ...]` and the search half is REST
        (`search/issues`, returning `{"items": [...]}`) rather than the
        GraphQL-backed `gh search issues`. The behaviour asserted below is
        unchanged — only the call these tests intercept moved.
        """
        def run(argv, **kwargs):
            self.assertEqual(argv[0], "gh-as",
                             f"every call must be account-scoped: {argv}")
            gh_argv = argv[3:]  # strip ["gh-as", <account>, "gh"]
            if gh_argv[:4] == ["api", "-X", "GET", "search/issues"]:
                # -f q=repo:R state:open <marker text>
                query = gh_argv[gh_argv.index("-f") + 1]
                kind = query.rsplit("kind=", 1)[-1]
                numbers = search_results.get(kind, [])
                return _fake_completed(
                    json.dumps({"items": [{"number": n} for n in numbers]})
                )
            if "api" in gh_argv:
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
            raise AssertionError(f"unexpected gh call: {argv}")
        return run

    def test_issue_with_matching_marker_as_latest_comment_is_queued(self):
        with patch("belt_mechanics.subprocess.run",
                  side_effect=self._mock_gh(
                      search_results={"ready-for-l3": [1530], "ae": [], "sweep": []},
                      comments={1530: [self._l1("handoff"), self._l1("ready-for-l3")]},
                  )):
            self.assertEqual(discover_queue("vitalharmony/hrse", "l3")[0], {1530: "ready-for-l3"})

    def test_issue_superseded_by_a_later_comment_is_not_queued(self):
        """The self-clearing property: once Lane 3 (or anyone) posts after
        the marker, the issue drops out with no separate bookkeeping."""
        with patch("belt_mechanics.subprocess.run",
                  side_effect=self._mock_gh(
                      search_results={"ready-for-l3": [1530], "ae": [], "sweep": []},
                      comments={1530: [self._l1("ready-for-l3"),
                                       "## Lane 3 Gate Results — PASS"]},
                  )):
            self.assertEqual(discover_queue("vitalharmony/hrse", "l3")[0], {})

    def test_no_search_hits_yields_empty_queue(self):
        with patch("belt_mechanics.subprocess.run",
                  side_effect=self._mock_gh(search_results={}, comments={})):
            self.assertEqual(discover_queue("vitalharmony/hrse", "l3")[0], {})

    def test_issue_found_by_two_kind_searches_is_deduplicated(self):
        """A search hit is only a candidate; the real classification decides
        the kind. An issue matching two searches (e.g. it once carried an
        `ae` marker, later superseded by `ready-for-l3`) must appear once,
        with whichever kind is actually latest."""
        with patch("belt_mechanics.subprocess.run",
                  side_effect=self._mock_gh(
                      search_results={"ready-for-l3": [1530], "ae": [1530], "sweep": []},
                      comments={1530: [self._l1("ae"), self._l1("ready-for-l3")]},
                  )):
            queue, _ok = discover_queue("vitalharmony/hrse", "l3")
            self.assertEqual(queue, {1530: "ready-for-l3"})

    def test_two_real_currently_queued_issues_hrse1058_and_1531(self):
        """Live shape observed 2026-09-03: two separate issues, each with
        its own ready-for-l3 comment, no cross-contamination."""
        with patch("belt_mechanics.subprocess.run",
                  side_effect=self._mock_gh(
                      search_results={"ready-for-l3": [1058, 1531], "ae": [], "sweep": []},
                      comments={
                          1058: [self._l1("handoff"), "## L2P", "## L2D",
                                 self._l1("ready-for-l3")],
                          1531: [self._l1("handoff"), "## L2P", "## L2D",
                                 self._l1("ready-for-l3")],
                      },
                  )):
            queue, _ok = discover_queue("vitalharmony/hrse", "l3")
            self.assertEqual(queue, {1058: "ready-for-l3", 1531: "ready-for-l3"})

    def test_ae_and_sweep_marker_is_queued_for_l3(self):
        """harmonic-forge#579 AC2 -- live reproduction: `kind=ae-and-sweep`
        is a real, valid Lane 1 choice with 9 live markers already on
        vitalharmony/hrse. Before this fix, `QUEUE_KINDS["l3"]` did not
        contain the string at all, so `_search_candidates` never even
        looked for it and `discover_queue` printed nothing for a real,
        actionable AE."""
        with patch("belt_mechanics.subprocess.run",
                  side_effect=self._mock_gh(
                      search_results={"ready-for-l3": [], "ae": [], "sweep": [],
                                       "ae-and-sweep": [1725]},
                      comments={1725: [self._l1("ae-and-sweep")]},
                  )):
            queue, _ok = discover_queue("vitalharmony/hrse", "l3")
            self.assertEqual(queue, {1725: "ae-and-sweep"})

    def test_l2_finding_after_ready_for_l3_does_not_drop_the_issue(self):
        """harmonic-forge#580 AC1 -- live reproduction: a `## L2 Finding`
        comment posted after `ready-for-l3` must not change the issue's
        Lane 3 queue membership. Before the fix, the finding became
        `last_kind`, failed the `last_kind[0] == 'l1'` check, and silently
        dropped a genuinely queued issue out of the belt."""
        with patch("belt_mechanics.subprocess.run",
                  side_effect=self._mock_gh(
                      search_results={"ready-for-l3": [571], "ae": [], "sweep": []},
                      comments={571: [
                          self._l1("ready-for-l3"),
                          "## L2 Finding — receipt-backed finding (harmonic-forge#571)",
                      ]},
                  )):
            queue, _ok = discover_queue("vitalharmony/hrse", "l3")
            self.assertEqual(queue, {571: "ready-for-l3"})

    def test_l2_finding_does_not_resurrect_a_superseded_issue(self):
        """The finding-skip must not go too far the other direction: an
        issue genuinely superseded by a real status transition (not a
        finding) must still drop, finding present or not. Uses an `l3`-
        classified superseding comment, which never reaches the `l2`-only
        skip predicate at all -- a coarser check than the one below."""
        with patch("belt_mechanics.subprocess.run",
                  side_effect=self._mock_gh(
                      search_results={"ready-for-l3": [571], "ae": [], "sweep": []},
                      comments={571: [
                          self._l1("ready-for-l3"),
                          "## L2 Finding — receipt-backed finding (harmonic-forge#571)",
                          "## Lane 3 Gate Results — PASS",
                      ]},
                  )):
            queue, _ok = discover_queue("vitalharmony/hrse", "l3")
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
                      search_results={"handoff": [571], "rework": []},
                      comments={571: [
                          self._l1("handoff"),
                          "## L2D — receipt-backed status (harmonic-forge#371)",
                      ]},
                  )):
            queue, _ok = discover_queue("vitalharmony/hrse", "l2")
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
                      search_results={"ready-for-l3": [571], "ae": [], "sweep": []},
                      comments={571: [self._l1("ready-for-l3"), finding_body]},
                  )):
            queue, _ok = discover_queue("vitalharmony/hrse", "l3")
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
    worktrees; the repo-wide sweep is the suspenders' backstop, not the belt's
    pull source. Asserted mechanically because the regression that produced
    #590 was a prose edit that read plausibly."""

    def _lane1_belt_command(self) -> str:
        text = _SKILL_MD.read_text(encoding="utf-8")
        match = re.search(r"^- \*\*Lane 1\*\*.*?```\n(.*?)```",
                          text, re.MULTILINE | re.DOTALL)
        self.assertIsNotNone(match, "SKILL.md's Lane 1 belt command block was not found")
        return match.group(1)

    def test_lane1_belt_arms_all_worktrees(self):
        self.assertIn("--all-worktrees", self._lane1_belt_command())

    def test_lane1_belt_does_not_arm_the_repo_wide_sweep(self):
        self.assertNotIn("--queue-for l1", self._lane1_belt_command(),
                         "the repo-wide sweep is the suspenders' backstop "
                         "(harmonic-forge#590); arming it as the belt is the "
                         "regression this issue fixed")

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

    def test_suspenders_still_carry_the_repo_wide_sweep(self):
        """AC2: demoted to the pull loop, not deleted -- and reachable there,
        as a literal command, not a description of one."""
        text = _SKILL_MD.read_text(encoding="utf-8")
        suspenders = text.split("## The suspenders", 1)
        self.assertEqual(len(suspenders), 2, "suspenders section not found")
        self.assertIn("--queue-for l1", suspenders[1],
                      "the repo-wide sweep must be armed somewhere; the "
                      "suspenders' pull loop is where harmonic-forge#590 put it")

    def test_discover_l1_sweep_is_kept_not_deleted(self):
        """AC2: demoted, not removed -- it is still the suspenders' backstop."""
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

    def test_the_suspenders_sweep_derives_its_repo_set_too(self):
        """The Lane 1 backstop is a --queue-for command outside any lane
        bullet, so the per-lane guard above never reaches it."""
        text = _SKILL_MD.read_text(encoding="utf-8")
        sweep = re.search(r"(watch_lane_posts\.py --queue-for l1[^\n]*)",
                          text.split("## The suspenders", 1)[1])
        self.assertIsNotNone(sweep, "the suspenders' Lane 1 sweep command was not found")
        self.assertIn("--account-repos", sweep.group(1))
        self.assertNotIn("--repo vitalharmony/", sweep.group(1))


class DiscoverQueueFailsClosedPerIssueTests(unittest.TestCase):
    """harmonic-forge#602 — found by an out-of-family (Codex/gpt-5.6-sol)
    review of the design assessment, after four in-family passes missed it.

    `_fetch_all_comments` returns None on a FAILED fetch. `discover_queue`
    consumed that as `or ()` and still reported `fetch_ok=True`, so
    `queue_cycle` counted the repo as reporting and retracted the issue with
    `left-queue-for-<lane>` -- telling the lane the ball moved on because one
    comment fetch hit a rate limit."""

    def test_one_issue_comment_fetch_failure_marks_the_repo_unreliable(self):
        with patch("watch_lane_posts._search_candidates", return_value={1530}), \
             patch("watch_lane_posts._fetch_all_comments", return_value=None):
            queued, fetch_ok = discover_queue("vitalharmony/hrse", "l3")
        self.assertFalse(fetch_ok, "a failed comment fetch must not report success")
        self.assertEqual(queued, {})

    def test_a_genuinely_empty_comment_list_still_reports_success(self):
        """`[]` means zero comments and must stay distinguishable from None --
        otherwise the fix trades a false retraction for a stuck queue."""
        with patch("watch_lane_posts._search_candidates", return_value={1530}), \
             patch("watch_lane_posts._fetch_all_comments", return_value=[]):
            queued, fetch_ok = discover_queue("vitalharmony/hrse", "l3")
        self.assertTrue(fetch_ok)
        self.assertEqual(queued, {})

    def test_queue_cycle_does_not_retract_on_an_issue_level_failure(self):
        """The end-to-end property: the previously-queued issue survives."""
        last = {("vitalharmony/hrse", 1530): "ready-for-l3"}
        with patch("watch_lane_posts.discover_queue", return_value=({}, False)):
            queue, lines, ok = watch_lane_posts.queue_cycle(
                ["vitalharmony/hrse"], "l3", last, {}, "2026-09-10T00:00:00Z")
        self.assertEqual(queue, last, "the prior queue must carry forward")
        self.assertEqual(ok, set(), "a failed repo must not be counted as reporting")
        self.assertFalse([l for l in lines if "left-queue" in l])


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


if __name__ == "__main__":
    unittest.main()
