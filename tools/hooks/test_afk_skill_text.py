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
        belt = _afk_section(ROOT / "skills" / "afk" / "SKILL.md")
        belt_line = (ROOT / "skills" / "belt-and-suspenders" / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("skills/afk/SKILL.md", belt_line)
        self.assertRegex(belt_line, r"60-minute idle cutoff")
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
        self.assertIn("Every lane reads this section while afk is on.", belt)
        self.assertIn('"Do not invoke" above refers only to the Skill tool, which toggles nothing',
                      belt)

    def test_toggle_skills_reply_with_the_state_only(self):
        # harmonic-forge#1347: the reply names the hook's state, never what the skill does not do.
        for name in ("afk", "auto-ae"):
            text = (ROOT / "skills" / name / "SKILL.md").read_text(encoding="utf-8")
            reply = text.split("## What to do when this skill is invoked", 1)[1].split("\n## ", 1)[0]
            self.assertIn("this turn carries the", reply)
            self.assertIn("status` ", reply)
            self.assertIn("Never state an", reply)
            self.assertNotIn("04:11Z", reply)
            self.assertNotIn("16:16Z", reply)
            self.assertNotIn("toggles nothing", reply)
            self.assertNotIn("toggles nothing", text.split("---", 2)[1])

    def test_lane3_answers_a_chat_only_ae_neutrally(self):
        text = (ROOT / "skills" / "lane3-gate-platform" / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("a chat-only AE gets one line: `waiting for the posted AE on H<N>`", text)
        self.assertIn("deny_lane3_ae_self_post.py", text)
        self.assertIn("STOP immediately, execute nothing, report BLOCKED naming the gap", text)

    def test_codex_wires_no_afk_toggle(self):
        import json
        codex = json.loads((ROOT / ".codex" / "hooks.json").read_text(encoding="utf-8"))
        text = json.dumps(codex)
        self.assertNotIn("afk_toggle.py", text)
        self.assertIn("afk_defer.py", text)
        afk = (ROOT / "skills" / "afk" / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("Typed in Claude Code", afk)


if __name__ == "__main__":
    unittest.main()
