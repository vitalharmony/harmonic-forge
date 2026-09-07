#!/usr/bin/env python3
"""Tests for `statusline_compaction.py` (harmonic-forge#497).

The load-bearing property here is not what the segment says — it is that the
segment can never take the statusline down. A statusline command that raises
either blanks the whole line or spams it on every render, and the operator
cannot act on a diagnostic they cannot read. So most of these tests are about
failure paths, and each of them asserts *both* an empty string and a zero exit.
"""
from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import statusline_compaction as sc  # noqa: E402


class SegmentTests(unittest.TestCase):
    def _with_marker(self, payload: object | None = None, *,
                     corrupt: bool = False) -> str:
        """Run `segment()` against a marker directory built for this test.

        `MARKER_DIR` is resolved at import time, so it is patched directly
        rather than through the environment — patching `CLAUDE_CONFIG_DIR`
        here would silently read the operator's real markers instead.
        """
        import compaction_marker as cm

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(cm, "MARKER_DIR", Path(tmp)):
                path = Path(tmp) / "sess-1.json"
                if corrupt:
                    path.write_text("{not json", encoding="utf-8")
                elif payload is not None:
                    path.write_text(json.dumps(payload), encoding="utf-8")
                return sc.segment("sess-1")

    def test_silent_below_one(self) -> None:
        """`compact:0` on every session would spend permanent width on the
        default case. Silence is what makes the segment mean something."""
        self.assertEqual(self._with_marker({"compactions": 0}), "")
        self.assertEqual(self._with_marker({}), "")

    def test_counts_render(self) -> None:
        self.assertEqual(self._with_marker({"compactions": 1}), "compact:1")

    def test_threshold_adds_the_bang(self) -> None:
        """A count is information; a count past the threshold is an
        instruction (R-0339: restart at the batch boundary). Rendering both
        identically would leave the operator holding the threshold."""
        self.assertEqual(self._with_marker({"compactions": 2}), "compact:2!")
        self.assertEqual(self._with_marker({"compactions": 7}), "compact:7!")
        self.assertEqual(sc.RESTART_THRESHOLD, 2)

    def test_no_marker_is_silent_not_an_error(self) -> None:
        self.assertEqual(self._with_marker(None), "")

    def test_corrupt_marker_is_silent_not_an_error(self) -> None:
        self.assertEqual(self._with_marker(corrupt=True), "")

    def test_non_numeric_count_is_silent(self) -> None:
        """A hand-edited or future-schema marker must not raise."""
        self.assertEqual(self._with_marker({"compactions": "many"}), "")
        self.assertEqual(self._with_marker({"compactions": None}), "")

    def test_empty_session_id_is_silent(self) -> None:
        self.assertEqual(sc.segment(""), "")


class MainTests(unittest.TestCase):
    """`main()` must exit 0 on every input. No exceptions."""

    def _run(self, stdin_text: str) -> tuple[str, int]:
        out = io.StringIO()
        with mock.patch.object(sys, "stdin", io.StringIO(stdin_text)), \
                mock.patch.object(sys, "stdout", out):
            rc = sc.main()
        return out.getvalue(), rc

    def test_malformed_stdin_exits_zero_and_prints_nothing(self) -> None:
        for text in ("", "not json", "[]", "null"):
            with self.subTest(stdin=text):
                output, rc = self._run(text)
                self.assertEqual(output, "")
                self.assertEqual(rc, 0)

    def test_missing_session_id_exits_zero(self) -> None:
        output, rc = self._run(json.dumps({"model": {"display_name": "x"}}))
        self.assertEqual(output, "")
        self.assertEqual(rc, 0)

    def test_no_trailing_newline(self) -> None:
        """The segment is concatenated into a line by the caller, so a
        newline here would break the statusline into two rows."""
        import compaction_marker as cm

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(cm, "MARKER_DIR", Path(tmp)):
                (Path(tmp) / "s.json").write_text(
                    json.dumps({"compactions": 3}), encoding="utf-8")
                output, rc = self._run(json.dumps({"session_id": "s"}))
        self.assertEqual(output, "compact:3!")
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
