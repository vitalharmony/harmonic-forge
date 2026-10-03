#!/usr/bin/env python3
"""Tests for the hrse-gate grant broker (harmonic-forge#878, Variant 2).

The store root is injected by patching the module constant in-process, never
through an argument (the broker has no path-valued argument by design).
Ownership is exercised by patching the euid and owner lookups; the real
chown to hrse-gate needs root and is covered by the live check (TC10).
"""
from __future__ import annotations

import datetime as dt
import json
import os
import stat
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import hrse_gate_broker as b  # noqa: E402

SHA = "a" * 40
OTHER_SHA = "b" * 40
RUN = "script=scripts/1-1892-revive.py,apply"
DRY = "script=scripts/1-1892-revive.py"
COUNT = "count-label=Task"


class _StoreCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        (root / "grants").mkdir()
        (root / "receipts").mkdir()
        me = os.getuid(), os.getgid()
        self._patches = [mock.patch.object(b, "STORE_ROOT", root),
                         mock.patch.object(b, "_geteuid", return_value=0),
                         mock.patch.object(b, "_owner_ids", return_value=me)]
        for p in self._patches:
            p.start()
        self.root = root

    def tearDown(self):
        for p in reversed(self._patches):
            p.stop()
        self._tmp.cleanup()

    def grant(self, action=RUN, sha=SHA, issue="1892", ttl=60):
        return b.grant(issue, sha, action, ttl)

    def refused(self, code, issue="1892", sha=SHA, action=RUN):
        with self.assertRaises(b.Refused) as ctx:
            b.consume(issue, sha, action)
        self.assertEqual(ctx.exception.code, code, str(ctx.exception))
        return str(ctx.exception)


class SingleUseTests(_StoreCase):
    def test_a_grant_is_consumed_once(self):
        nonce = self.grant()
        self.assertEqual(b.consume("1892", SHA, RUN), nonce)
        self.assertFalse((self.root / "grants" / f"{nonce}.json").exists())
        self.assertTrue((self.root / "receipts" / f"{nonce}.json").exists())
        self.assertIn("already consumed", self.refused(b.EXIT_CONSUMED))

    def test_concurrent_consume_succeeds_once(self):
        self.grant()
        results: list[object] = []
        barrier = threading.Barrier(8)

        def worker():
            barrier.wait()
            try:
                results.append(b.consume("1892", SHA, RUN))
            except b.Refused as exc:
                results.append(exc.code)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sum(1 for r in results if isinstance(r, str)), 1, results)

    def test_two_grants_buy_two_runs(self):
        self.grant()
        self.grant()
        b.consume("1892", SHA, RUN)
        b.consume("1892", SHA, RUN)
        self.refused(b.EXIT_CONSUMED)


class ExactActionTests(_StoreCase):
    def test_a_grant_for_one_script_refuses_another(self):
        self.grant(action="script=scripts/1-1892-revive.py,apply")
        self.assertIn("mismatch", self.refused(
            b.EXIT_MISMATCH, action="script=scripts/1-1891-backfill.py,apply"))

    def test_a_dry_run_grant_does_not_buy_an_apply(self):
        self.grant(action=DRY)
        self.refused(b.EXIT_MISMATCH, action=RUN)
        self.assertTrue(b.consume("1892", SHA, DRY))

    def test_an_apply_grant_does_not_buy_a_dry_run_or_a_count(self):
        self.grant(action=RUN)
        self.refused(b.EXIT_MISMATCH, action=DRY)
        self.refused(b.EXIT_MISMATCH, action=COUNT)

    def test_a_grant_at_one_sha_or_issue_refuses_another(self):
        self.grant()
        self.refused(b.EXIT_NO_GRANT, sha=OTHER_SHA)
        self.refused(b.EXIT_NO_GRANT, issue="1867")

    def test_no_grant_at_all(self):
        self.assertIn("no authorization", self.refused(b.EXIT_NO_GRANT))


class ExpiryTests(_StoreCase):
    def test_an_expired_grant_is_refused(self):
        self.grant(ttl=1)
        later = dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=2)
        with mock.patch.object(b, "_now", return_value=later):
            self.assertIn("expired", self.refused(b.EXIT_EXPIRED))
        self.assertEqual(len(list((self.root / "receipts").iterdir())), 0)

    def test_ttl_is_bounded(self):
        with self.assertRaises(b.Refused):
            self.grant(ttl=0)
        with self.assertRaises(b.Refused):
            self.grant(ttl=b.MAX_TTL_MINUTES + 1)


class GrantAuthorityTests(_StoreCase):
    def test_grant_refuses_unless_root(self):
        with mock.patch.object(b, "_geteuid", return_value=1000):
            with self.assertRaises(b.Refused) as ctx:
                self.grant()
        self.assertIn("root only", str(ctx.exception))
        self.assertEqual(list((self.root / "grants").iterdir()), [])

    def test_grant_file_is_hrse_gate_0600(self):
        with mock.patch.object(b.os, "fchown") as fchown, \
                mock.patch.object(b, "_owner_ids", return_value=(4321, 8765)):
            nonce = self.grant()
        fchown.assert_called_once()
        self.assertEqual(fchown.call_args.args[1:], (4321, 8765))
        mode = stat.S_IMODE((self.root / "grants" / f"{nonce}.json").stat().st_mode)
        self.assertEqual(mode, 0o600)

    def test_a_tampered_or_unknown_grant_file_is_ignored(self):
        (self.root / "grants" / "not-a-nonce.json").write_text(json.dumps(
            {"issue": 1892, "sha": SHA, "script": "scripts/1-1892-revive.py", "apply": True,
             "expires": "2999-01-01T00:00:00+00:00"}))
        self.refused(b.EXIT_NO_GRANT)


class ArgumentSurfaceTests(_StoreCase):
    """The sudoers rule is `consume *`, so the parser IS the rule's scope."""

    def run_main(self, *argv):
        with mock.patch("sys.stderr"), mock.patch("sys.stdout"):
            try:
                return b.main(list(argv))
            except SystemExit as exc:
                return exc.code

    def test_consume_accepts_no_path_or_unknown_token(self):
        self.grant()
        for argv in (("consume", "1892", SHA, RUN, "--store-root", "/tmp/x"),
                     ("consume", "--store-root=/tmp/x", "1892", SHA, RUN),
                     ("consume", "1892", SHA, RUN, "extra"),
                     ("consume", "1892", SHA, "script=/etc/passwd"),
                     ("consume", "1892", SHA, "script=scripts/1-x.py,apply,force"),
                     ("consume", "1892", "HEAD", RUN),
                     ("cons", "1892", SHA, RUN)):
            self.assertNotEqual(self.run_main(*argv), 0, argv)
        # The grant survived every refused call.
        self.assertEqual(len(list((self.root / "grants").iterdir())), 1)

    def test_the_parser_defines_no_path_valued_option(self):
        consume = b._parser()._subparsers._group_actions[0].choices["consume"]
        options = [s for a in consume._actions for s in a.option_strings]
        self.assertEqual(options, ["-h", "--help"])

    def test_version_prints_this_files_digest(self):
        import hashlib
        self.assertEqual(b.version(), hashlib.sha256(Path(b.__file__).read_bytes()).hexdigest())


class SudoersScopeTests(unittest.TestCase):
    def test_consume_is_the_only_password_free_verb(self):
        doc = (HERE / "install-hrse-gate.md").read_text(encoding="utf-8")
        rules = [line for line in doc.splitlines() if "NOPASSWD" in line]
        self.assertEqual(len(rules), 1, rules)
        self.assertIn("NOPASSWD: /usr/local/libexec/hrse-gate consume *", rules[0])
        self.assertNotIn("grant", rules[0])


if __name__ == "__main__":
    unittest.main()
