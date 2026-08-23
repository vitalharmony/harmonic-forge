#!/usr/bin/env python3
"""Unit tests for l2_post.py (harmonic-forge#371).

Run: python3 tools/gh/test_l2_post.py
"""
import json
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import l2_post as lp
import receipt_runner as rr


def _fake_run(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


class TestComposeBody(unittest.TestCase):
    def test_labels_map_correctly(self):
        for kind, label in (("plan", "L2P"), ("completion", "L2D"), ("blocked", "L2B")):
            body = lp.compose_body(kind, [], "narrative text")
            self.assertIn(label, body)
            self.assertIn("narrative text", body)
            self.assertIn("Verified receipts", body)

    def test_receipts_embedded_as_json(self):
        receipts = [{"argv": ["echo", "hi"], "exit_code": 0}]
        body = lp.compose_body("completion", receipts, "n")
        self.assertIn('"echo"', body)
        self.assertIn('"exit_code": 0', body)


class TestPostSelfCheck(unittest.TestCase):
    def test_post_succeeds_when_refetch_matches(self):
        posted = {"id": 555, "html_url": "https://example/555"}
        refetched = {"body": "hello"}
        with unittest.mock.patch.object(lp, "_gh_api") as fake:
            fake.side_effect = [
                _fake_run(0, stdout=json.dumps(posted)),
                _fake_run(0, stdout=json.dumps(refetched)),
            ]
            result = lp.post("o/r", 1, "hello")
        self.assertEqual(result["comment_id"], 555)
        self.assertEqual(result["body_sha256"], lp._sha("hello"))

    def test_post_refuses_success_on_body_mismatch(self):
        """The core integrity property this issue exists for: a landed
        comment whose body doesn't match what was sent must never be
        reported as a success."""
        posted = {"id": 556, "html_url": "https://example/556"}
        refetched = {"body": "something else entirely"}
        with unittest.mock.patch.object(lp, "_gh_api") as fake:
            fake.side_effect = [
                _fake_run(0, stdout=json.dumps(posted)),
                _fake_run(0, stdout=json.dumps(refetched)),
            ]
            with self.assertRaises(SystemExit):
                lp.post("o/r", 1, "hello")

    def test_post_fails_when_initial_post_transport_fails(self):
        with unittest.mock.patch.object(lp, "_gh_api") as fake:
            fake.return_value = _fake_run(1, stderr="network error")
            with self.assertRaises(SystemExit):
                lp.post("o/r", 1, "hello")

    def test_post_fails_when_refetch_itself_fails(self):
        posted = {"id": 557, "html_url": "https://example/557"}
        with unittest.mock.patch.object(lp, "_gh_api") as fake:
            fake.side_effect = [
                _fake_run(0, stdout=json.dumps(posted)),
                _fake_run(1, stderr="not found"),
            ]
            with self.assertRaises(SystemExit):
                lp.post("o/r", 1, "hello")


class TestResolveLock(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        subprocess.run(["git", "init", "-q"], cwd=self._tmp.name, check=True)
        import os
        self._cwd = Path.cwd()
        os.chdir(self._tmp.name)

    def tearDown(self):
        import os
        os.chdir(self._cwd)
        self._tmp.cleanup()

    def test_resolve_lock_refused_without_fetchable_comment(self):
        rr.run_command(4242, ["false"])
        self.assertTrue(rr.is_locked(4242))
        with unittest.mock.patch.object(lp, "_gh_api") as fake:
            fake.return_value = _fake_run(1, stderr="404")
            with self.assertRaises(SystemExit):
                lp.resolve_lock("o/r", 4242, 999)
        self.assertTrue(rr.is_locked(4242))

    def test_resolve_lock_clears_when_comment_is_real(self):
        rr.run_command(4243, ["false"])
        self.assertTrue(rr.is_locked(4243))
        with unittest.mock.patch.object(lp, "_gh_api") as fake:
            fake.return_value = _fake_run(0, stdout='{"id": 999, "body": "resolved"}')
            lp.resolve_lock("o/r", 4243, 999)
        self.assertFalse(rr.is_locked(4243))


class TestSnapshot(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        subprocess.run(["git", "init", "-q"], cwd=self._tmp.name, check=True)
        import os
        self._cwd = Path.cwd()
        os.chdir(self._tmp.name)

    def tearDown(self):
        import os
        os.chdir(self._cwd)
        self._tmp.cleanup()

    def test_snapshot_records_comment_ids_and_body_hashes(self):
        comments = [{"id": 1, "body": "a"}, {"id": 2, "body": "b"}]
        raw = json.dumps(comments)
        with unittest.mock.patch.object(lp, "_gh_api") as fake:
            fake.return_value = _fake_run(0, stdout=raw)
            body = lp.snapshot("o/r", 7001)
        self.assertEqual(body["comment_ids"], [1, 2])
        self.assertEqual(body["comment_body_sha256"]["1"], lp._sha("a"))
        self.assertEqual(body["raw_response_sha256"], lp._sha(raw))


if __name__ == "__main__":
    unittest.main()
