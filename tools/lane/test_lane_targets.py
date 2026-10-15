"""hrse#2081 AC5: prove `select_targets`'s advisor mode targets both shapes
AC2 names -- a pane-hosted advisor process, and a daemon-hosted one reachable
only by name (`ADVISOR_NAMES`) -- while excluding lane pids, tracked lane
session ids, and any match against a *lane*'s name. This is the property
`--stop-advisor` depends on to reach the advisor's actual Claude session
without ever touching a lane session when the advisor is closed."""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import lane_targets as lt

LANE_NAMES = {"Lane 1", "Lane 2", "Lane 3", "Advisor", "Lane Advisor"}
VH = "/home/u/Harmonic_Projects/HRSE2"
LP = "/home/u/Harmonic_Projects/LeasePAL-App-Prototype"
VH_DIRS = {VH, VH + "-lane2", VH + "-lane3"}
LP_DIRS = {LP, LP + "-lane2", LP + "-lane3"}


def _agent(session_id, pid, kind="interactive", name=None, cwd=None):
    entry = {"sessionId": session_id, "pid": pid, "kind": kind}
    if name is not None:
        entry["name"] = name
    if cwd is not None:
        entry["cwd"] = cwd
    return entry


class TestAdvisorMode(unittest.TestCase):
    def test_targets_only_an_advisor_pane_process(self):
        listing = [_agent("adv-sid", 501)]
        result = lt.select_targets(
            lt.ADVISOR, listing, panes={501}, tracked=set(),
            lane_names=LANE_NAMES, me=set())
        assert result == listing

    def test_excludes_a_lane_pid_even_if_present_in_listing(self):
        listing = [_agent("adv-sid", 501), _agent("lane1-sid", 601)]
        result = lt.select_targets(
            lt.ADVISOR, listing, panes={501}, tracked=set(),
            lane_names=LANE_NAMES, me=set())
        assert result == [listing[0]]

    def test_excludes_a_tracked_lane_session_id_not_under_advisor_panes(self):
        """A tracked lane session id must never widen the advisor's own
        target set -- that state file belongs to `stop`'s teardown."""
        listing = [_agent("lane2-sid", 602)]
        result = lt.select_targets(
            lt.ADVISOR, listing, panes={501}, tracked={"lane2-sid"},
            lane_names=LANE_NAMES, me=set())
        assert result == []

    def test_targets_a_daemon_hosted_advisor_by_name_with_no_pid_at_all(self):
        """AC2's second clause: the advisor's own Claude session is
        daemon-hosted (real `claude agents --json` background entries carry
        no `pid` key at all, only `kind`+`name`), so pane membership alone
        can never match it. Without this, closing the advisor window kills
        the tmux session but leaves the actual Claude process leaked and
        unreachable -- the defect this test was written to catch after a
        preclose-inspection panel found the prior version of this test
        asserted the opposite of AC2's requirement."""
        listing = [{"sessionId": "adv-sid", "kind": "background", "name": "Advisor", "cwd": VH}]
        result = lt.select_targets(
            lt.ADVISOR, listing, panes={501}, tracked=set(),
            lane_names=LANE_NAMES, me=set(), workspace_dirs=VH_DIRS)
        assert result == listing

    def test_targets_a_daemon_hosted_advisor_under_the_legacy_name_too(self):
        listing = [{"sessionId": "adv-sid", "kind": "background", "name": "Lane Advisor", "cwd": VH}]
        result = lt.select_targets(
            lt.ADVISOR, listing, panes=set(), tracked=set(),
            lane_names=LANE_NAMES, me=set(), workspace_dirs=VH_DIRS)
        assert result == listing

    def test_excludes_a_background_lane_session_by_name(self):
        """Advisor mode must never match a LANE by name -- it uses its own
        narrow ADVISOR_NAMES, never the full `lane_names` passed in (which
        also contains "Lane 1"/"Lane 2"/"Lane 3")."""
        listing = [_agent("lane1-sid", None, kind="background", name="Lane 1")]
        result = lt.select_targets(
            lt.ADVISOR, listing, panes=set(), tracked=set(),
            lane_names=LANE_NAMES, me=set())
        assert result == []

    def test_excludes_an_advisor_named_session_from_an_unrelated_workspace_if_interactive(self):
        """An *interactive* session named "Advisor" (not daemon-hosted) is
        never a background entry, so the name check (kind=="background")
        cannot match it -- only an actual daemon-hosted Advisor/Lane
        Advisor session, or a pane-pid match, counts."""
        listing = [_agent("other-ws-sid", 701, kind="interactive", name="Advisor")]
        result = lt.select_targets(
            lt.ADVISOR, listing, panes={501}, tracked=set(),
            lane_names=LANE_NAMES, me=set())
        assert result == []

    def test_excludes_my_own_pid(self):
        listing = [_agent("adv-sid", 501)]
        result = lt.select_targets(
            lt.ADVISOR, listing, panes={501}, tracked=set(),
            lane_names=LANE_NAMES, me={501})
        assert result == []


class TestStopMode(unittest.TestCase):
    """The pre-existing default behavior, now routed through the extracted
    function -- must keep working identically."""

    def test_tracked_session_id_is_a_target(self):
        listing = [_agent("tracked-sid", 801)]
        result = lt.select_targets(
            lt.STOP, listing, panes=set(), tracked={"tracked-sid"},
            lane_names=LANE_NAMES, me=set())
        assert result == listing

    def test_pane_pid_is_a_target(self):
        listing = [_agent("pane-sid", 802)]
        result = lt.select_targets(
            lt.STOP, listing, panes={802}, tracked=set(),
            lane_names=LANE_NAMES, me=set())
        assert result == listing

    def test_background_name_fallback_is_a_target(self):
        listing = [_agent("bg-sid", 803, kind="background", name="Lane 1", cwd=VH)]
        result = lt.select_targets(
            lt.STOP, listing, panes=set(), tracked=set(),
            lane_names=LANE_NAMES, me=set(), workspace_dirs=VH_DIRS)
        assert result == listing

    def test_interactive_name_match_alone_is_not_a_target(self):
        """The name fallback is for daemon-hosted (background) sessions
        only -- a live interactive "Lane 1" may belong to another
        workspace entirely."""
        listing = [_agent("other-sid", 804, kind="interactive", name="Lane 1")]
        result = lt.select_targets(
            lt.STOP, listing, panes=set(), tracked=set(),
            lane_names=LANE_NAMES, me=set())
        assert result == []

    def test_no_sessionid_is_never_a_target(self):
        listing = [_agent(None, 805, kind="background", name="Lane 1")]
        result = lt.select_targets(
            lt.STOP, listing, panes={805}, tracked=set(),
            lane_names=LANE_NAMES, me=set())
        assert result == []


class TestWorkspaceScope(unittest.TestCase):
    """hrse#2258: every workspace's lanes carry the names `Lane 1`..`Lane 3` and
    `Advisor`, so the daemon-hosted name fallback must be scoped to the workspace whose
    window is closing. `_git_common_dir` is the one subprocess; patched here."""

    def _two_workspaces(self):
        return [
            _agent("vh-l1", None, kind="background", name="Lane 1", cwd=VH),
            _agent("vh-l2", None, kind="background", name="Lane 2", cwd=VH + "-lane2"),
            _agent("lp-l1", None, kind="background", name="Lane 1", cwd=LP),
            _agent("lp-adv", None, kind="background", name="Advisor", cwd=LP),
            _agent("vh-adv", None, kind="background", name="Advisor", cwd=VH),
        ]

    def test_ac1_stop_guard_and_advisor_only_reach_the_closing_workspace(self):
        listing = self._two_workspaces()
        with mock.patch.object(lt, "_git_common_dir", return_value=None):
            for mode in (lt.STOP, lt.ADVISOR):
                got = {a["sessionId"] for a in lt.select_targets(
                    mode, listing, panes=set(), tracked=set(), lane_names=LANE_NAMES,
                    me=set(), workspace_dirs=LP_DIRS)}
                assert got == ({"lp-l1", "lp-adv"} if mode == lt.STOP else {"lp-adv"}), mode
                got_vh = {a["sessionId"] for a in lt.select_targets(
                    mode, listing, panes=set(), tracked=set(), lane_names=LANE_NAMES,
                    me=set(), workspace_dirs=VH_DIRS)}
                assert got_vh == ({"vh-l1", "vh-l2", "vh-adv"} if mode == lt.STOP else {"vh-adv"}), mode

    def test_ac2_a_background_entry_with_no_cwd_is_never_targeted_by_name(self):
        listing = [_agent("no-cwd", None, kind="background", name="Lane 1")]
        for mode in (lt.STOP, lt.ADVISOR):
            with mock.patch.object(lt, "_git_common_dir", return_value=VH + "/.git"):
                assert lt.select_targets(
                    mode, listing, panes=set(), tracked=set(), lane_names=LANE_NAMES,
                    me=set(), workspace_dirs=VH_DIRS) == []

    def test_ac3_a_per_issue_worktree_belongs_to_its_repository_workspace_only(self):
        entry = _agent("wt", None, kind="background", name="Lane 2",
                       cwd="/home/u/Harmonic_Projects/.worktrees/hrse2-2258-impl")
        commons = {entry["cwd"]: VH + "/.git", VH: VH + "/.git", VH + "-lane2": VH + "/.git",
                   VH + "-lane3": VH + "/.git", LP: LP + "/.git", LP + "-lane2": LP + "/.git",
                   LP + "-lane3": LP + "/.git"}
        with mock.patch.object(lt, "_git_common_dir", side_effect=commons.get):
            assert lt.select_targets(
                lt.STOP, [entry], panes=set(), tracked=set(), lane_names=LANE_NAMES,
                me=set(), workspace_dirs=VH_DIRS) == [entry]
            assert lt.select_targets(
                lt.STOP, [entry], panes=set(), tracked=set(), lane_names=LANE_NAMES,
                me=set(), workspace_dirs=LP_DIRS) == []

    def test_a_failing_git_call_counts_as_not_in_the_workspace(self):
        entry = _agent("stale", None, kind="background", name="Lane 1", cwd="/gone/worktree")
        with mock.patch.object(lt, "_git_common_dir", return_value=None):
            assert lt.select_targets(
                lt.STOP, [entry], panes=set(), tracked=set(), lane_names=LANE_NAMES,
                me=set(), workspace_dirs=VH_DIRS) == []

    def test_ac4_without_workspace_dirs_the_name_fallback_matches_nothing(self):
        listing = self._two_workspaces()
        for dirs in (None, set()):
            for mode in (lt.STOP, lt.ADVISOR):
                assert lt.select_targets(
                    mode, listing, panes=set(), tracked=set(), lane_names=LANE_NAMES,
                    me=set(), workspace_dirs=dirs) == []

    def test_tracked_ids_and_pane_pids_are_unchanged_by_the_scope(self):
        listing = [_agent("tracked", 1, kind="background", name="Lane 1", cwd=LP),
                   _agent("pane", 2, cwd=LP)]
        got = lt.select_targets(lt.STOP, listing, panes={2}, tracked={"tracked"},
                                lane_names=LANE_NAMES, me=set(), workspace_dirs=VH_DIRS)
        assert got == listing

    def test_the_recorded_dirs_common_dirs_are_computed_once_per_call(self):
        listing = [_agent(f"s{n}", None, kind="background", name="Lane 1",
                          cwd=f"/elsewhere/wt{n}") for n in range(4)]
        with mock.patch.object(lt, "_git_common_dir", return_value=VH + "/.git") as git:
            lt.select_targets(lt.STOP, listing, panes=set(), tracked=set(),
                              lane_names=LANE_NAMES, me=set(), workspace_dirs=VH_DIRS)
        assert git.call_count == 4 + len(VH_DIRS)


class TestWorkspaceDirsFromPaths(unittest.TestCase):
    """`workspace_dirs_from_paths`: teardown's scope, derived from where the live panes are."""

    def _toplevels(self, mapping):
        return mock.patch.object(lt, "_git_toplevel", side_effect=lambda p: mapping.get(p))

    def test_a_pane_path_gives_its_repo_and_both_lane_worktrees(self):
        with self._toplevels({"/w/HRSE2-lane2/backend": "/w/HRSE2-lane2"}):
            assert lt.workspace_dirs_from_paths(["/w/HRSE2-lane2/backend"]) == {
                "/w/HRSE2", "/w/HRSE2-lane2", "/w/HRSE2-lane3"}

    def test_a_path_outside_any_repository_is_ignored(self):
        """A pane back at a shell in $HOME must never become a scope root: every session
        under it would match."""
        with self._toplevels({}):
            assert lt.workspace_dirs_from_paths(["/home/u", "/tmp"]) == set()

    def test_a_per_issue_worktree_is_its_own_scope_root_and_matches_through_its_repo(self):
        entry = _agent("wt", None, kind="background", name="Lane 2", cwd="/w/HRSE2-lane2")
        commons = {"/w/.worktrees/hrse2-1-impl": "/w/HRSE2/.git", "/w/HRSE2-lane2": "/w/HRSE2/.git",
                   "/w/HRSE2": "/w/HRSE2/.git", "/w/HRSE2-lane3": "/w/HRSE2/.git"}
        with self._toplevels({"/w/.worktrees/hrse2-1-impl": "/w/.worktrees/hrse2-1-impl"}), \
                mock.patch.object(lt, "_git_common_dir", side_effect=commons.get):
            dirs = lt.workspace_dirs_from_paths(["/w/.worktrees/hrse2-1-impl"])
            assert dirs == {"/w/.worktrees/hrse2-1-impl", "/w/.worktrees/hrse2-1-impl-lane2",
                            "/w/.worktrees/hrse2-1-impl-lane3"}
            assert lt.select_targets(lt.STOP, [entry], panes=set(), tracked=set(),
                                     lane_names=LANE_NAMES, me=set(), workspace_dirs=dirs) == [entry]

    def test_two_workspaces_on_two_repos_never_share_a_scope(self):
        tops = {"/w/HRSE2": "/w/HRSE2", "/w/LP-lane3": "/w/LP-lane3"}
        with self._toplevels(tops):
            assert lt.workspace_dirs_from_paths(["/w/HRSE2"]).isdisjoint(
                lt.workspace_dirs_from_paths(["/w/LP-lane3"]))


if __name__ == "__main__":
    unittest.main()
