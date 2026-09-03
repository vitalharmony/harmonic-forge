#!/usr/bin/env python3
"""Tests for ID bands and the cross-registry check (harmonic-forge#454).

Every test builds **fixture registries** in a temp dir. None reads the live
production files — the spec requires it, and the reason is concrete: the live
pair is currently clean, so a suite pointed at it would pass without
exercising a single failure path, which is how a check ships that has never
been seen to fail.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import check_cross_registry as cross  # noqa: E402
import check_rule_drift as drift  # noqa: E402


def _registry(path: Path, band_min, band_max, ids, accepted=None) -> Path:
    rows = "".join(textwrap.dedent(f"""
        [[rule]]
        id = "R-{i:04d}"
        file = "x.md"
        anchor = "a"
        statement = "s"
        text_sha = "deadbeefdeadbeef"
        hooks = []
        """) for i in ids)
    head = ""
    if band_min is not None:
        head += f"band_min = {band_min}\n"
    if band_max is not None:
        head += f"band_max = {band_max}\n"
    if accepted:
        head += "accepted_out_of_band = [" + ", ".join(f'"{a}"' for a in accepted) + "]\n"
    path.write_text(head + rows, encoding="utf-8")
    return path


class Bands(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_next_id_stays_inside_the_band(self):
        r = _registry(self.root / "a.toml", 5000, 9999, [5000, 5001])
        self.assertEqual(drift.next_id(r), "R-5002")

    def test_empty_band_starts_at_band_min(self):
        r = _registry(self.root / "a.toml", 5000, 9999, [])
        self.assertEqual(drift.next_id(r), "R-5000")

    def test_rows_outside_the_band_do_not_raise_the_allocation(self):
        """The accepted foreign range must not drag `--next-id` upward — this
        is what keeps forge allocating at R-0331 rather than R-0330+hrse."""
        r = _registry(self.root / "a.toml", 1, 4999, [1, 2, 7000])
        self.assertEqual(drift.next_id(r), "R-0003")

    def test_absent_band_fails_loudly(self):
        """The whole defect was a silent fallback to whole-file max()."""
        r = _registry(self.root / "a.toml", None, None, [1])
        with self.assertRaises(SystemExit) as ctx:
            drift.next_id(r)
        self.assertIn("band", str(ctx.exception).lower())

    def test_partial_band_declaration_fails_loudly(self):
        r = _registry(self.root / "a.toml", 5000, None, [5000])
        with self.assertRaises(SystemExit):
            drift.next_id(r)

    def test_exhausted_band_fails_rather_than_overflowing(self):
        r = _registry(self.root / "a.toml", 10, 11, [10, 11])
        with self.assertRaises(SystemExit) as ctx:
            drift.next_id(r)
        self.assertIn("exhausted", str(ctx.exception))

    def test_the_original_bug_cannot_recur(self):
        """Forge-shaped and hrse-shaped registries, allocated independently,
        must not collide — this is harmonic-forge#454 stated as a test."""
        f = _registry(self.root / "f.toml", 1, 4999, list(range(1, 253)) + [330])
        h = _registry(self.root / "h.toml", 5000, 9999, list(range(253, 330)),
                      accepted=["R-0253..R-0329"])
        self.assertNotEqual(drift.next_id(f), drift.next_id(h))
        self.assertEqual(drift.next_id(f), "R-0331")
        self.assertEqual(drift.next_id(h), "R-5000")


class CrossRegistry(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_clean_pair(self):
        f = _registry(self.root / "f.toml", 1, 4999, [1, 2])
        h = _registry(self.root / "h.toml", 5000, 9999, [5000])
        self.assertEqual(cross.check(f, h), [])

    def test_shared_id_is_caught(self):
        f = _registry(self.root / "f.toml", 1, 4999, [1, 2])
        h = _registry(self.root / "h.toml", 5000, 9999, [2, 5000],
                      accepted=["R-0002..R-0002"])
        failures = cross.check(f, h)
        self.assertTrue(any("BOTH registries" in x for x in failures))

    def test_the_accepted_hole_is_not_flagged(self):
        """TC4: the real, current state must not fire on first run."""
        f = _registry(self.root / "f.toml", 1, 4999, list(range(1, 253)) + [330])
        h = _registry(self.root / "h.toml", 5000, 9999, list(range(253, 330)),
                      accepted=["R-0253..R-0329"])
        self.assertEqual(cross.check(f, h), [])

    def test_undeclared_out_of_band_row_is_flagged(self):
        """Same shape as the accepted hole, but undeclared — must fire, or the
        exception mechanism would excuse everything."""
        f = _registry(self.root / "f.toml", 1, 4999, [1])
        h = _registry(self.root / "h.toml", 5000, 9999, [300, 5000])
        failures = cross.check(f, h)
        self.assertTrue(any("outside its band" in x for x in failures))

    def test_missing_band_in_either_registry_is_reported(self):
        f = _registry(self.root / "f.toml", None, None, [1])
        h = _registry(self.root / "h.toml", 5000, 9999, [5000])
        self.assertTrue(any("no band" in x for x in cross.check(f, h)))

    def test_unparseable_accepted_entry_fails_loudly(self):
        f = _registry(self.root / "f.toml", 1, 4999, [1])
        h = _registry(self.root / "h.toml", 5000, 9999, [5000], accepted=["nonsense"])
        with self.assertRaises(SystemExit):
            cross.check(f, h)

    def test_missing_sibling_reports_skipped_not_clean(self):
        """TC5, and the #440 lesson: a skipped check must be distinguishable
        from a passing one in the output, not just in someone's memory."""
        script = Path(__file__).resolve().parent / "check_cross_registry.py"
        f = _registry(self.root / "f.toml", 1, 4999, [1])
        proc = subprocess.run(
            [sys.executable, str(script), "--registry", str(f),
             "--sibling", str(self.root / "nope.toml")],
            capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0)
        self.assertIn("SKIPPED", proc.stdout)
        self.assertIn("not a pass", proc.stdout)
        self.assertNotIn("clean", proc.stdout)


class LiveRegistriesDeclareBands(unittest.TestCase):
    """The one check that does read the live file — that it declares a band at
    all. Without this the suite could pass while the shipped registry is the
    thing that fails loudly."""

    def test_forge_registry_declares_its_band(self):
        low, high = drift.load_band(Path(__file__).resolve().parent / "registry.toml")
        self.assertEqual((low, high), (1, 4999))


if __name__ == "__main__":
    unittest.main()
