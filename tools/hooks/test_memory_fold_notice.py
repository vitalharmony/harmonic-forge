"""Tests for memory_fold_notice.py (harmonic-forge#982, AC3).

`fold_due` is replaced by a stub line function and the suppression file lives
in a temp directory, so nothing reads the operator's store or cache.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import memory_fold_notice as notice  # noqa: E402

LINE = "memory-fold owed: 7 days since last fold"


class Decide(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.suppress = Path(self._tmp.name) / "notice.json"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def run_decide(self, payload: dict, lane: str | None = "1", now: float = 1000.0,
                   line: str = LINE, kinds: str = "fallback", deliver: bool = True) -> dict | None:
        env = {} if lane is None else {"LANE": lane}
        out, key = notice.decide(payload, env, now, line_fn=lambda _cwd: (line, kinds if line else ""),
                                 suppress_path=self.suppress)
        if out and key and deliver:
            notice.mark_sent(key, now, self.suppress)
        return out

    def test_lane1_prompt_gets_additional_context_only(self) -> None:
        out = self.run_decide({"prompt": "proactively find work to do", "cwd": "/x"})
        self.assertIsNotNone(out)
        self.assertNotIn("systemMessage", out)
        ctx = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn(LINE, ctx)
        self.assertIn("/memory-triage", ctx)
        self.assertIn("R-0375", ctx)

    def test_other_lanes_and_no_lane_are_silent(self) -> None:
        for lane in ("2", "3", None):
            self.assertIsNone(self.run_decide({"prompt": "hi"}, lane=lane), lane)

    def test_wrapped_prompt_is_silent(self) -> None:
        wrapped = "<task-notification>\n<task-id>x</task-id>\n</task-notification>"
        self.assertIsNone(self.run_decide({"prompt": wrapped}))

    def test_repeat_within_24h_is_suppressed_then_returns(self) -> None:
        self.assertIsNotNone(self.run_decide({"prompt": "a"}, now=1000.0))
        self.assertIsNone(self.run_decide({"prompt": "b"}, now=1000.0 + 3600))
        self.assertIsNotNone(self.run_decide({"prompt": "c"}, now=1000.0 + 25 * 3600))

    def test_a_changed_count_in_the_same_trigger_stays_suppressed(self) -> None:
        self.assertIsNotNone(self.run_decide({"prompt": "a"}, line="memory-fold owed: 15 new", kinds="volume"))
        self.assertIsNone(self.run_decide({"prompt": "b"}, now=1001.0, line="memory-fold owed: 16 new", kinds="volume"))

    def test_a_different_trigger_set_is_not_suppressed(self) -> None:
        self.assertIsNotNone(self.run_decide({"prompt": "a"}, now=1000.0))
        self.assertIsNotNone(self.run_decide({"prompt": "b"}, now=1001.0, kinds="recurrence,fallback"))

    def test_suppression_spans_sessions(self) -> None:
        self.assertIsNotNone(self.run_decide({"prompt": "a", "session_id": "s1"}))
        self.assertIsNone(self.run_decide({"prompt": "a", "session_id": "s2"}, now=1001.0))

    def test_compaction_start_delivers_despite_suppression(self) -> None:
        self.assertIsNotNone(self.run_decide({"prompt": "a", "session_id": "s1"}))
        out = self.run_decide({"hook_event_name": "SessionStart", "source": "compact",
                               "session_id": "s1"}, now=1001.0)
        self.assertIsNotNone(out)

    def test_undelivered_notice_is_not_suppressed(self) -> None:
        self.assertIsNotNone(self.run_decide({"prompt": "a"}, deliver=False))
        self.assertIsNotNone(self.run_decide({"prompt": "b"}, now=1001.0))

    def test_session_start_always_gets_the_line(self) -> None:
        self.assertIsNotNone(self.run_decide({"prompt": "a"}))
        out = self.run_decide({"hook_event_name": "SessionStart", "source": "clear"}, now=1001.0)
        self.assertEqual(out["hookSpecificOutput"]["hookEventName"], "SessionStart")

    def test_nothing_owed_is_silent(self) -> None:
        self.assertIsNone(self.run_decide({"prompt": "a"}, line=""))


class MainEndToEnd(unittest.TestCase):
    """main() for real: fold_due runs against a temp store and cache, the first
    tick delivers and records, the second is suppressed."""

    def test_second_tick_is_suppressed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store, config, cache = root / "store", root / "config", root / "cache"
            store.mkdir()
            config.mkdir()
            (store / "MEMORY.md").write_text("# Memory Index\n")
            (config / "settings.json").write_text(json.dumps({"autoMemoryDirectory": str(store)}))
            env = {**os.environ, "LANE": "1", "CLAUDE_CONFIG_DIR": str(config),
                   "XDG_CACHE_HOME": str(cache)}
            payload = json.dumps({"prompt": "proactively find work to do", "cwd": tmp})
            outs = [subprocess.run([sys.executable, str(HERE / "memory_fold_notice.py")],
                                   input=payload, capture_output=True, text=True, env=env,
                                   timeout=60).stdout for _ in range(2)]
            self.assertTrue((cache / "harmonic-forge" / "memory_fold" / "notice.json").exists())
        first = json.loads(outs[0])["hookSpecificOutput"]["additionalContext"]
        self.assertIn("memory-fold owed: no marker", first)
        self.assertNotIn("could not be written", first)
        self.assertEqual(outs[1], "")


class Main(unittest.TestCase):
    def test_garbage_input_exits_zero_silently(self) -> None:
        result = subprocess.run([sys.executable, str(HERE / "memory_fold_notice.py")],
                                input="not json", capture_output=True, text=True,
                                env={**os.environ, "LANE": "1"}, timeout=30)
        self.assertEqual((result.returncode, result.stdout), (0, ""))

    def test_not_lane1_exits_zero_silently(self) -> None:
        env = {k: v for k, v in os.environ.items() if k != "LANE"}
        result = subprocess.run([sys.executable, str(HERE / "memory_fold_notice.py")],
                                input=json.dumps({"prompt": "hi"}), capture_output=True,
                                text=True, env=env, timeout=30)
        self.assertEqual((result.returncode, result.stdout), (0, ""))


if __name__ == "__main__":
    unittest.main()
