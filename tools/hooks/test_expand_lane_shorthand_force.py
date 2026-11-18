#!/usr/bin/env python3
"""harmonic-forge#1169 AC1: `FORCE <KEY>: <reason>` writes a pending grant.

The grant store is a temp dir and the head lookup is faked; nothing here
reaches ~/.claude/state/force/ or a real preclose receipt.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "gh"))
import expand_lane_shorthand as m  # noqa: E402
import force_grant  # noqa: E402

HEAD = "c" * 40


class ForceTrigger(unittest.TestCase):
    real_covered_head = staticmethod(force_grant.covered_head)

    def setUp(self) -> None:
        self.state = Path(tempfile.mkdtemp())
        for target, attr, value in ((force_grant, "FORCE_STATE_DIR", self.state),
                                    (force_grant, "covered_head", lambda repo, issue: HEAD)):
            patcher = mock.patch.object(target, attr, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        os.environ.pop("CLAUDE_CODE_ENTRYPOINT", None)
        # harmonic-forge#1422: a reforge line reads the receipt archive; never the operator's.
        archive_root = Path(tempfile.mkdtemp())
        (archive_root / ".hf-telemetry-test-root").write_text("")
        env = mock.patch.dict(os.environ, {"HARMONIC_FORGE_TELEMETRY_ARCHIVE": str(archive_root)})
        env.start()
        self.addCleanup(env.stop)

    def force(self, prompt: str, lane: str | None = "1") -> str:
        return m.authorize_force(prompt, "sess-7", lane)

    def grant(self, key: str = "F1166") -> dict | None:
        path = self.state / f"{key}.json"
        return json.loads(path.read_text()) if path.exists() else None

    def test_a_typed_first_line_writes_a_pending_grant_bound_to_the_head(self) -> None:
        receipt = self.force("FORCE F1166: both post-verdict findings fixed")
        self.assertIn("pending plain grant written for F1166", receipt)
        grant = self.grant()
        self.assertEqual((grant["status"], grant["sha"], grant["repo"], grant["issue"]),
                         ("pending", HEAD, "vitalharmony/harmonic-forge", 1166))
        self.assertEqual(grant["reason"], "both post-verdict findings fixed")

    def test_a_provenance_failure_writes_nothing(self) -> None:
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_ENTRYPOINT": "sdk-py"}):
            receipt = self.force("FORCE F1166: reason")
        self.assertIn("FORCE REFUSED", receipt)
        self.assertIn("CLAUDE_CODE_ENTRYPOINT", receipt)
        self.assertIsNone(self.grant())

    def test_quoted_misplaced_lowercase_or_empty_lines_write_nothing(self) -> None:
        for prompt in ("```\nFORCE F1166: reason\n```", "> FORCE F1166: reason",
                       "please\nFORCE F1166: reason", "  FORCE F1166: reason",
                       "force F1166: reason", "FORCE F1166:"):
            with self.subTest(prompt=prompt):
                self.assertIn("FORCE REFUSED", self.force(prompt))
                self.assertIsNone(self.grant())

    def test_lane_other_than_one_and_a_foreign_key_write_nothing(self) -> None:
        self.assertIn("LANE is '2'", self.force("FORCE F1166: reason", lane="2"))
        self.assertIn("cross-account", self.force("FORCE K12: reason"))
        self.assertIsNone(self.grant())
        self.assertIsNone(self.grant("K12"))

    def test_no_preclose_receipt_writes_nothing(self) -> None:
        with mock.patch.object(force_grant, "covered_head", lambda repo, issue: None):
            self.assertIn("no preclose receipt", self.force("FORCE F1166: reason"))
        self.assertIsNone(self.grant())

    def test_the_grant_binds_to_the_post_verdict_head_through_the_real_lookup(self) -> None:
        """Preclose pass 1, finding 4: covered_head and force_head are not stubbed;
        only the receipt read is, holding a pass at A and a post-verdict check at B."""
        import preclose_check  # noqa: PLC0415
        pass_sha, post_verdict_sha = "a" * 40, "b" * 40
        receipt = {"reviewed_sha": pass_sha, "status": "complete",
                   "pass_history": [{"sha": pass_sha, "surviving": 1}],
                   "post_verdict_check": {"base_sha": pass_sha, "head_sha": post_verdict_sha}}
        with mock.patch.object(force_grant, "covered_head", self.real_covered_head), \
                mock.patch.object(preclose_check, "find_receipt", return_value=receipt):
            self.force("FORCE F1166: post-verdict check clean")
        self.assertEqual(self.grant()["sha"], post_verdict_sha)
        receipt.pop("post_verdict_check")
        with mock.patch.object(force_grant, "covered_head", self.real_covered_head), \
                mock.patch.object(preclose_check, "find_receipt", return_value=receipt):
            self.force("FORCE F1166: no post-verdict check")
        self.assertEqual(self.grant()["sha"], pass_sha)

    def test_a_prompt_without_force_is_silent(self) -> None:
        self.assertEqual(self.force("BATCH F1166"), "")



class ForceModes(ForceTrigger):
    """harmonic-forge#1174 AC1: `FORCE <KEY> [reforge <sha> | cluster]: <reason>`."""

    def test_a_cluster_grant_records_its_mode(self) -> None:
        receipt = self.force("FORCE F1166: x".replace(":", " cluster:", 1))
        self.assertIn("pending cluster grant", receipt)
        self.assertEqual((self.grant()["mode"], self.grant()["sha"]), ("cluster", HEAD))

    def test_a_reforge_grant_binds_the_typed_sha_not_the_covered_head(self) -> None:
        with mock.patch.object(force_grant, "covered_head", lambda repo, issue: None):
            receipt = self.force("FORCE F1166 reforge ABCDEF123456: new approach")
        self.assertIn("pending reforge grant", receipt)
        grant = self.grant()
        self.assertEqual((grant["mode"], grant["sha_prefix"], grant["sha"]),
                         ("reforge", "abcdef123456", None))

    def _isolated_archive(self) -> Path:
        """A test archive root (the sentinel archive_root demands) so no test
        reads the operator's real receipt archive."""
        root = Path(tempfile.mkdtemp())
        (root / ".hf-telemetry-test-root").write_text("")
        patcher = mock.patch.dict(os.environ, {"HARMONIC_FORGE_TELEMETRY_ARCHIVE": str(root)})
        patcher.start()
        self.addCleanup(patcher.stop)
        return root

    def test_a_reforge_on_a_head_the_current_receipt_reviewed_writes_nothing(self) -> None:
        """harmonic-forge#1422 AC1: a pass head and the post-verdict head both refuse."""
        import preclose_check  # noqa: PLC0415
        self._isolated_archive()
        receipt = {"pass_history": [{"sha": "a" * 40}],
                   "post_verdict_check": {"head_sha": "b" * 40}}
        for sha in ("a" * 12, "B" * 12):
            with self.subTest(sha), mock.patch.object(preclose_check, "find_receipt", return_value=receipt):
                out = self.force(f"FORCE F1166 reforge {sha}: new approach")
            self.assertIn("already reviewed", out)
            self.assertIsNone(self.grant())

    def test_a_reforge_on_an_archived_post_verdict_head_writes_nothing(self) -> None:
        """AC1 hard case (pitch-inspection): post_verdict_check is one slot the
        next check overwrites, so an earlier post-verdict head lives only in the
        archive -- F1419's 96f53c1."""
        import preclose_check  # noqa: PLC0415
        root = self._isolated_archive()
        sys.path.insert(0, str(Path(force_grant.__file__).resolve().parent.parent / "telemetry"))
        import archive  # noqa: PLC0415
        archived = {"repo": "vitalharmony/harmonic-forge", "issue": 1166, "pass_history": [],
                    "post_verdict_check": {"head_sha": "c" * 40}}
        archive.archive("preclose-receipts", [archived],
                        origin=archive.origin_for_repo("vitalharmony/harmonic-forge"))
        self.assertTrue(list(root.rglob("*.jsonl.gz")))
        with mock.patch.object(preclose_check, "find_receipt", return_value={"pass_history": []}):
            out = self.force("FORCE F1166 reforge cccccccccccc: new approach")
        self.assertIn("already reviewed", out)
        self.assertIsNone(self.grant())

    def test_a_reforge_with_no_receipt_and_no_archive_writes_the_grant(self) -> None:
        """AC2, and the receipt-absent case: reviewed_heads is empty, never a refusal."""
        import preclose_check  # noqa: PLC0415
        self._isolated_archive()
        with mock.patch.object(preclose_check, "find_receipt", return_value=None):
            self.assertEqual(force_grant.reviewed_heads("vitalharmony/harmonic-forge", 1166), set())
            out = self.force("FORCE F1166 reforge dddddddddddd: new approach")
        self.assertIn("pending reforge grant", out)
        self.assertEqual(self.grant()["sha_prefix"], "dddddddddddd")

    def test_reforge_without_sha_refuses(self) -> None:
        self.assertIn("names the head it approves", self.force("FORCE F1166 reforge: new approach"))
        self.assertIsNone(self.grant())

    def test_cluster_with_sha_refuses(self) -> None:
        self.assertIn("takes no SHA", self.force("FORCE F1166 cluster abcdef123456: x"))
        self.assertIsNone(self.grant())

    def test_unknown_mode_refuses_and_writes_nothing(self) -> None:
        self.assertIn("unknown FORCE mode", self.force("FORCE F1166 rebase: x"))
        self.assertIsNone(self.grant())

    def test_malformed_modes_get_their_own_refusal(self) -> None:
        for line in ("FORCE F1166 reforge abc12z456789: x", "FORCE F1166 reforge2: x",
                     "FORCE F1166 reforge " + "a" * 41 + ": x", "FORCE F1166 Reforge abcdef123456: x",
                     "FORCE F1166 REFORGE abcdef123456: x", "FORCE F1166 reforge abc123: x"):
            with self.subTest(line=line):
                receipt = self.force(line)
                self.assertIn("FORCE REFUSED for F1166", receipt)
                self.assertNotIn("first line", receipt)
                self.assertIsNone(self.grant())

    def test_a_replaced_grant_is_named(self) -> None:
        self.force("FORCE F1166: first")
        receipt = self.force("FORCE F1166 cluster: second")
        self.assertIn("replaces the plain grant at cccccccccccc", receipt)

    def test_a_route_grant_binds_the_typed_sha_with_no_receipt(self) -> None:
        """harmonic-forge#1244 AC1 (kill check: the receipt lookup back on route)."""
        with mock.patch.object(force_grant, "covered_head", lambda repo, issue: None):
            receipt = self.force("FORCE F1166 route " + "ABCDEF1234" * 4 + ": release this merge")
        self.assertIn("pending route grant", receipt)
        self.assertIn("then the merge", receipt)
        self.assertNotIn("--force run", receipt)
        grant = self.grant()
        self.assertEqual((grant["mode"], grant["sha_prefix"], grant["sha"], grant["status"]),
                         ("route", "abcdef1234" * 4, None, "pending"))

    def test_route_without_sha_refuses_and_says_why(self) -> None:
        self.assertIn("names the head it releases", self.force("FORCE F1166 route: release"))
        self.assertIsNone(self.grant())

    def test_malformed_route_lines_write_nothing_and_say_why(self) -> None:
        for line, why in (("FORCE F1166 route abc12z456789: x", "not a full 40-character"),
                          ("FORCE F1166 route abc123: x", "not a full 40-character"),
                          ("FORCE F1166 route abcdef123456: x", "not a full 40-character"),
                          ("FORCE F1166 route " + "a" * 41 + ": x", "not a full 40-character"),
                          ("FORCE F1166 route abcdef123456 abcdef123456: x", "names the head it releases"),
                          ("FORCE F1166 Route abcdef123456: x", "unknown FORCE mode")):
            with self.subTest(line=line):
                self.assertIn(why, self.force(line))
                self.assertIsNone(self.grant())

    def test_a_colonless_line_says_the_colon_is_missing(self) -> None:
        for line in ("FORCE F1166 route", "FORCE F1166 route abcdef123456", "FORCE F1166",
                     "FORCE route abcdef123456: release", "FORCE F1166a route abcdef123456: x",
                     "FORCE F1234567 route abcdef123456: x"):
            with self.subTest(line=line):
                receipt = self.force(line)
                self.assertIn("needs a key like F1244 and a colon", receipt)
                self.assertNotIn("first line", receipt)
                self.assertIsNone(self.grant())

    def test_unknown_mode_names_route(self) -> None:
        self.assertIn("`route <sha>`", self.force("FORCE F1166 rebase: x"))

if __name__ == "__main__":
    unittest.main()
