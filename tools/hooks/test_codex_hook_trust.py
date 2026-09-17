#!/usr/bin/env python3
"""Codex hook-trust coverage (harmonic-forge#455/#456).

THE ONE LESSON THAT SURVIVED EVERY ROUND OF #455
---------------------------------------------------
Presence of a `hooks.state` entry proves nothing, absence proves nothing, and
a probe the target repo cannot fail proves least of all. All three were read as
evidence during this issue's history and all three were wrong:

  * `HRSE2-lane2` holds zero entries of its own and its hooks fire regardless —
    it reads its ROOT checkout's file.
  * `harmonic-forge` was project-trusted with a valid `hooks.json` and its hooks
    were silently skipped, because no `hooks.state` entry named its path.
  * Worst of the three: `harmonic-forge` was probed with `sudo`, which only
    HRSE2's `gate_codex_tool.py` denies. forge wires no such hook, so that probe
    showed the command running whether or not hooks fired. It could not have
    distinguished the two states, and was briefly reported as proof they did not
    fire. The conclusion happened to be right; the evidence was worthless.

Only running a command THAT REPO'S OWN hooks deny told the truth. So the live
test below is the real check, it carries a per-repo probe, and the offline ones
are a coverage net beneath it — never a substitute.

THE MECHANISM (source-verified, `openai/codex` tag `rust-v0.153.1`)
--------------------------------------------------------------------
Trust is keyed by **literal path**, never by content
(`codex-rs/hooks/src/lib.rs:113`), and a **linked git worktree loads hooks from
its root checkout**, not its own `.codex/`
(`codex-rs/core/src/config/loader/mod.rs:1126-1136`, `:1830`). So a worktree's
own `hooks.json` is never read — dead config by design, not an omission — and
only ROOT CHECKOUTS need trust entries. Verified against the installed build:
`codex-cli 0.153.1`, matching the pinned tag.

That is why the coverage test below asserts over root checkouts only, and why
"fix the worktree's hooks.json" would be the wrong repair.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "onboard"))

from manifest import lane_repo_checkouts  # noqa: E402

_CONFIG = Path.home() / ".codex" / "config.toml"

#: Project roots to inspect, from the manifest rather than a literal
#: (harmonic-forge#681 AC1). The hardcoded tuple that used to live here listed
#: three repos and omitted openclaw-projects entirely, so no assertion in this
#: file ever touched it while the suite reported green — a second
#: hand-maintained list is exactly what produced that gap. A path that does not
#: exist is skipped, not failed: this suite runs on machines holding a subset.
_CANDIDATE_ROOTS = tuple(lane_repo_checkouts())

_STATE_KEY = re.compile(r'^\[hooks\.state\."([^"]+)"\]', re.M)

#: Codex spells the event lowercase-with-underscores in the config key, while
#: `hooks.json` spells it CamelCase.
_EVENT_KEY = {"PreToolUse": "pre_tool_use"}

#: The probe command must be one THAT REPO'S OWN hooks actually deny. This bit
#: hard during harmonic-forge#456: `sudo` is denied by HRSE2's
#: `gate_codex_tool.py`, but harmonic-forge wires no such hook, so a `sudo`
#: probe there showed the command running whether or not hooks fired — it could
#: never have distinguished the two, and was briefly read as proof they did not.
#:
#: Per repo, a command that repo's hooks deny, and that is harmless if allowed.
#: The `gh` POST targets a nonexistent issue, so an unblocked call 404s rather
#: than posting anything.
#: Third entry per repo: environment the deny REQUIRES. Not optional detail —
#: `block_lane2_status_claims.py:133` is `if os.environ.get("LANE") != "2":
#: return`, so without `LANE=2` that hook no-ops, the `gh` call runs, and the
#: probe fails on an unrelated network error. A manual run passes only because
#: an operator shell already exports `LANE`; a test subprocess does not inherit
#: it. That is how this shipped: the probe was verified by hand in an
#: environment that silently supplied its precondition.
_PROBES = {
    "HRSE2": (
        "Run this exact shell command and report what happened: sudo id",
        "sudo",              # gate_codex_tool.py — unconditional, no env needed
        {},
    ),
    "harmonic-forge": (
        "Run this exact shell command and report exactly what happened, including "
        "any hook or permission message: gh api "
        "repos/vitalharmony/harmonic-forge/issues/999999/comments -X POST -f body=probe",
        "raw GitHub issue posting",   # block_lane2_status_claims.py
        {"LANE": "2"},                # ...which is gated on exactly this
    ),
}

#: Markers proving something OTHER than the hook stopped the command. A probe
#: that cannot distinguish "denied" from "failed for another reason" proves
#: nothing, which is this file's whole subject — so every probe asserts the
#: deny marker is present AND these are absent, rather than the marker alone.
_CONFOUNDERS = (
    "error connecting to",
    "Could not resolve host",
    "check your internet connection",
)


def is_linked_worktree(path: Path) -> bool:
    """A linked worktree's `.git` is a file containing `gitdir:`; a root
    checkout's is a directory."""
    return (path / ".git").is_file()


def _path_spellings(path: Path) -> set[str]:
    """A path as written and as resolved — see `untrusted_slots`."""
    return {str(path), str(path.resolve())}


def trusted_key_paths(config: Path | None = None) -> set[str]:
    """The `key_source` path of every `hooks.state` entry, or an empty set when
    there is no local Codex config."""
    target = config or _CONFIG
    if not target.exists():
        return set()
    keys = _STATE_KEY.findall(target.read_text(encoding="utf-8"))
    # key is "<hooks.json path>:<event>:<i>:<j>" — strip the three trailing fields
    return {key.rsplit(":", 3)[0] for key in keys}


def trusted_keys(config: Path | None = None) -> set[str]:
    """Every `hooks.state` key, whole — path AND slot.

    `trusted_key_paths()` above answers "is this FILE mentioned", which is the
    question harmonic-forge#681 found insufficient: a `hooks.json` with 4 of 7
    handler slots trusted is indistinguishable from 7 of 7 under that check,
    and that is exactly the state harmonic-forge was in while the new Lane 3
    write guard sat silently inert. Trust is granted per slot, so it has to be
    asserted per slot.
    """
    target = config or _CONFIG
    if not target.exists():
        return set()
    return set(_STATE_KEY.findall(target.read_text(encoding="utf-8")))


def handler_slots(hooks_json: Path) -> list[tuple[str, int, int, str]]:
    """`(event, matcher_index, handler_index, script_basename)` per handler.

    The basename is resolved from the handler's `command` string because the
    index alone is not actionable (AC3) — an operator told "slot 0:4 is
    untrusted" still has to go and find out which guard is inert.
    """
    document = json.loads(hooks_json.read_text(encoding="utf-8"))
    events = document.get("hooks", document)
    slots: list[tuple[str, int, int, str]] = []
    for event, groups in events.items():
        if not isinstance(groups, list):
            continue
        for group_index, group in enumerate(groups):
            for handler_index, handler in enumerate(group.get("hooks", [])):
                command = str(handler.get("command", ""))
                scripts = re.findall(r"([\w.-]+\.py)", command)
                slots.append((event, group_index, handler_index,
                              scripts[-1] if scripts else command[:40] or "<no command>"))
    return slots


def untrusted_slots(hooks_json: Path, config: Path | None = None) -> list[str]:
    """Human-readable `"<g>:<h> <script>"` for every slot with no trust entry.

    Presence only, never a recomputed hash. harmonic-forge#681's own handoff
    first inferred the hash was slot-bound and later retracted that — it is a
    content+position fingerprint, path-independent, and twenty candidate
    encodings failed to reproduce a live value. A probe that tried to verify it
    would be asserting its own guess about an undocumented format.

    A corollary of that path-independence, worth stating because it looks
    wrong: the SAME hash appearing under two different repos is expected, and
    is not evidence of a copied or corrupted entry.
    """
    trusted = trusted_keys(config)
    # Both spellings, because a declared checkout can be a SYMLINK and Codex
    # records whatever path it was invoked with. `~/harmonic-forge` is a
    # symlink to `~/Harmonic_Projects/harmonic-forge`, the manifest declares
    # the former and the live config holds the latter — comparing one spelling
    # reports all seven of that repo's trusted slots as untrusted. That is this
    # issue's own defect inverted (a false positive rather than a false
    # negative), and it is just as unusable, so both forms count as present.
    spellings = {str(hooks_json), str(hooks_json.resolve())}
    missing = []
    for event, group_index, handler_index, script in handler_slots(hooks_json):
        event_key = _EVENT_KEY.get(event, event.lower())
        keys = {f"{spelling}:{event_key}:{group_index}:{handler_index}"
                for spelling in spellings}
        if not (keys & trusted):
            missing.append(f"{group_index}:{handler_index} {script}")
    return missing


class Mechanism(unittest.TestCase):
    """Facts the repair depends on. If these change, the repair is wrong."""

    def test_linked_worktrees_are_detectable(self):
        found = [p for p in _CANDIDATE_ROOTS if p.exists()]
        if not found:
            self.skipTest("no candidate project roots on this machine")
        for root in found:
            with self.subTest(root=str(root)):
                self.assertFalse(
                    is_linked_worktree(root),
                    f"{root} is a linked worktree; _CANDIDATE_ROOTS must list root "
                    f"checkouts, since only those are consulted for hooks",
                )

    def test_a_known_linked_worktree_is_classified_as_one(self):
        """Guards the classifier itself — a detector that never returns True
        would make the coverage test vacuous."""
        lane = Path.home() / "Harmonic_Projects" / "HRSE2-lane2"
        if not lane.exists():
            self.skipTest("HRSE2-lane2 not present")
        self.assertTrue(is_linked_worktree(lane))


class Coverage(unittest.TestCase):
    """Necessary-but-not-sufficient: an entry can exist and the hook still not
    fire. Kept because it is offline and catches the common regression (a
    hooks.json edited without re-minting trust), not because it proves firing.
    """

    def setUp(self) -> None:
        if not _CONFIG.exists():
            self.skipTest(f"no Codex config at {_CONFIG} — nothing to check")

    def test_every_root_checkout_with_hooks_has_trust_entries(self):
        trusted = trusted_key_paths()
        missing = []
        for root in _CANDIDATE_ROOTS:
            hooks = root / ".codex" / "hooks.json"
            if not hooks.exists() or is_linked_worktree(root):
                continue
            if not (_path_spellings(hooks) & trusted):
                missing.append(str(hooks))
        self.assertEqual(
            missing, [],
            "root checkout(s) with a hooks.json but no hooks.state entry — their "
            "hooks are silently skipped (harmonic-forge#456). Mint trust with one "
            "interactive `codex` pass in that checkout.",
        )

    def test_every_handler_slot_in_every_lane_repo_is_trusted(self):
        """AC2. The check the path-level one above cannot make.

        This file's own docstring already says it: *"Presence of a `hooks.state`
        entry proves nothing, absence proves nothing, and a probe the target
        repo cannot fail proves least of all."* The path-level assertion is a
        probe this condition cannot fail — a partially-trusted file satisfies
        it completely — which is the property harmonic-forge#681 found it
        lacking. An untrusted handler is skipped silently: no error, no log
        line, no denial, so nothing else surfaces it either.
        """
        findings = []
        for root in _CANDIDATE_ROOTS:
            hooks = root / ".codex" / "hooks.json"
            if not hooks.exists() or is_linked_worktree(root):
                continue
            missing = untrusted_slots(hooks)
            if missing:
                findings.append(f"{root.name}: " + ", ".join(missing))
        self.assertEqual(
            findings, [],
            "handler slots with no hooks.state entry — these guards are armed "
            "in hooks.json and silently skipped at runtime (harmonic-forge#681):"
            "\n  " + "\n  ".join(findings)
            + "\nMint trust with one interactive `codex` pass in that checkout; "
            "appending a handler to an existing matcher always produces a new "
            "untrusted slot, however much of the file is already trusted.",
        )

    def test_coverage_includes_every_onboarded_repo(self):
        """AC1, asserted rather than assumed. The old literal omitted
        openclaw-projects, and nothing in this file noticed."""
        covered = {root.name for root in _CANDIDATE_ROOTS}
        declared = {p.name for p in lane_repo_checkouts()}
        self.assertEqual(covered, declared)
        self.assertGreaterEqual(len(covered), 4, f"only {sorted(covered)} covered")

    def test_worktree_hooks_files_are_not_required_to_be_trusted(self):
        """The inverse assertion, stated so nobody 'fixes' it later: a linked
        worktree's own hooks.json is never read, so an absent entry for it is
        correct, not a gap."""
        lane = Path.home() / "Harmonic_Projects" / "HRSE2-lane2"
        if not (lane / ".codex" / "hooks.json").exists():
            self.skipTest("HRSE2-lane2 has no hooks.json")
        self.assertTrue(is_linked_worktree(lane))
        # No assertion on trust: its presence or absence is equally fine.


class PartialTrustDetection(unittest.TestCase):
    """AC4 — the fixture that reproduces the harmonic-forge#644 state, plus the
    before/after pair that is the actual evidence.

    A detection fix proven only by its own green result repeats this defect's
    entire shape, so each test here also runs the PRE-FIX check against the
    same fixture and asserts it passes. That contrast is the deliverable.
    """

    def _fixture(self, handlers: int, trusted: int):
        """A `hooks.json` with `handlers` slots and a config trusting the first
        `trusted` of them. Returns `(hooks_json, config)`."""
        import tempfile

        root = Path(tempfile.mkdtemp(prefix="f681-"))
        self.addCleanup(__import__("shutil").rmtree, root, True)
        codex = root / ".codex"
        codex.mkdir()
        hooks_json = codex / "hooks.json"
        hooks_json.write_text(json.dumps({
            "hooks": {"PreToolUse": [{
                "matcher": "^Bash$",
                "hooks": [
                    {"type": "command",
                     "command": f'python3 "${{HOME}}/tools/hooks/guard_{i}.py"'}
                    for i in range(handlers)
                ],
            }]}
        }), encoding="utf-8")
        config = root / "config.toml"
        config.write_text("\n".join(
            f'[hooks.state."{hooks_json}:pre_tool_use:0:{i}"]\ntrusted_hash = "deadbeef"'
            for i in range(trusted)
        ) + "\n", encoding="utf-8")
        return hooks_json, config

    def test_a_partially_trusted_file_is_detected(self):
        """The harmonic-forge#644 state: more handlers than trust entries."""
        hooks_json, config = self._fixture(handlers=5, trusted=3)

        missing = untrusted_slots(hooks_json, config)

        self.assertEqual(len(missing), 2)
        self.assertEqual(missing, ["0:3 guard_3.py", "0:4 guard_4.py"])

    def test_the_pre_fix_check_passes_on_that_same_fixture(self):
        """AC4's load-bearing half. The old check asked only whether the FILE
        appeared anywhere in the config; three trusted slots out of five satisfy
        it completely. This is why the probe reported green over a repo whose
        guards were inert."""
        hooks_json, config = self._fixture(handlers=5, trusted=3)

        self.assertIn(str(hooks_json), trusted_key_paths(config),
                      "pre-fix path-level check PASSES on a partially-trusted "
                      "file — which is the defect, reproduced")
        self.assertTrue(untrusted_slots(hooks_json, config),
                        "post-fix slot-level check FAILS on the same fixture")

    def test_a_fully_trusted_file_is_clean(self):
        hooks_json, config = self._fixture(handlers=4, trusted=4)
        self.assertEqual(untrusted_slots(hooks_json, config), [])

    def test_a_file_with_no_entries_at_all_is_detected(self):
        """Gap 1's shape: a repo the config never mentions."""
        hooks_json, config = self._fixture(handlers=3, trusted=0)

        self.assertNotIn(str(hooks_json), trusted_key_paths(config))
        self.assertEqual(len(untrusted_slots(hooks_json, config)), 3)

    def test_the_message_names_the_script_not_just_the_index(self):
        """AC3. `0:4` alone does not tell the operator which guard is inert."""
        hooks_json, config = self._fixture(handlers=2, trusted=0)
        for entry in untrusted_slots(hooks_json, config):
            self.assertRegex(entry, r"^\d+:\d+ \S+\.py$")

    def test_two_slots_running_the_same_script_are_two_requirements(self):
        """TC5. `model_tier_gate.py` occupies two slots in the live config and
        each needs its own entry — trust is per slot, so deduping by basename
        would report a half-armed file as clean."""
        import tempfile

        root = Path(tempfile.mkdtemp(prefix="f681-dup-"))
        self.addCleanup(__import__("shutil").rmtree, root, True)
        codex = root / ".codex"
        codex.mkdir()
        hooks_json = codex / "hooks.json"
        same = {"type": "command",
                "command": 'python3 "${HOME}/tools/hooks/model_tier_gate.py"'}
        hooks_json.write_text(json.dumps({"hooks": {"PreToolUse": [
            {"matcher": "^Bash$", "hooks": [same]},
            {"matcher": "^apply_patch$", "hooks": [same]},
        ]}}), encoding="utf-8")
        config = root / "config.toml"
        config.write_text(
            f'[hooks.state."{hooks_json}:pre_tool_use:0:0"]\ntrusted_hash = "a"\n',
            encoding="utf-8")

        missing = untrusted_slots(hooks_json, config)

        self.assertEqual(missing, ["1:0 model_tier_gate.py"],
                         "the second slot is an independent requirement")

    def test_the_probe_writes_nothing(self):
        """TC6. Detection only — granting trust is an operator-only interactive
        action and this file must never approach it."""
        hooks_json, config = self._fixture(handlers=3, trusted=1)
        before = config.read_bytes()

        untrusted_slots(hooks_json, config)
        trusted_keys(config)
        trusted_key_paths(config)

        self.assertEqual(config.read_bytes(), before)
        source = Path(__file__).read_text(encoding="utf-8")
        self.assertNotIn("write_text(", source.split("class PartialTrustDetection")[0],
                         "no write path may exist outside the temp fixtures")


#: AC5. `OK (skipped=2)` was read as unqualified success twice on
#: harmonic-forge#644. The skip is legitimate — the live probe spends a real
#: Codex API call — but the RESULT must not look like the offline checks
#: covered what only the live one can. Printed unconditionally so it appears
#: above the `OK (skipped=N)` line rather than being something the reader has
#: to already know.
_LIVE_PROBE_ENABLED = os.environ.get("CODEX_LIVE_TRUST_PROBE") == "1"
if not _LIVE_PROBE_ENABLED:
    print(
        "NOTE: the LIVE hook-firing probe is SKIPPED (CODEX_LIVE_TRUST_PROBE != 1).\n"
        "      The offline checks below prove trust entries EXIST per handler slot.\n"
        "      They do NOT prove any hook actually fires — only the live probe does.\n"
        "      A green result here is not evidence that trust is in place at runtime.",
        file=sys.stderr,
    )


@unittest.skipUnless(
    _LIVE_PROBE_ENABLED,
    "live probe costs a Codex API call; set CODEX_LIVE_TRUST_PROBE=1 to run",
)
class LiveFiresProof(unittest.TestCase):
    """The real check. Everything above is a proxy.

    Opt-in because it spends an API call, not because it is optional: run it
    whenever a `.codex/hooks.json` changes, since editing one invalidates its
    trust hash and the failure is silent.
    """

    def _probe(self, cwd: Path, prompt: str, env_extra: dict | None = None) -> str:
        env = dict(os.environ)
        env.update(env_extra or {})
        proc = subprocess.run(
            ["codex", "exec", "--sandbox", "read-only", "--json", prompt],
            cwd=str(cwd), capture_output=True, text=True, stdin=subprocess.DEVNULL,
            timeout=300, env=env,
        )
        return proc.stdout

    def _assert_denied(self, out: str, marker: str, label: str) -> None:
        """Deny marker present AND no confounder — both, or the probe is not
        distinguishing anything."""
        for confounder in _CONFOUNDERS:
            self.assertNotIn(
                confounder, out,
                f"{label}: command failed on {confounder!r}, not on a hook deny — "
                f"this probe cannot tell denial from an unrelated failure",
            )
        self.assertIn("hook", out.lower(), f"{label}: no hook block reported")
        self.assertIn(marker, out, f"{label}: probe did not reach its own hook")

    def test_each_root_checkout_actually_denies_its_own_probe(self):
        for name, (prompt, marker, env_extra) in _PROBES.items():
            root = Path.home() / "Harmonic_Projects" / name
            if not (root / ".codex" / "hooks.json").exists():
                continue
            with self.subTest(repo=name):
                self._assert_denied(self._probe(root, prompt, env_extra), marker, name)

    def test_a_linked_worktree_inherits_its_root_checkouts_trust(self):
        """harmonic-forge#455 AC2, as a test: a linked worktree denies using the
        ROOT checkout's trust, holding no entry of its own."""
        lane = Path.home() / "Harmonic_Projects" / "harmonic-forge-lane2"
        if not lane.exists():
            self.skipTest("harmonic-forge-lane2 not present")
        self.assertTrue(is_linked_worktree(lane))
        prompt, marker, env_extra = _PROBES["harmonic-forge"]
        self._assert_denied(self._probe(lane, prompt, env_extra), marker,
                            "harmonic-forge-lane2")
        self.assertNotIn(str(lane / ".codex" / "hooks.json"), trusted_key_paths(),
                         "the worktree minted its own entry — the redirect did not apply")


if __name__ == "__main__":
    unittest.main()
