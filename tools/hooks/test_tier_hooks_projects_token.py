"""harmonic-forge#1456: every Tier hook reads a vitalharmony board with the
projects token in a lane, through item_list_cache's one opt-in runner contract.

A fake `subprocess.run` refuses any GH_TOKEN but the projects token, so a read
that does not carry it comes back as an error (LOOKUP_FAILED)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HOOKS = Path(__file__).resolve().parent
GH = HOOKS.parent / "gh"
for path in (HOOKS, GH):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import item_list_cache  # noqa: E402
import model_tier_gate  # noqa: E402
import tier_model_trigger_check  # noqa: E402

PROJECTS_TOKEN = "projects-token-1456"
SLOT_TOKEN = "fine-grained-slot-token"


def _payload(owner: str, tier: str = "standard") -> str:
    node = {"project": {"number": 1, "owner": {"login": owner}}, "value": {"name": tier}}
    return json.dumps({"data": {"repository": {"issue": {"projectItems": {"nodes": [node]}}}}})


class Harness(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        (self.home / "vitalharmony-projects").mkdir()
        (self.home / "vitalharmony-projects" / "token").write_text(PROJECTS_TOKEN)
        self.seen: list[dict] = []

    def lane_env(self, lane: str, slot: str = "vitalharmony-lane") -> dict:
        return {"LANE": lane, "GH_ACCT_HOME": str(self.home),
                "GH_CONFIG_DIR": str(self.home / slot), "GH_TOKEN": SLOT_TOKEN,
                "PATH": os.environ.get("PATH", "")}

    def fake_run(self, owner: str):
        def run(cmd, *args, env=None, **kwargs):
            seen = dict(env if env is not None else os.environ)
            self.seen.append(seen)
            if seen.get("GH_TOKEN") != PROJECTS_TOKEN:
                return subprocess.CompletedProcess(cmd, 1, "", "Resource not accessible by personal access token")
            return subprocess.CompletedProcess(cmd, 0, _payload(owner), "")
        return run

    def patch_env(self, env: dict):
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)


class TierHooksReadWithProjectsToken(Harness):
    def _lookup(self, repo="vitalharmony/hrse", owner="vitalharmony"):
        boards = {repo.lower(): "1"}
        with mock.patch.object(tier_model_trigger_check, "_board_owners", return_value={repo.lower(): owner}), \
                mock.patch.object(model_tier_gate.subprocess, "run", side_effect=self.fake_run(owner)):
            return tier_model_trigger_check.lookup_tier(repo, 2819, boards)

    def test_trigger_check_reads_tier_in_lane_1_and_2(self):
        for lane in ("1", "2"):
            with self.subTest(lane=lane):
                self.patch_env(self.lane_env(lane))
                tier, error = self._lookup()
                self.assertEqual((tier, error), ("standard", None))
                self.assertEqual(self.seen[-1]["GH_TOKEN"], PROJECTS_TOKEN)

    def test_edit_gate_reads_tier_in_lane_1_and_2(self):
        for lane in ("1", "2"):
            with self.subTest(lane=lane):
                self.patch_env(self.lane_env(lane))
                with mock.patch.object(model_tier_gate.subprocess, "run", side_effect=self.fake_run("vitalharmony")):
                    tier, error = model_tier_gate.read_tier(
                        "vitalharmony/hrse", 2819, "1", run=model_tier_gate.timed_run, ttl=0, owner="vitalharmony")
                self.assertEqual((tier, error), ("standard", None))

    def test_downshift_reminder_reads_through_lookup_tier(self):
        import tier_downshift_reminder  # noqa: PLC0415
        self.assertIs(tier_downshift_reminder.tier_model_trigger_check.lookup_tier,
                      tier_model_trigger_check.lookup_tier)
        self.patch_env(self.lane_env("2"))
        self.assertEqual(self._lookup()[0], "standard")

    def test_kenekted_read_never_carries_the_projects_token(self):
        env = self.lane_env("1", slot="harmonicarchitect-lane")
        self.patch_env(env)
        with mock.patch.object(tier_model_trigger_check, "_board_owners",
                               return_value={"kenekted/kenekted-platform": "harmonicarchitect"}), \
                mock.patch.object(model_tier_gate.subprocess, "run", side_effect=self.fake_run("harmonicarchitect")):
            tier_model_trigger_check.lookup_tier("kenekted/kenekted-platform", 1,
                                                 {"kenekted/kenekted-platform": "1"})
        self.assertTrue(self.seen)
        self.assertTrue(all(s.get("GH_TOKEN") != PROJECTS_TOKEN for s in self.seen))

    def test_empty_projects_slot_is_lookup_failed(self):
        (self.home / "vitalharmony-projects" / "token").write_text("")
        self.patch_env(self.lane_env("1"))
        tier, error = self._lookup()
        self.assertEqual(tier, model_tier_gate.LOOKUP_FAILED)
        self.assertTrue(error)


class ContractEdges(Harness):
    def test_scan_branch_uses_the_projects_token(self):
        """A cross-owner board: the per-issue read is blind, so the board scan runs."""
        self.patch_env(self.lane_env("1"))
        calls = []

        def run(cmd, env=None):
            calls.append(dict(env or {}))
            if cmd[:3] == ["gh", "api", "graphql"]:
                empty = {"data": {"repository": {"issue": {"projectItems": {"nodes": []}}}}}
                return subprocess.CompletedProcess(cmd, 0, json.dumps(empty), "")
            if (env or {}).get("GH_TOKEN") != PROJECTS_TOKEN:
                return subprocess.CompletedProcess(cmd, 1, "", "refused")
            items = {"items": [{"content": {"number": 7, "repository": "org/repo"}, "tier": "deep"}]}
            return subprocess.CompletedProcess(cmd, 0, json.dumps(items), "")
        run.board_base_env = None
        try:
            item_list_cache.fetch_issue_tier("org/repo", 7, "1", run=run, owner="vitalharmony")
        except item_list_cache.GhItemListError:
            pass
        scans = [c for c in calls[1:]]
        self.assertTrue(scans, "the scan branch never ran")
        self.assertEqual(scans[0].get("GH_TOKEN"), PROJECTS_TOKEN)

    def test_te_queue_runner_gets_no_env(self):
        """A runner that does not opt in is called as run(args): te_queue keeps
        its own identity (harmonic-forge#1107)."""
        self.patch_env(self.lane_env("1"))
        received = []

        def run(args):  # te_queue's shape: one argument, no board_base_env
            received.append(args)
            return subprocess.CompletedProcess(args, 0, _payload("vitalharmony"), "")
        self.assertEqual(item_list_cache.fetch_issue_tier("vitalharmony/harmonic-forge", 1, "1",
                                                          run=run, owner="vitalharmony"), "standard")
        self.assertEqual(len(received), 1)

    def test_mock_runner_auto_attribute_is_not_an_opt_in(self):
        """A test double's auto-created attribute must not opt a runner in."""
        run = mock.MagicMock(return_value=subprocess.CompletedProcess([], 0, _payload("vitalharmony"), ""))
        item_list_cache.fetch_issue_tier("vitalharmony/harmonic-forge", 1, "1", run=run, owner="vitalharmony")
        self.assertNotIn("env", run.call_args.kwargs)


if __name__ == "__main__":
    unittest.main()
