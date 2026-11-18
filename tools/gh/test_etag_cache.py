"""harmonic-forge#1393: the ETag cache, driven through the real `gh_as_raw` against a fake
GitHub (the one thing replaced is the `gh-as` subprocess)."""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _etag_cache  # noqa: E402
from belt_mechanics import CallCounter  # noqa: E402


class FakeGitHub:
    """`resources` maps a path to (etag, body). Records every request."""

    def __init__(self):
        self.resources = {}
        self.logins = {"vitalharmony": "vitalharmony", "other": "other"}
        self.requests = []          # (account, path, if_none_match)
        self.fail = {}              # path -> (status line, extra stdout) overrides
        self.raise_on = {}          # path -> exception

    def __call__(self, cmd, **kw):
        assert cmd[0] == "gh-as", cmd
        account, argv = cmd[1], cmd[3:]
        path = [a for a in argv if not a.startswith("-") and a not in ("api", "GET")][0]
        inm = None
        if "-H" in argv:
            inm = argv[argv.index("-H") + 1].split(": ", 1)[1]
        self.requests.append((account, path, inm))
        if path in self.raise_on:
            raise self.raise_on[path]
        if path == "user":
            body = json.dumps({"login": self.logins.get(account)}) if self.logins.get(account) else "{}"
            return self._p(f"HTTP/2.0 200 OK\r\nEtag: W/\"u\"\r\n\r\n{body}", 0)
        if path in self.fail:
            status, body = self.fail[path]
            return self._p(f"HTTP/2.0 {status}\r\n\r\n{body}", 1)
        etag, body = self.resources[path]
        if inm == etag:
            return self._p("HTTP/2.0 304 Not Modified\r\nEtag: " + etag.replace("W/", "") + "\r\n\r\n", 1)
        hdr = f"HTTP/2.0 200 OK\r\nEtag: {etag}\r\nX-Ratelimit-Used: 5\r\nX-Ratelimit-Reset: 9\r\n\r\n"
        return self._p(hdr + body, 0)

    @staticmethod
    def _p(stdout, returncode):
        return type("P", (), {"returncode": returncode, "stdout": stdout, "stderr": ""})()


class CacheCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {"HOME": tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        _etag_cache._IDENTITY.clear()
        self.addCleanup(_etag_cache._IDENTITY.clear)
        self.gh = FakeGitHub()
        sp = mock.patch("belt_mechanics.subprocess.run", side_effect=self.gh)
        sp.start()
        self.addCleanup(sp.stop)
        self.counter = CallCounter()
        self.gh.resources["repos/o/r/issues/1"] = ('W/"v1"', '{"n": 1}')

    def get(self, path="repos/o/r/issues/1", account="vitalharmony"):
        return _etag_cache.conditional_get(account, path, self.counter)

    def reads(self, path="repos/o/r/issues/1"):
        return [r for r in self.gh.requests if r[1] == path]

    # AC1
    def test_a_second_read_of_an_unchanged_resource_sends_if_none_match_and_returns_the_first_body(self):
        first = self.get()
        second = self.get()
        self.assertEqual((first.status, first.body, first.cached), (200, '{"n": 1}', False))
        self.assertEqual((second.status, second.body, second.cached), (304, '{"n": 1}', True))
        self.assertEqual([r[2] for r in self.reads()], [None, 'W/"v1"'])
        self.assertEqual((self.counter.calls_304, self.counter.calls_conditional), (1, 1))

    def test_a_changed_resource_returns_the_new_body_and_replaces_the_entry(self):
        self.get()
        self.gh.resources["repos/o/r/issues/1"] = ('W/"v2"', '{"n": 2}')
        changed = self.get()
        self.assertEqual((changed.status, changed.body), (200, '{"n": 2}'))
        again = self.get()
        self.assertEqual((again.status, again.body, again.cached), (304, '{"n": 2}', True))

    # failure rules: only a real 304 returns the cached body
    def test_a_403_or_5xx_never_returns_the_cached_body(self):
        self.get()
        for status in ("403 Forbidden", "500 Server Error", "502 Bad Gateway", "429 Too Many Requests"):
            self.gh.fail["repos/o/r/issues/1"] = (status, '{"message":"x"}')
            resp = self.get()
            self.assertIsNone(resp.body, status)
            self.assertFalse(resp.cached, status)

    def test_a_timeout_never_returns_the_cached_body(self):
        self.get()
        self.gh.raise_on["repos/o/r/issues/1"] = subprocess.TimeoutExpired("gh", 60)
        resp = self.get()
        self.assertEqual((resp.status, resp.body, resp.cached), (None, None, False))

    def test_a_404_or_410_reads_as_missing_with_its_status(self):
        self.get()
        for status in (404, 410):
            self.gh.fail["repos/o/r/issues/1"] = (f"{status} Gone", "{}")
            resp = self.get()
            self.assertEqual((resp.status, resp.body), (status, None))

    def test_a_secondary_limit_403_is_counted(self):
        self.gh.fail["repos/o/r/issues/1"] = ("403 Forbidden", '{"message":"secondary rate limit"}')
        resp = self.get()
        self.assertIsNone(resp.body)
        self.assertEqual(self.counter.calls_403_secondary, 1)

    # the cache file
    def test_a_corrupt_entry_is_a_plain_unconditional_read_that_rewrites_it(self):
        self.get()
        entry = next(_etag_cache.cache_root().rglob("*.json"))
        entry.write_text("{not json")
        resp = self.get()
        self.assertEqual((resp.status, resp.body), (200, '{"n": 1}'))
        self.assertIsNone(self.reads()[-1][2])
        self.assertEqual(self.get().status, 304)

    def test_an_entry_with_the_wrong_shape_is_a_miss(self):
        self.get()
        entry = next(_etag_cache.cache_root().rglob("*.json"))
        entry.write_text(json.dumps({"etag": 5}))
        self.assertEqual(self.get().status, 200)

    def test_the_query_is_part_of_the_key(self):
        self.gh.resources["repos/o/r/issues/1/comments?per_page=100&page=1"] = ('W/"p1"', "[1]")
        self.gh.resources["repos/o/r/issues/1/comments?per_page=100&page=2"] = ('W/"p2"', "[2]")
        for path in self.gh.resources:
            self.get(path)
        for path in list(self.gh.resources)[1:]:
            self.assertEqual(self.get(path).status, 304, path)
        self.assertEqual(len(list(_etag_cache.cache_root().rglob("*.json"))), 3)

    def test_the_credential_is_part_of_the_key(self):
        self.get(account="vitalharmony")
        self.gh.logins["other"] = "someone-else"
        other = self.get(account="other")
        self.assertEqual(other.status, 200)
        self.assertIsNone(self.reads()[-1][2], "an entry from another credential must not be replayed")

    def test_two_logins_on_one_slot_do_not_share_an_entry(self):
        self.get()
        _etag_cache._IDENTITY.clear()
        self.gh.logins["vitalharmony"] = "renamed-login"
        self.assertEqual(self.get().status, 200)

    def test_an_unreadable_login_means_no_cache_at_all(self):
        self.gh.logins["vitalharmony"] = None
        self.assertEqual(self.get().status, 200)
        self.assertEqual(self.get().status, 200)
        self.assertEqual([r[2] for r in self.reads()], [None, None])
        self.assertFalse(list(_etag_cache.cache_root().rglob("*.json")))

    def test_a_response_with_no_etag_is_not_cached(self):
        def no_etag(cmd, **kw):
            if cmd[-1] == "user":
                return FakeGitHub._p('HTTP/2.0 200 OK\r\n\r\n{"login": "vitalharmony"}', 0)
            return FakeGitHub._p("HTTP/2.0 200 OK\r\n\r\n[]", 0)
        with mock.patch("belt_mechanics.subprocess.run", side_effect=no_etag):
            self.assertEqual(_etag_cache.conditional_get("vitalharmony", "repos/o/r/x", self.counter).status, 200)
        self.assertFalse(list(_etag_cache.cache_root().rglob("*.json")))

    def test_the_identity_is_read_once_per_process(self):
        self.get()
        self.get()
        self.assertEqual(len([r for r in self.gh.requests if r[1] == "user"]), 1)

    # AC6: pruning
    def test_prune_removes_entries_unused_for_a_day_and_keeps_used_ones(self):
        self.gh.resources["repos/o/r/issues/2"] = ('W/"x"', "{}")
        self.get("repos/o/r/issues/1")
        self.get("repos/o/r/issues/2")
        files = sorted(_etag_cache.cache_root().rglob("*.json"))
        old = time.time() - 25 * 3600
        os.utime(files[0], (old, old))
        self.assertEqual(_etag_cache.prune(), 1)
        self.assertEqual(len(list(_etag_cache.cache_root().rglob("*.json"))), 1)

    def test_a_use_refreshes_the_mtime_so_a_used_entry_survives_the_prune(self):
        self.get()
        entry = next(_etag_cache.cache_root().rglob("*.json"))
        old = time.time() - 25 * 3600
        os.utime(entry, (old, old))
        self.get()  # a 304 touches the entry
        self.assertEqual(_etag_cache.prune(), 0)

    def test_prune_survives_a_file_deleted_by_another_process(self):
        self.get()
        entry = next(_etag_cache.cache_root().rglob("*.json"))
        old = time.time() - 25 * 3600
        os.utime(entry, (old, old))
        real_unlink = Path.unlink

        def racing_unlink(self_path, *a, **k):
            real_unlink(self_path, *a, **k)      # the other process got there first...
            return real_unlink(self_path, *a, **k)  # ...so ours raises FileNotFoundError
        with mock.patch.object(Path, "unlink", racing_unlink):
            self.assertEqual(_etag_cache.prune(), 0)

    def test_prune_with_no_cache_directory_is_a_no_op(self):
        self.assertEqual(_etag_cache.prune(), 0)


if __name__ == "__main__":
    unittest.main()
