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


class EndGh(FakeGh):
    """Adds the comment-list read `end` uses to find a prior post."""

    def __init__(self, existing: list[tuple[str, str]] | None = None, **kw) -> None:
        super().__init__(**kw)
        self.existing = existing or []

    def __call__(self, argv, **kw):
        if list(argv[:3]) == ["gh", "api", "user"]:
            return subprocess.CompletedProcess(argv, 0, "vitalharmony\n", "")
        if "--paginate" in argv:
            self.calls.append(list(argv))
            out = "".join(json.dumps([e[0], e[1], e[2] if len(e) > 2 else "vitalharmony"]) + "\n"
                          for e in self.existing)
            return subprocess.CompletedProcess(argv, 0, out, "")
        return super().__call__(argv, **kw)


def sticky_receipt() -> dict:
    """Two passes after the waiver's granted_at (2026-10-17T01:02:03Z), both
    with the forced cross-family call -- the IMC stop (pass 1, F1042)."""
    run = {"completed_at": "2026-10-17T02:00:00Z", "cross_family_ran": True}
    return {"status": "complete", "pass_history": [
        {"sha": "a", "surviving": 2, **run}, {"sha": "b", "surviving": 1, **run}]}


class EndTests(unittest.TestCase):
    """harmonic-forge#1042: `imc_waiver.py end`."""

    def setUp(self) -> None:
        import preclose_check  # noqa: PLC0415
        self.pc = preclose_check
        self.tmp = Path(tempfile.mkdtemp())
        self.state = self.tmp / "imc"
        self.batch = self.tmp / "batch-authorized.json"
        for target, value in ((imc_waiver, ("IMC_STATE_DIR", self.state)),
                              (batch_auth, ("STATE_PATH", self.batch))):
            patcher = patch.object(target, *value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.preclose = sticky_receipt()
        finder = patch.object(preclose_check, "find_receipt",
                              lambda repo, issue: self.preclose)
        finder.start()
        self.addCleanup(finder.stop)
        self.now = datetime(2026, 10, 17, 1, 2, 3, tzinfo=timezone.utc)
        self.state.mkdir(parents=True)
        (self.state / f"{KEY}.json").write_text(json.dumps(
            {"key": KEY, "granted_at": imc_waiver._iso(self.now)}))
        (self.state / f"{KEY}.waiver.json").write_text(json.dumps(
            {"key": KEY, "status": "recorded", "comment_url": URL,
             "granted_at": imc_waiver._iso(self.now)}))
        batch_auth.authorize([KEY], actions=["gh pr merge"], state_path=self.batch)

    def end(self, gh=None, now=None):
        return imc_waiver.end(KEY, reason="sticky-wicket PATCH", now=now or self.now,
                              batch_state_path=self.batch, runner=gh or EndGh())

    def archived(self, kind: str = "waiver") -> list[Path]:
        return sorted((self.state / "archive").glob(f"{KEY}.{kind}.*.json"))

    def test_end_archives_receipt_and_posts(self) -> None:
        archive = self.state / "archive" / f"{KEY}.waiver.20261017T010203Z.json"
        seen: dict = {}

        class Watch(EndGh):
            def __call__(self, argv, **kw):
                if "--method" in argv:
                    seen["at_post"] = json.loads(archive.read_text()) if archive.exists() else None
                    live = archive.parent.parent / f"{KEY}.waiver.json"
                    seen["live_at_post"] = json.loads(live.read_text()) if live.exists() else None
                return super().__call__(argv, **kw)

        gh = Watch()
        final = self.end(gh)
        # Pass 1 (fail-direction): IMC stays enforced -- the live receipt is
        # still in place, marked `ending` -- until the post has succeeded.
        self.assertIsNone(seen["at_post"])
        self.assertEqual(seen["live_at_post"]["status"], "ending")
        self.assertEqual(seen["live_at_post"]["comment_url"], URL)
        self.assertFalse((self.state / f"{KEY}.waiver.json").exists())
        files = self.archived()
        self.assertEqual([p.name for p in files], [f"{KEY}.waiver.20261017T010203Z.json"])
        saved = json.loads(files[0].read_text())
        self.assertEqual(saved, final)
        self.assertEqual(saved["status"], "ended")
        self.assertEqual(saved["end_comment_url"], URL)
        body = gh.posted_body or ""
        self.assertTrue(body.startswith("## IMC ended — F892"))
        self.assertIn("sticky-wicket PATCH", body)
        self.assertIn(URL, body)
        self.assertIn("R-0229", body)
        self.assertIsNone(imc_waiver.waiver_receipt("vitalharmony/harmonic-forge", 892))

    def test_imc_refusal_does_not_fire_after_end(self) -> None:
        with self.assertRaises(SystemExit):
            self.pc.imc_refusal("vitalharmony/harmonic-forge", 892, "--force")
        self.end()
        self.pc.imc_refusal("vitalharmony/harmonic-forge", 892, "--force")
        self.assertFalse(self.pc.imc_forced("vitalharmony/harmonic-forge", 892))

    def test_end_refuses_at_pass_count_1(self) -> None:
        # Under-waiver pass (pass 2, test-honesty): only the count can refuse it.
        self.preclose = {"status": "complete", "pass_history": [
            {"sha": "a", "surviving": 2, "completed_at": "2026-10-17T02:00:00Z", "cross_family_ran": True}]}
        gh = EndGh()
        with self.assertRaises(SystemExit) as raised:
            self.end(gh)
        self.assertIn("not at an IMC stop", str(raised.exception))
        self.assertTrue((self.state / f"{KEY}.waiver.json").exists())
        self.assertEqual(gh.calls, [])
        self.assertTrue(imc_waiver.live_merge_grant(KEY, self.batch))

    def test_end_refuses_with_no_preclose_receipt(self) -> None:
        self.preclose = None
        with self.assertRaises(SystemExit):
            self.end()

    def test_end_refuses_one_waived_pass_even_if_cap_message_says_stop(self) -> None:
        """Reforge pass 2 sticky-wicket: only the pass count may refuse here.
        cap_message is pinned to STICKY_WICKET, so the `len(waived) >=
        MAX_PASSES` check is the deciding condition."""
        import preclose_passes  # noqa: PLC0415
        self.preclose = {"status": "complete", "pass_history": [
            {"sha": "a", "surviving": 2, "completed_at": "2026-10-17T02:00:00Z", "cross_family_ran": True}]}
        with patch.object(preclose_passes, "cap_message",
                          return_value=preclose_passes.STICKY_WICKET):
            with self.assertRaises(SystemExit) as refused:
                self.end()
        self.assertIn("not at an IMC stop", str(refused.exception))
        self.assertTrue((self.state / f"{KEY}.waiver.json").exists())

    def test_end_refuses_a_ruled_cluster_without_a_waiver_era_pass(self) -> None:
        """Reforge pass 2 (test honesty): a current-epoch PATCH cluster whose
        passes all ran before the waiver, or without the forced cross-family
        call, is no IMC stop. Only `and waived` can refuse it."""
        for entry in ({"completed_at": "2026-10-17T00:00:00Z", "cross_family_ran": True},
                      {"completed_at": "2026-10-17T02:00:00Z", "cross_family_ran": False}):
            self.preclose = {"status": "complete",
                             "pass_history": [{"sha": "a", "surviving": 2, **entry}],
                             "mechanism_cluster": {"epoch": 0, "verdict": "PATCH"}}
            gh = EndGh()
            with self.assertRaises(SystemExit) as refused:
                self.end(gh)
            self.assertIn("not at an IMC stop", str(refused.exception))
            self.assertTrue((self.state / f"{KEY}.waiver.json").exists())
            self.assertEqual(gh.calls, [])

    def test_end_refuses_when_second_pass_was_clean(self) -> None:
        self.preclose = {"status": "complete", "pass_history": [
            {"sha": "a", "surviving": 2, "completed_at": "2026-10-17T02:00:00Z", "cross_family_ran": True}, {"sha": "b", "surviving": 0, "completed_at": "2026-10-17T02:00:00Z", "cross_family_ran": True}]}
        with self.assertRaises(SystemExit):
            self.end()

    def test_end_accepts_a_current_epoch_cluster_verdict(self) -> None:
        self.preclose = {"status": "complete", "pass_history": [
            {"sha": "a", "surviving": 2, "completed_at": "2026-10-17T02:00:00Z",
             "cross_family_ran": True}],
                         "mechanism_cluster": {"epoch": 0, "verdict": "PATCH"}}
        self.assertEqual(self.end()["status"], "ended")

    def test_end_refuses_passes_from_before_the_waiver(self) -> None:
        """Pass 1 (silent-bypass): two survivor passes recorded before
        granted_at, or without the forced cross-family call, are no IMC stop."""
        early = {"completed_at": "2026-10-17T00:00:00Z", "cross_family_ran": True}
        self.preclose = {"status": "complete", "pass_history": [
            {"sha": "a", "surviving": 2, **early}, {"sha": "b", "surviving": 1, **early}]}
        with self.assertRaises(SystemExit) as refused:
            self.end()
        self.assertIn("not at an IMC stop", str(refused.exception))
        self.assertTrue((self.state / f"{KEY}.waiver.json").exists())
        late_no_cf = {"completed_at": "2026-10-17T02:00:00Z", "cross_family_ran": False}
        self.preclose["pass_history"] = [{"sha": "a", "surviving": 2, **late_no_cf},
                                         {"sha": "b", "surviving": 1, **late_no_cf}]
        with self.assertRaises(SystemExit):
            self.end()
        self.assertTrue((self.state / f"{KEY}.waiver.json").exists())

    def test_end_refuses_an_unruled_or_stale_epoch_cluster(self) -> None:
        self.preclose = {"status": "complete", "pass_history": [
            {"sha": "a", "surviving": 2, "epoch": 1, "completed_at": "2026-10-17T02:00:00Z", "cross_family_ran": True}],
            "mechanism_cluster": {"epoch": 0, "verdict": "REFORGE"}}
        with self.assertRaises(SystemExit):
            self.end()
        self.preclose["mechanism_cluster"] = {"epoch": 1}
        with self.assertRaises(SystemExit):
            self.end()
        # Pass 2 cross-family: an unrecognized verdict is not a stop.
        self.preclose["mechanism_cluster"] = {"epoch": 1, "verdict": "NOT_A_RULING"}
        with self.assertRaises(SystemExit):
            self.end()
        self.assertTrue((self.state / f"{KEY}.waiver.json").exists())

    def test_retry_after_archive_write_before_unlink_posts_once(self) -> None:
        """Reforge: a crash after the archived `ended` file is written but
        before the live journal is removed. The retry resumes the same run id,
        finds the prior post by marker and author, and does not repost."""
        gh = EndGh()
        real_unlink = Path.unlink
        def crash(path, *a, **kw):
            if path.name == f"{KEY}.waiver.json":
                raise OSError("killed")
            return real_unlink(path, *a, **kw)
        with patch.object(Path, "unlink", crash), self.assertRaises(OSError):
            self.end(gh)
        live = json.loads((self.state / f"{KEY}.waiver.json").read_text())
        self.assertEqual(live["status"], "ending")  # never rewritten as `ended`
        retry = EndGh(existing=[(URL, gh.posted_body or "")])
        final = self.end(retry, now=self.now + timedelta(minutes=5))
        self.assertEqual(final["end_comment_url"], URL)
        self.assertFalse(any("--method" in c for c in retry.calls))
        self.assertEqual(len(self.archived()), 1)
        self.assertFalse((self.state / f"{KEY}.waiver.json").exists())

    def test_resumes_a_legacy_ended_live_receipt_without_reposting(self) -> None:
        """Pass 2: a live receipt left `ended` (pre-reforge crash window)
        resumes its own run id instead of starting a fresh end."""
        path = self.state / f"{KEY}.waiver.json"
        data = json.loads(path.read_text())
        ended_at = imc_waiver._iso(self.now)
        data.update(status="ended", ended_at=ended_at, end_reason="r",
                    end_comment_url="https://x/first")
        path.write_text(json.dumps(data))
        gh = EndGh()
        final = self.end(gh, now=self.now + timedelta(minutes=5))
        self.assertEqual(final["end_comment_url"], "https://x/first")
        self.assertEqual(final["ended_at"], ended_at)
        self.assertFalse(any("--method" in c for c in gh.calls))

    def test_a_marker_from_another_author_is_not_the_end_comment(self) -> None:
        marker = imc_waiver.end_marker(KEY, imc_waiver._iso(self.now))
        forged = imc_waiver.end_heading(KEY) + "\n\n" + marker
        gh = EndGh(existing=[("https://x/forged", forged, "someone-else")])
        final = self.end(gh)
        self.assertNotEqual(final["end_comment_url"], "https://x/forged")
        self.assertEqual(sum(1 for c in gh.calls if "--method" in c), 1)

    def test_overlapping_ends_post_once(self) -> None:
        """Pass 2 (second-run), tightened in reforge pass 1 (test honesty):
        `end` holds a per-key lock, so a second run waits for the first.
        Posts are counted across BOTH runners; without the lock both read
        the same `recorded` receipt and both post."""
        import threading
        inside, release = threading.Event(), threading.Event()
        posted: list = []

        class Counting(EndGh):
            def __init__(self, slow: bool, **kw):
                super().__init__(**kw)
                self.slow = slow

            def __call__(self, argv, **kw):
                if "--method" in argv:
                    posted.append(1)
                    if self.slow:
                        inside.set()
                        release.wait(5)
                return super().__call__(argv, **kw)

        outcome: dict = {}

        def run(name, gh):
            try:
                outcome[name] = self.end(gh)
            except SystemExit as exc:
                outcome[name] = exc

        t = threading.Thread(target=run, args=("first", Counting(True)))
        t.start()
        inside.wait(5)
        t2 = threading.Thread(target=run, args=("second", Counting(False)))
        t2.start()
        t2.join(0.3)
        self.assertTrue(t2.is_alive(), "second end must wait on the lock")
        release.set()
        t.join(5)
        t2.join(5)
        self.assertEqual(len(posted), 1, "two overlapping ends posted twice")
        self.assertEqual(outcome["first"]["status"], "ended")

    def test_no_unconsumed_batch_target_and_no_hook_record_after_end(self) -> None:
        self.assertTrue(imc_waiver.live_merge_grant(KEY, self.batch))
        self.end()
        self.assertFalse(imc_waiver.live_merge_grant(KEY, self.batch))
        entry = json.loads(self.batch.read_text())[KEY]
        self.assertTrue(all(t["consumed"] for t in entry["targets"]))

    def test_hook_record_archived_not_deleted(self) -> None:
        self.end()
        self.assertFalse((self.state / f"{KEY}.json").exists())
        self.assertEqual(len(self.archived("hook")), 1)
        with patch.dict(imc_waiver.os.environ, {"LANE": "1"}):
            with self.assertRaises(SystemExit) as raised:
                imc_waiver.record(KEY, branch="feat/892-x", now=self.now,
                                  batch_state_path=self.batch, runner=EndGh())
        self.assertIn("no IMC hook record", str(raised.exception))

    def test_retry_after_crash_posts_once_archives_once(self) -> None:
        class CrashOnPost(EndGh):
            def __call__(self, argv, **kw):
                if "--method" in argv:
                    return subprocess.CompletedProcess(argv, 1, "", "network down")
                return super().__call__(argv, **kw)

        with self.assertRaises(SystemExit):
            self.end(CrashOnPost())
        # A failed post leaves IMC enforced: nothing archived, the live
        # receipt still read by waiver_receipt (pass 1, fail-direction).
        self.assertEqual(len(self.archived()), 0)
        live = json.loads((self.state / f"{KEY}.waiver.json").read_text())
        self.assertEqual(live["status"], "ending")
        self.assertIsNotNone(imc_waiver.waiver_receipt("vitalharmony/harmonic-forge", 892))
        gh = EndGh()
        final = self.end(gh, now=self.now + timedelta(minutes=5))
        self.assertEqual(len(self.archived()), 1)
        self.assertEqual(final["status"], "ended")
        self.assertEqual(sum(1 for c in gh.calls if "--method" in c), 1)

    def test_a_marker_only_comment_is_not_the_end_comment(self) -> None:
        """Pass 1 (cross-family): a comment carrying only the end marker (an
        edited-down prior post) never counts; the full comment is posted."""
        marker = imc_waiver.end_marker(KEY, imc_waiver._iso(self.now))
        gh = EndGh(existing=[("https://x/edited", marker)])
        final = self.end(gh)
        self.assertEqual(sum(1 for c in gh.calls if "--method" in c), 1)
        self.assertNotEqual(final["end_comment_url"], "https://x/edited")

    def test_retry_after_post_does_not_repost(self) -> None:
        class CrashOnRefetch(EndGh):
            def __call__(self, argv, **kw):
                if ("--method" not in argv and "--paginate" not in argv
                        and list(argv[:3]) != ["gh", "api", "user"]):
                    return subprocess.CompletedProcess(argv, 1, "", "boom")
                return super().__call__(argv, **kw)

        first = CrashOnRefetch()
        with self.assertRaises(SystemExit):
            self.end(first)
        gh = EndGh(existing=[(URL, first.posted_body or "")])
        final = self.end(gh, now=self.now + timedelta(minutes=5))
        self.assertEqual(final["end_comment_url"], URL)
        self.assertFalse(any("--method" in c for c in gh.calls))
        self.assertEqual(len(self.archived()), 1)

    def test_end_with_nothing_to_end_refuses(self) -> None:
        self.end()
        with self.assertRaises(SystemExit) as raised:
            self.end()
        self.assertIn("no IMC waiver receipt", str(raised.exception))


class EndRuleTextTests(unittest.TestCase):
    def test_r0384_names_end_and_r0229(self) -> None:
        text = (HERE.parent.parent / "rules" / "lane-shorthand.md").read_text(encoding="utf-8")
        span = text.split("<!-- R-0384 -->", 1)[1].split("<!-- /R-0384 -->", 1)[0]
        flat = " ".join(span.split())
        self.assertIn("imc_waiver.py end --key <K>", flat)
        self.assertIn("after which R-0229 is owed", flat)


class StateDirTests(unittest.TestCase):
    def test_default_state_dir_resolves_home_at_call_time(self) -> None:
        with tempfile.TemporaryDirectory() as home, patch.dict("os.environ", {"HOME": home}):
            self.assertEqual(imc_waiver.state_dir(), Path(home) / ".claude" / "state" / "imc")


if __name__ == "__main__":
    unittest.main()
