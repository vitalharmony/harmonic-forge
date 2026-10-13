#!/usr/bin/env python3
"""AC5 (harmonic-forge#895): `imc_waiver.py record` refuses on every missing
precondition, and writes its receipt only after a refetch finds the comment.
Every state path is a temp dir; `gh` is a fake runner."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "hooks"))
import batch_auth  # noqa: E402
import imc_waiver  # noqa: E402

KEY = "F892"
URL = "https://github.com/vitalharmony/harmonic-forge/issues/892#issuecomment-123"


class FakeGh:
    """POST returns URL; the refetch returns whatever `fetched` says."""

    def __init__(self, post_ok: bool = True, fetched: str | None = None) -> None:
        self.calls: list[list[str]] = []
        self.post_ok = post_ok
        self.fetched = fetched
        self.posted_body: str | None = None

    def __call__(self, argv, **_kw):
        self.calls.append(list(argv))
        if "--method" in argv:
            body_arg = argv[argv.index("-F") + 1]
            self.posted_body = Path(body_arg.split("@", 1)[1]).read_text(encoding="utf-8")
            if not self.post_ok:
                return subprocess.CompletedProcess(argv, 1, "", "HTTP 403")
            return subprocess.CompletedProcess(argv, 0, URL + "\n", "")
        body = self.posted_body if self.fetched is None else self.fetched
        return subprocess.CompletedProcess(argv, 0, (body or "") + "\n", "")


class RecordTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.state = self.tmp / "imc"
        self.batch = self.tmp / "batch-authorized.json"
        for target, value in ((imc_waiver, ("IMC_STATE_DIR", self.state)),
                              (batch_auth, ("STATE_PATH", self.batch))):
            patcher = patch.object(target, *value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.now = datetime.now(timezone.utc)
        lane = patch.dict(imc_waiver.os.environ, {"LANE": "1"})
        lane.start()
        self.addCleanup(lane.stop)

    def hook_record(self, granted: datetime | None = None) -> None:
        self.state.mkdir(parents=True, exist_ok=True)
        (self.state / f"{KEY}.json").write_text(json.dumps({
            "key": KEY, "repo": "vitalharmony/harmonic-forge", "issue": 892,
            "granted_at": imc_waiver._iso(granted or self.now), "session_id": "sess-1",
            "line": "BATCH F892 IMC F892", "hook_version": "1"}))

    def grant(self) -> None:
        batch_auth.authorize([KEY], actions=["gh pr merge"], state_path=self.batch)

    def record(self, branch: str = "fix/892-imc-waiver", gh: FakeGh | None = None):
        return imc_waiver.record(KEY, branch=branch, now=self.now,
                                 batch_state_path=self.batch, runner=gh or FakeGh())

    def receipt_exists(self) -> bool:
        return (self.state / f"{KEY}.waiver.json").exists()

    def test_refuses_outside_lane_1(self) -> None:
        self.hook_record()
        with patch.dict(imc_waiver.os.environ, {"LANE": "2"}):
            with self.assertRaises(SystemExit) as refused:
                imc_waiver.record("F892", branch="fix/892-x", batch_state_path=self.batch)
        self.assertIn("LANE", str(refused.exception))

    def test_refuses_with_no_hook_record(self) -> None:
        self.grant()
        with self.assertRaises(SystemExit) as refused:
            self.record()
        self.assertIn("no IMC hook record", str(refused.exception))
        self.assertFalse(self.receipt_exists())

    def test_refuses_with_an_expired_hook_record(self) -> None:
        self.hook_record(self.now - timedelta(hours=12, minutes=1))
        self.grant()
        with self.assertRaises(SystemExit) as refused:
            self.record()
        self.assertIn("expired", str(refused.exception))
        self.assertFalse(self.receipt_exists())

    def test_refuses_with_no_live_batch_grant(self) -> None:
        self.hook_record()
        with self.assertRaises(SystemExit) as refused:
            self.record()
        self.assertIn("no live BATCH merge grant", str(refused.exception))

    def test_refuses_with_a_consumed_grant(self) -> None:
        self.hook_record()
        self.grant()
        state = json.loads(self.batch.read_text())
        for target in state[KEY]["targets"]:
            target["consumed"] = True
        self.batch.write_text(json.dumps(state))
        with self.assertRaises(SystemExit):
            self.record()

    def test_refuses_on_a_wrong_branch_shape(self) -> None:
        self.hook_record()
        self.grant()
        for branch in ("l1/f892-imc", "fix/893-other", "fix/8920-x", "main", "feat/892_x", ""):
            with self.subTest(branch=branch):
                gh = FakeGh()
                with self.assertRaises(SystemExit) as refused:
                    self.record(branch=branch, gh=gh)
                self.assertIn("does not match", str(refused.exception))
                self.assertEqual(gh.calls, [], "nothing is posted on a refusal")
        self.assertFalse(self.receipt_exists())

    def test_posts_refetches_then_writes_receipt(self) -> None:
        self.hook_record()
        self.grant()
        gh = FakeGh()
        receipt = self.record(branch="feat/892-imc", gh=gh)
        self.assertEqual(len(gh.calls), 2)
        self.assertIn("repos/vitalharmony/harmonic-forge/issues/892/comments", gh.calls[0])
        self.assertIn("repos/vitalharmony/harmonic-forge/issues/comments/123", gh.calls[1])
        body = gh.posted_body or ""
        self.assertTrue(body.startswith("## IMC waiver — F892"))
        for part in ("**Who:**", "sess-1", "**When:**", "R-0229", "R-0230",
                     "kill-check receipt", "cross-family", "**What is not waived:** everything else"):
            self.assertIn(part, body)
        saved = json.loads((self.state / f"{KEY}.waiver.json").read_text())
        self.assertEqual(saved, receipt)
        self.assertEqual(saved["comment_url"], URL)
        self.assertEqual(saved["status"], "recorded")
        self.assertEqual(saved["branch"], "feat/892-imc")
        hook = json.loads((self.state / f"{KEY}.json").read_text())
        self.assertEqual(saved["granted_at"], hook["granted_at"])

    def saved(self) -> dict:
        return json.loads((self.state / f"{KEY}.waiver.json").read_text())

    def test_receipt_stays_pending_when_refetch_does_not_find_comment(self) -> None:
        self.hook_record()
        self.grant()
        with self.assertRaises(SystemExit) as refused:
            self.record(gh=FakeGh(fetched="something else"))
        self.assertIn("refetch", str(refused.exception))
        self.assertEqual(self.saved()["status"], "pending")
        self.assertIsNone(self.saved()["comment_url"])

    def test_receipt_stays_pending_when_post_fails(self) -> None:
        self.hook_record()
        self.grant()
        with self.assertRaises(SystemExit):
            self.record(gh=FakeGh(post_ok=False))
        self.assertEqual(self.saved()["status"], "pending")

    def test_pending_receipt_is_written_before_the_post(self) -> None:
        self.hook_record()
        self.grant()
        seen: list = []

        class Crash(FakeGh):
            def __call__(inner, argv, **kw):
                seen.append(imc_waiver.waiver_receipt("vitalharmony/harmonic-forge", 892))
                raise KeyboardInterrupt("crash mid-post")

        with self.assertRaises(KeyboardInterrupt):
            self.record(gh=Crash())
        self.assertEqual(seen[0]["status"], "pending")
        self.assertIsNotNone(imc_waiver.waiver_receipt("vitalharmony/harmonic-forge", 892))

    def test_crash_after_post_keeps_receipt_and_retry_completes(self) -> None:
        self.hook_record()
        self.grant()

        class CrashOnRefetch(FakeGh):
            def __call__(inner, argv, **kw):
                if "--method" not in argv:
                    raise KeyboardInterrupt("crash after post")
                return super().__call__(argv, **kw)

        with self.assertRaises(KeyboardInterrupt):
            self.record(gh=CrashOnRefetch())
        self.assertEqual(self.saved()["status"], "pending")
        receipt = self.record()
        self.assertEqual(receipt["status"], "recorded")
        self.assertEqual(self.saved()["comment_url"], URL)

    def test_unreadable_receipt_counts_as_present(self) -> None:
        self.state.mkdir(parents=True, exist_ok=True)
        path = self.state / f"{KEY}.waiver.json"
        for content in ('{"key": "F89', '["a list"]', ""):
            path.write_text(content)
            self.assertEqual(imc_waiver.waiver_receipt("vitalharmony/harmonic-forge", 892),
                             {"unreadable": True}, content)
        path.unlink()
        self.assertIsNone(imc_waiver.waiver_receipt("vitalharmony/harmonic-forge", 892))

    def test_cross_account_key_refused(self) -> None:
        with self.assertRaises(SystemExit) as refused:
            imc_waiver.record("K12", branch="fix/12-x", now=self.now,
                              batch_state_path=self.batch, runner=FakeGh())
        self.assertIn("not a vitalharmony repo", str(refused.exception))


class StateDirTests(unittest.TestCase):
    def test_default_state_dir_resolves_home_at_call_time(self) -> None:
        with tempfile.TemporaryDirectory() as home, patch.dict("os.environ", {"HOME": home}):
            self.assertEqual(imc_waiver.state_dir(), Path(home) / ".claude" / "state" / "imc")


if __name__ == "__main__":
    unittest.main()
