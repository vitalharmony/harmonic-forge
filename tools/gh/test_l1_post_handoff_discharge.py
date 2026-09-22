#!/usr/bin/env python3
"""harmonic-forge#687 AC2 — a posted handoff discharges its owed record.

The obligation is written by `gh_issue.py` at filing time and blocks the
Stop event until it clears. This is the clearing half, and it lives here
rather than in harmonic-forge because `l1_post.py` does: the Affected Files
table in F687's handoff names `tools/gh/l1_post.py`, which does not exist —
`l1_post.py` is HRSE2's, and forge's `mise run l1-post` delegates to it.

Two properties matter and are tested as such:

1. **Only that issue clears** (AC2/AC5). Two issues filed in one turn are
   two independent obligations; posting one handoff must not clear both.
2. **A missing store never fails a post.** `l1_post.py` has no hard
   dependency on ~/harmonic-forge existing, and a handoff that posted and
   then reported an error would be strictly worse than a stale record that
   prunes itself in seven days.
"""
import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).parent

#: Where the obligation store lives. `$HARMONIC_FORGE_HOOKS` overrides it,
#: and that override is the reason this suite is worth having at all: the
#: store ships in harmonic-forge#687's OTHER branch, so until that merges,
#: `~/harmonic-forge/tools/hooks/handoff_owed.py` does not exist and every
#: test here skips. A suite that silently skips is a suite that does not
#: exist, so point it at the unmerged checkout to actually run it:
#:
#:     HARMONIC_FORGE_HOOKS=/tmp/harmonic-forge-687-impl/tools/hooks \
#:         .venv/bin/python3 -m pytest scripts/test_l1_post_handoff_discharge.py
#:
#: Production never sets it and resolves the real path, unchanged.
FORGE_HOOKS = Path(os.environ.get(
    "HARMONIC_FORGE_HOOKS",
    str(ROOT.parents[1] / "tools" / "hooks")))


def load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


@unittest.skipUnless((FORGE_HOOKS / "handoff_owed.py").exists(),
                     "harmonic-forge#687's store is not present")
class HandoffDischarge(unittest.TestCase):
    def setUp(self) -> None:
        sys.path.insert(0, str(FORGE_HOOKS))
        import handoff_owed
        self.store = handoff_owed
        self.l1_post = load("l1_post")
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patcher = mock.patch.object(self.store, "OWED_DIR",
                                    Path(tmp.name) / "handoff_owed")
        patcher.start()
        self.addCleanup(patcher.stop)
        # The module under test imported the store at ITS import time, so the
        # patch has to reach the same object. It does — both names bind the
        # one module instance — but assert it rather than assume it, because
        # a future `from handoff_owed import discharge` would silently break
        # this whole file into testing nothing.
        self.assertIs(self.l1_post._handoff_owed, self.store)

    def test_posting_a_handoff_clears_that_issue_only(self):
        self.store.record("vitalharmony/hrse", 701, key="filer")
        self.store.record("vitalharmony/hrse", 702, key="filer")
        self.l1_post._discharge_handoff_owed("vitalharmony/hrse", 701)
        self.assertEqual([e["issue"] for e in self.store.outstanding("filer")],
                         [702])

    def test_the_same_number_in_another_repo_is_untouched(self):
        self.store.record("vitalharmony/hrse", 687, key="filer")
        self.store.record("vitalharmony/harmonic-forge", 687, key="filer")
        self.l1_post._discharge_handoff_owed("vitalharmony/harmonic-forge", 687)
        self.assertEqual([e["repo"] for e in self.store.outstanding("filer")],
                         ["vitalharmony/hrse"])

    def test_it_clears_a_record_written_by_a_different_session(self):
        """Lane 1 routinely posts the handoff for an issue a no-LANE session
        filed. Session-scoping the clear would leave that filer blocked with
        the handoff already live on the issue."""
        self.store.record("vitalharmony/hrse", 701, key="someone-else")
        self.l1_post._discharge_handoff_owed("vitalharmony/hrse", 701)
        self.assertEqual(self.store.outstanding("someone-else"), [])

    def test_discharging_an_issue_with_no_record_is_silent(self):
        self.l1_post._discharge_handoff_owed("vitalharmony/hrse", 4242)

    def test_a_store_that_raises_never_fails_the_post(self):
        with mock.patch.object(self.store, "discharge", side_effect=OSError):
            self.l1_post._discharge_handoff_owed("vitalharmony/hrse", 701)

    def test_an_absent_store_never_fails_the_post(self):
        with mock.patch.object(self.l1_post, "_handoff_owed", None):
            self.l1_post._discharge_handoff_owed("vitalharmony/hrse", 701)


if __name__ == "__main__":
    unittest.main()
