"""harmonic-forge#1072: the main-block-last check."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import check_test_main_last as check  # noqa: E402

MID = '''import unittest
class A(unittest.TestCase):
    def test_a(self): pass
if __name__ == "__main__":
    unittest.main()
class B(unittest.TestCase):
    def test_b(self): pass
def setUpModule(): pass
'''

END = '''import unittest
def setUpModule(): pass
class A(unittest.TestCase):
    def test_a(self): pass
if __name__ == "__main__":
    unittest.main()
'''


class MainBlockLastTests(unittest.TestCase):
    def test_mid_file_block_is_reported_at_the_next_statement(self):
        self.assertEqual(check.violations(MID), [(4, 6)])

    def test_block_at_end_passes(self):
        self.assertEqual(check.violations(END), [])

    def test_no_main_block_passes(self):
        self.assertEqual(check.violations("import unittest\nclass A: pass\n"), [])

    def test_any_statement_after_the_block_fails(self):
        """The reforged rule: the block must be last, whatever follows it."""
        src = 'if __name__ == "__main__":\n    pass\ndef helper(): pass\n'
        self.assertEqual(check.violations(src), [(1, 3)])

    def test_every_guard_spelling_and_placement_is_found(self):
        """Sticky-wicket REFORGE: no spelling escapes, because none is recognized."""
        guards = (
            'if "__main__" == __name__:\n    pass\n',                      # reversed
            'if __name__ == "__main__" and True:\n    pass\n',             # compound
            'if False:\n    pass\nelif __name__ == "__main__":\n    pass\n',  # elif
            'try:\n    if __name__ == "__main__":\n        pass\nexcept Exception:\n    pass\n',  # nested
            'if __name__ == "__ma" + "in__":\n    pass\n',                 # computed
        )
        for guard in guards:
            src = guard + "class Late:\n    pass\n"
            found = check.violations(src)
            self.assertEqual(len(found), 1, guard)
            self.assertEqual(found[0][0], 1, guard)

    def test_any_block_type_after_the_guard_fails(self):
        """match/case, if, try: the statements after the guard are not inspected."""
        for after in ('match 1:\n    case _:\n        class Late: pass\n',
                      'if True:\n    class Late: pass\n',
                      'try:\n    def test_x(): pass\nexcept Exception:\n    pass\n'):
            src = 'if __name__ == "__main__":\n    pass\n' + after
            self.assertEqual(check.violations(src), [(1, 3)], after)

    def test_main_fails_on_a_mid_file_fixture_and_passes_at_end(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "test_mid.py").write_text(MID)
            self.assertEqual(check.main([tmp]), 1)
            (Path(tmp) / "test_mid.py").write_text(END)
            self.assertEqual(check.main([tmp]), 0)

    def test_missing_root_or_nothing_scanned_fails(self):
        """Scanning nothing is not 'ok'."""
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(check.main([str(Path(tmp) / "nope")]), 2)
            self.assertEqual(check.main([tmp]), 2)  # empty: no test_*.py
            (Path(tmp) / "plain.py").write_text("x = 1\n")
            self.assertEqual(check.main([str(Path(tmp) / "plain.py")]), 2)

    def test_missing_root_fails_even_when_another_root_scans_files(self):
        """Preclose pass 2: one missing root is not masked by another's files."""
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "test_clean.py").write_text(END)
            self.assertEqual(check.main([tmp, str(Path(tmp) / "nope")]), 2)

    def test_exclude_skips_only_the_named_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            owned = Path(tmp) / "test_owned.py"
            owned.write_text(MID)
            (Path(tmp) / "test_clean.py").write_text(END)
            self.assertEqual(check.main([tmp, "--exclude", str(owned)]), 0)
            (Path(tmp) / "test_other.py").write_text(MID)
            self.assertEqual(check.main([tmp, "--exclude", str(owned)]), 1)


if __name__ == "__main__":
    unittest.main()
