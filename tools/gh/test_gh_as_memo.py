#!/usr/bin/env python3
"""Tests for gh-as's identity-probe memo (harmonic-forge#957). A fake `gh` on
PATH serves `auth token` and `api user` and logs every call, so nothing touches
a real credential."""
from __future__ import annotations

import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

GH_AS = Path(__file__).resolve().parent / "gh-as"
SHIM = Path(__file__).resolve().parent / "gh_shim"


class GhAsMemoTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.bin = root / "bin"
        self.bin.mkdir()
        self.home = root / "accounts"
        self.slot = self.home / "vitalharmony"
        self.slot.mkdir(parents=True)
        self.log = root / "calls.log"
        self.token = root / "token"
        self.login = root / "login"
        self.token.write_text("tok-A")
        self.login.write_text("vitalharmony")
        fake = self.bin / "gh"
        fake.write_text(
            "#!/usr/bin/env bash\n"
            f'printf "%s\\n" "$*" >> "{self.log}"\n'
            'case "$*" in\n'
            f'  "auth token") cat "{self.token}" ;;\n'
            f'  "api user --jq .login") cat "{self.login}" ;;\n'
            "esac\n")
        fake.chmod(0o755)

    def run_gh_as(self, ttl: str | None = None) -> subprocess.CompletedProcess:
        env = {**os.environ, "PATH": f"{self.bin}:{os.environ['PATH']}", "GH_ACCT_HOME": str(self.home)}
        # harmonic-forge#1396: a lane session's LANE maps vitalharmony to its lane
        # slot; these tests exercise the plain account mapping.
        env.pop("LANE", None)
        env.pop("GH_AS_IDENTITY_TTL", None)
        if ttl is not None:
            env["GH_AS_IDENTITY_TTL"] = ttl
        return subprocess.run(["bash", str(GH_AS), "vitalharmony", "true"],
                              capture_output=True, text=True, env=env)

    def probes(self) -> int:
        if not self.log.exists():
            return 0
        return self.log.read_text().splitlines().count("api user --jq .login")

    def test_a_second_call_under_the_same_token_skips_the_probe(self):  # AC1
        self.assertEqual(self.run_gh_as().returncode, 0)
        self.assertEqual(self.run_gh_as().returncode, 0)
        self.assertEqual(self.probes(), 1)

    def test_a_changed_token_reprobes_and_a_wrong_login_refuses(self):  # AC2
        self.run_gh_as()
        self.token.write_text("tok-B")
        self.login.write_text("someone-else")
        result = self.run_gh_as()
        self.assertEqual(self.probes(), 2)
        self.assertEqual(result.returncode, 1)
        self.assertIn("refusing to run", result.stderr)

    def test_an_unreadable_token_probes_every_time(self):  # AC3
        self.token.write_text("")
        self.run_gh_as()
        self.run_gh_as()
        self.assertEqual(self.probes(), 2)
        self.assertFalse((self.slot / ".identity-memo").exists())

    def test_a_failed_probe_is_never_remembered(self):  # AC3
        self.login.write_text("someone-else")
        self.assertEqual(self.run_gh_as().returncode, 1)
        self.assertFalse((self.slot / ".identity-memo").exists())

    def test_only_a_fingerprint_is_stored_mode_600(self):  # AC4
        self.run_gh_as()
        memo = self.slot / ".identity-memo"
        self.assertNotIn("tok-A", memo.read_text())
        self.assertEqual(stat.S_IMODE(memo.stat().st_mode), 0o600)

    def test_ttl_zero_turns_memo_off(self):  # AC5
        self.run_gh_as(ttl="0")
        self.run_gh_as(ttl="0")
        self.assertEqual(self.probes(), 2)

    def test_an_expired_memo_reprobes(self):  # AC5
        self.run_gh_as()
        memo = self.slot / ".identity-memo"
        fp, acct, _ = memo.read_text().split()
        memo.write_text(f"{fp} {acct} 1\n")
        self.run_gh_as()
        self.assertEqual(self.probes(), 2)

    def test_a_malformed_memo_time_reprobes_instead_of_crashing(self):
        self.run_gh_as()
        memo = self.slot / ".identity-memo"
        fp, acct, _ = memo.read_text().split()
        for bad in ("abc", "1 2"):
            memo.write_text(f"{fp} {acct} {bad}\n")
            self.assertEqual(self.run_gh_as().returncode, 0, bad)
        self.assertEqual(self.probes(), 3)

    def test_a_token_changed_during_the_probe_is_not_remembered(self):
        fake = self.bin / "gh"
        fake.write_text(fake.read_text().replace(
            f'  "api user --jq .login") cat "{self.login}" ;;',
            f'  "api user --jq .login") cat "{self.login}"; echo tok-B > "{self.token}" ;;'))
        result = self.run_gh_as()
        self.assertEqual(result.returncode, 1)
        self.assertIn("changed its token", result.stderr)
        self.assertFalse((self.slot / ".identity-memo").exists())

    def test_a_probe_that_exits_nonzero_is_a_failed_probe(self):
        fake = self.bin / "gh"
        fake.write_text(fake.read_text().replace(
            f'  "api user --jq .login") cat "{self.login}" ;;',
            f'  "api user --jq .login") cat "{self.login}"; exit 1 ;;'))
        self.assertEqual(self.run_gh_as().returncode, 1)
        self.assertFalse((self.slot / ".identity-memo").exists())

    def test_the_shim_exempts_auth_token_before_the_override(self):  # AC6
        text = SHIM.read_text()
        exempt = text.index('argv == ["auth", "token"]')
        self.assertLess(exempt, text.index("consume_override()"))


class GhAsLaneSlotTests(unittest.TestCase):
    """harmonic-forge#1396: in a lane, `gh-as vitalharmony` runs on the lane
    slot's file token as GH_TOKEN, and refuses when that slot has none."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.bin = root / "bin"
        self.bin.mkdir()
        self.home = root / "accounts"
        (self.home / "vitalharmony").mkdir(parents=True)
        self.lane_slot = self.home / "vitalharmony-lane2"
        self.lane_slot.mkdir()
        fake = self.bin / "gh"
        # Answers the identity probe and echoes the token it ran under, so the
        # test sees which credential the command actually carried.
        fake.write_text(
            "#!/usr/bin/env bash\n"
            'case "$*" in\n'
            '  "auth token") printf "%s" "${GH_TOKEN:-keyring-admin}" ;;\n'
            '  "api user --jq .login") echo vitalharmony ;;\n'
            '  "whoami") printf "token=%s dir=%s\\n" "${GH_TOKEN:-keyring-admin}" "$GH_CONFIG_DIR" ;;\n'
            "esac\n")
        fake.chmod(0o755)

    def run_gh_as(self, lane: str | None) -> subprocess.CompletedProcess:
        env = {**os.environ, "PATH": f"{self.bin}:{os.environ['PATH']}", "GH_ACCT_HOME": str(self.home),
               "GH_AS_IDENTITY_TTL": "0", "GH_TOKEN": "inherited-should-be-cleared"}
        env.pop("LANE", None)
        if lane is not None:
            env["LANE"] = lane
        return subprocess.run(["bash", str(GH_AS), "vitalharmony", "gh", "whoami"],
                              capture_output=True, text=True, env=env)

    def test_a_lane_runs_on_its_slot_file_token(self):
        (self.lane_slot / "hosts.yml").write_text(
            "github.com:\n    users:\n        vitalharmony:\n            oauth_token: lane2-tok\n")
        result = self.run_gh_as("2")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("token=lane2-tok", result.stdout)
        self.assertIn(f"dir={self.lane_slot}", result.stdout)

    def test_a_lane_slot_without_a_file_token_refuses(self):
        result = self.run_gh_as("2")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("refusing rather than using the keyring", result.stderr)
        self.assertNotIn("token=", result.stdout)

    def test_a_missing_lane_slot_names_a_repair_that_can_work(self):
        """#1425: --init writes the keyring and checks login == slot name, so it
        can never repair a lane slot; the hint names the slot's own file."""
        import shutil
        shutil.rmtree(self.lane_slot)
        result = self.run_gh_as("2")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(f"GH_CONFIG_DIR={self.lane_slot} gh auth login --with-token", result.stderr)
        self.assertNotIn("gh-as --init", result.stderr)

    def test_a_revoked_lane_token_names_the_lane_slot(self):
        (self.lane_slot / "hosts.yml").write_text(
            "github.com:\n    users:\n        vitalharmony:\n            oauth_token: revoked\n")
        fake = self.bin / "gh"
        fake.write_text("#!/usr/bin/env bash\nexit 1\n")
        result = self.run_gh_as("2")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("slot 'vitalharmony-lane2'", result.stderr)
        self.assertIn(f"GH_CONFIG_DIR={self.lane_slot} gh auth login --with-token", result.stderr)
        self.assertNotIn("gh-as --init", result.stderr)

    def test_no_lane_keeps_the_shared_slot(self):
        result = self.run_gh_as(None)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"dir={self.home / 'vitalharmony'}", result.stdout)
        self.assertNotIn("inherited-should-be-cleared", result.stdout)


if __name__ == "__main__":
    unittest.main()
