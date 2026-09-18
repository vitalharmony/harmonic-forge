#!/usr/bin/env python3
"""Proves `tools/run_tests.py`'s `redirected_belt_candidates_dir` guard
(harmonic-forge#691 preclose finding) actually keeps a `base_dir`-less
`belt_candidates.record_candidate` call off the real
`~/.claude/state/belt/candidates/` directory for the duration of the
context manager -- and restores the original default afterward.

Mutation-style: this does not just check that the function exists, it
calls `record_candidate` with NO explicit `base_dir` (the exact call shape
`tools/gh/l2_post.py` makes) from inside the guard and asserts the file
landed in the redirected tmp dir, never in the real directory.

Run: python3 tools/gh/test_run_tests_belt_guard.py
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

_GH_DIR = Path(__file__).resolve().parent
_TOOLS_DIR = _GH_DIR.parent
for _dir in (_GH_DIR, _TOOLS_DIR):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

import belt_candidates  # noqa: E402
import run_tests  # noqa: E402


class RedirectedBeltCandidatesDirTests(unittest.TestCase):
    def test_record_with_no_base_dir_lands_in_the_redirect_not_the_real_dir(self):
        real_dir = belt_candidates.DEFAULT_CANDIDATES_DIR
        with tempfile.TemporaryDirectory() as tmp:
            redirect = Path(tmp) / "belt-candidates"
            with run_tests.redirected_belt_candidates_dir(redirect):
                self.assertEqual(belt_candidates.DEFAULT_CANDIDATES_DIR, redirect)
                # The exact call shape l2_post.py makes: no base_dir.
                belt_candidates.record_candidate(
                    "vitalharmony/harmonic-forge", 691, "completion", "l2")
                files = list(redirect.glob("*.json"))
                self.assertEqual(len(files), 1)
                self.assertEqual(
                    files[0].name,
                    "vitalharmony__harmonic-forge__691.json",
                )
            # Real directory must never have received this entry.
            real_file = real_dir / "vitalharmony__harmonic-forge__691.json"
            self.assertFalse(
                real_file.exists(),
                "record_candidate with no base_dir wrote into the real "
                "~/.claude/state/belt/candidates/ directory -- the guard "
                "failed to redirect it",
            )

    def test_original_default_is_restored_after_the_context_exits(self):
        original = belt_candidates.DEFAULT_CANDIDATES_DIR
        with tempfile.TemporaryDirectory() as tmp:
            redirect = Path(tmp) / "belt-candidates"
            with run_tests.redirected_belt_candidates_dir(redirect):
                self.assertEqual(belt_candidates.DEFAULT_CANDIDATES_DIR, redirect)
            self.assertEqual(belt_candidates.DEFAULT_CANDIDATES_DIR, original)

    def test_restored_even_when_the_body_raises(self):
        original = belt_candidates.DEFAULT_CANDIDATES_DIR
        with tempfile.TemporaryDirectory() as tmp:
            redirect = Path(tmp) / "belt-candidates"
            with self.assertRaises(RuntimeError):
                with run_tests.redirected_belt_candidates_dir(redirect):
                    raise RuntimeError("boom")
            self.assertEqual(belt_candidates.DEFAULT_CANDIDATES_DIR, original)


if __name__ == "__main__":
    unittest.main()
