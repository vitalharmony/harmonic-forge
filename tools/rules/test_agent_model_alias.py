"""Every agent definition names its model by alias, never a full ID (harmonic-forge#937).

A full `claude-` ID pins one release and goes stale at the next one; the
advisory agents ran `claude-opus-5` after `claude-opus-5-5` shipped. An alias
(`opus` …) resolves to the latest model of that family. A missing `model:`
falls back to Sonnet, so it fails here too.
"""

import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
AGENTS_DIR = _ROOT / "agents"
ALLOWED = {"opus", "sonnet", "haiku", "fable", "inherit"}


def _model_of(path: Path) -> str | None:
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    for line in lines[1:]:
        if line.strip() == "---":
            return None
        key, sep, value = line.partition(":")
        if sep and key.strip() == "model":
            return value.strip().strip("'\"")
    return None


class AgentModelAliasTest(unittest.TestCase):
    def test_every_agent_uses_a_model_alias(self):
        files = sorted(AGENTS_DIR.glob("*.md"))
        self.assertTrue(files, f"no agent definitions found under {AGENTS_DIR}")
        for path in files:
            model = _model_of(path)
            with self.subTest(agent=path.name):
                self.assertIn(
                    model,
                    ALLOWED,
                    f"{path.name}: model is {model!r}; use an alias "
                    f"({', '.join(sorted(ALLOWED))}), not a full model ID",
                )


if __name__ == "__main__":
    unittest.main()
