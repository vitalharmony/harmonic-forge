#!/usr/bin/env python3
"""harmonic-forge#651 AC8: assert the belt docs and watch_lane_posts.py's own
module docstring both match CANONICAL_BELTS -- byte-for-byte, as PARSED
argparse values, never a hand-typed comparison that could itself drift.

Two documents describe the same table (`watch_lane_posts.CANONICAL_BELTS`):
`skills/belt-and-suspenders/SKILL.md` (the operator-facing arming
instructions) and `watch_lane_posts.py`'s own module docstring (the
`Usage` block anyone reading the script sees first). AC1's whole point is
that there is now exactly ONE canonical command per lane/mode; a doc that
silently drifts from the table -- or from its sibling doc -- reintroduces
the 2026-09-14 incident's proximate cause one level up: a session that
copies a stale example from whichever of the two docs it happened to read.

harmonic-forge#659 cut `SKILL.md` to "run `tools/lane/belt_plan.py`, make
exactly the calls it prints" and moved its former body to `DESIGN.md`. So the
operator-facing commands are now `belt_plan.py`'s output, compared here against
the table; `DESIGN.md` keeps the per-lane examples and is still synced; and
`SKILL.md` must carry no command at all.

This is a doc-sync check, not a runtime-behavior test: it fails loudly the
moment anyone hand-edits one of the two docs (or the table) without the
others, which is exactly the property `tools/gh/test_watch_lane_posts.py`'s
own `BeltSkillDocSyncTests` establishes for a different pair of facts.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import watch_lane_posts  # noqa: E402

_SKILL_DIR = (Path(__file__).resolve().parent.parent.parent
              / "skills" / "belt-and-suspenders")
_SKILL_MD = _SKILL_DIR / "SKILL.md"
_DESIGN_MD = _SKILL_DIR / "DESIGN.md"
_BELT_PLAN = Path(__file__).resolve().parent.parent / "lane" / "belt_plan.py"

# Both docs' example commands are written as `python3 <script> <flags...>`,
# optionally split across lines with a trailing `\` continuation (the
# module docstring's style) -- never as a single physical line in either
# doc, so the continuation is joined away BEFORE matching, rather than
# handled inside the regex itself: `[^\n]` happily consumes a bare `\`
# (it is not a newline), which starves a `\\\n` alternative positioned
# after it of the backslash it needs -- the joined-first approach sidesteps
# that class of regex bug entirely instead of tuning around it.
_COMMAND_RE = re.compile(
    r"python3\s+(?:watch_lane_posts\.py|~/harmonic-forge/tools/gh/watch_lane_posts\.py)"
    r"([^\n`]*)"
)


def _extract_commands(text: str) -> list[list[str]]:
    """Every `python3 .../watch_lane_posts.py <flags>` example in `text`,
    each tokenized into an argv list."""
    joined = text.replace("\\\n", " ")
    return [match.group(1).split() for match in _COMMAND_RE.finditer(joined)]


def _canonical_entries() -> list[tuple[str, list[str]]]:
    """Every `CANONICAL_BELTS` entry, flattened to `(label, argv)` in table
    order -- lane "1", "2", "3", matching the order the docs present them in.
    Lane 3's sweep entry is retired (harmonic-forge#659)."""
    entries = []
    for lane in ("1", "2", "3"):
        # harmonic-forge#917: one entry per lane per workspace, so a second entry
        # is another workspace's belt, not a sweep.
        for entry in watch_lane_posts.CANONICAL_BELTS[lane]:
            entries.append((f"lane{lane}-{entry['workspace']}", entry["argv"]))
    return entries


def _normalize(parser, argv: list[str]) -> dict:
    ns = parser.parse_args(argv)
    d = vars(ns).copy()
    d["watch"] = sorted(d.get("watch") or [])
    return d


class BeltSkillMatchesCanonicalTableTests(unittest.TestCase):

    def setUp(self):
        self.parser = watch_lane_posts._build_parser()
        self.canonical = _canonical_entries()

    def test_belt_plan_prints_exactly_the_canonical_command_per_lane(self):
        """harmonic-forge#659 AC2: `belt_plan.py`'s Monitor command is the
        table's argv, as run -- a subprocess, so the printed JSON is what is
        checked, not a function a refactor could route around."""
        # harmonic-forge#917: belt_plan.py prints the entry for the workspace of the
        # checkout it runs in, so it runs from a fixture `vh` checkout -- never this
        # test's own checkout, which on a CI runner is registered nowhere.
        with tempfile.TemporaryDirectory() as tmp:
            checkout = Path(tmp) / "alpha"
            checkout.mkdir()
            manifest = Path(tmp) / "projects.toml"
            manifest.write_text(
                f'[[project]]\nname = "alpha"\nprefix = "A"\nrepo = "o/alpha"\n'
                f'account = "vitalharmony"\npath = "{checkout}"\nonboarded = true\n'
                'workspace = "vh"\n[project.protocol]\n'
                'worktree_name = "{checkout}-lane{lane}"\nl1_post_task = "l1-post"\n'
                'lane_comment_task = "lane-comment"\ngate_checkout_task = "gate-checkout"\n'
                'lane3_begin_task = "lane3-begin"\nlane3_end_task = "lane3-end"\n'
                'runs_lane3 = true\n', encoding="utf-8")
            for lane in ("1", "2", "3"):
                with self.subTest(lane=lane):
                    env = {**os.environ, "LANE": lane,
                           "FORGE_PROJECTS_MANIFEST": str(manifest)}
                    out = subprocess.run([sys.executable, str(_BELT_PLAN)], env=env,
                                         cwd=str(checkout), capture_output=True,
                                         text=True, check=True)
                    plan = json.loads(out.stdout)
                    argv = _extract_commands(plan["monitor"]["command"])
                    self.assertEqual(len(argv), 1, plan["monitor"]["command"])
                    vh = next(e for e in watch_lane_posts.CANONICAL_BELTS[lane]
                              if e["workspace"] == "vh")
                    self.assertEqual(argv[0], vh["argv"])
                    self.assertEqual(plan["loop"],
                                     {"skill": "loop",
                                      "args": "10m proactively find work to do"})

    def test_skill_md_carries_no_command(self):
        """harmonic-forge#659 AC4: a command retyped in the skill is a second
        source; the skill points at belt_plan.py and nothing else."""
        self.assertEqual(_extract_commands(_SKILL_MD.read_text(encoding="utf-8")), [])

    def test_design_md_has_exactly_the_canonical_commands(self):
        text = _DESIGN_MD.read_text(encoding="utf-8")
        extracted = _extract_commands(text)
        self.assertEqual(
            len(extracted), len(self.canonical),
            f"DESIGN.md must show exactly {len(self.canonical)} belt/sweep "
            f"commands (one per CANONICAL_BELTS entry); found {len(extracted)}: "
            f"{extracted}")
        for (label, canonical_argv), argv in zip(self.canonical, extracted):
            with self.subTest(entry=label):
                self.assertEqual(
                    _normalize(self.parser, argv),
                    _normalize(self.parser, canonical_argv),
                    f"DESIGN.md's {label} command does not match "
                    f"CANONICAL_BELTS[...] once parsed")

    def test_module_docstring_has_exactly_the_canonical_commands(self):
        text = watch_lane_posts.__doc__ or ""
        extracted = _extract_commands(text)
        self.assertEqual(
            len(extracted), len(self.canonical),
            f"watch_lane_posts.py's module docstring must show exactly "
            f"{len(self.canonical)} belt/sweep commands; found "
            f"{len(extracted)}: {extracted}")
        for (label, canonical_argv), argv in zip(self.canonical, extracted):
            with self.subTest(entry=label):
                self.assertEqual(
                    _normalize(self.parser, argv),
                    _normalize(self.parser, canonical_argv),
                    f"watch_lane_posts.py docstring's {label} command does "
                    f"not match CANONICAL_BELTS[...] once parsed")

    def test_skill_md_and_docstring_examples_agree_with_each_other(self):
        """Not just each vs. the table -- each vs. the OTHER doc, so a typo
        that happens to also land in CANONICAL_BELTS (impossible, since the
        table is code, but the point of this test is not to rely on that)
        cannot hide two docs quietly disagreeing with each other."""
        skill_extracted = _extract_commands(_DESIGN_MD.read_text(encoding="utf-8"))
        doc_extracted = _extract_commands(watch_lane_posts.__doc__ or "")
        self.assertEqual(len(skill_extracted), len(doc_extracted))
        for skill_argv, doc_argv in zip(skill_extracted, doc_extracted):
            self.assertEqual(
                _normalize(self.parser, skill_argv),
                _normalize(self.parser, doc_argv))

    def test_no_retired_sweep_example_remains_in_any_doc(self):
        """--sweep-for l1 (harmonic-forge#640) and --sweep-for l3
        (harmonic-forge#659) are both retired -- no doc may show either as a
        runnable example, prose retirement notes excepted (those don't match
        _COMMAND_RE, which requires the script-invocation prefix)."""
        for path_or_text, name in (
            (_SKILL_MD.read_text(encoding="utf-8"), "SKILL.md"),
            (_DESIGN_MD.read_text(encoding="utf-8"), "DESIGN.md"),
            (watch_lane_posts.__doc__ or "", "watch_lane_posts.py docstring"),
        ):
            for argv in _extract_commands(path_or_text):
                with self.subTest(doc=name, argv=argv):
                    self.assertNotIn(
                        "--sweep-for", argv,
                        f"{name} still shows a runnable --sweep-for example")

    def test_lane3_table_holds_only_the_queue_belt(self):
        """harmonic-forge#659 AC1; one queue belt per workspace since harmonic-forge#917."""
        entries = watch_lane_posts.CANONICAL_BELTS["3"]
        self.assertEqual([e["workspace"] for e in entries],
                         watch_lane_posts.manifest_workspaces())
        for entry in entries:
            self.assertIn("--queue-for", entry["argv"])
            self.assertNotIn("--sweep-for", entry["argv"])


class ObligationsTableMatchesOwesTests(unittest.TestCase):
    """harmonic-forge#851 AC2.3: `SKILL.md` rule 9's table is the code's
    `OWES` table, row for row. A session acts on the doc; the belt prints
    the code. They must never name different obligations."""

    _ROW_RE = re.compile(r"^\s*\|\s*(l[123])\s*\|\s*`([\w-]+)`\s*\|", re.MULTILINE)

    def test_rule_9_rows_equal_owes(self):
        documented = set(self._ROW_RE.findall(_SKILL_MD.read_text(encoding="utf-8")))
        expected = {(lane, owes) for lane, kinds in watch_lane_posts.OWES.items()
                    for owes in kinds.values()}
        expected.add(("l2", watch_lane_posts.PLAN_FIRST_OWES))
        self.assertEqual(documented, expected)

    #: Reforge ruling, fix 4: each row's action is bound to its `(lane, owes)`
    #: key, so swapping two rows' steps fails rather than passing on the same
    #: vocabulary. One phrase per row that only that row's step contains.
    _ACTION_PHRASE = {
        ("l3", "spec"): "fetch_lane1_context.py",
        ("l3", "gate"): "lane3-begin",
        ("l3", "sweep-missing"): "L3B",
        ("l2", "implement"): "implement from the handoff",
        ("l2", "plan"): "l2_post.py --kind plan",
        ("l2", "fix"): "do the work on the branch",
        ("l1", "plan-review"): "Implementation Spec",
        ("l1", "spec-review"): "Review Lane 3's spec",
    }
    _FULL_ROW_RE = re.compile(r"^\s*\|\s*(l[123])\s*\|\s*`([\w-]+)`\s*\|(.*)\|\s*$", re.MULTILINE)

    def _rows(self) -> dict[tuple[str, str], str]:
        return {(lane, owes): step for lane, owes, step
                in self._FULL_ROW_RE.findall(_SKILL_MD.read_text(encoding="utf-8"))}

    def test_each_row_carries_its_own_step(self):
        rows = self._rows()
        self.assertEqual(set(rows), set(self._ACTION_PHRASE))
        for key, phrase in self._ACTION_PHRASE.items():
            with self.subTest(row=key):
                self.assertIn(phrase, rows[key])
                for other, other_phrase in self._ACTION_PHRASE.items():
                    if other != key:
                        self.assertNotIn(other_phrase, rows[key])

    def test_every_l2_post_kind_named_is_a_valid_choice(self):
        """Reforge ruling, fix 3: a `l2_post.py --kind <x>` the doc names must
        be one `l2_post.py` accepts (`spec` is not, and argparse exits 2)."""
        source = (_SKILL_MD.parents[2] / "tools" / "gh" / "l2_post.py").read_text(encoding="utf-8")
        found = re.search(r'"--kind"[^)]*?choices=\(([^)]*)\)', source, re.DOTALL)
        self.assertIsNotNone(found, "l2_post.py --kind choices not found")
        choices = set(re.findall(r'"([\w-]+)"', found.group(1)))
        named = re.findall(r"l2_post\.py --kind ([\w-]+)", _SKILL_MD.read_text(encoding="utf-8"))
        self.assertTrue(named)
        for kind in named:
            with self.subTest(kind=kind):
                self.assertIn(kind, choices)


if __name__ == "__main__":
    unittest.main()
