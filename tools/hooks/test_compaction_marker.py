#!/usr/bin/env python3
"""Tests for compaction_marker.py (harmonic-forge#446).

The payload fixture is a **real captured `SessionStart` record** with
`source: "compact"`, taken from a live compaction and redacted, not hand-written
— a hand-rolled fixture would keep passing while the real record shape drifted,
which is the failure this issue's AC1 called out by name.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
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
