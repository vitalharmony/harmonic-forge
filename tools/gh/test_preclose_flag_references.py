"""harmonic-forge#713: a planner flag named in the docs must exist.

harmonic-forge#704 moved preclose_check.py from a copy that predated #701,
silently dropping `--gate`, `--findings`, `--envelope` and `--not-triggered`,
while agents/preclose-inspection.md and the preclose-check skill kept telling
callers to use them. Nothing failed, because nothing compared the two. This
does: every `--flag` the agent file or skill names alongside the planner must
be one the planner's argparse actually defines.
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PLANNER = ROOT / "tools" / "gh" / "preclose_check.py"
DOCS = (ROOT / "agents" / "preclose-inspection.md",
        ROOT / "skills" / "preclose-check" / "SKILL.md")
_DEFINED = re.compile(r'add_argument\(\s*"(--[a-z][a-z-]*)"')
_NAMED = re.compile(r"(?<![\w-])(--[a-z][a-z-]*)")


def planner_flags() -> set[str]:
    return set(_DEFINED.findall(PLANNER.read_text())) | {"--help"}


def named_flags(text: str) -> set[str]:
    """Flags on lines that invoke or name the planner, plus the lines of any
    fenced block that does."""
    named: set[str] = set()
    in_block, block, block_names_planner = False, [], False
    for line in text.splitlines():
        if line.strip().startswith("```"):
            if in_block and block_names_planner:
                for b in block:
                    named |= set(_NAMED.findall(b))
            in_block, block, block_names_planner = not in_block, [], False
            continue
        if in_block:
            block.append(line)
            block_names_planner |= "preclose_check.py" in line
        elif "preclose_check.py" in line:
            named |= set(_NAMED.findall(line))
    return named


class FlagReferenceTests(unittest.TestCase):
    def test_every_documented_planner_flag_exists(self) -> None:
        defined = planner_flags()
        for doc in DOCS:
            with self.subTest(doc=doc.name):
                missing = named_flags(doc.read_text()) - defined
                self.assertFalse(missing, f"{doc.name} names planner flags that do not exist: {sorted(missing)}")

    def test_the_check_sees_the_gate_flags(self) -> None:
        """Guards against a parser too loose to catch the #704 regression."""
        skill = named_flags(DOCS[1].read_text())
        self.assertTrue({"--gate", "--findings", "--complete"} <= skill, skill)

    def test_it_would_have_caught_the_704_regression(self) -> None:
        pre_701 = PLANNER.read_text().replace('"--gate"', '"--xgate"')
        self.assertNotIn("--gate", set(_DEFINED.findall(pre_701)))
        self.assertIn("--gate", named_flags(DOCS[1].read_text()))


if __name__ == "__main__":
    unittest.main()
