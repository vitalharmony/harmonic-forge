#!/usr/bin/env python3
"""AC6' registry test (harmonic-forge#691, mirroring hrse#1882/`8c524d1c`'s
pattern for `_ACCEPT_WRITERS`): `{files in this repo that emit an
"l1-post v1" footer} == {files in this repo that import belt_candidates}`.

The pre-rescope design instrumented `l2_post.py` as a writer but never
`post_lane_discussion.py` -- a second, undercovered writer that
was silent about being undercovered, exactly the class of drift hrse#1882
made a test for rather than a third prose reminder. This is that same
fix, generalized to files rather than a candidate-type dict: an emitter is
found by AST (a real f-string whose literal text contains
`<!-- l1-post v1;`, never a docstring's plain example of one), so a FUTURE
fourth writer in this repo that stamps the footer without importing the
recorder fails this test the moment it lands, rather than needing a fifth
prose reminder.

F706 moved the two HRSE-owned writers and their registry responsibility here,
so this one scan now covers all three platform-owned marker emitters.

Run: python3 tools/gh/test_belt_candidate_registry.py
"""
from __future__ import annotations

import ast
import unittest
from pathlib import Path

_GH_DIR = Path(__file__).resolve().parent

#: Files this registry does not judge:
#:   - every `test_*.py`: a test builds an expected-footer f-string to
#:     assert against, or mocks the recorder, neither of which is this
#:     repo declaring a NEW production writer.
#:   - `watch_lane_posts.py`: imports `belt_candidates` to READ
#:     (`read_candidates`), never to record a post -- the reader, not a
#:     fourth writer. Its own coverage is `ReadQueueCandidatesTests` in
#:     `test_watch_lane_posts.py`, not this registry.
_EXCLUDE_STEMS = {"watch_lane_posts"}


def _is_emitter(path: Path) -> bool:
    """True when `path` builds an f-string whose literal text contains
    `l1-post v1;` -- i.e. it actually CONSTRUCTS the footer, interpolating
    a `kind=`/`posted-by=` value into it, as opposed to merely quoting one
    in a docstring example or a regex pattern (both plain `ast.Constant`
    strings, never `ast.JoinedStr`)."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (SyntaxError, OSError):
        return False
    for node in ast.walk(tree):
        if not isinstance(node, ast.JoinedStr):
            continue
        for part in node.values:
            if isinstance(part, ast.Constant) and isinstance(part.value, str):
                if "l1-post v1;" in part.value:
                    return True
    return False


def _imports_belt_candidates(path: Path) -> bool:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (SyntaxError, OSError):
        return False
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(alias.name == "belt_candidates" for alias in node.names):
                return True
        elif isinstance(node, ast.ImportFrom):
            if node.module == "belt_candidates":
                return True
    return False


def _candidate_files() -> list[Path]:
    return [
        p for p in sorted(_GH_DIR.glob("*.py"))
        if p.stem not in _EXCLUDE_STEMS and not p.stem.startswith("test_")
    ]


class BeltCandidateRegistryTests(unittest.TestCase):
    def test_every_l1_post_footer_emitter_in_this_repo_imports_belt_candidates(self):
        emitters = {p.name for p in _candidate_files() if _is_emitter(p)}
        importers = {p.name for p in _candidate_files() if _imports_belt_candidates(p)}
        self.assertEqual(
            emitters, importers,
            "a file in tools/gh/ emits the `l1-post v1;` footer but does not "
            "import belt_candidates (or vice versa) -- every marker-posting "
            "tool must call belt_candidates.record_candidate on every "
            "successful post (harmonic-forge#691 AC1'/AC6')",
        )

    def test_all_three_platform_writers_are_known_emitters_and_importers(self):
        """Sanity floor: the scan above must not pass vacuously because it
        found zero files on either side."""
        emitters = {p.name for p in _candidate_files() if _is_emitter(p)}
        importers = {p.name for p in _candidate_files() if _imports_belt_candidates(p)}
        for name in ("l1_post.py", "l2_post.py", "post_lane_discussion.py"):
            self.assertIn(name, emitters)
            self.assertIn(name, importers)


if __name__ == "__main__":
    unittest.main()
