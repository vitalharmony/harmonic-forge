#!/usr/bin/env python3
"""Run every test in this repo (harmonic-forge#293).

Why this exists: the repo had 14 test files / 336 passing tests and nothing
that ran them. `mise run check` was a `py_compile` syntax pass whose own
description claimed "no test suite exists yet". A regression in
`tools/hooks/` -- the lane-governance layer -- would not fail loudly, it
would quietly stop enforcing something, which is the failure mode least
likely to be noticed by hand.

Deliberately dependency-free (`unittest`, stdlib only), so CI is checkout +
run python with no install step. That was a real choice, not the default:
13 of the 14 files were already plain unittest, and the one exception
(test_wrapper_parity.py) was converted rather than adding pytest as this
repo's first-ever declared dependency. See #293.

`unittest discover` cannot start at `tools/` -- it is not an importable
package -- so each leaf directory is discovered separately, and each is
added to sys.path because the test files import their subjects by bare
module name.

Single source of truth for both `mise run test` and CI: they invoke this
script rather than each spelling out discovery, so the two cannot drift.
"""

from __future__ import annotations

import contextlib
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
# `rules` was missing until harmonic-forge#447's second pass: the registry
# suite had been shipped and reported as passing while `mise run check` never
# loaded it. A suite the verification gate does not run is the
# `narrative_budget_check.py` failure class this repo has already deleted a
# tool over — passing locally is not passing. Adding a new leaf directory
# under `tools/` means adding it here too; nothing else discovers it.
#
# harmonic-forge#594 preclose finding: entries may reach outside `tools/`.
# `skills/belt-and-suspenders/test_skill_text.py` guards the belt protocol's
# own doc and had been RED since #590 merged, unnoticed, because nothing ran
# it -- which is the condition under which a vacuous assertion in the sibling
# `tools/gh` suite survived review. A doc guard nobody runs is the same
# failure class this comment already describes one level up.
TEST_DIRS = ["gh", "hooks", "lane", "memory", "onboard", "rules",
             "../skills/belt-and-suspenders", "../skills/verification-gate"]
PATTERN = "test_*.py"


def _test_files() -> list[Path]:
    return sorted(p for d in TEST_DIRS for p in (ROOT / d).glob(PATTERN))


def _loaded_modules(suite: unittest.TestSuite) -> set[str]:
    """Module name of every test case actually in the suite."""
    modules: set[str] = set()
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            modules |= _loaded_modules(item)
        else:
            modules.add(type(item).__module__.rsplit(".", 1)[-1])
    return modules


@contextlib.contextmanager
def redirected_belt_candidates_dir(tmp_dir: Path):
    """harmonic-forge#691 preclose finding: `tools/gh/l2_post.py`'s belt-
    candidate recorder call (`belt_candidates.record_candidate(...)`, no
    `base_dir`) always targets `belt_candidates.DEFAULT_CANDIDATES_DIR` --
    the operator's real `~/.claude/state/belt/candidates/` -- unless a
    caller redirects it. `tools/gh/test_l2_post.py` drives `l2_post.main()`
    end to end through a successful post and does not mock this call, so
    every run of this suite writes a real file into the operator's live
    belt state unless something redirects the module's default first.

    This repo is deliberately pytest-free (harmonic-forge#293 -- see this
    file's own module docstring), so there is no conftest.py
    autouse-fixture choke point the way HRSE2's `scripts/conftest.py`
    provides for its pytest suite (see the companion vitalharmony/hrse#1926,
    `_belt_candidates_real_dir_untouched`). `main()` below is this repo's
    actual single choke point instead -- `build_suite()` already collects
    every `test_*.py` under every `TEST_DIRS` entry into one
    `unittest.TestSuite` that runs exactly once here (`mise run check` and
    CI both call only this script, never a test file directly) -- so
    patching the one shared `belt_candidates` module object for the
    duration of that one run covers every current and future test in the
    suite, whether or not the test itself imports `belt_candidates`.

    Known residual gap, accepted rather than fixed here: invoking a single
    test file directly (e.g. `python3 tools/gh/test_l2_post.py`, which
    several files in this suite support via their own
    `if __name__ == "__main__"`) bypasses this script and is NOT covered --
    only runs through `tools/run_tests.py` are. Auditing every test file
    for that case individually is the wrong shape for this fix, the same
    reasoning the hrse#1926 comment gives for not auditing every caller of
    `l1_post`/`post_lane_discussion` instead of patching their one shared
    module.

    Module-attribute patching (not import-time patching of
    `l2_post.belt_candidates`) because `l2_post.py` imports the module
    itself (`import belt_candidates`), so `l2_post.belt_candidates` and
    `sys.modules["belt_candidates"]` are the same object -- patching the
    one shared attribute here is visible to every alias automatically.
    """
    forge_gh = Path(__file__).resolve().parent / "gh"
    if str(forge_gh) not in sys.path:
        sys.path.insert(0, str(forge_gh))
    import belt_candidates
    original = belt_candidates.DEFAULT_CANDIDATES_DIR
    belt_candidates.DEFAULT_CANDIDATES_DIR = tmp_dir
    try:
        yield
    finally:
        belt_candidates.DEFAULT_CANDIDATES_DIR = original


def build_suite() -> unittest.TestSuite:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for name in TEST_DIRS:
        directory = ROOT / name
        if not directory.is_dir():
            continue
        # Tests import their subject by bare module name (`import gh_issue`),
        # so the directory has to be importable before discovery loads them.
        sys.path.insert(0, str(directory))
        suite.addTest(loader.discover(start_dir=str(directory), pattern=PATTERN))
    return suite


def main() -> int:
    files = _test_files()
    if not files:
        print("[test] no test files found — refusing to report success", file=sys.stderr)
        return 2

    suite = build_suite()

    # A file present on disk but absent from the suite contributes nothing and
    # fails nothing -- it looks exactly like "all tests pass". Compare against
    # the modules the loader actually produced, NOT against another glob of
    # the same directory: two counts derived from one source always agree,
    # which is a check that cannot fail.
    missing = {p.stem for p in files} - _loaded_modules(suite)
    if missing:
        print(
            "[test] these test files exist but were not collected: "
            + ", ".join(sorted(missing)),
            file=sys.stderr,
        )
        return 2

    with tempfile.TemporaryDirectory() as tmp:
        with redirected_belt_candidates_dir(Path(tmp) / "belt-candidates"):
            result = unittest.TextTestRunner(verbosity=1).run(suite)
    if not result.wasSuccessful():
        return 1
    print(f"[test] OK — {result.testsRun} tests across {len(files)} files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
