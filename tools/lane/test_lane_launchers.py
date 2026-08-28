#!/usr/bin/env python3
"""Tests for the lane launchers and the closed agent registry (harmonic-forge#322).

Every test invokes the launcher scripts DIRECTLY with a controlled environment
and a disposable fixture tree. None of them starts a real session, and none runs
from a real lane worktree -- both are hard requirements, not conveniences:

  * The handoff's Lane 3 Gate Variant note: "the artifact under test is the
    thing that launches the gate session." Lane 3 cannot verify these launchers
    by using them, because a broken `lane3` prevents its own gate from starting.
  * The handoff's Pre-Flight Preconditions: the launchers `cd` into real lane
    worktrees by design, so a naive test starts a session in `<project>-lane2`.

`systemd-inhibit` is stubbed, so the recorded argv IS the effective launch
command and nothing is ever execed. See baseline_capture.py, which owns the
fixture builder these tests reuse.

Test-case numbering below follows the handoff's own "Test Cases (for Lane 3)"
list, so a Lane 3 gate can map each of its ten cases onto a named test.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import baseline_capture as bc  # noqa: E402

LANE_DIR = Path(__file__).resolve().parent
BASELINE = LANE_DIR / "baseline_launch_tuples.json"
ADDITIONS = LANE_DIR / "lane3_safety_additions.txt"


class _FixtureTree:
    """A disposable project tree with stubbed CLIs, as a context manager."""

    def __init__(self, *, versions: dict[str, str] | None = None,
                 with_backend_env: bool = False):
        self._versions = versions
        self._with_backend_env = with_backend_env

    def __enter__(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        self.main, self.stub_bin = bc.build_fixture_tree(
            self.root, versions=self._versions)
        self.lane2 = self.root / "proj-lane2"
        self.lane3 = self.root / "proj-lane3"
        if self._with_backend_env:
            (self.main / "backend").mkdir()
            (self.main / "backend" / ".env").write_text("KEY=value\n")
        return self

    def __exit__(self, *exc):
        self._tmp.cleanup()
        return False

    def run(self, lane: str, args: list[str], **env_overrides) -> dict:
        return bc.capture_cell(LANE_DIR, self.main, self.stub_bin, lane, args,
                               env_overrides=env_overrides)

    def run_script(self, script: str, args: list[str] | None = None,
                   **env_overrides) -> subprocess.CompletedProcess:
        env = dict(os.environ)
        for key in ("LANE", "LANE_AGENT", "LANE_CLI", "LANE_PERMISSION_MODE",
                    "GH_CONFIG_DIR"):
            env.pop(key, None)
        env["PATH"] = f"{self.stub_bin}{os.pathsep}{env['PATH']}"
        env.update(env_overrides)
        return subprocess.run(
            ["bash", str(LANE_DIR / script), *(args or [])],
            cwd=self.main, env=env, capture_output=True, text=True)


def _agent_args(cell: dict) -> list[str]:
    """The agent command and its flags -- argv past systemd-inhibit's own."""
    return cell["argv"][5:]


def _why(cell: dict) -> str:
    return next(a for a in cell["argv"] if a.startswith("--why="))[len("--why="):]


def _code_only(path: Path) -> str:
    """A script's executable lines, with comments and here-doc prose removed.

    Every one of these launchers documents the mutation it no longer performs,
    quoting the original line -- which is required (the handoff's
    Read-Before-Edit instruction) and which makes a naive substring search over
    the whole file assert the opposite of what it means to.
    """
    lines = []
    in_heredoc = False
    for line in path.read_text().splitlines():
        if in_heredoc:
            if line.strip() == "EOF":
                in_heredoc = False
            continue
        if line.lstrip().startswith("#"):
            continue
        if "<<EOF" in line or "<<'EOF'" in line:
            in_heredoc = True
        lines.append(line)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# TC1 / TC9 -- all 9 lane x agent combinations
# ---------------------------------------------------------------------------
class NineCombinations(unittest.TestCase):
    """TC1: every combination resolves to the correct worktree, LANE,
    LANE_AGENT, GH_CONFIG_DIR and flag set.  TC9: and names the real agent."""

    EXPECTED_CWD = {"1": "proj", "2": "proj-lane2", "3": "proj-lane3"}
    EXPECTED_DISPLAY = {"claude": "Claude Code", "codex": "Codex",
                        "gemini": "Gemini"}
    EXPECTED_WHY_PREFIX = {"1": "Lane 1", "2": "Lane 2 (implementer)",
                           "3": "Lane 3 (gate)"}

    def test_all_nine_combinations_launch_correctly(self):
        with _FixtureTree() as tree:
            for lane in ("1", "2", "3"):
                for agent in ("claude", "codex", "gemini"):
                    with self.subTest(lane=lane, agent=agent):
                        cell = tree.run(lane, [], **{"LANE_CLI": agent})
                        self.assertTrue(cell["launched"], cell.get("stderr"))
                        self.assertEqual(
                            Path(cell["cwd"]).name, self.EXPECTED_CWD[lane])
                        self.assertEqual(cell["env"]["LANE"], lane)
                        self.assertEqual(cell["env"]["LANE_AGENT"], agent)
                        self.assertEqual(
                            cell["env"]["GH_CONFIG_DIR"],
                            str(Path.home() / ".config" / "gh-vitalharmony"))
                        # The agent's own command is always the first token
                        # after any env(1) prefix.
                        self.assertIn(agent, _agent_args(cell))

    def test_why_string_names_the_actual_agent(self):
        """TC9 -- AC7.  Every launcher said 'Claude Code' unconditionally."""
        with _FixtureTree() as tree:
            for lane in ("1", "2", "3"):
                for agent in ("claude", "codex", "gemini"):
                    with self.subTest(lane=lane, agent=agent):
                        cell = tree.run(lane, [], **{"LANE_CLI": agent})
                        self.assertEqual(
                            _why(cell),
                            f"{self.EXPECTED_WHY_PREFIX[lane]} "
                            f"{self.EXPECTED_DISPLAY[agent]} session")

    def test_agent_flag_selects_the_agent(self):
        """AC1 -- `--agent` is the canonical interface, not just LANE_CLI."""
        with _FixtureTree() as tree:
            for agent in ("claude", "codex", "gemini"):
                with self.subTest(agent=agent):
                    cell = tree.run("1", ["--agent", agent])
                    self.assertTrue(cell["launched"], cell.get("stderr"))
                    self.assertEqual(cell["env"]["LANE_AGENT"], agent)

    def test_native_args_pass_through_after_a_bare_double_dash(self):
        """AC1 -- 'native CLI args passed after `--`'."""
        with _FixtureTree() as tree:
            cell = tree.run("1", ["--agent", "codex", "--", "-p", "hello"])
            self.assertTrue(cell["launched"], cell.get("stderr"))
            self.assertEqual(_agent_args(cell), ["codex", "-p", "hello"])

    def test_double_dash_protects_a_literal_agent_argument(self):
        """The escape hatch, if an agent CLI ever grows its own --agent."""
        with _FixtureTree() as tree:
            cell = tree.run("1", ["--agent", "codex", "--", "--agent", "x"])
            self.assertTrue(cell["launched"], cell.get("stderr"))
            self.assertEqual(_agent_args(cell), ["codex", "--agent", "x"])


# ---------------------------------------------------------------------------
# TC2 / TC3 -- the closed registry rejects, and execs nothing
# ---------------------------------------------------------------------------
class ClosedRegistry(unittest.TestCase):

    def test_unknown_agent_exits_nonzero_and_execs_nothing(self):
        """TC2 -- AC3.  Verify no process was spawned, not merely that an
        error printed: the stub systemd-inhibit writes its capture file if and
        only if it ran, so an absent file is proof nothing was execed."""
        with _FixtureTree() as tree:
            cell = tree.run("1", ["--agent", "bogus"])
            self.assertFalse(cell["launched"])
            self.assertNotEqual(cell["returncode"], 0)
            self.assertIn("bogus", cell["stderr"])
            self.assertIn("Nothing was launched", cell["stderr"])

    def test_unknown_agent_is_rejected_at_every_lane(self):
        with _FixtureTree() as tree:
            for lane in ("1", "2", "3"):
                with self.subTest(lane=lane):
                    cell = tree.run(lane, ["--agent", "not-an-agent"])
                    self.assertFalse(cell["launched"])
                    self.assertNotEqual(cell["returncode"], 0)

    def test_agent_and_lane_cli_together_is_an_error(self):
        """TC3 -- AC3.  Neither wins; ADR-007 § 3 rejects a precedence rule."""
        with _FixtureTree() as tree:
            cell = tree.run("1", ["--agent", "gemini"], LANE_CLI="codex")
            self.assertFalse(cell["launched"])
            self.assertNotEqual(cell["returncode"], 0)
            self.assertIn("mutually exclusive", cell["stderr"])

    def test_empty_agent_value_is_rejected(self):
        with _FixtureTree() as tree:
            for args in (["--agent", ""], ["--agent="]):
                with self.subTest(args=args):
                    cell = tree.run("1", args)
                    self.assertFalse(cell["launched"])
                    self.assertIn("--agent requires a value", cell["stderr"])

    def test_agent_alias_resolves_by_prefix(self):
        """LANE_CLI is retained for aliases (ADR-007 § 3): claude-api is the
        claude agent running a wrapper binary, and still execs the wrapper."""
        with _FixtureTree() as tree:
            cell = tree.run("1", [], LANE_CLI="claude-api")
            self.assertTrue(cell["launched"], cell.get("stderr"))
            self.assertEqual(cell["env"]["LANE_AGENT"], "claude")
            self.assertEqual(_agent_args(cell)[0], "claude-api")

    def test_unregistered_lane_cli_is_refused(self):
        """A deliberate tightening beyond AC8's literal wording, enumerated in
        the completion report: before this change an unmatched LANE_CLI fell
        through to bare passthrough and silently received NO policy injection
        and NO version floor.  An agent-selection path that bypasses the
        registry is the 'reads as enforced but isn't' failure this issue
        removes."""
        with _FixtureTree() as tree:
            cell = tree.run("1", [], LANE_CLI="totally-unknown-cli")
            self.assertFalse(cell["launched"])
            self.assertIn("matches no registered agent", cell["stderr"])

    def test_ack_stale_is_rejected_outside_lane3(self):
        with _FixtureTree() as tree:
            for lane in ("1", "2"):
                with self.subTest(lane=lane):
                    cell = tree.run(lane, ["--ack-stale", "why"])
                    self.assertFalse(cell["launched"])
                    self.assertIn("Lane 3 option", cell["stderr"])


# ---------------------------------------------------------------------------
# NC5 -- the registry source is not fail-open
# ---------------------------------------------------------------------------
class RegistryIntegrity(unittest.TestCase):
    """A missing or half-parsed registry lets execution continue with a
    partially-defined registry, no error, even under `set -euo pipefail`.  That
    is a launcher that reads as protected and is not."""

    def _copy_lane_dir(self, dest: Path) -> Path:
        dest.mkdir()
        for name in ("lane1", "lane2", "lane3", "_lane_args.sh",
                     "_cli_launch.sh", "_agent_registry.sh",
                     "_gh_config_dir.sh"):
            (dest / name).write_text((LANE_DIR / name).read_text())
        (dest / "policies").mkdir()
        for policy in (LANE_DIR / "policies").glob("*.toml"):
            (dest / "policies" / policy.name).write_text(policy.read_text())
        return dest

    def _run_with_broken_registry(self, mutate) -> dict:
        with _FixtureTree() as tree:
            lane_dir = self._copy_lane_dir(tree.root / "lanedir")
            mutate(lane_dir / "_agent_registry.sh")
            return bc.capture_cell(lane_dir, tree.main, tree.stub_bin, "1", [],
                                   env_overrides={"LANE_CLI": "claude"})

    def test_missing_registry_refuses_to_launch(self):
        cell = self._run_with_broken_registry(lambda p: p.unlink())
        self.assertFalse(cell["launched"])
        self.assertNotEqual(cell["returncode"], 0)

    def test_truncated_registry_refuses_to_launch(self):
        def truncate(path: Path) -> None:
            # Keep only the first table, so the file parses but the registry is
            # incomplete -- the failure mode a syntax check would not catch.
            text = path.read_text()
            path.write_text(text[:text.index("declare -A AGENT_VERSION_MIN")])

        cell = self._run_with_broken_registry(truncate)
        self.assertFalse(cell["launched"])
        self.assertNotEqual(cell["returncode"], 0)

    def test_corrupt_registry_refuses_to_launch(self):
        def corrupt(path: Path) -> None:
            path.write_text(path.read_text() + "\nthis is ( not valid bash\n")

        cell = self._run_with_broken_registry(corrupt)
        self.assertFalse(cell["launched"])
        self.assertNotEqual(cell["returncode"], 0)

    def test_registry_lookup_has_no_default_fallback(self):
        """NC5: no `:-` anywhere in the registry's own lookups.  A default turns
        'the registry does not describe this agent' into 'the agent has no
        safety flags', which is the fail-open shape ADR-007 § 7 names."""
        source = _code_only(LANE_DIR / "_agent_registry.sh")
        body = source[source.index("registry_lookup()"):]
        self.assertNotIn(":-", body)

    def test_every_agent_declares_a_policy_slot_for_every_lane(self):
        """NC7: gemini:3 is declared EMPTY, not omitted, so harmonic-forge#326
        fills a declared field rather than reopening the launcher."""
        source = (LANE_DIR / "_agent_registry.sh").read_text()
        for agent in ("claude", "codex", "gemini"):
            for lane in ("1", "2", "3"):
                with self.subTest(agent=agent, lane=lane):
                    self.assertIn(f"[{agent}:{lane}]", source)


# ---------------------------------------------------------------------------
# TC4 -- LANE and LANE_AGENT reach the child and cannot be set by passthrough
# ---------------------------------------------------------------------------
class LaneEnvironment(unittest.TestCase):

    def test_lane_and_lane_agent_reach_the_child_process(self):
        with _FixtureTree() as tree:
            cell = tree.run("2", [], LANE_CLI="codex")
            self.assertEqual(cell["env"]["LANE"], "2")
            self.assertEqual(cell["env"]["LANE_AGENT"], "codex")

    def test_passthrough_args_cannot_set_lane_or_lane_agent(self):
        """AC2, stated precisely.  A launcher cannot enforce immutability --
        `readonly` does not survive `exec`.  What IS true, and what this
        asserts, is the structural property of process environments: a child
        cannot alter its parent's, so the values every hook subprocess reads
        are fixed by how the session was started."""
        with _FixtureTree() as tree:
            cell = tree.run("2", ["--", "LANE=1", "LANE_AGENT=gemini"],
                            LANE_CLI="codex")
            self.assertTrue(cell["launched"], cell.get("stderr"))
            self.assertEqual(cell["env"]["LANE"], "2")
            self.assertEqual(cell["env"]["LANE_AGENT"], "codex")
            # The tokens are forwarded to the agent verbatim, as arguments --
            # they simply are not environment assignments.
            self.assertIn("LANE=1", _agent_args(cell))

    def test_lane_env_is_set_before_the_agent_is_resolved(self):
        """A pre-existing LANE in the operator's shell must not leak through."""
        with _FixtureTree() as tree:
            cell = tree.run("3", [], LANE_CLI="codex", LANE="1")
            self.assertTrue(cell["launched"], cell.get("stderr"))
            self.assertEqual(cell["env"]["LANE"], "3")


# ---------------------------------------------------------------------------
# TC5 -- safety flags cannot be removed or contradicted via passthrough
# ---------------------------------------------------------------------------
class SafetyFlagsUnremovable(unittest.TestCase):
    """AC4.  The deny list is DERIVED from the same declaration that injects
    the flag, so the two cannot drift -- that derivation is what is tested
    here, not a hand-maintained second list."""

    def test_declared_policy_flag_cannot_be_supplied_by_passthrough(self):
        with _FixtureTree() as tree:
            for lane in ("1", "2"):  # the lanes gemini declares a policy for
                with self.subTest(lane=lane):
                    cell = tree.run(lane, ["--admin-policy", "/dev/null"],
                                    LANE_CLI="gemini")
                    self.assertFalse(cell["launched"])
                    self.assertIn("cannot be set, removed, or contradicted",
                                  cell["stderr"])

    def test_declared_policy_flag_cannot_be_contradicted_in_equals_form(self):
        with _FixtureTree() as tree:
            cell = tree.run("1", ["--admin-policy=/dev/null"],
                            LANE_CLI="gemini")
            self.assertFalse(cell["launched"])
            self.assertNotEqual(cell["returncode"], 0)

    def test_declared_policy_flag_is_denied_even_after_a_double_dash(self):
        """`--` forwards args verbatim, so it must NOT be a bypass for AC4 --
        the deny scan runs over the passthrough array, not over the raw
        command line."""
        with _FixtureTree() as tree:
            cell = tree.run("2", ["--", "--admin-policy", "/dev/null"],
                            LANE_CLI="gemini")
            self.assertFalse(cell["launched"])
            self.assertNotEqual(cell["returncode"], 0)

    def test_policy_flag_is_actually_injected_where_declared(self):
        with _FixtureTree() as tree:
            for lane, expected in (("1", "gemini-lane1.toml"),
                                   ("2", "gemini-lane2.toml")):
                with self.subTest(lane=lane):
                    cell = tree.run(lane, [], LANE_CLI="gemini")
                    args = _agent_args(cell)
                    self.assertIn("--admin-policy", args)
                    self.assertTrue(
                        args[args.index("--admin-policy") + 1].endswith(expected))

    def test_lane3_declares_no_safety_flag_for_any_agent_today(self):
        """AC4 is VACUOUS AT LANE 3 today, and saying so is the point.

        Codex's `--sandbox read-only` was dropped (Lane 1 decision 1): verified
        live against codex-cli 0.150.1, at least five further passthrough paths
        defeat or escalate past it, so a denylist cannot hold the property AC4
        asserts.  Claude gets no new Lane 3 flag (decision 3).  Gemini's Lane 3
        policy is harmonic-forge#326's and is not built.

        This test exists so the vacuity is recorded rather than mistaken for
        enforcement -- and so it FAILS the day a flag is added without amending
        lane3_safety_additions.txt."""
        additions = bc._load_lane3_additions(ADDITIONS)
        self.assertEqual(additions, [])
        with _FixtureTree() as tree:
            for agent in ("claude", "codex", "gemini"):
                with self.subTest(agent=agent):
                    cell = tree.run("3", [], LANE_CLI=agent)
                    self.assertTrue(cell["launched"], cell.get("stderr"))
                    self.assertNotIn("--admin-policy", _agent_args(cell))
                    self.assertNotIn("--sandbox", _agent_args(cell))

    def test_deny_mechanism_is_registry_generic_not_gemini_specific(self):
        """NC7.  Fill the declared-empty gemini:3 slot and the deny follows,
        with no launcher change -- which is exactly what harmonic-forge#326
        needs to be true."""
        with _FixtureTree() as tree:
            lane_dir = tree.root / "lanedir"
            lane_dir.mkdir()
            for name in ("lane1", "lane2", "lane3", "_lane_args.sh",
                         "_cli_launch.sh", "_gh_config_dir.sh"):
                (lane_dir / name).write_text((LANE_DIR / name).read_text())
            registry = (LANE_DIR / "_agent_registry.sh").read_text().replace(
                '  [gemini:3]=""', '  [gemini:3]="gemini-lane3.toml"')
            (lane_dir / "_agent_registry.sh").write_text(registry)
            (lane_dir / "policies").mkdir()
            for policy in (LANE_DIR / "policies").glob("*.toml"):
                (lane_dir / "policies" / policy.name).write_text(
                    policy.read_text())
            (lane_dir / "policies" / "gemini-lane3.toml").write_text(
                '[[rules]]\nname = "placeholder"\n')

            injected = bc.capture_cell(lane_dir, tree.main, tree.stub_bin, "3",
                                       [], env_overrides={"LANE_CLI": "gemini"})
            self.assertTrue(injected["launched"], injected.get("stderr"))
            self.assertIn("--admin-policy", _agent_args(injected))

            denied = bc.capture_cell(lane_dir, tree.main, tree.stub_bin, "3",
                                     ["--admin-policy", "/dev/null"],
                                     env_overrides={"LANE_CLI": "gemini"})
            self.assertFalse(denied["launched"])
            self.assertIn("cannot be set, removed, or contradicted",
                          denied["stderr"])

    def test_missing_policy_file_refuses_to_launch(self):
        """NC8 -- harmonic-forge#362's fail-closed guard, carried forward as a
        registry-declared precondition.  The Gemini CLI itself does NOT fail
        closed: verified live 2026-08-28, a nonexistent --admin-policy path
        prints only a stderr warning and the session starts completely
        unprotected under --yolo."""
        with _FixtureTree() as tree:
            lane_dir = tree.root / "lanedir"
            lane_dir.mkdir()
            for name in ("lane1", "lane2", "lane3", "_lane_args.sh",
                         "_cli_launch.sh", "_agent_registry.sh",
                         "_gh_config_dir.sh"):
                (lane_dir / name).write_text((LANE_DIR / name).read_text())
            (lane_dir / "policies").mkdir()  # deliberately empty
            cell = bc.capture_cell(lane_dir, tree.main, tree.stub_bin, "1", [],
                                   env_overrides={"LANE_CLI": "gemini"})
            self.assertFalse(cell["launched"])
            self.assertIn("policy file missing", cell["stderr"])

    def test_invalid_policy_toml_refuses_to_launch(self):
        with _FixtureTree() as tree:
            lane_dir = tree.root / "lanedir"
            lane_dir.mkdir()
            for name in ("lane1", "lane2", "lane3", "_lane_args.sh",
                         "_cli_launch.sh", "_agent_registry.sh",
                         "_gh_config_dir.sh"):
                (lane_dir / name).write_text((LANE_DIR / name).read_text())
            (lane_dir / "policies").mkdir()
            (lane_dir / "policies" / "gemini-lane1.toml").write_text(
                "this is [ not valid toml\n")
            cell = bc.capture_cell(lane_dir, tree.main, tree.stub_bin, "1", [],
                                   env_overrides={"LANE_CLI": "gemini"})
            self.assertFalse(cell["launched"])
            self.assertIn("not valid TOML", cell["stderr"])


# ---------------------------------------------------------------------------
# TC6 / TC7 -- lane3 is check-only
# ---------------------------------------------------------------------------
class Lane3IsCheckOnly(unittest.TestCase):
    """AC5 and AC6.  Both harmonic-forge#255's staleness protection and #264's
    env-sync guarantee survive -- as checks.  Neither was dropped, and lane3
    performs zero mutations either way."""

    @staticmethod
    def _advance_origin(tree: _FixtureTree) -> str:
        """Move origin/main ahead of the lane3 worktree, from INSIDE the
        fixture repo.  The remote tip therefore exists in the local object
        store, which exercises the ancestry stage of the check."""
        (tree.main / "NEW.md").write_text("advanced\n")
        subprocess.run(["git", "add", "NEW.md"], cwd=tree.main, check=True,
                       capture_output=True)
        subprocess.run(["git", "commit", "-q", "-m", "advance"], cwd=tree.main,
                       check=True, capture_output=True)
        subprocess.run(["git", "push", "-q", "origin", "main"], cwd=tree.main,
                       check=True, capture_output=True)
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=tree.main,
                              check=True, capture_output=True,
                              text=True).stdout.strip()

    @staticmethod
    def _advance_origin_from_elsewhere(tree: _FixtureTree) -> str:
        """Move origin/main ahead from a SEPARATE clone, so the new tip is not
        in the fixture repo's object store at all.

        This is the condition the old code could never observe, because it
        fetched the object before comparing -- and it is the one DJC3's stage 1
        exists for: `git cat-file -e` on a tip we have never fetched is
        unambiguous staleness, with no ancestry math available.
        """
        origin = tree.root / "github.com" / "vitalharmony" / "lane-fixture.git"
        other = tree.root / "other-clone"
        subprocess.run(["git", "clone", "-q", str(origin), str(other)],
                       check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "o@example.invalid"],
                       cwd=other, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Other"], cwd=other,
                       check=True, capture_output=True)
        (other / "ELSEWHERE.md").write_text("from another clone\n")
        subprocess.run(["git", "add", "ELSEWHERE.md"], cwd=other, check=True,
                       capture_output=True)
        subprocess.run(["git", "commit", "-q", "-m", "elsewhere"], cwd=other,
                       check=True, capture_output=True)
        subprocess.run(["git", "push", "-q", "origin", "main"], cwd=other,
                       check=True, capture_output=True)
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=other,
                              check=True, capture_output=True,
                              text=True).stdout.strip()

    def test_lane3_refuses_on_staleness_and_mutates_nothing(self):
        """TC6 + TC7.  Assert no fetch occurred, not merely that a message
        printed: the remote-tracking ref must be byte-identical afterwards."""
        with _FixtureTree() as tree:
            self._advance_origin(tree)
            before = subprocess.run(
                ["git", "rev-parse", "origin/main"], cwd=tree.lane3,
                capture_output=True, text=True).stdout.strip()

            cell = tree.run("3", [], LANE_CLI="claude")
            self.assertFalse(cell["launched"])
            self.assertIn("REFUSING TO START", cell["stderr"])
            self.assertIn("commit(s) behind origin/main", cell["stderr"])
            self.assertIn("lane3-provision", cell["stderr"])

            after = subprocess.run(
                ["git", "rev-parse", "origin/main"], cwd=tree.lane3,
                capture_output=True, text=True).stdout.strip()
            self.assertEqual(before, after,
                             "lane3 fetched -- AC5 requires zero mutations")

    def test_lane3_catches_a_tip_it_has_never_fetched(self):
        """DJC3 stage 1.  Strictly MORE staleness is caught than before, with
        zero mutations: the old check fetched the object first, so it could
        never observe this state."""
        with _FixtureTree() as tree:
            remote_sha = self._advance_origin_from_elsewhere(tree)
            self.assertNotEqual(
                subprocess.run(["git", "cat-file", "-e", remote_sha],
                               cwd=tree.lane3, capture_output=True).returncode,
                0, "fixture invalid: the tip is already in the object store")

            cell = tree.run("3", [], LANE_CLI="claude")
            self.assertFalse(cell["launched"])
            self.assertIn("never fetched", cell["stderr"])
            self.assertIn(remote_sha, cell["stderr"])

            # Still no fetch: the object must remain absent afterwards.
            self.assertNotEqual(
                subprocess.run(["git", "cat-file", "-e", remote_sha],
                               cwd=tree.lane3, capture_output=True).returncode,
                0, "lane3 fetched -- AC5 requires zero mutations")

    def test_lane3_escape_hatch_starts_the_session(self):
        """TC7.  The original code's reason for warning-only was that a session
        legitimately gating an older target branch is not behind by mistake.
        That case is preserved -- the operator states it, in writing, once."""
        with _FixtureTree() as tree:
            self._advance_origin(tree)
            cell = tree.run("3", ["--ack-stale", "gating PR #123's branch"],
                            LANE_CLI="claude")
            self.assertTrue(cell["launched"], cell.get("stderr"))
            self.assertIn("staleness acknowledged", cell["stderr"])

    def test_lane3_escape_hatch_rejects_an_empty_reason(self):
        """Following HRSE2/scripts/l1_post.py:810's --ack-overlap precedent and
        its test_empty_ack_overlap_reason_is_rejected_at_the_cli: an escape
        hatch that accepts an empty justification is not an escape hatch."""
        with _FixtureTree() as tree:
            self._advance_origin(tree)
            for args in (["--ack-stale", ""], ["--ack-stale="]):
                with self.subTest(args=args):
                    cell = tree.run("3", args, LANE_CLI="claude")
                    self.assertFalse(cell["launched"])
                    self.assertIn("non-empty reason", cell["stderr"])

    def test_lane3_starts_when_not_stale(self):
        with _FixtureTree() as tree:
            cell = tree.run("3", [], LANE_CLI="claude")
            self.assertTrue(cell["launched"], cell.get("stderr"))

    def test_lane3_refuses_when_the_remote_cannot_be_reached(self):
        """NC3.  'Cannot determine' is treated as drift, not as its absence --
        an undetermined staleness state is exactly the state the original
        incident occurred in."""
        with _FixtureTree() as tree:
            subprocess.run(["git", "remote", "set-url", "origin",
                            str(tree.root / "does-not-exist.git")],
                           cwd=tree.main, check=True, capture_output=True)
            cell = tree.run("3", [], LANE_CLI="claude")
            self.assertFalse(cell["launched"])
            self.assertIn("cannot determine", cell["stderr"])

    def test_lane3_uses_the_fully_qualified_ref(self):
        """NC4.  `git ls-remote origin main` returns two lines on a real repo
        (refs/heads/main and refs/remotes/origin/main); the qualified form
        returns exactly one."""
        source = _code_only(LANE_DIR / "lane3")
        self.assertIn("ls-remote origin refs/heads/main", source)
        self.assertNotIn("ls-remote origin main", source)

    def test_lane3_never_reads_the_stale_remote_tracking_ref(self):
        """NC2.  `git rev-parse origin/main` reads the stale local ref, which
        is precisely what this redesign exists to stop trusting.  Every
        comparison must use the SHA ls-remote returned."""
        source = _code_only(LANE_DIR / "lane3")
        self.assertNotIn("rev-parse origin/main", source)

    def test_lane3_performs_no_mutating_git_or_filesystem_operation(self):
        """AC5, asserted against the source as well as against behavior: the
        behavioral test above can only catch the mutations it thought to look
        for."""
        source = _code_only(LANE_DIR / "lane3")
        for mutation in ("git fetch", "ln -sf", "git checkout", "git pull",
                         "git reset"):
            with self.subTest(mutation=mutation):
                self.assertNotIn(mutation, source)

    def test_lane3_refuses_on_env_drift_with_no_escape_hatch(self):
        """AC6 -- harmonic-forge#264's protection, preserved as a check.  The
        asymmetry with staleness is deliberate: backend/.env has no legitimate
        per-worktree divergence at all, so there is nothing to acknowledge."""
        with _FixtureTree(with_backend_env=True) as tree:
            (tree.lane3 / "backend").mkdir()
            (tree.lane3 / "backend" / ".env").write_text("KEY=stale\n")

            cell = tree.run("3", [], LANE_CLI="claude")
            self.assertFalse(cell["launched"])
            self.assertIn("backend/.env is not linked", cell["stderr"])
            self.assertIn("no acknowledgement flag", cell["stderr"])

            # TC6: the drifted file is REPORTED, not repaired.
            self.assertFalse((tree.lane3 / "backend" / ".env").is_symlink())
            self.assertEqual(
                (tree.lane3 / "backend" / ".env").read_text(), "KEY=stale\n")

    def test_ack_stale_does_not_bypass_the_env_check(self):
        with _FixtureTree(with_backend_env=True) as tree:
            (tree.lane3 / "backend").mkdir()
            (tree.lane3 / "backend" / ".env").write_text("KEY=stale\n")
            cell = tree.run("3", ["--ack-stale", "deliberate"],
                            LANE_CLI="claude")
            self.assertFalse(cell["launched"])
            self.assertIn("backend/.env is not linked", cell["stderr"])

    def test_lane3_starts_when_the_env_symlink_is_correct(self):
        with _FixtureTree(with_backend_env=True) as tree:
            (tree.lane3 / "backend").mkdir()
            (tree.lane3 / "backend" / ".env").symlink_to(
                tree.main / "backend" / ".env")
            cell = tree.run("3", [], LANE_CLI="claude")
            self.assertTrue(cell["launched"], cell.get("stderr"))


class Lane3Provision(unittest.TestCase):
    """The two mutations lane3 gave up, and nothing else."""

    def test_provision_repairs_staleness_and_the_env_symlink(self):
        with _FixtureTree(with_backend_env=True) as tree:
            (tree.lane3 / "backend").mkdir()
            (tree.lane3 / "backend" / ".env").write_text("KEY=stale\n")
            Lane3IsCheckOnly._advance_origin(tree)

            proc = tree.run_script("lane3-provision")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertTrue((tree.lane3 / "backend" / ".env").is_symlink())

            # And lane3 now starts, which is the whole contract between them.
            cell = tree.run("3", [], LANE_CLI="claude")
            self.assertTrue(cell["launched"], cell.get("stderr"))

    def test_provision_refuses_to_clobber_uncommitted_work(self):
        with _FixtureTree() as tree:
            Lane3IsCheckOnly._advance_origin(tree)
            (tree.lane3 / "README.md").write_text("locally modified\n")
            proc = tree.run_script("lane3-provision")
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("uncommitted changes", proc.stderr)

    def test_provision_is_not_a_flag_on_lane3(self):
        """DJC2.  A flag means the same script both mutates and does not,
        decided by an argument -- which reintroduces the hazard AC5 removes the
        moment the flag reaches an alias or muscle memory."""
        self.assertNotIn("--provision", _code_only(LANE_DIR / "lane3"))


# ---------------------------------------------------------------------------
# TC8 -- the AC8 regression baseline
# ---------------------------------------------------------------------------
class RegressionBaseline(unittest.TestCase):

    def test_claude_and_codex_tuples_match_the_committed_baseline(self):
        """TC8.  The fixture was captured from origin/main BEFORE any registry
        work; this is what makes AC8 falsifiable by Lane 3 rather than a claim
        in a completion report."""
        captured = bc.capture_all(LANE_DIR)
        fixture = json.loads(BASELINE.read_text())
        diffs = bc.compare(captured, fixture,
                           bc._load_lane3_additions(ADDITIONS))
        self.assertEqual(diffs, [], "\n".join(diffs))

    def test_the_baseline_covers_every_cell(self):
        fixture = json.loads(BASELINE.read_text())
        self.assertEqual(len(fixture["cells"]),
                         len(bc.LANES) * len(bc.AGENTS) * len(bc.ARG_SHAPES))


# ---------------------------------------------------------------------------
# TC10 -- AC9 version floors
# ---------------------------------------------------------------------------
class VersionFloors(unittest.TestCase):

    def test_installed_versions_are_accepted(self):
        with _FixtureTree() as tree:
            for agent in ("claude", "codex", "gemini"):
                with self.subTest(agent=agent):
                    cell = tree.run("1", [], LANE_CLI=agent)
                    self.assertTrue(cell["launched"], cell.get("stderr"))

    def test_a_version_below_the_floor_is_rejected(self):
        below = {"claude": "2.0.999 (Claude Code)",
                 "codex": "codex-cli 0.149.0",
                 "gemini": "0.55.9"}
        with _FixtureTree(versions=below) as tree:
            for agent in ("claude", "codex", "gemini"):
                with self.subTest(agent=agent):
                    cell = tree.run("1", [], LANE_CLI=agent)
                    self.assertFalse(cell["launched"])
                    self.assertIn("below the supported minimum", cell["stderr"])

    def test_a_newer_version_is_accepted(self):
        """The floor is MINOR, not patch, precisely so a routine CLI upgrade
        does not become a false alarm -- two of the three CLIs moved between
        this issue's handoff and its implementation."""
        newer = {"claude": "9.9.9 (Claude Code)", "codex": "codex-cli 9.9.9",
                 "gemini": "9.9.9"}
        with _FixtureTree(versions=newer) as tree:
            for agent in ("claude", "codex", "gemini"):
                with self.subTest(agent=agent):
                    cell = tree.run("1", [], LANE_CLI=agent)
                    self.assertTrue(cell["launched"], cell.get("stderr"))

    def test_an_unparseable_version_refuses_to_launch(self):
        with _FixtureTree(versions={"codex": "no version here"}) as tree:
            cell = tree.run("1", [], LANE_CLI="codex")
            self.assertFalse(cell["launched"])
            self.assertIn("could not parse a version", cell["stderr"])

    def test_the_qualified_patch_versions_are_recorded(self):
        """AC9: 'with the supported range recorded'.  The floor catches a
        genuinely too-old CLI; the qualified patch version keeps the parity
        suite's claim (harmonic-forge#325) precise."""
        source = (LANE_DIR / "_agent_registry.sh").read_text()
        for version in ("2.1.250", "0.150.1", "0.56.0"):
            with self.subTest(version=version):
                self.assertIn(version, source)


if __name__ == "__main__":
    unittest.main()
