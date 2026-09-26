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
import shutil
import signal
import subprocess
import sys
import tempfile
import time
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
        self.home = self.root / "home"
        extension_record = self.home / ".gemini" / "extensions" / "lane3-context" / ".gemini-extension-install.json"
        extension_record.parent.mkdir(parents=True)
        extension_record.write_text(json.dumps({"source": str(LANE_DIR.parent / "gemini" / "lane3-context"), "type": "link"}))
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
        env_overrides.setdefault("HOME", str(self.home))
        return bc.capture_cell(LANE_DIR, self.main, self.stub_bin, lane, args,
                               env_overrides=env_overrides)

    def run_script(self, script: str, args: list[str] | None = None,
                   **env_overrides) -> subprocess.CompletedProcess:
        env = dict(os.environ)
        for key in ("LANE", "LANE_AGENT", "LANE_CLI", "LANE_PERMISSION_MODE",
                    "LANE_DEFAULT_MODEL", "LANE_DEFAULT_EFFORT",
                    "CLAUDE_CODE_EFFORT_LEVEL", "ANTHROPIC_MODEL",
                    "ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL",
                    "ANTHROPIC_DEFAULT_FABLE_MODEL", "ANTHROPIC_DEFAULT_HAIKU_MODEL",
                    "GH_CONFIG_DIR"):
            env.pop(key, None)
        env["PATH"] = f"{self.stub_bin}{os.pathsep}{env['PATH']}"
        env["HOME"] = str(self.home)
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
                            str(tree.home / ".config" / "gh-vitalharmony"))
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
            # harmonic-forge#756: lane 1's private TMPDIR grant + sandbox keys.
            self.assertEqual(_agent_args(cell),
                             ["codex", "--add-dir",
                              f"{tree.home}/.cache/codex-lane-tmp/lane1",
                              "--no-daemon", *CodexLaneTmp.KEYS, "-p", "hello"])

    def test_double_dash_protects_a_literal_agent_argument(self):
        """The escape hatch, if an agent CLI ever grows its own --agent."""
        with _FixtureTree() as tree:
            cell = tree.run("1", ["--agent", "codex", "--", "--agent", "x"])
            self.assertTrue(cell["launched"], cell.get("stderr"))
            # harmonic-forge#756: lane 1's private TMPDIR grant + sandbox keys.
            self.assertEqual(_agent_args(cell),
                             ["codex", "--add-dir",
                              f"{tree.home}/.cache/codex-lane-tmp/lane1",
                              "--no-daemon", *CodexLaneTmp.KEYS, "--agent", "x"])


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
                     "_cli_launch.sh", "_lane_cleanup.sh", "_agent_registry.sh",
                     "_gh_config_dir.sh", "_lane_refresh.sh"):
            (dest / name).write_text((LANE_DIR / name).read_text())
        (dest / "policies").mkdir()
        for policy in (LANE_DIR / "policies").glob("*.toml"):
            (dest / "policies" / policy.name).write_text(policy.read_text())
        copied_extension = dest.parent / "gemini" / "lane3-context"
        copied_extension.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(LANE_DIR.parent / "gemini" / "lane3-context", copied_extension)
        return dest

    def _run_with_broken_registry(self, mutate) -> dict:
        with _FixtureTree() as tree:
            lane_dir = self._copy_lane_dir(tree.root / "lanedir")
            extension_record = tree.home / ".gemini" / "extensions" / "lane3-context" / ".gemini-extension-install.json"
            extension_record.write_text(json.dumps({"source": str(lane_dir.parent / "gemini" / "lane3-context"), "type": "link"}))
            mutate(lane_dir / "_agent_registry.sh")
            return bc.capture_cell(lane_dir, tree.main, tree.stub_bin, "1", [],
                                   env_overrides={"LANE_CLI": "claude", "HOME": str(tree.home)})

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

    NEW_665_ATTRS = ("AGENT_MODEL_FLAG", "AGENT_MODEL_FLAG_ENV",
                     "AGENT_MODEL_FLAG_VALUE", "AGENT_EFFORT_FLAG",
                     "AGENT_EFFORT_FLAG_ENV", "AGENT_EFFORT_FLAG_VALUE",
                     "AGENT_EFFORT_LEVELS", "AGENT_LAUNCH_REFUSED_ENV")

    def test_every_665_attribute_is_required(self):
        """harmonic-forge#665 TC8: dropping any new attribute's claude entry
        refuses the launch -- even with ANTHROPIC_MODEL set, the case where a
        swallowed lookup would let a refused launch through."""
        import re
        for attr in self.NEW_665_ATTRS:
            with self.subTest(attr=attr):
                def drop(path: Path, attr=attr) -> None:
                    text = path.read_text()
                    block = re.search(rf"declare -A {attr}=\(\n(.*?)\n\)", text, re.S)
                    self.assertIsNotNone(block, attr)
                    body = re.sub(r"^  \[claude\]=.*\n?", "", block.group(1), count=1, flags=re.M)
                    self.assertNotEqual(body, block.group(1), attr)
                    path.write_text(text[:block.start(1)] + body + text[block.end(1):])

                cell = self._run_with_broken_registry(drop)
                self.assertFalse(cell["launched"], cell.get("stderr"))
                self.assertNotEqual(cell["returncode"], 0)

    def test_refused_env_lookup_failure_is_not_swallowed(self):
        """Even if AGENT_LAUNCH_REFUSED_ENV left the required list, a missing
        entry must still stop the launch rather than skip the refusal loop."""
        def drop_from_required_and_entry(path: Path) -> None:
            text = path.read_text()
            text = text.replace(" AGENT_LAUNCH_REFUSED_ENV\n)", "\n)", 1)
            import re
            text = re.sub(r"(declare -A AGENT_LAUNCH_REFUSED_ENV=\(\n)  \[claude\]=.*\n", r"\1", text, count=1)
            path.write_text(text)

        with _FixtureTree() as tree:
            lane_dir = self._copy_lane_dir(tree.root / "lanedir")
            extension_record = tree.home / ".gemini" / "extensions" / "lane3-context" / ".gemini-extension-install.json"
            extension_record.write_text(json.dumps({"source": str(lane_dir.parent / "gemini" / "lane3-context"), "type": "link"}))
            registry = lane_dir / "_agent_registry.sh"
            drop_from_required_and_entry(registry)
            self.assertNotIn("AGENT_LAUNCH_REFUSED_ENV\n)", registry.read_text())
            cell = bc.capture_cell(lane_dir, tree.main, tree.stub_bin, "1", [],
                                   env_overrides={"LANE_CLI": "claude",
                                                  "HOME": str(tree.home),
                                                  "ANTHROPIC_MODEL": "opus"})
        self.assertFalse(cell["launched"], cell.get("stderr"))
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

    def test_gemini_lane3_refuses_missing_redirected_or_dangling_context_extension(self):
        """H1414: a fresh gate may start only with the canonical extension."""
        with _FixtureTree() as tree:
            record = tree.home / ".gemini" / "extensions" / "lane3-context" / ".gemini-extension-install.json"
            for state in ("missing", "redirected", "dangling"):
                with self.subTest(state=state):
                    if record.exists():
                        record.unlink()
                    if state == "redirected":
                        foreign = tree.root / "foreign-extension"
                        foreign.mkdir(exist_ok=True)
                        (foreign / "gemini-extension.json").write_text("{}")
                        (foreign / "lane3_context_mcp.py").write_text("")
                        record.write_text(json.dumps({"source": str(foreign), "type": "link"}))
                    elif state == "dangling":
                        record.write_text(json.dumps({"source": str(tree.root / "missing-extension"), "type": "link"}))

                    cell = tree.run("3", [], LANE_CLI="gemini")
                    self.assertFalse(cell["launched"], cell.get("stderr"))
                    self.assertIn("REFUSING TO START", cell["stderr"])

                    record.write_text(json.dumps({"source": str(LANE_DIR.parent / "gemini" / "lane3-context"), "type": "link"}))

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

    def test_lane3_launch_flags_match_the_committed_closed_list(self):
        """The amendment gate (harmonic-forge#322 required change 9).

        Originally `test_lane3_declares_no_safety_flag_for_any_agent_today`,
        asserting the list was EMPTY -- AC4 was vacuous at Lane 3 because Codex's
        `--sandbox read-only` had been dropped (five live-verified escalation
        paths on codex-cli 0.150.1) and Claude got no new flag.

        harmonic-forge#326 is amendment 1: it filled the declared-empty
        `AGENT_LANE_POLICY[gemini:3]` slot, and THIS TEST FAILED until
        `--admin-policy` was added to lane3_safety_additions.txt -- which is
        exactly the gate working. So the assertion generalizes rather than
        relaxes: whatever a Lane 3 launch adds beyond the pre-#322 baseline must
        appear on the committed list, and nothing else may.

        Note on why this test carries the gate rather than the AC8 comparator:
        `baseline_capture.compare()` only inspects Claude and Codex cells
        (`AC8_AGENTS`), so a Gemini Lane 3 change passes `--compare` untouched.
        For Gemini -- the only agent the list currently applies to -- this test
        IS the enforcement. Verified: filling the registry slot failed here and
        passed `--compare`.
        """
        additions = set(bc._load_lane3_additions(ADDITIONS))
        baseline = json.loads(BASELINE.read_text())["cells"]
        with _FixtureTree() as tree:
            for agent in ("claude", "codex", "gemini"):
                with self.subTest(agent=agent):
                    cell = tree.run("3", [], LANE_CLI=agent)
                    self.assertTrue(cell["launched"], cell.get("stderr"))
                    # Compare against the baseline WITH AC2/AC7's declared
                    # deltas applied, so the agent-aware `--why` string is not
                    # mistaken for an undeclared Lane 3 safety addition.
                    expected = bc._apply_declared_deltas(
                        baseline[f"lane3/{agent}/none"], agent)
                    was = set(expected["argv"])
                    now = set(cell["argv"])
                    undeclared = {a for a in now - was if a.startswith("-")}
                    self.assertLessEqual(
                        undeclared, additions,
                        f"{agent} Lane 3 gained {sorted(undeclared - additions)} "
                        f"which is not on lane3_safety_additions.txt -- amend "
                        f"that list in its own issue, or drop the flag")

    def test_gemini_lane3_policy_is_injected_and_unremovable(self):
        """harmonic-forge#326 AC4, the property #326's tier depends on.

        The launcher supplies the Lane 3 policy, and a passthrough
        `--admin-policy` is rejected outright rather than winning by
        last-flag-wins. Both halves asserted -- injection alone would be a
        policy that is advisory, not a boundary (ADR-007 § 9)."""
        with _FixtureTree() as tree:
            cell = tree.run("3", [], LANE_CLI="gemini")
            self.assertTrue(cell["launched"], cell.get("stderr"))
            args = _agent_args(cell)
            self.assertIn("--admin-policy", args)
            self.assertTrue(
                args[args.index("--admin-policy") + 1].endswith("gemini-lane3.toml"))

            denied = tree.run("3", ["--admin-policy", "/dev/null"],
                              LANE_CLI="gemini")
            self.assertFalse(denied["launched"])
            self.assertIn("cannot be set, removed, or contradicted",
                          denied["stderr"])

    def test_claude_lane3_remains_flagless(self):
        """AC4 stays vacuous for Claude, and that is recorded rather than
        mistaken for enforcement. Claude has no launcher safety flag at
        Lane 3 at all (unchanged by harmonic-forge#644, which only touches
        Codex)."""
        with _FixtureTree() as tree:
            args = _agent_args(tree.run("3", [], LANE_CLI="claude"))
            self.assertNotIn("--admin-policy", args)
            self.assertNotIn("--sandbox", args)

    def test_codex_lane3_gains_sandbox_and_add_dir(self):
        """harmonic-forge#644: unlike Claude above, Codex Lane 3 DOES carry a
        launcher-supplied safety grant now -- the OS sandbox plus a
        forge-owned write-guard hook (`lane3_codex_write_guard.py`), not a
        `.codex/hooks.json`-only enforcement path as the prior test assumed."""
        with _FixtureTree() as tree:
            args = _agent_args(tree.run("3", [], LANE_CLI="codex"))
            self.assertIn("--sandbox", args)
            self.assertEqual(args[args.index("--sandbox") + 1], "workspace-write")
            self.assertIn("--add-dir", args)
            self.assertTrue(
                args[args.index("--add-dir") + 1].endswith("Harmonic_Projects/testplan"))

    def test_codex_lane3_sandbox_and_add_dir_reach_resume_last(self):
        """NC2 (Lane 1's check of Plan F644): the grant is injected globally,
        before the subcommand, so it reaches a `resume --last` launch too --
        not only the bare form above. `resume`/`--last` are passthrough
        (step 5), injected after the launcher's own flags (step 4b), so the
        result is `codex --sandbox workspace-write --add-dir <testplan>
        --no-daemon resume --last` (harmonic-forge#754 added the step-4c
        `--no-daemon`), never the injection moved after the subcommand.
        harmonic-forge#756 added two more `--add-dir`s and the two `-c`
        sandbox keys, all still before the subcommand."""
        with _FixtureTree() as tree:
            cell = tree.run("3", ["resume", "--last"], LANE_CLI="codex")
            self.assertTrue(cell["launched"], cell.get("stderr"))
            args = _agent_args(cell)
            home = str(tree.home)
            self.assertEqual(args, [
                "codex", "--sandbox", "workspace-write",
                "--add-dir", f"{home}/Harmonic_Projects/testplan",
                "--add-dir", f"{home}/.cache/codex-lane-tmp/lane3",
                "--add-dir", f"{home}/.cache/cymagraph",
                "--no-daemon",
                "-c", "sandbox_workspace_write.exclude_slash_tmp=true",
                "-c", "sandbox_workspace_write.exclude_tmpdir_env_var=true",
                "resume", "--last"])

    def test_codex_lane3_caller_sandbox_denied(self):
        """A caller-supplied `--sandbox` at codex:3 is refused outright and
        nothing launches (harmonic-forge#644 NC1/AC4) -- unlike `--add-dir`
        below, `--sandbox` cannot be silently overridden without risking a
        widen to `danger-full-access`."""
        with _FixtureTree() as tree:
            denied = tree.run("3", ["--sandbox", "danger-full-access"], LANE_CLI="codex")
            self.assertFalse(denied["launched"])
            self.assertIn("cannot be set, removed, or contradicted", denied["stderr"])

    def test_codex_lane3_caller_add_dir_coexists(self):
        """A caller-supplied `--add-dir` at codex:3 still launches, with BOTH
        the launcher's own testplan grant and the caller's own directory
        present -- `--add-dir` is repeatable, verified live, so it is
        injected unconditionally rather than denied (harmonic-forge#644)."""
        with _FixtureTree() as tree:
            cell = tree.run("3", ["--add-dir", "/tmp"], LANE_CLI="codex")
            self.assertTrue(cell["launched"], cell.get("stderr"))
            args = _agent_args(cell)
            add_dir_indices = [i for i, a in enumerate(args) if a == "--add-dir"]
            # testplan + lane3 TMPDIR + cymagraph (harmonic-forge#756) + caller's
            self.assertEqual(len(add_dir_indices), 4)
            values = {args[i + 1] for i in add_dir_indices}
            self.assertIn("/tmp", values)
            self.assertTrue(any(v.endswith("Harmonic_Projects/testplan") for v in values))

    def test_deny_mechanism_is_registry_generic_not_gemini_specific(self):
        """NC7.  Fill the declared-empty gemini:3 slot and the deny follows,
        with no launcher change -- which is exactly what harmonic-forge#326
        needs to be true."""
        with _FixtureTree() as tree:
            lane_dir = tree.root / "lanedir"
            lane_dir.mkdir()
            for name in ("lane1", "lane2", "lane3", "_lane_args.sh",
                         "_cli_launch.sh", "_lane_cleanup.sh", "_gh_config_dir.sh", "_lane_refresh.sh"):
                (lane_dir / name).write_text((LANE_DIR / name).read_text())
            registry = (LANE_DIR / "_agent_registry.sh").read_text().replace(
                '  [gemini:3]=""', '  [gemini:3]="gemini-lane3.toml"')
            (lane_dir / "_agent_registry.sh").write_text(registry)
            (lane_dir / "policies").mkdir()
            for policy in (LANE_DIR / "policies").glob("*.toml"):
                (lane_dir / "policies" / policy.name).write_text(
                    policy.read_text())
            copied_extension = lane_dir.parent / "gemini" / "lane3-context"
            copied_extension.parent.mkdir(parents=True)
            shutil.copytree(LANE_DIR.parent / "gemini" / "lane3-context", copied_extension)
            extension_record = tree.home / ".gemini" / "extensions" / "lane3-context" / ".gemini-extension-install.json"
            extension_record.write_text(json.dumps({"source": str(copied_extension), "type": "link"}))
            (lane_dir / "policies" / "gemini-lane3.toml").write_text(
                '[[rules]]\nname = "placeholder"\n')

            injected = bc.capture_cell(lane_dir, tree.main, tree.stub_bin, "3",
                                       [], env_overrides={"LANE_CLI": "gemini", "HOME": str(tree.home)})
            self.assertTrue(injected["launched"], injected.get("stderr"))
            self.assertIn("--admin-policy", _agent_args(injected))

            denied = bc.capture_cell(lane_dir, tree.main, tree.stub_bin, "3",
                                     ["--admin-policy", "/dev/null"],
                                     env_overrides={"LANE_CLI": "gemini", "HOME": str(tree.home)})
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
                         "_cli_launch.sh", "_lane_cleanup.sh", "_agent_registry.sh",
                         "_gh_config_dir.sh", "_lane_refresh.sh"):
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
                         "_cli_launch.sh", "_lane_cleanup.sh", "_agent_registry.sh",
                         "_gh_config_dir.sh", "_lane_refresh.sh"):
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
class Lane3RefreshesAtLaunch(unittest.TestCase):
    """harmonic-forge#761, replacing #322 AC5's check-only design by operator
    ruling (2026-09-26): lane3 brings its worktree to origin/main and relinks
    backend/.env at launch, and RECORDS the outcome, so a repaired
    precondition stays distinguishable from one that never needed repair.
    It still refuses when the remote state cannot be determined, or when a
    checkout would carry tracked changes."""

    REFRESH_KEYS = "LANE_REFRESH_STATUS,LANE_REFRESH_FROM,LANE_REFRESH_TO,LANE_REFRESH_ENV"

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

    def _run3(self, tree, args=()):
        return tree.run("3", list(args), LANE_CLI="claude",
                        LANE_CAPTURE_EXTRA_ENV=self.REFRESH_KEYS,
                        LANE_REFRESH_LOG_DIR=str(tree.root / "refresh-log"))

    @staticmethod
    def _head(path):
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=path,
                              check=True, capture_output=True,
                              text=True).stdout.strip()

    def test_lane3_updates_a_stale_worktree_and_records_it(self):
        """TC1 (lane3 path) and AC4."""
        with _FixtureTree() as tree:
            before = self._head(tree.lane3)
            remote_sha = self._advance_origin(tree)
            cell = self._run3(tree)
            self.assertTrue(cell["launched"], cell.get("stderr"))
            self.assertEqual(self._head(tree.lane3), remote_sha)
            env = cell["extra_env"]
            self.assertEqual(env["LANE_REFRESH_STATUS"], "updated")
            self.assertEqual(env["LANE_REFRESH_FROM"], before)
            self.assertEqual(env["LANE_REFRESH_TO"], remote_sha)
            self.assertIn("updated at launch", cell["stderr"])
            log = (tree.root / "refresh-log" / "refresh.log").read_text().splitlines()
            self.assertEqual(len(log), 1)
            self.assertIn("\tupdated\t", log[0])

    def test_lane3_fetches_a_tip_it_has_never_seen(self):
        """The old check could only refuse here; the tip is fetched now."""
        with _FixtureTree() as tree:
            remote_sha = self._advance_origin_from_elsewhere(tree)
            cell = self._run3(tree)
            self.assertTrue(cell["launched"], cell.get("stderr"))
            self.assertEqual(self._head(tree.lane3), remote_sha)
            self.assertEqual(cell["extra_env"]["LANE_REFRESH_STATUS"], "updated")

    def test_lane3_refuses_on_tracked_changes_and_moves_nothing(self):
        """TC2 (lane3 path)."""
        with _FixtureTree() as tree:
            before = self._head(tree.lane3)
            self._advance_origin(tree)
            (tree.lane3 / "README.md").write_text("locally modified\n")
            cell = self._run3(tree)
            self.assertFalse(cell["launched"])
            self.assertIn("tracked changes", cell["stderr"])
            self.assertIn("README.md", cell["stderr"])
            self.assertEqual(self._head(tree.lane3), before)

    def test_an_untracked_backend_env_alone_never_blocks(self):
        with _FixtureTree(with_backend_env=True) as tree:
            self._advance_origin(tree)
            (tree.lane3 / "backend").mkdir()
            (tree.lane3 / "backend" / ".env").write_text("KEY=stale\n")
            cell = self._run3(tree)
            self.assertTrue(cell["launched"], cell.get("stderr"))

    def test_lane3_leaves_a_worktree_ahead_of_origin_alone(self):
        """TC8: HEAD already contains the remote tip (a gate on a Lane 2
        branch) records `current` and does not move."""
        with _FixtureTree() as tree:
            (tree.lane3 / "AHEAD.md").write_text("ahead\n")
            for cmd in (["git", "add", "AHEAD.md"],
                        ["git", "-c", "user.email=a@example.invalid",
                         "-c", "user.name=A", "commit", "-q", "-m", "ahead"]):
                subprocess.run(cmd, cwd=tree.lane3, check=True, capture_output=True)
            ahead = self._head(tree.lane3)
            cell = self._run3(tree)
            self.assertTrue(cell["launched"], cell.get("stderr"))
            self.assertEqual(self._head(tree.lane3), ahead)
            self.assertEqual(cell["extra_env"]["LANE_REFRESH_STATUS"], "current")

    def test_lane3_escape_hatch_skips_the_update(self):
        """TC5."""
        with _FixtureTree() as tree:
            before = self._head(tree.lane3)
            self._advance_origin(tree)
            cell = self._run3(tree, ["--ack-stale", "gating PR #123's branch"])
            self.assertTrue(cell["launched"], cell.get("stderr"))
            self.assertIn("staleness acknowledged", cell["stderr"])
            self.assertEqual(self._head(tree.lane3), before)
            self.assertEqual(cell["extra_env"]["LANE_REFRESH_STATUS"], "ack-stale")

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

    def test_lane3_starts_when_current(self):
        with _FixtureTree() as tree:
            cell = self._run3(tree)
            self.assertTrue(cell["launched"], cell.get("stderr"))
            self.assertEqual(cell["extra_env"]["LANE_REFRESH_STATUS"], "current")

    def test_lane3_refuses_when_the_remote_cannot_be_reached(self):
        """TC4 (lane3 path).  'Cannot determine' is treated as drift, not as
        its absence."""
        with _FixtureTree() as tree:
            subprocess.run(["git", "remote", "set-url", "origin",
                            str(tree.root / "does-not-exist.git")],
                           cwd=tree.main, check=True, capture_output=True)
            cell = self._run3(tree)
            self.assertFalse(cell["launched"])
            self.assertIn("cannot determine", cell["stderr"])

    def test_the_refresh_uses_the_fully_qualified_ref(self):
        """NC4.  `git ls-remote origin main` returns two lines on a real repo
        (refs/heads/main and refs/remotes/origin/main); the qualified form
        returns exactly one."""
        source = _code_only(LANE_DIR / "_lane_refresh.sh")
        self.assertIn("ls-remote origin refs/heads/main", source)
        self.assertNotIn("ls-remote origin main", source)

    def test_nothing_reads_the_stale_remote_tracking_ref(self):
        """NC2.  The update target is the SHA ls-remote returned, never the
        local remote-tracking ref."""
        for name in ("lane3", "_lane_refresh.sh"):
            with self.subTest(file=name):
                self.assertNotIn("rev-parse origin/main",
                                 _code_only(LANE_DIR / name))

    def test_no_launcher_pulls_resets_or_pushes(self):
        """AC8: the only mutations are the recorded checkout and the .env
        relink."""
        for name in ("lane1", "lane2", "lane3", "_lane_refresh.sh"):
            source = _code_only(LANE_DIR / name)
            for mutation in ("git pull", "git reset", "git push", "reset --hard"):
                with self.subTest(file=name, mutation=mutation):
                    self.assertNotIn(mutation, source)

    def test_lane3_relinks_a_drifted_env_and_records_it(self):
        """TC12, AC6."""
        with _FixtureTree(with_backend_env=True) as tree:
            (tree.lane3 / "backend").mkdir()
            (tree.lane3 / "backend" / ".env").write_text("KEY=stale\n")
            cell = self._run3(tree)
            self.assertTrue(cell["launched"], cell.get("stderr"))
            link = tree.lane3 / "backend" / ".env"
            self.assertTrue(link.is_symlink())
            self.assertEqual(link.resolve(), (tree.main / "backend" / ".env").resolve())
            self.assertEqual(cell["extra_env"]["LANE_REFRESH_ENV"], "relinked")

    def test_ack_stale_still_relinks_the_env(self):
        """--ack-stale skips only the checkout; backend/.env has no
        legitimate per-worktree divergence."""
        with _FixtureTree(with_backend_env=True) as tree:
            (tree.lane3 / "backend").mkdir()
            (tree.lane3 / "backend" / ".env").write_text("KEY=stale\n")
            cell = self._run3(tree, ["--ack-stale", "deliberate"])
            self.assertTrue(cell["launched"], cell.get("stderr"))
            self.assertTrue((tree.lane3 / "backend" / ".env").is_symlink())
            self.assertEqual(cell["extra_env"]["LANE_REFRESH_ENV"], "relinked")

    def test_lane3_records_a_correct_env_link_as_ok(self):
        with _FixtureTree(with_backend_env=True) as tree:
            (tree.lane3 / "backend").mkdir()
            (tree.lane3 / "backend" / ".env").symlink_to(
                tree.main / "backend" / ".env")
            cell = self._run3(tree)
            self.assertTrue(cell["launched"], cell.get("stderr"))
            self.assertEqual(cell["extra_env"]["LANE_REFRESH_ENV"], "ok")


class Lane3Provision(unittest.TestCase):
    """The two mutations lane3 gave up, and nothing else."""

    def test_provision_repairs_staleness_and_the_env_symlink(self):
        with _FixtureTree(with_backend_env=True) as tree:
            (tree.lane3 / "backend").mkdir()
            (tree.lane3 / "backend" / ".env").write_text("KEY=stale\n")
            Lane3RefreshesAtLaunch._advance_origin(tree)

            proc = tree.run_script("lane3-provision")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertTrue((tree.lane3 / "backend" / ".env").is_symlink())

            # And lane3 now starts, which is the whole contract between them.
            cell = tree.run("3", [], LANE_CLI="claude")
            self.assertTrue(cell["launched"], cell.get("stderr"))

    def test_provision_refuses_to_clobber_uncommitted_work(self):
        with _FixtureTree() as tree:
            Lane3RefreshesAtLaunch._advance_origin(tree)
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
# harmonic-forge#665 -- launch-default model and effort
# ---------------------------------------------------------------------------
def _flag_values(args: list[str], flag: str) -> list[str]:
    """Every value given for `flag`, in spaced or `=` form."""
    values = []
    for i, arg in enumerate(args):
        if arg == flag and i + 1 < len(args):
            values.append(args[i + 1])
        elif arg.startswith(flag + "="):
            values.append(arg[len(flag) + 1:])
    return values


class LaunchDefaultModelAndEffort(unittest.TestCase):

    def test_clean_env_injects_sonnet_and_no_effort_at_every_lane(self):
        """TC1 / AC1."""
        with _FixtureTree() as tree:
            for lane in ("1", "2", "3"):
                with self.subTest(lane=lane):
                    cell = tree.run(lane, [], LANE_CLI="claude")
                    self.assertTrue(cell["launched"], cell.get("stderr"))
                    args = _agent_args(cell)
                    self.assertEqual(_flag_values(args, "--model"), ["sonnet"])
                    self.assertEqual(_flag_values(args, "--effort"), [])

    def test_env_sets_model_and_effort(self):
        """TC2 / AC2."""
        with _FixtureTree() as tree:
            cell = tree.run("1", [], LANE_CLI="claude",
                            LANE_DEFAULT_MODEL="opus", LANE_DEFAULT_EFFORT="low")
            self.assertTrue(cell["launched"], cell.get("stderr"))
            args = _agent_args(cell)
            self.assertEqual(_flag_values(args, "--model"), ["opus"])
            self.assertEqual(_flag_values(args, "--effort"), ["low"])

    def test_passthrough_suppresses_injection_in_every_form(self):
        """TC3 / TC4 / AC2: spaced and `=` forms, at Lane 2 and Lane 3, and
        with native args after a bare `--` (which _lane_args.sh consumes)."""
        cases = [
            ("2", ["--model", "fable"], "--model", ["fable"]),
            ("2", ["--model=fable"], "--model", ["fable"]),
            ("3", ["--model", "fable"], "--model", ["fable"]),
            ("3", ["--model=fable"], "--model", ["fable"]),
            ("2", ["--effort", "max"], "--effort", ["max"]),
            ("3", ["--effort=high"], "--effort", ["high"]),
            ("3", ["--", "--effort", "high"], "--effort", ["high"]),
        ]
        with _FixtureTree() as tree:
            for lane, argv, flag, expected in cases:
                with self.subTest(lane=lane, argv=argv):
                    cell = tree.run(lane, argv, LANE_CLI="claude",
                                    LANE_DEFAULT_MODEL="opus",
                                    LANE_DEFAULT_EFFORT="low")
                    self.assertTrue(cell["launched"], cell.get("stderr"))
                    self.assertEqual(_flag_values(_agent_args(cell), flag),
                                     expected)

    def test_empty_passthrough_value_falls_back_to_the_default(self):
        """Operator ruling 2026-09-15: an empty value means "not given" --
        sonnet, and no --effort."""
        cases = [["--model="], ["--model", ""], ["--effort="],
                 ["--effort", ""], ["--model=", "--effort", ""]]
        with _FixtureTree() as tree:
            for argv in cases:
                with self.subTest(argv=argv):
                    cell = tree.run("2", argv, LANE_CLI="claude")
                    self.assertTrue(cell["launched"], cell.get("stderr"))
                    args = _agent_args(cell)
                    self.assertEqual(_flag_values(args, "--model"), ["sonnet"])
                    self.assertEqual(_flag_values(args, "--effort"), [])
                    self.assertNotIn("", args)

    def test_empty_env_values_fall_back_to_the_default(self):
        with _FixtureTree() as tree:
            cell = tree.run("2", [], LANE_CLI="claude", LANE_DEFAULT_MODEL="",
                            LANE_DEFAULT_EFFORT="")
            self.assertTrue(cell["launched"], cell.get("stderr"))
            args = _agent_args(cell)
            self.assertEqual(_flag_values(args, "--model"), ["sonnet"])
            self.assertEqual(_flag_values(args, "--effort"), [])

    def test_lane_model_is_never_converted_into_a_launch_flag(self):
        """TC5 / AC3: LANE_MODEL is the tier hooks' bypass, not a model choice."""
        with _FixtureTree() as tree:
            cell = tree.run("2", [], LANE_CLI="claude", LANE_MODEL="opus")
            self.assertTrue(cell["launched"], cell.get("stderr"))
            self.assertEqual(_flag_values(_agent_args(cell), "--model"),
                             ["sonnet"])

    REFUSED = ("CLAUDE_CODE_EFFORT_LEVEL", "ANTHROPIC_MODEL",
               "ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL",
               "ANTHROPIC_DEFAULT_FABLE_MODEL", "ANTHROPIC_DEFAULT_HAIKU_MODEL")

    def test_contradicting_env_refuses_and_names_the_variable(self):
        """TC6 / AC4: the error LEADS with the variable that was set."""
        with _FixtureTree() as tree:
            for var in self.REFUSED:
                with self.subTest(var=var):
                    cell = tree.run("2", [], LANE_CLI="claude", **{var: "x"})
                    self.assertFalse(cell["launched"])
                    self.assertNotEqual(cell["returncode"], 0)
                    self.assertTrue(cell["stderr"].startswith(f"lane2: {var} is set"),
                                    cell["stderr"])

    def test_contradicting_settings_env_refuses(self):
        """A settings `env` block reaches the session like the shell does."""
        with _FixtureTree() as tree:
            for where in ("user", "project", "project-local"):
                for var in ("ANTHROPIC_MODEL", "CLAUDE_CODE_EFFORT_LEVEL",
                            "ANTHROPIC_DEFAULT_OPUS_MODEL"):
                    with self.subTest(where=where, var=var):
                        path = {
                            "user": tree.home / ".claude" / "settings.json",
                            "project": tree.lane2 / ".claude" / "settings.json",
                            "project-local": tree.lane2 / ".claude" / "settings.local.json",
                        }[where]
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_text(json.dumps({"env": {var: "x"}}))
                        try:
                            cell = tree.run("2", [], LANE_CLI="claude")
                        finally:
                            path.unlink()
                        self.assertFalse(cell["launched"])
                        self.assertTrue(
                            cell["stderr"].startswith(f"lane2: {var} in {path}"),
                            cell["stderr"])

    def test_unrelated_or_empty_settings_env_still_launches(self):
        with _FixtureTree() as tree:
            path = tree.home / ".claude" / "settings.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"env": {"ANTHROPIC_MODEL": "",
                                                "CLAUDE_CODE_ENABLE_TODO_TOOLS": "1"}}))
            cell = tree.run("2", [], LANE_CLI="claude")
            self.assertTrue(cell["launched"], cell.get("stderr"))

    def test_invalid_effort_refuses_to_launch(self):
        """Exact match against ONE level: typos, fragments and runs of valid
        levels are all refused."""
        with _FixtureTree() as tree:
            for value in ("lwo", "igh", "low medium", "high xhigh", " low"):
                with self.subTest(value=value):
                    cell = tree.run("2", [], LANE_CLI="claude",
                                    LANE_DEFAULT_EFFORT=value)
                    self.assertFalse(cell["launched"])
                    self.assertIn("not an effort level", cell["stderr"])

    def test_every_valid_effort_level_launches(self):
        with _FixtureTree() as tree:
            for value in ("low", "medium", "high", "xhigh", "max"):
                with self.subTest(value=value):
                    cell = tree.run("2", [], LANE_CLI="claude",
                                    LANE_DEFAULT_EFFORT=value)
                    self.assertTrue(cell["launched"], cell.get("stderr"))
                    self.assertEqual(
                        _flag_values(_agent_args(cell), "--effort"), [value])

    def test_codex_and_gemini_get_no_model_or_effort_and_are_not_refused(self):
        """TC7 / AC5: the refusal and injection are claude-only declarations."""
        with _FixtureTree() as tree:
            for agent in ("codex", "gemini"):
                with self.subTest(agent=agent):
                    cell = tree.run("2", [], LANE_CLI=agent,
                                    LANE_DEFAULT_MODEL="opus",
                                    LANE_DEFAULT_EFFORT="low",
                                    **{var: "x" for var in self.REFUSED})
                    self.assertTrue(cell["launched"], cell.get("stderr"))
                    args = _agent_args(cell)
                    self.assertNotIn("--model", args)
                    self.assertNotIn("--effort", args)


# ---------------------------------------------------------------------------
# harmonic-forge#754 -- Codex runs in-process, so LANE reaches its commands
# ---------------------------------------------------------------------------
class CodexSessionFlags(unittest.TestCase):
    """Codex 0.157's TUI hands tool execution to a shared app-server daemon
    that never saw LANE. The launcher injects `--no-daemon` at every lane, and
    refuses the passthrough that would undo it. The LIVE proof (AC1/AC3) is in
    the issue's PR -- these assert the launch tuple the canary relied on."""

    SHAPES = ([], ["-p", "hi"], ["resume", "--last"], ["exec", "printenv LANE"])

    def test_codex_gets_no_daemon_at_every_lane_before_any_subcommand(self):
        with _FixtureTree() as tree:
            for lane in ("1", "2", "3"):
                for shape in self.SHAPES:
                    with self.subTest(lane=lane, shape=shape):
                        cell = tree.run(lane, ["--agent", "codex", "--", *shape])
                        self.assertTrue(cell["launched"], cell.get("stderr"))
                        args = _agent_args(cell)
                        self.assertEqual(args.count("--no-daemon"), 1, args)
                        # Top-level flag: must precede the caller's args, or
                        # clap rejects it after `exec` (verified live).
                        self.assertEqual(args[len(args) - len(shape):], shape)
                        self.assertLess(args.index("--no-daemon"),
                                        len(args) - len(shape))

    def test_other_agents_get_no_session_flag(self):
        with _FixtureTree() as tree:
            for lane in ("1", "2", "3"):
                for agent in ("claude", "gemini"):
                    with self.subTest(lane=lane, agent=agent):
                        cell = tree.run(lane, ["--agent", agent])
                        self.assertTrue(cell["launched"], cell.get("stderr"))
                        self.assertNotIn("--no-daemon", _agent_args(cell))

    def test_passthrough_that_would_reach_a_daemon_is_refused(self):
        refused = (["--no-daemon"], ["--remote", "unix://"],
                   ["--remote=ws://127.0.0.1:1"], ["--", "--remote", "unix://"])
        with _FixtureTree() as tree:
            for lane in ("1", "2", "3"):
                for args in refused:
                    with self.subTest(lane=lane, args=args):
                        passthrough = args if args[0] == "--" else ["--", *args]
                        cell = tree.run(lane, ["--agent", "codex", *passthrough])
                        self.assertFalse(cell["launched"])
                        self.assertIn("cannot be set, removed, or contradicted",
                                      cell["stderr"])

    def test_session_flags_are_a_required_registry_attribute(self):
        """NC5: an agent missing the declaration is a launch refusal, never an
        agent that silently gets no session flags."""
        source = (LANE_DIR / "_agent_registry.sh").read_text()
        required = source.split("_REGISTRY_REQUIRED_ATTRS=(", 1)[1].split(")", 1)[0]
        for attr in ("AGENT_SESSION_FLAGS", "AGENT_SESSION_DENIED"):
            with self.subTest(attr=attr):
                self.assertIn(attr, required.split())


class CodexLaneTmp(unittest.TestCase):
    """harmonic-forge#756: every Codex lane excludes `/tmp` and `$TMPDIR` as
    sandbox writable roots (whose `.git` protection mount otherwise created an
    empty host `/tmp/.git`), and gets a private TMPDIR it can write instead.
    The live proof (AC2's `/tmp/.git` poll) is Lane 1's TC1; these assert the
    launch tuple it relies on."""

    KEYS = ["-c", "sandbox_workspace_write.exclude_slash_tmp=true",
            "-c", "sandbox_workspace_write.exclude_tmpdir_env_var=true"]
    ADD_DIRS = {
        "1": [".cache/codex-lane-tmp/lane1"],
        "2": ["Harmonic_Projects/.worktrees", ".cache/codex-lane-tmp/lane2",
              ".cache/cymagraph"],
        "3": ["Harmonic_Projects/testplan", ".cache/codex-lane-tmp/lane3",
              ".cache/cymagraph"],
    }
    SENTINEL_TMPDIR = "/nonexistent/caller-tmpdir"

    def _run(self, tree, lane, args):
        return tree.run(lane, args, LANE_CAPTURE_EXTRA_ENV="TMPDIR,GIT_CEILING_DIRECTORIES",
                        TMPDIR=self.SENTINEL_TMPDIR)

    def test_codex_carries_both_keys_each_add_dir_and_a_private_tmpdir(self):
        with _FixtureTree() as tree:
            for lane in ("1", "2", "3"):
                for shape in ([], ["resume", "--last"]):
                    with self.subTest(lane=lane, shape=shape):
                        cell = self._run(tree, lane, ["--agent", "codex", "--", *shape])
                        self.assertTrue(cell["launched"], cell.get("stderr"))
                        args = _agent_args(cell)
                        # Both keys, once each, contiguous, before the caller's args.
                        start = args.index("-c")
                        self.assertEqual(args[start:start + 4], self.KEYS)
                        self.assertEqual(args.count("-c"), 2)
                        self.assertEqual(args[len(args) - len(shape):], shape)
                        self.assertLess(start + 4, len(args) - len(shape) + 1)
                        granted = [args[i + 1] for i, a in enumerate(args)
                                   if a == "--add-dir"]
                        expected = [f"{tree.home}/{d}" for d in self.ADD_DIRS[lane]]
                        self.assertEqual(granted, expected)
                        for path in expected:
                            self.assertTrue(Path(path).is_dir(), path)
                        tmpdir = cell["extra_env"]["TMPDIR"]
                        self.assertEqual(tmpdir, f"{tree.home}/.cache/codex-lane-tmp/lane{lane}")
                        self.assertIn(tmpdir, granted, "TMPDIR must be a writable root")
                        self.assertNotEqual(tmpdir, self.SENTINEL_TMPDIR)
                        # Codex mounts its empty `.git` at every writable root,
                        # TMPDIR included: discovery must stop above it.
                        self.assertEqual(
                            (cell["extra_env"]["GIT_CEILING_DIRECTORIES"] or "").split(":")[0], tmpdir)

    def test_claude_and_gemini_are_unchanged_and_keep_the_callers_tmpdir(self):
        """TC7: no key, no add-dir, and TMPDIR is not overridden."""
        with _FixtureTree() as tree:
            for lane in ("1", "2", "3"):
                for agent in ("claude", "gemini"):
                    with self.subTest(lane=lane, agent=agent):
                        cell = self._run(tree, lane, ["--agent", agent])
                        self.assertTrue(cell["launched"], cell.get("stderr"))
                        args = _agent_args(cell)
                        self.assertNotIn("--add-dir", args)
                        self.assertFalse(any("sandbox_workspace_write" in a for a in args))
                        self.assertEqual(cell["extra_env"]["TMPDIR"], self.SENTINEL_TMPDIR)
            self.assertFalse((tree.home / ".cache" / "codex-lane-tmp").exists()
                             and any((tree.home / ".cache" / "codex-lane-tmp").iterdir()),
                             "a non-Codex launch created a Codex lane dir")

    def test_passthrough_override_of_an_exclude_key_is_refused(self):
        """NC4: `-c` is last-wins and the launcher's keys come before
        passthrough, so an override must be refused, not left to precedence --
        in every spelling Codex accepts for a config override."""
        refused = (
            ["-c", "sandbox_workspace_write.exclude_slash_tmp=false"],
            ["-c", "sandbox_workspace_write.exclude_tmpdir_env_var=false"],
            ["--config", "sandbox_workspace_write.exclude_slash_tmp=false"],
            ["-csandbox_workspace_write.exclude_slash_tmp=false"],
            # cross-family verify: codex also accepts `-c=key=value`.
            ["-c=sandbox_workspace_write.exclude_slash_tmp=false"],
            ["--config=sandbox_workspace_write.exclude_tmpdir_env_var=false"],
            ["-c", "sandbox_workspace_write.exclude_some_future_key=false"],
            ["-c", "sandbox_workspace_write={exclude_slash_tmp=false}"],
            ["--config=sandbox_workspace_write={}"],
            ["-c", 'sandbox_workspace_write.writable_roots=["/tmp"]'],
            ["--config=sandbox_workspace_write.network_access=true"],
            ["exec", "-c", "sandbox_workspace_write.exclude_slash_tmp=false", "true"],
        )
        with _FixtureTree() as tree:
            for lane in ("1", "2", "3"):
                for args in refused:
                    with self.subTest(lane=lane, args=args):
                        cell = tree.run(lane, ["--agent", "codex", "--", *args])
                        self.assertFalse(cell["launched"])
                        self.assertIn("cannot be set, removed, or contradicted",
                                      cell["stderr"])

    def test_unrelated_config_overrides_still_launch(self):
        """The deny is scoped to the sandbox_workspace_write table: other
        `-c` overrides, and a `*` token that must never glob, are untouched."""
        allowed = (["-c", "model_reasoning_effort=high"],
                   ["--config=model=gpt-5"],
                   ["-p", "sandbox_workspace_write is mentioned in prose"],
                   ["*.md"])
        with _FixtureTree() as tree:
            for args in allowed:
                with self.subTest(args=args):
                    cell = tree.run("2", ["--agent", "codex", "--", *args])
                    self.assertTrue(cell["launched"], cell.get("stderr"))
                    self.assertEqual(_agent_args(cell)[-len(args):], args)

    def test_the_prefix_token_is_never_glob_expanded(self):
        """`registry_lane_denied_tokens` must emit the `*` token literally even
        when the cwd holds a file the glob would match."""
        with _FixtureTree() as tree:
            (tree.main / "sandbox_workspace_write.exclude_decoy").write_text("")
            out = subprocess.run(
                ["bash", "-c",
                 f'source "{LANE_DIR}/_agent_registry.sh" && '
                 'registry_lane_denied_tokens codex 2'],
                cwd=tree.main, capture_output=True, text=True, check=True).stdout
            self.assertIn("sandbox_workspace_write*", out.split())
            self.assertNotIn("sandbox_workspace_write.exclude_decoy", out)

    def test_lane3_compare_rejects_a_listed_token_going_missing(self):
        """NC3: the Lane 3 cell is justified by the closed list, and the
        comparator accepts ADDED listed tokens only -- dropping one the
        baseline already had (here `--sandbox`) is still a diff."""
        fixture = json.loads(BASELINE.read_text())
        additions = bc._load_lane3_additions(ADDITIONS)
        key = "lane3/codex/none"
        good = bc._apply_declared_deltas(fixture["cells"][key], "codex")
        good = json.loads(json.dumps(good))
        good["argv"] += self.KEYS
        self.assertEqual(bc.compare({"cells": {key: good}},
                                    {"cells": {key: fixture["cells"][key]}},
                                    additions), [])
        bad = json.loads(json.dumps(good))
        i = bad["argv"].index("--sandbox")
        del bad["argv"][i:i + 2]
        self.assertNotEqual(bc.compare({"cells": {key: bad}},
                                       {"cells": {key: fixture["cells"][key]}},
                                       additions), [])


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
                 "codex": "codex-cli 0.156.9",
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
        for version in ("2.1.250", "0.157.0", "0.56.0"):
            with self.subTest(version=version):
                self.assertIn(version, source)


# ---------------------------------------------------------------------------
# harmonic-forge#651 -- sync_rules.py --pull runs before the final exec
# ---------------------------------------------------------------------------
class PlatformRulesSync(unittest.TestCase):
    """Every lane launches through the shared `_cli_launch.sh`, which is where
    `sync_rules.py --pull` is invoked (immediately before the final exec, per
    the issue) -- so asserting it fires at all three lanes exercises the one
    shared call site rather than three separate ones. Best-effort by design:
    the fixture's HOME has no `harmonic-forge/sync_rules.py` at all, which is
    exactly the "sync fails" case, and the launch must still proceed."""

    def test_sync_rules_pull_runs_and_failure_does_not_block_launch(self):
        with _FixtureTree() as tree:
            for lane in ("1", "2", "3"):
                with self.subTest(lane=lane):
                    cell = tree.run(lane, [])
                    self.assertTrue(cell["launched"], cell.get("stderr"))
                    self.assertIn("sync_rules.py --pull failed", cell["stderr"])
                    self.assertIn("continuing with the current "
                                  "~/harmonic-forge checkout", cell["stderr"])

    def test_sync_rules_pull_invoked_before_the_final_exec(self):
        """The call site lives in `_cli_launch.sh`, sourced by every launcher
        strictly before its own final `systemd-inhibit` invocation -- grepped
        directly, the same style `_read_launcher_source` already uses
        elsewhere in this file to check launcher script text rather than
        runtime behavior. harmonic-forge#751 replaced the launchers' `exec
        systemd-inhibit ...` with `lane_run_with_cleanup systemd-inhibit
        ...` (a child, not an exec, so the launcher survives to clean up) --
        this test now locates that final `systemd-inhibit` invocation instead
        of the retired `exec` keyword, keeping the same assertion intent."""
        source = (LANE_DIR / "_cli_launch.sh").read_text()
        self.assertIn("sync_rules.py", source)
        self.assertIn('--pull', source)
        for lane in ("1", "2", "3"):
            launcher = _code_only(LANE_DIR / f"lane{lane}")
            source_idx = launcher.find("_cli_launch.sh")
            inhibit_idx = launcher.rfind("systemd-inhibit")
            self.assertGreater(inhibit_idx, source_idx,
                                f"lane{lane}: _cli_launch.sh must be sourced "
                                "before the final systemd-inhibit invocation")


# ---------------------------------------------------------------------------
# harmonic-forge#751 -- lane launchers clean up on SIGHUP/exit
# ---------------------------------------------------------------------------
class LaneCleanupWrapper(unittest.TestCase):
    """Unit tests for `_lane_cleanup.sh`'s `lane_run_with_cleanup`, run
    directly with bash -- not through a full launcher, so these don't need a
    fixture tree. A fake `mise` on PATH records whether `mise run pc-down`
    was invoked; nothing here starts a real service."""

    CLEANUP_SH = LANE_DIR / "_lane_cleanup.sh"

    def _fake_mise_bin(self, tmp: Path, *, declares_pc_down: bool) -> Path:
        stub_bin = tmp / "stubbin"
        stub_bin.mkdir()
        calls_log = tmp / "mise_run_calls.log"
        tasks_file = tmp / "tasks_ls_output.txt"
        tasks_file.write_text("pc-down\n" if declares_pc_down else "check\nrestart\n")
        mise = stub_bin / "mise"
        mise.write_text(
            "#!/usr/bin/env bash\n"
            f"tasks_file={json.dumps(str(tasks_file))!s}\n"
            f"calls_log={json.dumps(str(calls_log))!s}\n"
            "if [ \"$1\" = tasks ] && [ \"$2\" = ls ]; then cat \"$tasks_file\"; exit 0; fi\n"
            "if [ \"$1\" = run ] && [ \"$2\" = pc-down ]; then "
            "echo called >> \"$calls_log\"; exit 0; fi\n"
            "exit 0\n"
        )
        mise.chmod(0o755)
        return stub_bin, calls_log

    def _run_wrapper(self, tmp: Path, stub_bin: Path, lane_name: str,
                      child_cmd: str) -> subprocess.CompletedProcess:
        script = tmp / "run.sh"
        script.write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            f"_lane_name={lane_name}\n"
            f"source {json.dumps(str(self.CLEANUP_SH))!s}\n"
            f"lane_run_with_cleanup bash -c {json.dumps(child_cmd)!s}\n"
        )
        script.chmod(0o755)
        env = dict(os.environ)
        env["PATH"] = f"{stub_bin}{os.pathsep}{env['PATH']}"
        return subprocess.run(["bash", str(script)], cwd=tmp, env=env,
                               capture_output=True, text=True, timeout=30)

    def test_pc_down_runs_for_lane3_when_task_declared(self):
        with tempfile.TemporaryDirectory() as tmp_s:
            tmp = Path(tmp_s)
            stub_bin, calls_log = self._fake_mise_bin(tmp, declares_pc_down=True)
            proc = self._run_wrapper(tmp, stub_bin, "lane3", "exit 0")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertTrue(calls_log.exists(),
                             "lane3 with a declared pc-down task must run it")

    def test_pc_down_skipped_when_task_not_declared(self):
        with tempfile.TemporaryDirectory() as tmp_s:
            tmp = Path(tmp_s)
            stub_bin, calls_log = self._fake_mise_bin(tmp, declares_pc_down=False)
            proc = self._run_wrapper(tmp, stub_bin, "lane3", "exit 0")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertFalse(calls_log.exists(),
                              "no pc-down task declared -- nothing should run")

    def test_pc_down_never_runs_for_lane1_even_when_task_declared(self):
        """lane1 always runs in the shared main checkout, which on HRSE2
        legitimately runs the operator's persistent dev stack under the same
        `pc-down` task name -- see _lane_cleanup.sh's own comment. This is
        the AC2 guarantee made unconditional rather than "normally" true."""
        with tempfile.TemporaryDirectory() as tmp_s:
            tmp = Path(tmp_s)
            stub_bin, calls_log = self._fake_mise_bin(tmp, declares_pc_down=True)
            proc = self._run_wrapper(tmp, stub_bin, "lane1", "exit 0")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertFalse(calls_log.exists(),
                              "lane1 must never run pc-down, task or not")

    def test_child_exit_status_is_propagated(self):
        with tempfile.TemporaryDirectory() as tmp_s:
            tmp = Path(tmp_s)
            stub_bin, _ = self._fake_mise_bin(tmp, declares_pc_down=False)
            proc = self._run_wrapper(tmp, stub_bin, "lane3", "exit 7")
            self.assertEqual(proc.returncode, 7)

    def test_cleanup_runs_exactly_once_on_group_hup(self):
        """The wrapper never backgrounds its child and never touches job
        control (see _lane_cleanup.sh's header) specifically so the child
        stays in the SAME process group as the wrapper -- the group a real
        terminal hangup signals as a whole. `Popen.send_signal` targets one
        pid only, which would not exercise that path, so this starts the
        wrapper in its own session (`start_new_session=True`) and signals
        the whole process GROUP with `os.killpg`, the way a real hangup
        does."""
        with tempfile.TemporaryDirectory() as tmp_s:
            tmp = Path(tmp_s)
            stub_bin, calls_log = self._fake_mise_bin(tmp, declares_pc_down=True)
            proc = subprocess.Popen(
                ["bash", "-c",
                 f"_lane_name=lane3; source {self.CLEANUP_SH}; "
                 "lane_run_with_cleanup bash -c 'sleep 30'"],
                cwd=tmp,
                env={**os.environ, "PATH": f"{stub_bin}{os.pathsep}{os.environ['PATH']}"},
                start_new_session=True,
            )
            time.sleep(1)
            os.killpg(os.getpgid(proc.pid), signal.SIGHUP)
            proc.wait(timeout=10)
            # The plain child (`sleep 30`, no HUP handler of its own) dies of
            # SIGHUP's own default disposition -- the same signal the
            # wrapper's trap survives long enough to clean up after.
            self.assertEqual(proc.returncode, 129)
            self.assertEqual(calls_log.read_text().count("called\n"), 1,
                              "cleanup must run exactly once, not once per "
                              "signal plus once on EXIT")

    def test_a_single_group_int_does_not_kill_the_session_early(self):
        """Regression test for the finding a preclose-inspection pass caught:
        an earlier design explicitly forwarded INT to the child and then
        immediately ran cleanup and exited, which would kill a live agent
        session on its first Ctrl-C (the CLI's normal single-press cancel
        gesture) instead of letting the CLI handle it. This wrapper does no
        such forwarding -- the child receives the terminal's SIGINT directly
        (same process group), and the wrapper's own trap is deferred until
        the foreground child actually returns. A child that ignores SIGINT
        entirely (`trap '' INT`, standing in for a CLI absorbing a cancel
        keypress) must keep running, and pc-down must not have fired yet,
        for as long as it keeps running after the group signal."""
        with tempfile.TemporaryDirectory() as tmp_s:
            tmp = Path(tmp_s)
            stub_bin, calls_log = self._fake_mise_bin(tmp, declares_pc_down=True)
            proc = subprocess.Popen(
                ["bash", "-c",
                 f"_lane_name=lane3; source {self.CLEANUP_SH}; "
                 "lane_run_with_cleanup bash -c "
                 "'trap \"\" INT; sleep 2; exit 42'"],
                cwd=tmp,
                env={**os.environ, "PATH": f"{stub_bin}{os.pathsep}{os.environ['PATH']}"},
                start_new_session=True,
            )
            time.sleep(0.5)
            os.killpg(os.getpgid(proc.pid), signal.SIGINT)
            time.sleep(0.5)
            self.assertIsNone(proc.poll(),
                               "a single Ctrl-C must not end the session "
                               "while the child is still ignoring it and "
                               "running")
            self.assertFalse(calls_log.exists(),
                              "cleanup must not run before the child "
                              "actually exits")
            proc.wait(timeout=10)
            self.assertEqual(proc.returncode, 42,
                              "the child's own natural exit status, not the "
                              "wrapper's fixed signal-trap code, since it "
                              "was never forwarded a terminating signal")
            self.assertEqual(calls_log.read_text().count("called\n"), 1)

    def test_stdin_is_preserved_without_job_control(self):
        """The rejected `"$@" &` design lost the child's stdin to /dev/null
        (bash's documented behaviour for an async command with job control
        off) -- verified live during review. This wrapper never backgrounds
        the child at all, so stdin needs no special handling; assert it
        actually reaches the child."""
        with tempfile.TemporaryDirectory() as tmp_s:
            tmp = Path(tmp_s)
            stub_bin, _ = self._fake_mise_bin(tmp, declares_pc_down=False)
            script = tmp / "run.sh"
            script.write_text(
                "#!/usr/bin/env bash\n"
                "set -euo pipefail\n"
                "_lane_name=lane3\n"
                f"source {json.dumps(str(self.CLEANUP_SH))!s}\n"
                "lane_run_with_cleanup cat\n"
            )
            script.chmod(0o755)
            env = dict(os.environ)
            env["PATH"] = f"{stub_bin}{os.pathsep}{env['PATH']}"
            result = subprocess.run(["bash", str(script)], cwd=tmp, env=env,
                                     input="hello from the terminal\n",
                                     capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "hello from the terminal\n")


if __name__ == "__main__":
    unittest.main()


class DeferredToolsLaunchPrompt(unittest.TestCase):
    """harmonic-forge#765: every Claude lane's launch prompt tells the session
    to load deferred MCP tools with ToolSearch before calling one unavailable
    (hrse#1392's root cause); Codex and Gemini launches are unchanged."""

    SENTENCE = ('load them with ToolSearch("select:<names>") before concluding a tool is '
                'unavailable, and never report a browser or tool as unavailable from a '
                'names-only listing.')

    def test_every_claude_lane_carries_the_sentence(self):
        with _FixtureTree() as tree:
            for lane in ("1", "2", "3"):
                for agent in ("claude", "codex", "gemini"):
                    with self.subTest(lane=lane, agent=agent):
                        cell = tree.run(lane, ["--agent", agent])
                        self.assertTrue(cell["launched"], cell.get("stderr"))
                        carried = any(self.SENTENCE in a for a in _agent_args(cell))
                        self.assertEqual(carried, agent == "claude")
