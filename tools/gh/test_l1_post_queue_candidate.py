#!/usr/bin/env python3
"""harmonic-forge#691 (rescoped), AC5'. `l1_post.py`'s `record_queue_candidate`
is now a thin adapter over the shared `belt_candidates.record_candidate`
(harmonic-forge) -- the belt candidate source that replaces the account-wide
scan harmonic-forge#686 removed. `belt_candidates`'s own unit tests (one
file per issue, atomicity, eligibility filtering) live in that module's
own `test_belt_candidates.py`; this file covers two things specific to
THIS tool: the adapter's own contract (`posted_by` is always `"l1"`,
never derived from `LANE`), and the AC5' integration assertion that
`main()` actually calls it on every successful post -- not merely that
the function exists.
"""
import sys
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import l1_post as L  # noqa: E402


class RecordQueueCandidateAdapterTests(unittest.TestCase):
    """The adapter itself: `record_queue_candidate` always records
    `posted_by="l1"` and is a silent no-op if the platform checkout (and
    so `belt_candidates`) is absent -- never a reason this tool fails to
    post."""

    def test_delegates_to_belt_candidates_with_posted_by_l1(self) -> None:
        with unittest.mock.patch.object(L, "_belt_candidates") as fake:
            L.record_queue_candidate("example/project", 1921, "handoff")
        fake.record_candidate.assert_called_once_with(
            "example/project", 1921, "handoff", "l1")

    def test_a_missing_platform_checkout_never_raises(self) -> None:
        """A sibling directory moving must never make this tool unable to
        post -- but (harmonic-forge#691 preclose finding 4) it is no longer
        a SILENT no-op; see `BeltCandidateFailedImportIsVisibleTests`
        below."""
        with unittest.mock.patch.object(L, "_belt_candidates", None):
            L.record_queue_candidate("vitalharmony/hrse", 1, "handoff")  # must not raise


class BeltCandidateFailedImportIsVisibleTests(unittest.TestCase):
    """harmonic-forge#691 preclose finding 4. Same hole as `item_list_cache`'s
    neighboring `try/except ImportError` -- except that one falls back to a
    still-functional uncached path, so its silence is honest; this one
    falls back to recording nothing while the caller's own success message
    still prints. The fix: a stderr warning naming what failed, on every
    call where recording didn't happen, without raising (the post itself
    must still succeed)."""

    def test_a_missing_platform_checkout_prints_a_stderr_warning(self) -> None:
        with unittest.mock.patch.object(L, "_belt_candidates", None), \
             unittest.mock.patch.object(sys, "stderr", new=__import__("io").StringIO()) as fake_err:
            L.record_queue_candidate("example/project", 1921, "handoff")
        warning = fake_err.getvalue()
        self.assertIn("belt-candidate not recorded", warning)
        self.assertIn("example/project#1921", warning)

    def test_a_present_platform_checkout_prints_no_warning(self) -> None:
        with unittest.mock.patch.object(L, "_belt_candidates") as fake, \
             unittest.mock.patch.object(sys, "stderr", new=__import__("io").StringIO()) as fake_err:
            L.record_queue_candidate("vitalharmony/hrse", 1921, "handoff")
        fake.record_candidate.assert_called_once()
        self.assertNotIn("belt-candidate not recorded", fake_err.getvalue())


class L1PostBeltCandidateIntegrationTests(unittest.TestCase):
    """AC5'. `main()` actually invokes the recorder on a successful post,
    for both the single-kind path and the `ae-and-sweep` atomic pair."""

    def _base_argv(self, kind: str, **extra: str) -> list[str]:
        argv = [
            "l1_post.py", "--repo", "vitalharmony/hrse", "--issue", "1921",
            "--kind", kind, "--sha", "deadbeef", "--branch", "l1/test",
        ]
        for key, value in extra.items():
            argv += [f"--{key.replace('_', '-')}", value]
        return argv

    def test_a_single_kind_post_records_its_own_kind(self) -> None:
        argv = self._base_argv("rework", file="/dev/null")
        with unittest.mock.patch.object(sys, "argv", argv), \
             unittest.mock.patch.object(L, "resolve_sha", return_value="deadbeef"), \
             unittest.mock.patch.object(L, "regular_body", return_value="body"), \
             unittest.mock.patch.object(L, "reject_reserved_marker"), \
             unittest.mock.patch.object(L, "validate_lead"), \
             unittest.mock.patch.object(
                 L, "post_kind", return_value=("https://example/1", 1)), \
             unittest.mock.patch.object(L, "record_queue_candidate") as recorder:
            L.main()
        recorder.assert_called_once_with("vitalharmony/hrse", 1921, "rework")

    def test_ae_and_sweep_records_both_kinds_in_order(self) -> None:
        argv = [
            "l1_post.py", "--repo", "vitalharmony/hrse", "--issue", "1921",
            "--kind", "ae-and-sweep", "--sha", "deadbeef", "--branch", "l1/test",
            "--ae-file", "/dev/null", "--sweep-file", "/dev/null",
            "--spec-comment", "42",
        ]
        with unittest.mock.patch.object(sys, "argv", argv), \
             unittest.mock.patch.object(L, "resolve_sha", return_value="deadbeef"), \
             unittest.mock.patch.object(L, "regular_body", return_value="body"), \
             unittest.mock.patch.object(L, "reject_reserved_marker"), \
             unittest.mock.patch.object(L, "validate_ae"), \
             unittest.mock.patch.object(L, "validate_lead"), \
             unittest.mock.patch.object(L, "validate_sweep"), \
             unittest.mock.patch.object(L, "run", return_value=unittest.mock.Mock(
                 returncode=0, stdout="## Lane 3 Test Spec")), \
             unittest.mock.patch.object(
                 L, "post_kind", side_effect=[("https://example/ae", 1),
                                              ("https://example/sweep", 2)]), \
             unittest.mock.patch.object(L, "record_queue_candidate") as recorder:
            L.main()
        recorder.assert_has_calls([
            unittest.mock.call("vitalharmony/hrse", 1921, "ae"),
            unittest.mock.call("vitalharmony/hrse", 1921, "sweep"),
        ])
        self.assertEqual(recorder.call_count, 2)


if __name__ == "__main__":
    unittest.main()
