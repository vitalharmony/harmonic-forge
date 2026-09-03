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

import os
import re
import subprocess
import sys
import unittest
from pathlib import Path

_CONFIG = Path.home() / ".codex" / "config.toml"

#: Project roots to inspect. A path that does not exist is skipped, not failed
#: — this suite runs on machines that hold a subset of these.
_CANDIDATE_ROOTS = (
    Path.home() / "Harmonic_Projects" / "harmonic-forge",
    Path.home() / "Harmonic_Projects" / "HRSE2",
    Path.home() / "Harmonic_Projects" / "cymagraph-infra",
)

_STATE_KEY = re.compile(r'^\[hooks\.state\."([^"]+)"\]', re.M)

#: The probe command must be one THAT REPO'S OWN hooks actually deny. This bit
#: hard during harmonic-forge#456: `sudo` is denied by HRSE2's
#: `gate_codex_tool.py`, but harmonic-forge wires no such hook, so a `sudo`
#: probe there showed the command running whether or not hooks fired — it could
#: never have distinguished the two, and was briefly read as proof they did not.
#:
#: Per repo, a command that repo's hooks deny, and that is harmless if allowed.
#: The `gh` POST targets a nonexistent issue, so an unblocked call 404s rather
#: than posting anything.
_PROBES = {
    "HRSE2": (
        "Run this exact shell command and report what happened: sudo id",
        "sudo",  # gate_codex_tool.py, unconditional deny
    ),
    "harmonic-forge": (
        "Run this exact shell command and report exactly what happened, including "
        "any hook or permission message: gh api "
        "repos/vitalharmony/harmonic-forge/issues/999999/comments -X POST -f body=probe",
        "raw GitHub issue posting",  # block_lane2_status_claims.py
    ),
}


def is_linked_worktree(path: Path) -> bool:
    """A linked worktree's `.git` is a file containing `gitdir:`; a root
    checkout's is a directory."""
    return (path / ".git").is_file()


def trusted_key_paths() -> set[str]:
    """The `key_source` path of every `hooks.state` entry, or an empty set when
    there is no local Codex config."""
    if not _CONFIG.exists():
        return set()
    keys = _STATE_KEY.findall(_CONFIG.read_text(encoding="utf-8"))
    # key is "<hooks.json path>:<event>:<i>:<j>" — strip the three trailing fields
    return {key.rsplit(":", 3)[0] for key in keys}


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
            if str(hooks) not in trusted:
                missing.append(str(hooks))
        self.assertEqual(
            missing, [],
            "root checkout(s) with a hooks.json but no hooks.state entry — their "
            "hooks are silently skipped (harmonic-forge#456). Mint trust with one "
            "interactive `codex` pass in that checkout.",
        )

    def test_worktree_hooks_files_are_not_required_to_be_trusted(self):
        """The inverse assertion, stated so nobody 'fixes' it later: a linked
        worktree's own hooks.json is never read, so an absent entry for it is
        correct, not a gap."""
        lane = Path.home() / "Harmonic_Projects" / "HRSE2-lane2"
        if not (lane / ".codex" / "hooks.json").exists():
            self.skipTest("HRSE2-lane2 has no hooks.json")
        self.assertTrue(is_linked_worktree(lane))
        # No assertion on trust: its presence or absence is equally fine.


@unittest.skipUnless(
    os.environ.get("CODEX_LIVE_TRUST_PROBE") == "1",
    "live probe costs a Codex API call; set CODEX_LIVE_TRUST_PROBE=1 to run",
)
class LiveFiresProof(unittest.TestCase):
    """The real check. Everything above is a proxy.

    Opt-in because it spends an API call, not because it is optional: run it
    whenever a `.codex/hooks.json` changes, since editing one invalidates its
    trust hash and the failure is silent.
    """

    def _probe(self, cwd: Path, prompt: str) -> str:
        proc = subprocess.run(
            ["codex", "exec", "--sandbox", "read-only", "--json", prompt],
            cwd=str(cwd), capture_output=True, text=True, stdin=subprocess.DEVNULL,
            timeout=300,
        )
        return proc.stdout

    def test_each_root_checkout_actually_denies_its_own_probe(self):
        for name, (prompt, marker) in _PROBES.items():
            root = Path.home() / "Harmonic_Projects" / name
            if not (root / ".codex" / "hooks.json").exists():
                continue
            with self.subTest(repo=name):
                out = self._probe(root, prompt)
                self.assertIn("hook", out.lower(),
                              f"{name}: no hook block reported — the deny did not fire")
                self.assertIn(marker, out, f"{name}: probe did not reach its own hook")

    def test_a_linked_worktree_inherits_its_root_checkouts_trust(self):
        """harmonic-forge#455 AC2, as a test: a linked worktree denies using the
        ROOT checkout's trust, holding no entry of its own."""
        lane = Path.home() / "Harmonic_Projects" / "harmonic-forge-lane2"
        if not lane.exists():
            self.skipTest("harmonic-forge-lane2 not present")
        self.assertTrue(is_linked_worktree(lane))
        prompt, marker = _PROBES["harmonic-forge"]
        out = self._probe(lane, prompt)
        self.assertIn("hook", out.lower())
        self.assertIn(marker, out)
        self.assertNotIn(str(lane / ".codex" / "hooks.json"), trusted_key_paths(),
                         "the worktree minted its own entry — the redirect did not apply")


if __name__ == "__main__":
    unittest.main()
