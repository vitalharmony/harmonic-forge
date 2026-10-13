"""harmonic-forge#975: the pre-push gate for Tooling Exception branches."""
from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import check_receipt as C  # noqa: E402

SHA = "a" * 40
OTHER = "b" * 40
SLUG = "vitalharmony__harmonic-forge"
SSH = "git@github.com:vitalharmony/harmonic-forge.git"


def line(branch: str, sha: str = SHA, local: str | None = None) -> str:
    return f"{local or 'refs/heads/' + branch} {sha} refs/heads/{branch} {'0' * 40}"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        patch = mock.patch.object(C, "STORE", Path(self.tmp.name))
        patch.start()
        self.addCleanup(patch.stop)
        self.addCleanup(self.tmp.cleanup)
        self.err = mock.patch("sys.stderr", new_callable=lambda: __import__("io").StringIO())
        self.stderr = self.err.start()
        self.addCleanup(self.err.stop)

    def receipt(self, sha: str = SHA):
        path = C.receipt_path(SLUG, sha)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()


class RepoSlug(unittest.TestCase):
    def test_url_forms_normalize_alike(self):
        for url in (SSH, "https://github.com/vitalharmony/harmonic-forge",
                    "https://github.com/vitalharmony/harmonic-forge.git",
                    "ssh://git@github.com/vitalharmony/harmonic-forge.git",
                    "ssh://git@ssh.github.com:443/vitalharmony/harmonic-forge.git",
                    "https://GitHub.com/VitalHarmony/Harmonic-Forge.git"):
            self.assertEqual(C.repo_slug(url), SLUG, url)


class Verify(Base):
    def test_te_branch_without_receipt_is_refused(self):
        with mock.patch.object(C, "labels", return_value=["tooling-exception"]):
            self.assertEqual(C.verify(SSH, [line("fix/975-x")]), 1)
        self.assertIn("no passing 'mise run check' receipt", self.stderr.getvalue())

    def test_te_branch_with_receipt_passes_across_url_forms(self):
        self.receipt()  # recorded from an https origin, pushed over ssh
        with mock.patch.object(C, "labels", return_value=["tooling-exception"]):
            self.assertEqual(C.verify(SSH, [line("fix/975-x")]), 0)

    def test_rebased_commit_needs_its_own_receipt(self):
        self.receipt(SHA)
        with mock.patch.object(C, "labels", return_value=["tooling-exception"]):
            self.assertEqual(C.verify(SSH, [line("fix/975-x", OTHER)]), 1)

    def test_non_te_branch_passes(self):
        with mock.patch.object(C, "labels", return_value=["feature"]):
            self.assertEqual(C.verify(SSH, [line("feat/2360-x")]), 0)

    def test_unmatched_branch_names_are_not_looked_up(self):
        with mock.patch.object(C, "labels") as looked:
            self.assertEqual(C.verify(SSH, [line("feat/975x"), line("main")]), 0)
            looked.assert_not_called()

    def test_deletion_of_te_branch_passes(self):
        deletion = f"(delete) {'0' * 40} refs/heads/fix/975-old {SHA}"
        with mock.patch.object(C, "labels", return_value=["tooling-exception"]):
            self.assertEqual(C.verify(SSH, [deletion]), 0)

    def test_head_refspec_is_gated_by_its_remote_ref(self):
        with mock.patch.object(C, "labels", return_value=["tooling-exception"]):
            self.assertEqual(C.verify(SSH, [line("fix/975-x", local="HEAD")]), 1)

    def test_lookup_failure_warns_and_passes(self):
        failed = subprocess.CompletedProcess([], 1, "", "gh: error")
        with mock.patch.object(C.subprocess, "run", return_value=failed):
            self.assertEqual(C.verify(SSH, [line("fix/975-x")]), 0)
        self.assertIn("WARNING", self.stderr.getvalue())


class Main(Base):
    def test_unreadable_store_warns_and_passes(self):
        stdin = mock.patch("sys.stdin", __import__("io").StringIO(line("fix/975-x") + "\n"))
        with stdin, mock.patch.object(C, "labels", return_value=["tooling-exception"]), \
                mock.patch.object(C, "receipt_path", side_effect=PermissionError("denied")):
            self.assertEqual(C.main(["x", "verify", "origin", SSH]), 0)
        self.assertIn("WARNING", self.stderr.getvalue())


class Record(Base):
    def _repo(self, root: Path):
        run = lambda *a: subprocess.run(["git", *a], cwd=root, check=True, capture_output=True)  # noqa: E731
        run("init", "-q")
        run("remote", "add", "origin", "https://github.com/vitalharmony/harmonic-forge.git")
        (root / "f").write_text("x")
        run("add", "f")
        run("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "c")
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True,
                              text=True).stdout.strip()

    def test_clean_tree_records_head(self):
        with TemporaryDirectory() as d:
            head = self._repo(Path(d))
            C.record(d)
            self.assertTrue(C.receipt_path(SLUG, head).exists())

    def test_head_moved_during_check_refuses(self):
        with TemporaryDirectory() as d:
            head = self._repo(Path(d))
            self.assertEqual(C.record(d, head=OTHER), 0)
            self.assertFalse(C.receipt_path(SLUG, head).exists())

    def test_no_origin_warns_and_does_not_fail(self):
        with TemporaryDirectory() as d:
            self._repo(Path(d))
            subprocess.run(["git", "remote", "remove", "origin"], cwd=d, check=True)
            self.assertEqual(C.record(d), 0)
        self.assertIn("WARNING", self.stderr.getvalue())

    def test_untracked_file_refuses(self):
        with TemporaryDirectory() as d:
            head = self._repo(Path(d))
            (Path(d) / "untracked").write_text("y")
            C.record(d)
            self.assertFalse(C.receipt_path(SLUG, head).exists())


if __name__ == "__main__":
    unittest.main()
