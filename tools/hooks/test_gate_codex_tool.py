"""Focused contract tests for the Codex PreToolUse command guard."""

import importlib.util
import io
import json
import os
import subprocess
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).with_name("gate_codex_tool.py")
SPEC = importlib.util.spec_from_file_location("gate_codex_tool", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def _run_main(payload: dict) -> tuple[int, str]:
    stdout = io.StringIO()
    with patch("sys.stdin", io.StringIO(json.dumps(payload))), patch("sys.stdout", stdout):
        rc = MODULE.main()
    return rc, stdout.getvalue()


class GateCodexToolTests(unittest.TestCase):
    def test_blocks_persistent_mutation_paths_for_lane3(self) -> None:
        with patch.dict(os.environ, {"LANE": "3"}):
            self.assertEqual(MODULE.blocked_reason(["git", "commit", "-m", "x"], True), "git commit is not allowed")
        self.assertEqual(MODULE.blocked_reason(["bash", "-c", "git commit"], False), "shell -c indirection is not allowed")

    def test_allows_git_mutation_when_not_lane3(self) -> None:
        self.assertIsNone(MODULE.blocked_reason(["git", "commit", "-m", "x"], False))
        self.assertIsNone(MODULE.blocked_reason(["git", "checkout", "--", "file"], False))

    def test_allows_read_only_command(self) -> None:
        self.assertIsNone(MODULE.blocked_reason(["git", "status", "--short"], True))

    def test_is_lane3_session_lane_set_to_3(self) -> None:
        with patch.dict(os.environ, {"LANE": "3"}):
            self.assertTrue(MODULE.is_lane3_session(Path.cwd()))

    def test_is_lane3_session_lane_set_to_other(self) -> None:
        with patch.dict(os.environ, {"LANE": "2"}):
            self.assertFalse(MODULE.is_lane3_session(Path.cwd()))

    def test_is_lane3_session_lane_unset_no_marker(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with patch.object(MODULE._canonical, "lane3_task_without_marker", return_value="gate-checkout"):
                self.assertFalse(MODULE.is_lane3_session(Path.cwd()))

    def test_is_lane3_session_lane_unset_fresh_marker(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with patch.object(MODULE._canonical, "lane3_task_without_marker", return_value=None):
                self.assertTrue(MODULE.is_lane3_session(Path.cwd()))

    def test_uses_codex_pretool_deny_shape(self) -> None:
        payload = {"tool_name": "Bash", "tool_input": {"command": "git push"}, "cwd": str(Path.cwd())}
        with patch.dict(os.environ, {"LANE": "3"}):
            rc, out = _run_main(payload)
        self.assertEqual(rc, 0)
        output = json.loads(out)
        self.assertEqual(output["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertEqual(output["hookSpecificOutput"]["hookEventName"], "PreToolUse")

    def test_git_push_allowed_when_lane2(self) -> None:
        payload = {"tool_name": "Bash", "tool_input": {"command": "git push"}, "cwd": str(Path.cwd())}
        with patch.dict(os.environ, {"LANE": "2"}):
            rc, out = _run_main(payload)
        self.assertEqual(rc, 0)
        self.assertEqual(out, "")

    def test_ignores_non_bash_tools(self) -> None:
        payload = {"tool_name": "apply_patch", "tool_input": {"command": "anything"}}
        rc, out = _run_main(payload)
        self.assertEqual(rc, 0)
        self.assertEqual(out, "")

    def test_gate_task_denied_when_lane2(self) -> None:
        payload = {"tool_name": "Bash", "tool_input": {"command": "mise run gate-checkout foo"}, "cwd": str(Path.cwd())}
        with patch.dict(os.environ, {"LANE": "2"}):
            rc, out = _run_main(payload)
        self.assertEqual(rc, 0)
        output = json.loads(out)
        self.assertEqual(output["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_gate_task_allowed_when_lane3(self) -> None:
        # Isolates the LANE-conditional check this test is named for from
        # the separate canonical-location check  covered by
        # GateWorktreeLocationDenialTests below -- patched to a no-op here
        # so this test's cwd (wherever the suite happens to run from)
        # doesn't need to actually be named `-lane3`.
        payload = {"tool_name": "Bash", "tool_input": {"command": "mise run gate-checkout foo"}, "cwd": str(Path.cwd())}
        with patch.dict(os.environ, {"LANE": "3"}), patch.object(MODULE, "_canonical_lane3_worktree", return_value=Path.cwd()):
            rc, out = _run_main(payload)
        self.assertEqual(rc, 0)
        self.assertEqual(out, "")

    def test_gate_task_denied_in_compound_command_when_lane2(self) -> None:
        payload = {
            "tool_name": "Bash",
            "tool_input": {"command": f"cd {Path.cwd()} && mise run gate-checkout foo"},
            "cwd": str(Path.cwd()),
        }
        with patch.dict(os.environ, {"LANE": "2"}):
            rc, out = _run_main(payload)
        self.assertEqual(rc, 0)
        output = json.loads(out)
        self.assertEqual(output["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_direct_post_denied_when_lane_unset(self) -> None:
        payload = {
            "tool_name": "Bash",
            "tool_input": {"command": 'gh issue comment 230 --body "PASS"'},
            "cwd": str(Path.cwd()),
        }
        with patch.dict(os.environ, {}, clear=True):
            rc, out = _run_main(payload)
        self.assertEqual(rc, 0)
        output = json.loads(out)
        self.assertEqual(output["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_direct_post_denied_when_lane1(self) -> None:
        payload = {
            "tool_name": "Bash",
            "tool_input": {"command": 'gh issue comment 230 --body "PASS"'},
            "cwd": str(Path.cwd()),
        }
        with patch.dict(os.environ, {"LANE": "1"}):
            rc, out = _run_main(payload)
        self.assertEqual(rc, 0)
        output = json.loads(out)
        self.assertEqual(output["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_direct_post_allowed_when_lane3(self) -> None:
        payload = {
            "tool_name": "Bash",
            "tool_input": {"command": 'gh issue comment 230 --body "PASS"'},
            "cwd": str(Path.cwd()),
        }
        with patch.dict(os.environ, {"LANE": "3"}):
            rc, out = _run_main(payload)
        self.assertEqual(rc, 0)
        self.assertEqual(out, "")

    def test_direct_post_allowed_when_lane2(self) -> None:
        payload = {
            "tool_name": "Bash",
            "tool_input": {"command": 'gh issue comment 230 --body "PASS"'},
            "cwd": str(Path.cwd()),
        }
        with patch.dict(os.environ, {"LANE": "2"}):
            rc, out = _run_main(payload)
        self.assertEqual(rc, 0)
        self.assertEqual(out, "")


class BulkCommentReadDenialTests(unittest.TestCase):
    """harmonic-forge#257: four separate Lane 3 contamination incidents on
    one issue, each via a different bulk-comment-read command. These are
    the literal commands that actually caused each incident."""

    def test_gh_issue_view_comments_denied_for_lane3(self) -> None:
        payload = {
            "tool_name": "Bash",
            "tool_input": {"command": "gh issue view 100 --repo OWNER/REPO --comments"},
            "cwd": str(Path.cwd()),
        }
        with patch.dict(os.environ, {"LANE": "3"}):
            rc, out = _run_main(payload)
        self.assertEqual(rc, 0)
        output = json.loads(out)
        self.assertEqual(output["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_gh_api_bulk_comments_paginate_denied_for_lane3(self) -> None:
        payload = {
            "tool_name": "Bash",
            "tool_input": {"command": "gh api repos/OWNER/REPO/issues/100/comments --paginate"},
            "cwd": str(Path.cwd()),
        }
        with patch.dict(os.environ, {"LANE": "3"}):
            rc, out = _run_main(payload)
        self.assertEqual(rc, 0)
        output = json.loads(out)
        self.assertEqual(output["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_gh_api_bulk_comments_with_jq_preview_denied_for_lane3(self) -> None:
        payload = {
            "tool_name": "Bash",
            "tool_input": {
                "command": (
                    "gh api repos/OWNER/REPO/issues/100/comments --paginate "
                    "--jq '.[] | {id,user,created_at,body_start:(.body[0:200])}'"
                )
            },
            "cwd": str(Path.cwd()),
        }
        with patch.dict(os.environ, {"LANE": "3"}):
            rc, out = _run_main(payload)
        self.assertEqual(rc, 0)
        output = json.loads(out)
        self.assertEqual(output["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_gh_api_bulk_comments_denied_in_compound_command_for_lane3(self) -> None:
        payload = {
            "tool_name": "Bash",
            "tool_input": {
                "command": "cd project-lane3 && gh api repos/OWNER/REPO/issues/100/comments --paginate"
            },
            "cwd": str(Path.cwd()),
        }
        with patch.dict(os.environ, {"LANE": "3"}):
            rc, out = _run_main(payload)
        self.assertEqual(rc, 0)
        output = json.loads(out)
        self.assertEqual(output["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_fetch_lane1_context_script_allowed_for_lane3(self) -> None:
        payload = {
            "tool_name": "Bash",
            "tool_input": {
                "command": "python3 ~/harmonic-forge/tools/gh/fetch_lane1_context.py --repo OWNER/REPO --issue 100"
            },
            "cwd": str(Path.cwd()),
        }
        with patch.dict(os.environ, {"LANE": "3"}):
            rc, out = _run_main(payload)
        self.assertEqual(rc, 0)
        self.assertEqual(out, "")

    def test_single_comment_by_id_allowed_for_lane3(self) -> None:
        payload = {
            "tool_name": "Bash",
            "tool_input": {"command": "gh api repos/OWNER/REPO/issues/comments/1000000001"},
            "cwd": str(Path.cwd()),
        }
        with patch.dict(os.environ, {"LANE": "3"}):
            rc, out = _run_main(payload)
        self.assertEqual(rc, 0)
        self.assertEqual(out, "")

    def test_issue_body_only_fetch_allowed_for_lane3(self) -> None:
        payload = {
            "tool_name": "Bash",
            "tool_input": {"command": "gh api repos/OWNER/REPO/issues/100 --jq .body"},
            "cwd": str(Path.cwd()),
        }
        with patch.dict(os.environ, {"LANE": "3"}):
            rc, out = _run_main(payload)
        self.assertEqual(rc, 0)
        self.assertEqual(out, "")

    def test_gh_api_bulk_comments_allowed_for_lane2(self) -> None:
        payload = {
            "tool_name": "Bash",
            "tool_input": {"command": "gh api repos/OWNER/REPO/issues/100/comments --paginate"},
            "cwd": str(Path.cwd()),
        }
        with patch.dict(os.environ, {"LANE": "2"}):
            rc, out = _run_main(payload)
        self.assertEqual(rc, 0)
        self.assertEqual(out, "")

    def test_gh_issue_view_comments_allowed_for_lane1(self) -> None:
        payload = {
            "tool_name": "Bash",
            "tool_input": {"command": "gh issue view 100 --repo OWNER/REPO --comments"},
            "cwd": str(Path.cwd()),
        }
        with patch.dict(os.environ, {"LANE": "1"}):
            rc, out = _run_main(payload)
        self.assertEqual(rc, 0)
        self.assertEqual(out, "")


class CloudCliDenialTests(unittest.TestCase):
    """the shared cloud-CLI policy: the Codex half of the shared Lane 3 cloud-CLI policy.

    The shared module owns the allow-list and has its own exhaustive suite
    (`harmonic-forge/tools/hooks/test_lane3_cloud_cli_policy.py`). What these
    assert is the *wiring*: that Codex reaches the policy at all, that it is
    Lane-3-gated, and that it fails closed — the three things that live on
    this side of the seam.
    """

    def test_mutating_kubectl_denied_for_lane3(self) -> None:
        self.assertIsNotNone(MODULE.cloud_cli_denial("kubectl delete pod p", True))

    def test_safe_kubectl_allowed_for_lane3(self) -> None:
        self.assertIsNone(MODULE.cloud_cli_denial("kubectl get nodes", True))

    def test_no_opinion_when_not_lane3(self) -> None:
        # AC3: a Lane-3-scoped concern. Lane 2 keeps its normal cloud surface.
        self.assertIsNone(MODULE.cloud_cli_denial("kubectl delete pod p", False))

    def test_compound_command_bypass_denied(self) -> None:
        # The class `blocked_reason()` cannot catch: it inspects only args[0]
        # of the whole command, so `true` would pass unexamined.
        self.assertIsNone(MODULE.blocked_reason(MODULE.normalize("true && kubectl delete ns/foo"), True))
        self.assertIsNotNone(MODULE.cloud_cli_denial("true && kubectl delete ns/foo", True))

    def test_wrapper_prefix_bypasses_denied(self) -> None:
        for command in (
            "env KUBECONFIG=x kubectl delete pod p",
            "sudo kubectl delete pod p",
            "timeout 30 kubectl delete pod p",
            "xargs kubectl delete pod",
        ):
            with self.subTest(command=command):
                self.assertIsNotNone(MODULE.cloud_cli_denial(command, True))

    def test_unparseable_command_fails_closed(self) -> None:
        reason = MODULE.cloud_cli_denial('kubectl delete pod "unterminated', True)
        self.assertIsNotNone(reason)
        self.assertIn("could not be parsed", reason)

    def test_reaches_the_cascade_end_to_end_for_lane3(self) -> None:
        with patch.dict(os.environ, {"LANE": "3"}):
            rc, out = _run_main({
                "tool_name": "Bash", "cwd": str(Path.cwd()),
                "tool_input": {"command": "kubectl delete pod p"},
            })
        self.assertEqual(rc, 0)
        decision = json.loads(out)["hookSpecificOutput"]
        self.assertEqual(decision["permissionDecision"], "deny")
        self.assertIn("read-only cloud-CLI allow-list", decision["permissionDecisionReason"])

    def test_safe_command_passes_the_whole_cascade_for_lane3(self) -> None:
        with patch.dict(os.environ, {"LANE": "3"}):
            rc, out = _run_main({
                "tool_name": "Bash", "cwd": str(Path.cwd()),
                "tool_input": {"command": "kubectl get nodes"},
            })
        self.assertEqual(rc, 0)
        self.assertEqual(out, "")

    def test_is_lane3_session_delegates_to_the_shared_probe(self) -> None:
        # NC3: one copy of the three-case logic, in the shared layer.
        with patch.object(MODULE._cloud_policy, "is_lane3_session", return_value=True) as probe:
            self.assertTrue(MODULE.is_lane3_session(Path.cwd()))
        probe.assert_called_once()


class AeSweepSelfPostWiringTests(unittest.TestCase):
    """harmonic-forge#407 Q1: before this, the AE/gate-readiness-sweep
    self-authorization guard existed only for Claude Code (registered in
    this repo's tracked `.claude/settings.json`) -- a Codex-filled Lane 3
    session had no equivalent at all. These assert the *wiring* only --
    that Codex reaches the canonical hook's `decision()` at all and
    translates its result correctly -- by mocking `_ae_sweep_hook.decision`
    rather than depending on the shared `~/harmonic-forge` checkout's
    currently-merged state (the same idiom
    `test_is_lane3_session_delegates_to_the_shared_probe` already uses for
    the cloud-CLI policy delegation). The canonical hook's own exhaustive
    suite (`harmonic-forge/tools/hooks/test_deny_lane3_ae_self_post.py`)
    owns the actual heading/footer/transport/normalization logic."""

    def _mock_decision(self, denied: bool, reason: str = ""):
        result = (
            {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": reason}}
            if denied
            else {}
        )
        return patch.object(MODULE._ae_sweep_hook, "decision", return_value=result)

    def test_denial_translated_from_canonical_hook(self) -> None:
        with self._mock_decision(True, "harmonic-forge#407: sweep self-post denied") as mock_decision:
            reason = MODULE.ae_sweep_self_post_denial("mise run lane-comment --issue 1359 --file x.md", Path.cwd())
        mock_decision.assert_called_once_with("mise run lane-comment --issue 1359 --file x.md", Path.cwd())
        self.assertEqual(reason, "harmonic-forge#407: sweep self-post denied")

    def test_no_opinion_translated_from_canonical_hook(self) -> None:
        with self._mock_decision(False):
            reason = MODULE.ae_sweep_self_post_denial("ls -la", Path.cwd())
        self.assertIsNone(reason)

    def test_reaches_the_cascade_end_to_end_for_lane3(self) -> None:
        with self._mock_decision(True, "harmonic-forge#407: sweep self-post denied"):
            with patch.dict(os.environ, {"LANE": "3"}):
                rc, out = _run_main({
                    "tool_name": "Bash", "cwd": str(Path.cwd()),
                    "tool_input": {"command": "gh issue comment 1359 --body irrelevant"},
                })
        self.assertEqual(rc, 0)
        decision = json.loads(out)["hookSpecificOutput"]
        self.assertEqual(decision["permissionDecision"], "deny")
        self.assertEqual(decision["permissionDecisionReason"], "harmonic-forge#407: sweep self-post denied")

    def test_ae_denied_for_lane3_against_the_real_merged_hook(self) -> None:
        """One integration-style check against the actual `decision()`
        currently on disk at `~/harmonic-forge` -- AE detection already
        exists there (harmonic-forge#216), unlike the sweep matcher this
        issue adds, so this doesn't depend on F407's own forge-side change
        having merged yet."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            f.write("## AE — approved, execute\n\nOperator approved.\n")
            body_path = f.name
        try:
            with patch.dict(os.environ, {"LANE": "3"}):
                reason = MODULE.ae_sweep_self_post_denial(
                    f"mise run lane-comment --issue 706 --file {body_path}", Path.cwd()
                )
        finally:
            Path(body_path).unlink()
        self.assertIsNotNone(reason)
        self.assertIn("harmonic-forge#216", reason)

    def test_no_opinion_when_not_lane3_against_the_real_merged_hook(self) -> None:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            f.write("## AE — approved, execute\n\nOperator approved.\n")
            body_path = f.name
        try:
            with patch.dict(os.environ, {"LANE": "2"}):
                reason = MODULE.ae_sweep_self_post_denial(
                    f"mise run lane-comment --issue 706 --file {body_path}", Path.cwd()
                )
        finally:
            Path(body_path).unlink()
        self.assertIsNone(reason)


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "remote", "add", "origin", "https://github.com/OWNER/REPO.git"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)
    (path / "f.txt").write_text("x")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=path, check=True)


def _add_worktree(main: Path, target: Path) -> None:
    subprocess.run(["git", "worktree", "add", "-q", "--detach", str(target)], cwd=main, check=True)


class CanonicalWorktreeGuardTests(unittest.TestCase):
    """Round 2 of the canonical-worktree guard. Round 1 (self-reviewed) shipped a resolver that
    derived "canonical" from the cwd being checked -- exactly backwards,
    since that cwd is the thing under suspicion. A 5-lens adversarial
    pre-close panel (unanimous) found it fails in every case that matters:
    a stray worktree gets told to recover into its own nonexistent
    `-lane3` sibling, any directory literally named `...-lane3` certifies
    itself, subdirectories of the real canonical worktree are falsely
    denied, `lane3-begin` was never covered at all, and the new git
    denials were bypassed by any compound command or `git -C` prefix.
    These tests exercise the redesigned, non-spoofable resolver (the main
    worktree's `-lane3` sibling from the `git worktree list` registry,
    harmonic-forge#720, real subprocess calls against
    real fixture repos -- not mocked away) plus the fixed containment
    check, task coverage, and segment/wrapper-aware git-mutation guard."""

    def test_resolver_finds_registered_canonical_from_the_stray_worktree_itself(self) -> None:
        # The exact bug: round 1 derived "canonical" from the stray
        # worktree's own toplevel and got a nonexistent sibling. The fix
        # must return the REAL registered canonical no matter which
        # worktree (main, canonical, or a third ad hoc one) is asking.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            main = root / "project"
            _init_repo(main)
            canonical = root / "project-lane3"
            _add_worktree(main, canonical)
            stray = root / "project-1013-gate"
            _add_worktree(main, stray)
            resolved = MODULE._canonical_lane3_worktree(stray)
            self.assertEqual(resolved, canonical)

    def test_resolver_none_when_canonical_path_is_not_actually_registered(self) -> None:
        # A directory merely named "...-lane3" must not self-certify --
        # it has to be a real entry in `git worktree list`.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            main = root / "project"
            _init_repo(main)
            never_registered = root / "project-lane3"  # deliberately not `worktree add`ed
            self.assertIsNone(MODULE._canonical_lane3_worktree(main))

    def test_resolver_none_for_a_repo_with_no_registered_lane3_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            other = root / "other-repo"
            other.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=other, check=True)
            subprocess.run(["git", "remote", "add", "origin", "https://github.com/vitalharmony/harmonic-forge.git"], cwd=other, check=True)
            self.assertIsNone(MODULE._canonical_lane3_worktree(other))

    def test_resolver_none_outside_any_git_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(MODULE._canonical_lane3_worktree(Path(tmp)))

    def test_location_check_fails_closed_when_canonical_cannot_be_verified(self) -> None:
        # Round 1 fail-open regression: "can't tell" must mean deny for a
        # check whose whole job is confirming a safe location.
        with patch.object(MODULE, "_canonical_lane3_worktree", return_value=None):
            reason = MODULE.gate_worktree_location_denial("mise run gate-checkout foo", Path("/tmp/anywhere"), True)
        self.assertIsNotNone(reason)

    def test_location_check_allows_canonical_root_and_its_subdirectories(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            main = root / "project"
            _init_repo(main)
            canonical = root / "project-lane3"
            _add_worktree(main, canonical)
            (canonical / "backend").mkdir()
            self.assertIsNone(MODULE.gate_worktree_location_denial("mise run gate-checkout foo", canonical, True))
            self.assertIsNone(MODULE.gate_worktree_location_denial("mise run gate-restart", canonical / "backend", True))

    def test_location_check_denies_a_stray_registered_worktree_with_full_recovery_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            main = root / "project"
            _init_repo(main)
            canonical = root / "project-lane3"
            _add_worktree(main, canonical)
            stray = root / "project-1013-gate"
            _add_worktree(main, stray)
            reason = MODULE.gate_worktree_location_denial("mise run gate-checkout foo", stray, True)
        self.assertIsNotNone(reason)
        self.assertIn(f"cd {canonical.resolve()}", reason)
        self.assertIn("gate-checkout", reason)
        self.assertIn("lane3-begin", reason)

    def test_location_check_covers_lane3_begin_not_just_the_gate_dash_tasks(self) -> None:
        # Round 1's LANE3_ONLY_TASKS-only coverage excluded exactly the
        # command that failed in the real incident.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            main = root / "project"
            _init_repo(main)
            canonical = root / "project-lane3"
            _add_worktree(main, canonical)
            stray = root / "project-1010-gate"
            _add_worktree(main, stray)
            reason = MODULE.gate_worktree_location_denial("mise run lane3-begin --issue 1010", stray, True)
        self.assertIsNotNone(reason)

    def test_location_check_is_a_no_op_when_not_lane3(self) -> None:
        self.assertIsNone(
            MODULE.gate_worktree_location_denial("mise run gate-checkout foo", Path("/tmp/wrong"), False)
        )

    def test_git_subcommand_skips_dash_C_and_finds_the_real_subcommand(self) -> None:
        self.assertEqual(MODULE._git_subcommand(["-C", "/x", "worktree", "add", "/y"]), ("worktree", ["add", "/y"]))
        self.assertEqual(MODULE._git_subcommand(["--no-pager", "checkout", "main"]), ("checkout", ["main"]))
        self.assertEqual(MODULE._git_subcommand(["checkout", "main"]), ("checkout", ["main"]))
        self.assertIsNone(MODULE._git_subcommand([]))

    def test_git_mutation_denial_catches_worktree_add_via_compound_command(self) -> None:
        reason = MODULE.git_mutation_denial("cd /tmp && git worktree add /tmp/x-gate", Path("/tmp"), True)
        self.assertIsNotNone(reason)
        self.assertIn("git worktree add", reason)

    def test_git_mutation_denial_catches_worktree_add_via_dash_C_prefix(self) -> None:
        reason = MODULE.git_mutation_denial("git -C /home/x/project worktree add /tmp/x-gate", Path("/tmp"), True)
        self.assertIsNotNone(reason)

    def test_git_mutation_denial_catches_checkout_via_wrapper_prefix(self) -> None:
        reason = MODULE.git_mutation_denial("env FOO=bar git checkout main", Path("/tmp"), True)
        self.assertIsNotNone(reason)

    def test_git_mutation_denial_catches_bare_switch(self) -> None:
        self.assertIsNotNone(MODULE.git_mutation_denial("git switch main", Path("/tmp"), True))

    def test_git_mutation_denial_allows_worktree_add_for_lane2(self) -> None:
        # Lane 2 must keep its own per-issue implementation worktree path,
        # including the `git -C <mainroot> worktree add` form.
        self.assertIsNone(MODULE.git_mutation_denial("git -C /home/x/project worktree add /tmp/x-impl", Path("/tmp"), False))
        self.assertIsNone(MODULE.git_mutation_denial("git worktree add /tmp/x-impl", Path("/tmp"), False))

    def test_git_mutation_denial_ignores_unrelated_commands(self) -> None:
        self.assertIsNone(MODULE.git_mutation_denial("git status", Path("/tmp"), True))
        self.assertIsNone(MODULE.git_mutation_denial("ls -la", Path("/tmp"), True))

    def test_end_to_end_incident_shaped_command_denied(self) -> None:
        # The literal shape of the real incident: LANE=3, standing in an ad
        # hoc gate worktree, running the command that actually failed.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            main = root / "project"
            _init_repo(main)
            canonical = root / "project-lane3"
            _add_worktree(main, canonical)
            stray = root / "project-1010-gate"
            _add_worktree(main, stray)
            payload = {
                "tool_name": "Bash",
                "tool_input": {"command": "mise run lane3-begin --issue 1010"},
                "cwd": str(stray),
            }
            with patch.dict(os.environ, {"LANE": "3"}):
                rc, out = _run_main(payload)
            self.assertEqual(rc, 0)
            output = json.loads(out)
            self.assertEqual(output["hookSpecificOutput"]["permissionDecision"], "deny")
            self.assertIn(str(canonical.resolve()), output["hookSpecificOutput"]["permissionDecisionReason"])

    def test_end_to_end_worktree_add_denied_for_lane3(self) -> None:
        payload = {
            "tool_name": "Bash",
            "tool_input": {"command": "git worktree add /tmp/h1236-gate"},
            "cwd": str(Path.cwd()),
        }
        with patch.dict(os.environ, {"LANE": "3"}):
            rc, out = _run_main(payload)
        self.assertEqual(rc, 0)
        output = json.loads(out)
        self.assertEqual(output["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_end_to_end_gate_commands_work_from_the_real_canonical_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            main = root / "project"
            _init_repo(main)
            canonical = root / "project-lane3"
            _add_worktree(main, canonical)
            for command in ("mise run gate-checkout foo", "mise run lane3-begin --issue 1"):
                payload = {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(canonical)}
                with patch.dict(os.environ, {"LANE": "3"}):
                    rc, out = _run_main(payload)
                self.assertEqual(rc, 0)
                self.assertEqual(out, "")


if __name__ == "__main__":
    unittest.main()
