"""harmonic-forge#1439: the README's Operator commands index tracks lane-shorthand.md.

The README indexes each operator command by linking its `## ` section in
`rules/lane-shorthand.md`; it never copies the rule text. This guard fails when
the rule file gains a command section with no README row, or a README link
points at an anchor no heading produces.
"""
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
README = ROOT / "README.md"
SHORTHAND = ROOT / "rules" / "lane-shorthand.md"
LINK_PREFIX = "rules/lane-shorthand.md#"
SKILLS = ("skills/afk/SKILL.md", "skills/auto-ae/SKILL.md")

# `## ` sections that are reference material, not an operator command line.
EXEMPT = {
    "Derived lane states and `kind=rework`",  # explains lane_state keys; the operator types none of it
}


def slug(heading: str) -> str:
    """GitHub's heading anchor: lowercase, drop everything but letters, digits,
    spaces, hyphens and underscores, then each space becomes a hyphen (no
    collapsing, so ` — ` yields `--`)."""
    kept = re.sub(r"[^\w\- ]", "", heading.strip().lower())
    return kept.replace(" ", "-")


def headings(shorthand_text: str) -> list[str]:
    return [line[3:].strip() for line in shorthand_text.splitlines() if line.startswith("## ")]


def operator_section(readme_text: str) -> str:
    match = re.search(r"^## Operator commands\n(.*?)(?=^## |\Z)", readme_text, re.M | re.S)
    if match is None:
        raise AssertionError("README has no `## Operator commands` section")
    return match.group(1)


def linked_anchors(section: str) -> list[str]:
    return re.findall(re.escape(LINK_PREFIX) + r"([\w\-]+)\)", section)


def coverage_problems(readme_text: str, shorthand_text: str) -> list[str]:
    """Every non-exempt heading linked exactly once; every link resolves."""
    anchors = linked_anchors(operator_section(readme_text))
    known = {slug(h) for h in headings(shorthand_text)}
    problems = []
    for heading in headings(shorthand_text):
        if heading in EXEMPT:
            continue
        count = anchors.count(slug(heading))
        if count != 1:
            problems.append(f"`## {heading}` has {count} README rows, expected 1")
    problems += [f"dead anchor #{a}" for a in anchors if a not in known]
    return problems


class ReadmeOperatorCommandsTests(unittest.TestCase):
    def setUp(self):
        self.readme = README.read_text(encoding="utf-8")
        self.shorthand = SHORTHAND.read_text(encoding="utf-8")

    def test_every_command_section_has_one_row(self):
        self.assertEqual(coverage_problems(self.readme, self.shorthand), [])

    def test_every_link_resolves(self):
        known = {slug(h) for h in headings(self.shorthand)}
        anchors = linked_anchors(operator_section(self.readme))
        self.assertTrue(anchors)
        self.assertEqual([a for a in anchors if a not in known], [])

    def test_skill_links_exist(self):
        section = operator_section(self.readme)
        for path in SKILLS:
            self.assertIn(f"]({path})", section)
            self.assertTrue((ROOT / path).is_file(), path)

    def test_exempt_headings_exist(self):
        """A stale exemption would hide nothing and mislead the next reader."""
        self.assertEqual(EXEMPT - set(headings(self.shorthand)), set())

    def test_new_command_section_without_a_row_fails(self):
        extra = self.shorthand + "\n## `NEW` — an added command\n\ntext\n"
        problems = coverage_problems(self.readme, extra)
        self.assertEqual(problems, ["`## `NEW` — an added command` has 0 README rows, expected 1"])

    def test_slug_matches_github(self):
        self.assertEqual(slug("`close` — one compound instruction, not three approvals"),
                         "close--one-compound-instruction-not-three-approvals")
        self.assertEqual(slug("`FORCE` — the operator's preclose override"),
                         "force--the-operators-preclose-override")
        self.assertEqual(slug("Operator → lane triggers"), "operator--lane-triggers")

    def test_current_slugs_are_pinned(self):
        """A slug-rule change breaks visibly here, not as a silent dead link."""
        self.assertEqual([slug(h) for h in headings(self.shorthand)], [
            "lane-status-tokens",
            "derived-lane-states-and-kindrework",
            "close--one-compound-instruction-not-three-approvals",
            "eoq--end-of-queue-the-default-for-any-unmarked-mid-turn-message",
            "now--interrupt",
            "batch--pre-authorize-a-multi-issue-merge-pass",
            "imc--per-issue-waiver-of-the-tooling-exception-review",
            "force--the-operators-preclose-override",
            "park--the-operator-lets-an-open-issue-leave-a-lanes-task-list",
            "scan--the-operator-allows-one-bounded-gh-scan",
            "ui--the-operators-approval-of-a-rendered-ui-change",
            "operator--lane-triggers",
            "repo-prefixes",
        ])


if __name__ == "__main__":
    sys.exit(unittest.main())
