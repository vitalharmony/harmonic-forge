"""harmonic-forge#1086: `accepts` rule (b) tolerates a version-only replay conflict.

Each test builds a scratch repo: a base, a reviewed branch (rec), a main that
moved, and the reviewed change rebased onto main (cur). `accepts` is asked
whether `cur` is the change `rec` identifies.
"""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import preclose_passes as pp  # noqa: E402


def sh(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                          text=True).stdout.strip()


def pkg(version, **extra):
    return json.dumps({"name": "x", "version": version, **extra}, indent=2) + "\n"


def lock(version):
    return json.dumps({"name": "x", "version": version,
                       "packages": {"": {"version": version}}}, indent=2) + "\n"


class VersionOnlyReplay(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.d = Path(self.tmp.name)
        sh(self.d, "init", "-q", "-b", "main")
        sh(self.d, "config", "user.email", "t@example.invalid")
        sh(self.d, "config", "user.name", "T")
        self.write("package.json", pkg("1.0.0"))
        self.write("package-lock.json", lock("1.0.0"))
        self.write("code.py", "a = 1\n")
        self.base = self.commit("base")

    def write(self, rel, text):
        p = self.d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)

    def commit(self, msg):
        sh(self.d, "add", "-A")
        sh(self.d, "commit", "-q", "-m", msg)
        return sh(self.d, "rev-parse", "HEAD")

    def scenario(self, rec_pkg, main_pkg, cur_pkg, rec_extra=None, main_extra=None,
                 cur_extra=None):
        """Build rec (reviewed), main and cur (rec rebased onto main, hand-built)."""
        sh(self.d, "checkout", "-q", "-b", "rec", self.base)
        self.write("package.json", rec_pkg)
        self.write("code.py", "a = 2\n")
        for rel, text in (rec_extra or {}).items():
            self.write(rel, text)
        rec = self.commit("rec")
        sh(self.d, "checkout", "-q", "main")
        self.write("package.json", main_pkg)
        self.write("other.py", "b = 1\n")
        for rel, text in (main_extra or {}).items():
            self.write(rel, text)
        main = self.commit("main")
        sh(self.d, "checkout", "-q", "-b", "cur", main)
        self.write("package.json", cur_pkg)
        self.write("code.py", "a = 2\n")
        for rel, text in (cur_extra or {}).items():
            self.write(rel, text)
        cur = self.commit("cur")
        recorded = {"head_sha": rec, "merge_base_sha": self.base, "tree": None}
        return recorded, cur, main

    def accepts(self, recorded, cur, main):
        return pp.accepts(recorded, cur, cur_base=main, repo_dir=self.d)

    # AC1: the reproduced shape is accepted.
    def test_version_only_conflict_is_accepted(self):
        r, c, m = self.scenario(pkg("1.0.1"), pkg("1.1.0"), pkg("1.0.1"))
        self.assertTrue(self.accepts(r, c, m))

    def test_package_lock_root_version_conflict_is_accepted(self):
        r, c, m = self.scenario(pkg("1.0.1"), pkg("1.1.0"), pkg("1.0.1"),
                                rec_extra={"package-lock.json": lock("1.0.1")},
                                main_extra={"package-lock.json": lock("1.1.0")},
                                cur_extra={"package-lock.json": lock("1.0.1")})
        self.assertTrue(self.accepts(r, c, m))

    # AC2: refused shapes.
    def test_conflict_on_a_non_exempt_path_is_refused(self):
        r, c, m = self.scenario(pkg("1.0.1"), pkg("1.1.0"), pkg("1.0.1"),
                                main_extra={"code.py": "a = 3\n"})
        self.assertFalse(self.accepts(r, c, m))

    def test_reviewed_change_that_also_edits_a_non_exempt_key_is_refused(self):
        r, c, m = self.scenario(pkg("1.0.1", scripts={"t": "x"}), pkg("1.1.0"),
                                pkg("1.0.1", scripts={"t": "x"}))
        self.assertFalse(self.accepts(r, c, m))

    def test_conflict_on_a_non_exempt_json_file_is_refused_even_if_json_equal(self):
        """Preclose pass 1: only VERSION_EXEMPT basenames are judged. A conflicted
        config.json reformatted on both sides is JSON-equal to its base on each
        side, so only the basename refusal stops it."""
        self.write("config.json", '{"a": 1}\n')
        self.base = self.commit("base with config")
        r, c, m = self.scenario(pkg("1.0.1"), pkg("1.1.0"), pkg("1.0.1"),
                                rec_extra={"config.json": '{"a":1}\n'},
                                main_extra={"config.json": '{ "a" : 1 }\n'},
                                cur_extra={"config.json": '{"a":1}\n'})
        self.assertFalse(self.accepts(r, c, m))

    def test_head_that_drops_the_reviewed_non_exempt_edit_is_refused(self):
        """Kill check: the reviewed side is what catches a head that is
        version-only against its base but silently dropped the reviewed edit."""
        r, c, m = self.scenario(pkg("1.0.1", scripts={"t": "x"}), pkg("1.1.0"), pkg("1.0.1"))
        self.assertFalse(self.accepts(r, c, m))

    def test_head_that_differs_from_its_base_in_a_non_exempt_key_is_refused(self):
        r, c, m = self.scenario(pkg("1.0.1"), pkg("1.1.0"), pkg("1.0.1", private=True))
        self.assertFalse(self.accepts(r, c, m))

    def test_mode_change_on_the_exempt_file_is_refused(self):
        r, c, m = self.scenario(pkg("1.0.1"), pkg("1.1.0"), pkg("1.0.1"))
        sh(self.d, "checkout", "-q", "cur")
        (self.d / "package.json").chmod(0o755)
        c = self.commit("mode")
        self.assertFalse(self.accepts(r, c, m))

    def test_nested_package_json_conflict_is_judged_by_basename_and_both_sides(self):
        nested = {"sub/package.json": pkg("2.0.1")}
        r, c, m = self.scenario(pkg("1.0.0"), pkg("1.0.0"), pkg("1.0.0"),
                                rec_extra=nested, main_extra={"sub/package.json": pkg("2.1.0")},
                                cur_extra=nested)
        # sub/package.json is added on both sides (add/add): absent at rec_base -> refused.
        self.assertFalse(self.accepts(r, c, m))

    def test_extra_exempt_file_differing_in_a_non_exempt_key_is_still_compared(self):
        r, c, m = self.scenario(pkg("1.0.1"), pkg("1.1.0"), pkg("1.0.1"),
                                cur_extra={"web/package.json": pkg("9.9.9", main="x.js")})
        self.assertFalse(self.accepts(r, c, m))

    # AC3: fail closed.
    def test_merge_tree_error_exit_refuses(self):
        r, c, m = self.scenario(pkg("1.0.1"), pkg("1.1.0"), pkg("1.0.1"))
        real = pp.run_git

        def broken(repo_dir, *args, **kw):
            if args[:1] == ("merge-tree",):
                return subprocess.CompletedProcess(args, 128, "", "fatal: boom")
            return real(repo_dir, *args, **kw)
        pp.run_git = broken
        try:
            self.assertFalse(self.accepts(r, c, m))
        finally:
            pp.run_git = real

    def test_unparseable_exempt_json_refuses(self):
        r, c, m = self.scenario("{not json\n", pkg("1.1.0"), "{not json\n")
        self.assertFalse(self.accepts(r, c, m))

    # Unchanged behavior: a clean replay still takes today's path.
    def test_clean_replay_is_still_accepted(self):
        r, c, m = self.scenario(pkg("1.0.0"), pkg("1.0.0"), pkg("1.0.0"))
        self.assertTrue(self.accepts(r, c, m))


if __name__ == "__main__":
    unittest.main()
