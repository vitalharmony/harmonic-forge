#!/usr/bin/env python3
"""Unit tests for block_lane1_status_claims.py's cwd threading (harmonic-forge#210).

Narrowly scoped to the cwd-threading fix itself, not this file's full
surface (autoclose-keyword denial, Lane 2/3 worktree checks, etc.) —
that's separately-scoped follow-up work. Run: python3
tools/hooks/test_block_lane1_status_claims.py"""

import sys
import unittest.mock
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import block_lane1_status_claims as m


def _is_denied(result: dict) -> bool:
    return result.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"


class TestCwdThreading(unittest.TestCase):
    def test_decision_resolves_relative_body_file_against_passed_cwd(self):
        """The actual bug, with a real differentiating case:
        pr_body_autoclose_text() resolves a *relative* --body-file path
        against `cwd` (`path = cwd / path` when not absolute). A relative
        path resolves to a real file (containing an autoclose keyword)
        when cwd is the directory that actually holds it, and to nothing
        when cwd is elsewhere — this produces a genuinely different
        decision() outcome depending on which cwd value is used, proving
        the payload's cwd (not Path.cwd()) is what's actually consulted."""
        with tempfile.TemporaryDirectory() as tmpdir:
            body_dir = Path(tmpdir)
            (body_dir / "pr-body.txt").write_text("Closes #123\n")

            # Relative path, cwd = the directory that actually holds it -> denied.
            result_correct_cwd = m.decision(
                "gh pr create --title x --body-file pr-body.txt", body_dir,
            )
            self.assertTrue(_is_denied(result_correct_cwd))

            # Same relative path, cwd = somewhere else entirely -> file not
            # found there, OSError caught, no match -> allowed.
            with tempfile.TemporaryDirectory() as elsewhere:
                result_wrong_cwd = m.decision(
                    "gh pr create --title x --body-file pr-body.txt", Path(elsewhere),
                )
                self.assertFalse(_is_denied(result_wrong_cwd))

    def test_gate_checkout_denial_unaffected_by_cwd(self):
        """gate-checkout's LANE-3-only denial is LANE-based, not
        cwd-based — confirms passing a scratch cwd doesn't accidentally
        suppress or alter this unrelated check."""
        import os
        os.environ.pop("LANE", None)
        with tempfile.TemporaryDirectory() as scratch:
            result = m.decision("mise run gate-checkout main", Path(scratch))
        self.assertTrue(_is_denied(result))


class TestExistingRegressions(unittest.TestCase):
    """#167-era regressions, re-confirmed with cwd explicitly passed."""

    def test_ordinary_command_passes_through(self):
        result = m.decision("ls -la", Path.cwd())
        self.assertEqual(result, {})

    def test_gate_checkout_denied_without_lane3(self):
        import os
        os.environ.pop("LANE", None)
        result = m.decision("mise run gate-checkout main", Path.cwd())
        self.assertTrue(_is_denied(result))

    def test_autoclose_keyword_in_body_file_denied(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("Closes #123\n")
            body_path = f.name
        try:
            result = m.decision(f"gh pr create --title x --body-file {body_path}", Path.cwd())
            self.assertTrue(_is_denied(result))
        finally:
            Path(body_path).unlink()


class TestBulkCommentReadDenial(unittest.TestCase):
    """harmonic-forge#260: a fifth Lane 3 contamination incident on
    hrse#793 came through a Claude Code Lane 3 session -- #258 added the
    equivalent check to the Codex-side gate_codex_tool.py hook only, and
    this canonical file (what a Claude Lane 3 session's .claude/settings.json
    actually wires) had no check at all, so `gh issue view --comments`
    sailed through unblocked. These are the literal commands from the
    real incidents."""

    def test_gh_issue_view_comments_denied_for_lane3(self):
        import os
        with unittest.mock.patch.dict(os.environ, {"LANE": "3"}):
            result = m.decision("gh issue view 793 --repo vitalharmony/hrse --comments", Path.cwd())
        self.assertTrue(_is_denied(result))

    def test_gh_api_bulk_comments_paginate_denied_for_lane3(self):
        import os
        with unittest.mock.patch.dict(os.environ, {"LANE": "3"}):
            result = m.decision("gh api repos/vitalharmony/hrse/issues/793/comments --paginate", Path.cwd())
        self.assertTrue(_is_denied(result))

    def test_fetch_lane1_context_script_allowed_for_lane3(self):
        import os
        with unittest.mock.patch.dict(os.environ, {"LANE": "3"}):
            result = m.decision(
                "python3 ~/harmonic-forge/tools/gh/fetch_lane1_context.py --repo vitalharmony/hrse --issue 793",
                Path.cwd(),
            )
        self.assertEqual(result, {})

    def test_single_comment_by_id_allowed_for_lane3(self):
        import os
        with unittest.mock.patch.dict(os.environ, {"LANE": "3"}):
            result = m.decision("gh api repos/vitalharmony/hrse/issues/comments/5273884235", Path.cwd())
        self.assertEqual(result, {})

    def test_issue_body_only_fetch_allowed_for_lane3(self):
        import os
        with unittest.mock.patch.dict(os.environ, {"LANE": "3"}):
            result = m.decision("gh api repos/vitalharmony/hrse/issues/793 --jq .body", Path.cwd())
        self.assertEqual(result, {})

    def test_gh_issue_view_comments_allowed_for_lane2(self):
        import os
        with unittest.mock.patch.dict(os.environ, {"LANE": "2"}):
            result = m.decision("gh issue view 793 --repo vitalharmony/hrse --comments", Path.cwd())
        self.assertEqual(result, {})

    def test_gh_issue_view_comments_allowed_for_lane1(self):
        import os
        with unittest.mock.patch.dict(os.environ, {"LANE": "1"}):
            result = m.decision("gh issue view 793 --repo vitalharmony/hrse --comments", Path.cwd())
        self.assertEqual(result, {})


class TestMalformedPayload(unittest.TestCase):
    def test_non_string_command_fails_closed(self):
        """This file's existing default for posting controls is fail-
        CLOSED (the opposite of mypy_cwd_trap.py's fail-open default) —
        confirmed unchanged by the cwd-threading fix."""
        result = m.decision(None, Path.cwd())
        self.assertTrue(_is_denied(result))


class _BashWriteSurface(unittest.TestCase):
    """Shared fixture: a real `-lane2` git worktree beside a real main
    checkout, so `resolve_main_checkout_root` does its actual derivation
    rather than being mocked out (harmonic-forge#458)."""

    LANE = "2"

    def setUp(self) -> None:
        import os
        import subprocess
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name).resolve()
        self.main = base / "proj"
        self.lane2 = base / "proj-lane2"
        self.main.mkdir(parents=True)
        self.lane2.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(self.lane2)], check=True)
        # The main checkout is a real git repo too: several assertions below
        # run with cwd INSIDE it, and `resolve_main_checkout_root` fails open
        # when cwd is not in a repo at all -- which would make them pass
        # vacuously.
        subprocess.run(["git", "init", "-q", str(self.main)], check=True)
        self.protected = self.main / ".claude" / "settings.json"
        self.protected.parent.mkdir(parents=True)
        self.protected.write_text("{}\n")
        patcher = unittest.mock.patch.dict(os.environ, {"LANE": self.LANE})
        patcher.start()
        self.addCleanup(patcher.stop)

    def denied(self, command: str, cwd: Path | None = None) -> bool:
        return _is_denied(m.decision(command, cwd or self.lane2))


class TestBashWriteConstructs(_BashWriteSurface):
    """The six shapes reproduced live on harmonic-forge#458.

    `Edit` and `Write` into a protected path were already denied; every
    `Bash` shape below was allowed, because the `Bash` branch checked
    transports and never the path predicates that govern the other surface.
    """

    def test_plain_redirect_is_denied(self):
        """The finding that mattered most: the issue was filed as a
        scripted-write bypass, but the simplest shell construct did it."""
        self.assertTrue(self.denied(f"echo x > {self.protected}"))

    def test_glued_redirect_is_denied(self):
        """`echo x>path` arrives as ONE token from the shared tokenizer."""
        self.assertTrue(self.denied(f"echo x>{self.protected}"))

    def test_append_redirect_is_denied(self):
        self.assertTrue(self.denied(f"echo x >> {self.protected}"))

    def test_tee_is_denied(self):
        self.assertTrue(self.denied(f"echo x | tee {self.protected}"))

    def test_sed_in_place_is_denied(self):
        self.assertTrue(self.denied(f"sed -i s/a/b/ {self.protected}"))

    def test_copy_move_install_destinations_are_denied(self):
        for verb in ("cp", "mv", "install"):
            with self.subTest(verb=verb):
                self.assertTrue(self.denied(f"{verb} /tmp/src {self.protected}"))

    def test_truncate_and_dd_are_denied(self):
        self.assertTrue(self.denied(f"truncate -s 0 {self.protected}"))
        self.assertTrue(self.denied(f"dd if=/dev/zero of={self.protected}"))

    def test_write_after_cd_uses_the_tracked_cwd(self):
        """Relative target, resolved against the segment's own `cd`."""
        self.assertTrue(self.denied(
            f"cd {self.main} && echo x > .claude/settings.json"))


class TestCrossProjectMainBranchWrite(unittest.TestCase):
    """The reported incident's actual shape, and the one the plan for this
    issue got wrong (harmonic-forge#458).

    The write was into ANOTHER project's main checkout — `~/harmonic-forge`
    from an HRSE2 session. `lane2_write_in_main_checkout` resolves the
    protected root from the session's own cwd, so it returns False here; what
    denies it on the `Edit` surface is `write_on_main_branch`
    (harmonic-forge#384). Wiring only the two predicates the plan named passed
    every unit test and still allowed all three write shapes on the live
    re-run. This is that re-run, as a test.
    """

    def setUp(self) -> None:
        import subprocess
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name).resolve()
        self.other = base / "other-project"
        self.other.mkdir(parents=True)
        run = lambda *a: subprocess.run(["git", "-C", str(self.other), *a],  # noqa: E731
                                        check=True, capture_output=True)
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.other)], check=True)
        run("config", "user.email", "t@example.com")
        run("config", "user.name", "t")
        self.tracked = self.other / "settings.json"
        self.tracked.write_text("{}\n")
        run("add", "settings.json")
        run("commit", "-qm", "seed")
        self.untracked = self.other / "scratch.txt"
        self.elsewhere = base / "unrelated"
        self.elsewhere.mkdir()

        import os
        patcher = unittest.mock.patch.dict(os.environ, {"LANE": "2"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def denied(self, command: str) -> bool:
        return _is_denied(m.decision(command, self.elsewhere))

    def test_redirect_into_another_projects_main_branch_is_denied(self):
        self.assertTrue(self.denied(f"echo x > {self.tracked}"))

    def test_interpreter_write_into_another_projects_main_branch_is_denied(self):
        self.assertTrue(self.denied(
            f"""python3 -c "open('{self.tracked}','w').write('x')" """))

    def test_heredoc_write_into_another_projects_main_branch_is_denied(self):
        self.assertTrue(self.denied(
            f"python3 <<'PY'\nopen('{self.tracked}','w').write('x')\nPY\n"))

    def test_reading_it_is_still_allowed(self):
        self.assertFalse(self.denied(f"cat {self.tracked}"))

    def test_untracked_file_in_the_same_checkout_is_allowed(self):
        """#384's own boundary, unchanged: a new/scratch file is not the
        violation this guards — only editing a tracked file on `main` is."""
        self.assertFalse(self.denied(f"echo x > {self.untracked}"))


class TestInterpreterWriteRule(_BashWriteSurface):
    """A protected path AND a write verb, both required (operator-confirmed,
    endorsed at plan review). Either signal alone is not enough."""

    def test_dash_c_write_is_denied(self):
        self.assertTrue(self.denied(
            f"""python3 -c "open('{self.protected}','w').write('x')" """))

    def test_heredoc_write_is_denied(self):
        """`command_segments` masks heredoc bodies, so this rule reads the
        RAW command — a heredoc script was one of the six live shapes."""
        self.assertTrue(self.denied(
            f"python3 <<'PY'\nopen('{self.protected}','w').write('x')\nPY\n"))

    def test_pathlib_and_shutil_verbs_are_denied(self):
        for body in (f"Path('{self.protected}').write_text('x')",
                     f"shutil.copy('/tmp/a', '{self.protected}')",
                     f"os.replace('/tmp/a', '{self.protected}')"):
            with self.subTest(body=body):
                self.assertTrue(self.denied(f'python3 -c "{body}"'))

    def test_inner_shell_redirect_is_denied(self):
        self.assertTrue(self.denied(
            f"""bash -c "echo x > {self.protected}" """))

    def test_reading_a_protected_path_is_allowed(self):
        """The required negative. A path-mention-only rule would have denied
        this issue's OWN hook survey, which was a python3 heredoc reading
        every protected settings file — that is why both signals are
        required, and without this test the over-broad version would pass."""
        self.assertFalse(self.denied(
            f"python3 -c \"print(open('{self.protected}').read())\""))
        self.assertFalse(self.denied(
            f"python3 <<'PY'\nprint(open('{self.protected}').read())\nPY\n"))

    def test_numeric_comparison_in_a_read_script_is_not_a_write(self):
        """`>` is a write verb only when a path-shaped operand follows it."""
        self.assertFalse(self.denied(
            f"python3 -c \"d=open('{self.protected}').read()\nif len(d) > 3: pass\""))

    def test_known_limit_an_external_script_file_is_not_seen(self):
        """Stated, not hidden: `python3 /tmp/w.py` carries neither the
        protected path nor a write verb in its command text, so nothing
        static can see it. Same class as `getattr(open, ...)` or a base64'd
        payload — `block_irreversible_ops.py` concedes shell-wrapper
        indirection for the identical reason rather than pretending to
        coverage it does not have."""
        self.assertFalse(self.denied("python3 /tmp/write_it.py"))


class TestBashReadsAndUnrelatedCommandsStillPass(_BashWriteSurface):
    """The false-positive half. The deny requires a write construct TARGETING
    a protected path, never a mention of one."""

    def test_reads_are_allowed(self):
        for command in (f"cat {self.protected}",
                        f"grep -n x {self.protected}",
                        f"git show HEAD:{self.protected}",
                        f"ls -la {self.protected}"):
            with self.subTest(command=command):
                self.assertFalse(self.denied(command))

    def test_unrelated_command_is_allowed(self):
        self.assertFalse(self.denied("ls -la"))

    def test_writing_inside_the_lane2_worktree_is_allowed(self):
        self.assertFalse(self.denied(f"echo x > {self.lane2}/scratch.txt"))

    def test_dev_null_and_stderr_redirects_are_allowed(self):
        for command in ("ls -la > /dev/null", "ls -la 2>/dev/null",
                        "ls -la > /dev/null 2>&1"):
            with self.subTest(command=command):
                self.assertFalse(self.denied(command))

    def test_a_quoted_angle_bracket_is_not_a_redirect(self):
        self.assertFalse(self.denied('git commit -m "a > b"'))

    def test_sed_script_operand_is_not_treated_as_a_path(self):
        """Run from INSIDE the protected checkout, `s/a/b/` resolves under it
        — so mistaking sed's script for a filename denies a legitimate edit
        of an unprotected file."""
        self.assertFalse(self.denied(
            "sed -i s/a/b/ /tmp/unprotected.txt", cwd=self.main))

    def test_truncate_size_operand_is_not_treated_as_a_path(self):
        self.assertFalse(self.denied(
            "truncate -s 0 /tmp/unprotected.txt", cwd=self.main))


class TestLane3ThroughTheBashSurface(_BashWriteSurface):
    """The same second-surface wiring for Lane 3's deny-by-default predicate."""

    LANE = "3"

    def test_write_outside_testplan_is_denied(self):
        self.assertTrue(self.denied("echo x > /tmp/lane3-scratch.txt"))

    def test_write_inside_testplan_is_allowed(self):
        self.assertFalse(self.denied(f"echo x > {m.TESTPLAN_ROOT}/gate.md"))

    def test_reads_and_dev_null_are_unaffected(self):
        self.assertFalse(self.denied("cat /etc/hostname"))
        self.assertFalse(self.denied("ls -la > /dev/null"))


class TestPayloadSurface(unittest.TestCase):
    """End-to-end through `main()`, which is what the hook actually runs.

    harmonic-forge#455's Lane 3 finding, applied here before the gate has to
    find it again: a permit-case assertion that only checks "no denial came
    back" cannot distinguish an allow from a hook that crashed and printed
    `{}`. `test_the_permit_assertions_are_not_vacuous` is the control — it
    fails if this harness is pointed at anything that always returns `{}`.
    """

    def _run(self, payload: dict, lane: str = "2") -> dict:
        import json
        import os
        import subprocess
        env = dict(os.environ, LANE=lane)
        proc = subprocess.run(
            [sys.executable, str(Path(__file__).parent / "block_lane1_status_claims.py")],
            input=json.dumps(payload), capture_output=True, text=True,
            timeout=30, env=env,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout or "{}")

    def setUp(self) -> None:
        import subprocess
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name).resolve()
        self.main = base / "proj"
        self.lane2 = base / "proj-lane2"
        self.main.mkdir(parents=True)
        self.lane2.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(self.lane2)], check=True)
        # The main checkout is a real git repo too: several assertions below
        # run with cwd INSIDE it, and `resolve_main_checkout_root` fails open
        # when cwd is not in a repo at all -- which would make them pass
        # vacuously.
        subprocess.run(["git", "init", "-q", str(self.main)], check=True)
        self.protected = self.main / ".claude" / "settings.json"
        self.protected.parent.mkdir(parents=True)
        self.protected.write_text("{}\n")

    def _bash(self, command: str) -> dict:
        return self._run({"tool_name": "Bash", "cwd": str(self.lane2),
                          "tool_input": {"command": command}})

    def test_bash_redirect_is_denied_end_to_end(self):
        self.assertTrue(_is_denied(self._bash(f"echo x > {self.protected}")))

    def test_edit_and_write_regression_pair(self):
        """TC3: the original surface must be unchanged."""
        for tool in ("Edit", "Write"):
            with self.subTest(tool=tool):
                result = self._run({"tool_name": tool, "cwd": str(self.lane2),
                                    "tool_input": {"file_path": str(self.protected)}})
                self.assertTrue(_is_denied(result))

    def test_the_permit_assertions_are_not_vacuous(self):
        """If `block_lane1_status_claims.py` were replaced by a stub printing
        `{}` unconditionally, every permit-case test above would still pass
        and this one would fail. That is the whole point of it."""
        allowed = self._bash("ls -la")
        denied = self._bash(f"echo x > {self.protected}")
        self.assertEqual(allowed, {})
        self.assertNotEqual(denied, {},
                            "the hook is not evaluating commands at all — the "
                            "permit-case results above are meaningless")


class TestBashMatcherIsWired(unittest.TestCase):
    """harmonic-forge#458's second half: this repo's own settings had this
    hook on `Edit|Write` only, so neither the pre-existing transport check nor
    the new write check fired here at all. Asserted against the tracked file in
    this checkout, resolved from the test's location — deterministic, and it
    travels with the diff instead of depending on what is deployed."""

    _OWN = Path(__file__).resolve().parents[2] / ".claude" / "settings.json"

    def test_bash_matcher_carries_this_hook(self):
        import json
        data = json.loads(self._OWN.read_text(encoding="utf-8"))
        entries = (data.get("hooks") or {}).get("PreToolUse") or []
        bash = [e for e in entries if e.get("matcher") == "Bash"]
        self.assertTrue(bash, f"no Bash PreToolUse matcher in {self._OWN}")
        self.assertIn("block_lane1_status_claims.py", json.dumps(bash),
                      f"block_lane1_status_claims.py not wired for Bash in {self._OWN}")


if __name__ == "__main__":
    unittest.main()
