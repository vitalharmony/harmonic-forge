"""harmonic-forge#650 -- the gh_shim executable.

Imported as a module (it is a valid, `#!`-prefixed Python file with an
`if __name__ == "__main__"` guard) so its internals can be exercised without
actually exec'ing a real `gh`.
"""
import importlib.machinery
import importlib.util
import os
import pwd
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_SHIM_PATH = Path(__file__).parent / "gh_shim"
_INSTALLER = Path(__file__).parent / "install_gh_shim.sh"


def _load_shim():
    # gh_shim has no .py extension (it's installed as a bare executable), so
    # spec_from_file_location can't infer a loader from the suffix -- supply
    # the source loader explicitly.
    loader = importlib.machinery.SourceFileLoader("gh_shim_module", str(_SHIM_PATH))
    spec = importlib.util.spec_from_file_location("gh_shim_module", _SHIM_PATH, loader=loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def _pinned_env() -> dict:
    """harmonic-forge#1511: os.environ without the gh auth variables, so a lane
    shell's GH_CONFIG_DIR or GH_TOKEN never decides an in-process main() test."""
    return {k: v for k, v in os.environ.items()
            if k not in ("GH_TOKEN", "GITHUB_TOKEN", "GH_CONFIG_DIR", "GH_ACCT_HOME")}


class RealGhResolution(unittest.TestCase):
    def test_skips_itself_when_shim_is_first_on_path(self):
        """Simulate PATH listing a directory containing a copy of this same
        shim before a directory containing the real gh -- resolution must
        skip its own realpath and find the real binary."""
        shim = _load_shim()
        with mock.patch("os.path.isfile", side_effect=lambda p: p.endswith("gh")), \
             mock.patch("os.path.realpath") as fake_realpath, \
             mock.patch.dict("os.environ", {"PATH": "/fake/shim/dir:/usr/bin"}):
            def realpath_side_effect(path):
                if path == "/fake/shim/dir/gh":
                    return shim._self_realpath()
                if path == "/usr/bin/gh":
                    return "/usr/bin/gh"
                return path
            fake_realpath.side_effect = realpath_side_effect
            resolved = shim._resolve_real_gh()
        self.assertEqual(resolved, "/usr/bin/gh")

    def test_falls_back_to_usr_bin_gh_when_nothing_found(self):
        shim = _load_shim()
        with mock.patch("os.path.isfile", return_value=False), \
             mock.patch.dict("os.environ", {"PATH": "/nowhere"}):
            self.assertEqual(shim._resolve_real_gh(), "/usr/bin/gh")


class Refusal(unittest.TestCase):
    def _run_main_with(self, argv, scan_reason=None, override=False, budget_result=(4000, 1.0)):
        shim = _load_shim()
        fake_patterns = mock.Mock()
        fake_patterns.scan_reason.return_value = scan_reason
        fake_patterns.consume_override.return_value = override
        fake_patterns.budget.return_value = budget_result
        # harmonic-forge#1511: pinned, so a lane shell's GH_CONFIG_DIR never decides it.
        pinned = {k: v for k, v in os.environ.items()
                  if k not in ("GH_TOKEN", "GITHUB_TOKEN", "GH_CONFIG_DIR", "GH_ACCT_HOME")}
        with mock.patch.dict(os.environ, pinned, clear=True), \
             mock.patch.object(sys, "argv", ["gh", *argv]), \
             mock.patch.object(shim, "_resolve_real_gh", return_value="/usr/bin/gh"), \
             mock.patch.dict(sys.modules, {"gh_scan_patterns": fake_patterns}), \
             mock.patch("os.execv") as fake_execv:
            try:
                shim.main()
            except SystemExit as exc:
                return exc.code, fake_execv
            return None, fake_execv

    def test_scan_pattern_refuses_with_exit_3(self):
        code, execv = self._run_main_with(["issue", "list"], scan_reason="scan!", override=False)
        self.assertEqual(code, 3)
        execv.assert_not_called()

    def test_override_present_lets_a_scan_through(self):
        code, execv = self._run_main_with(["issue", "list"], scan_reason="scan!", override=True)
        self.assertIsNone(code)
        execv.assert_called_once()

    # harmonic-forge#964: the caller marker, not the argv, decides the exemption.
    def _with_caller(self, caller, **kw):
        env = {k: v for k, v in os.environ.items() if k != "GH_SHIM_CALLER"}
        if caller is not None:
            env["GH_SHIM_CALLER"] = caller
        with mock.patch.dict(os.environ, env, clear=True):
            return self._run_main_with(["project", "item-list", "1"], scan_reason="scan!", **kw)

    def test_each_sanctioned_caller_lets_a_scan_through(self):
        for caller in ("item_list_cache", "batch_preflight", "l1_post", "gh_issue", "ui_grant"):
            code, execv = self._with_caller(caller)
            self.assertIsNone(code, caller)
            execv.assert_called_once()

    def test_an_unknown_empty_or_absent_marker_is_refused(self):
        for caller in ("someone_else", "", None):
            code, execv = self._with_caller(caller)
            self.assertEqual(code, 3, caller)
            execv.assert_not_called()

    def test_the_budget_floor_still_fires_for_a_sanctioned_caller(self):
        code, execv = self._with_caller("item_list_cache", budget_result=(500, 1.0))
        self.assertEqual(code, 3)
        execv.assert_not_called()

    def test_low_budget_refuses_with_exit_3(self):
        code, execv = self._run_main_with(["pr", "view", "5"], scan_reason=None,
                                           budget_result=(500, 1.0))
        self.assertEqual(code, 3)
        execv.assert_not_called()

    def test_healthy_budget_and_no_scan_execs_real_gh(self):
        code, execv = self._run_main_with(["pr", "view", "5"], scan_reason=None,
                                           budget_result=(4000, 1.0))
        self.assertIsNone(code)
        execv.assert_called_once()

    def test_auth_commands_skip_the_budget_check(self):
        shim = _load_shim()
        fake_patterns = mock.Mock()
        fake_patterns.scan_reason.return_value = None
        fake_patterns.consume_override.return_value = False
        with mock.patch.dict(os.environ, _pinned_env(), clear=True), \
             mock.patch.object(sys, "argv", ["gh", "auth", "status"]), \
             mock.patch.object(shim, "_resolve_real_gh", return_value="/usr/bin/gh"), \
             mock.patch.dict(sys.modules, {"gh_scan_patterns": fake_patterns}), \
             mock.patch("os.execv") as fake_execv:
            shim.main()
        fake_patterns.budget.assert_not_called()
        fake_execv.assert_called_once()

    def test_the_identity_probe_is_exempt_from_the_budget_floor(self):
        """harmonic-forge#804: the per-project identity assertion runs before
        every lane post, so a low REST budget must not block it -- or a low
        budget would block every post at its first line."""
        code, execv = self._run_main_with(["api", "user", "--jq", ".login"],
                                           scan_reason=None, budget_result=(500, 1.0))
        self.assertIsNone(code)
        execv.assert_called_once()

    def test_the_identity_probe_does_not_spend_the_operators_override(self):
        """harmonic-forge#804 preclose: the probe is the first gh call of every
        entrypoint, so consuming the one-shot override here would spend it on a call
        that needed none and leave the scan it was touched for still refused."""
        shim = _load_shim()
        fake_patterns = mock.Mock()
        fake_patterns.scan_reason.return_value = None
        fake_patterns.consume_override.return_value = True
        with mock.patch.dict(os.environ, _pinned_env(), clear=True), \
             mock.patch.object(sys, "argv", ["gh", "api", "user", "--jq", ".login"]), \
             mock.patch.object(shim, "_resolve_real_gh", return_value="/usr/bin/gh"), \
             mock.patch.dict(sys.modules, {"gh_scan_patterns": fake_patterns}), \
             mock.patch("os.execv") as fake_execv:
            shim.main()
        fake_patterns.consume_override.assert_not_called()
        fake_patterns.scan_reason.assert_not_called()
        fake_execv.assert_called_once()

    def test_a_lookalike_api_user_call_is_not_exempt(self):
        """Only the exact probe is exempt: other `api user` shapes still pay."""
        code, execv = self._run_main_with(["api", "user", "--jq", ".email"],
                                           scan_reason=None, budget_result=(500, 1.0))
        self.assertEqual(code, 3)
        execv.assert_not_called()

    def test_internal_exception_fails_open(self):
        """A bug in the shim must never brick `gh` entirely."""
        shim = _load_shim()
        fake_patterns = mock.Mock()
        fake_patterns.consume_override.return_value = False
        fake_patterns.scan_reason.side_effect = RuntimeError("boom")
        with mock.patch.dict(os.environ, _pinned_env(), clear=True), \
             mock.patch.object(sys, "argv", ["gh", "pr", "view", "5"]), \
             mock.patch.object(shim, "_resolve_real_gh", return_value="/usr/bin/gh"), \
             mock.patch.dict(sys.modules, {"gh_scan_patterns": fake_patterns}), \
             mock.patch("os.execv") as fake_execv:
            shim.main()
        fake_execv.assert_called_once()


class LaneSlotToken(unittest.TestCase):
    """harmonic-forge#1511: a lane slot named by GH_CONFIG_DIR with no token runs
    on that slot's own token, or is refused; everything else is unchanged.

    Every case builds its own environment (a lane shell exports GH_CONFIG_DIR and
    GH_TOKEN, and must not decide these results). The token is applied first in
    main(), so the close/merge gate and the budget probe inherit it too."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "accounts"
        self.root.mkdir()
        self.addCleanup(self._tmp.cleanup)

    def _slot(self, name, token=None, root=None):
        d = (root or self.root) / name
        d.mkdir(parents=True)
        if token is not None:
            (d / "hosts.yml").write_text(
                f"github.com:\n    user: vitalharmony\n    oauth_token: {token}\n", encoding="utf-8")
        return d

    def _env(self, env):
        base = {k: v for k, v in os.environ.items()
                if k not in ("GH_TOKEN", "GITHUB_TOKEN", "GH_CONFIG_DIR", "GH_ACCT_HOME",
                             "GH_SHIM_CALLER")}
        base["GH_ACCT_HOME"] = str(self.root)
        base.update(env)
        return base

    def _main(self, argv, env):
        """Run main() with the gate and the budget probe recorded: what GH_TOKEN
        each saw, and what the final exec saw."""
        shim = _load_shim()
        seen = {}
        fake_patterns = mock.Mock()
        fake_patterns.scan_reason.return_value = None
        fake_patterns.consume_override.return_value = False

        def budget():
            seen["budget"] = os.environ.get("GH_TOKEN")
            return (4000, 1.0)

        def gate(_argv, _real_gh):
            seen["gate"] = os.environ.get("GH_TOKEN")

        def execv(_path, _args):
            seen["exec"] = os.environ.get("GH_TOKEN")

        fake_patterns.budget.side_effect = budget
        with mock.patch.dict(os.environ, self._env(env), clear=True), \
             mock.patch.object(sys, "argv", ["gh", *argv]), \
             mock.patch.object(shim, "_resolve_real_gh", return_value="/usr/bin/gh"), \
             mock.patch.object(shim, "_close_merge_gate", side_effect=gate), \
             mock.patch.dict(sys.modules, {"gh_scan_patterns": fake_patterns}), \
             mock.patch("os.execv", side_effect=execv) as fake_execv:
            try:
                shim.main()
            except SystemExit as exc:
                return exc.code, seen, fake_execv
            return None, seen, fake_execv

    def test_a_lane_slot_with_no_token_runs_on_the_slots_own_token(self):
        d = self._slot("vitalharmony-lane1", token="T1")
        code, seen, execv = self._main(["pr", "view", "5"], {"GH_CONFIG_DIR": str(d)})
        self.assertIsNone(code)
        execv.assert_called_once_with("/usr/bin/gh", ["/usr/bin/gh", "pr", "view", "5"])
        self.assertEqual(seen, {"gate": "T1", "budget": "T1", "exec": "T1"})

    def test_a_lane_slot_with_no_readable_token_is_refused_before_the_gate(self):
        d = self._slot("vitalharmony-lane2")
        with mock.patch("sys.stderr") as err:
            code, seen, execv = self._main(["pr", "view", "5"], {"GH_CONFIG_DIR": str(d)})
        self.assertEqual(code, 4)
        self.assertEqual(seen, {})
        execv.assert_not_called()
        self.assertIn("vitalharmony-lane2", "".join(str(c) for c in err.write.call_args_list))

    def test_a_token_already_set_is_left_alone(self):
        d = self._slot("vitalharmony-lane1", token="T1")
        for key in ("GH_TOKEN", "GITHUB_TOKEN"):
            code, seen, execv = self._main(["pr", "view", "5"], {"GH_CONFIG_DIR": str(d), key: "X"})
            self.assertIsNone(code, key)
            execv.assert_called_once()
            self.assertEqual(seen["exec"], "X" if key == "GH_TOKEN" else None, key)

    def test_auth_is_never_modified(self):
        d = self._slot("vitalharmony-lane1", token="T1")
        for argv in (["auth", "login", "--with-token"], ["auth", "token"]):
            code, seen, execv = self._main(argv, {"GH_CONFIG_DIR": str(d)})
            self.assertIsNone(code, argv)
            execv.assert_called_once()
            self.assertIsNone(seen["exec"], argv)

    def test_other_slots_and_outside_dirs_are_unchanged(self):
        dirs = [self._slot(n, token="T") for n in
                ("vitalharmony", "harmonicarchitect", "vitalharmony-projects")]
        dirs.append(self._slot("vitalharmony-lane1", token="T",
                               root=Path(self._tmp.name) / "elsewhere"))
        for d in dirs:
            code, seen, execv = self._main(["pr", "view", "5"], {"GH_CONFIG_DIR": str(d)})
            self.assertIsNone(code, d)
            execv.assert_called_once()
            self.assertIsNone(seen["exec"], d)

    def test_an_error_identifying_the_slot_fails_open(self):
        shim = _load_shim()
        with mock.patch.dict(os.environ, self._env({}), clear=True), \
             mock.patch.object(shim, "_lane_slot", side_effect=TypeError("boom")):
            shim._apply_lane_slot_token(["pr", "view", "5"])
            self.assertNotIn("GH_TOKEN", os.environ)

    def test_no_config_dir_is_unchanged(self):
        code, seen, execv = self._main(["pr", "view", "5"], {})
        self.assertIsNone(code)
        execv.assert_called_once()
        self.assertIsNone(seen["exec"])

    def test_every_real_gh_exec_goes_through_the_helper(self):
        source = _SHIM_PATH.read_text(encoding="utf-8")
        helper = source.index("def _exec_real_gh(")
        end = source.index("\ndef ", helper + 1)
        outside = source[:helper] + source[end:]
        self.assertNotIn("os.execv(", outside)
        self.assertNotIn("os.execve(", source)
        self.assertGreaterEqual(outside.count("_exec_real_gh(real_gh, argv)"), 3)
        main_src = source[source.index("def main()"):]
        self.assertLess(main_src.index("_apply_lane_slot_token(argv)"),
                        main_src.index("_close_merge_gate(argv"))


class StateDirStep(unittest.TestCase):
    """harmonic-forge#1521: a gh call with neither HOME nor XDG_STATE_HOME must not write its
    state into the working directory, because the real gh then uses the relative path
    `.local/state/gh/device-id`. The shim supplies the state dir gh would have used."""

    PASSWD_HOME = "/home/someone"

    def _env_at_exec(self, env: dict, argv=("--version",), getpwuid=None) -> dict:
        """`main()` on a pinned environment with exactly `env`'s HOME and XDG_STATE_HOME; the
        environment as the real gh would receive it at the (mocked) exec."""
        shim = _load_shim()
        fake_patterns = mock.Mock()
        fake_patterns.scan_reason.return_value = None
        fake_patterns.consume_override.return_value = False
        fake_patterns.budget.return_value = (5000, 1.0)
        base = {k: v for k, v in _pinned_env().items() if k not in ("HOME", "XDG_STATE_HOME")}
        seen = {}
        fake_pw = getpwuid or mock.Mock(return_value=mock.Mock(pw_dir=self.PASSWD_HOME))
        with mock.patch.dict(os.environ, {**base, **env}, clear=True), \
             mock.patch.object(sys, "argv", ["gh", *argv]), \
             mock.patch.object(shim, "_resolve_real_gh", return_value="/usr/bin/gh"), \
             mock.patch.object(shim.pwd, "getpwuid", fake_pw), \
             mock.patch.dict(sys.modules, {"gh_scan_patterns": fake_patterns}), \
             mock.patch("os.execv", side_effect=lambda *a: seen.update(dict(os.environ))):
            shim.main()
        return seen

    def test_neither_variable_set_gives_an_absolute_state_dir_outside_the_cwd(self):
        env = self._env_at_exec({})
        self.assertEqual(env["XDG_STATE_HOME"], f"{self.PASSWD_HOME}/.local/state")
        self.assertTrue(os.path.isabs(env["XDG_STATE_HOME"]))
        self.assertFalse(Path(env["XDG_STATE_HOME"]).is_relative_to(Path.cwd()))

    def test_the_real_passwd_home_is_used_when_nothing_is_mocked(self):
        shim = _load_shim()
        fake_patterns = mock.Mock()
        fake_patterns.scan_reason.return_value = None
        fake_patterns.consume_override.return_value = False
        base = {k: v for k, v in _pinned_env().items() if k not in ("HOME", "XDG_STATE_HOME")}
        seen = {}
        with mock.patch.dict(os.environ, base, clear=True), \
             mock.patch.object(sys, "argv", ["gh", "--version"]), \
             mock.patch.object(shim, "_resolve_real_gh", return_value="/usr/bin/gh"), \
             mock.patch.dict(sys.modules, {"gh_scan_patterns": fake_patterns}), \
             mock.patch("os.execv", side_effect=lambda *a: seen.update(dict(os.environ))):
            shim.main()
        self.assertEqual(seen["XDG_STATE_HOME"],
                         os.path.join(pwd.getpwuid(os.getuid()).pw_dir, ".local", "state"))

    def test_an_empty_variable_counts_as_unset(self):
        for env in ({"HOME": ""}, {"XDG_STATE_HOME": ""}, {"HOME": "", "XDG_STATE_HOME": ""}):
            with self.subTest(env=env):
                self.assertEqual(self._env_at_exec(env)["XDG_STATE_HOME"], f"{self.PASSWD_HOME}/.local/state")

    def test_a_set_home_leaves_the_environment_unchanged(self):
        env = self._env_at_exec({"HOME": "/home/other"})
        self.assertEqual(env["HOME"], "/home/other")
        self.assertNotIn("XDG_STATE_HOME", env)

    def test_a_set_xdg_state_home_is_kept_as_it_is(self):
        env = self._env_at_exec({"XDG_STATE_HOME": "/var/tmp/custom-state"})
        self.assertEqual(env["XDG_STATE_HOME"], "/var/tmp/custom-state")
        self.assertNotIn("HOME", env)
        both = self._env_at_exec({"HOME": "/home/other", "XDG_STATE_HOME": "/var/tmp/custom-state"})
        self.assertEqual((both["HOME"], both["XDG_STATE_HOME"]), ("/home/other", "/var/tmp/custom-state"))

    def test_a_failed_passwd_lookup_leaves_the_environment_unchanged(self):
        for error in (KeyError("getpwuid(): uid not found"), OSError("passwd unreadable")):
            with self.subTest(error=type(error).__name__):
                env = self._env_at_exec({}, getpwuid=mock.Mock(side_effect=error))
                self.assertNotIn("XDG_STATE_HOME", env)
                self.assertNotIn("HOME", env)

    def test_an_unusable_passwd_home_is_not_turned_into_a_relative_path(self):
        """An empty or relative home would only recreate the relative `.local/state`."""
        for home in ("", "relative/home", None):
            with self.subTest(home=home):
                env = self._env_at_exec({}, getpwuid=mock.Mock(return_value=mock.Mock(pw_dir=home)))
                self.assertNotIn("XDG_STATE_HOME", env)

    def test_it_runs_for_every_kind_of_call_including_the_identity_probe_and_auth(self):
        for argv in (["--version"], ["auth", "token"], ["api", "user", "--jq", ".login"], ["issue", "view", "1"]):
            with self.subTest(argv=argv):
                self.assertEqual(self._env_at_exec({}, argv=tuple(argv))["XDG_STATE_HOME"],
                                 f"{self.PASSWD_HOME}/.local/state")

    def test_it_runs_before_the_lane_slot_token_step_so_every_subprocess_inherits_it(self):
        shim = _load_shim()
        order = []
        fake_patterns = mock.Mock()
        fake_patterns.scan_reason.return_value = None
        fake_patterns.consume_override.return_value = False
        base = {k: v for k, v in _pinned_env().items() if k not in ("HOME", "XDG_STATE_HOME")}
        with mock.patch.dict(os.environ, base, clear=True), \
             mock.patch.object(sys, "argv", ["gh", "--version"]), \
             mock.patch.object(shim, "_resolve_real_gh", return_value="/usr/bin/gh"), \
             mock.patch.object(shim.pwd, "getpwuid", return_value=mock.Mock(pw_dir=self.PASSWD_HOME)), \
             mock.patch.object(shim, "_apply_lane_slot_token",
                               side_effect=lambda argv: order.append(os.environ.get("XDG_STATE_HOME"))), \
             mock.patch.dict(sys.modules, {"gh_scan_patterns": fake_patterns}), \
             mock.patch("os.execv"):
            shim.main()
        self.assertEqual(order, [f"{self.PASSWD_HOME}/.local/state"])

    @unittest.skipUnless(os.path.exists("/usr/bin/gh"), "needs the real gh at /usr/bin/gh")
    def test_a_scrubbed_environment_through_the_real_shim_leaves_nothing_in_the_cwd(self):
        """The case the issue names, end to end: `gh --version` through the shim with `env -i`,
        run from a scratch directory, leaves no `.local/` there. Without the step the real gh
        creates `<cwd>/.local/state/gh/device-id`."""
        with tempfile.TemporaryDirectory() as scratch:
            result = subprocess.run(
                [sys.executable, str(_SHIM_PATH), "--version"], cwd=scratch, capture_output=True,
                text=True, env={"PATH": "/usr/bin:/bin"})
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("gh version", result.stdout)
            self.assertEqual(os.listdir(scratch), [], "the real gh wrote into its working directory")


class Installer(unittest.TestCase):
    """Exercises install_gh_shim.sh's idempotent-link logic against a
    throwaway temp HOME/PATH -- never the real operator environment."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.home = Path(self._tmpdir.name)
        self.local_bin = self.home / ".local" / "bin"

    def tearDown(self):
        self._tmpdir.cleanup()

    def _run_installer(self):
        env = dict(os.environ)
        env["HOME"] = str(self.home)
        # command -v gh must resolve through the freshly-populated PATH.
        env["PATH"] = f"{self.local_bin}:{env.get('PATH', '')}"
        return subprocess.run(
            ["bash", str(_INSTALLER)], capture_output=True, text=True, env=env,
        )

    def test_creates_local_bin_and_links_when_absent(self):
        result = self._run_installer()
        self.assertEqual(result.returncode, 0, result.stderr)
        target = self.local_bin / "gh"
        self.assertTrue(target.is_symlink())
        self.assertEqual(os.path.realpath(target), os.path.realpath(_SHIM_PATH))

    def test_idempotent_when_rerun(self):
        first = self._run_installer()
        self.assertEqual(first.returncode, 0, first.stderr)
        second = self._run_installer()
        self.assertEqual(second.returncode, 0, second.stderr)

    def test_refuses_to_overwrite_a_real_file(self):
        self.local_bin.mkdir(parents=True)
        real_gh = self.local_bin / "gh"
        real_gh.write_text("#!/bin/sh\necho not the shim\n")
        real_gh.chmod(0o755)
        result = self._run_installer()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("already exists", result.stderr)
        # Must not have been touched.
        self.assertFalse(real_gh.is_symlink())

    def test_refuses_to_overwrite_a_symlink_to_something_else(self):
        self.local_bin.mkdir(parents=True)
        elsewhere = self.home / "some_other_gh"
        elsewhere.write_text("#!/bin/sh\n")
        elsewhere.chmod(0o755)
        (self.local_bin / "gh").symlink_to(elsewhere)
        result = self._run_installer()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not this repo's gh_shim", result.stderr)

    def test_a_dotdot_spelling_of_local_bin_on_path_is_the_same_dir(self):  # harmonic-forge#960
        self.local_bin.mkdir(parents=True)
        (self.home / ".local" / "share").mkdir()
        env = dict(os.environ, HOME=str(self.home),
                   PATH=f"{self.home}/.local/share/../bin:{os.environ.get('PATH', '')}")
        result = subprocess.run(["bash", str(_INSTALLER)], capture_output=True, text=True, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
