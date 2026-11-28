#!/usr/bin/env python3
"""Unit tests for mypy_cwd_trap.py (harmonic-forge#167).
Run: python3 tools/hooks/test_mypy_cwd_trap.py"""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))
import mypy_cwd_trap as m


def _is_denied(result: dict) -> bool:
    return result.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"


class TestMypyCwdTrap(unittest.TestCase):
    def setUp(self):
        # Every case below runs as if in an HRSE2-shaped repo (has
        # backend/mypy.ini) unless a test overrides it.
        patcher = patch.object(m, "_repo_has_mypy_trap", return_value=True)
        self.addCleanup(patcher.stop)
        patcher.start()

    # --- Correct forms — must be allowed --------------------------------

    def test_bare_mypy_with_cwd_already_backend(self):
        result = m.decision(".venv/bin/mypy app", "/home/x/HRSE2/backend")
        self.assertFalse(_is_denied(result))

    def test_cd_backend_then_mypy(self):
        result = m.decision("cd backend && .venv/bin/mypy app", "/home/x/HRSE2")
        self.assertFalse(_is_denied(result))

    def test_scratch_worktree_backend_cwd(self):
        result = m.decision("mypy app", "/tmp/hrse2-700-impl/backend")
        self.assertFalse(_is_denied(result))

    def test_explicit_config_file_flag(self):
        result = m.decision("mypy --config-file backend/mypy.ini backend/app", "/home/x/HRSE2")
        self.assertFalse(_is_denied(result))

    def test_explicit_config_file_env_inline(self):
        result = m.decision("MYPY_CONFIG_FILE=backend/mypy.ini mypy backend/app", "/home/x/HRSE2")
        self.assertFalse(_is_denied(result))

    # --- Incorrect forms — must be denied --------------------------------

    def test_repo_root_mypy_app_wrong_cwd(self):
        result = m.decision("mypy app", "/home/x/HRSE2")
        self.assertTrue(_is_denied(result))

    def test_repo_root_absolute_path(self):
        result = m.decision(".venv/bin/mypy backend/app", "/home/x/HRSE2")
        self.assertTrue(_is_denied(result))

    def test_python_module_form_wrong_cwd(self):
        result = m.decision("python3 -m mypy app", "/home/x/HRSE2")
        self.assertTrue(_is_denied(result))

    def test_uv_run_form_wrong_cwd(self):
        result = m.decision("uv run mypy app", "/home/x/HRSE2")
        self.assertTrue(_is_denied(result))

    # --- Never intercepted -------------------------------------------------

    def test_grep_mypy_not_intercepted(self):
        result = m.decision("grep mypy CLAUDE.md", "/home/x/HRSE2")
        self.assertEqual(result, {})

    def test_cat_mise_toml_not_intercepted(self):
        result = m.decision("cat mise.toml", "/home/x/HRSE2")
        self.assertEqual(result, {})

    def test_env_only_segment_does_not_crash(self):
        # A real line shape from scripts/verify_vertical_boundary.sh.
        result = m.decision('MYPY="$scratch/.venv/bin/mypy"', "/home/x/HRSE2")
        self.assertEqual(result, {})

    # --- Heredoc masking ----------------------------------------------------

    def test_heredoc_with_unbalanced_apostrophe_still_catches_mypy(self):
        command = (
            "git commit -m \"$(cat <<'EOF'\n"
            "fix: don't break things\n"
            "EOF\n"
            ")\" && mypy app"
        )
        result = m.decision(command, "/home/x/HRSE2")
        self.assertTrue(_is_denied(result))

    # --- Repo scoping --------------------------------------------------------

    def test_harmonic_forge_repo_has_no_trap_never_denied(self):
        with patch.object(m, "_repo_has_mypy_trap", return_value=False):
            result = m.decision("mypy tools/hooks", "/home/x/harmonic-forge")
        self.assertEqual(result, {})

    # --- Fail-open --------------------------------------------------------

    def test_non_string_command_fails_open(self):
        result = m.decision(None, "/home/x/HRSE2")
        self.assertEqual(result, {})


#: harmonic-forge#1608 P3: `Path.expanduser()` raises RuntimeError on an unknown
#: `~user`, so a reverted guard raises here instead of deciding.
UNKNOWN_HOME = "~nosuchuser-1608/x"


def _git_repo(path: Path, trap: bool) -> Path:
    import subprocess
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    if trap:
        (path / "backend").mkdir()
        (path / "backend" / "mypy.ini").write_text("[mypy]\n")
    return path


class Issue1608UnknownUserHome(unittest.TestCase):
    """harmonic-forge#1608 (two-pass K2): bash leaves an unknown `~user`
    literal, so `cd ~nosuchuser-1608` enters `<cwd>/~nosuchuser-1608` when it
    exists and otherwise fails, leaving the cwd. Real repos, no patched trap,
    so the test asserts which directory decided."""

    def setUp(self):
        import tempfile
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def test_cd_to_an_unknown_user_home_leaves_the_cwd_and_still_denies(self):
        # A missing literal directory: the `cd` fails, mypy runs from the
        # hrse-shaped repo root, and the trap applies.
        hrse = _git_repo(self.root / "hrse", trap=True)
        result = m.decision(f"cd {UNKNOWN_HOME}; .venv/bin/mypy app", str(hrse))
        self.assertTrue(_is_denied(result))

    def test_cd_to_an_existing_literal_unknown_user_dir_follows_it_and_denies(self):
        # A forge-shaped cwd (no trap) holding a `~nosuchuser-1608` symlink to
        # an hrse-shaped repo: bash runs mypy in hrse, so the hook denies, the
        # same as `cd <absolute hrse path>`.
        forge = _git_repo(self.root / "forge", trap=False)
        hrse = _git_repo(self.root / "hrse", trap=True)
        (forge / "~nosuchuser-1608").symlink_to(hrse)
        self.assertFalse(_is_denied(m.decision(".venv/bin/mypy app", str(forge))))
        self.assertTrue(_is_denied(m.decision(f"cd {hrse} && .venv/bin/mypy app", str(forge))))
        self.assertTrue(_is_denied(m.decision("cd ~nosuchuser-1608 && .venv/bin/mypy app", str(forge))))


if __name__ == "__main__":
    unittest.main()
