"""`mise.toml` is valid TOML (harmonic-forge#433).

The failure this guards is **a test task reporting success without running**.
A `[tasks.*]` description containing an unescaped `"` inside a double-quoted
TOML string makes mise fail at config-parse time, so `mise run <anything>`
never executes — and in a combined pipeline the surrounding commands' output
makes the run look green. That happened live in this issue: `mise run
scripts-test` "passed" while mise had refused to parse the file at all.

The hrse half of this issue carries the identical guard; the defect is
per-repo because each repo has its own `mise.toml`.
"""
from __future__ import annotations

import tomllib
import unittest
from pathlib import Path

_MISE_TOML = Path(__file__).resolve().parents[2] / "mise.toml"


class MiseTomlParsesTests(unittest.TestCase):
    def test_mise_toml_is_valid_toml(self):
        try:
            tomllib.loads(_MISE_TOML.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as exc:
            self.fail(f"{_MISE_TOML} is not valid TOML — every `mise run` "
                      f"would fail at config-parse time: {exc}")

    def test_the_guard_can_actually_fail(self):
        """A guard that cannot fire is the same class of defect it guards.

        This is the real bug's shape: an unescaped `"` inside a
        double-quoted description.
        """
        bad = '[tasks.x]\ndescription = "Exits 1 only on "work that exists nowhere else"."\n'
        with self.assertRaises(tomllib.TOMLDecodeError):
            tomllib.loads(bad)


if __name__ == "__main__":
    unittest.main()
