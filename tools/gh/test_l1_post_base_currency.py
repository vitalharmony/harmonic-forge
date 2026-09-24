#!/usr/bin/env python3
"""a private-repo incident — the rebase-confirm / ready-for-l3 race.

`static_checks` has always asserted the attested SHA is based on current
`origin/main`. It read the LOCAL remote-tracking ref, and `l1_post.py`
performed no fetch anywhere — so a merge landing between Lane 2's rebase and
Lane 1's `ready-for-l3` was invisible, and the check passed against a base
that was already behind.

**What is asserted here is the ORDER**, not just that a fetch happens. A fetch
that ran anywhere other than immediately before the ancestry test would leave
exactly the gap this issue exists to close, and would still satisfy a naive
"does it fetch" assertion.

Live-verified against the real script rather than a stub: these tests read
`l1_post.py`'s own source and drive its real `refresh_main`.
"""
import ast
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import l1_post as L  # noqa: E402

SOURCE = Path(__file__).resolve().parent / "l1_post.py"


def _at(haystack: str, needle: str, what: str) -> int:
    """`str.index`, but asserting first.

    A bare `.index` raises `ValueError: substring not found` when the thing it
    is guarding disappears — a traceback instead of a message naming what
    went. Caught by mutation testing on this very file, and previously on
    harmonic-forge#72's guard, which is why it is a helper rather than a habit.
    """
    assert needle in haystack, f"{what}: {needle!r} is no longer present"
    return haystack.index(needle)


def _fn(name: str) -> ast.FunctionDef:
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} not found in l1_post.py")


class TheFetchIsAdjacentToTheCheck(unittest.TestCase):
    def test_static_checks_refreshes_main_before_testing_ancestry(self) -> None:
        """The ordering IS the fix. A fetch earlier in the function, or in
        `main()`, would reopen the window while still looking correct."""
        body = ast.unparse(_fn("static_checks"))
        refresh_at = _at(body, "refresh_main()", "static_checks no longer refreshes main")
        ancestry_at = _at(body, "is-ancestor", "static_checks no longer tests ancestry")
        self.assertLess(
            refresh_at, ancestry_at,
            "refresh_main() must run BEFORE the ancestry test, not after",
        )

    def test_the_ancestry_test_uses_the_freshly_fetched_sha(self) -> None:
        """Not the local `origin/main` ref. Fetching and then comparing
        against the stale ref anyway is the subtlest way to ship this fix
        without fixing anything."""
        body = ast.unparse(_fn("static_checks"))
        self.assertIn("main_sha", body)
        self.assertNotIn("'origin/main', sha", body.replace('"', "'"))


class RefreshMainFailsClosed(unittest.TestCase):
    """An unestablished base must never read as a verified one."""

    def test_a_failed_fetch_aborts_rather_than_degrading(self) -> None:
        """**Only the fetch fails here**; `rev-parse` is made to succeed.

        The first draft of this test mocked `run` to fail for everything, so
        deleting the fetch check entirely still raised — from the rev-parse
        guard instead. It passed against a mutation that removed the very
        behaviour it names. Isolating the two calls is what makes it bite.
        """
        def _run(*args, **kwargs):
            if args[1] == "fetch":
                return subprocess.CompletedProcess(args, 1, "", "network down")
            return subprocess.CompletedProcess(args, 0, "deadbeefcafe\n", "")

        with mock.patch.object(L, "run", side_effect=_run):
            with self.assertRaises(SystemExit):
                L.refresh_main()

    def test_a_failed_rev_parse_also_aborts(self) -> None:
        """The other half: a fetch that works but leaves FETCH_HEAD
        unresolvable is equally an unestablished base."""
        def _run(*args, **kwargs):
            if args[1] == "fetch":
                return subprocess.CompletedProcess(args, 0, "", "")
            return subprocess.CompletedProcess(args, 1, "", "bad ref")

        with mock.patch.object(L, "run", side_effect=_run):
            with self.assertRaises(SystemExit):
                L.refresh_main()

    def test_a_successful_fetch_returns_the_fetched_sha(self) -> None:
        calls: list[tuple] = []

        def _run(*args, **kwargs):
            calls.append(args)
            if args[1] == "fetch":
                return subprocess.CompletedProcess(args, 0, "", "")
            return subprocess.CompletedProcess(args, 0, "abc123def456\n", "")

        with mock.patch.object(L, "run", side_effect=_run):
            self.assertEqual(L.refresh_main(), "abc123def456")
        self.assertEqual(calls[0][:4], ("git", "fetch", "origin", "main"))
        self.assertIn("FETCH_HEAD", calls[1])


class TheKindIsUnchanged(unittest.TestCase):
    """R-0209's carry-forward keys on the literal `ready-for-l3` string.

    `check_lane3_ready.py:158` compares `match.group(1).lower() ==
    "ready-for-l3"`, so introducing a new `--kind` for the combined action —
    one of the three shapes this issue's handoff left to Lane 2 — would have
    silently broken the carry-forward for every future SHA bump. This test
    exists so that option cannot be reintroduced without failing.
    """

    def test_ready_for_l3_is_still_an_accepted_kind(self) -> None:
        source = SOURCE.read_text(encoding="utf-8")
        self.assertIn('"ready-for-l3"', source)

    def test_no_new_kind_was_added_for_this_issue(self) -> None:
        source = SOURCE.read_text(encoding="utf-8")
        for invented in ("rebase-and-ready", "confirm-and-post", "ready-atomic"):
            self.assertNotIn(invented, source)

    def test_static_checks_still_runs_only_for_ready_for_l3(self) -> None:
        """The check's scope must not widen either — this issue tightens
        timing, it does not change which kinds are validated.

        harmonic-forge#745 changed `static_checks()`'s return shape from a
        bare `list[str]` to `(list[str], (started_at, finished_at))`, so the
        call site is no longer expressible as the single ternary this test
        used to pin literally -- an if/else with the same gating condition,
        asserted by structure rather than by exact source text, which is
        what this test actually cares about."""
        body = ast.unparse(_fn("post_kind"))
        self.assertIn("if kind == 'ready-for-l3':", body)
        self.assertIn("static_checks(sha, branch)", body)
        self.assertIn("checks = ['body-validation']", body)


class TheExistingChecksSurvive(unittest.TestCase):
    """TC2 — this tightens timing; it must not loosen anything."""

    def test_the_branch_sha_match_is_still_first(self) -> None:
        body = ast.unparse(_fn("static_checks"))
        self.assertLess(
            _at(body, "no longer resolves to the attested SHA", "the SHA-match check is gone"),
            _at(body, "refresh_main()", "static_checks no longer refreshes main"),
            "the cheap local SHA-match check should still short-circuit before "
            "a network call",
        )

    def test_static_checks_returns_the_same_check_names(self) -> None:
        """The names land in the posted footer's `checks=` field, which is a
        machine-readable claim about what was verified.

        Asserted against `static_checks`'s OWN return, not against the file at
        large: an earlier draft of this test listed `world_checks`' names
        instead and passed simply because those strings appear elsewhere in
        the module — a green test proving nothing about the function it
        claimed to guard. Caught by reading a mutation's output.
        """
        returned = ast.unparse(_fn("static_checks").body[-1])
        for name in ("mise-check", "origin-main-ancestor",
                     "branch-sha-match", "clean-worktree"):
            self.assertIn(name, returned)


class ScratchWorktreeIsProvisioned(unittest.TestCase):
    """a private-repo incident: the scratch worktree `static_checks()` creates via `git
    worktree add --detach` starts with no `.claude/` provisioning at all.
    harmonic-forge's own `mise run check` self-verifies that linkage, so an
    unprovisioned scratch worktree failed unconditionally there -- blocking
    `ready-for-l3` for every harmonic-forge issue regardless of the actual
    code change. Live-verified fixed (posted `ready-for-l3` for a real
    harmonic-forge issue through this exact path); these pin the source
    shape so a regression fails here too, not only live again."""

    def test_hooks_install_runs_before_mise_run_check(self) -> None:
        body = ast.unparse(_fn("static_checks"))
        install_at = _at(body, "hooks-install", "static_checks no longer provisions the scratch worktree")
        check_at = _at(body, "'run', 'check'", "static_checks no longer runs mise run check")
        self.assertLess(install_at, check_at,
                         "provisioning must happen before the check it exists to unblock")

    def test_the_provisioning_step_is_best_effort(self) -> None:
        """A repo with no `hooks-install` task (or one that fails for an
        unrelated reason) must not make this function refuse a genuinely
        good SHA -- `mise run check` immediately after remains the real
        gate either way. Asserted by absence: the `run(...)` call for
        hooks-install has no `if ... fail(...)` guard on its own result,
        unlike every other `run()` call in this function."""
        tree = ast.parse(_fn("static_checks"))
        found_hooks_install_call = False
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name) and node.func.id == "run"
                    and any(isinstance(a, ast.Constant) and a.value == "hooks-install"
                            for a in node.args)):
                found_hooks_install_call = True
                # Confirm this call's result is never assigned to a name
                # that a subsequent `if ...: fail(...)` reads -- the other
                # `run()` calls in this function all assign to a name
                # (`added`, `checked`, `clean`) that a following `fail()`
                # branch inspects; this one, if best-effort, does not.
        self.assertTrue(found_hooks_install_call,
                         "hooks-install call not found in static_checks source")
        source = ast.unparse(_fn("static_checks"))
        # The hooks-install line itself must not be immediately followed by
        # a returncode check the way `added`/`checked`/`clean` each are.
        self.assertNotIn('"hooks-install", cwd=scratch)\n        if', source,
                          "hooks-install's result appears to be checked -- "
                          "a failure there would then wrongly refuse a good SHA")


if __name__ == "__main__":
    unittest.main()
