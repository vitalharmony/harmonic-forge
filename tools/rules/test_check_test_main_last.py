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
    def test_mid_file_block_is_reported_with_lines(self):
        self.assertEqual(check.violations(MID), [(4, 6), (4, 8)])

    def test_block_at_end_passes(self):
        self.assertEqual(check.violations(END), [])

    def test_no_main_block_passes(self):
        self.assertEqual(check.violations("import unittest\nclass A: pass\n"), [])

    def test_helper_function_after_block_is_not_reported(self):
        src = 'if __name__ == "__main__":\n    pass\ndef helper(): pass\n'
        self.assertEqual(check.violations(src), [])

    def test_main_fails_on_a_mid_file_fixture_and_passes_at_end(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "test_mid.py").write_text(MID)
            self.assertEqual(check.main([tmp]), 1)
            (Path(tmp) / "test_mid.py").write_text(END)
            self.assertEqual(check.main([tmp]), 0)

    def test_reversed_and_compound_guards_are_recognized(self):
        """Preclose pass 1: the guard is matched by what it names, not its shape."""
        for guard in ('if "__main__" == __name__:', 'if __name__ == "__main__" and True:'):
            src = f"{guard}\n    pass\nclass Late:\n    pass\n"
            self.assertEqual(check.violations(src), [(1, 3)], guard)

    def test_definition_inside_a_top_level_block_after_main_is_reported(self):
        """Preclose pass 1 (cross-family): a class nested in if/try after the block."""
        src = ('if __name__ == "__main__":\n    pass\n'
               'if True:\n    class Late:\n        pass\n'
               'try:\n    def test_x(): pass\nexcept Exception:\n    pass\n')
        self.assertEqual(check.violations(src), [(1, 4), (1, 7)])

    def test_method_inside_a_class_before_main_is_not_double_counted(self):
        src = 'class A:\n    def test_a(self): pass\nif __name__ == "__main__":\n    pass\n'
        self.assertEqual(check.violations(src), [])

    def test_missing_root_or_nothing_scanned_fails(self):
        """Preclose pass 1: scanning nothing is not 'ok'."""
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(check.main([str(Path(tmp) / "nope")]), 2)
            self.assertEqual(check.main([tmp]), 2)  # empty: no test_*.py
            (Path(tmp) / "plain.py").write_text("x = 1\n")
            self.assertEqual(check.main([str(Path(tmp) / "plain.py")]), 2)

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
