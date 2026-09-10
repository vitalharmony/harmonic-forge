#!/usr/bin/env python3
"""Unit tests for watch_lane_posts.py (harmonic-forge#442) -- pure parsing
logic only, no live gh/API calls. Fixtures are real comment bodies from
hrse#1530 (trimmed), not invented shapes."""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from watch_lane_posts import (
    _BRANCH_ISSUE_RE,
    _classify,
    discover_from_worktree,
    discover_l1_sweep,
    discover_queue,
    list_open_issues,
    report_resolution,
    resolve_worktree,
)


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
            self.assertEqual(discover_queue("vitalharmony/hrse", "l3"), {1530: "ready-for-l3"})

    def test_issue_superseded_by_a_later_comment_is_not_queued(self):
        """The self-clearing property: once Lane 3 (or anyone) posts after
        the marker, the issue drops out with no separate bookkeeping."""
        with patch("belt_mechanics.subprocess.run",
                  side_effect=self._mock_gh(
                      search_results={"ready-for-l3": [1530], "ae": [], "sweep": []},
                      comments={1530: [self._l1("ready-for-l3"),
                                       "## Lane 3 Gate Results — PASS"]},
                  )):
            self.assertEqual(discover_queue("vitalharmony/hrse", "l3"), {})

    def test_no_search_hits_yields_empty_queue(self):
        with patch("belt_mechanics.subprocess.run",
                  side_effect=self._mock_gh(search_results={}, comments={})):
            self.assertEqual(discover_queue("vitalharmony/hrse", "l3"), {})

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
            queue = discover_queue("vitalharmony/hrse", "l3")
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
            queue = discover_queue("vitalharmony/hrse", "l3")
            self.assertEqual(queue, {1058: "ready-for-l3", 1531: "ready-for-l3"})


class L1SweepTests(unittest.TestCase):
    """harmonic-forge#570 AC1/AC8 -- Lane 1's repo-wide newest-marker sweep,
    given a runnable form. Every `gh` call mocked, no network."""

    def _mock_gh(self, open_issues: list[int], comments: dict):
        def run(argv, **kwargs):
            self.assertEqual(argv[0], "gh-as")
            gh_argv = argv[3:]
            if any(a == "repos/vitalharmony/hrse/issues" for a in gh_argv):
                # harmonic-forge#570 preclose finding: `gh api` promotes a
                # call with `-f` params to POST unless told otherwise, and a
                # POST here is issue *creation* -- assert the explicit GET
                # so a regression back to an implicit POST fails this test
                # rather than silently returning nothing in production.
                self.assertIn("-X", gh_argv, f"issues list must be explicit GET: {gh_argv}")
                self.assertEqual(gh_argv[gh_argv.index("-X") + 1], "GET")
                return _fake_completed("\n".join(str(n) for n in open_issues))
            if "api" in gh_argv:
                path = next(a for a in gh_argv if a.startswith("repos/") and "/comments" in a)
                issue = int(path.rsplit("/", 2)[-2])
                bodies = comments.get(issue, [])
                return _fake_completed(json.dumps([{"body": b} for b in bodies]))
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

    def test_l1_sweep_still_checks_a_stale_already_queued_issue(self):
        """harmonic-forge#570 preclose finding: `since` must not drop an
        issue that stopped receiving updates but was never resolved -- it
        must stay in the candidate set via `extra_issues`."""
        with patch("belt_mechanics.subprocess.run",
                  side_effect=self._mock_gh(
                      [],  # nothing NEW since the watermark
                      {1530: ["## L2D — receipt-backed status (harmonic-forge#371)"]},
                  )):
            queue = discover_l1_sweep("vitalharmony/hrse", since="2026-09-01T00:00:00Z",
                                      extra_issues=[1530])
            self.assertEqual(list(queue), [1530])

    def test_issue_whose_newest_comment_is_lane2_needs_l1(self):
        with patch("belt_mechanics.subprocess.run",
                  side_effect=self._mock_gh(
                      [1530],
                      {1530: ["## L2D — receipt-backed status (harmonic-forge#371)"]},
                  )):
            queue = discover_l1_sweep("vitalharmony/hrse")
            self.assertEqual(list(queue), [1530])
            lane, detail = queue[1530]
            self.assertEqual(lane, "l2")

    def test_issue_whose_newest_comment_is_lane1s_own_is_not_queued(self):
        body = "## Some post\n\n<!-- l1-post v1; kind=discussion; posted-by=LANE1 -->"
        with patch("belt_mechanics.subprocess.run",
                  side_effect=self._mock_gh([1530], {1530: [body]})):
            self.assertEqual(discover_l1_sweep("vitalharmony/hrse"), {})

    def test_issue_with_no_classified_comment_carries_no_ball(self):
        with patch("belt_mechanics.subprocess.run",
                  side_effect=self._mock_gh([1530], {1530: ["just chat, no heading"]})):
            self.assertEqual(discover_l1_sweep("vitalharmony/hrse"), {})

    def test_issue_with_zero_comments_carries_no_ball(self):
        with patch("belt_mechanics.subprocess.run",
                  side_effect=self._mock_gh([1530], {})):
            self.assertEqual(discover_l1_sweep("vitalharmony/hrse"), {})


if __name__ == "__main__":
    unittest.main()
