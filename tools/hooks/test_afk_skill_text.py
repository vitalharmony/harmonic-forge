"""harmonic-forge#1338 AC4/AC6: each lane's skill states its afk behavior."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _afk_section(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    match = re.search(r"^## While afk is on.*?(?=^## |\Z)", text, re.M | re.S)
    return match.group(0) if match else ""


class SkillText(unittest.TestCase):
    def test_each_lane_states_its_afk_behavior(self):
        belt = _afk_section(ROOT / "skills" / "belt-and-suspenders" / "SKILL.md")
        lane3 = _afk_section(ROOT / "skills" / "lane3-gate-platform" / "SKILL.md")
        self.assertTrue(belt, "belt skill has no afk paragraph")
        self.assertTrue(lane3, "lane3-gate-platform has no afk paragraph")
        self.assertRegex(belt, r"60 minutes")
        self.assertRegex(belt, r"Monitor")
        lane2 = next((l for l in belt.splitlines() if l.startswith("- **Lane 2:**")), "")
        self.assertIn("L2B", lane2)
        self.assertIn("ntfy.py push", lane2)
        for text in (lane3,):
            self.assertIn("L3B", text)
            self.assertIn("ntfy.py push", text)
            self.assertRegex(text, r"(?s)Tier P spec .*never an auto-AE")
        self.assertRegex(belt, r"(?s)Tier P spec .*never an auto-AE")


if __name__ == "__main__":
    unittest.main()
