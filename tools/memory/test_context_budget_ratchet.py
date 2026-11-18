#!/usr/bin/env python3
"""Tests for the context-budget ratchet (harmonic-forge#521)."""
from __future__ import annotations

import inspect
import os
import sys
import tempfile
import tomllib
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import context_budget  # noqa: E402
import context_budget_ratchet as m  # noqa: E402


class _Repo(unittest.TestCase):
    """A fake repo whose measured surface is whatever we say it is.

    `surface()` is patched at the seam rather than building real CLAUDE.md and
    rules trees: the ratchet's job is comparing two measurements, and every
    case here is about the comparison, not about the measuring — which
    `test_context_budget.py` already covers.
    """

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = Path(tmp.name).resolve()
        self._rows: list[tuple[str, Path, int]] = []
        real_surface = context_budget.surface

        def fake_surface(repo: Path):
            return self._rows

        m.surface = fake_surface
        self.addCleanup(lambda: setattr(m, "surface", real_surface))

    def set_surface(self, **files: int) -> None:
        self._rows = [("unscoped rule", self.repo / name, size)
                      for name, size in files.items()]

    def write_baseline(self, text: str) -> None:
        m.baseline_path(self.repo).write_text(text, encoding="utf-8")


class TestUnmeasuredIsAFinding(_Repo):
    """Spec item 6 / AC5 — absence is a finding, never a silent pass."""

    def test_a_repo_with_no_baseline_fails_rather_than_passing(self):
        self.set_surface(**{"CLAUDE.md": 100})
        code, report = m.check(self.repo)
        self.assertEqual(code, 1)
        self.assertIn("UNMEASURED", report)

    def test_the_report_says_why_absence_is_not_a_pass(self):
        self.set_surface(**{"CLAUDE.md": 100})
        _code, report = m.check(self.repo)
        self.assertIn("not a repo that is under budget", report)


class TestAsymmetry(_Repo):
    """AC2 — a decrease never requires acknowledgement.

    The asymmetry is the design: making someone justify an improvement is how
    a ratchet becomes theatre, and a symmetric check would do exactly that.
    """

    def test_a_decrease_passes_with_no_entry(self):
        self.set_surface(**{"CLAUDE.md": 500})
        self.write_baseline('total_bytes = 900\n[files]\n"CLAUDE.md" = 900\n')
        code, report = m.check(self.repo)
        self.assertEqual(code, 0)
        self.assertIn("shrank by 400", report)
        self.assertIn("no acknowledgement", report)

    def test_an_unchanged_surface_passes(self):
        self.set_surface(**{"CLAUDE.md": 900})
        self.write_baseline('total_bytes = 900\n[files]\n"CLAUDE.md" = 900\n')
        self.assertEqual(m.check(self.repo)[0], 0)

    def test_an_increase_without_an_entry_fails(self):
        self.set_surface(**{"CLAUDE.md": 1200})
        self.write_baseline('total_bytes = 900\n[files]\n"CLAUDE.md" = 900\n')
        code, report = m.check(self.repo)
        self.assertEqual(code, 1)
        self.assertIn("no [[increase]] entry", report)

    def test_a_file_removed_entirely_is_a_decrease(self):
        self.set_surface(**{"CLAUDE.md": 900})
        self.write_baseline('total_bytes = 1400\n[files]\n'
                            '"CLAUDE.md" = 900\n"extra.md" = 500\n')
        self.assertEqual(m.check(self.repo)[0], 0)


class TestAntiRubberStamp(_Repo):
    """The property the whole entry exists for: it is re-measured, not read.

    An acknowledgement nobody checks is a checkbox, and a checkbox measures
    nothing. Every case below is a plausible-looking entry that fails.
    """

    BASE = 'total_bytes = 900\n[files]\n"CLAUDE.md" = 900\n"other.md" = 0\n'

    def entry(self, files: str, byte_count: str,
              why: str = 'why = "a real reason"',
              when: str = 'date = "2026-09-09"') -> None:
        self.write_baseline(
            self.BASE + f"\n[[increase]]\n{when}\nfiles = [{files}]\n"
                        f"bytes = {byte_count}\n{why}\n")

    def test_a_correct_entry_passes(self):
        self.set_surface(**{"CLAUDE.md": 1200, "other.md": 0})
        self.entry('"CLAUDE.md"', "300")
        code, report = m.check(self.repo)
        self.assertEqual(code, 0, report)
        self.assertIn("acknowledged", report)

    def test_a_wrong_byte_count_fails(self):
        """The copy-paste case: plausible, round, and not what happened."""
        self.set_surface(**{"CLAUDE.md": 1200, "other.md": 0})
        self.entry('"CLAUDE.md"', "500")
        code, report = m.check(self.repo)
        self.assertEqual(code, 1)
        self.assertIn("bytes = 500", report)
        self.assertIn("measured net change is 300", report)

    def test_naming_a_file_that_did_not_grow_fails(self):
        self.set_surface(**{"CLAUDE.md": 1200, "other.md": 0})
        self.entry('"CLAUDE.md", "other.md"', "300")
        code, report = m.check(self.repo)
        self.assertEqual(code, 1)
        self.assertIn("named but did not grow: other.md", report)

    def test_omitting_a_file_that_did_grow_fails(self):
        self.set_surface(**{"CLAUDE.md": 1200, "other.md": 50})
        self.entry('"CLAUDE.md"', "350")
        code, report = m.check(self.repo)
        self.assertEqual(code, 1)
        self.assertIn("grew but not named: other.md", report)

    def test_an_empty_why_fails(self):
        self.set_surface(**{"CLAUDE.md": 1200, "other.md": 0})
        self.entry('"CLAUDE.md"', "300", why='why = "   "')
        code, report = m.check(self.repo)
        self.assertEqual(code, 1)
        self.assertIn("no `why`", report)

    def test_a_missing_date_fails(self):
        self.set_surface(**{"CLAUDE.md": 1200, "other.md": 0})
        self.entry('"CLAUDE.md"', "300", when='date = ""')
        code, report = m.check(self.repo)
        self.assertEqual(code, 1)
        self.assertIn("no `date`", report)

    def test_a_stale_entry_does_not_authorise_a_later_growth(self):
        """An entry that was correct once must not license the next increase.

        Only the latest entry is validated, and it is validated against the
        CURRENT delta — so yesterday's honest acknowledgement cannot be left in
        place to wave through tomorrow's growth."""
        self.set_surface(**{"CLAUDE.md": 1200, "other.md": 0})
        self.entry('"CLAUDE.md"', "300")
        self.assertEqual(m.check(self.repo)[0], 0)
        self.set_surface(**{"CLAUDE.md": 1500, "other.md": 0})
        code, report = m.check(self.repo)
        self.assertEqual(code, 1, report)


class TestFailureMessageNamesWhatToMove(_Repo):
    """Spec item 7 / AC7 — the message points at the criterion, not just the
    number. A failure that says only "over budget" leaves the reader to
    rediscover hrse#1730's classification from scratch."""

    def test_the_message_names_the_files_that_grew_with_their_deltas(self):
        self.set_surface(**{"CLAUDE.md": 1200, "rules.md": 700})
        self.write_baseline('total_bytes = 900\n[files]\n"CLAUDE.md" = 900\n')
        _code, report = m.check(self.repo)
        self.assertIn("CLAUDE.md", report)
        self.assertIn("rules.md", report)
        self.assertIn("+", report)

    def test_the_message_names_the_criterion_and_the_scoping_escape(self):
        self.set_surface(**{"CLAUDE.md": 1200})
        self.write_baseline('total_bytes = 900\n[files]\n"CLAUDE.md" = 900\n')
        _code, report = m.check(self.repo)
        self.assertIn("BEFORE its first prompt", report)
        self.assertIn("hrse#1730", report)
        self.assertIn("paths:", report)
        self.assertIn("`globs:` alone does NOT", report)

    def test_the_message_offers_a_pasteable_increase_entry(self):
        self.set_surface(**{"CLAUDE.md": 1200})
        self.write_baseline('total_bytes = 900\n[files]\n"CLAUDE.md" = 900\n')
        _code, report = m.check(self.repo)
        self.assertIn("[[increase]]", report)
        self.assertIn("bytes = 300", report)
        self.assertIn("a copied entry fails", report)


class TestBaselineRoundTrip(_Repo):
    """Spec item 3 — per-file counts, not just a total."""

    def test_update_writes_every_file_and_the_total(self):
        self.set_surface(**{"CLAUDE.md": 900, "rules.md": 100})
        m.update(self.repo)
        text = m.baseline_path(self.repo).read_text(encoding="utf-8")
        self.assertIn("total_bytes = 1000", text)
        self.assertIn('"CLAUDE.md" = 900', text)
        self.assertIn('"rules.md" = 100', text)

    def test_update_preserves_increase_history(self):
        """A ratchet that forgets why it moved is a number, not a record."""
        self.set_surface(**{"CLAUDE.md": 1200, "other.md": 0})
        self.write_baseline(
            'total_bytes = 900\n[files]\n"CLAUDE.md" = 900\n"other.md" = 0\n'
            '\n[[increase]]\ndate = "2026-09-09"\nfiles = ["CLAUDE.md"]\n'
            'bytes = 300\nwhy = "a real reason"\n')
        m.update(self.repo)
        text = m.baseline_path(self.repo).read_text(encoding="utf-8")
        self.assertIn("[[increase]]", text)
        self.assertIn("a real reason", text)
        self.assertIn("total_bytes = 1200", text)

    def test_a_multi_line_why_survives_a_round_trip(self):
        """harmonic-forge#909: a rationale with newlines, quotes and a
        backslash made --update write TOML it could not read back."""
        why = 'line one\nsays "quoted" and C:\\path\nline three'
        self.set_surface(**{"CLAUDE.md": 1200})
        self.write_baseline(
            'total_bytes = 900\n[files]\n"CLAUDE.md" = 900\n'
            '\n[[increase]]\ndate = "2026-09-09"\nfiles = ["CLAUDE.md"]\n'
            'bytes = 300\nwhy = """\n' + why.replace("\\", "\\\\").replace('"', '\\"') + '"""\n')
        m.update(self.repo)
        self.assertEqual(m.load_baseline(self.repo)["increase"][0]["why"], why)

    def test_every_escape_shape_round_trips(self):
        """harmonic-forge#909 preclose: a trailing quote run was escaped twice,
        and a control character wrote TOML that could not be read back."""
        cases = ['a\n"""', 'a\n""""', 'x\n"', 'plain "q"', 'tab\there', 'a\x01b',
                 'm\x01\nz', '\nlead', '"""""\n""', 'cr\r\nx', 'x"', 'back\\slash']
        for text in cases:
            with self.subTest(text=text):
                self.assertEqual(tomllib.loads("w = " + m._toml_string(text))["w"], text)

    def test_a_rewritten_baseline_passes_its_own_check(self):
        self.set_surface(**{"CLAUDE.md": 900, "rules.md": 100})
        m.update(self.repo)
        self.assertEqual(m.check(self.repo)[0], 0)

    def test_malformed_toml_is_a_refusal_not_a_silent_pass(self):
        self.set_surface(**{"CLAUDE.md": 900})
        self.write_baseline("this is not = = toml\n")
        with self.assertRaises(m.RatchetError):
            m.check(self.repo)


class TestGateNarrowsToWhatTheRepoControls(_Repo):
    """harmonic-forge#538 — out-of-repo growth is reported, never gated.

    29% of HRSE2's baselined surface is `~/.claude/CLAUDE.md` and the shared
    memory index. Writing a memory is a standing directive every session
    follows, so gating on those turned a routine, instructed action into a red
    build plus a bookkeeping commit in an unrelated repo — and a gate that a
    session breaks by doing as it is told gets commented out, after which the
    ratchet protects nothing.
    """

    def set_mixed(self, in_repo_bytes: int, outside_bytes: int) -> None:
        """One file inside the repo, one outside, measured together."""
        self._rows = [
            ("unscoped rule", self.repo / "CLAUDE.md", in_repo_bytes),
            ("memory index", Path.home() / "memstore" / "MEMORY.md", outside_bytes),
        ]

    BASE = ('total_bytes = 1900\n[files]\n'
            '"CLAUDE.md" = 900\n"~/memstore/MEMORY.md" = 1000\n')

    def test_out_of_repo_growth_alone_does_not_gate(self):
        """The exact scenario found live post-merge on harmonic-forge#521."""
        self.set_mixed(in_repo_bytes=900, outside_bytes=1100)
        self.write_baseline(self.BASE)
        code, report = m.check(self.repo)
        self.assertEqual(code, 0, report)

    def test_out_of_repo_growth_is_still_reported(self):
        """Reported, not gated — the distinction is the whole fix. Silently
        ignoring it would hide hrse#1730's finding that a third of the surface
        is not the repo's to trim."""
        self.set_mixed(in_repo_bytes=900, outside_bytes=1100)
        self.write_baseline(self.BASE)
        _code, report = m.check(self.repo)
        self.assertIn("~/memstore/MEMORY.md +100", report)
        self.assertIn("NOT gated", report)
        self.assertIn("2000 bytes", report,
                      "the printed total must still include the grown file")

    def test_in_repo_growth_still_gates(self):
        """The regression guard. This fix must narrow the gate, never disable
        it — an over-broad version would pass everything and read as green."""
        self.set_mixed(in_repo_bytes=1200, outside_bytes=1000)
        self.write_baseline(self.BASE)
        code, report = m.check(self.repo)
        self.assertEqual(code, 1)
        self.assertIn("CLAUDE.md", report)

    def test_in_repo_growth_gates_even_when_out_of_repo_shrank(self):
        """A shrinking memory index must not bankroll in-repo growth: the
        totals would net out and the gate would silently stop firing."""
        self.set_mixed(in_repo_bytes=1200, outside_bytes=200)
        self.write_baseline(self.BASE)
        code, _report = m.check(self.repo)
        self.assertEqual(code, 1)

    def test_both_growing_gates_on_the_in_repo_part_only(self):
        self.set_mixed(in_repo_bytes=1200, outside_bytes=1100)
        self.write_baseline(self.BASE)
        code, report = m.check(self.repo)
        self.assertEqual(code, 1)
        self.assertIn("+   300  CLAUDE.md", report)
        self.assertIn("NOT gated", report)

    def test_an_increase_entry_covers_only_the_in_repo_delta(self):
        """AC2 — the acknowledgement mechanism is unaffected for in-repo
        growth, and it is re-measured against the GATED delta, so an author
        does not have to account for bytes outside their repo."""
        self.set_mixed(in_repo_bytes=1200, outside_bytes=1100)
        self.write_baseline(
            self.BASE + '\n[[increase]]\ndate = "2026-09-09"\n'
                        'files = ["CLAUDE.md"]\nbytes = 300\n'
                        'why = "a real reason"\n')
        code, report = m.check(self.repo)
        self.assertEqual(code, 0, report)
        self.assertIn("acknowledged", report)

    def test_in_repo_shrink_with_out_of_repo_growth_passes_quietly(self):
        self.set_mixed(in_repo_bytes=500, outside_bytes=1100)
        self.write_baseline(self.BASE)
        code, report = m.check(self.repo)
        self.assertEqual(code, 0)
        self.assertIn("in-repo surface shrank", report)


class TestInRepoPredicate(unittest.TestCase):
    """The gate's scope reads the baseline key's own shape — the same
    distinction `_relative_key` writes — rather than re-resolving paths."""

    def test_repo_relative_keys_are_in_repo(self):
        for key in ("CLAUDE.md", ".claude/rules/planning.md"):
            with self.subTest(key=key):
                self.assertTrue(m.in_repo(key))

    def test_home_and_absolute_keys_are_not(self):
        for key in ("~/.claude/CLAUDE.md",
                    "~/Harmonic_Projects/operator-memory/MEMORY.md",
                    "/etc/somewhere/else.md"):
            with self.subTest(key=key):
                self.assertFalse(m.in_repo(key))


class TestSymlinkAttribution(unittest.TestCase):
    """harmonic-forge#1414: a repo-relative key whose real path is outside the
    repo is reported, not gated. Real temp dirs, real symlinks, real sizes —
    no `surface()` stub, because the bug lived in what the key resolves to."""

    KEY = ".claude/rules/linked.md"

    def setUp(self) -> None:
        repo_tmp = tempfile.TemporaryDirectory()
        out_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(repo_tmp.cleanup)
        self.addCleanup(out_tmp.cleanup)
        self.repo = Path(repo_tmp.name).resolve()
        self.outside = Path(out_tmp.name).resolve()
        (self.repo / ".claude" / "rules").mkdir(parents=True)
        (self.repo / "CLAUDE.md").write_text("x" * 100, encoding="utf-8")

    def link(self, target: Path) -> Path:
        link = self.repo / self.KEY
        os.symlink(target, link)
        return target

    def baseline_then_grow(self, path: Path, grow: int = 500) -> tuple[int, str]:
        m.update(self.repo)
        path.write_text(path.read_text(encoding="utf-8") + "y" * grow,
                        encoding="utf-8")
        return m.check(self.repo)

    def test_a_link_to_a_file_outside_is_reported_not_gated(self):
        target = self.outside / "rule.md"
        target.write_text("r" * 200, encoding="utf-8")
        self.link(target)
        code, report = self.baseline_then_grow(target)
        self.assertEqual(code, 0, report)
        self.assertIn("NOT gated", report)
        self.assertIn(f"{self.KEY} +500", report)
        self.assertIn("symlink outside the repo", report)

    def test_b_link_to_a_file_inside_the_repo_gates(self):
        target = self.repo / "docs-rule.md"
        target.write_text("r" * 200, encoding="utf-8")
        self.link(target)
        code, report = self.baseline_then_grow(target)
        self.assertEqual(code, 1, report)

    def test_c_ordinary_file_gates(self):
        plain = self.repo / self.KEY
        plain.write_text("r" * 200, encoding="utf-8")
        code, report = self.baseline_then_grow(plain)
        self.assertEqual(code, 1, report)

    def test_d_deleted_tracked_file_stays_in_repo(self):
        self.assertTrue(m.in_repo(".claude/rules/gone.md", repo=self.repo))

    def test_e_platform_rule_link_outside_still_gates(self):
        (self.repo / "sync_rules.py").write_text(
            'UNIVERSAL_RULE_FILES = ["x.md"]\n', encoding="utf-8")
        (self.repo / "rules").mkdir()
        own = self.repo / "rules" / "x.md"
        own.write_text("r" * 200, encoding="utf-8")
        foreign = self.outside / "x.md"
        foreign.write_text("r" * 200, encoding="utf-8")
        os.symlink(foreign, self.repo / ".claude" / "rules" / "x.md")
        code, report = self.baseline_then_grow(own)
        self.assertEqual(code, 1, report)

    def test_f_every_check_call_site_passes_repo(self):
        """Outside link grows AND an in-repo file shrinks, so all four
        `in_repo` call sites in `check` run; any one missing `repo=` fails."""
        target = self.outside / "rule.md"
        target.write_text("r" * 200, encoding="utf-8")
        self.link(target)
        m.update(self.repo)
        target.write_text("r" * 700, encoding="utf-8")
        (self.repo / "CLAUDE.md").write_text("x" * 50, encoding="utf-8")
        calls: list[Path | None] = []
        real = m.in_repo

        def spy(key, repo=None):
            calls.append(repo)
            return real(key, repo)

        m.in_repo = spy
        self.addCleanup(lambda: setattr(m, "in_repo", real))
        code, report = m.check(self.repo)
        self.assertEqual(code, 0, report)
        self.assertTrue(calls)
        self.assertNotIn(None, calls, "a check() call site dropped repo=")
        # Each of the four sites ran at least once.
        src = inspect.getsource(m.check)
        self.assertEqual(src.count("in_repo(key, repo=repo)"), 4)
        self.assertNotRegex(src, r"in_repo\(key\)")

    def test_g_report_line_has_no_absolute_path(self):
        target = self.outside / "rule.md"
        target.write_text("r" * 200, encoding="utf-8")
        self.link(target)
        _code, report = self.baseline_then_grow(target)
        line = next(l for l in report.splitlines() if "NOT gated" in l)
        self.assertNotIn("/home", line)
        self.assertNotIn(str(self.outside), line)
        note = line.split("symlink outside the repo: ", 1)[1]
        self.assertFalse(note.startswith("/"), line)
        self.assertNotIn("../..", note, "relpath climbed toward the root")

    def test_g2_note_forms(self):
        """Under the repo's parent: `../`; under home: `~/`; else the key."""
        # Own layout under self.outside: <o>/wt/repo links to <o>/forge/x.md
        # and <o>/wt/x.md — a `.worktrees/` checkout's shape.
        repo = self.outside / "wt" / "repo"
        (repo / ".claude" / "rules").mkdir(parents=True)
        (self.outside / "forge").mkdir()
        far = self.outside / "forge" / "x.md"
        near = self.outside / "wt" / "x.md"
        for f in (far, near):
            f.write_text("r", encoding="utf-8")
        link = repo / self.KEY
        os.symlink(near, link)
        self.assertEqual(m._symlink_note(repo, self.KEY),
                         " (symlink outside the repo: ../x.md)")
        link.unlink()
        os.symlink(far, link)
        with mock.patch.object(m.Path, "home", return_value=self.outside):
            self.assertEqual(m._symlink_note(repo, self.KEY),
                             " (symlink outside the repo: ~/forge/x.md)")
        with mock.patch.object(m.Path, "home", return_value=self.repo):
            self.assertEqual(m._symlink_note(repo, self.KEY),
                             f" (symlink outside the repo: {self.KEY})")


class TestKeysForFilesOutsideTheRepo(unittest.TestCase):
    """Two measured files live outside the repo — the operator's global
    CLAUDE.md and the shared memory index. A repo-relative-only key would
    crash or drop them, understating the surface by exactly the two files
    nobody can trim from inside the repo."""

    def test_a_home_relative_key_is_used_outside_the_repo(self):
        repo = Path("/tmp/some-repo")
        key = m._relative_key(repo, Path.home() / ".claude" / "CLAUDE.md")
        self.assertEqual(key, "~/.claude/CLAUDE.md")

    def test_a_repo_relative_key_is_used_inside(self):
        repo = Path("/tmp/some-repo")
        self.assertEqual(m._relative_key(repo, repo / "CLAUDE.md"), "CLAUDE.md")


class TestDerivationIsRecorded(unittest.TestCase):
    """AC4 — the numbers and their provenance live in the tool's own text,
    including that the target is a judgment call rather than a derivation."""

    def test_the_two_numbers_are_what_the_ratification_settled(self):
        self.assertEqual(m.CEILING_BYTES, 48_000)
        self.assertEqual(m.TARGET_BYTES, 24_000)

    def test_the_docstring_cites_the_compaction_measurement(self):
        self.assertIn("harmonic-forge#497", m.__doc__)
        self.assertIn("10-56", m.__doc__)
        self.assertIn("12k", m.__doc__)

    def test_the_docstring_labels_the_target_as_a_judgment_call(self):
        self.assertIn("judgment call", m.__doc__)
        self.assertIn("not derived from anything", m.__doc__)

    def test_the_docstring_carries_the_compliance_table(self):
        self.assertIn("77,123", m.__doc__)
        self.assertIn("harmonic-forge", m.__doc__)

    def test_the_docstring_records_that_there_was_no_gate(self):
        """The finding that reframed the issue: not an unheeded gate, no gate.
        Whitespace-normalized because the sentence wraps in the source."""
        flat = " ".join(m.__doc__.split())
        self.assertIn("There was no unheeded gate; there was no gate", flat)


class TestNoNetwork(unittest.TestCase):
    """AC6 — local only. The runner cannot see `~/.claude/CLAUDE.md` or the
    memory index, so a CI invocation would measure a different, smaller
    surface and report a pass that means nothing."""

    def test_the_source_reaches_no_network(self):
        source = Path(m.__file__).read_text(encoding="utf-8")
        for forbidden in ("requests", "urllib", "httpx", "socket",
                          "subprocess", "gh api"):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
