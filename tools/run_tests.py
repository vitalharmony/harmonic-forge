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

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TEST_DIRS = ["gh", "hooks"]
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

    result = unittest.TextTestRunner(verbosity=1).run(suite)
    if not result.wasSuccessful():
        return 1
    print(f"[test] OK — {result.testsRun} tests across {len(files)} files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
