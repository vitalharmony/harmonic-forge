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
    """Flags in any paragraph or fenced block that names the planner.

    Paragraph-level, not line-level (harmonic-forge#713 preclose finding): the
    agent file names the planner in wrapped prose, so a reflow that moved
    `--gate` onto the next line would otherwise make it invisible here and
    let the #704 regression pass silently again.
    """
    named: set[str] = set()
    chunks: list[list[str]] = [[]]
    in_block = False
    for line in text.splitlines():
        fence = line.strip().startswith("```")
        if fence or (not in_block and not line.strip()):
            chunks.append([])
            if fence:
                in_block = not in_block
            continue
        chunks[-1].append(line)
    for chunk in chunks:
        body = "\n".join(chunk)
        if "preclose_check.py" in body:
            named |= set(_NAMED.findall(body))
    return named


class FlagReferenceTests(unittest.TestCase):
    def test_every_documented_planner_flag_exists(self) -> None:
        defined = planner_flags()
        for doc in DOCS:
            with self.subTest(doc=doc.name):
                missing = named_flags(doc.read_text()) - defined
                self.assertFalse(missing, f"{doc.name} names planner flags that do not exist: {sorted(missing)}")

    def test_the_check_sees_the_gate_flags(self) -> None:
        """Guards against a parser too loose to catch the #704 regression, in
        BOTH documents -- AC4 names the agent file specifically."""
        skill = named_flags(DOCS[1].read_text())
        self.assertTrue({"--gate", "--findings", "--complete"} <= skill, skill)
        agent = named_flags(DOCS[0].read_text())
        self.assertIn("--gate", agent)

    def test_a_flag_wrapped_onto_the_next_prose_line_is_still_seen(self) -> None:
        text = ("The planner (`tools/gh/preclose_check.py`) evaluates it; run\n"
                "`--gate` to check.\n\nAn unrelated paragraph with `--other`.\n")
        self.assertEqual(named_flags(text), {"--gate"})

    def test_it_would_have_caught_the_704_regression(self) -> None:
        pre_701 = PLANNER.read_text().replace('"--gate"', '"--xgate"')
        self.assertNotIn("--gate", set(_DEFINED.findall(pre_701)))
        for doc in DOCS:
            self.assertIn("--gate", named_flags(doc.read_text()), doc.name)


if __name__ == "__main__":
    unittest.main()
