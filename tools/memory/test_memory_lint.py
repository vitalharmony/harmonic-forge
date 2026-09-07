"""Tests for the memory-store lint and its SessionStart delivery (harmonic-forge#494).

The numeric assertions pin to a fixture, never to the live store. The live store
is grown by the harness, so a live-pinned count is one that changes without any
commit — a test that fails on somebody else's memory write proves nothing about
this code.

**AC6 IS PINNED TO `testdata/conventions/`, A SYNTHETIC FIXTURE.** The issue as
filed named `testdata/store_d494c39/`, a frozen copy of the operator's real
memory store. That copy is not here and must never be: `harmonic-forge` is a
PUBLIC repository, and the store holds a file documenting a live Keycloak test
credential alongside investor context, resume canon, customer-naming rules and
tenant priorities. Committing it would be its first publication, and a public
push is not reversible in practice.

`testdata/conventions/` reproduces every structural condition AC6 exists to pin
— one orphan, both naming conventions resolving including the shortened-`name:`
shape, one issue reference — with no operator content, giving AC6's numbers
(1 orphan / 0 dead links / 0 broken refs) without its provenance. It is also the
better fixture on the merits: a frozen real snapshot would drift the moment the
live store is next edited, and a private-repo copy would make this suite pass
vacuously in a public-only checkout.

Amended by Lane 1 on 2026-09-06 (harmonic-forge#494, "L1 — Question 1 decided"),
which ratified dropping the frozen copy rather than relocating or redacting it.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import datetime
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import memory_lint as lint  # noqa: E402
import session_start_summary as summary  # noqa: E402

HERE = Path(__file__).resolve().parent
TESTDATA = HERE / "testdata"
CONVENTIONS = TESTDATA / "conventions"
CLEAN = TESTDATA / "clean"
DIRTY = TESTDATA / "dirty"
AGING = TESTDATA / "aging"
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
        launching three sessions.

        **Hermetic as of harmonic-forge#500.** The first version read the
        operator's real `~/.claude/settings.json` and asserted three real repo
        paths collapsed to one — which is a property of that machine's
        configuration, not of this code. On CI, where no `autoMemoryDirectory`
        is set, the fallback correctly returns a different path per repo and
        the assertion failed on correct behaviour. The property worth pinning
        is the one the code owns: WHEN the key is set, every repo resolves to
        it."""
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "config"
            cfg.mkdir()
            store = Path(tmp) / "shared-store"
            store.mkdir()
            (cfg / "settings.json").write_text(
                json.dumps({"autoMemoryDirectory": str(store)}))
            original = os.environ.get("CLAUDE_CONFIG_DIR")
            os.environ["CLAUDE_CONFIG_DIR"] = str(cfg)
            try:
                roots = [Path("/anywhere/HRSE2"), Path("/anywhere/harmonic-forge"),
                         Path("/somewhere/else/cymagraph-infra")]
                resolved = {lint.resolve_store(r) for r in roots}
            finally:
                if original is None:
                    os.environ.pop("CLAUDE_CONFIG_DIR", None)
                else:
                    os.environ["CLAUDE_CONFIG_DIR"] = original
        self.assertEqual(resolved, {store},
                         f"repos disagree on the store: {resolved}")

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

    def _invoke(self, store: Path | None = None) -> tuple[int, dict]:
        """Run the hook as a subprocess, optionally against a pinned store.

        **`store` exists because of harmonic-forge#500.** The healthy-store
        case previously inherited the ambient environment and so depended on
        the operator having a real memory store on disk. On CI there is none,
        the hook correctly reported the store as missing, and the test failed
        on correct behaviour. Pinning a fixture via `CLAUDE_CONFIG_DIR` — the
        same key `resolve_store` reads — makes "healthy" a property of the
        fixture rather than of whoever is running the suite.
        """
        env = dict(os.environ)
        tmpdir = None
        if store is not None:
            tmpdir = tempfile.TemporaryDirectory()
            cfg = Path(tmpdir.name)
            (cfg / "settings.json").write_text(
                json.dumps({"autoMemoryDirectory": str(store)}))
            env["CLAUDE_CONFIG_DIR"] = str(cfg)
        try:
            proc = subprocess.run(
                [sys.executable, str(HERE / "session_start_summary.py")],
                input="{}", capture_output=True, text=True, env=env)
        finally:
            if tmpdir is not None:
                tmpdir.cleanup()
        self.assertTrue(proc.stdout.strip(), "empty stdout is never a valid output")
        return proc.returncode, json.loads(proc.stdout)

    def test_healthy_store_emits_additional_context_and_exits_zero(self) -> None:
        rc, payload = self._invoke(store=CLEAN)
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
        """harmonic-forge#500: `resolve_store` is pinned to a fixture here too.
        Without it, on a machine with no store the hook reported the MISSING
        STORE and never reached the injected failure — so the test asserted
        the wrong error text and failed for a reason unrelated to what it
        names."""
        def boom(*_a, **_k):
            raise RuntimeError("synthetic lint failure")

        original = lint.check_orphans
        original_resolve = lint.resolve_store
        lint.check_orphans = boom  # type: ignore[assignment]
        lint.resolve_store = lambda *a, **k: CLEAN  # type: ignore[assignment]
        try:
            payload = summary.build_payload()
        finally:
            lint.check_orphans = original  # type: ignore[assignment]
            lint.resolve_store = original_resolve  # type: ignore[assignment]
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


class AgingCheckTests(unittest.TestCase):
    """harmonic-forge#500 — Check 8, and the inverted grandfathering.

    The inversion is the whole point: harmonic-forge#494 would have gated
    only files that HAD `first_seen:`, and zero of the live store's 144
    `feedback_*` files had it, so the check would have gated on nothing on
    delivery. Here a missing field is itself the finding.
    """

    def _findings(self, store: Path) -> list[str]:
        return lint.check_aging(store)

    def _named(self, store: Path, filename: str) -> list[str]:
        return [f for f in self._findings(store) if f.startswith(filename)]

    def test_ac3_a_file_missing_first_seen_is_itself_a_finding(self) -> None:
        """The inversion. Under #494's original grandfathering this file
        would have been SKIPPED, which is exactly how the check gated on
        nothing."""
        found = self._named(AGING, "feedback_no_first_seen.md")
        self.assertEqual(len(found), 1, found)
        self.assertIn("missing first_seen", found[0])

    def test_ac3_the_missing_field_finding_gates(self) -> None:
        """Not merely reported: it must reach exit 1 under `--gate`."""
        self.assertEqual(lint.run(AGING, gate=True), 1)

    def test_ac4_instances_over_threshold_without_promoted_fails(self) -> None:
        found = self._named(AGING, "feedback_recurred_unpromoted.md")
        self.assertEqual(len(found), 1, found)
        self.assertIn("instances=3", found[0])
        self.assertIn("allow_unpromoted.toml", found[0])

    def test_ac4_a_valid_promoted_marker_clears_it(self) -> None:
        """Same instances count, but promoted to an ID that exists in a
        registry and shrunk to a pointer -- no finding at all."""
        self.assertEqual(self._named(AGING, "feedback_promoted_ok.md"), [])

    def test_ac4_a_promoted_file_over_the_pointer_size_fails(self) -> None:
        """A 2 KB "pointer" is the original incident log with a marker bolted
        on, and still costs its full weight in every session."""
        found = self._named(AGING, "feedback_promoted_too_big.md")
        self.assertEqual(len(found), 1, found)
        self.assertIn("> 600", found[0])

    def test_a_promoted_id_absent_from_both_registries_fails(self) -> None:
        found = self._named(AGING, "feedback_promoted_unknown_id.md")
        self.assertEqual(len(found), 1, found)
        self.assertIn("neither rule registry", found[0])

    def test_a_fresh_one_off_never_fires(self) -> None:
        """The check must not simply flag everything -- a memory under both
        thresholds is the normal case and has to stay silent, or the signal
        is worthless."""
        self.assertEqual(self._named(AGING, "feedback_fresh.md"), [])

    def test_the_clean_fixture_stays_green_under_the_new_check(self) -> None:
        """`mise run check` gates against `testdata/clean`. Adding Check 8
        without updating that fixture would have turned the repo's own gate
        red on landing -- the same self-inflicted failure harmonic-forge#494
        hit with its README."""
        self.assertEqual(lint.check_aging(CLEAN), [])
        self.assertEqual(lint.run(CLEAN, gate=True), 0)

    def test_only_feedback_files_are_aged(self) -> None:
        """`project_*`/`reference_*`/`user_*` memories are not lessons and
        carry no promotion obligation; the policy is about feedback."""
        names = [f.split(":")[0] for f in self._findings(AGING)]
        self.assertTrue(all(n.startswith("feedback") for n in names), names)

    def test_allow_unpromoted_requires_a_reason(self) -> None:
        """An entry with an empty reason must not silence the finding: an
        exemption with no stated reason is indistinguishable from an
        oversight."""
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp)
            shutil.copytree(AGING, store, dirs_exist_ok=True)
            allow = Path(tmp) / "allow.toml"
            allow.write_text('feedback_recurred_unpromoted.md = ""\n')
            with unittest.mock.patch.object(lint, "_ALLOW_UNPROMOTED", allow):
                found = [f for f in lint.check_aging(store)
                         if f.startswith("feedback_recurred_unpromoted.md")]
        self.assertEqual(len(found), 1, "empty reason must not exempt")

    def test_allow_unpromoted_with_a_real_reason_exempts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp)
            shutil.copytree(AGING, store, dirs_exist_ok=True)
            allow = Path(tmp) / "allow.toml"
            allow.write_text(
                'feedback_recurred_unpromoted.md = "judgment call, no predicate"\n')
            with unittest.mock.patch.object(lint, "_ALLOW_UNPROMOTED", allow):
                found = [f for f in lint.check_aging(store)
                         if f.startswith("feedback_recurred_unpromoted.md")]
        self.assertEqual(found, [])


class PreCloseRegressionTests(unittest.TestCase):
    """The five defects `preclose-inspection` found in F500's first draft.

    Each is pinned here because each was invisible in the diff that
    introduced it: three were time- or environment-dependent, and two were
    silent mis-scorings that only showed against the live store.
    """

    def test_fixtures_are_immune_to_the_calendar(self) -> None:
        """FINDING 1: `clean/feedback_alpha.md` pins an absolute
        `first_seen:`, so on 2026-09-15 it crossed the 14-day threshold and
        turned harmonic-forge's own commit gate and CI red with no code
        change. The gate now pins `--today`; this asserts the flag actually
        governs, so the fixture cannot age out again."""
        self.assertEqual(
            lint.check_aging(CLEAN, today=datetime.date(2026, 12, 25)), [],
            "the clean fixture must stay green at ANY date")
        self.assertEqual(lint.run(CLEAN, gate=True,
                                  today=datetime.date(2030, 1, 1)), 0)

    def test_the_gate_pins_today_for_every_fixture_run(self) -> None:
        """The wiring half of the same finding: an unpinned fixture run in
        `mise run check` is a time bomb whose expiry is invisible in the diff
        that plants it."""
        check = (FORGE_ROOT / "mise.toml").read_text()
        check = check[check.index("[tasks.check]"):check.index("[tasks.memory-backfill]")]
        for line in check.splitlines():
            if "memory_lint.py" in line and "--store" in line:
                self.assertIn("--today", line, f"unpinned fixture run: {line}")

    def test_the_aging_assertion_pins_exit_code_one(self) -> None:
        """FINDING 5: `run()` returns 2 for a MISSING store, so a bare
        non-zero test passed if the fixture were deleted — while the comment
        above it claimed the check was proven."""
        check = (FORGE_ROOT / "mise.toml").read_text()
        self.assertIn('test "$rc" -eq 1', check)
        self.assertEqual(lint.run(TESTDATA / "does_not_exist", gate=True), 2,
                         "a missing store must be distinguishable from a finding")

    def test_promoted_ids_validate_without_a_home_directory(self) -> None:
        """FINDING 2: `_corpus()` read registries only from absolute
        `Path.home()` paths, which exist on no CI runner. `rule_ids` came
        back empty there and `promoted:` validation silently became a no-op,
        so any string acted as a promotion marker."""
        _mise, rule_ids, _roots = lint._corpus()
        self.assertTrue(rule_ids, "no rule IDs resolved — validation is a no-op")
        self.assertIn(str(FORGE_ROOT / "tools" / "rules" / "registry.toml"),
                      [str(FORGE_ROOT / "tools" / "rules" / "registry.toml")])
        self.assertTrue(
            (FORGE_ROOT / "tools" / "rules" / "registry.toml").is_file(),
            "the in-repo registry is what makes this work off the operator's box")


class InstanceParsingTests(unittest.TestCase):
    """FINDING 3: `instances:` was scored from advice prose and from
    citations of OTHER memories' enumerations. A wrong count does not merely
    misreport — for a file under the age threshold it is the sole reason a
    gating promotion obligation appears, so it invents work.
    """

    def setUp(self) -> None:
        sys.path.insert(0, str(HERE))
        import backfill_frontmatter
        self.parse = backfill_frontmatter.parse_instances

    def test_advice_prose_is_not_a_count(self) -> None:
        self.assertEqual(
            self.parse("retry the connection check at least once or twice "
                       "after a brief pause.")[0], 1)

    def test_narrating_one_incident_twice_is_not_two_incidents(self) -> None:
        self.assertEqual(
            self.parse('`check_lane3_ready.py` reported "ready" — twice, '
                       "before and after the edit.")[0], 1)

    def test_advice_to_not_wait_for_a_second_time_is_not_a_count(self) -> None:
        self.assertEqual(
            self.parse("Do this immediately — don't wait to be asked a "
                       "second time.")[0], 1)

    def test_a_citation_of_another_memorys_enumeration_is_not_borrowed(self) -> None:
        self.assertEqual(
            self.parse("Same shape as [[feedback_git_er_done_bias]] instance 4.")[0], 1)

    def test_a_line_leading_enumeration_heading_is_a_count(self) -> None:
        self.assertEqual(self.parse("**Instance 6 (2026-08-27):** it happened.")[0], 6)
        self.assertEqual(self.parse("**Tenth instance** — again.")[0], 10)

    def test_a_cardinal_count_mid_sentence_is_still_a_count(self) -> None:
        """Deliberately NOT line-anchored: "confirmed six times" is genuine
        prose, unlike the enumeration headings above."""
        self.assertEqual(self.parse("This was corrected four times.")[0], 4)
        self.assertEqual(self.parse("confirmed six times across sessions")[0], 6)

    def test_a_recurrence_verb_near_twice_is_a_count(self) -> None:
        self.assertEqual(self.parse("Recurred twice in one session.")[0], 2)

    def test_the_highest_stated_count_wins(self) -> None:
        """A file recording both an early and a later occurrence has recurred
        the later number of times."""
        self.assertEqual(
            self.parse("**8th occurrence** here.\n\n**Tenth instance** later.")[0], 10)


class FrontmatterParserTests(unittest.TestCase):
    """harmonic-forge#500: the parser had to learn to leave the nested
    `metadata:` block, or every field the backfill appends reads as missing.
    """

    def test_a_top_level_key_after_the_metadata_block_is_seen(self) -> None:
        text = ("---\nname: x\ndescription: y\nmetadata:\n  type: feedback\n"
                "  modified: 2026-01-01\nfirst_seen: 2026-02-03\ninstances: 4\n---\n\nbody\n")
        fm = lint._parse_frontmatter(text)
        self.assertEqual(fm.get("first_seen"), "2026-02-03")
        self.assertEqual(fm.get("instances"), "4")
        self.assertEqual(fm.get("metadata.type"), "feedback")

    def test_a_nested_key_is_still_not_read_as_top_level(self) -> None:
        """The dedent rule must not go the other way: an indented key stays
        nested, or `metadata.type` would collide with a top-level `type`."""
        text = ("---\nname: x\nmetadata:\n  type: feedback\n"
                "  instances: 99\n---\n\nbody\n")
        fm = lint._parse_frontmatter(text)
        self.assertNotIn("instances", fm)


if __name__ == "__main__":
    unittest.main()
