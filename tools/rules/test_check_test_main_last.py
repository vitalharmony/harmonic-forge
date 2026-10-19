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

    def test_exclude_skips_only_the_named_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            owned = Path(tmp) / "test_owned.py"
            owned.write_text(MID)
            self.assertEqual(check.main([tmp, "--exclude", str(owned)]), 0)
            (Path(tmp) / "test_other.py").write_text(MID)
            self.assertEqual(check.main([tmp, "--exclude", str(owned)]), 1)


if __name__ == "__main__":
    unittest.main()
