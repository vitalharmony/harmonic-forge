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

    def cli(self, *argv, env_lane=None):
        env = {"LANE": env_lane} if env_lane is not None else {}
        with mock.patch.dict(os.environ, env), mock.patch.object(ntfy, "post",
                                                                 side_effect=lambda *a, **k: self.posts.append(a) or 200):
            if env_lane is None:
                os.environ.pop("LANE", None)
            return ntfy.main(["push", *argv])

    def test_key_is_lane_plus_issue(self):
        self.assertEqual(self.cli("--lane", "2", "--key", "H1", "--text", "owed"), 0)
        self.assertEqual(self.cli("--lane", "3", "--key", "H1", "--text", "owed"), 0)
        self.assertEqual(len(self.posts), 2)  # Lane 2 and Lane 3 on one issue both send
        self.cli("--lane", "2", "--key", "H1", "--text", "again")
        self.assertEqual(len(self.posts), 2)  # same lane within the hour sends once
        self.assertEqual([p[2] for p in self.posts], ["2:H1", "3:H1"])

    def test_lane_defaults_to_env_then_none(self):
        self.cli("--key", "H5", "--text", "x", env_lane="2")
        self.cli("--key", "H5", "--text", "x")
        self.assertEqual([p[2] for p in self.posts], ["2:H5", "none:H5"])

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

    def test_notify_reports_non_2xx_without_topic(self):
        import contextlib, io
        topic = self.home / ntfy.REMINDER_TOPIC_RELPATH
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            ntfy.notify("x", str(topic), "t", transport=self.transport(500), label="orphan_reap: ")
        self.assertIn("orphan_reap: ntfy not sent: HTTP 500", err.getvalue())
        self.assertNotIn("secret-topic", err.getvalue())


if __name__ == "__main__":
    unittest.main()
