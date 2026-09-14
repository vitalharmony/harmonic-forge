#!/usr/bin/env python3
"""Unit tests for session_model.py and record_session_model.py
(harmonic-forge#656 AC4). Filesystem only, never the operator's real home or
cache. Run: python3 tools/hooks/test_session_model.py"""

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import record_session_model  # noqa: E402
import session_model as sm  # noqa: E402

# Shapes copied from real Claude Code 2.1.270 transcripts (2026-09-14).
ATTACHMENT_OPUS = {
    "isSidechain": False, "type": "attachment",
    "attachment": {"type": "model", "identity": {
        "modelId": "claude-opus-5[1m]", "marketingName": "Opus 5 (1M context)",
        "knowledgeCutoff": "May 2026"}},
}
SET_OPUS = {"type": "user", "message": {"role": "user", "content":
            "<local-command-stdout>Set model to `Opus 5 (1M context)` for this session "
            "only</local-command-stdout>"}}
SET_SONNET_ANSI = {"type": "user", "message": {"role": "user", "content":
                   "<local-command-stdout>Set model to \x1b[1mSonnet 5\x1b[22m and saved "
                   "as your default for new sessions</local-command-stdout>"}}
SET_FABLE = {"type": "user", "message": {"role": "user", "content":
             "<local-command-stdout>Set model to `Fable 5.1` for this session only"
             "</local-command-stdout>"}}


def assistant(model):
    return {"type": "assistant", "message": {"role": "assistant", "model": model,
                                             "content": [{"type": "text", "text": "ok"}]}}


def user(text):
    return {"type": "user", "message": {"role": "user", "content": text}}


class Base(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.home = self.root / "home"
        (self.home / ".claude").mkdir(parents=True)
        self.records = self.root / "records"

    def transcript(self, *entries):
        path = self.root / "t.jsonl"
        path.write_text("".join(json.dumps(e) + "\n" for e in entries))
        return str(path)

    def current(self, transcript, cwd="", session_id=None):
        return sm.current_model(transcript, cwd, session_id,
                                record_dir=self.records, home=self.home)


class TranscriptSources(Base):
    def test_assistant_message_model(self):
        path = self.transcript(user("hi"), assistant("claude-sonnet-5"))
        self.assertEqual(self.current(path), "claude-sonnet-5")

    def test_later_model_command_beats_earlier_assistant(self):
        path = self.transcript(assistant("claude-sonnet-5"), SET_OPUS)
        self.assertEqual(self.current(path), "Opus 5 (1M context)")
        self.assertEqual(sm.family(self.current(path)), "opus")

    def test_later_assistant_beats_earlier_model_command(self):
        path = self.transcript(SET_OPUS, assistant("claude-sonnet-5"))
        self.assertEqual(self.current(path), "claude-sonnet-5")

    def test_attachment_model_id(self):
        path = self.transcript(assistant("claude-sonnet-5"), ATTACHMENT_OPUS)
        self.assertEqual(self.current(path), "claude-opus-5[1m]")

    def test_ansi_bold_display_name_from_older_builds(self):
        path = self.transcript(assistant("claude-opus-5"), SET_SONNET_ANSI)
        self.assertEqual(self.current(path), "Sonnet 5")

    def test_fable_display_name(self):
        self.assertEqual(sm.family(self.current(self.transcript(SET_FABLE))), "fable")

    def test_quoted_set_model_text_is_not_a_command(self):
        """A compaction summary or pasted text quoting the output is not a switch."""
        quoted = user("the operator ran /model: <local-command-stdout>Set model to "
                      "`Opus 5`</local-command-stdout>")
        path = self.transcript(assistant("claude-sonnet-5"), quoted)
        self.assertEqual(self.current(path), "claude-sonnet-5")

    def test_synthetic_and_sidechain_entries_are_skipped(self):
        side = assistant("claude-haiku-4-5")
        side["isSidechain"] = True
        path = self.transcript(assistant("claude-sonnet-5"), assistant("<synthetic>"), side)
        self.assertEqual(self.current(path), "claude-sonnet-5")


class Fallbacks(Base):
    def test_session_start_record_used_when_transcript_has_none(self):
        record_session_model.record({"session_id": "abc-123", "model": "claude-opus-5"},
                                    record_dir=self.records)
        path = self.transcript(user("first prompt"))
        self.assertEqual(self.current(path, session_id="abc-123"), "claude-opus-5")

    def test_transcript_beats_record(self):
        record_session_model.record({"session_id": "abc", "model": "claude-opus-5"},
                                    record_dir=self.records)
        path = self.transcript(assistant("claude-sonnet-5"))
        self.assertEqual(self.current(path, session_id="abc"), "claude-sonnet-5")

    def test_record_rejects_path_like_session_ids(self):
        self.assertIsNone(record_session_model.record(
            {"session_id": "../../etc/x", "model": "opus"}, record_dir=self.records))
        self.assertFalse(self.records.exists())

    def test_settings_order_local_then_project_then_user(self):
        project = self.root / "proj"
        (project / ".git").mkdir(parents=True)
        (project / ".claude").mkdir()
        sub = project / "a" / "b"
        sub.mkdir(parents=True)
        (self.home / ".claude" / "settings.json").write_text(json.dumps({"model": "sonnet"}))
        self.assertEqual(self.current("", cwd=str(sub)), "sonnet")
        (project / ".claude" / "settings.json").write_text(json.dumps({"model": "opus"}))
        self.assertEqual(self.current("", cwd=str(sub)), "opus")
        (project / ".claude" / "settings.local.json").write_text(json.dumps({"model": "fable"}))
        self.assertEqual(self.current("", cwd=str(sub)), "fable")

    def test_nothing_resolves_to_none(self):
        self.assertIsNone(self.current(str(self.root / "missing.jsonl"), cwd=str(self.root)))

    def test_malformed_settings_are_skipped(self):
        (self.home / ".claude" / "settings.json").write_text("{not json")
        self.assertIsNone(self.current("", cwd=str(self.root)))


class RecordHookMain(Base):
    def test_main_records_the_payload_model(self):
        """Preclose fix 10: nothing tested that main() writes the record."""
        import io
        from unittest.mock import patch
        payload = {"session_id": "abc-123", "model": "claude-opus-5[1m]",
                   "hook_event_name": "SessionStart", "source": "startup"}
        with patch("sys.stdin", io.StringIO(json.dumps(payload))), \
             patch.object(sm, "SESSION_MODEL_DIR", self.records):
            record_session_model.main()
        self.assertEqual((self.records / "abc-123").read_text(encoding="utf-8"),
                         "claude-opus-5[1m]\n")
        self.assertEqual(sm.recorded_model("abc-123", self.records), "claude-opus-5[1m]")

    def test_main_never_raises_on_garbage(self):
        import io
        from unittest.mock import patch
        with patch("sys.stdin", io.StringIO("not json")):
            record_session_model.main()


if __name__ == "__main__":
    unittest.main()
