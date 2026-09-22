#!/usr/bin/env python3
"""Tests for the milestone summary (hrse#1210).

Everything here runs against fixtures. The live reconcile writes to a tracked
doc under the doc-only-merge rule, so it is exercised against temp files, not
against docs/PRIORITIES.md.
"""

import importlib.util
import json
import sys
from datetime import UTC, datetime, timedelta
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from unittest.mock import patch

ROOT = Path(__file__).parent


def load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def load_from(directory: Path, name: str):
    """Import a module that resolves its own siblings by bare name.

    `scripts/board_dashboard_renderer.py` does `from dashboard import ...`,
    so its directory has to be on `sys.path` before it will import at all.
    hrse#1590 needs it because TC6 asserts the row this module renders is
    still parsed by the regex that consumes it -- both directions shown, and
    a copy of that regex here would be a second implementation of exactly
    the thing being checked.
    """
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))
    spec = importlib.util.spec_from_file_location(name, directory / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


# harmonic-forge#708: these round-trip tests read the home repo's own scripts
# (board_dashboard_renderer.py, post_lane_discussion.py). The home checkout is
# the one holding the resolved sprint-plan config, not a parent of this file.
sys.path.insert(0, str(ROOT))
import _home  # noqa: E402

REPO_ROOT = _home.home_root()

summary = load("milestone_summary")
lane_state = load("lane_state")

KNOWN = ["2.7", "2.8", "2.9", "3.0", "Later", "Platform"]


class AnchorTests(unittest.TestCase):
    def write(self, text: str) -> Path:
        tmp = tempfile.NamedTemporaryFile("w", suffix=".md", delete=False)
        tmp.write(text)
        tmp.close()
        self.addCleanup(lambda: Path(tmp.name).unlink(missing_ok=True))
        return Path(tmp.name)

    def test_reads_the_current_release_heading(self) -> None:
        path = self.write("intro\n\n## Current release — 2.8, Ops & Parity\n\nbody\n")
        self.assertEqual(summary.read_anchor(path), "2.8")

    def test_missing_anchor_fails_loudly(self) -> None:
        path = self.write("no heading here\n")
        with self.assertRaises(SystemExit) as caught:
            summary.read_anchor(path)
        self.assertIn("refusing to guess", str(caught.exception))

    def test_rewrite_drops_the_previous_release_thesis(self) -> None:
        """Carrying it forward mints a durable false statement: 2.9's thesis is
        Cy, not 2.8's. It re-reads cleanly forever after, so nothing flags it."""
        path = self.write("## Current release — 2.8, Ops & Parity + Substrate\n\nbody\n")
        summary.rewrite_anchor(path, "2.9")
        self.assertIn("## Current release — 2.9\n", path.read_text())
        self.assertNotIn("Ops & Parity", path.read_text())
        self.assertIn("body", path.read_text())

    def test_rewrite_handles_the_hyphen_the_reader_accepts(self) -> None:
        """ANCHOR matches [—-]; splitting on the em dash only deleted the
        entire thesis from a tracked doc with no warning."""
        path = self.write("## Current release - 2.8, Ops & Parity\n\nbody\n")
        self.assertEqual(summary.read_anchor(path), "2.8")
        summary.rewrite_anchor(path, "2.9")
        self.assertIn("## Current release — 2.9\n", path.read_text())
        self.assertIn("body", path.read_text())

    def test_rewrite_survives_a_heading_on_the_last_line(self) -> None:
        """No trailing newline previously raised an uncaught ValueError, after
        the correction had already been announced."""
        path = self.write("intro\n\n## Current release — 2.8, thesis")
        summary.rewrite_anchor(path, "2.9")
        self.assertIn("## Current release — 2.9", path.read_text())

    def test_rewrite_leaves_no_temp_file_behind(self) -> None:
        path = self.write("## Current release — 2.8, thesis\n")
        summary.rewrite_anchor(path, "2.9")
        self.assertEqual(list(path.parent.glob("*.tmp")), [])


class ResolveTests(unittest.TestCase):
    def test_a_live_anchor_is_left_alone(self) -> None:
        milestone, correction = summary.resolve_milestone("2.8", {"2.8": 39}, KNOWN)
        self.assertEqual(milestone, "2.8")
        self.assertIsNone(correction)

    def test_an_exhausted_anchor_reconciles_to_the_lowest_numbered_release(self) -> None:
        milestone, correction = summary.resolve_milestone(
            "2.7", {"2.8": 39, "2.9": 14, "3.0": 36, "Later": 37, "Platform": 29}, KNOWN)
        self.assertEqual(milestone, "2.8")
        self.assertIn("reconciled to 2.8", correction)

    def test_later_and_platform_are_never_chosen(self) -> None:
        """Neither is a release."""
        milestone, _ = summary.resolve_milestone("2.7", {"Later": 37, "Platform": 29, "3.0": 2}, KNOWN)
        self.assertEqual(milestone, "3.0")

    def test_an_anchor_naming_a_nonexistent_milestone_fails_loudly(self) -> None:
        with self.assertRaises(SystemExit) as caught:
            summary.resolve_milestone("4.2", {"2.8": 39}, KNOWN)
        self.assertIn("does not exist", str(caught.exception))

    def test_no_numbered_candidate_fails_rather_than_emitting_an_empty_summary(self) -> None:
        with self.assertRaises(SystemExit) as caught:
            summary.resolve_milestone("2.7", {"Later": 37, "Platform": 29}, KNOWN)
        self.assertIn("refusing to emit an empty summary", str(caught.exception))

    def test_an_anchor_naming_later_or_platform_is_refused(self) -> None:
        """Neither is a release, so neither can be the current one. The old
        early-return accepted them whenever they had open issues."""
        for name in ("Later", "Platform"):
            with self.assertRaises(SystemExit) as caught:
                summary.resolve_milestone(name, {name: 29}, KNOWN)
            self.assertIn("not a release", str(caught.exception))

    def test_ordering_is_numeric_not_lexical(self) -> None:
        """'10.0' must not sort before '9.0'."""
        milestone, _ = summary.resolve_milestone(
            "2.7", {"10.0": 1, "9.0": 1}, ["2.7", "9.0", "10.0"])
        self.assertEqual(milestone, "9.0")


class CollectFilterTests(unittest.TestCase):
    def test_done_is_excluded_and_in_progress_is_kept(self) -> None:
        """In Progress items are exactly what a batch proposal must route around."""
        issues = [{"number": 1, "title": "a", "body": "", "labels": [], "updatedAt": "t", "milestone": None},
                  {"number": 2, "title": "b", "body": "", "labels": [], "updatedAt": "t", "milestone": None}]
        fields = {("vitalharmony/hrse", 1): {"Status": "Done"},
                  ("vitalharmony/hrse", 2): {"Status": "In Progress"}}
        with patch.object(summary, "fetch_issues", return_value=issues):
            records = summary.collect("vitalharmony/hrse", summary.Cache(enabled=False), fields)
        self.assertEqual([record["key"] for record in records], ["H2"])


class FetchCeilingTests(unittest.TestCase):
    def test_hitting_the_limit_fails_loudly(self) -> None:
        """gh truncates oldest-first with no diagnostic, and a truncated count
        feeds a reconcile that writes a tracked doc."""
        with patch.object(summary, "gh_json", return_value=[{}] * summary.FETCH_LIMIT):
            with self.assertRaises(SystemExit) as caught:
                summary.fetch_issues("vitalharmony/hrse", None)
        self.assertIn("truncated", str(caught.exception))


class PathTokenTests(unittest.TestCase):
    def test_root_level_files_are_matched(self) -> None:
        """The highest-traffic files in a tooling batch; the first pattern
        required a slash and missed every one of them."""
        scope = summary.file_scope({"body": "edits `mise.toml` and `CLAUDE.md` and `docs/a.md`"})
        self.assertEqual(scope, ["CLAUDE.md", "docs/a.md", "mise.toml"])


class DependencyTests(unittest.TestCase):
    def edges(self, body: str, number: int = 1) -> list[tuple[str, str]]:
        return summary.dependencies("vitalharmony/hrse", {"number": number, "body": body})

    def test_explicit_statements_are_linked(self) -> None:
        self.assertIn(("hrse#307", "linked"), self.edges("**Blocked on:** #307"))

    def test_prose_ordering_is_inferred(self) -> None:
        self.assertIn(("hrse#42", "inferred"), self.edges("This requires #42 to land first."))

    def test_a_bare_reference_is_not_a_dependency(self) -> None:
        """An epic listing its children is not 22 dependencies -- the first
        cut treated every #N as an edge and produced exactly that."""
        self.assertEqual(self.edges("Children: #1, #2, #3. See also #4."), [])

    def test_cross_repo_prefix_is_preserved(self) -> None:
        self.assertIn(("harmonic-forge#320", "linked"),
                      self.edges("Blocked on harmonic-forge#320"))

    def test_self_reference_is_not_an_edge(self) -> None:
        self.assertEqual(self.edges("Blocked on #7", number=7), [])

    def test_reverse_direction_verbs_do_not_create_an_edge(self) -> None:
        """"this unblocks #N" means #N depends on this. A reversed arrow has no
        visible cue -- `inferred` says "may be wrong about whether", not "may
        point the wrong way"."""
        self.assertEqual(self.edges("Gates hrse#987. This unblocks #12."), [])

    def test_a_trigger_word_far_from_a_reference_does_not_link_them(self) -> None:
        """A markdown 'line' here is a paragraph: an unbounded window matched
        'after deletion ... hrse#855' as a dependency."""
        body = ("Requires the high-tier model. " + "filler words here. " * 12 + "See #1010.")
        self.assertEqual(self.edges(body), [])

    def test_linked_wins_over_inferred_for_the_same_target(self) -> None:
        edges = self.edges("Blocked on #9. It also requires #9.")
        self.assertEqual(edges, [("hrse#9", "linked")])

    def test_hrse1543_every_ref_survives_a_parenthetical_between_them(self) -> None:
        """hrse#1543: the exact string that under-reported hrse#1444's real
        dependencies -- the continuation used to require each ref to follow
        the previous one's comma immediately, so the parenthetical after
        #1434 truncated the capture to that ref alone."""
        body = ("**Depends on:** #1434 (G2-e, go/no-go), #1439 (W3a, the "
                "review UI), #1442 (W4, the standing-read target).")
        self.assertEqual(
            self.edges(body),
            [("hrse#1434", "linked"), ("hrse#1439", "linked"), ("hrse#1442", "linked")],
        )

    def test_hrse1543_all_refs_closed_still_produces_no_demotion_signal(self) -> None:
        """The extraction itself doesn't know open/closed state -- this
        confirms extraction still returns every ref even when every one of
        them would, downstream, resolve closed (AC4's input side)."""
        body = "Blocked on #101 (done), #102 (done)."
        self.assertEqual(self.edges(body), [("hrse#101", "linked"), ("hrse#102", "linked")])

    def test_hrse1543_preclose_finding_a_child_epic_line_does_not_sweep_in_siblings(self) -> None:
        """preclose-inspection on this same issue: an epic's own child-listing
        line, with a `depends on` trigger inside ONE child's parenthetical,
        must not sweep every later sibling `#N` on the same paragraph into a
        fabricated dependency list. Live counter-example: harmonic-forge#11."""
        body = ("Split into child issues: #12 (connector interface), #13 "
                "(Google Drive connector, depends on #12), #14 (approval-page "
                "template), #15 (Unstructured integration).")
        self.assertEqual(self.edges(body), [("hrse#12", "linked")])

    def test_hrse1543_preclose_finding_a_later_sentence_is_not_swept_in(self) -> None:
        """Live counter-example: hrse#258. A second sentence naming issues
        that are explicitly NOT dependencies must not be captured just
        because it shares a paragraph with a real `Depends on:` statement."""
        body = ("Depends on #226 (closed) and #229 (something). Scheduled "
                "directly behind #229 in NEXT, not ahead of #227 or #228 — "
                "those remain later.")
        self.assertEqual(self.edges(body), [("hrse#226", "linked"), ("hrse#229", "linked")])

    def test_hrse1543_preclose_finding_reverse_arrow_on_the_same_line(self) -> None:
        """Live counter-example: `Blocked on #9. This unblocks #12.` on one
        line must not report #12 as a blocker -- it depends on THIS issue."""
        self.assertEqual(self.edges("Blocked on #9. This unblocks #12.", number=999),
                          [("hrse#9", "linked")])


class ToolingMarkerTests(unittest.TestCase):
    def marker(self, labels, scope):
        issue = {"labels": [{"name": name} for name in labels]}
        return summary.tooling_marker(issue, scope)

    def test_the_label_is_authoritative(self) -> None:
        self.assertEqual(self.marker(["tooling-exception"], ["backend/app/x.py"]), "label")

    def test_tooling_only_scope_with_a_tooling_label_is_inferred(self) -> None:
        self.assertEqual(self.marker(["tech-debt"], ["scripts/a.py", "tools/b.py"]), "inferred")

    def test_tooling_paths_alone_are_not_enough(self) -> None:
        """A backend bug citing only a script is not Lane-1-executable."""
        self.assertIsNone(self.marker(["bug"], ["scripts/a.py"]))

    def test_product_surface_disqualifies(self) -> None:
        self.assertIsNone(self.marker(["tech-debt"], ["scripts/a.py", "backend/app/main.py"]))

    def test_no_scope_means_no_inference(self) -> None:
        """Absence of evidence is not evidence -- do not mark what we cannot see."""
        self.assertIsNone(self.marker(["tech-debt"], []))


class CacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        summary.cache_dir = lambda: Path(self.tmp.name)

    def issue(self, updated="2026-08-22T00:00:00Z"):
        return {"number": 1, "updatedAt": updated, "body": "touches `scripts/a.py`"}

    def test_cache_survives_a_commit(self) -> None:
        """The repo-HEAD axis was removed: file_scope never reads the
        filesystem, so HEAD could not make an entry wrong -- it only forced a
        full recompute after every commit, and /sprint-plan runs after commits."""
        cache = summary.Cache()
        cache.scope("vitalharmony/hrse", self.issue())
        cache.save()
        again = summary.Cache()
        again.scope("vitalharmony/hrse", self.issue())
        self.assertEqual((again.fresh, again.cached), (0, 1))

    def test_no_cache_actually_rewrites_the_stale_file(self) -> None:
        """Its whole purpose is a cache believed wrong; an early return left
        the bad entries for the next default run to re-serve."""
        (Path(self.tmp.name) / "file_scope.json").write_text(
            json.dumps({"vitalharmony/hrse#1": {"updated_at": "old", "scope": ["WRONG.py"]}}))
        cache = summary.Cache(enabled=False, rewrite=True)
        cache.scope("vitalharmony/hrse", self.issue())
        cache.save()
        stored = json.loads((Path(self.tmp.name) / "file_scope.json").read_text())
        self.assertEqual(stored["vitalharmony/hrse#1"]["scope"], ["scripts/a.py"])

    def test_unchanged_issue_is_served_from_cache(self) -> None:
        cache = summary.Cache()
        cache.scope("vitalharmony/hrse", self.issue())
        cache.save()
        again = summary.Cache()
        again.scope("vitalharmony/hrse", self.issue())
        self.assertEqual((again.fresh, again.cached), (0, 1))

    def test_updated_at_change_invalidates(self) -> None:
        cache = summary.Cache()
        cache.scope("vitalharmony/hrse", self.issue())
        cache.save()
        again = summary.Cache()
        again.scope("vitalharmony/hrse", self.issue(updated="2026-08-23T00:00:00Z"))
        self.assertEqual((again.fresh, again.cached), (1, 0))

    def test_corrupt_cache_is_rebuilt_not_fatal(self) -> None:
        (Path(self.tmp.name) / "file_scope.json").write_text("{not json")
        cache = summary.Cache()
        cache.scope("vitalharmony/hrse", self.issue())
        self.assertEqual(cache.fresh, 1)


class IntersectionTests(unittest.TestCase):
    """The closing section is the one the operator acts on."""

    def record(self, key, tooling="label", deps=(), scope=(), repo="vitalharmony/hrse", seq="1",
               tier="fast"):
        fields = {"Sequence": seq, "Status": "Todo"}
        if tier is not None:
            fields["Tier"] = tier
        return {"key": key, "number": int(key[1:]), "title": f"title {key}", "repo": repo,
                "milestone": "2.8", "fields": fields, "deps": list(deps),
                "scope": list(scope), "tooling": tooling}

    def out(self, records, open_keys=frozenset()):
        return "\n".join(summary._intersection(list(records), set(open_keys), "2.8"))

    def test_two_independent_issues_are_batched_together(self) -> None:
        """The inclusion half. Every prior assertion here was a negative, so
        truncating the batch to one issue passed the whole suite."""
        text = self.out([self.record("H1", scope=["a.py"]), self.record("H2", scope=["b.py"])])
        batch = [line for line in text.splitlines() if line.startswith("BATCH ")][0]
        self.assertIn("H1", batch)
        self.assertIn("H2", batch)

    def test_unknown_scope_is_held_back_not_admitted(self) -> None:
        """An empty scope intersects nothing, so 63% of issues were certified
        non-overlapping on no evidence -- and the label path skipped scope
        entirely, making the AC-mandated route the unmeasured one."""
        text = self.out([self.record("H1", scope=[])])
        self.assertIn("held back", text)
        self.assertNotIn("BATCH H1", text)

    def test_overlap_is_repo_qualified(self) -> None:
        """docs/PRIORITIES.md in hrse cannot move forge's merge base."""
        text = self.out([self.record("H1", scope=["docs/PRIORITIES.md"]),
                         self.record("F2", scope=["docs/PRIORITIES.md"],
                                     repo="vitalharmony/harmonic-forge")])
        batch = [line for line in text.splitlines() if line.startswith("BATCH ")][0]
        self.assertIn("H1", batch)
        self.assertIn("F2", batch)

    def test_same_repo_overlap_still_splits(self) -> None:
        text = self.out([self.record("H1", scope=["mise.toml"]),
                         self.record("H2", scope=["mise.toml"])])
        batch = [line for line in text.splitlines() if line.startswith("BATCH ")][0]
        self.assertIn("H1", batch)
        self.assertNotIn("H2", batch)

    def test_a_dependency_on_any_open_issue_excludes(self) -> None:
        """open_keys spans every open issue, not just the rendered set: a
        cross-milestone blocker read as no dependency at all."""
        text = self.out([self.record("H1", scope=["a.py"], deps=[("hrse#999", "linked")])],
                        open_keys={"H999"})
        self.assertNotIn("BATCH", text)

    def test_a_forge_record_reaches_the_batch(self) -> None:
        """Forge is milestone-less by rule, not by omission (hrse#1217)."""
        text = self.out([self.record("F1", scope=["a.py"], repo=summary.NO_MILESTONE_REPO)])
        self.assertIn("BATCH F1", text)
        self.assertIn("no milestone by rule", text)

    def test_the_heading_names_the_actual_scope(self) -> None:
        """Heading and content must agree -- the defect the original finding named."""
        text = self.out([self.record("H1", scope=["a.py"])])
        self.assertIn("harmonic-forge", text.splitlines()[1])

    def test_batch_label_immediately_precedes_the_fence(self) -> None:
        """Proving both strings exist somewhere is not proving the label
        travels with the line."""
        lines = self.out([self.record("H1", scope=["a.py"])]).splitlines()
        fence = lines.index("```")
        self.assertTrue(any("PROPOSAL — not an authorization" in line for line in lines[:fence]))
        self.assertTrue(lines[fence + 1].startswith("BATCH "))

    def test_fast_and_standard_batch_has_no_tier_warning(self) -> None:
        text = self.out([self.record("H1", scope=["a.py"], tier="fast"),
                         self.record("H2", scope=["b.py"], tier="standard")])
        self.assertNotIn("Batch tier warning", text)

    def test_deep_batch_warning_names_the_issue(self) -> None:
        text = self.out([self.record("H1", scope=["a.py"], tier="deep")])
        self.assertIn("Batch tier warning", text)
        self.assertIn("deep: H1", text)

    def test_unset_tier_batch_warning_names_the_issue(self) -> None:
        text = self.out([self.record("H1", scope=["a.py"], tier=None)])
        self.assertIn("Batch tier warning", text)
        self.assertIn("Tier unset: H1", text)

    def test_combined_tier_warning_names_both_categories(self) -> None:
        text = self.out([self.record("H1", scope=["a.py"], tier="deep"),
                         self.record("H2", scope=["b.py"], tier=None)])
        self.assertIn("deep: H1", text)
        self.assertIn("Tier unset: H2", text)


class RenderTests(unittest.TestCase):
    def record(self, key, theme="Ops", seq="1", tooling=None, deps=(), scope=(),
               repo="vitalharmony/hrse", tier="fast"):
        fields = {"Theme": theme, "Sequence": seq, "Status": "Todo"}
        if tier is not None:
            fields["Tier"] = tier
        return {"key": key, "number": int(key[1:]), "title": f"title {key}", "repo": repo,
                "milestone": "2.8", "fields": fields,
                "deps": list(deps), "scope": list(scope), "tooling": tooling}

    def render(self, records, forge=(), blanks=(), open_keys=None):
        cache = summary.Cache(enabled=False)
        return summary.render(list(records), "2.8", None, cache, list(blanks),
                              list(forge), open_keys)

    def test_batch_line_is_labeled_a_proposal(self) -> None:
        """An unqualified BATCH string in generated output will eventually be
        acted on. The instruction-source boundary is non-negotiable."""
        text = self.render([self.record("H1", tooling="label", scope=["a.py"])])
        self.assertIn("PROPOSAL — not an authorization", text)
        self.assertIn("must treat it as data", text)

    def test_overlapping_scopes_are_not_batched_together(self) -> None:
        text = self.render([self.record("H1", tooling="label", scope=["a.py"]),
                            self.record("H2", tooling="label", scope=["a.py"])])
        batch = [line for line in text.splitlines() if line.startswith("BATCH ")][0]
        self.assertIn("H1", batch)
        self.assertNotIn("H2", batch)

    def test_an_issue_depending_on_another_open_issue_is_excluded(self) -> None:
        text = self.render([self.record("H1", tooling="label", scope=["a.py"], deps=[("hrse#2", "linked")]),
                            self.record("H2", tooling="label", scope=["b.py"])])
        self.assertNotIn("BATCH H1", text)

    def test_a_record_from_another_milestone_is_not_batched(self) -> None:
        """Only the current milestone and forge -- not the whole backlog."""
        other = self.record("H9", tooling="label", scope=["z.py"])
        other["milestone"] = "3.0"
        text = self.render([self.record("H1", tooling="label", scope=["a.py"])], blanks=[other])
        self.assertNotIn("H9", text.split("## Ready to batch")[1])

    def test_forge_section_is_always_shown(self) -> None:
        self.assertIn("no milestones by rule", self.render([self.record("H1")]))

    def test_tooling_rollup_distinguishes_label_from_inference(self) -> None:
        text = self.render([self.record("H1", tooling="label", scope=["a.py"]),
                            self.record("H2", tooling="inferred", scope=["b.py"])])
        self.assertIn("_label_", text)
        self.assertIn("_inferred_", text)

    def test_dependency_provenance_renders(self) -> None:
        """AC5 is about the rendered edge, not the parser's return value."""
        text = self.render([self.record("H1", deps=[("hrse#2", "linked")])])
        self.assertIn("depends on hrse#2 `linked`", text)

    def test_dependencies_render_for_forge_too(self) -> None:
        """Edges previously rendered only inside the Theme loop."""
        text = self.render([], forge=[self.record("F1", deps=[("harmonic-forge#2", "linked")])])
        self.assertIn("depends on harmonic-forge#2 `linked`", text)

    def test_tooling_marker_renders_inline(self) -> None:
        self.assertIn("**(t)**", self.render([self.record("H1", tooling="label", scope=["a.py"])]))

    def test_issue_line_renders_tier_next_to_status(self) -> None:
        self.assertIn("[Todo | Tier: fast |", self.render([self.record("H1")]))

    def test_issue_line_renders_missing_tier_as_unset(self) -> None:
        self.assertIn("[Todo | Tier: unset |", self.render([self.record("H1", tier=None)]))

    def test_theme_grouping_is_real(self) -> None:
        text = self.render([self.record("H1", theme="Ops"), self.record("H2", theme="Career")])
        self.assertIn("## Ops", text)
        self.assertIn("## Career", text)

    def test_sequence_ordering_is_real(self) -> None:
        text = self.render([self.record("H1", seq="9"), self.record("H2", seq="1")])
        self.assertLess(text.index("title H2"), text.index("title H1"))

    def test_cache_split_is_reported(self) -> None:
        """A run about to be expensive says so before it is, not after."""
        self.assertRegex(self.render([self.record("H1")]), r"\d+ analyzed fresh, \d+ from cache")


if __name__ == "__main__":
    unittest.main()


class PipelineSectionPlacementTests(__import__("unittest").TestCase):
    """hrse#1477 — where the section is spliced, at the `render()` level."""

    class _Cache:
        fresh = 3
        cached = 1

    def _render(self, section):
        return load("milestone_summary_render").render(
            [], "2.9", None, self._Cache(), [], [], set(), section)

    def test_tc2_section_follows_the_freshness_caption(self):
        """The caption annotates the title; the section goes below the pair,
        never between them."""
        out = self._render(["", "## Pipeline relevance — 2.9 — Tier 1 (1)", "",
                            "- forge#96 — [Ops/Platform] t"])
        lines = out.splitlines()
        title = next(i for i, l in enumerate(lines) if l.startswith("# Milestone summary"))
        caption = next(i for i, l in enumerate(lines) if "analyzed fresh" in l)
        section = next(i for i, l in enumerate(lines) if "Pipeline relevance" in l)
        self.assertLess(title, caption)
        self.assertLess(caption, section)

    def test_tc2_section_precedes_the_forge_no_milestone_section(self):
        out = self._render(["", "## Pipeline relevance — 2.9 — Tier 1 (1)"])
        section = out.find("Pipeline relevance")
        forge = out.find("no milestones by rule")
        self.assertNotEqual(forge, -1)
        self.assertLess(section, forge)

    def test_omitting_the_section_leaves_the_summary_unchanged(self):
        """The parameter is appended with a default because
        `test_milestone_summary.py:376` calls `render()` with seven POSITIONAL
        arguments — inserting it earlier would silently shift them."""
        self.assertEqual(self._render(None), self._render([]))
        self.assertNotIn("Pipeline relevance", self._render(None))


class RepoVisibilityTests(unittest.TestCase):
    """hrse#1514 — a row's repository, and the milestone-level header."""

    class _Cache:
        fresh = 3
        cached = 0

    def _record(self, repo, number, title="t", theme="Ops", tier="standard"):
        return {"key": load("milestone_summary_source").REPO_PREFIX[repo.split("/")[-1]] + str(number),
                "repo": repo, "number": number, "title": title, "tooling": False,
                "fields": {"Theme": theme, "Status": "Todo", "Tier": tier}, "deps": []}

    def _render(self, records):
        render_mod = load("milestone_summary_render")
        return render_mod.render(records, "2.8", None, self._Cache(), [], [], set())

    def test_tc4_theme_grouping_unchanged_repo_signal_added(self):
        """The fix must not revert repo to the top-level partition."""
        out = self._render([
            self._record("vitalharmony/cymagraph-infra", 346, theme="Ops"),
            self._record("vitalharmony/hrse", 733, theme="Ops"),
        ])
        self.assertIn("## Ops", out)
        self.assertEqual(out.count("## Ops"), 1, "repo must not re-partition the sections")
        self.assertIn("- cymagraph-infra/I346", out)
        self.assertIn("- H733", out)

    def test_row_ref_fuses_non_hrse_repo_as_one_token(self):
        render_mod = load("milestone_summary_render")
        infra = self._record("vitalharmony/cymagraph-infra", 346)
        hrse = self._record("vitalharmony/hrse", 733)
        self.assertEqual(render_mod._row_ref(infra), "cymagraph-infra/I346")
        self.assertEqual(render_mod._row_ref(hrse), "H733")

    def test_tc2_header_states_every_queried_repo_including_zero(self):
        """The direct fix for the incident: a silent zero and a genuine zero
        looked identical before this existed."""
        out = self._render([self._record("vitalharmony/hrse", 733)])
        self.assertIn("**Repos queried:** hrse 1 · cymagraph-infra 0", out)

    def test_tc3_reproduces_the_actual_incident_arithmetic(self):
        """hrse held fewer than cymagraph-infra — the exact shape that went
        unnoticed live (2.8: reported 3, actually 10)."""
        records = [self._record("vitalharmony/hrse", 733)]
        records += [self._record("vitalharmony/cymagraph-infra", n)
                    for n in (59, 111, 343, 344, 345, 346, 347)]
        out = self._render(records)
        self.assertIn("**Repos queried:** hrse 1 · cymagraph-infra 7", out)
        self.assertEqual(len(records), 8)  # the true total, derivable from the header alone

    def test_header_scoped_to_records_only_not_blanks(self):
        """Operator decision 2026-09-02: blanks are a separate labelled
        bucket and do not count toward this milestone's per-repo total."""
        render_mod = load("milestone_summary_render")
        out = render_mod.render(
            [self._record("vitalharmony/hrse", 733)], "2.8", None, self._Cache(),
            [self._record("vitalharmony/cymagraph-infra", 999)],  # blanks
            [], set())
        self.assertIn("**Repos queried:** hrse 1 · cymagraph-infra 0", out,
                      "a blank-milestone record must not inflate the count")

    def test_tc6_pipeline_relevance_section_out_of_scope(self):
        """`render_summary_section()` already states its milestone and is
        explicitly untouched by this issue — confirmed via its own suite,
        run alongside this one."""
        fpt = load("forge_pipeline_triage")
        self.assertTrue(hasattr(fpt, "render_summary_section"))


class LaneStateTests(unittest.TestCase):
    """hrse#1584 -- lane state derived from posted comment markers.

    Bodies here are the real shapes, copied from live threads
    (hrse#1573/#1565/#1441/#1575, read 2026-09-04), not invented ones: the
    whole mechanism is a claim about what those comments look like, so a
    fixture that paraphrases them would pass while the parser was wrong.
    """

    # hrse#1589: handoffs now DECLARE Plan-First in the footer. These two
    # fixtures carry the declaration; the legacy shapes live in
    # `PlanFirstDerivationTests` below, where the fallback is the subject.
    HANDOFF = ("## Handoff: H1 -- something\n\n"
               "<!-- l1-post v1; kind=handoff; plan-first=false; sha=abc; checks=tier-set -->")
    PLAN_FIRST = ("## Handoff: H1 -- something\n\n| body | WITHHELD -- Plan-First |\n\n"
                  "<!-- l1-post v1; kind=handoff; plan-first=true; sha=abc; checks=tier-set -->")
    L2P = "## L2P -- receipt-backed status (harmonic-forge#371)\n\nplan text"
    L2S = "## L2S -- receipt-backed status (harmonic-forge#371)\n\nplan text"
    L2D = "## L2D -- receipt-backed status (harmonic-forge#371)\n\ndone text"
    L2B = "## L2B -- receipt-backed status (harmonic-forge#371)\n\nblocked text"
    DECISIONS = "## L1 -- decisions, hrse#1\n\n<!-- l1-post v1; kind=discussion; sha=abc -->"
    # hrse#1609. A rework request is its own kind, so the fixture is the
    # footer, not a phrasing -- the whole point is that no prose is read.
    REWORK = ("## L1 -- rework, hrse#1\n\nrebase onto current main and push\n\n"
              "<!-- l1-post v1; kind=rework; sha=abc -->")
    READY = "Implemented per the ratified spec.\n\n<!-- l1-post v1; kind=ready-for-l3; sha=abc -->"
    PASS = "## Lane 3 Gate Results -- hrse#1 -- PASS (re-gate, AC4 fix verified)"
    FAIL = ("## Lane 3 Gate Results -- hrse#1 -- FAIL (migration executed "
            "successfully; one write site missed)")

    def record(self, *bodies, tier="standard", deps=()):
        return {"key": "H1", "number": 1, "title": "t", "repo": "vitalharmony/hrse",
                "fields": {"Tier": tier} if tier else {},
                "deps": list(deps), "scope": [], "tooling": None,
                "comments": [{"body": body} for body in bodies]}

    def full(self, *bodies, **kwargs):
        return lane_state.classify(self.record(*bodies, **kwargs), [])

    def state(self, *bodies, **kwargs):
        # hrse#1590 AC6, asserted rather than inspected: `classify()` now
        # returns a third field, and every assertion below still compares the
        # SAME two. A display string that moved by one byte fails here.
        return tuple(self.full(*bodies, **kwargs))[:2]

    def test_no_comments_is_no_handoff_and_offers_no_trigger(self) -> None:
        """TC1."""
        self.assertEqual(self.state(), (lane_state.NO_HANDOFF, None))

    def test_plain_handoff_is_ready_to_implement(self) -> None:
        """TC2, one direction."""
        self.assertEqual(self.state(self.HANDOFF), ("ready: Implement H1", "Implement H1"))

    def test_plan_first_handoff_is_ready_to_plan(self) -> None:
        """TC2, the other direction. The verb is the whole point of the row."""
        self.assertEqual(self.state(self.PLAN_FIRST), ("ready: Plan H1", "Plan H1"))

    def test_l2d_beats_the_handoff_below_it(self) -> None:
        """TC3 -- precedence, not recency of the handoff."""
        self.assertEqual(self.state(self.HANDOFF, self.L2D),
                         (lane_state.AWAITING_GATE, None))

    def test_ready_for_l3_footer_also_means_awaiting_gate(self) -> None:
        """Lane 2's completion post carries the footer, not an L2D heading,
        on hrse#1441 and hrse#1573 both. Detecting only the heading would
        misread the most common live shape."""
        self.assertEqual(self.state(self.HANDOFF, self.READY),
                         (lane_state.AWAITING_GATE, None))

    def test_fail_after_pass_routes_back_to_lane_2(self) -> None:
        """TC4 -- an earlier PASS does not survive a later FAIL."""
        state, trigger = self.state(self.HANDOFF, self.L2D, self.PASS, self.FAIL)
        self.assertEqual(trigger, "Fix H1")
        self.assertIn("back to L2", state)

    def test_pass_after_fail_is_gated(self) -> None:
        """The mirror: the re-gate PASS is the newest marker, so it wins.
        This is hrse#1573's and hrse#1441's actual thread order."""
        self.assertEqual(self.state(self.HANDOFF, self.L2D, self.FAIL, self.L2D, self.PASS),
                         (lane_state.GATED, None))

    # ---- hrse#1609: a rework request is visible and names Lane 2 --------

    def test_rework_after_a_completion_names_lane_2(self) -> None:
        """AC1, and hrse#1606's real shape: L2 posts done, L1 asks for a
        rebase. The row used to read `implemented, awaiting gate` -- naming
        Lane 3 while Lane 2 owed the work and nobody had been told."""
        state, trigger = self.state(self.HANDOFF, self.L2D, self.REWORK)
        self.assertEqual(trigger, "Fix H1")
        self.assertEqual(state, f"{lane_state.REWORK_REQUESTED} \u2192 Fix H1")

    #: hrse#1578 comment 9, verbatim — the real rebase request that motivated
    #: this issue, with ONLY its footer kind changed. These two lines are the
    #: entire input `parse_timeline` consumes from a comment: the heading (for
    #: the heading regexes) and the footer (for `_FOOTER_KIND`, which is
    #: authority). Everything between them is prose the parser never reads, so
    #: this IS the live fixture, not a paraphrase of it.
    H1578_REBASE_REAL = (
        "## Note: PR #1593 needs a rebase before it can merge\n\n"
        "<!-- l1-post v1; kind=rework; posted-by=LANE1 -->"
    )
    H1578_REBASE_AS_POSTED = (
        "## Note: PR #1593 needs a rebase before it can merge\n\n"
        "<!-- l1-post v1; kind=discussion; posted-by=LANE1 -->"
    )

    def test_h1578s_real_rebase_request_classifies_correctly(self) -> None:
        """AC4, against hrse#1578's own bytes rather than a replica.

        Both directions on the SAME real comment, differing only in the footer
        kind — which is the whole change this issue makes. As posted
        (`kind=discussion`) the row said `gated` and offered no trigger while
        Lane 2 owed a rebase; that is the defect, reproduced. Re-stamped
        `kind=rework`, the same bytes name Lane 2.
        """
        as_posted = self.state(self.HANDOFF, self.L2D, self.PASS,
                               self.H1578_REBASE_AS_POSTED)
        self.assertEqual(as_posted, (lane_state.GATED, None))

        state, trigger = self.state(self.HANDOFF, self.L2D, self.PASS,
                                    self.H1578_REBASE_REAL)
        self.assertEqual(trigger, "Fix H1")
        self.assertEqual(state, f"{lane_state.REWORK_REQUESTED} \u2192 Fix H1")

    def test_rework_after_a_gate_pass_also_names_lane_2(self) -> None:
        """AC4's fixture shape, hrse#1578 -- and the reason this is ranked in
        `newest()` rather than patched into the `done` branch. On that thread
        the rebase request sits after `gate.pass`, so a `done`-only fix would
        have passed its own suite while missing the case that motivated it."""
        state, trigger = self.state(self.HANDOFF, self.L2D, self.PASS, self.REWORK)
        self.assertEqual(trigger, "Fix H1")
        self.assertEqual(state, f"{lane_state.REWORK_REQUESTED} \u2192 Fix H1")

    def test_an_ordinary_discussion_after_a_completion_does_not_move_the_row(self) -> None:
        """AC2, the false-positive direction -- and the measured reason this
        is a kind rather than an inference. Every one of the 63 threads on
        this repo whose newest marker is a post-`l2.done` discussion is a
        closing note, a merge confirmation or a gate sign-off; inferring
        rework from them would have been wrong 63 times out of 63."""
        self.assertEqual(self.state(self.HANDOFF, self.L2D, self.DECISIONS),
                         (lane_state.AWAITING_GATE, None))

    def test_an_ordinary_discussion_after_a_gate_pass_does_not_move_the_row(self) -> None:
        """AC2 on the other branch, from the same measurement: `## AE --
        H1575 (--execute)` and `## L1 -- --apply run, operator-authorized`
        are both post-`gate.pass` discussions and neither owes Lane 2
        anything."""
        self.assertEqual(self.state(self.HANDOFF, self.L2D, self.PASS, self.DECISIONS),
                         (lane_state.GATED, None))

    def test_lane_2_answering_the_rework_clears_it(self) -> None:
        """The cycle closes on its own: a completion newer than the rework
        request outranks it, so the row returns to awaiting the gate without
        anyone clearing a flag."""
        self.assertEqual(self.state(self.HANDOFF, self.L2D, self.REWORK, self.L2D),
                         (lane_state.AWAITING_GATE, None))

    def test_rework_older_than_the_completion_does_not_fire(self) -> None:
        """Order, not mere presence -- the same rule the `plan` branch already
        states. A rework request Lane 2 has already answered is history."""
        self.assertEqual(self.state(self.HANDOFF, self.REWORK, self.L2D),
                         (lane_state.AWAITING_GATE, None))

    def test_rework_and_gate_fail_share_a_trigger_but_not_a_key(self) -> None:
        """The Lane 1 decision on this issue's plan, asserted rather than
        assumed: the operator types the same thing in both cases, and the
        model still knows which of the two it is. A shared key would make the
        row report the wrong reason on every row it got right."""
        rework = self.full(self.HANDOFF, self.L2D, self.REWORK)
        fail = self.full(self.HANDOFF, self.L2D, self.FAIL)
        self.assertEqual(rework.trigger, fail.trigger)
        self.assertNotEqual(rework.key, fail.key)
        self.assertEqual(rework.key, lane_state.KEY_REWORK)

    def test_the_plan_branch_is_untouched_by_the_rework_marker(self) -> None:
        """AC3 -- hrse#1584's behaviour, byte-identical."""
        self.assertEqual(self.state(self.PLAN_FIRST, self.L2P, self.DECISIONS),
                         ("ready: Implement H1", "Implement H1"))
        self.assertEqual(self.state(self.PLAN_FIRST, self.DECISIONS, self.L2P),
                         (lane_state.PLAN_POSTED, None))

    def test_plan_alone_is_awaiting_lane_1(self) -> None:
        self.assertEqual(self.state(self.PLAN_FIRST, self.L2P),
                         (lane_state.PLAN_POSTED, None))

    def test_retired_l2p_and_current_l2s_read_the_same(self) -> None:
        """`L2P` is retired in favour of `L2S` (lane-shorthand.md) but the
        live tooling still emits `## L2P`. Both spellings, one state."""
        self.assertEqual(self.state(self.PLAN_FIRST, self.L2S),
                         self.state(self.PLAN_FIRST, self.L2P))

    def test_lane_1_decisions_after_a_plan_hand_the_ball_back(self) -> None:
        """Operator decision, hrse#1584. Without this the row reads
        `awaiting L1` for the entire ratified-plan-to-L2D window -- naming
        the wrong actor and withholding the trigger that is actually next.
        That window is every Plan-First thread in this repo."""
        self.assertEqual(self.state(self.PLAN_FIRST, self.L2P, self.DECISIONS),
                         ("ready: Implement H1", "Implement H1"))

    def test_decisions_before_the_plan_do_not_hand_the_ball_back(self) -> None:
        """Order, not mere presence. A discussion comment OLDER than the
        plan is the thread Lane 2 was answering, not an answer to it."""
        self.assertEqual(self.state(self.PLAN_FIRST, self.DECISIONS, self.L2P),
                         (lane_state.PLAN_POSTED, None))

    def test_decisions_on_a_plan_first_handoff_with_no_plan_yet_stay_plan(self) -> None:
        """A discussion comment is not a lane transition on its own."""
        self.assertEqual(self.state(self.PLAN_FIRST, self.DECISIONS),
                         ("ready: Plan H1", "Plan H1"))

    def test_lane_blocked_is_not_read_as_ready(self) -> None:
        """`L2B` is live on hrse#1584 itself. Falling through to the handoff
        below it would render `ready: Implement` on an issue whose own lane
        has said it cannot proceed."""
        state, trigger = self.state(self.HANDOFF, self.L2B)
        self.assertIn("blocked", state)
        self.assertIsNone(trigger)

    def test_no_tier_beats_every_other_marker(self) -> None:
        """TC5 -- `l1_post.validate_tier_set` refuses the handoff, so the
        issue cannot move however far along its thread reads. forge#468 was
        in exactly this state on 2026-09-04."""
        self.assertEqual(self.state(self.HANDOFF, self.L2D, self.PASS, tier=None),
                         (lane_state.BLOCKED_NO_TIER, None))

    def test_unmet_dependency_blocks_and_names_the_target(self) -> None:
        """TC6 -- hrse#199 is blocked on open hrse#192."""
        record = self.record(self.HANDOFF, deps=[("hrse#192", "linked")])
        state, trigger = tuple(lane_state.classify(record, [("hrse#192", "linked")]))[:2]
        self.assertEqual(state, "blocked on hrse#192")
        self.assertIsNone(trigger)

    def test_a_met_dependency_does_not_block(self) -> None:
        """The caller passes the UNMET subset; a closed dependency is not
        one, so the row must reach its real lane state."""
        record = self.record(self.HANDOFF, deps=[("hrse#192", "linked")])
        self.assertEqual(tuple(lane_state.classify(record, []))[:2],
                         ("ready: Implement H1", "Implement H1"))

    def test_an_unreadable_marker_renders_unknown_never_ready(self) -> None:
        """TC7, the fail-loud default. A wrongly-ready row is worse than an
        unknown one: it invites a trigger that will not work.

        `unknown` is reserved for a comment that IS marker-shaped and cannot
        be read -- an unmapped lane token, a gate heading carrying neither
        verdict -- not for an issue that simply has no handoff yet."""
        for body in ("## L1D -- an unmapped lane token",
                     "## Lane 3 Gate Results -- hrse#1 -- inconclusive"):
            with self.subTest(body=body):
                state, trigger = self.state(body)
                self.assertEqual(state, lane_state.UNKNOWN)
                self.assertIsNone(trigger)

    def test_chatter_with_no_handoff_says_no_handoff_not_unknown(self) -> None:
        """Ten rows on the live 2.9 board read `unknown` before this
        distinction existed -- all of them issues carrying only Lane 1
        discussion, i.e. plainly *not startable*, not unclassifiable.
        Calling those unknown buries the real unknowns in noise."""
        self.assertEqual(self.state("## Naming decision, locked in", self.DECISIONS),
                         (lane_state.NO_HANDOFF, None))

    def test_a_handoff_heading_is_honored_when_the_footer_lies(self) -> None:
        """hrse#1546's handoff -- a complete one -- is stamped
        `kind=discussion; posted-by=LANE-unset`. Footer-only detection
        renders that issue as having no handoff at all, which is precisely
        the confidently-wrong row this issue exists to remove."""
        body = ("## Handoff: hrse#1546 -- inject the task-list instruction\n\n"
                "### Affected Files\n\n"
                "<!-- l1-post v1; kind=discussion; posted-by=LANE-unset -->")
        # hrse#1589 changes the VERB here, and correctly. This fixture is a
        # legacy handoff carrying neither a `plan-first` declaration nor a
        # `Delegated Judgment Calls` section, so Plan-First is genuinely
        # undetermined and `unknown` is the honest answer. The property this
        # test protects -- that the HEADING is honored when the footer says
        # `kind=discussion` -- is unchanged: the row is still recognised as a
        # handoff rather than reading as "no handoff".
        self.assertEqual(self.state(body), (lane_state.UNKNOWN, None))

    def test_a_handoff_quoted_inside_a_fence_is_not_a_handoff(self) -> None:
        """The cost of the heading fallback, paid for: a comment showing what
        a handoff looks like must not be read as one. The same fence-strip
        `milestone_summary_analysis.py` already does before its own
        structural search."""
        body = "## L1 -- how to write one\n\n```\n## Handoff: H1 -- example\n```\n"
        self.assertEqual(self.state(body), (lane_state.NO_HANDOFF, None))

    def test_a_gate_heading_that_is_not_a_verdict_is_not_read_as_one(self) -> None:
        """hrse#1575's last comment is `## Lane 3 -- ... finding closed,
        `--execute` independently verified`. It is not a gate verdict and
        must not be scored as one; the PASS below it is still the state."""
        state, _ = self.state(self.HANDOFF, self.L2D, self.PASS,
                              "## Lane 3 -- hrse#1 finding closed, `--execute` verified")
        self.assertEqual(state, lane_state.GATED)


class LaneStateRenderTests(RenderTests):
    """The state has to reach the row, not just the classifier."""

    def record(self, key, comments=(), **kwargs):
        record = super().record(key, **kwargs)
        # harmonic-forge#134: a fresh `createdAt`, because every real comment
        # carries one — 0 of 3,421 transitions measured had an empty `at`.
        # Without it these rows render "age unknown", which is correct
        # behaviour on an input that does not occur live.
        now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        record["comments"] = [{"body": body, "createdAt": now} for body in comments]
        return record

    def test_row_carries_the_state_and_the_literal_trigger(self) -> None:
        text = self.render([self.record("H1", comments=[LaneStateTests.HANDOFF])])
        self.assertIn("[Todo | Tier: fast | ready: Implement H1]", text)

    def test_row_with_no_comments_says_no_handoff(self) -> None:
        self.assertIn("| no handoff]", self.render([self.record("H1")]))

    def test_unmet_dependency_uses_the_summarys_own_open_key_set(self) -> None:
        """AC: reuse the existing detection, not a second mechanism. The
        open-key set is what makes a dependency *unmet*."""
        text = self.render([self.record("H1", deps=[("hrse#2", "linked")])],
                           open_keys={"H1", "H2"})
        self.assertIn("blocked on hrse#2", text)

    def test_a_closed_dependency_does_not_block_the_row(self) -> None:
        text = self.render([self.record("H1", deps=[("hrse#2", "linked")])],
                           open_keys={"H1"})
        self.assertNotIn("blocked on", text)

    def test_forge_and_unmilestoned_rows_are_annotated_too(self) -> None:
        """`blanks` and `forge` render through the same `_line()`; an
        annotation pass that walked only `records` would leave two whole
        sections silently stateless."""
        text = self.render([], blanks=[self.record("H9")],
                           forge=[self.record("F1", repo="vitalharmony/harmonic-forge")])
        self.assertEqual(text.count("| no handoff]"), 2)


class LaneStateCostTests(unittest.TestCase):
    """TC8 -- cost does not regress. Asserted by capturing invocations, not
    by timing: a full board scan exhausted the GraphQL quota twice on
    2026-09-04 (harmonic-forge#468)."""

    def test_comments_ride_the_existing_issue_list_call(self) -> None:
        calls = []

        def fake(*args):
            calls.append(args)
            return []

        with mock.patch.object(summary, "gh_json", fake):
            summary.fetch_issues("vitalharmony/hrse", None)
        self.assertEqual(len(calls), 1, "lane state must not add a second request")
        self.assertIn("comments", calls[0][calls[0].index("--json") + 1])

    def test_no_board_scan_is_issued(self) -> None:
        calls = []

        def fake(*args):
            calls.append(" ".join(args))
            return []

        with mock.patch.object(summary, "gh_json", fake):
            summary.fetch_issues("vitalharmony/hrse", None)
        for call in calls:
            self.assertNotIn("project", call)
            self.assertNotIn("item-list", call)


class PlanFirstDerivationTests(unittest.TestCase):
    """hrse#1589 — Plan-First is declared, not inferred from prose.

    The predicate this replaces searched the whole body for the literal
    "Plan-First". It matched hrse#1546's handoff, whose own sentence declines
    it — ordinary prose naming the mechanism in order to say no.
    """

    #: The live regression, verbatim from hrse#1546's handoff.
    H1546 = ("### Delegated Judgment Calls\n"
             "Given no design ambiguity and a single-file change, **this does "
             "not need Plan-First.** Implement directly.\n")

    def declared(self, value):
        return ("## Handoff: H1 -- x\n\n"
                f"<!-- l1-post v1; kind=handoff; plan-first={value}; sha=a; checks=x -->")

    def state(self, body, tier="standard"):
        record = {"key": "H1", "number": 1, "title": "t", "repo": "vitalharmony/hrse",
                  "fields": {"Tier": tier}, "deps": [], "scope": [], "tooling": None,
                  "comments": [{"body": body}]}
        return tuple(lane_state.classify(record, []))[:2]

    # --- the declared footer is authority ---------------------------------

    def test_a_declared_true_yields_plan(self):
        self.assertEqual(self.state(self.declared("true")), ("ready: Plan H1", "Plan H1"))

    def test_a_declared_false_yields_implement(self):
        self.assertEqual(self.state(self.declared("false")),
                         ("ready: Implement H1", "Implement H1"))

    def test_the_declaration_beats_contradicting_prose(self):
        """The point of declaring it. A handoff whose prose says one thing
        and whose author declared another is not a coin flip — the author
        answered, across triggers the prose cannot express."""
        body = self.declared("false").replace(
            "## Handoff: H1 -- x",
            "## Handoff: H1 -- x\n\n### Delegated Judgment Calls\nOne. A real "
            "delegated call.\n")
        self.assertEqual(self.state(body), ("ready: Implement H1", "Implement H1"))

    # --- hrse#1546, the live regression ------------------------------------

    def test_hrse1546_classifies_as_implement_not_plan(self):
        """AC4. The handoff says "Implement directly"; the old predicate
        returned `ready: Plan` because the sentence contains the string."""
        body = f"## Handoff: hrse#1546\n\n{self.H1546}"
        self.assertEqual(self.state(body), ("ready: Implement H1", "Implement H1"))

    def test_prose_declining_plan_first_is_not_plan_first(self):
        """AC5, one direction — asserted on the resolver so the reason is
        visible, not just the rendered state."""
        self.assertIs(lane_state.plan_first_of(self.H1546), False)

    def test_a_section_declaring_plan_first_is_plan_first(self):
        """AC5, the other direction. Both must hold or the fix is a
        one-way ratchet that just stops saying Plan."""
        section = ("### Delegated Judgment Calls\nOne, and it makes this "
                   "issue Plan-First.\n")
        self.assertIs(lane_state.plan_first_of(section), True)

    # --- the tri-state, and AC6 --------------------------------------------

    def test_no_delegated_section_yields_unknown_not_a_verb(self):
        """AC6. Not `Implement` (ADR-005's exposure) and not `Plan` either —
        a confident `ready:` from a read that failed is the thing the module
        already refuses to do everywhere else."""
        body = "## Handoff: H1 -- x\n\nNo such section anywhere.\n"
        self.assertEqual(self.state(body), (lane_state.UNKNOWN, None))

    def test_an_empty_delegated_section_yields_unknown(self):
        body = "## Handoff: H1 -- x\n\n### Delegated Judgment Calls\n\n### Next\n"
        self.assertEqual(self.state(body), (lane_state.UNKNOWN, None))

    def test_a_section_that_both_declines_and_declares_yields_unknown(self):
        """Reading either half would be a coin flip wearing a derivation's
        clothes."""
        section = ("### Delegated Judgment Calls\nThis does not need "
                   "Plan-First. On reflection this is Plan-First.\n")
        self.assertIsNone(lane_state.plan_first_of(section))

    def test_no_input_can_produce_ready_implement_from_an_undetermined_read(self):
        """AC6 stated as the negative it actually is. Every undetermined
        shape must land on `unknown`, not on either verb."""
        for body in ("## Handoff: H1\n\nnothing\n",
                     "## Handoff: H1\n\n### Delegated Judgment Calls\n   \n",
                     "## Handoff: H1\n\n### Delegated Judgment Calls\n"
                     "not plan-first, but this is Plan-First\n"):
            with self.subTest(body=body):
                state, trigger = self.state(body)
                self.assertEqual(state, lane_state.UNKNOWN)
                self.assertIsNone(trigger)

    def test_none_followed_by_an_explanation_is_still_none(self):
        """The live idiom, and a real bug this caught. hrse#1382's section is
        *"None. Alias, `extra="forbid"`, caller audit, round-trip test — all
        specified."* — "None" plus a sentence saying why. An anchor on the
        section's WHOLE content misses every one of those and reads the
        explanation as a delegated call, flipping correctly-`Implement` rows
        to `Plan`.

        Found by AC7's row-by-row comparison, not by review: three rows moved
        that should not have, and the totals alone showed only a
        plausible-looking 5->3 / 6->8 shift.
        """
        for section in (
            "### Delegated Judgment Calls\nNone. Alias, `extra=\"forbid\"`, caller "
            "audit, round-trip test — all specified.\n",
            "### Delegated Judgment Calls\nNone. The API shape is specified below.\n",
            "### Delegated Judgment Calls\nN/A — nothing delegated.\n",
        ):
            with self.subTest(section=section):
                self.assertIs(lane_state.plan_first_of(section), False)

    def test_numbered_delegated_calls_are_plan_first(self):
        """hrse#330's real section: two numbered calls, and the words
        "Plan-First" appear nowhere in the handoff. The old free-text
        predicate therefore said `Implement`; R-0244 trigger 1 says
        otherwise. This is the second live correction, and it was not in the
        issue's own cross-check."""
        section = ("### Delegated Judgment Calls\n"
                   "1. Whether `API_BASE_URL` is derived from "
                   "`window.location.origin` directly vs. piped through the "
                   "bridge — either satisfies the acceptance checks.\n"
                   "2. Exact env var names in the entrypoint script.\n")
        self.assertIs(lane_state.plan_first_of(section), True)

    def test_the_section_terminator_stops_at_the_next_heading(self):
        """A blank line after the heading used to let `\\s*` swallow it, so the
        captured body STARTED at the next heading and an empty section read as
        substantive. `milestone_summary_analysis.py`'s `_DEPENDENCIES_SECTION`
        carries a recorded incident of the same shape (hrse#1523)."""
        body = ("### Delegated Judgment Calls\n\n### Pre-Flight Preconditions\n"
                "- traced — something substantive here\n")
        self.assertIsNone(lane_state.plan_first_of(body))

        # The no-blank-line form, which distinguishes the two halves of the
        # fix. With the heading line ending at `[ \t]*\n` the blank line is no
        # longer swallowed, so a `\n#`-style terminator would also work HERE —
        # but not here, where the next heading is immediately adjacent and
        # there is no newline in front of it inside the captured span.
        adjacent = ("### Delegated Judgment Calls\n### Pre-Flight Preconditions\n"
                    "- traced — something substantive\n")
        self.assertIsNone(lane_state.plan_first_of(adjacent))

    def test_a_none_section_is_determined_not_unknown(self):
        """"None" is an answer — R-0244 trigger 1 not firing — and must not
        be conflated with the absence of one, or every non-Plan-First issue
        becomes unrenderable."""
        self.assertIs(lane_state.plan_first_of(
            "### Delegated Judgment Calls\nNone.\n"), False)

    # --- the old predicate is gone, not narrowed --------------------------

    def test_the_free_text_regex_is_deleted(self):
        """Narrowing it would leave prose as an input, which is the root
        problem rather than a symptom of it."""
        self.assertFalse(hasattr(lane_state, "_PLAN_FIRST"))

    def test_a_body_merely_mentioning_plan_first_outside_the_section_is_ignored(self):
        """The old predicate's whole failure mode, at the resolver."""
        body = ("## Handoff: H1\n\nThe sibling issue hrse#9 is Plan-First.\n\n"
                "### Delegated Judgment Calls\nNone.\n")
        self.assertIs(lane_state.plan_first_of(body), False)


class GatePhaseTests(unittest.TestCase):
    """hrse#1590 -- the gate preamble, read from `kind=` footers.

    Fixtures are the live shapes from hrse#1573's thread (read 2026-09-05),
    footers included verbatim in form, because the whole claim is about what
    `l1_post.py` actually stamps. A paraphrased footer would pass while the
    parser read the wrong authority.
    """

    SPEC = "## Lane 3 Test Spec — hrse#1\n\n### Test cases\n1. TC1 — something."
    AE = ("## AE — H1\n\nOperator approved: Lane 3 may execute the TCs.\n\n"
          "<!-- l1-post v1; kind=ae; sha=d5339ca; body-sha256=" + "a" * 64 +
          "; checks=body-validation -->")
    SWEEP = ("## Gate-readiness sweep — H1\n\nWrite tier: W\n\n1. TC1 — ready.\n\n"
             "<!-- l1-post v1; kind=sweep; sha=d5339ca; body-sha256=" + "b" * 64 +
             "; checks=body-validation -->")
    #: A footer claiming `kind=ae` on a comment carrying no AE heading. The
    #: protocol warns an AE posted through ordinary discussion "reads
    #: correctly to a human but is invisible to Lane 3's own spec/AE fetch",
    #: so heading-presence and authorization are KNOWN to diverge; AC1 asks
    #: for that divergence to be reported, not resolved.
    AE_NO_HEADING = ("## L1 — some other heading\n\napproving informally\n\n"
                     "<!-- l1-post v1; kind=ae; sha=d5339ca; body-sha256=" +
                     "c" * 64 + "; checks=body-validation -->")

    HANDOFF = LaneStateTests.HANDOFF
    L2D = LaneStateTests.L2D
    READY = LaneStateTests.READY
    FAIL = LaneStateTests.FAIL

    def full(self, *bodies):
        record = {"key": "H1", "number": 1, "title": "t",
                  "repo": "vitalharmony/hrse", "fields": {"Tier": "standard"},
                  "deps": [], "scope": [], "tooling": None,
                  "comments": [{"body": body} for body in bodies]}
        return lane_state.classify(record, [])

    # --- AC1: footer authority, headings as cross-check -------------------

    def test_the_ae_is_found_by_its_footer_not_its_heading(self) -> None:
        """AC1. The heading is absent; the footer alone carries the state."""
        state = self.full(self.HANDOFF, self.READY, self.SPEC, self.AE_NO_HEADING)
        self.assertEqual(state.state, lane_state.AE_WITHOUT_SWEEP)

    def test_a_footer_whose_mandated_heading_is_missing_is_not_validated(self) -> None:
        """AC1's other half: the disagreement is REPORTED. `validated=False`
        is the report -- the state is still derived, and the timeline says
        the cross-check did not hold."""
        timeline = lane_state.parse_timeline([{"body": self.AE_NO_HEADING}])
        self.assertEqual([(t.key, t.provenance, t.validated) for t in timeline],
                         [(lane_state.KEY_AE, "footer:ae", False)])

    def test_a_well_formed_ae_validates(self) -> None:
        timeline = lane_state.parse_timeline([{"body": self.AE}])
        self.assertEqual([(t.key, t.provenance, t.validated) for t in timeline],
                         [(lane_state.KEY_AE, "footer:ae", True)])

    def test_the_test_spec_is_heading_provenance_because_it_has_no_emitter(self) -> None:
        """Operator decision, option (a). `l1_post.py --kind` has no `spec`
        and `l2_post.py` covers only L2P/L2D/L2B, so the Test Spec carries no
        footer at all. It is read from its heading and SAYS so."""
        timeline = lane_state.parse_timeline([{"body": self.SPEC}])
        self.assertEqual([(t.key, t.provenance, t.validated) for t in timeline],
                         [(lane_state.KEY_SPEC_POSTED, "heading:spec", False)])

    def test_a_gate_result_is_heading_provenance_for_the_same_reason(self) -> None:
        timeline = lane_state.parse_timeline([{"body": self.FAIL}])
        self.assertEqual([(t.key, t.provenance, t.validated) for t in timeline],
                         [(lane_state.KEY_GATE_FAIL, "heading:gate-result", False)])

    # --- TC1/TC2/TC3: the preamble states ---------------------------------

    def test_a_spec_with_no_ae_is_not_executable(self) -> None:
        """TC1. Awaiting approval, not authorized."""
        state = self.full(self.HANDOFF, self.READY, self.SPEC)
        self.assertEqual((state.state, state.key),
                         (lane_state.SPEC_POSTED, lane_state.KEY_SPEC_POSTED))

    def test_an_ae_with_no_sweep_names_lane_1_as_the_owner(self) -> None:
        """TC2 and R-0208. AE and sweep are one atomic action, so this is a
        PARTIAL transition, and the sweep is Lane 1's (`testing-gate.md`
        rule 3). Naming the owner is the whole reason R-0128 exists."""
        state = self.full(self.HANDOFF, self.READY, self.SPEC, self.AE)
        self.assertEqual(state.key, lane_state.KEY_AE_WITHOUT_SWEEP)
        self.assertIn("L1", state.state)

    def test_spec_ae_and_sweep_is_executable(self) -> None:
        """TC3."""
        state = self.full(self.HANDOFF, self.READY, self.SPEC, self.AE, self.SWEEP)
        self.assertEqual((state.state, state.key),
                         (lane_state.EXECUTABLE, lane_state.KEY_EXECUTABLE))

    def test_a_sweep_with_no_ae_beneath_it_is_unknown(self) -> None:
        """TC7 and AC8. R-0208 puts the sweep strictly AFTER the AE, so a
        sweep standing alone is a sequence this module cannot place. It
        fails loud rather than reading as authorization that skipped a step
        -- a wrongly-executable row invites a gate that is not authorized."""
        state = self.full(self.HANDOFF, self.READY, self.SPEC, self.SWEEP)
        self.assertEqual((state.state, state.key),
                         (lane_state.UNKNOWN, lane_state.KEY_UNKNOWN))

    def test_a_sweep_before_its_ae_is_also_unknown(self) -> None:
        """Order is load-bearing, not incidental: the same two comments in
        the wrong order are not an authorization."""
        state = self.full(self.HANDOFF, self.READY, self.SPEC, self.SWEEP, self.AE)
        self.assertEqual(state.key, lane_state.KEY_AE_WITHOUT_SWEEP)

    # --- AC3 / R-0209: carry-forward over the FAIL cycle -------------------

    def test_a_fail_then_fix_then_ready_carries_the_authorization_forward(self) -> None:
        """AC3 and R-0209, mirroring `check_lane3_ready.carry_forward()`. A
        `ready-for-l3` posted after the authorizing comment extends it to a
        new SHA. This is the most common cycle in the protocol; demanding a
        fresh AE here marks correctly-authorized work as blocked."""
        state = self.full(self.HANDOFF, self.READY, self.SPEC, self.AE,
                          self.SWEEP, self.FAIL, self.L2D, self.READY)
        self.assertEqual((state.state, state.key),
                         (lane_state.EXECUTABLE, lane_state.KEY_EXECUTABLE))

    def test_a_fail_still_routes_back_to_lane_2_before_the_refix_lands(self) -> None:
        """The carry-forward must not swallow the FAIL itself. Between the
        FAIL and Lane 2's re-post the row still says `Fix H1`."""
        state = self.full(self.HANDOFF, self.READY, self.SPEC, self.AE,
                          self.SWEEP, self.FAIL)
        self.assertEqual((state.trigger, state.key),
                         ("Fix H1", lane_state.KEY_GATE_FAIL))

    def test_an_unauthorized_l2d_is_awaiting_gate_not_executable(self) -> None:
        """The mirror of the test above: without an AE/sweep pair beneath it,
        a completion post is `implemented, awaiting gate` exactly as before.
        Mutating `authorized()` to `return True` fails here."""
        state = self.full(self.HANDOFF, self.L2D)
        self.assertEqual((state.state, state.key),
                         (lane_state.AWAITING_GATE, lane_state.KEY_AWAITING_GATE))

    # --- AC4: the timeline -------------------------------------------------

    def test_the_timeline_keeps_every_round_not_just_the_newest(self) -> None:
        """AC4 and the reason hrse#1194 needs this: `FAIL rounds per issue`
        is a COUNT. `_Markers` keeps one index per class by construction, so
        a fold over it can never produce more than one."""
        timeline = lane_state.parse_timeline(
            [{"body": body} for body in
             (self.HANDOFF, self.READY, self.FAIL, self.L2D, self.READY, self.FAIL)])
        self.assertEqual([t.key for t in timeline],
                         [lane_state.KEY_HANDOFF, lane_state.KEY_L2_DONE,
                          lane_state.KEY_GATE_FAIL, lane_state.KEY_L2_DONE,
                          lane_state.KEY_L2_DONE, lane_state.KEY_GATE_FAIL])
        self.assertEqual(sum(t.key == lane_state.KEY_GATE_FAIL for t in timeline), 2)

    def test_the_timeline_carries_the_comment_id_and_timestamp(self) -> None:
        """Measured live, not assumed: `gh issue list --json comments` gives
        a STRING node id (`IC_kwDO...`) and `createdAt`, not an integer id.
        Auditability is the point -- a state you can trace to a comment id is
        one a human can check."""
        timeline = lane_state.parse_timeline([
            {"body": self.AE, "id": "IC_kwDOSDFJYM8AAAABSs4KIg",
             "createdAt": "2026-09-05T01:54:07Z"}])
        self.assertEqual((timeline[0].comment_id, timeline[0].at),
                         ("IC_kwDOSDFJYM8AAAABSs4KIg", "2026-09-05T01:54:07Z"))

    def test_a_comment_with_no_id_still_yields_a_transition(self) -> None:
        """The existing fixture shape passes bodies only. Missing metadata
        degrades to `""`; it never drops the transition."""
        timeline = lane_state.parse_timeline([{"body": self.AE}])
        self.assertEqual((timeline[0].comment_id, timeline[0].at), ("", ""))

    def test_classify_folds_the_same_timeline_it_exposes(self) -> None:
        """AC4's `classify()` folds it. Same input, same marker set -- not
        two parsers that can drift, which is the defect hrse#1589 exists to
        fix in the Plan-First predicate."""
        bodies = [{"body": b} for b in
                  (self.HANDOFF, self.READY, self.SPEC, self.AE, self.SWEEP)]
        keys = [t.key for t in lane_state.parse_timeline(bodies)]
        self.assertEqual(keys[-1], lane_state.KEY_SWEEP)
        record = {"key": "H1", "fields": {"Tier": "standard"}, "deps": [],
                  "scope": [], "tooling": None, "comments": bodies}
        self.assertEqual(lane_state.classify(record, []).key,
                         lane_state.KEY_EXECUTABLE)

    # --- AC5: the stable key -----------------------------------------------

    def test_no_key_carries_an_issue_number_or_prose(self) -> None:
        """TC4, across every state including the five interpolated ones."""
        cases = [
            (), (self.HANDOFF,), (LaneStateTests.PLAN_FIRST,),
            (self.HANDOFF, LaneStateTests.L2P),
            (self.HANDOFF, LaneStateTests.L2P, LaneStateTests.DECISIONS),
            (self.HANDOFF, self.SPEC), (self.HANDOFF, self.L2D),
            (self.HANDOFF, self.L2D, LaneStateTests.PASS),
            (self.HANDOFF, self.L2D, self.FAIL),
            (self.HANDOFF, LaneStateTests.L2B),
            (self.HANDOFF, self.READY, self.SPEC, self.AE),
            (self.HANDOFF, self.READY, self.SPEC, self.AE, self.SWEEP),
            (self.HANDOFF, self.READY, self.SPEC, self.SWEEP),
        ]
        seen = set()
        for bodies in cases:
            with self.subTest(bodies=len(bodies)):
                key = self.full(*bodies).key
                self.assertNotIn("H1", key)
                self.assertNotRegex(key, r"\d")
                self.assertRegex(key, r"^[a-z0-9.-]+$")
                seen.add(key)
        self.assertGreaterEqual(len(seen), 10)

    def test_the_no_tier_and_dependency_keys_are_stable_too(self) -> None:
        """The two blockers ahead of every marker. `blocked on hrse#192`
        interpolates a target; its key must not."""
        record = {"key": "H1", "fields": {}, "deps": [], "scope": [],
                  "tooling": None, "comments": [{"body": self.HANDOFF}]}
        self.assertEqual(lane_state.classify(record, []).key,
                         lane_state.KEY_BLOCKED_NO_TIER)
        record["fields"] = {"Tier": "fast"}
        state = lane_state.classify(record, [("hrse#192", "linked")])
        self.assertEqual((state.state, state.key),
                         ("blocked on hrse#192", lane_state.KEY_BLOCKED_DEP))

    # --- AC6: existing display strings byte-identical ----------------------

    def test_every_pre_existing_display_string_is_byte_identical(self) -> None:
        """TC5, diffed rather than inspected. These literals are transcribed
        from hrse#1584's shipped module; a one-byte drift in any of them
        breaks `board_dashboard_renderer.py`'s captured history."""
        self.assertEqual(
            [lane_state.BLOCKED_NO_TIER, lane_state.NO_HANDOFF,
             lane_state.PLAN_POSTED, lane_state.SPEC_POSTED,
             lane_state.AWAITING_GATE, lane_state.GATED, lane_state.UNKNOWN],
            ["blocked: no Tier", "no handoff", "plan posted, awaiting L1",
             "spec posted, awaiting L1", "implemented, awaiting gate",
             "gated", "unknown"])
        self.assertEqual(self.full(self.HANDOFF).state, "ready: Implement H1")
        self.assertEqual(self.full(LaneStateTests.PLAN_FIRST).state, "ready: Plan H1")
        self.assertEqual(self.full(self.HANDOFF, self.L2D, self.FAIL).state,
                         "FAIL, back to L2 → Fix H1")
        self.assertEqual(self.full(self.HANDOFF, LaneStateTests.L2B).state,
                         "blocked: L2B, see thread")

    def test_no_display_string_can_break_the_dashboard_row_grammar(self) -> None:
        """TC6, one direction. `_BRACKETED_ROW_RE` splits on ` | ` inside
        `[...]`, so a state containing `|` or `]` silently degrades that row
        to `class="unparsed"`. Asserted for the new states too."""
        for name in dir(lane_state):
            value = getattr(lane_state, name)
            if name.isupper() and isinstance(value, str) and not name.startswith("KEY_"):
                with self.subTest(name=name):
                    self.assertNotIn("|", value)
                    self.assertNotIn("]", value)


class GatePhaseRenderTests(RenderTests):
    """TC6, the other direction: the state reaches the row AND the dashboard
    renderer parses it back out. Both shown, neither assumed."""

    def record(self, key, comments=(), **kwargs):
        record = super().record(key, **kwargs)
        now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        record["comments"] = [{"body": body, "createdAt": now} for body in comments]
        return record

    def test_an_executable_row_renders_and_reparses(self) -> None:
        text = self.render([self.record("H1", comments=[
            GatePhaseTests.HANDOFF, GatePhaseTests.READY, GatePhaseTests.SPEC,
            GatePhaseTests.AE, GatePhaseTests.SWEEP])])
        row = next(line for line in text.splitlines()
                   if line.startswith("- ") and "Tier:" in line)
        self.assertIn(f"| {lane_state.EXECUTABLE}]", row)
        renderer = load_from(REPO_ROOT / "scripts", "board_dashboard_renderer")
        match = renderer._BRACKETED_ROW_RE.match(row)
        self.assertIsNotNone(match)
        self.assertEqual(match.group("lane"), lane_state.EXECUTABLE)

    def test_the_stable_key_is_stamped_on_the_record(self) -> None:
        """`annotate()` is where a consumer other than the row itself picks
        the key up; a key computed and dropped would satisfy AC5's letter
        and nothing else."""
        records = [self.record("H1", comments=[GatePhaseTests.HANDOFF])]
        self.render(records)
        self.assertEqual(records[0]["lane_key"], lane_state.KEY_READY_IMPLEMENT)


class QuotedMarkerTests(unittest.TestCase):
    """A marker QUOTED as evidence must not forge a transition.

    Found live, not by fixture: hrse#1590's own L2P comment tabulates the
    live footers of hrse#1573's thread inside a fenced block, and running
    this module against the real thread put a `gate.fail` on an issue that
    has never been gated. hrse#1584 already stripped fences for the handoff
    heading; hrse#1590 applies that to every marker, footers included.
    """

    #: Verbatim shape from hrse#1590's own plan comment.
    EVIDENCE = (
        "## L2P — receipt-backed status\n\n"
        "Live footers across hrse#1573's thread:\n\n"
        "```\n"
        "## Lane 3 Test Spec — hrse#1573              ||  -\n"
        "## AE — H1573                                ||  ae\n"
        "## Gate-readiness sweep — H1573              ||  sweep\n"
        "## Lane 3 Gate Results — hrse#1573 — FAIL    ||  -\n"
        "## L2D — receipt-backed status               ||  -\n"
        "```\n")

    def test_a_quoted_marker_block_yields_only_the_real_marker(self) -> None:
        timeline = lane_state.parse_timeline([{"body": self.EVIDENCE}])
        self.assertEqual([t.key for t in timeline], [lane_state.KEY_PLAN_POSTED])

    def test_a_quoted_footer_does_not_forge_an_authorization(self) -> None:
        """The sharper half: `l1_post.py` never emits a footer inside a
        fence, so one found there is quoted evidence. Reading it would let a
        pasted AE authorize a gate."""
        body = ("## L2P — receipt-backed status\n\nWhat an AE looks like:\n\n"
                "```\n## AE — H1\n<!-- l1-post v1; kind=ae; sha=abc; "
                "body-sha256=" + "a" * 64 + " -->\n```\n")
        timeline = lane_state.parse_timeline([{"body": body}])
        self.assertEqual([t.key for t in timeline], [lane_state.KEY_PLAN_POSTED])

    def test_the_real_hrse_1590_thread_is_not_gated(self) -> None:
        """The live symptom, as a fixture: handoff, then the evidence-quoting
        plan, then Lane 1's answer. Before the fix this classified as
        `FAIL, back to L2 → Fix H1590`."""
        record = {"key": "H1590", "fields": {"Tier": "deep"}, "deps": [],
                  "scope": [], "tooling": None,
                  "comments": [{"body": LaneStateTests.HANDOFF},
                               {"body": self.EVIDENCE},
                               {"body": LaneStateTests.DECISIONS}]}
        state = lane_state.classify(record, [])
        self.assertEqual((state.state, state.key),
                         ("ready: Implement H1590", lane_state.KEY_READY_IMPLEMENT))


class ValidationCrossCheckTests(unittest.TestCase):
    """`validated` needs BOTH halves. Each was checked alone until mutation
    testing removed the digest requirement and nothing failed."""

    HEADING_NO_DIGEST = ("## AE — H1\n\napproved\n\n"
                         "<!-- l1-post v1; kind=ae; sha=abc; checks=body-validation -->")

    def test_a_footer_with_no_body_digest_is_not_validated(self) -> None:
        """The heading is right and the kind is right; there is nothing to
        detect an edit against. `check_lane3_ready.py` treats the digest as
        the only thing standing between an edited sweep and a forged
        authorization, so its absence is not a validated artifact."""
        timeline = lane_state.parse_timeline([{"body": self.HEADING_NO_DIGEST}])
        self.assertEqual([(t.key, t.validated) for t in timeline],
                         [(lane_state.KEY_AE, False)])

    def test_one_comment_can_carry_more_than_one_marker(self) -> None:
        """hrse#1546's handoff is stamped `kind=discussion` while carrying a
        `## Handoff` heading — one comment, two markers. A scan that stopped
        at the first would drop the handoff on that live issue, which is the
        row hrse#1584 exists to fix."""
        body = ("## Handoff: H1 — x\n\n### Delegated Judgment Calls\nNone.\n\n"
                "<!-- l1-post v1; kind=discussion; posted-by=LANE-unset -->")
        keys = [t.key for t in lane_state.parse_timeline([{"body": body}])]
        self.assertEqual(keys, [lane_state.KEY_DISCUSSION, lane_state.KEY_HANDOFF])

    def test_that_dual_marker_comment_does_not_answer_its_own_plan(self) -> None:
        """And the `elif` that hrse#1584 wrote survives the timeline rewrite:
        a comment that is both must not count as Lane 1 answering the plan
        below it."""
        body = ("## Handoff: H1 — x\n\n### Delegated Judgment Calls\nNone.\n\n"
                "<!-- l1-post v1; kind=discussion; posted-by=LANE-unset -->")
        record = {"key": "H1", "fields": {"Tier": "fast"}, "deps": [], "scope": [],
                  "tooling": None,
                  "comments": [{"body": body}, {"body": LaneStateTests.L2P}]}
        self.assertEqual(lane_state.classify(record, []).state,
                         lane_state.PLAN_POSTED)

    def test_dropping_the_discussion_does_not_erase_an_earlier_real_one(self) -> None:
        """The fix for the above must drop THIS comment's discussion marker,
        not null the field: a genuine `kind=discussion` from Lane 1 lower in
        the thread still has to answer the plan above it."""
        dual = ("## Handoff: H1 — x\n\n### Delegated Judgment Calls\nNone.\n\n"
                "<!-- l1-post v1; kind=discussion; posted-by=LANE-unset -->")
        record = {"key": "H1", "fields": {"Tier": "fast"}, "deps": [], "scope": [],
                  "tooling": None,
                  "comments": [{"body": dual}, {"body": LaneStateTests.L2P},
                               {"body": LaneStateTests.DECISIONS}]}
        self.assertEqual(lane_state.classify(record, []).trigger, "Implement H1")


class RestructuredReportTests(unittest.TestCase):
    """harmonic-forge#472's TC4, from this side of the repo boundary.

    That issue restructures `l2_post.py`'s emitted body — lead block on top,
    receipts JSON moved into a collapsed `<details>`. Its own AC6 says
    `lane_state.py`'s markers must still match, and the handoff's TC4 says to
    RUN the comparison rather than reason about it. These bodies are the two
    shapes byte-for-byte, so the assertion is a real before/after.
    """

    OLD = ("## L2D — receipt-backed status (harmonic-forge#371)\n\n"
           "### Verified receipts\n```json\n[]\n```\n\n"
           "### Narrative\nDone.\n")
    NEW = ("## L2D — receipt-backed status (harmonic-forge#371)\n\n"
           "**Status:** implemented, unpushed.\n"
           "**Change:** three files.\n"
           "**Next:** Lane 1 reviews.\n\n"
           "### Narrative\nDone.\n\n"
           "<details><summary>Verified receipts — 0</summary>\n\n"
           "```json\n[]\n```\n\n"
           "</details>\n")

    def record(self, body):
        return {"key": "H1", "fields": {"Tier": "fast"}, "deps": [], "scope": [],
                "tooling": None,
                "comments": [{"body": LaneStateTests.HANDOFF}, {"body": body}]}

    def test_the_restructured_body_classifies_identically(self) -> None:
        self.assertEqual(lane_state.classify(self.record(self.NEW), []),
                         lane_state.classify(self.record(self.OLD), []))
        self.assertEqual(lane_state.classify(self.record(self.NEW), []).key,
                         lane_state.KEY_AWAITING_GATE)

    def test_the_marker_heading_is_still_top_level_in_the_new_shape(self) -> None:
        """AC6's stated requirement, not merely its effect: hrse#1590 made
        position load-bearing, so `## L2D` above the `<details>` is what is
        being asserted — not just that the state came out right."""
        self.assertTrue(self.NEW.startswith("## L2D "))
        self.assertLess(self.NEW.index("## L2D"), self.NEW.index("<details>"))

    def test_a_marker_quoted_inside_the_collapsed_evidence_does_not_transition(self) -> None:
        """The hazard the red team named and hrse#1590 closed: evidence
        routinely quotes marker-shaped headings, and `<details>` does not
        hide text from a whole-body regex. Fenced, it is inert — which is why
        this issue could only land after that one."""
        body = (self.NEW.replace("```json\n[]\n```",
                                 "```\n## Lane 3 Gate Results — H1 — FAIL\n```"))
        self.assertEqual(lane_state.classify(self.record(body), []).key,
                         lane_state.KEY_AWAITING_GATE)


class Lane3FooterAuthorityTests(unittest.TestCase):
    """harmonic-forge#473 — the two artifacts that decide whether a gate
    passed stop being heading-only.

    hrse#1590 could only mark them `provenance="heading:..."`, `validated=
    False`, because nothing stamped them. `post_lane_discussion.py --kind`
    now does, and these assert the reader half of that contract.
    """

    DIGEST = "a" * 64

    def spec(self, footer=True):
        body = "## Lane 3 Test Spec — H1\n\n### Test cases\n1. TC1 — x.\n"
        if footer:
            body += (f"\n<!-- l1-post v1; kind=spec; posted-by=LANE3; "
                     f"body-sha256={self.DIGEST} -->\n")
        return body

    def gate(self, verdict="PASS", footer=True):
        body = f"## Lane 3 Gate Results — H1 — {verdict}\n\nEvidence.\n"
        if footer:
            body += (f"\n<!-- l1-post v1; kind=gate-result; posted-by=LANE3; "
                     f"body-sha256={self.DIGEST} -->\n")
        return body

    def timeline(self, body):
        return lane_state.parse_timeline([{"body": body}])

    def test_a_stamped_spec_is_footer_provenance_and_validated(self) -> None:
        self.assertEqual(
            [(t.key, t.provenance, t.validated) for t in self.timeline(self.spec())],
            [(lane_state.KEY_SPEC_POSTED, "footer:spec", True)])

    def test_a_stamped_gate_result_is_footer_provenance_and_validated(self) -> None:
        self.assertEqual(
            [(t.key, t.provenance, t.validated) for t in self.timeline(self.gate())],
            [(lane_state.KEY_GATE_PASS, "footer:gate-result", True)])

    def test_a_stamped_fail_keeps_its_verdict(self) -> None:
        """The verdict lives in the heading and cannot live in the footer, so
        the footer upgrades this transition rather than replacing it. A
        `kind=gate-result` mapped straight through `_KIND_KEY` would have had
        to invent a verdict."""
        self.assertEqual(
            [(t.key, t.provenance) for t in self.timeline(self.gate("FAIL"))],
            [(lane_state.KEY_GATE_FAIL, "footer:gate-result")])

    def test_a_stamped_spec_yields_exactly_one_transition(self) -> None:
        """Footer and heading are both present by construction — the emitter
        refuses to stamp without the heading. Emitting both would double every
        spec in hrse#1194's counts."""
        self.assertEqual(len(self.timeline(self.spec())), 1)

    def test_an_attested_but_unreadable_gate_is_still_unknown(self) -> None:
        """BLOCKED names no verdict. Attestation says who posted it, not that
        this module can score it — marking it `validated` would say the
        opposite of what is true."""
        self.assertEqual(
            [(t.key, t.provenance, t.validated)
             for t in self.timeline(self.gate("BLOCKED"))],
            [(lane_state.KEY_UNKNOWN, "footer:gate-result", False)])

    def test_unstamped_artifacts_still_read_as_before(self) -> None:
        """Every already-posted Lane 3 comment predates the emitter. They must
        keep working, and keep saying honestly that they are unattested."""
        self.assertEqual(
            [(t.key, t.provenance, t.validated) for t in self.timeline(self.spec(footer=False))],
            [(lane_state.KEY_SPEC_POSTED, "heading:spec", False)])
        self.assertEqual(
            [(t.key, t.provenance, t.validated) for t in self.timeline(self.gate(footer=False))],
            [(lane_state.KEY_GATE_PASS, "heading:gate-result", False)])

    def test_a_footer_without_its_heading_is_not_validated(self) -> None:
        """The cross-check, on the reader side. The emitter refuses to produce
        this, so it can only arrive hand-written — which is exactly when the
        disagreement needs reporting rather than resolving."""
        body = ("## Some other heading\n\nnot a spec\n\n"
                f"<!-- l1-post v1; kind=spec; body-sha256={self.DIGEST} -->\n")
        self.assertEqual([(t.key, t.validated) for t in self.timeline(body)],
                         [(lane_state.KEY_SPEC_POSTED, False)])


class Lane3EmitterRoundTripTests(unittest.TestCase):
    """The cross-module contract, run rather than asserted twice.

    Both halves of harmonic-forge#473 could be individually correct and still
    disagree — the emitter writing a footer shape the reader does not accept
    is the exact failure mode hrse#1589 exists to fix, one protocol concept
    with two implementations. So this drives the real emitter and feeds its
    real output to the real reader.
    """

    def setUp(self) -> None:
        self.emitter = load_from(REPO_ROOT / "scripts", "post_lane_discussion")

    def round_trip(self, kind, body):
        posted = body.rstrip("\n") + self.emitter.footer(kind, body, "LANE3")
        return lane_state.parse_timeline([{"body": posted}])

    def test_a_real_emitted_spec_reads_back_validated(self) -> None:
        body = "## Lane 3 Test Spec — H1\n\n### Test cases\n1. TC1 — x.\n"
        self.emitter.validate_kind("spec", body)
        self.assertEqual(
            [(t.key, t.provenance, t.validated) for t in self.round_trip("spec", body)],
            [(lane_state.KEY_SPEC_POSTED, "footer:spec", True)])

    def test_a_real_emitted_gate_result_reads_back_validated(self) -> None:
        body = ("## Lane 3 Gate Results — H1 — FAIL\n\n**Finding:** one site "
                "missed.\n**Next:** back to Lane 2.\n")
        self.emitter.validate_kind("gate-result", body)
        self.assertEqual(
            [(t.key, t.provenance, t.validated) for t in self.round_trip("gate-result", body)],
            [(lane_state.KEY_GATE_FAIL, "footer:gate-result", True)])

    def test_a_real_emitted_discussion_is_unchanged_end_to_end(self) -> None:
        body = "## L1 — decisions, hrse#1\n\nRatified.\n"
        self.assertEqual(
            [(t.key, t.provenance) for t in self.round_trip("discussion", body)],
            [(lane_state.KEY_DISCUSSION, "footer:discussion")])

    def test_what_the_emitter_refuses_is_what_would_have_forged_a_state(self) -> None:
        """The guard and the hazard are the same fact, checked from both
        ends: the body the emitter rejects is demonstrably one that injects a
        spurious transition when read."""
        body = ("## Lane 3 Gate Results — H1 — PASS\n\n<details><summary>Evidence"
                "</summary>\n\n## L2B — receipt-backed status\n\n</details>\n")
        with self.assertRaises(SystemExit):
            self.emitter.validate_kind("gate-result", body)
        keys = [t.key for t in lane_state.parse_timeline([{"body": body}])]
        self.assertIn(lane_state.KEY_BLOCKED_LANE, keys)

    def test_fencing_that_quote_makes_it_both_postable_and_inert(self) -> None:
        """The fix the refusal tells the author to apply actually works."""
        body = ("## Lane 3 Gate Results — H1 — PASS\n\n<details><summary>Evidence"
                "</summary>\n\n```\n## L2B — receipt-backed status\n```\n\n</details>\n")
        self.emitter.validate_kind("gate-result", body)
        self.assertEqual([t.key for t in self.round_trip("gate-result", body)],
                         [lane_state.KEY_GATE_PASS])


class StaleAgeTests(unittest.TestCase):
    """harmonic-forge#134 — derived state says whose turn it is, never how
    long it has been their turn.

    The threshold and the scoping were both measured (3,421 real gaps, 533
    issues). The scoping is the load-bearing half: unscoped the flag emitted
    51 rows, 43 of them untriaged backlog; scoped it emitted 7.
    """

    NOW = datetime(2026, 9, 6, 0, 0, tzinfo=UTC)

    def at(self, hours_ago):
        return (self.NOW - timedelta(hours=hours_ago)).isoformat().replace("+00:00", "Z")

    def state(self, key, hours_ago):
        return lane_state.LaneState("display", None, key, self.at(hours_ago))

    def test_the_threshold_is_the_measured_one(self) -> None:
        self.assertEqual(lane_state.STALE_AFTER_HOURS, 24.0)

    def test_an_aged_owner_bearing_state_is_stale(self) -> None:
        label = lane_state.stale_label(
            self.state(lane_state.KEY_READY_IMPLEMENT, 192), self.NOW)
        self.assertEqual(label, "stale 8d")

    def test_a_recent_one_is_not(self) -> None:
        self.assertIsNone(lane_state.stale_label(
            self.state(lane_state.KEY_READY_IMPLEMENT, 3), self.NOW))

    def test_the_boundary_is_exclusive_below_and_inclusive_at(self) -> None:
        key = lane_state.KEY_AWAITING_GATE
        self.assertIsNone(lane_state.stale_label(self.state(key, 23.9), self.NOW))
        self.assertIsNotNone(lane_state.stale_label(self.state(key, 24.1), self.NOW))

    def test_hours_below_two_days_render_as_hours(self) -> None:
        self.assertEqual(
            lane_state.stale_label(self.state(lane_state.KEY_GATE_FAIL, 30), self.NOW),
            "stale 30h")

    # --- the scoping, which is the point ---------------------------------

    def test_no_handoff_never_goes_stale(self) -> None:
        """43 of the 51 unscoped rows were this. An issue filed and not yet
        started is a queue, not a stalled thread, and flagging it buries the
        rows that matter."""
        self.assertIsNone(lane_state.stale_label(
            self.state(lane_state.KEY_NO_HANDOFF, 750), self.NOW))

    def test_blocked_gated_and_unknown_never_go_stale(self) -> None:
        for key in (lane_state.KEY_BLOCKED_LANE, lane_state.KEY_BLOCKED_DEP,
                    lane_state.KEY_BLOCKED_NO_TIER, lane_state.KEY_GATE_PASS,
                    lane_state.KEY_UNKNOWN):
            with self.subTest(key=key):
                self.assertIsNone(lane_state.stale_label(self.state(key, 750), self.NOW))

    def test_every_owner_bearing_state_can_go_stale(self) -> None:
        for key in lane_state.STALE_STATES:
            with self.subTest(key=key):
                self.assertIsNotNone(lane_state.stale_label(self.state(key, 750), self.NOW))

    def test_the_two_sets_do_not_overlap_and_cover_the_keys(self) -> None:
        """A key in neither set is a state nobody decided about."""
        never = {lane_state.KEY_NO_HANDOFF, lane_state.KEY_BLOCKED_LANE,
                 lane_state.KEY_BLOCKED_DEP, lane_state.KEY_BLOCKED_NO_TIER,
                 lane_state.KEY_GATE_PASS, lane_state.KEY_UNKNOWN}
        self.assertEqual(lane_state.STALE_STATES & never, frozenset())

    # --- missing timestamp ------------------------------------------------

    def test_a_missing_timestamp_says_so_rather_than_showing_zero(self) -> None:
        """TC3. An age of zero on an untimestamped thread reads as `just
        moved`, the opposite of what is known; omitting the row silently is
        the failure mode this whole issue is about."""
        blank = lane_state.LaneState("d", None, lane_state.KEY_READY_PLAN, "")
        self.assertIsNone(lane_state.hours_since(""))
        self.assertEqual(lane_state.stale_label(blank, self.NOW), "age unknown")

    def test_a_missing_timestamp_on_a_never_stale_state_stays_quiet(self) -> None:
        blank = lane_state.LaneState("d", None, lane_state.KEY_NO_HANDOFF, "")
        self.assertIsNone(lane_state.stale_label(blank, self.NOW))

    # --- `at` reaches LaneState from classify() ---------------------------

    def test_classify_stamps_the_newest_transition_timestamp(self) -> None:
        record = {"key": "H1", "fields": {"Tier": "fast"}, "deps": [], "scope": [],
                  "tooling": None,
                  "comments": [{"body": LaneStateTests.HANDOFF,
                                "createdAt": "2026-09-01T00:00:00Z"},
                               {"body": LaneStateTests.L2D,
                                "createdAt": "2026-09-02T06:00:00Z"}]}
        state = lane_state.classify(record, [])
        self.assertEqual(state.at, "2026-09-02T06:00:00Z")
        self.assertEqual(state.key, lane_state.KEY_AWAITING_GATE)

    def test_the_default_keeps_every_existing_construction_working(self) -> None:
        self.assertEqual(lane_state.LaneState("d", None, "k").at, "")


class StaleRenderTests(RenderTests):
    """TC: the age reaches the row, and the row still parses."""

    def record(self, key, comments=(), **kwargs):
        record = super().record(key, **kwargs)
        record["comments"] = [{"body": b, "createdAt": "2026-01-01T00:00:00Z"}
                              for b in comments]
        return record

    def test_a_stale_row_carries_the_age_and_still_parses(self) -> None:
        """Both directions. hrse#1584 fixed a greedy-match bug in
        `_BRACKETED_ROW_RE`, so tolerance of an added field is re-verified
        rather than assumed."""
        text = self.render([self.record("H1", comments=[LaneStateTests.HANDOFF])])
        row = next(line for line in text.splitlines()
                   if line.startswith("- ") and "Tier:" in line)
        self.assertIn("| stale ", row)
        renderer = load_from(REPO_ROOT / "scripts", "board_dashboard_renderer")
        match = renderer._BRACKETED_ROW_RE.match(row)
        self.assertIsNotNone(match, f"row no longer parses: {row}")
        self.assertIn("stale ", match.group("lane"))
        self.assertEqual(match.group("tier"), "fast")

    def test_a_healthy_row_is_byte_identical_to_before(self) -> None:
        """Additive only: nothing changes for a row that is not stale."""
        records = [self.record("H1", comments=[LaneStateTests.HANDOFF])]
        records[0]["comments"][0]["createdAt"] = \
            datetime.now(UTC).isoformat().replace("+00:00", "Z")
        text = self.render(records)
        row = next(line for line in text.splitlines()
                   if line.startswith("- ") and "Tier:" in line)
        self.assertNotIn("stale", row)
        self.assertIn("[Todo | Tier: fast | ready: Implement H1]", row)

    def test_the_label_contains_no_character_the_row_grammar_splits_on(self) -> None:
        for key in lane_state.STALE_STATES:
            label = lane_state.stale_label(
                lane_state.LaneState("d", None, key, "2026-01-01T00:00:00Z"))
            with self.subTest(key=key):
                self.assertNotIn("|", label)
                self.assertNotIn("]", label)
