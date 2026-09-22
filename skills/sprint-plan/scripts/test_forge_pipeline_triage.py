#!/usr/bin/env python3
"""Tests for forge_pipeline_triage.py (hrse#1476)."""

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

import forge_pipeline_triage as fpt
import pipeline_rank as pr


def _page(items):
    return json.dumps(items)


class FetchOpenIssuesTests(__import__("unittest").TestCase):
    def test_pull_requests_are_skipped(self):
        raw = _page([
            {"number": 1, "title": "a real issue", "body": "x"},
            {"number": 2, "title": "a PR", "body": "y", "pull_request": {"url": "..."}},
        ])
        with patch.object(fpt, "_run", return_value=raw):
            issues = fpt.fetch_open_issues("vitalharmony/hrse")
        self.assertEqual([i["number"] for i in issues], [1])

    def test_pagination_is_not_silently_truncated_past_30_items(self):
        """hrse#1476's own named trap: the manual proof-of-concept's own
        methodology note flags the `gh issue list` 30-item default-limit
        trap that has previously bitten this project (hrse#966/forge#430).
        Reproduces that trap here: 45 issues split across two REST pages,
        concatenated by `--paginate`, must all survive."""
        page1 = _page([{"number": n, "title": f"issue {n}", "body": ""} for n in range(1, 46)])
        page2 = _page([{"number": n, "title": f"issue {n}", "body": ""} for n in range(46, 51)])
        with patch.object(fpt, "_run", return_value=page1 + page2) as mock_run:
            issues = fpt.fetch_open_issues("vitalharmony/harmonic-forge")
        self.assertEqual(mock_run.call_count, 1)  # one gh invocation, --paginate does the rest
        # The mechanism, not just the count: without --paginate on the actual
        # `gh api` invocation, gh's own default page size silently truncates
        # at 30 regardless of how many pages this test's mock hands back —
        # verified by removing the flag from the source and confirming this
        # assertion is the one that catches it (hrse#1476 preclose-inspection
        # finding: the prior version of this test passed either way).
        self.assertIn("--paginate", mock_run.call_args.args)
        self.assertEqual(len(issues), 50)
        self.assertEqual([i["number"] for i in issues][:3], [1, 2, 3])
        self.assertEqual([i["number"] for i in issues][-1], 50)

    def test_scan_limit_fires_loudly_rather_than_silently_truncating(self):
        """The inverse of the pagination trap: if a repo genuinely has more
        open issues than ISSUE_SCAN_LIMIT, fail rather than silently
        returning a partial, misleadingly-confident triage."""
        many = _page([{"number": n, "title": "x", "body": ""} for n in range(1, fpt.ISSUE_SCAN_LIMIT + 5)])
        with patch.object(fpt, "_run", return_value=many):
            with self.assertRaises(fpt.TriageError):
                fpt.fetch_open_issues("vitalharmony/hrse")


class ReferenceExtractionTests(__import__("unittest").TestCase):
    def test_explicit_forge_prefix_is_matched(self):
        body = "This depends on forge#96 landing first."
        matches = list(fpt._REFERENCE_RE.finditer(body))
        self.assertEqual([m.group(1) for m in matches], ["96"])

    def test_full_harmonic_forge_prefix_is_matched(self):
        body = "See harmonic-forge#317 for the epic."
        matches = list(fpt._REFERENCE_RE.finditer(body))
        self.assertEqual([m.group(1) for m in matches], ["317"])

    def test_bare_hash_number_is_never_matched(self):
        """The deliberate design choice (hrse#1476, found live): a bare
        `#N` is this repo's own-repo reference convention, not cross-repo.
        Matching it produced a real false positive during development —
        this issue's own filing text matched itself. Regression-guarded."""
        body = "Part of #96, blocked on #305, see also #11."
        matches = list(fpt._REFERENCE_RE.finditer(body))
        self.assertEqual(matches, [])


class ClassifyTests(__import__("unittest").TestCase):
    def _entry(self, entries, number):
        # `repo` filter matters now: `classify()` emits hrse rows too, and an
        # hrse issue can share a number with a forge one.
        return next(e for e in entries
                    if e.repo == "forge" and e.issue == number)

    def _forge(self, entries):
        return [e for e in entries if e.repo == "forge"]

    def test_block_language_wins_bucket_a(self):
        forge_open = {96}
        forge_meta = {96: {"theme": "Ops", "venture": "CymaGraph", "title": "t"}}
        hrse_issues = [{"number": 1, "title": "x", "body": "This is blocked on forge#96 shipping."}]
        entries = fpt.classify(forge_open, forge_meta, hrse_issues)
        self.assertEqual(self._entry(entries, 96).bucket, "A")

    def test_plain_reference_without_block_language_is_bucket_b(self):
        forge_open = {96}
        forge_meta = {96: {"theme": "Ops", "venture": "CymaGraph", "title": "t"}}
        hrse_issues = [{"number": 1, "title": "x", "body": "See forge#96 for background."}]
        entries = fpt.classify(forge_open, forge_meta, hrse_issues)
        self.assertEqual(self._entry(entries, 96).bucket, "B")

    def test_no_reference_at_all_is_bucket_c(self):
        forge_open = {96}
        forge_meta = {96: {"theme": "Ops", "venture": "CymaGraph", "title": "t"}}
        hrse_issues = [{"number": 1, "title": "x", "body": "Nothing about forge here."}]
        entries = fpt.classify(forge_open, forge_meta, hrse_issues)
        self.assertEqual(self._entry(entries, 96).bucket, "C")

    def test_every_open_forge_issue_gets_exactly_one_entry(self):
        forge_open = {1, 2, 3}
        forge_meta = {1: {"theme": "A", "venture": "V", "title": "one"}}
        entries = fpt.classify(forge_open, forge_meta, [])
        self.assertEqual(sorted(e.issue for e in entries), [1, 2, 3])

    def test_forge_issue_off_the_board_is_not_silently_dropped(self):
        """An issue absent from `forge_meta` (unboarded) still gets an
        entry, with Theme/Venture as None -- not a reason to skip it."""
        forge_open = {99}
        entries = fpt.classify(forge_open, {}, [])
        entry = self._entry(entries, 99)
        self.assertIsNone(entry.theme)
        self.assertEqual(entry.bucket, "C")

    def test_citation_is_the_sentence_not_the_whole_body(self):
        forge_open = {96}
        forge_meta = {96: {"theme": None, "venture": None, "title": "t"}}
        body = "Unrelated first sentence. This depends on forge#96 for real. Unrelated last sentence."
        hrse_issues = [{"number": 1, "title": "x", "body": body}]
        entries = fpt.classify(forge_open, forge_meta, hrse_issues)
        sentence = self._entry(entries, 96).referenced_by[0]["sentence"]
        self.assertIn("forge#96", sentence)
        self.assertNotIn("Unrelated first", sentence)
        self.assertNotIn("Unrelated last", sentence)

    def test_bullet_list_reference_does_not_absorb_a_neighboring_bullet(self):
        """hrse#1476 preclose-inspection finding, live-reproduced: hrse issue
        bodies are predominantly bullet lists with no periods, so the
        sentence-boundary search must stop at a newline, not fall through to
        a raw ±120-char window that can pull in an unrelated adjacent
        bullet's language (e.g. "blocks") and wrongly promote a plain
        dependency into Bucket A."""
        forge_open = {96}
        forge_meta = {96: {"theme": None, "venture": None, "title": "t"}}
        body = (
            "## Dependencies\n"
            "- Needs forge#96 to land first\n"
            "- The stale-cache defect blocks the nightly enrichment run\n"
        )
        hrse_issues = [{"number": 1, "title": "x", "body": body}]
        entries = fpt.classify(forge_open, forge_meta, hrse_issues)
        entry = self._entry(entries, 96)
        self.assertEqual(entry.bucket, "B")
        sentence = entry.referenced_by[0]["sentence"]
        self.assertNotIn("blocks", sentence)


class BoardFallbackTests(__import__("unittest").TestCase):
    """hrse#1476 preclose-inspection finding: in the repo-hygiene.yml CI
    runner, harmonic-forge is checked out at $GITHUB_WORKSPACE/harmonic-forge,
    not $HOME/harmonic-forge, so `_item_list_cache` used to always be None
    there and the direct `gh project item-list` fallback (previously
    untested) always ran unexercised in the one environment that actually
    uses it."""

    def test_direct_gh_project_item_list_fallback_parses_correctly(self):
        payload = json.dumps({"items": [
            {"theme": "Ops", "venture": "CymaGraph",
             "content": {"repository": fpt.FORGE_REPO, "number": 96, "title": "t"}},
            {"theme": None, "venture": None,
             "content": {"repository": "vitalharmony/other-repo", "number": 1, "title": "skip me"}},
        ]})
        with patch.object(fpt, "_item_list_cache", None), \
             patch.object(fpt, "_run", return_value=payload) as mock_run:
            result = fpt.fetch_forge_theme_venture()
        self.assertEqual(result, {96: {"theme": "Ops", "venture": "CymaGraph", "title": "t"}})
        self.assertIn("--format", mock_run.call_args.args)

    def test_direct_fallback_scan_limit_fires_loudly(self):
        payload = json.dumps({"items": [{"content": {}}] * 5000})
        with patch.object(fpt, "_item_list_cache", None), \
             patch.object(fpt, "_run", return_value=payload):
            with self.assertRaises(fpt.TriageError):
                fpt.fetch_forge_theme_venture()


class NeverMutatesTests(__import__("unittest").TestCase):
    #: Mutating gh forms this module must never issue, as the individual
    #: argv tokens `_run()` actually receives (every real call in this
    #: module is `_run("gh", "api", ..., "-X", "PATCH", ...)`-shaped, one
    #: token per argument, not a single string containing "-X PATCH" —
    #: a substring scan over the raw source text misses that shape entirely
    #: and was verified to pass with a real mutating call appended
    #: (hrse#1476 preclose-inspection finding).
    _MUTATING_TOKEN_SEQUENCES = (
        ("-X", "POST"), ("-X", "PATCH"), ("-X", "PUT"), ("-X", "DELETE"),
        ("item-edit",), ("issue", "close"), ("issue", "edit"), ("issue", "comment"),
    )

    def test_module_defines_no_write_capable_function(self):
        """Report-only per AC2 — every `_run(...)` call site's own argument
        tokens (via AST, not the raw source text) are checked for a
        mutating gh form, so a call written in the module's real calling
        convention — one token per argv element — is still caught."""
        import ast

        source = Path(fpt.__file__).read_text()
        tree = ast.parse(source)

        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == "_run"):
                continue
            tokens = [a.value for a in node.args
                      if isinstance(a, ast.Constant) and isinstance(a.value, str)]
            for seq in self._MUTATING_TOKEN_SEQUENCES:
                for i in range(len(tokens) - len(seq) + 1):
                    self.assertNotEqual(
                        tuple(tokens[i:i + len(seq)]), seq,
                        f"_run() call at line {node.lineno} contains mutating "
                        f"gh form {seq!r} in its literal argument tokens",
                    )

    def test_guard_actually_catches_a_mutating_call_written_in_module_idiom(self):
        """Regression guard for the finding itself: confirms the AST check
        above is not itself another guard the module's idiom defeats, by
        running it against source with a real mutating call appended in
        exactly the tokenized shape every other call in this module uses."""
        import ast

        source = Path(fpt.__file__).read_text() + (
            '\n\ndef _mutate(n):\n'
            '    _run("gh", "api", "-X", "PATCH", f"repos/x/issues/{n}", "-f", "state=closed")\n'
        )
        tree = ast.parse(source)
        found = False
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == "_run"):
                continue
            tokens = [a.value for a in node.args
                      if isinstance(a, ast.Constant) and isinstance(a.value, str)]
            if any(tuple(tokens[i:i + 2]) == ("-X", "PATCH")
                   for i in range(len(tokens) - 1)):
                found = True
        self.assertTrue(found, "guard failed to detect an appended mutating call")


if __name__ == "__main__":
    __import__("unittest").main()


class SummarySectionTests(__import__("unittest").TestCase):
    """hrse#1477 — the sprint-summary emitter.

    Separate from `render()`'s tests on purpose: two consumers, two shapes,
    two functions. `render()` feeds a terminal; this feeds
    `board_dashboard_renderer.py`'s line grammar, which turns anything it
    cannot parse into red `class="unparsed"` monospace on the live page.
    """

    def _entry(self, repo, issue, tier, title="t", theme=None, venture=None):
        return fpt.TriageEntry(repo=repo, issue=issue, title=title, group=tier,
                               group_why="why", theme=theme, venture=venture)

    def test_tc4_no_line_uses_a_shape_the_renderer_rejects(self):
        """The `<-` citation and `!!` ratchet are the two shapes proven red."""
        entries = [self._entry("forge", 96, "1", theme="Ops", venture="Platform"),
                   self._entry("hrse", 1477, "1")]
        lines = fpt.render_summary_section(entries, "2.9")
        self.assertTrue(lines)
        for line in lines:
            self.assertFalse(line.startswith("<-"), line)
            self.assertFalse(line.startswith("!!"), line)
        self.assertTrue(any(l.startswith("**Ratchet:**") for l in lines))

    def test_tc6_every_row_renders_and_the_count_is_the_whole_group(self):
        """hrse#1543's requirement, satisfied more completely by hrse#1604.

        #1543 required "the heading must not claim a total it is not showing",
        and met it by printing `(5 of 12)` — the elision made visible rather
        than silent. Visible was not enough: the other seven rows were still
        unreachable from that section, which is what hrse#1604 reported.

        Uncapping satisfies #1543's requirement outright, so the `(N of M)`
        form is gone rather than weakened — with no cap there is nothing to
        elide, and a heading that can only ever print the true total cannot
        misstate it. This test is #1543's, rewritten rather than deleted, so
        the reason the assertion inverted stays on the record.
        """
        entries = [self._entry("forge", n, "1") for n in range(1, 13)]
        lines = fpt.render_summary_section(entries, "2.9")
        rows = [l for l in lines if l.startswith("- ")]
        self.assertEqual(len(rows), 12, "every row in the group must render")
        heading = next(l for l in lines if l.startswith("## "))
        self.assertIn("(12)", heading)
        self.assertNotIn(" of ", heading,
                         "the elision label cannot occur — there is no cap")

    def test_the_cap_constant_is_retired(self):
        """hrse#1604 step 3. A surviving constant that nothing reads is how a
        cap gets quietly reintroduced by someone who finds it and assumes it
        was meant to be used."""
        self.assertFalse(hasattr(fpt, "SUMMARY_ROWS_PER_GROUP"))

    def test_an_uncapped_group_still_shows_a_plain_count(self):
        entries = [self._entry("forge", n, "1") for n in range(1, 3)]
        lines = fpt.render_summary_section(entries, "2.9")
        heading = next(l for l in lines if l.startswith("## "))
        self.assertIn("(2)", heading)
        self.assertNotIn(" of ", heading)

    def test_tc7_hrse_rows_are_classified_not_merely_referenced(self):
        """#1489's whole change. Asserts the PROPERTY, conditional on hrse
        rows existing — under a 2.8 injection there are currently zero, and
        the anchor moves, so a count would be pinned to a moment.

        hrse#1522: the row shape assertion changed from the old em-dash
        format to the bracketed `[Tier | Theme]` shape — hrse rows now carry
        the board `Tier`/`Theme` fields too (previously forge-only), so both
        repos share one row grammar rather than two.
        """
        entries = [self._entry("forge", 96, "1"), self._entry("hrse", 1477, "3")]
        lines = fpt.render_summary_section(entries, "2.9")
        hrse_rows = [l for l in lines if l.startswith("- hrse#")]
        for row in hrse_rows:
            self.assertRegex(row, r"^- hrse#\d+ \[[^|\]]+ \| [^\]]+\] ")
        if hrse_rows:
            group3 = [l for l in lines if l.startswith("## ") and "Group 3" in l]
            self.assertTrue(group3, "an hrse row must appear under a group heading")

    def test_tc8_the_heading_names_the_injected_milestone(self):
        """The section can never silently disagree with the page header.

        Falsification: passing a different milestone must change the heading.
        The two resolvers genuinely diverged live (2.8 vs 2.9) before the
        milestone was injected, so a test that only passes today would not
        have caught it.
        """
        entries = [self._entry("forge", 96, "1")]
        self.assertIn("— 2.9 —", fpt.render_summary_section(entries, "2.9")[1])
        self.assertIn("— 3.0 —", fpt.render_summary_section(entries, "3.0")[1])

    def test_tc10_missing_board_tier_or_theme_renders_unset_not_omitted(self):
        """hrse#1522: `board_tier`/`theme` are real board data now (plumbed
        from the same `board_fields()` read `milestone_summary.py` already
        performs), not the old forge-only fabrication risk this test used
        to guard against — so an unset value renders the literal `unset`
        placeholder, the same convention `milestone_summary_render._line()`
        already uses for a bracketed row's Tier cell, rather than being
        silently dropped."""
        lines = fpt.render_summary_section(
            [self._entry("hrse", 1477, "1", theme=None, venture=None)], "2.9")
        row = next(l for l in lines if l.startswith("- hrse#"))
        self.assertIn("[unset | unset]", row)
        forge = fpt.render_summary_section(
            [self._entry("forge", 96, "1", theme="Ops", venture="Platform")], "2.9")
        self.assertIn("[unset | Ops]", next(l for l in forge if l.startswith("- ")))

    def test_tc9_failure_text_is_collapsed_and_leads_with_bold(self):
        """`gh_json` interpolates raw multi-line stderr; every one of these
        was verified to render `unparsed` if passed through as-is."""
        for raw in ("- gh: could not authenticate",
                    '{"message":"Bad credentials"}',
                    "!! something",
                    "<- something",
                    "line one\nline two\n  line three"):
            with self.subTest(raw=raw):
                lines = fpt.render_summary_failure(RuntimeError(raw))
                body = lines[-1]
                self.assertTrue(body.startswith("**Triage unavailable:**"), body)
                self.assertNotIn("\n", body)

    def test_tc9_systemexit_is_representable(self):
        """`SystemExit` is not an `Exception`; the handler must take
        `BaseException` or it catches none of the real failure paths."""
        self.assertFalse(issubclass(SystemExit, Exception))
        lines = fpt.render_summary_failure(SystemExit("anchor is stale"))
        self.assertIn("anchor is stale", lines[-1])

    def test_empty_entries_emit_only_the_ratchet(self):
        lines = fpt.render_summary_section([], "2.9")
        self.assertEqual([l for l in lines if l.startswith("## ")], [])
        self.assertTrue(any("Ratchet" in l for l in lines))


class NotReadyDemotionTests(unittest.TestCase):
    """hrse#1523 — the four live cases named in the issue: hrse#199
    (blocked on an open #192), hrse#258 (epic), hrse#303/#304 (scope-capture
    hint, unlabelled), hrse#1025 (design-gated hint, unlabelled)."""

    FORGE_META: dict = {}

    def _hrse(self, number, title, body, labels=("feature",), extra=None):
        issue = {"number": number, "title": title, "body": body,
                 "labels": [{"name": name} for name in labels]}
        if extra:
            issue.update(extra)
        return issue

    def test_ac1_blocked_on_a_still_open_dependency_is_demoted_hrse199(self):
        blocker = self._hrse(
            192, "BACKLOG-052 Phase 1", "Lifecycle states on ActionItem.")
        blocked = self._hrse(
            199, "Google Tasks Self-Accountability Sync",
            "## Dependencies\n\n- **#192 (BACKLOG-052 Phase 1)** — the "
            "lifecycle states. This issue consumes those states; it cannot "
            "ship before them.\n\nSyncs via google_auth_service and Task nodes.")
        entries = fpt.classify(set(), self.FORGE_META, [blocker, blocked],
                               hrse_reference_sources=[blocker, blocked])
        row = next(e for e in entries if e.issue == 199)
        self.assertEqual(row.group, pr.GROUP_NOT_READY)
        self.assertIn("hrse#192", row.group_why)

    def test_ac1_a_closed_blocker_does_not_demote(self):
        """The falsification case: #192 absent from the open set (i.e.
        closed) must leave #199 ranked exactly as its own text would rank
        it — the demotion is about the blocker's STATE, not its mention."""
        blocked = self._hrse(
            199, "Google Tasks Self-Accountability Sync",
            "## Dependencies\n\n- **#192 (BACKLOG-052 Phase 1)** — the "
            "lifecycle states. This issue consumes those states; it cannot "
            "ship before them.\n\nSyncs via google_auth_service and Task nodes.")
        entries = fpt.classify(set(), self.FORGE_META, [blocked],
                               hrse_reference_sources=[blocked])
        row = next(e for e in entries if e.issue == 199)
        self.assertNotEqual(row.group, pr.GROUP_NOT_READY)

    def test_ac4_an_epic_is_separated_regardless_of_child_rank_hrse258(self):
        child = self._hrse(
            226, "Cy child slice", "Part of #258. profile_service change.")
        epic = self._hrse(
            258, "EPIC: Cy — in-app AI assistant", "Cy owns zero new data model.",
            labels=("feature", "epic"))
        entries = fpt.classify(set(), self.FORGE_META, [child, epic],
                               hrse_reference_sources=[child, epic])
        row = next(e for e in entries if e.issue == 258)
        self.assertEqual(row.group, pr.GROUP_NOT_READY)
        # AC5: cited as the CHILDREN's rank — the epic's own body names no
        # stage, so this text is only correct if it came from inheritance.
        self.assertIn("children would rank 1", row.group_why)

    def test_ac5_an_epic_with_no_children_cites_its_own_rank_not_a_phantom_child(self):
        """preclose finding: `inherit_epic_group` leaves the ranking
        unchanged when there is nothing to inherit from, and the demotion
        message must say "its own rank", never "children would rank" for a
        number that came from the epic's own body."""
        epic = self._hrse(
            700, "EPIC: something with no children in this milestone",
            "profile_service change.", labels=("feature", "epic"))
        entries = fpt.classify(set(), self.FORGE_META, [epic],
                               hrse_reference_sources=[epic])
        row = next(e for e in entries if e.issue == 700)
        self.assertEqual(row.group, pr.GROUP_NOT_READY)
        self.assertIn("its own rank would be 1", row.group_why)
        self.assertNotIn("children would rank", row.group_why)

    def test_ac3_scope_capture_hint_hrse303_hrse304_never_demotes_unlabelled(self):
        for number, title in ((303, "Cy: scheduled self-checking"),
                              (304, "Cy: periodic news-monitoring")):
            with self.subTest(number=number):
                issue = self._hrse(
                    number, title,
                    "Reads opportunity_service state, generalizing a "
                    "scheduled pattern.\n\nPick up via a `product-strategy` "
                    "pass when actually prioritized — this issue is "
                    "scope-capture, not a spec.")
                entries = fpt.classify(set(), self.FORGE_META, [issue],
                                       hrse_reference_sources=[issue])
                row = next(e for e in entries if e.issue == number)
                self.assertEqual(row.hint, "scope-capture-not-a-spec")
                self.assertNotEqual(row.group, pr.GROUP_NOT_READY)

    def test_ac3_design_gated_hint_hrse1025_never_demotes_unlabelled(self):
        issue = self._hrse(
            1025, "Cy: offer to extract commitments",
            "Materialises Task nodes via opportunity_task_queries.\n\n"
            "**Filed pending a robust design — a `product-strategy` pass "
            "must run before any Lane 1 handoff.**")
        entries = fpt.classify(set(), self.FORGE_META, [issue],
                               hrse_reference_sources=[issue])
        row = next(e for e in entries if e.issue == 1025)
        self.assertEqual(row.hint, "design-gated-before-handoff")
        self.assertNotEqual(row.group, pr.GROUP_NOT_READY)

    def test_ac2_the_not_ready_label_demotes_through_classify(self):
        issue = self._hrse(
            500, "Some capability", "profile_service change.",
            labels=("feature", pr.NOT_READY_LABEL))
        entries = fpt.classify(set(), self.FORGE_META, [issue],
                               hrse_reference_sources=[issue])
        row = next(e for e in entries if e.issue == 500)
        self.assertEqual(row.group, pr.GROUP_NOT_READY)

    def test_ac1_forge_dependency_blocker_is_demoted_too(self):
        """The machinery is shared with forge rows, not hrse-only."""
        forge_issues = [{
            "number": 97, "title": "OAuth follow-up",
            "body": "Reviews the SCOPES granted.\n\n"
                    "## Dependencies\n\n- **forge#96** — must land first.",
            "labels": [{"name": "bug"}],
        }, {
            "number": 96, "title": "OAuth scopes", "body": "SCOPES review.",
            "labels": [{"name": "bug"}],
        }]
        entries = fpt.classify({96, 97}, self.FORGE_META, [],
                               forge_issues=forge_issues,
                               hrse_reference_sources=[])
        row = next(e for e in entries if e.repo == "forge" and e.issue == 97)
        self.assertEqual(row.group, pr.GROUP_NOT_READY)
        self.assertIn("forge#96", row.group_why)

    def test_a_row_with_neither_hint_nor_demotion_carries_no_hint(self):
        issue = self._hrse(600, "Ordinary feature", "profile_service change.")
        entries = fpt.classify(set(), self.FORGE_META, [issue],
                               hrse_reference_sources=[issue])
        row = next(e for e in entries if e.issue == 600)
        self.assertIsNone(row.hint)
        self.assertEqual(row.group, "1")

    def test_inferred_dependency_language_hints_but_never_demotes(self):
        """preclose finding: `INFERRED_DEP`'s phrase guesses (`requires`,
        `gated on`) must not authoritatively demote -- only a `linked`
        declaration (`blocked on`/`depends on`, or a `## Dependencies`
        heading's bulleted ref) may."""
        blocker = self._hrse(1300, "Some open issue", "profile_service change.")
        issue = self._hrse(
            601, "Ordinary feature",
            "Requires no schema change. Context in #1300. "
            "opportunity_service touches this too.")
        entries = fpt.classify(set(), self.FORGE_META, [blocker, issue],
                               hrse_reference_sources=[blocker, issue])
        row = next(e for e in entries if e.issue == 601)
        self.assertNotEqual(row.group, pr.GROUP_NOT_READY)

    def test_ratchet_integrity_a_pre_unclassified_epic_is_not_moved_out_of_it(self):
        """preclose finding: demoting a group-1..4 issue to NOT_READY must
        never shrink `check_ratchet`'s UNCLASSIFIED numerator for an issue
        that was ALREADY UNCLASSIFIED before demotion -- that would loosen
        the fail-if-worse guard with no real classification improvement."""
        epic = self._hrse(
            900, "EPIC: names no code surface at all", "No stage keyword here.",
            labels=("feature", "epic"))
        entries = fpt.classify(set(), self.FORGE_META, [epic],
                               hrse_reference_sources=[epic])
        row = next(e for e in entries if e.issue == 900)
        self.assertEqual(row.group, pr.GROUP_UNCLASSIFIED)

    def test_dependencies_section_stops_at_a_subheading(self):
        """preclose finding: a `### Notes` subheading's own bullets must
        not be read as Dependencies-section blockers."""
        blocker = self._hrse(99, "Unrelated prior art", "profile_service change.")
        issue = self._hrse(
            602, "Feature with a real and a fake dep",
            "opportunity_service change.\n\n"
            "## Dependencies\n\n- **#7 (nonexistent, irrelevant)**\n\n"
            "### Notes\n\n- **#99** — related prior art, nothing to wait on.")
        entries = fpt.classify(set(), self.FORGE_META, [blocker, issue],
                               hrse_reference_sources=[blocker, issue])
        row = next(e for e in entries if e.issue == 602)
        self.assertNotEqual(row.group, pr.GROUP_NOT_READY)

    def test_dependencies_section_ignores_a_quoted_code_fence_example(self):
        """preclose finding: a `## Dependencies` example inside a fenced
        code block must not be read as a real declaration."""
        blocker = self._hrse(42, "Unrelated", "profile_service change.")
        issue = self._hrse(
            603, "Feature quoting an example",
            "opportunity_service change. Example:\n"
            "```\n## Dependencies\n\n- **#42** — fake, inside a fence.\n```\n"
            "No real deps here.")
        entries = fpt.classify(set(), self.FORGE_META, [blocker, issue],
                               hrse_reference_sources=[blocker, issue])
        row = next(e for e in entries if e.issue == 603)
        self.assertNotEqual(row.group, pr.GROUP_NOT_READY)

    def test_render_summary_section_shows_the_demotion_cause_on_the_page(self):
        """preclose finding: AC5's cause reached `render()` (the terminal)
        but not `render_summary_section()` (the actual sprint-summary page)
        -- this is the page-facing regression test for that gap."""
        blocker = self._hrse(192, "Blocker", "profile_service change.")
        blocked = self._hrse(
            199, "Google Tasks sync",
            "opportunity_service change.\n\n"
            "## Dependencies\n\n- **#192** — it cannot ship before them.")
        entries = fpt.classify(set(), self.FORGE_META, [blocker, blocked],
                               hrse_reference_sources=[blocker, blocked])
        lines = fpt.render_summary_section(entries, "2.9")
        row = next(l for l in lines if l.startswith("- hrse#199"))
        self.assertIn("blocked on open hrse#192", row)

    def test_render_summary_section_shows_the_hint_on_the_page(self):
        """Same gap, for AC3's hint rather than AC5's demotion cause."""
        issue = self._hrse(
            303, "Cy: scheduled self-checking",
            "opportunity_service change.\n\n"
            "Pick up via a `product-strategy` pass when actually "
            "prioritized — this issue is scope-capture, not a spec.")
        entries = fpt.classify(set(), self.FORGE_META, [issue],
                               hrse_reference_sources=[issue])
        lines = fpt.render_summary_section(entries, "2.9")
        row = next(l for l in lines if l.startswith("- hrse#303"))
        self.assertIn("hint: scope-capture-not-a-spec", row)


class RankerAndSummaryAgreeTests(__import__("unittest").TestCase):
    """hrse#1631 AC3: `pipeline_rank.rank()` and the group the summary
    actually renders must agree for the same issue.

    The invariant is stated ONCE here rather than per-issue, because the
    defect it guards was never about a particular issue: `fetch_open_issues()`
    projected the REST payload down to `{number, title, body}` and dropped
    `labels`, so `classify()` ranked every forge row against an empty label
    set while a direct `rank()` call saw the real ones. Any forge issue whose
    group turns on a label diverged; `harmonic-forge#97` (`pipeline-clock`,
    Group 2 by ranker, UNCLASSIFIED by summary) is simply the one that was
    caught, mid-sequencing-decision.
    """

    def test_fetch_open_issues_carries_labels_through(self):
        """The projection itself, pinned. This is the single line whose
        omission caused the divergence -- assert the key survives, in the
        `[{name}]` shape `_labels()` reads."""
        raw = _page([{"number": 97, "title": "t", "body": "b",
                      "labels": [{"name": "infrastructure"},
                                 {"name": "pipeline-clock"}]}])
        with patch.object(fpt, "_run", return_value=raw):
            issues = fpt.fetch_open_issues("vitalharmony/harmonic-forge")
        self.assertEqual(fpt._labels(issues[0]), {"infrastructure", "pipeline-clock"})

    def test_an_unlabelled_issue_still_yields_an_empty_set_not_a_crash(self):
        """The absent-key case must stay safe: `_labels()` treats a missing
        key and an empty list alike, and the projection must not turn an
        unlabelled issue into a KeyError."""
        raw = _page([{"number": 1, "title": "t", "body": "b"}])
        with patch.object(fpt, "_run", return_value=raw):
            issues = fpt.fetch_open_issues("vitalharmony/harmonic-forge")
        self.assertEqual(fpt._labels(issues[0]), set())

    def test_the_only_legitimate_divergence_is_the_demotion_pass(self):
        """The invariant, stated as what the code actually holds.

        **An earlier draft asserted `classify()`'s group == `rank()`'s group
        outright, and that is false by design.** `classify()` runs
        `_demote_if_not_ready()` AFTER `rank()`, which deliberately overrides
        the text rank using cross-issue state `rank()` cannot see (an `epic`
        label, a `not-ready` label, an open dependency). Measured against the
        live forge repo, five open issues diverge for exactly that reason —
        so the flat-equality version would have gone red on correct
        behaviour the first time anyone extended it with an epic case.

        What must hold, and what this asserts: the two agree UNLESS the
        demotion pass fired, and when it fired the row carries the reason.
        That is the property whose violation was the actual defect — a row
        diverging with no demotion and no stated cause.

        Parametrised over cases whose bodies rank 1-4, since
        `_demote_if_not_ready` returns early on anything else and a case that
        never reaches the labels proves nothing about them (two subtests in
        the earlier draft were vacuous for exactly that reason: they passed
        unchanged with the defect reintroduced).
        """
        # `epic` ALONE never reaches the demotion pass: a body only ranks
        # 1-4 when a capability label is present too, so the epic case needs
        # `feature` alongside it. `not-ready` is deliberately absent from the
        # demoting cases -- `rank()` returns NOT_READY for it directly, so
        # `classify()` agrees with it and no divergence exists to assert.
        cases = [
            # (title, body, labels, demoted_to) -- `demoted_to` is the group
            # the override must produce, not merely "something different".
            # Asserting only inequality passed vacuously when the projection
            # defect was reintroduced: the defect creates its OWN divergence
            # (both sides lose the labels, so `classify()` says UNCLASSIFIED
            # while a direct `rank()` with real labels says 1), which
            # satisfied a `!=` check for entirely the wrong reason.
            ("OAuth verification demo video", "opportunity_service change.",
             [{"name": "infrastructure"}, {"name": "pipeline-clock"}], None),
            ("An epic that would otherwise rank 1", "opportunity_service change.",
             [{"name": "epic"}, {"name": "feature"}], "NOT_READY"),
            ("Not ready, demoted by rank() itself", "opportunity_service change.",
             [{"name": "not-ready"}, {"name": "feature"}], None),
            ("Plain capability", "opportunity_service change.",
             [{"name": "feature"}], None),
        ]
        for title, body, labels, demoted_to in cases:
            with self.subTest(title=title):
                raw = _page([{"number": 97, "title": title, "body": body,
                              "labels": labels}])
                with patch.object(fpt, "_run", return_value=raw):
                    fetched = fpt.fetch_open_issues("vitalharmony/harmonic-forge")
                direct = pr.rank(title, body, {l["name"] for l in labels})
                entries = fpt.classify(
                    {97}, {97: {"theme": "Ops", "venture": "CymaGraph", "title": title}},
                    [], forge_issues=fetched)
                entry = next(e for e in entries if e.repo == "forge" and e.issue == 97)
                if demoted_to:
                    # The override fired: the group is the SPECIFIC one the
                    # demotion produces, it genuinely differs from the text
                    # rank, and it carries its cause -- without which it is
                    # indistinguishable from the silent divergence this issue
                    # was filed about (AC4).
                    self.assertEqual(
                        entry.group, demoted_to,
                        f"expected the demotion pass to produce {demoted_to!r}")
                    self.assertNotEqual(entry.group, direct.group)
                    self.assertIn(
                        "epic", entry.group_why,
                        "a demoted row must state why, per AC4")
                else:
                    self.assertEqual(
                        entry.group, direct.group,
                        f"classify() said {entry.group!r} ({entry.group_why}); "
                        f"rank() said {direct.group!r} ({direct.why}) — and no "
                        "demotion applies, so they must agree")

    def test_every_case_here_actually_exercises_the_labels(self):
        """Guards the parametrisation above against going vacuous.

        Each case's body must rank 1-4, because `_demote_if_not_ready`
        returns early otherwise and the labels are never consulted on either
        side. This is the check that would have caught the earlier draft's
        two dead subtests at authoring time rather than at preclose.
        """
        for title, labels in (
                ("An epic that would otherwise rank 1", {"epic", "feature"}),
                ("Plain capability", {"feature"}),
                ("OAuth verification demo video", {"infrastructure", "pipeline-clock"})):
            with self.subTest(title=title):
                # Ranked WITH the case's own labels, because the labels are
                # what lift a body to 1-4 in the first place -- an earlier
                # version of this guard ranked with an empty set and so
                # asserted a premise none of the cases could satisfy.
                self.assertIn(
                    pr.rank(title, "opportunity_service change.", labels).group,
                    ("1", "2", "3", "4"),
                    "must rank 1-4 or `_demote_if_not_ready` returns early "
                    "and the labels are never consulted")

    def test_the_pipeline_clock_label_reaches_group_two_end_to_end(self):
        """AC2's shape, as a unit test: `pipeline-clock` on a forge issue
        must produce a rendered `Group 2` row, not merely a Group-2 ranking.
        Also routed through `fetch_open_issues()`, for the reason above --
        this is the full path the live summary takes."""
        raw = _page([{"number": 97, "title": "OAuth verification demo video",
                      "body": "provision tenant, record, submit",
                      "labels": [{"name": "infrastructure"},
                                 {"name": "pipeline-clock"}]}])
        with patch.object(fpt, "_run", return_value=raw):
            fetched = fpt.fetch_open_issues("vitalharmony/harmonic-forge")
        entries = fpt.classify(
            {97}, {97: {"theme": "Ops", "venture": "CymaGraph",
                        "title": "OAuth verification demo video"}},
            [], forge_issues=fetched)
        self.assertEqual(
            next(e for e in entries if e.issue == 97).group, "2")
        lines = fpt.render_summary_section(entries, "2.9")
        self.assertTrue(
            any("Group 2" in line for line in lines),
            "no Group 2 heading rendered for a pipeline-clock issue")
        self.assertTrue(
            any(line.startswith("- forge#97") for line in lines),
            "forge#97 absent from the rendered section entirely")
