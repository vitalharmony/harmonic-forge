#!/usr/bin/env python3
"""Tests for lane3_audit.py and install_lane_hooks.py (harmonic-forge#720).

The marker's own dead-owner and lease tests stay with the marker in the
consuming repo until harmonic-forge#721 moves its protocol half here.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import lane3_audit as audit  # noqa: E402


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True,
                   capture_output=True, text=True)


class AuditRecordTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.log = Path(self._tmp.name) / "lane3-audit.jsonl"

    def _entries(self) -> list[dict]:
        return audit.read(path=self.log)

    def test_record_appends_one_line_of_json(self) -> None:
        self.assertTrue(audit.record("post-checkout", "moved",
                                     prior_head="a" * 40, target="b" * 40,
                                     path=self.log))
        entries = self._entries()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["event"], "post-checkout")
        self.assertEqual(entries[0]["outcome"], "moved")
        self.assertEqual(entries[0]["prior_head"], "a" * 40)

    def test_every_record_carries_time_worktree_and_ancestry(self) -> None:
        """The four fields AC1 names, plus the chain that makes them usable."""
        audit.record("gate-checkout", "attempted", path=self.log)
        entry = self._entries()[0]
        for field in ("at", "worktree", "session_owner", "ancestry", "pid"):
            self.assertIn(field, entry)
        self.assertIsInstance(entry["ancestry"], list)

    def test_a_refusal_is_recorded_too(self) -> None:
        """AC2 — a guard that fired never reaches git, so nothing else sees it."""
        audit.record("marker-check", "refused", detail="held by another session",
                     path=self.log)
        entry = self._entries()[0]
        self.assertEqual(entry["outcome"], "refused")
        self.assertIn("another session", entry["detail"])

    def test_records_accumulate_rather_than_overwrite(self) -> None:
        for i in range(5):
            audit.record("post-checkout", "moved", target=str(i), path=self.log)
        self.assertEqual([e["target"] for e in self._entries()],
                         ["0", "1", "2", "3", "4"])

    def test_record_never_raises_on_an_unwritable_path(self) -> None:
        """A logging failure must never be able to fail a gate or a checkout."""
        self.assertFalse(
            audit.record("post-checkout", "moved",
                         path=Path("/nonexistent-root/nope/audit.jsonl")))

    def test_malformed_lines_are_skipped_not_fatal(self) -> None:
        self.log.write_text('{"event": "ok"}\nNOT JSON\n', encoding="utf-8")
        self.assertEqual([e["event"] for e in self._entries()], ["ok"])

    def test_log_is_trimmed_to_a_bound(self) -> None:
        with mock.patch.object(audit, "MAX_RECORDS", 10):
            for i in range(25):
                audit.record("post-checkout", "moved", target=str(i), path=self.log)
        self.assertLessEqual(len(self._entries()), 10)


class AuditSurvivesLaneEndTests(unittest.TestCase):
    """AC1 — the record must outlive `lane3-end`, which the marker did not."""

    def test_audit_path_is_the_common_dir_not_the_marker_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _git("init", "-q", cwd=root)
            path = audit.audit_path(root)
            self.assertIsNotNone(path)
            self.assertEqual(path.name, audit.AUDIT_FILENAME)
            # lane3-end does `rm -f "$git_dir/LANE3_ACTIVE"` and nothing else.
            self.assertNotEqual(path.name, "LANE3_ACTIVE")

    def test_removing_the_marker_leaves_the_log(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _git("init", "-q", cwd=root)
            log = audit.audit_path(root)
            audit.record("marker-check", "allowed", worktree=root, path=log)
            (root / ".git" / "LANE3_ACTIVE").write_text("owner_pid=1\n")
            (root / ".git" / "LANE3_ACTIVE").unlink()      # what lane3-end does
            self.assertTrue(log.is_file())
            self.assertEqual(len(audit.read(path=log)), 1)


class HookShimTests(unittest.TestCase):
    """Preclose finding 2 — the audit must survive a checkout to an old tree.

    `core.hooksPath` pointed inside the working tree, and git runs
    post-checkout AFTER updating it, so the DESTINATION commit's hook ran. A
    checkout to anything cut before the audit landed recorded nothing — which
    is the exact move attribution most needs, and the shape of the incident.
    """

    def test_shim_is_installed_outside_every_working_tree(self) -> None:
        import install_lane_hooks

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            (repo / "scripts").mkdir(parents=True)
            (repo / ".githooks").mkdir()
            (repo / ".githooks" / "post-checkout").write_text(
                "#!/bin/bash\nexit 0\n", encoding="utf-8")
            (repo / "scripts" / "lane3_audit.py").write_text("", encoding="utf-8")
            _git("init", "-q", cwd=repo)

            wrapper_dir, names = install_lane_hooks.install(repo)
            self.assertIn("post-checkout", names)
            # Outside the working tree: no checkout can swap or remove it.
            self.assertNotIn(str(repo / ".githooks"), str(wrapper_dir))
            self.assertTrue((wrapper_dir / "post-checkout").is_file())
            body = (wrapper_dir / "post-checkout").read_text(encoding="utf-8")
            self.assertIn("lane3_audit.py", body)
            self.assertIn(".githooks/post-checkout", body,
                          "the shim must still delegate to the tracked hook")

    def test_shim_wraps_every_tracked_hook_not_just_post_checkout(self) -> None:
        import install_lane_hooks

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            (repo / ".githooks").mkdir(parents=True)
            for name in ("post-merge", "pre-commit"):
                (repo / ".githooks" / name).write_text("#!/bin/bash\nexit 0\n",
                                                       encoding="utf-8")
            _git("init", "-q", cwd=repo)
            _, names = install_lane_hooks.install(repo)
            self.assertEqual(set(names), {"post-merge", "pre-commit", "post-checkout"})


class InstallerSourceTests(unittest.TestCase):
    """harmonic-forge#720: the installer copies the PLATFORM's canonical audit
    script, not the consuming repo's `scripts/lane3_audit.py` (now a shim)."""

    def test_the_common_dir_copy_is_forges_canonical_audit(self) -> None:
        import install_lane_hooks
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            _git("init", "-q", cwd=repo)
            (repo / "scripts").mkdir()
            (repo / "scripts" / "lane3_audit.py").write_text("# a consuming-repo shim\n", encoding="utf-8")
            install_lane_hooks.install(repo)
            common = Path(subprocess.run(["git", "rev-parse", "--git-common-dir"], cwd=repo,
                                         capture_output=True, text=True, check=True).stdout.strip())
            if not common.is_absolute():
                common = repo / common
            copied = (common / "lane3_audit.py").read_text(encoding="utf-8")
            self.assertEqual(copied, (HERE / "lane3_audit.py").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
