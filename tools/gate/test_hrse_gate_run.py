#!/usr/bin/env python3
"""Tests for hrse-gate-run, the one auto-approved object for a Lane 3
production run (harmonic-forge#878 sticky-wicket PATCH).

Root ownership cannot be produced in a test, so the manifest's ownership check
is exercised by patching the expected path to a temp file (refused: not root)
and by stubbing `_immutable` for the cases that test later steps. Every
subprocess (broker, sudo, git) is stubbed; nothing here runs sudo.
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import hrse_gate_run as w  # noqa: E402

SHA = "c" * 40
RULE = "(hrse-gate) NOPASSWD: /usr/local/libexec/hrse-gate consume *"
BASE_RULE = "(neo4j) NOPASSWD: /usr/bin/neo4j-admin database dump neo4j --to-stdout"
SUDO, GIT = str(w.SUDO), str(w.GIT)
NONCE = "c" * 32


class _Case(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.worktree = root / "HRSE2-lane3"
        (self.worktree / "scripts").mkdir(parents=True)
        self.script = self.worktree / "scripts" / "gate_production_run.py"
        self.script.write_text("print('reviewed gate script')\n")
        self.manifest = {
            "worktree": str(self.worktree), "python": sys.executable,
            "script_sha256": hashlib.sha256(self.script.read_bytes()).hexdigest(),
            "broker_sha256": "b" * 64,
            "python_sha256": hashlib.sha256(Path(os.path.realpath(sys.executable)).read_bytes()).hexdigest(),
            "sudo_rules": [BASE_RULE, RULE],
        }
        self.calls: list[list[str]] = []
        self.envs: list[dict] = []
        self.sudo_rules = [BASE_RULE, RULE]
        self.consume_rc = 0
        self.consume_out = "consumed " + NONCE
        self.payload_blob, self.payload_here = "1" * 40, "1" * 40

    def tearDown(self):
        self._tmp.cleanup()

    def _fake_run(self, argv):
        self.calls.append(argv)
        rc, out = 0, ""
        if argv[:2] == [str(w.BROKER), "--version"]:
            out = self.broker_version if hasattr(self, "broker_version") else self.manifest["broker_sha256"]
        elif argv[:3] == [SUDO, "-n", "-l"]:
            out = "User mmangus may run the following commands on host:\n" + \
                "\n".join(f"    {r}" for r in self.sudo_rules)
        elif argv[:2] == [GIT, "-C"] and argv[3] == "rev-parse" and argv[4] == "HEAD":
            out = SHA
        elif argv[:2] == [GIT, "-C"] and argv[3] == "rev-parse":
            out = self.payload_blob
        elif argv[:2] == [GIT, "-C"] and argv[3] == "hash-object":
            out = self.payload_here
        elif argv[:4] == [SUDO, "-n", "-u", "hrse-gate"]:
            rc, out = self.consume_rc, self.consume_out
        return mock.Mock(returncode=rc, stdout=out, stderr="refused" if rc else "")

    def run_wrapper(self, *argv, lane="3"):
        with mock.patch.dict(os.environ, {"LANE": lane}), \
                mock.patch.object(w, "load_manifest", return_value=self.manifest), \
                mock.patch.object(w, "_run", side_effect=self._fake_run), \
                mock.patch.object(w, "_root_tool", side_effect=str), \
                mock.patch.object(w.os, "execve") as execv, \
                mock.patch.object(w.os, "chdir"), \
                mock.patch("sys.stderr"):
            rc = w.main(list(argv))
        return rc, execv

    def consumed(self):
        return [c for c in self.calls if c[:4] == [SUDO, "-n", "-u", "hrse-gate"]]


class HappyPathTests(_Case):
    def test_apply_spends_one_grant_then_execs_the_verified_script_with_apply(self):
        rc, execv = self.run_wrapper("--issue", "1892", "--script", "scripts/1-1892-revive.py",
                                     "--mode", "apply")
        self.assertEqual(self.consumed()[0][-3:], ["1892", SHA, "script=scripts/1-1892-revive.py,apply"])
        argv = execv.call_args.args[1]
        self.assertEqual(argv[1], str(self.script))
        self.assertEqual(argv[-3:], ["--script", "scripts/1-1892-revive.py", "--apply"])

    def test_the_gate_script_is_handed_the_spent_grant_id(self):
        """Ruling item 3 (F): the gate script refuses without HRSE_GATE_GRANT,
        so the runner -- the only grant-spending caller -- must set it, and
        must overwrite any value the lane's environment already carries."""
        with mock.patch.dict(os.environ, {"HRSE_GATE_GRANT": "f" * 32}):
            rc, execv = self.run_wrapper("--issue", "1892", "--script", "scripts/1-1892-revive.py")
        self.assertEqual(execv.call_args.args[2]["HRSE_GATE_GRANT"], NONCE)

    def test_a_dry_run_and_a_count_name_their_own_actions(self):
        self.run_wrapper("--issue", "1892", "--script", "scripts/1-1892-revive.py")
        self.run_wrapper("--issue", "1867", "--count-label", "Task")
        actions = [c[-1] for c in self.consumed()]
        self.assertEqual(actions, ["script=scripts/1-1892-revive.py", "count-label=Task"])


class RefusalTests(_Case):
    def assert_refused_without_consuming(self, *argv, lane="3"):
        rc, execv = self.run_wrapper(*argv, lane=lane)
        self.assertEqual(rc, w.EXIT_REFUSED)
        self.assertEqual(self.consumed(), [], "a grant was spent on a refused run")
        execv.assert_not_called()

    def test_a_consume_that_prints_no_grant_id_never_execs(self):
        self.consume_out = "consumed"
        rc, execv = self.run_wrapper("--issue", "1", "--count-label", "Task")
        self.assertEqual(rc, w.EXIT_REFUSED)
        execv.assert_not_called()

    def test_outside_lane3(self):
        self.assert_refused_without_consuming("--issue", "1", "--count-label", "Task", lane="1")

    def test_a_script_that_does_not_match_the_reviewed_digest(self):
        self.script.write_text("print('agent-edited')\n")
        self.assert_refused_without_consuming("--issue", "1", "--count-label", "Task")

    def test_a_stale_broker(self):
        """Fails for the right reason (item 10): only the broker's --version
        differs, and the refusal names the broker digest."""
        self.broker_version = "e" * 64
        with mock.patch("sys.stderr", new_callable=__import__("io").StringIO) as err:
            with mock.patch.dict(os.environ, {"LANE": "3"}), \
                    mock.patch.object(w, "load_manifest", return_value=self.manifest), \
                    mock.patch.object(w, "_run", side_effect=self._fake_run), \
                    mock.patch.object(w, "_root_tool", side_effect=str), \
                    mock.patch.object(w.os, "execve") as execv:
                rc = w.main(["--issue", "1", "--count-label", "Task"])
        self.assertEqual(rc, w.EXIT_REFUSED)
        self.assertIn("broker_sha256", err.getvalue())
        execv.assert_not_called()
        self.assertEqual(self.consumed(), [])

    def test_a_widened_sudo_rule_on_the_host(self):
        """Survivor S5: the host's rules, not the install doc."""
        for rules in ([BASE_RULE, RULE, "(root) NOPASSWD: /usr/local/libexec/hrse-gate grant *"],
                      [BASE_RULE, "(root) NOPASSWD: /usr/local/libexec/hrse-gate consume *"],
                      [BASE_RULE, RULE, "(ALL) NOPASSWD: ALL"],
                      [BASE_RULE, RULE, "(root) NOPASSWD: /usr/local/libexec/*"],
                      [BASE_RULE, RULE, "(root) NOPASSWD: /bin/sh"],
                      []):
            with self.subTest(rules=rules):
                self.calls.clear()
                self.sudo_rules = rules
                self.assert_refused_without_consuming("--issue", "1", "--count-label", "Task")

    def test_a_subsuming_rule_inside_the_reviewed_set_still_refuses(self):
        """Item 2: even if the manifest itself records it, a password-free ALL
        or wildcard rule could reach the broker's grant verb."""
        for extra in ("(ALL) NOPASSWD: ALL", "(root) NOPASSWD: /usr/local/libexec/*"):
            with self.subTest(extra=extra):
                self.calls.clear()
                self.sudo_rules = self.manifest["sudo_rules"] = [BASE_RULE, RULE, extra]
                self.assert_refused_without_consuming("--issue", "1", "--count-label", "Task")

    def test_tools_are_absolute_and_run_with_a_scrubbed_path(self):
        """Item 1: a planted ~/.local/bin/sudo earlier on PATH is never called."""
        recorded = []
        real = subprocess.run
        def spy(argv, **kw):
            recorded.append((argv, kw.get("env")))
            return mock.Mock(returncode=1, stdout="", stderr="")
        with mock.patch.object(w.subprocess, "run", side_effect=spy):
            w._run([SUDO, "-n", "-l"])
        argv, env = recorded[0]
        self.assertTrue(argv[0].startswith("/usr/bin/"))
        self.assertEqual(env["PATH"], "/usr/bin:/bin")
        self.assertNotIn("HOME", env)

    def test_an_edited_payload_refuses(self):
        """Item 9 (drift detector): the migration script must match HEAD."""
        self.payload_here = "2" * 40
        self.assert_refused_without_consuming("--issue", "1892", "--script", "scripts/1-1892-revive.py",
                                              "--mode", "apply")

    def test_a_substituted_interpreter_refuses(self):
        """Item 9 (drift detector): the interpreter's resolved binary is pinned."""
        self.manifest["python_sha256"] = "f" * 64
        self.assert_refused_without_consuming("--issue", "1", "--count-label", "Task")

    def test_absence_is_a_refusal_not_a_traceback(self):
        """Item 7: a missing manifest, broker or script refuses with its cause."""
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"LANE": "3"}), \
                    mock.patch.object(w, "MANIFEST", Path(tmp) / "absent.json"), \
                    mock.patch("sys.stderr"):
                self.assertEqual(w.main(["--issue", "1", "--count-label", "Task"]), w.EXIT_REFUSED)
        self.script.unlink()
        rc, execv = self.run_wrapper("--issue", "1", "--count-label", "Task")
        self.assertEqual(rc, w.EXIT_REFUSED)
        execv.assert_not_called()

    def test_a_non_migration_script_or_label(self):
        for argv in (("--script", "scripts/gate_production_run.py"), ("--script", "/etc/passwd"),
                     ("--count-label", "Task) DETACH DELETE (n")):
            with self.subTest(argv=argv):
                self.assert_refused_without_consuming("--issue", "1", *argv)

    def test_a_non_decimal_issue(self):
        self.assert_refused_without_consuming("--issue", "²", "--count-label", "Task")

    def test_mode_on_a_count(self):
        self.assert_refused_without_consuming("--issue", "1", "--count-label", "Task", "--mode", "apply")

    def test_no_grant_means_no_run(self):
        self.consume_rc = 3
        rc, execv = self.run_wrapper("--issue", "1", "--count-label", "Task")
        self.assertEqual(rc, w.EXIT_REFUSED)
        execv.assert_not_called()

    def test_a_manifest_the_agent_could_write_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            m = Path(tmp) / "manifest.json"
            m.write_text(json.dumps(self.manifest))
            with mock.patch.object(w, "MANIFEST", m):
                with self.assertRaises(w.Refused):
                    w.load_manifest()  # owned by this uid, not root


class SanctionedFormTests(unittest.TestCase):
    """Item 11: the permission matcher's semantics are NOT asserted in-repo (a
    reimplemented matcher only restates the author's assumptions). What is
    checked is fact: the sanctioned forms never carry the `--apply` token that
    HRSE2's tracked ask rules key on, and the policy names the runner by
    absolute path. Whether a chained command (`hrse-gate-run ... && <other>`)
    rides the allow rule is an accepted, documented residual
    (install-hrse-gate.md, "Limits, stated")."""

    POLICY = HERE.parent / "lane" / "policies" / "claude-lane3.json"

    def test_the_sanctioned_forms_never_carry_the_apply_token(self):
        for argv in (["--issue", "1892", "--script", "scripts/1-1892-revive.py", "--mode", "apply"],
                     ["--issue", "1867", "--count-label", "Task"]):
            self.assertNotIn("--apply", argv)
            w._parser().parse_args(argv)  # and they parse

    def test_the_runner_rejects_a_literal_apply_flag(self):
        with mock.patch("sys.stderr"), self.assertRaises(SystemExit):
            w._parser().parse_args(["--issue", "1", "--script", "scripts/1-x.py", "--apply"])

    def test_the_policy_names_the_runner_by_absolute_path(self):
        self.assertEqual(json.loads(self.POLICY.read_text())["permissions"]["allow"],
                         ["Bash(/usr/local/libexec/hrse-gate-run:*)"])


if __name__ == "__main__":
    unittest.main()


class RootToolTests(unittest.TestCase):
    """A root tool may be a root-owned symlink (openSUSE's /usr/bin/git); a
    symlink the lane owns, or one pointing at a lane-writable file, is refused.
    Ownership is faked through os.lstat, so the tests run on any host."""

    def _stat(self, uid=0, mode=0o755, link=False):
        kind = stat.S_IFLNK if link else stat.S_IFREG
        return os.stat_result((kind | mode, 0, 0, 1, uid, 0, 0, 0, 0, 0))

    def _dir(self, uid=0, mode=0o755):
        return os.stat_result((stat.S_IFDIR | mode, 0, 0, 1, uid, 0, 0, 0, 0, 0))

    def _check(self, stats, real="/usr/libexec/git/git"):
        def lstat(p):
            return stats[str(p)]
        with mock.patch.object(w.os, "lstat", side_effect=lstat), \
                mock.patch.object(w.os.path, "realpath", return_value=real):
            return w._root_tool(Path("/usr/bin/git"))

    def root_tree(self, **over):
        stats = {"/usr/bin/git": self._stat(link=True, mode=0o777), "/usr/bin": self._dir(),
                 "/usr/libexec/git/git": self._stat(), "/usr/libexec/git": self._dir()}
        stats.update(over)
        return stats

    def test_a_root_owned_symlink_execs_its_resolved_target(self):
        self.assertEqual(self._check(self.root_tree()), "/usr/libexec/git/git")

    def test_a_plain_root_owned_file_is_unchanged(self):
        stats = {"/usr/bin/git": self._stat(), "/usr/bin": self._dir()}
        self.assertEqual(self._check(stats), "/usr/bin/git")

    def test_a_lane_owned_symlink_is_refused(self):
        with self.assertRaises(w.Refused):
            self._check(self.root_tree(**{"/usr/bin/git": self._stat(uid=1000, link=True, mode=0o777)}))

    def test_a_symlink_to_a_lane_writable_target_is_refused(self):
        with self.assertRaises(w.Refused):
            self._check(self.root_tree(**{"/usr/libexec/git/git": self._stat(uid=1000)}))

    def test_a_symlink_into_a_group_writable_directory_is_refused(self):
        with self.assertRaises(w.Refused):
            self._check(self.root_tree(**{"/usr/libexec/git": self._dir(mode=0o775)}))

    def test_this_hosts_git_passes_when_it_is_root_owned(self):
        git = Path("/usr/bin/git")
        if not git.exists() or os.lstat(git).st_uid != 0:
            self.skipTest("no root-owned /usr/bin/git on this host")
        self.assertTrue(os.path.realpath(w._root_tool(git)).endswith("git"))
