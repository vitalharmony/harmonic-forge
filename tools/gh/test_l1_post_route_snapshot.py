#!/usr/bin/env python3
"""Tests for the handoff route snapshot (harmonic-forge#1231, semantic routing A3)."""
import hashlib
import importlib.util
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent / "routing"))


def load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


post = load("l1_post")
snap = post._route_snapshot  # the module object l1_post itself calls, so patches reach it
sys.path.insert(0, str(ROOT.parent / "telemetry"))
import emit  # noqa: E402
import gates  # noqa: E402
import route_store  # noqa: E402

REPO = "vitalharmony/harmonic-forge"
KEY = f"{REPO}#1231"
SHA = "a" * 40


def _handoff(files: list[str], threat: str | None = "good-faith") -> str:
    rows = "\n".join(f"| {f} | 10 | Modify |" for f in files)
    lead = "## Handoff: [Issue #1231 — test]\n\n**Scope:** s.\n**Next:** n.\n"
    if threat:
        lead += f"**Threat model:** {threat}\n"
    return (lead + "\n### Affected Files\n| File | Lines | Change type |\n|---|---|---|\n" + rows
            + "\n\n### Root Cause / Entry Point\n> x\n")


class Snapshot(unittest.TestCase):
    def test_lane_machinery_is_path_2(self):
        line, route = snap.snapshot(_handoff(["tools/gh/l1_post.py"]), REPO)
        self.assertEqual((route.path, route.gate_id), (2, "lane-machinery"))
        self.assertTrue(line.startswith("Route (snapshot): path 2 — gate lane-machinery — "))

    def test_hrse_product_is_path_1(self):
        line, route = snap.snapshot(_handoff(["`backend/app/x.py`"]), "vitalharmony/hrse")
        self.assertEqual((route.path, route.gate_id), (1, "hrse-product"))
        self.assertIn("path 1 — gate hrse-product", line)

    def test_header_divider_and_new_rows_add_no_paths(self):
        halves = snap.affected_files(_handoff(["tools/a.py"]), REPO)
        self.assertEqual(halves, {REPO: ["tools/a.py"]})

    def test_qualified_path_is_its_own_half(self):
        halves = snap.affected_files(_handoff(["vitalharmony/hrse:backend/x.py", "tools/a.py"]), REPO)
        self.assertEqual(halves, {"vitalharmony/hrse": ["backend/x.py"], REPO: ["tools/a.py"]})

    def test_hostile_threat_model_is_read_from_the_lead(self):
        body = _handoff(["tools/hooks/x.py"], threat="hostile-author: batch-merge-grant")
        self.assertEqual(snap.threat_model(body), "hostile-author: batch-merge-grant")
        self.assertEqual(snap.snapshot(body, REPO)[1].gate_id, "authorization")

    def test_evaluator_error_is_an_unavailable_line(self):
        """AC5."""
        with patch.object(gates, "evaluate", side_effect=KeyError("x")):
            line, route = snap.snapshot(_handoff(["tools/a.py"]), REPO)
        self.assertIsNone(route)
        self.assertEqual(line, "Route (snapshot): unavailable — KeyError")


class TempStore(unittest.TestCase):
    """Route events go to a temp store, never the live telemetry store."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = patch.dict(os.environ, {emit.STORE_ENV: str(Path(self.tmp.name) / "store")})
        patcher.start()
        self.addCleanup(patcher.stop)


class Recording(TempStore):

    def _events(self):
        return route_store.load(KEY)

    def test_first_route_is_recorded_at_the_handoff_checkpoint(self):
        snap.record_snapshot(REPO, 1231, gates.Route(2, "lane-machinery", "r"))
        events = self._events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["attrs"]["checkpoint"], "handoff")

    def test_looser_result_is_not_recorded_in_force(self):
        """AC4."""
        snap.record_snapshot(REPO, 1231, gates.Route(1, "hrse-product", "r"))
        note = snap.record_snapshot(REPO, 1231, gates.Route(2, "lane-machinery", "r"))
        self.assertIn("looser than path 1", note)
        self.assertEqual(route_store.current(KEY)["attrs"]["path"], 1)
        self.assertEqual(len(self._events()), 1)

    def test_stricter_result_becomes_the_route_in_force(self):
        snap.record_snapshot(REPO, 1231, gates.Route(2, "lane-machinery", "r"))
        self.assertIsNone(snap.record_snapshot(REPO, 1231, gates.Route(1, "hrse-product", "r")))
        self.assertEqual(route_store.current(KEY)["attrs"]["path"], 1)


class PostedHandoff(TempStore):
    """AC2 and AC3 through `_post_kind`, with the world mocked out."""

    def setUp(self):
        super().setUp()
        for name in ("world_checks", "write_receipt", "validate_sticky_wicket", "_discharge_handoff_owed"):
            patcher = patch.object(post, name, return_value=(["w"], []) if name == "world_checks" else None)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.object(post.pitch_receipt, "consume")
        patcher.start()
        self.addCleanup(patcher.stop)

    def _post(self, body, comment):
        with patch.object(post, "comment_body", comment):
            return post._post_kind(REPO, 1231, "handoff", body, SHA, "b")

    def test_line_is_inside_the_digest_and_an_event_is_written(self):
        posted = {}

        def comment(repo, issue, text):
            posted["text"] = text
            return "https://example.test/c", 1

        self._post(_handoff(["tools/gh/l1_post.py"]), comment)
        text = posted["text"]
        self.assertIn("Route (snapshot): path 2 — gate lane-machinery", text)
        covered = text.split("<!-- l1-post")[0].rstrip("\n")
        digest = re.search(r"body-sha256=([0-9a-f]{64})", text)[1]
        self.assertEqual(hashlib.sha256(covered.encode()).hexdigest(), digest)
        self.assertEqual(route_store.current(KEY)["attrs"]["checkpoint"], "handoff")

    def test_failed_post_records_nothing(self):
        def comment(*_):
            raise SystemExit("post failed")

        with self.assertRaises(SystemExit):
            self._post(_handoff(["tools/gh/l1_post.py"]), comment)
        self.assertEqual(route_store.load(KEY), [])

    def test_record_error_after_the_post_never_raises(self):
        """AC5."""
        with patch.object(snap, "record_snapshot", side_effect=RuntimeError("store down")):
            url, _ = self._post(_handoff(["tools/gh/l1_post.py"]), lambda *a: ("https://example.test/c", 1))
        self.assertEqual(url, "https://example.test/c")

    def test_only_a_handoff_carries_the_line(self):
        posted = {}

        def comment(repo, issue, text):
            posted["text"] = text
            return "https://example.test/c", 1

        with patch.object(post, "comment_body", comment):
            post._post_kind(REPO, 1231, "rework", "**Finding:** x\n", SHA, "b")
        self.assertNotIn("Route (snapshot)", posted["text"])
        self.assertEqual(route_store.load(KEY), [])


if __name__ == "__main__":
    unittest.main()
