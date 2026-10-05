"""harmonic-forge#909: every registry entry's `file` names the file its
annotated span is actually in.

`check_rule_drift.py` matches spans by id and `text_sha` and never reads
`file`, so a span moved between rules files leaves a stale `file` that nothing
catches. This is that check, plus the always-loaded rules #909 kept in
`rules/lane-shorthand.md` when it moved the tool reference out."""
import re
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SHORTHAND = ROOT / "rules" / "lane-shorthand.md"


def _span_files() -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    for path in [*sorted((ROOT / "rules").glob("*.md")), ROOT / "3-lane-protocol.md"]:
        for rid in re.findall(r"<!-- (R-\d{4}) -->", path.read_text(encoding="utf-8")):
            found.setdefault(rid, set()).add(path.relative_to(ROOT).as_posix())
    return found


class RegistryFileMatchesSpan(unittest.TestCase):
    def test_each_annotated_id_is_registered_against_the_file_it_is_in(self):
        registry = tomllib.loads((ROOT / "tools" / "rules" / "registry.toml").read_text(encoding="utf-8"))
        entries = next(v for v in registry.values() if isinstance(v, list))
        where = _span_files()
        wrong = [(e["id"], e["file"], sorted(where[e["id"]]))
                 for e in entries if e["id"] in where and where[e["id"]] != {e["file"]}]
        self.assertEqual(wrong, [], "registry `file` disagrees with where the span is")


class ShorthandKeepsItsUnconsultedRules(unittest.TestCase):
    def test_the_rules_that_must_fire_unconsulted_stay_always_loaded(self):
        text = SHORTHAND.read_text(encoding="utf-8")
        for rid in ("R-0208", "R-0209", "R-0117", "R-0118"):
            self.assertIn(rid, text)
        for rid in ("R-0117", "R-0118"):
            self.assertIn(f"<!-- {rid} -->", text)
            self.assertIn(f"<!-- /{rid} -->", text)
        self.assertEqual(text.count("rules/lane-tooling-reference.md"), 1, "one pointer for the moved section")

    def test_the_ratchet_gate_passes_on_this_checkout(self):
        # The gate measures this checkout's own rules/ files (not the link's
        # target), so growth past the committed baseline without an
        # [[increase]] entry fails here, not on the next commit to main.
        import subprocess, sys  # noqa: E401, PLC0415
        result = subprocess.run([sys.executable, str(ROOT / "tools" / "memory" / "context_budget_ratchet.py"),
                                 "--repo", str(ROOT), "--gate"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
