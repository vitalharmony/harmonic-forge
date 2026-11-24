#!/usr/bin/env python3
"""harmonic-forge#1507 preclose pass 1: the origin parser accepts only a
github.com host or an absolute local fixture path, and never a key the
manifest's own normalization would rewrite."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from origin_parse import parse_origin  # noqa: E402


class ParseOrigin(unittest.TestCase):
    def test_accepted_shapes(self) -> None:
        for url in ("https://github.com/Kenekted/kenekted-ai.git",
                    "https://github.com/kenekted/kenekted-ai",
                    "https://github.com/kenekted/kenekted-ai/",
                    "git@github.com:kenekted/kenekted-ai.git",
                    "ssh://git@github.com/kenekted/kenekted-ai.git",
                    "/tmp/x/github.com/kenekted/kenekted-ai.git"):
            with self.subTest(url=url):
                self.assertEqual(parse_origin(url), "kenekted/kenekted-ai")

    def test_another_host_is_refused(self) -> None:
        for url in ("https://notgithub.com/vitalharmony/harmonic-forge",
                    "https://evil.example/github.com/vitalharmony/harmonic-forge.git",
                    "https://github.com.evil.example/vitalharmony/harmonic-forge",
                    "git@evil.example:github.com/vitalharmony/harmonic-forge.git",
                    "ssh://git@github.com:22/vitalharmony/harmonic-forge.git",
                    "relative/github.com/vitalharmony/harmonic-forge.git",
                    "file:///tmp/github.com/vitalharmony/harmonic-forge.git"):
            with self.subTest(url=url):
                self.assertIsNone(parse_origin(url))

    def test_a_key_normalization_would_rewrite_is_refused(self) -> None:
        for url in ("https://github.com/vitalharmony/harmonic-forge.git.git",
                    "https://github.com/vitalharmony/..",
                    "https://github.com/../harmonic-forge"):
            with self.subTest(url=url):
                self.assertIsNone(parse_origin(url))

    def test_no_origin_is_refused(self) -> None:
        self.assertIsNone(parse_origin(""))


if __name__ == "__main__":
    unittest.main()
