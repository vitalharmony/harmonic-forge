#!/usr/bin/env python3
"""Tests for compaction_marker.py (harmonic-forge#446).

The payload fixture is a **real captured `SessionStart` record** with
`source: "compact"`, taken from a live compaction and redacted, not hand-written
— a hand-rolled fixture would keep passing while the real record shape drifted,
which is the failure this issue's AC1 called out by name.
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
import tempfile
import unittest
from enum import Enum
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import compaction_marker as cm  # noqa: E402

_FIXTURE = Path(__file__).resolve().parent / "testdata" / "sessionstart_compact.json"


def load_fixture() -> dict:
    return json.loads(_FIXTURE.read_text(encoding="utf-8"))


class Fixture(unittest.TestCase):
    def test_fixture_is_a_compact_sourced_sessionstart(self):
        payload = load_fixture()
        self.assertEqual(payload["hook_event_name"], "SessionStart")
        self.assertEqual(payload["source"], "compact")
        self.assertIn("session_id", payload)
        self.assertIn("cwd", payload)


class Gating(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.object(cm, "MARKER_DIR", Path(self.tmp.name) / "markers")
        patcher.start(); self.addCleanup(patcher.stop)

    def test_non_compact_source_is_a_no_op(self):
        for source in ("startup", "resume", "clear", "fork"):
            with self.subTest(source=source):
                payload = load_fixture() | {"source": source}
                self.assertEqual(cm.handle(payload, {"LANE": "2"}), {})

    def test_compact_source_injects_and_writes(self):
        result = cm.handle(load_fixture(), {"LANE": "2"})
        self.assertEqual(result["hookSpecificOutput"]["hookEventName"], "SessionStart")
        self.assertTrue(result["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(len(list(cm.MARKER_DIR.iterdir())), 1)


class Payload(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.object(cm, "MARKER_DIR", Path(self.tmp.name) / "markers")
        patcher.start(); self.addCleanup(patcher.stop)

    def _context(self, env) -> str:
        return cm.handle(load_fixture(), env)["hookSpecificOutput"]["additionalContext"]

    def _context_in(self, env, cwd: str) -> str:
        """Same, with the session's cwd overridden (harmonic-forge#464).

        `agent-foundation.md` routes on the repo, not the lane, so a test that
        cannot vary cwd cannot assert that routing at all.
        """
        payload = load_fixture()
        payload["cwd"] = cwd
        return cm.handle(payload, env)["hookSpecificOutput"]["additionalContext"]

    def test_states_the_two_tier_corpus_split(self):
        """The correction that mattered most: saying only "your directives are
        back" would suppress the recovery action, because the auto-loaded
        surface is not the protocol corpus."""
        text = self._context({"LANE": "2"})
        self.assertIn("re-loaded automatically", text)
        self.assertIn("was NOT", text)
        self.assertIn("3-lane-protocol.md", text)
        self.assertIn("universal-agent.md", text)

    def test_contains_no_rule_content(self):
        """Paths only. Shipping the corpus would spend the context budget this
        issue exists to protect."""
        text = self._context({"LANE": "2"})
        for phrase in ("Lane 2 never", "must never", "categorically",
                       "no scope creep", "Ambiguity Gate"):
            self.assertNotIn(phrase, text)
        self.assertLess(len(text), 900, "payload is drifting toward rule content")

    def test_lane_1_and_3_get_their_own_extra_file(self):
        self.assertIn("universal-lane1.md", self._context({"LANE": "1"}))
        self.assertIn("testing-gate.md", self._context({"LANE": "3"}))
        two = self._context({"LANE": "2"})
        self.assertNotIn("universal-lane1.md", two)
        self.assertNotIn("testing-gate.md", two)

    # --- harmonic-forge#464: the two entries the duplicated list dropped ---

    def test_universal_claude_is_named_to_every_lane(self):
        """It was in the docstring's corpus list and in neither tuple, so it
        reached nobody. Unconditional by decision: Lane 2 and Lane 3 accept
        either Claude Code or Codex, and one ignorable path costs less than the
        silent drop this issue documents."""
        for env in ({"LANE": "1"}, {"LANE": "2"}, {"LANE": "3"}, {}):
            with self.subTest(env=env):
                self.assertIn("universal-claude.md", self._context(env))

    def test_agent_foundation_routes_on_the_repo_not_the_lane(self):
        """The finding that changed this issue's shape: the corpus routes on two
        axes. Both directions are asserted — testing only the positive would
        pass just as well if the path were unconditional, which is the bug in
        the other direction."""
        inside = self._context_in({"LANE": "2"}, cm.forge_root() + "/tools/hooks")
        self.assertIn("agent-foundation.md", inside)

        outside = self._context_in({"LANE": "2"}, "/home/mmangus/Harmonic_Projects/HRSE2")
        self.assertNotIn("agent-foundation.md", outside)

    def test_the_forge_root_itself_counts_as_inside(self):
        """A session whose cwd IS the checkout, not a subdirectory of it."""
        self.assertIn(
            "agent-foundation.md", self._context_in({"LANE": "2"}, cm.forge_root()),
        )

    def test_repo_scoping_survives_a_symlinked_or_relative_cwd(self):
        """Both sides are resolved before comparing. An unresolved compare would
        drop the file for exactly the sessions that need it — this issue's own
        failure shape, reintroduced."""
        awkward = cm.forge_root() + "/tools/../tools/hooks"
        self.assertIn("agent-foundation.md", self._context_in({"LANE": "2"}, awkward))

    def test_an_unresolvable_cwd_does_not_raise(self):
        """A deleted or unreadable cwd costs one unnamed path, never the
        session's recovery note."""
        text = self._context_in({"LANE": "2"}, "/nonexistent/\x00bad")
        self.assertIn("3-lane-protocol.md", text)

    def test_lane_unset_says_unknown_and_does_not_crash(self):
        for env in ({}, {"LANE": ""}, {"LANE": "   "}):
            with self.subTest(env=env):
                text = self._context(env)
                self.assertIn("LANE=unknown", text)
                self.assertNotIn("LANE=\n", text)

    def test_cwd_comes_from_the_payload_not_a_template(self):
        """Real sessions run in `/tmp/<repo>-<n>-impl`, not `<repo>-lane2`."""
        self.assertIn("/tmp/hrse2-1234-impl", self._context({"LANE": "2"}))


class Marker(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name) / "markers"
        patcher = mock.patch.object(cm, "MARKER_DIR", self.dir)
        patcher.start(); self.addCleanup(patcher.stop)

    def test_marker_contents(self):
        cm.handle(load_fixture(), {"LANE": "2"})
        data = json.loads((self.dir / "00000000-0000-4000-8000-000000000000.json").read_text())
        self.assertEqual(data["lane"], "2")
        self.assertEqual(data["cwd"], "/tmp/hrse2-1234-impl")
        self.assertEqual(data["source"], "compact")
        self.assertIn("compacted_at", data)

    def test_two_sessions_do_not_collide(self):
        cm.handle(load_fixture(), {"LANE": "2"})
        cm.handle(load_fixture() | {"session_id": "11111111"}, {"LANE": "3"})
        self.assertEqual(len(list(self.dir.iterdir())), 2)

    def test_no_temp_files_left_behind(self):
        cm.handle(load_fixture(), {"LANE": "2"})
        self.assertEqual([p.name for p in self.dir.iterdir() if p.suffix == ".tmp"], [])

    def test_prune_uses_compacted_at_not_mtime(self):
        """Pruning on mtime would delete a live long-running session's marker,
        making #451's gate silently conclude "no compaction" — a false negative
        in a guard."""
        self.dir.mkdir(parents=True, exist_ok=True)
        stale = self.dir / "stale.json"
        fresh = self.dir / "fresh.json"
        now = 1_000_000_000.0
        from datetime import datetime, timezone
        old_iso = datetime.fromtimestamp(now - cm.TTL_SECONDS - 60, tz=timezone.utc).isoformat()
        new_iso = datetime.fromtimestamp(now - 60, tz=timezone.utc).isoformat()
        stale.write_text(json.dumps({"compacted_at": old_iso}))
        fresh.write_text(json.dumps({"compacted_at": new_iso}))
        # both files have identical, brand-new mtimes — only the recorded
        # timestamp distinguishes them
        cm.prune_markers(now)
        self.assertFalse(stale.exists())
        self.assertTrue(fresh.exists())

    def test_prune_tolerates_unreadable_entries(self):
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "corrupt.json").write_text("{not json")
        (self.dir / "no-field.json").write_text("{}")
        survivor = self.dir / "keep.json"
        from datetime import datetime, timezone
        survivor.write_text(json.dumps(
            {"compacted_at": datetime.now(tz=timezone.utc).isoformat()}))
        cm.prune_markers(1_000_000_000.0)  # must not raise
        self.assertTrue(survivor.exists())

    def test_prune_survives_a_missing_directory(self):
        cm.prune_markers(1_000_000_000.0)  # never created; must not raise

    def test_unwritable_marker_dir_still_injects(self):
        """The injection is the product; the marker is a signal for #451. A
        session that cannot write must still get its recovery note."""
        with mock.patch.object(cm, "write_marker", side_effect=OSError("read-only fs")):
            result = cm.handle(load_fixture(), {"LANE": "2"})
        self.assertIn("additionalContext", result["hookSpecificOutput"])


class Cli(unittest.TestCase):
    def _run(self, stdin: str) -> dict:
        proc = subprocess.run(
            [sys.executable, str(Path(__file__).resolve().parent / "compaction_marker.py")],
            input=stdin, capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)

    def test_malformed_payload_is_visible_not_silent(self):
        """A bare `{}` here is indistinguishable from "no compaction"."""
        out = self._run("not json at all")
        self.assertIn("systemMessage", out)
        self.assertIn("malformed", out["systemMessage"])

    def test_non_object_payload_is_visible(self):
        out = self._run("[1, 2, 3]")
        self.assertIn("systemMessage", out)

    def test_non_compact_payload_prints_empty_object(self):
        out = self._run(json.dumps(load_fixture() | {"source": "startup"}))
        self.assertEqual(out, {})


class CorpusDeclaration(unittest.TestCase):
    """harmonic-forge#464 — the declaration is the only enumeration, and a
    malformed one fails visibly.

    Every case here builds a **synthetic** `CorpusFile`/`CORPUS`. The real
    declaration is never mutated: a test that removed an entry to prove the
    deriver notices would leave the hook broken if the process died between the
    removal and the restore, and this hook runs when a session is least able to
    cope with that.
    """

    def test_the_module_holds_exactly_one_enumeration_of_corpus_paths(self):
        """AC1, and the actual defect: the docstring listed six corpus files and
        the tuples named four. Two hand-maintained copies of one fact drift —
        that is what a second copy does."""
        source = (Path(cm.__file__)).read_text(encoding="utf-8")
        docstring = ast.get_docstring(ast.parse(source)) or ""

        # Basenames, not full paths. Asserting the full path would pass on a
        # prefix technicality: the docstring could say `universal-claude.md`
        # while CORPUS holds `rules/universal-claude.md`, and the two lists
        # would be free to drift exactly as they did.
        for entry in cm.CORPUS:
            basename = entry.path.rsplit("/", 1)[-1]
            with self.subTest(path=entry.path):
                self.assertNotIn(
                    basename, docstring,
                    f"{basename} is named in both CORPUS and the module "
                    f"docstring — the duplication this issue removed",
                )

        # And no revived tuple: the derived values must not come back as
        # literals somebody edits alongside CORPUS.
        self.assertFalse(hasattr(cm, "_ALWAYS"), "_ALWAYS is a second list")
        self.assertFalse(hasattr(cm, "_BY_LANE"), "_BY_LANE is a second list")

    def test_every_real_entry_routes_somewhere_or_states_why_not(self):
        """The two dropped files were dropped by being in no branch at all. An
        entry must reach a lane, a repo, or carry a stated exclusion."""
        reachable = {
            cm.Routing.ALWAYS, cm.Routing.BY_LANE, cm.Routing.FORGE_REPO,
        }
        for entry in cm.CORPUS:
            with self.subTest(path=entry.path):
                if entry.routing in reachable:
                    continue
                self.assertIs(entry.routing, cm.Routing.EXCLUDED)
                self.assertTrue(entry.reason, "an exclusion must state its reason")

    def test_an_unhandled_routing_value_fails_loudly(self):
        """The drift case, over a synthetic declaration.

        Silently dropping an unrouted entry is this issue itself; silently
        including it would make the EXCLUDED tier meaningless. So it raises —
        and `handle()` turns that into a visible message rather than a crash
        (asserted below)."""
        class _Rogue(str, Enum):
            NOWHERE = "nowhere"

        rogue = cm.CorpusFile.__new__(cm.CorpusFile)
        object.__setattr__(rogue, "path", "rules/ghost.md")
        object.__setattr__(rogue, "routing", _Rogue.NOWHERE)
        object.__setattr__(rogue, "lane", None)
        object.__setattr__(rogue, "reason", None)

        with mock.patch.object(cm, "CORPUS", (rogue,)):
            with self.assertRaises(ValueError) as caught:
                cm.corpus_for("2", "/tmp")
        self.assertIn("rules/ghost.md", str(caught.exception))

    def test_a_bad_declaration_degrades_visibly_and_still_injects(self):
        """A malformed CORPUS must not cost the session its recovery note, and
        must not read as "nothing to re-read" — which is the shape this whole
        issue is about."""
        class _Rogue(str, Enum):
            NOWHERE = "nowhere"

        rogue = cm.CorpusFile.__new__(cm.CorpusFile)
        object.__setattr__(rogue, "path", "rules/ghost.md")
        object.__setattr__(rogue, "routing", _Rogue.NOWHERE)
        object.__setattr__(rogue, "lane", None)
        object.__setattr__(rogue, "reason", None)

        with mock.patch.object(cm, "CORPUS", (rogue,)):
            with tempfile.TemporaryDirectory() as tmp:
                with mock.patch.object(cm, "MARKER_DIR", Path(tmp) / "m"):
                    out = cm.handle(load_fixture(), {"LANE": "2"})

        self.assertIn("systemMessage", out)
        self.assertIn("corpus declaration is invalid", out["systemMessage"])
        context = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("could not be built", context)
        self.assertIn("re-read the issue thread", context)

    def test_by_lane_without_a_lane_is_refused_at_construction(self):
        """A routing that needs a lane and has none would match no lane and
        reach nobody — silently, which is the defect."""
        with self.assertRaises(ValueError):
            cm.CorpusFile("rules/x.md", cm.Routing.BY_LANE)

    def test_an_exclusion_without_a_reason_is_refused(self):
        """AC: a deliberately-not-reinjected file states why in source, rather
        than being an absence somebody has to infer."""
        with self.assertRaises(ValueError):
            cm.CorpusFile("rules/x.md", cm.Routing.EXCLUDED)
        # With a reason it is accepted, and routes to nobody.
        excluded = cm.CorpusFile("rules/x.md", cm.Routing.EXCLUDED, reason="covered elsewhere")
        with mock.patch.object(cm, "CORPUS", (excluded,)):
            self.assertEqual(cm.corpus_for("2", cm.forge_root()), [])


class Wiring(unittest.TestCase):
    """harmonic-forge#367's defect was wiring one repo and not the other."""

    #: This repo's own tracked settings file, resolved from the test's location
    #: rather than from $HOME — so this half is deterministic and travels with
    #: the diff instead of depending on what is deployed on the machine.
    _OWN = Path(__file__).resolve().parents[2] / ".claude" / "settings.json"

    #: The sibling repo's file, which lives in a different repository and can
    #: only be checked where it is present.
    _SIBLING = Path.home() / "Harmonic_Projects" / "HRSE2" / ".claude" / "settings.json"

    @staticmethod
    def _is_wired(path: Path) -> bool:
        data = json.loads(path.read_text(encoding="utf-8"))
        entries = (data.get("hooks") or {}).get("SessionStart") or []
        return (any(e.get("matcher") == "compact" for e in entries)
                and "compaction_marker.py" in json.dumps(entries))

    def test_wired_in_this_repo(self):
        """Deterministic: asserts the tracked file in this checkout."""
        self.assertTrue(self._OWN.exists(), f"missing {self._OWN}")
        self.assertTrue(self._is_wired(self._OWN),
                        f"SessionStart/compact not wired in {self._OWN}")

    def test_wired_in_the_sibling_repo(self):
        """harmonic-forge#367's defect was wiring one repo and not the other, so
        this is asserted rather than assumed.

        It is RED until hrse's companion commit lands — deliberately. A softer
        check that passed while the sibling was unwired would be the silent-skip
        shape this project has already paid for twice; a red test naming the
        file is the honest state of a two-repo change mid-landing.
        """
        if not self._SIBLING.exists():
            self.skipTest(f"sibling repo not present at {self._SIBLING}")
        self.assertTrue(
            self._is_wired(self._SIBLING),
            f"SessionStart/compact not wired in {self._SIBLING} — wiring one repo "
            f"and not the other is harmonic-forge#367's defect. Land the hrse "
            f"companion commit.",
        )


if __name__ == "__main__":
    unittest.main()
