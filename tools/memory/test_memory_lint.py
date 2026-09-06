"""Tests for the memory-store lint and its SessionStart delivery (harmonic-forge#494).

The numeric assertions pin to a fixture, never to the live store. The live store
is grown by the harness, so a live-pinned count is one that changes without any
commit — a test that fails on somebody else's memory write proves nothing about
this code.

**AC6 IS NOT SATISFIED AS WRITTEN, AND THAT IS DELIBERATE.** It pins counts to
`testdata/store_d494c39/`, a frozen copy of the operator's real memory store.
`harmonic-forge` is a PUBLIC repository, and that store holds a file documenting
a live Keycloak test credential plus investor context, resume canon, customer
naming rules and tenant priorities. Committing it here would be its first
publication and is not reversible in practice.

`testdata/conventions/` is a SYNTHETIC fixture reproducing the same structural
conditions AC6 exists to pin — one orphan, both naming conventions resolving,
one issue reference — with no operator content. It gives AC6's shape (1 / 0 / 0)
without its provenance. Whether the real frozen copy may live somewhere private
instead is Lane 1's decision, not this suite's.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import memory_lint as lint  # noqa: E402
import session_start_summary as summary  # noqa: E402

HERE = Path(__file__).resolve().parent
TESTDATA = HERE / "testdata"
CONVENTIONS = TESTDATA / "conventions"
CLEAN = TESTDATA / "clean"
DIRTY = TESTDATA / "dirty"
FORGE_ROOT = HERE.parent.parent


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(HERE / "memory_lint.py"), *args],
        capture_output=True, text=True,
    )


class StoreResolution(unittest.TestCase):
    """TC1/TC2 — the defect this issue exists to fix."""

    def test_resolves_the_shared_store_from_settings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "config"
            cfg.mkdir()
            store = Path(tmp) / "store"
            store.mkdir()
            (cfg / "settings.json").write_text(
                json.dumps({"autoMemoryDirectory": str(store), "unrelated": "secret"}))
            os.environ["CLAUDE_CONFIG_DIR"] = str(cfg)
            try:
                self.assertEqual(lint.resolve_store(), store)
            finally:
                del os.environ["CLAUDE_CONFIG_DIR"]

    def test_the_resolved_store_agrees_from_every_repo(self) -> None:
        """TC1. `resolve_store` takes the repo root as an argument, so running
        it with three different roots is the same question the handoff asked by
        launching three sessions."""
        roots = [Path.home() / "Harmonic_Projects/HRSE2",
                 Path.home() / "harmonic-forge",
                 Path.home() / "Harmonic_Projects/cymagraph-infra"]
        resolved = {lint.resolve_store(r) for r in roots}
        self.assertEqual(len(resolved), 1, f"repos disagree on the store: {resolved}")

    def test_falls_back_to_the_repo_derived_path_without_the_key(self) -> None:
        """TC2 — no crash, and the fallback is the legacy shape."""
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "config"
            cfg.mkdir()
            (cfg / "settings.json").write_text(json.dumps({"other": 1}))
            os.environ["CLAUDE_CONFIG_DIR"] = str(cfg)
            try:
                got = lint.resolve_store(Path("/home/x/Some_Repo"))
            finally:
                del os.environ["CLAUDE_CONFIG_DIR"]
        self.assertIn(".claude/projects", str(got))
        self.assertTrue(str(got).endswith("/memory"))

    def test_missing_settings_file_does_not_crash(self) -> None:
        os.environ["CLAUDE_CONFIG_DIR"] = "/nonexistent-dir-for-this-test"
        try:
            self.assertIsInstance(lint.resolve_store(), Path)
        finally:
            del os.environ["CLAUDE_CONFIG_DIR"]

    def test_first_output_line_names_the_store(self) -> None:
        out = _run("--store", str(CLEAN)).stdout
        self.assertTrue(out.splitlines()[0].startswith("store: "), out[:200])


class FixtureCounts(unittest.TestCase):
    """AC6's SHAPE (1 / 0 / 0), against the synthetic fixture. See the module
    docstring for why the frozen real store is not here."""

    def test_check1_reports_exactly_the_one_known_orphan(self) -> None:
        issues = lint.check_orphans(CONVENTIONS)
        self.assertEqual(len(issues), 1, issues)
        self.assertIn("feedback_the_orphan.md", issues[0])

    def test_check4_reports_zero_and_not_the_119_pre_fix_findings(self) -> None:
        broken, _refs = lint.check_broken_slugs(CONVENTIONS)
        self.assertEqual(broken, [], broken)

    def test_check5_reports_zero(self) -> None:
        self.assertEqual(lint.check_missing_frontmatter(CONVENTIONS), [])

    def test_the_exclusion_set_is_what_suppresses_the_extra_findings(self) -> None:
        """TC4 — add a `README.md`, then drop it from the exclusion set.

        Without this the counts above could be right by accident (the fixture
        simply not containing those files) rather than because the exclusion set
        does its job. harmonic-forge#493's own `README.md` is why this matters:
        it is both an orphan and frontmatter-less, so an unexcluded lint is red
        the day it lands.
        """
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp) / "store"
            shutil.copytree(CONVENTIONS, store)
            (store / "README.md").write_text("# Memory store\n\nNot a memory.\n")
            self.assertEqual(len(lint.check_orphans(store)), 1)
            self.assertEqual(len(lint.check_missing_frontmatter(store)), 0)
            original = lint.NON_MEMORY_FILES
            lint.NON_MEMORY_FILES = frozenset({"MEMORY.md"})
            try:
                self.assertEqual(len(lint.check_orphans(store)), 2)
                self.assertEqual(len(lint.check_missing_frontmatter(store)), 1)
            finally:
                lint.NON_MEMORY_FILES = original

    def test_both_naming_conventions_resolve(self) -> None:
        """The 119-false-positive cause, reproduced and then defeated.

        The fixture links by filename stem, by kebab `name:`, AND by a SHORTENED
        kebab `name:` — the third being the shape that makes a collision
        reachable and therefore the one worth pinning.
        """
        broken, _refs = lint.check_broken_slugs(CONVENTIONS)
        self.assertEqual(broken, [], broken)
        known, _ = lint.build_slug_index(CONVENTIONS)
        self.assertIn("marc_always_triggers_lanes", known)
        self.assertIn("feedback_marc_always_triggers_lanes", known)


class IssueReferences(unittest.TestCase):
    """TC5 — `[[hrse#1303]]` is not a broken memory link."""

    def test_issue_refs_are_categorised_separately(self) -> None:
        broken, refs = lint.check_broken_slugs(CONVENTIONS)
        self.assertTrue(any("hrse#1303" in r for r in refs), refs)
        self.assertFalse(any("hrse#1303" in b for b in broken), broken)

    def test_issue_refs_do_not_affect_the_exit_code(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp) / "s"
            shutil.copytree(CLEAN, store)
            body = (store / "feedback_alpha.md").read_text()
            (store / "feedback_alpha.md").write_text(body + "\nSee [[hrse#1303]].\n")
            self.assertEqual(_run("--store", str(store), "--gate").returncode, 0)


class SlugCollisions(unittest.TestCase):
    """TC6 — a reachable collision must be loud, not silently weakening Check 4."""

    def test_two_files_normalizing_alike_is_a_gating_finding(self) -> None:
        broken, _ = lint.check_broken_slugs(DIRTY)
        hits = [b for b in broken if "normalize to the same key" in b]
        self.assertEqual(len(hits), 1, broken)
        self.assertIn("feedback-colliding-pair.md", hits[0])
        self.assertIn("feedback_colliding_pair.md", hits[0])

    def test_the_conventions_fixture_has_no_collisions(self) -> None:
        _known, collisions = lint.build_slug_index(CONVENTIONS)
        self.assertEqual(collisions, [])

    def test_slug_normalizes_both_conventions(self) -> None:
        self.assertEqual(lint._slug("Feedback-Foo-Bar"), "feedback_foo_bar")
        self.assertEqual(lint._slug("feedback_foo_bar"), "feedback_foo_bar")


class IndexCap(unittest.TestCase):
    """TC7."""

    def test_over_cap_fails_only_under_gate(self) -> None:
        self.assertEqual(_run("--store", str(DIRTY), "--gate").returncode, 1)
        self.assertEqual(_run("--store", str(DIRTY)).returncode, 0)

    def test_over_cap_is_reported_with_both_numbers(self) -> None:
        findings, _warn = lint.check_index_cap(DIRTY)
        self.assertTrue(any("exceeds the 200-line load cap" in f for f in findings), findings)

    def test_warns_at_eighty_percent_without_failing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp) / "s"
            shutil.copytree(CLEAN, store)
            (store / "MEMORY.md").write_text("# Memory Index\n" + "x\n" * 180)
            findings, warnings = lint.check_index_cap(store)
            self.assertEqual(findings, [])
            self.assertTrue(any("lines" in w for w in warnings), warnings)


class ExitContract(unittest.TestCase):
    """TC8, plus the half of the contract the handoff states but does not test."""

    def test_clean_fixture_is_green_under_gate(self) -> None:
        result = _run("--store", str(CLEAN), "--gate")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("no gating findings", result.stdout)

    def test_report_only_is_green_even_on_the_dirty_fixture(self) -> None:
        self.assertEqual(_run("--store", str(DIRTY)).returncode, 0)

    def test_a_missing_store_is_exit_2_not_a_silent_pass(self) -> None:
        self.assertEqual(_run("--store", "/nonexistent-store-xyz").returncode, 2)


class SessionStartDelivery(unittest.TestCase):
    """TC9 — the four cases. An empty stdout is a failure in every one."""

    def _invoke(self) -> tuple[int, dict]:
        proc = subprocess.run(
            [sys.executable, str(HERE / "session_start_summary.py")],
            input="{}", capture_output=True, text=True)
        self.assertTrue(proc.stdout.strip(), "empty stdout is never a valid output")
        return proc.returncode, json.loads(proc.stdout)

    def test_healthy_store_emits_additional_context_and_exits_zero(self) -> None:
        rc, payload = self._invoke()
        self.assertEqual(rc, 0)
        self.assertEqual(payload["hookSpecificOutput"]["hookEventName"], "SessionStart")
        self.assertIn("memory:", payload["hookSpecificOutput"]["additionalContext"])
        self.assertNotIn("systemMessage", payload)

    def test_absent_store_still_emits_and_adds_system_message(self) -> None:
        original = lint.resolve_store
        lint.resolve_store = lambda *a, **k: Path("/nonexistent-store-abc")  # type: ignore[assignment]
        try:
            payload = summary.build_payload()
        finally:
            lint.resolve_store = original  # type: ignore[assignment]
        self.assertIn("systemMessage", payload)
        self.assertIn("was NOT checked", payload["hookSpecificOutput"]["additionalContext"])

    def test_unreadable_store_still_emits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp) / "s"
            shutil.copytree(CLEAN, store)
            (store / "MEMORY.md").chmod(0o000)
            original = lint.resolve_store
            lint.resolve_store = lambda *a, **k: store  # type: ignore[assignment]
            try:
                payload = summary.build_payload()
            finally:
                lint.resolve_store = original  # type: ignore[assignment]
                (store / "MEMORY.md").chmod(0o644)
        self.assertIn("hookSpecificOutput", payload)
        self.assertIn("additionalContext", payload["hookSpecificOutput"])

    def test_a_raising_lint_still_emits_with_a_system_message(self) -> None:
        def boom(*_a, **_k):
            raise RuntimeError("synthetic lint failure")

        original = lint.check_orphans
        lint.check_orphans = boom  # type: ignore[assignment]
        try:
            payload = summary.build_payload()
        finally:
            lint.check_orphans = original  # type: ignore[assignment]
        self.assertIn("systemMessage", payload)
        self.assertIn("synthetic lint failure", payload["systemMessage"])
        self.assertIn("hookSpecificOutput", payload)


class ReadOnly(unittest.TestCase):
    """TC10 / AC5 — no run modifies the store."""

    def test_a_full_run_leaves_the_fixture_untouched(self) -> None:
        before = {p.name: p.stat().st_mtime_ns for p in CONVENTIONS.glob("*.md")}
        _run("--store", str(CONVENTIONS), "--gate")
        after = {p.name: p.stat().st_mtime_ns for p in CONVENTIONS.glob("*.md")}
        self.assertEqual(before, after)

    def test_the_lint_source_holds_no_write_call(self) -> None:
        src = (HERE / "memory_lint.py").read_text()
        for forbidden in ("write_text(", "unlink(", "open(", "mkdir(", "rmtree("):
            self.assertNotIn(forbidden, src, f"{forbidden} in a read-only lint")


class Wiring(unittest.TestCase):
    """TC11 / TC12 / AC4 — the runners actually call it."""

    def test_forge_check_task_invokes_the_lint(self) -> None:
        body = (FORGE_ROOT / "mise.toml").read_text()
        check = body[body.index("[tasks.check]"):body.index("[tasks.commit]")]
        self.assertIn("tools/memory/memory_lint.py", check)
        self.assertIn("--gate", check)

    def test_forge_gate_runs_against_a_fixture_not_the_live_store(self) -> None:
        """The live store cannot be gated — no commit could turn it green."""
        body = (FORGE_ROOT / "mise.toml").read_text()
        check = body[body.index("[tasks.check]"):body.index("[tasks.commit]")]
        self.assertIn("testdata/clean", check)

    def test_the_memory_suite_is_actually_discovered(self) -> None:
        """harmonic-forge#447: a suite `mise run check` never loads ships dead."""
        run_tests = (FORGE_ROOT / "tools" / "run_tests.py").read_text()
        self.assertIn('"memory"', run_tests)


if __name__ == "__main__":
    unittest.main()
