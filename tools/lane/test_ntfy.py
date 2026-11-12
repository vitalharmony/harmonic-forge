"""harmonic-forge#1338: the shared ntfy helper and the afk push rate limit (fake transport only)."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import ntfy


class PushCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        patcher = mock.patch.dict(os.environ, {"HOME": str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)
        topic = self.home / ntfy.REMINDER_TOPIC_RELPATH
        topic.parent.mkdir(parents=True)
        topic.write_text("secret-topic\n")
        self.posts = []

    def transport(self, status=200):
        def send(url, body, headers):
            self.posts.append((url, body, headers))
            return status
        return send

    def test_one_push_per_key_per_hour(self):
        t = self.transport()
        self.assertEqual(ntfy.afk_push("H1", "owed", t, now=1000)[0], 0)
        code, message = ntfy.afk_push("H1", "owed again", t, now=1000 + 3599)
        self.assertEqual(code, 0)
        self.assertIn("skipped", message)
        self.assertEqual(len(self.posts), 1)
        self.assertEqual(self.posts[0][2], {"Title": "H1"})
        ntfy.afk_push("H2", "other key", t, now=1001)
        ntfy.afk_push("H1", "next hour", t, now=1000 + 3600)
        self.assertEqual(len(self.posts), 3)

    def test_http_failure_is_nonzero_and_never_names_topic(self):
        code, message = ntfy.afk_push("H1", "owed", self.transport(500), now=1)
        self.assertNotEqual(code, 0)
        self.assertIn("500", message)
        self.assertNotIn("secret-topic", message)
        ntfy.afk_push("H1", "retry", self.transport(), now=2)
        self.assertEqual(len(self.posts), 2)  # a failure does not spend the hour

    def test_notify_swallows_failure(self):
        def boom(url, body, headers):
            raise OSError("down")
        topic = self.home / ntfy.REMINDER_TOPIC_RELPATH
        ntfy.notify("x", str(topic), "t", transport=boom)  # no raise


if __name__ == "__main__":
    unittest.main()
