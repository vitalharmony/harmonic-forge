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



class CompactionCountTests(unittest.TestCase):
    """The `compactions` counter (harmonic-forge#497)."""

    def _compact(self, tmp: Path, session: str = "s") -> dict:
        import compaction_marker as cm
        with mock.patch.object(cm, "MARKER_DIR", tmp):
            cm.handle({"session_id": session, "source": "compact", "cwd": "/x",
                       "transcript_path": ""}, {})
            return cm.read_marker(session) or {}

    def test_a_first_compaction_counts_one(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(self._compact(Path(tmp)).get("compactions"), 1)

    def test_the_count_increments_across_compactions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            counts = [self._compact(path).get("compactions") for _ in range(4)]
        self.assertEqual(counts, [1, 2, 3, 4])

    def test_a_pre_counter_marker_seeds_at_one_not_zero(self) -> None:
        """A marker with no `compactions` key was written before the field
        existed — but its existence proves a prior compaction. Seeding at 0
        counted that session's SECOND compaction as its first, suppressing the
        restart `!` for exactly the long-lived sessions R-0339 targets."""
        import compaction_marker as cm
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            (path / "s.json").write_text(json.dumps(
                {"compacted_at": "2026-09-06T00:00:00+00:00", "source": "compact"}),
                encoding="utf-8")
            self.assertEqual(self._compact(path).get("compactions"), 2)

    def test_a_corrupt_count_still_records_this_compaction(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            (path / "s.json").write_text(
                json.dumps({"compacted_at": "2026-09-06T00:00:00+00:00",
                            "source": "compact", "compactions": "lots"}),
                encoding="utf-8")
            self.assertEqual(self._compact(path).get("compactions"), 2)

if __name__ == "__main__":
    unittest.main()


class LaneCrossCheck(unittest.TestCase):
    """harmonic-forge#479 — `LANE` is wrong for every daemon-spawned
    background job on a machine whose daemon was first started from a Lane 1
    shell.

    The fixtures below are the five markers that existed on disk when this
    was written, reproduced as `(env, cwd)` pairs. Four are real recorded
    states; the fifth is the shape they warn about.
    """

    def test_the_live_incident_is_caught(self) -> None:
        """Marker `ab5817ae`: `"lane": "1"` recorded with
        `"cwd": ".../HRSE2-lane2"`. The injection built from it told a Lane 2
        session "You are LANE=1", and that session stopped to ask the
        operator whether it was Lane 1 — twice."""
        lane, source = cm.resolve_lane(
            {"LANE": "1"}, "/home/mmangus/Harmonic_Projects/HRSE2-lane2")
        self.assertEqual((lane, source), ("2", "cwd_override"))

    def test_a_matching_pair_is_left_alone(self) -> None:
        """Marker `4e7d1fbe` — an interactive Lane 2 session, correct."""
        self.assertEqual(
            cm.resolve_lane(
                {"LANE": "2"}, "/home/mmangus/Harmonic_Projects/HRSE2-lane2"),
            ("2", "env"))

    def test_lane_1s_unsuffixed_checkout_makes_no_claim(self) -> None:
        """Marker `a58a3627`. `HRSE2` is Lane 1's own convention; an absent
        suffix is not evidence of anything and must never read as a
        contradiction."""
        self.assertEqual(
            cm.resolve_lane(
                {"LANE": "1"}, "/home/mmangus/Harmonic_Projects/HRSE2"),
            ("1", "env"))

    def test_an_unset_lane_with_no_claim_stays_unknown(self) -> None:
        """Marker `d89c456b`."""
        self.assertEqual(
            cm.resolve_lane(
                {}, "/home/mmangus/Harmonic_Projects/HRSE2"),
            ("unknown", "env"))

    def test_an_unset_lane_is_filled_in_by_the_cwd(self) -> None:
        """An unset var has nothing to contradict, so this is strictly more
        information at no risk — and it is `cwd`, not `cwd_override`, because
        nothing was wrong."""
        self.assertEqual(
            cm.resolve_lane(
                {}, "/home/mmangus/Harmonic_Projects/HRSE2-lane2"),
            ("2", "cwd"))


class LaneFromCwdMatchesAPathComponent(unittest.TestCase):
    """The correction to this issue's own Implementation Spec, which said to
    match the cwd's BASENAME."""

    def test_a_subdirectory_of_a_lane_worktree_still_resolves(self) -> None:
        """Marker `e0cc7963`'s cwd is `HRSE2-lane3/backend`. A basename-only
        match returns `None` there — silently indistinguishable from Lane 1's
        legitimately unsuffixed checkout. That was 1 of the 5 markers on
        disk, i.e. 20% of the available evidence, skipped."""
        self.assertEqual(
            cm.lane_from_cwd(
                "/home/mmangus/Harmonic_Projects/HRSE2-lane3/backend"), "3")

    def test_the_lane_3_case_the_original_framing_missed(self) -> None:
        """The blast radius is asymmetric. Only `universal-lane1.md` (lane 1)
        and `testing-gate.md` (lane 3) are BY_LANE, and Lane 2 has neither —
        so a mislabelled Lane 2 session loses nothing, while a mislabelled
        Lane 3 job loses the gate's own rules."""
        lane, source = cm.resolve_lane(
            {"LANE": "1"}, "/home/mmangus/Harmonic_Projects/HRSE2-lane3/backend")
        self.assertEqual((lane, source), ("3", "cwd_override"))
        # `corpus_for` returns absolute paths; compare on the suffix.
        gate_in = lambda lane: any(  # noqa: E731
            p.endswith("rules/testing-gate.md") for p in cm.corpus_for(lane))
        self.assertTrue(gate_in("3"), "Lane 3 must be told to re-read the gate's own rules")
        self.assertFalse(gate_in("1"), "and Lane 1 must not be — that is what makes the mislabel costly")

    def test_no_real_worktree_name_false_matches(self) -> None:
        """Every worktree basename across both repos, checked when this was
        written. A per-issue worktree or a non-lane suffix must make no
        claim, or the fallback would override a correct `LANE`."""
        for name in ("hrse2-1443-impl", "harmonic-forge-f326", "hrse2-733p",
                     "harmonic-forge-release", "forge-432-impl", "HRSE2",
                     "harmonic-forge", "hrse2-relnotes"):
            with self.subTest(name=name):
                self.assertIsNone(
                    cm.lane_from_cwd(f"/home/mmangus/x/{name}"))

    def test_the_real_lane_worktrees_all_match(self) -> None:
        for name, lane in (("HRSE2-lane2", "2"), ("HRSE2-lane3", "3"),
                           ("harmonic-forge-lane2", "2"),
                           ("harmonic-forge-lane3", "3")):
            with self.subTest(name=name):
                self.assertEqual(
                    cm.lane_from_cwd(f"/home/mmangus/x/{name}"), lane)

    def test_the_match_is_case_insensitive(self) -> None:
        self.assertEqual(cm.lane_from_cwd("/x/HRSE2-LANE2"), "2")


class LaneSourceReachesTheMarkerAndTheInjection(unittest.TestCase):
    """Fixing only the marker would leave `build_context()` still printing
    "You are LANE=1" into a Lane 2 session — which is the half of the
    incident the operator actually saw."""

    def _handle(self, env, cwd, tmp):
        with mock.patch.object(cm, "MARKER_DIR", Path(tmp)):
            out = cm.handle(
                {"source": "compact", "session_id": "s1", "cwd": cwd}, env)
            marker = json.loads((Path(tmp) / "s1.json").read_text())
        return out, marker

    def test_the_marker_records_the_corrected_lane_and_its_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, marker = self._handle(
                {"LANE": "1"}, "/home/mmangus/Harmonic_Projects/HRSE2-lane2", tmp)
        self.assertEqual(marker["lane"], "2")
        self.assertEqual(marker["lane_source"], "cwd_override")

    def test_the_injected_text_carries_the_corrected_lane_too(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out, _ = self._handle(
                {"LANE": "1"}, "/home/mmangus/Harmonic_Projects/HRSE2-lane2", tmp)
        blob = json.dumps(out)
        self.assertIn("LANE=2", blob)
        self.assertNotIn("LANE=1", blob)

    def test_a_correct_session_records_env_as_its_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, marker = self._handle(
                {"LANE": "2"}, "/home/mmangus/Harmonic_Projects/HRSE2-lane2", tmp)
        self.assertEqual(marker["lane_source"], "env")


FIXTURES = Path(__file__).resolve().parent / "testdata" / "compaction_markers"


class CorpusReloadDetector(unittest.TestCase):
    """harmonic-forge#480 — three conditions, each earned from a real false
    positive rather than imagined."""

    def reload(self, command: str) -> bool:
        return cm.is_corpus_reload("Bash", {"command": command})

    def test_a_read_verb_with_a_corpus_path_counts(self) -> None:
        self.assertTrue(self.reload(
            "cd /home/mmangus/harmonic-forge && sed -n '1,240p' rules/universal-lane1.md"))
        self.assertTrue(self.reload(
            "cd /home/mmangus/harmonic-forge && cat rules/universal-claude.md"))

    def test_wc_does_not_count(self) -> None:
        """The near-miss the verb list exists to decide about. `wc` reads the
        file and returns a COUNT, never content — marking "reloaded" for
        having measured the corpus is the opposite of what this detects."""
        self.assertFalse(self.reload(
            "cd /home/mmangus/harmonic-forge && wc -l 3-lane-protocol.md rules/universal-agent.md"))
        self.assertNotIn("wc", cm.READ_VERBS)

    def test_a_heredoc_disqualifies_the_whole_command(self) -> None:
        """A Python heredoc that WRITES a file frequently contains a corpus
        path in the text being written. "The command mentions a corpus path"
        matches every one of them."""
        self.assertFalse(self.reload(
            'python3 - <<PY\nprint("rules/testing-gate.md")\nPY'))

    def test_cat_as_a_writer_does_not_count(self) -> None:
        """`cat` is on the read list and is a writer here. Real shape, from
        the motivating transcript's action 29."""
        self.assertFalse(self.reload(
            "cd /tmp/x && cat >> tests.py <<EOF\nsee 3-lane-protocol.md\nEOF"))

    def test_a_redirected_segment_does_not_count(self) -> None:
        self.assertFalse(self.reload("cat rules/universal-agent.md > /tmp/copy"))

    def test_a_corpus_path_merely_mentioned_does_not_count(self) -> None:
        """The condition that separates a read from a search."""
        self.assertFalse(self.reload('grep -rn "3-lane-protocol.md" .'))
        self.assertFalse(self.reload("ls -la rules/testing-gate.md"))

    def test_the_read_tool_needs_none_of_that(self) -> None:
        self.assertTrue(cm.is_corpus_reload(
            "Read", {"file_path": "/home/mmangus/harmonic-forge/rules/testing-gate.md"}))
        self.assertFalse(cm.is_corpus_reload("Read", {"file_path": "/x/other.md"}))

    def test_a_write_tool_never_counts_however_it_is_shaped(self) -> None:
        for tool in ("Edit", "Write", "NotebookEdit"):
            with self.subTest(tool=tool):
                self.assertFalse(cm.is_corpus_reload(
                    tool, {"file_path": "/home/mmangus/harmonic-forge/3-lane-protocol.md"}))

    def test_a_segment_after_cd_is_judged_on_its_own_verb(self) -> None:
        self.assertFalse(self.reload("cd /home/mmangus/harmonic-forge"))


class DetectorAgainstFrozenFixtures(unittest.TestCase):
    """The five real markers, and the tool calls a marker alone cannot carry.

    Frozen on 2026-09-06 — `/tmp` plus a TTL prune plus rewrite-on-recompaction
    means a reference-in-place is unusable within days, which is how this
    issue's own predecessor lost two of its three cited rows.
    """

    def calls(self, session_prefix: str) -> dict:
        hits = list(FIXTURES.glob(f"{session_prefix}*.tool_calls.json"))
        self.assertEqual(len(hits), 1, f"expected one fixture for {session_prefix}")
        return json.loads(hits[0].read_text())

    def verdict(self, session_prefix: str):
        for record in self.calls(session_prefix)["records"]:
            if cm.is_corpus_reload(record["tool_name"], record["tool_input"]):
                return record["action"]
        return None

    def test_all_five_markers_are_vendored(self) -> None:
        self.assertEqual(len(list(FIXTURES.glob("*.json"))), 10,
                         "five markers plus five tool-call captures")

    def test_the_planning_session_reloaded_at_action_3_not_2(self) -> None:
        """`ab5817ae` is the session that planned this issue. Its own filing
        said action 2; action 2 was `wc -l`, which returns no content. The
        first real read is action 3."""
        self.assertEqual(self.verdict("ab5817ae"), 3)

    def test_the_other_four_did_not_reload(self) -> None:
        for prefix in ("4e7d1fbe", "a58a3627", "d89c456b", "e0cc7963"):
            with self.subTest(session=prefix):
                self.assertIsNone(self.verdict(prefix))

    def test_the_three_false_positive_shapes_are_all_present(self) -> None:
        """The rows worth vendoring. A fixture set without them would pass
        against a detector that only checks for a corpus path in the text."""
        records = self.calls("ab5817ae")["records"]
        rejected = [r for r in records
                    if not cm.is_corpus_reload(r["tool_name"], r["tool_input"])]
        self.assertEqual([r["action"] for r in rejected], [2, 20, 29, 38])


class NoteReloadIsUpdateOnlyAndSetOnce(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        patcher = mock.patch.object(cm, "MARKER_DIR", self.dir)
        patcher.start()
        self.addCleanup(patcher.stop)

    def marker(self, **extra):
        payload = {"compacted_at": "2026-09-05T07:55:34+00:00", "source": "compact",
                   "lane": "2", "cwd": "/x/HRSE2-lane2", **extra}
        (self.dir / "s1.json").write_text(json.dumps(payload))
        return payload

    def test_it_never_creates_a_marker(self) -> None:
        """A PreToolUse that could create one would manufacture a compaction
        that never happened, and harmonic-forge#451 would deny on it."""
        self.assertFalse(cm.note_reload("s1", "2026-09-05T08:00:00+00:00"))
        self.assertEqual(list(self.dir.glob("*.json")), [])

    def test_it_records_the_timestamp_on_an_existing_marker(self) -> None:
        self.marker()
        self.assertTrue(cm.note_reload("s1", "2026-09-05T08:00:00+00:00"))
        written = json.loads((self.dir / "s1.json").read_text())
        self.assertEqual(written["reloaded_at"], "2026-09-05T08:00:00+00:00")

    def test_it_preserves_every_other_field(self) -> None:
        original = self.marker()
        cm.note_reload("s1", "2026-09-05T08:00:00+00:00")
        written = json.loads((self.dir / "s1.json").read_text())
        for key, value in original.items():
            with self.subTest(key=key):
                self.assertEqual(written[key], value)

    def test_it_is_set_once(self) -> None:
        """The question is "has a reload happened since the boundary", so the
        first answers it. Rewriting would lose the original timestamp."""
        self.marker(reloaded_at="2026-09-05T08:00:00+00:00")
        self.assertFalse(cm.note_reload("s1", "2026-09-05T09:00:00+00:00"))
        self.assertEqual(
            json.loads((self.dir / "s1.json").read_text())["reloaded_at"],
            "2026-09-05T08:00:00+00:00")

    def test_a_corrupt_marker_is_not_overwritten(self) -> None:
        (self.dir / "s1.json").write_text("{not json")
        self.assertFalse(cm.note_reload("s1", "2026-09-05T08:00:00+00:00"))
        self.assertEqual((self.dir / "s1.json").read_text(), "{not json")


class HeredocAndRedirectAreSegmentScoped(unittest.TestCase):
    """The three shapes that fixed this predicate, found by mutation testing
    rather than by reading the code."""

    def reload(self, command: str) -> bool:
        return cm.is_corpus_reload("Bash", {"command": command})

    def test_the_motivating_false_positive_is_rejected_by_the_verb_rule_alone(self) -> None:
        """Actions 20 and 38: a Python heredoc whose BODY names a corpus
        path. `python3` is not a read verb, so this never needed a heredoc
        rule — which is why the first version's heredoc rule looked like it
        was doing work and was not."""
        self.assertFalse(self.reload(
            'cd /tmp/x && python3 - <<PY\np = "rules/testing-gate.md"\nPY'))

    def test_cat_reading_from_a_heredoc_is_rejected(self) -> None:
        """The shape the heredoc rule DOES earn: `cat` is a read verb and the
        corpus path is an argument, but it is heredoc content that was never
        read from disk."""
        self.assertFalse(self.reload(
            "echo x && cat <<EOF\nrules/universal-agent.md\nEOF"))

    def test_a_real_read_beside_an_unrelated_heredoc_still_counts(self) -> None:
        """The false NEGATIVE a whole-command heredoc rejection introduced.
        Scoping both disqualifiers per segment is what fixed it."""
        self.assertTrue(self.reload(
            "cat rules/universal-agent.md && python3 - <<PY\nprint(1)\nPY"))

    def test_a_real_read_beside_an_unrelated_redirect_still_counts(self) -> None:
        self.assertTrue(self.reload(
            "echo hi > /tmp/x && cat rules/universal-agent.md"))


class ReadToolPathMatchIsSuffixNotSubstring(unittest.TestCase):
    """A substring test would match a path that merely CONTAINS a corpus
    path — an editor backup, a diff dump, an archive of the corpus. Those
    are not the file, and reading one is not a reload."""

    def read(self, path: str) -> bool:
        return cm.is_corpus_reload("Read", {"file_path": path})

    def test_the_real_file_counts(self) -> None:
        self.assertTrue(self.read("/home/mmangus/harmonic-forge/rules/testing-gate.md"))

    def test_a_path_that_merely_contains_it_does_not(self) -> None:
        for path in ("/tmp/rules/testing-gate.md.bak",
                     "/tmp/rules/testing-gate.md.orig",
                     "/tmp/archive/rules/testing-gate.md/notes.txt"):
            with self.subTest(path=path):
                self.assertFalse(self.read(path))


class TriggerComesFromTheTranscriptNotThePayload(unittest.TestCase):
    """harmonic-forge#480 JDC3, and a correction to how the gate said to
    implement it.

    The FAIL directed reading `payload.get("trigger")` "since the SessionStart
    compaction event carries one". Measured across every transcript on this
    machine, `trigger` exists in exactly one place — `compactMetadata.trigger`
    on a `system` record inside the transcript — 80 `auto` and 5 `manual`. The
    captured real payload in `sessionstart_compact.json` carries none, so the
    instructed version would have been `None` on every real invocation.
    """

    def test_the_captured_real_payload_carries_no_trigger(self) -> None:
        """The measurement that makes this a correction rather than a
        preference. If a future harness version adds the field, this fails
        and the simpler implementation becomes available."""
        self.assertNotIn("trigger", load_fixture())

    def test_a_real_auto_record_is_read(self) -> None:
        self.assertEqual(cm.trigger_of(str(FIXTURES / "transcript_auto.jsonl")), "auto")

    def test_a_real_manual_record_is_read(self) -> None:
        self.assertEqual(cm.trigger_of(str(FIXTURES / "transcript_manual.jsonl")), "manual")

    def test_both_fixtures_are_verbatim_real_records(self) -> None:
        """Extracted from live transcripts, not synthesised — a hand-written
        `{"compactMetadata": {"trigger": "auto"}}` would pass a parser that
        the real record's shape breaks."""
        for name in ("transcript_auto.jsonl", "transcript_manual.jsonl"):
            with self.subTest(name=name):
                record = json.loads((FIXTURES / name).read_text().splitlines()[-1])
                meta = record["compactMetadata"]
                self.assertIn("preservedSegment", meta)
                self.assertIn("cumulativeDroppedTokens", meta)

    def test_the_newest_record_wins(self) -> None:
        """A long-running session compacts more than once. The trigger being
        recorded is this compaction's, not the first one's."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.jsonl"
            path.write_text(
                json.dumps({"type": "system", "compactMetadata": {"trigger": "manual"}}) + "\n"
                + json.dumps({"type": "user", "message": "work"}) + "\n"
                + json.dumps({"type": "system", "compactMetadata": {"trigger": "auto"}}) + "\n")
            self.assertEqual(cm.trigger_of(str(path)), "auto")

    def test_it_degrades_to_none_and_never_raises(self) -> None:
        """This hook runs when a session is least able to cope with a crash.
        A missing trigger costs harmonic-forge#451 one discriminator; an
        exception costs the session its recovery note."""
        for path in ("", "/nonexistent/none.jsonl", "/etc/hostname", "/etc"):
            with self.subTest(path=path):
                self.assertIsNone(cm.trigger_of(path))

    def test_a_record_straddling_a_chunk_boundary_is_still_found(self) -> None:
        """The reverse scan reads in chunks, so a record can be cut in half by
        a boundary. The overlap carried between chunks is what stops that
        losing it — without the overlap this record parses as two fragments,
        neither of which is JSON."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.jsonl"
            record = json.dumps({"type": "system", "id": "y" * 4096,
                                 "compactMetadata": {"trigger": "auto"}})
            # Pad so the record lands astride a CHUNK_BYTES boundary.
            pad_before = cm.CHUNK_BYTES - (len(record) // 2)
            path.write_text("x" * pad_before + "\n" + record + "\n"
                            + json.dumps({"type": "user", "m": "after"}) + "\n")
            self.assertEqual(cm.trigger_of(str(path)), "auto")

    def test_a_record_far_past_the_old_budget_is_found(self) -> None:
        """harmonic-forge#489's regression case, at the observed magnitude.

        The deleted `TRIGGER_SCAN_BYTES` was 1 MiB. Measured across all 37
        transcripts on this machine the newest record sat a median of 3.5 MB
        and a maximum of 13.3 MiB from EOF — outside that budget on 32 of 37.
        8 MB is past any budget that was ever plausible.
        """
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.jsonl"
            with path.open("w") as fh:
                fh.write(json.dumps(
                    {"type": "system", "compactMetadata": {"trigger": "auto"}}) + "\n")
                filler = json.dumps({"type": "user", "pad": "z" * 8192}) + "\n"
                for _ in range(1024):          # ~8 MB after the record
                    fh.write(filler)
            self.assertGreater(path.stat().st_size, 8 << 20)
            self.assertFalse(hasattr(cm, "TRIGGER_SCAN_BYTES"),
                             "the budget was deleted, not resized")
            self.assertEqual(cm.trigger_of(str(path)), "auto")

    def test_a_record_older_than_not_before_is_rejected(self) -> None:
        """The defect a wider window would have shipped: finding the PREVIOUS
        compaction's record and reporting its trigger as this one's."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.jsonl"
            path.write_text(json.dumps({
                "type": "system", "timestamp": "2026-09-06T04:00:00.000Z",
                "compactMetadata": {"trigger": "auto"}}) + "\n")
            self.assertIsNone(cm.trigger_of(
                str(path), not_before="2026-09-06T04:23:24.104434+00:00"))

    def test_a_record_newer_than_not_before_is_accepted(self) -> None:
        """The real ordering: five live markers show the record's timestamp
        42-68 ms AFTER the marker's `compacted_at`."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.jsonl"
            path.write_text(json.dumps({
                "type": "system", "timestamp": "2026-09-06T04:23:24.146Z",
                "compactMetadata": {"trigger": "auto"}}) + "\n")
            self.assertEqual(cm.trigger_of(
                str(path), not_before="2026-09-06T04:23:24.104434+00:00"), "auto")

    def test_the_two_timestamp_spellings_compare_chronologically(self) -> None:
        """Not lexicographically. The transcript writes `...146Z`, the marker
        writes `...104434+00:00`, and `Z` (0x5A) sorts after `+` (0x2B) — so a
        string comparison inverts for exactly the pairs this is asked about.
        These are the real bytes from session ab5817ae.
        """
        record = "2026-09-06T04:23:24.146Z"
        marker = "2026-09-06T04:23:24.104434+00:00"
        self.assertGreater(cm._parse_stamp(record), cm._parse_stamp(marker))

    def test_a_naive_timestamp_is_not_a_crash(self) -> None:
        """A record without an offset is assumed UTC rather than raising —
        comparing aware and naive datetimes is a TypeError, and this runs on
        every tool call of a compacted session."""
        self.assertIsNotNone(cm._parse_stamp("2026-09-06T04:23:24.146"))
        for junk in (None, "", "not-a-date", 17):
            with self.subTest(junk=junk):
                self.assertIsNone(cm._parse_stamp(junk))

    def test_handle_does_not_write_a_trigger_at_all(self) -> None:
        """harmonic-forge#489. `SessionStart` cannot resolve it — the record is
        written 42-68 ms later — so the key is ABSENT, not `null`. "Not yet
        resolved" and "resolved, no answer" are different states and
        harmonic-forge#451 must not read the first as the second."""
        with tempfile.TemporaryDirectory() as tmp:
            markers = Path(tmp) / "markers"
            with mock.patch.object(cm, "MARKER_DIR", markers):
                cm.handle({"source": "compact", "session_id": "s1", "cwd": "/x/HRSE2-lane2",
                           "transcript_path": str(FIXTURES / "transcript_auto.jsonl")},
                          {"LANE": "2"})
                written = json.loads((markers / "s1.json").read_text())
        self.assertNotIn("trigger", written)
        self.assertEqual(written["lane"], "2")

    def test_handle_carries_the_transcript_path_forward(self) -> None:
        """So the probe can resolve the trigger without re-deriving it, and
        still works on a `PreToolUse` payload that omits the path."""
        with tempfile.TemporaryDirectory() as tmp:
            markers = Path(tmp) / "markers"
            with mock.patch.object(cm, "MARKER_DIR", markers):
                cm.handle({"source": "compact", "session_id": "s1", "cwd": "/x",
                           "transcript_path": "/some/where.jsonl"}, {"LANE": "2"})
                written = json.loads((markers / "s1.json").read_text())
        self.assertEqual(written["transcript_path"], "/some/where.jsonl")


class ResolveTriggerIsUpdateOnlyAndSetOnce(unittest.TestCase):
    """harmonic-forge#489 — the read-time half.

    Same two invariants `note_reload` carries, for the same reasons: a
    `PreToolUse` that could CREATE a marker would manufacture a compaction that
    never happened, and re-resolving would let a later compaction's record
    overwrite this one's answer.
    """

    def _marker(self, markers: Path, **extra) -> None:
        markers.mkdir(parents=True, exist_ok=True)
        payload = {"compacted_at": "2026-09-06T04:23:24.104434+00:00",
                   "source": "compact", "lane": "2", "cwd": "/x"}
        payload.update(extra)
        (markers / "s1.json").write_text(json.dumps(payload))

    def _transcript(self, tmp: Path, stamp: str, trigger: str = "auto") -> str:
        path = tmp / "t.jsonl"
        path.write_text(json.dumps({"type": "system", "timestamp": stamp,
                                    "compactMetadata": {"trigger": trigger}}) + "\n")
        return str(path)

    def test_it_fills_in_the_trigger(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            markers = Path(tmp) / "markers"
            self._marker(markers)
            path = self._transcript(Path(tmp), "2026-09-06T04:23:24.146Z")
            with mock.patch.object(cm, "MARKER_DIR", markers):
                self.assertEqual(cm.resolve_trigger("s1", path), "auto")
                written = json.loads((markers / "s1.json").read_text())
        self.assertEqual(written["trigger"], "auto")

    def test_it_never_creates_a_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            markers = Path(tmp) / "markers"
            path = self._transcript(Path(tmp), "2026-09-06T04:23:24.146Z")
            with mock.patch.object(cm, "MARKER_DIR", markers):
                self.assertIsNone(cm.resolve_trigger("s1", path))
            self.assertFalse((markers / "s1.json").exists())

    def test_it_is_set_once(self) -> None:
        """A second compaction's record must not overwrite the first's answer
        on a marker that already carries one."""
        with tempfile.TemporaryDirectory() as tmp:
            markers = Path(tmp) / "markers"
            self._marker(markers, trigger="manual")
            path = self._transcript(Path(tmp), "2026-09-06T04:23:24.146Z", "auto")
            with mock.patch.object(cm, "MARKER_DIR", markers):
                self.assertEqual(cm.resolve_trigger("s1", path), "manual")
                written = json.loads((markers / "s1.json").read_text())
        self.assertEqual(written["trigger"], "manual")

    def test_an_unresolvable_trigger_stays_absent_not_null(self) -> None:
        """The record may simply not have been flushed yet on a very fast first
        tool call. Writing `null` would freeze that transient state forever
        under the set-once rule above."""
        with tempfile.TemporaryDirectory() as tmp:
            markers = Path(tmp) / "markers"
            self._marker(markers)
            with mock.patch.object(cm, "MARKER_DIR", markers):
                self.assertIsNone(cm.resolve_trigger("s1", "/nonexistent.jsonl"))
                written = json.loads((markers / "s1.json").read_text())
        self.assertNotIn("trigger", written)

    def test_it_falls_back_to_the_path_the_marker_carries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            markers = Path(tmp) / "markers"
            path = self._transcript(Path(tmp), "2026-09-06T04:23:24.146Z")
            self._marker(markers, transcript_path=path)
            with mock.patch.object(cm, "MARKER_DIR", markers):
                self.assertEqual(cm.resolve_trigger("s1"), "auto")

    def test_a_previous_compactions_record_is_not_adopted(self) -> None:
        """End to end, the defect harmonic-forge#489 closes: the only record in
        the file predates this compaction, so the answer is no answer."""
        with tempfile.TemporaryDirectory() as tmp:
            markers = Path(tmp) / "markers"
            self._marker(markers)
            path = self._transcript(Path(tmp), "2026-09-06T03:00:00.000Z")
            with mock.patch.object(cm, "MARKER_DIR", markers):
                self.assertIsNone(cm.resolve_trigger("s1", path))
                written = json.loads((markers / "s1.json").read_text())
        self.assertNotIn("trigger", written)


class TriggerRejectsNonValues(unittest.TestCase):
    """`""` and a missing key are not triggers. Returning either would make
    harmonic-forge#451 compare its deny condition against a value that means
    "we did not find out"."""

    def trigger(self, meta) -> str | None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.jsonl"
            path.write_text(json.dumps({"type": "system", "compactMetadata": meta}) + "\n")
            return cm.trigger_of(str(path))

    def test_an_empty_trigger_is_not_a_value(self) -> None:
        self.assertIsNone(self.trigger({"trigger": "", "preTokens": 1}))

    def test_a_missing_trigger_key_is_not_a_value(self) -> None:
        self.assertIsNone(self.trigger({"preTokens": 1}))

    def test_a_non_string_trigger_is_not_a_value(self) -> None:
        for bogus in (True, 1, ["auto"], {"v": "auto"}, None):
            with self.subTest(bogus=bogus):
                self.assertIsNone(self.trigger({"trigger": bogus}))

    def test_a_record_with_no_compact_metadata_is_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.jsonl"
            path.write_text(
                json.dumps({"type": "system", "compactMetadata": {"trigger": "auto"}}) + "\n"
                + json.dumps({"type": "user", "message": "later work"}) + "\n")
            self.assertEqual(cm.trigger_of(str(path)), "auto")

    def test_a_valid_record_on_the_first_line_of_the_window_is_kept(self) -> None:
        """The boundary the removed slice would have broken: when the read
        budget lands exactly on a newline, the first line is a complete
        record and must not be discarded."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.jsonl"
            record = json.dumps({"type": "system", "compactMetadata": {"trigger": "manual"}})
            path.write_text(record + "\n")
            self.assertEqual(cm.trigger_of(str(path)), "manual")
