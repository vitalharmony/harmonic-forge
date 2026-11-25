#!/usr/bin/env python3
"""Unit tests for post_comment.py's deferral own-comment record
(harmonic-forge#1564 AC15). `subprocess.run` is mocked: no gh call is made."""
import io
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import deferral_store  # noqa: E402  (harmonic-forge#1564)
import post_comment  # noqa: E402

REPO = "vitalharmony/hrse"
ISSUE = 2757
COMMENT_ID = "7153999999"
BODY = "Not yet: hrse#1810 is still open.\n"


class PostCommentNotesOwnCommentTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.body_file = self.base / "body.md"
        self.body_file.write_text(BODY, encoding="utf-8")
        for patcher in (mock.patch.object(deferral_store, "DEFAULT_CANDIDATES_DIR", self.base),
                        mock.patch.object(post_comment.tasklist_owed, "record_post")):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _post(self, refetched: str) -> int:
        def run(cmd, **_kwargs):
            if "POST" in cmd:
                return mock.Mock(returncode=0, stdout=f"https://github.com/{REPO}/issues/{ISSUE}"
                                                      f"#issuecomment-{COMMENT_ID}\n", stderr="")
            return mock.Mock(returncode=0, stdout=refetched, stderr="")
        with mock.patch.object(post_comment.subprocess, "run", side_effect=run), \
             redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            return post_comment.post_comment(REPO, ISSUE, self.body_file, BODY)

    def _ids(self):
        entry = deferral_store.deferral_entry(REPO, ISSUE)
        return None if entry is None else entry["own_comment_ids"]

    def test_self_check_pass_records_the_id(self):
        deferral_store.record_deferral(REPO, ISSUE, "filing")
        self.assertEqual(self._post(BODY), 0)
        self.assertEqual(self._ids(), [COMMENT_ID])

    def test_self_check_failure_still_records_the_id(self):
        deferral_store.record_deferral(REPO, ISSUE, "filing")
        self.assertEqual(self._post("mangled"), 1)
        self.assertEqual(self._ids(), [COMMENT_ID])

    def test_no_recorded_deferral_creates_no_entry(self):
        self.assertEqual(self._post(BODY), 0)
        self.assertIsNone(deferral_store.deferral_entry(REPO, ISSUE))
        self.assertEqual(list((self.base / deferral_store.DEFERRALS_SUBDIR).glob("*.json"))
                         if (self.base / deferral_store.DEFERRALS_SUBDIR).exists() else [], [])

    def test_a_record_failure_never_fails_the_post(self):
        with mock.patch.object(deferral_store, "note_own_comment", side_effect=RuntimeError("boom")):
            self.assertEqual(self._post(BODY), 0)


if __name__ == "__main__":
    unittest.main()
