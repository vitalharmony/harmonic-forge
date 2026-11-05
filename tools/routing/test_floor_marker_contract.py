"""harmonic-forge#1250: the mod and the Python writer agree on the marker's path.

The mod's test (`mod/hooks/register.test.ts`) pins the path the running mod asks for,
and its harness cannot start Python. So this compares that pinned path, and the mod's
own template, with the writer. It needs no `claude` CLI, so CI holds the agreement too."""
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "hooks"))
import route_floor_marker as marker  # noqa: E402

MOD = HERE / "mod" / "hooks" / "register.ts"


class Contract(unittest.TestCase):
    def test_the_mod_looks_where_the_writer_writes(self):
        template = re.search(r"\$\.fs\.exists\(`([^`]+)`\)", MOD.read_text()).group(1)
        self.assertEqual(template.replace("${home}", "/h").replace("${id}", "abc"),
                         str(marker.marker_path("abc", home="/h")))

    def test_the_path_the_mod_test_pins_is_the_writers(self):
        source = MOD.with_name("register.test.ts").read_text()
        home = re.search(r"const HOME = '([^']+)'", source).group(1)
        session = re.search(r"const ID = '([^']+)'", source).group(1)
        pinned = re.search(r"const MARKER = '([^']+)'", source).group(1)
        self.assertEqual(pinned, str(marker.marker_path(session, home=home)))

    def test_the_mod_runs_nothing_and_reaches_no_network(self):
        source = MOD.read_text()
        for word in ("process.run", "fetch(", "tool.call", "route_floor.py"):
            self.assertNotIn(word, source)


class Marker(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patcher = mock.patch.dict("os.environ", {"HOME": tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_write_exists_clear(self):
        self.assertFalse(marker.exists("abc"))
        self.assertTrue(marker.write("abc"))
        self.assertTrue(marker.exists("abc"))
        self.assertFalse(marker.exists("other"))
        marker.clear("abc")
        self.assertFalse(marker.exists("abc"))

    def test_an_id_that_cannot_name_a_file_writes_nothing(self):
        for bad in ("../../etc/x", "", None, 7, "a/b"):
            with self.subTest(bad=bad):
                self.assertFalse(marker.write(bad))
                self.assertFalse(marker.exists(bad))
                marker.clear(bad)

    def test_a_write_that_fails_says_so_and_does_not_raise(self):
        with mock.patch.object(Path, "touch", side_effect=PermissionError("no")):
            self.assertFalse(marker.write("abc"))

    def test_eligible_is_lane_2_with_the_mod_loaded(self):
        self.assertTrue(marker.eligible({"LANE": "2", "LANE_ROUTE_MOD": "1"}))
        for env in ({"LANE": "1", "LANE_ROUTE_MOD": "1"}, {"LANE": "3", "LANE_ROUTE_MOD": "1"},
                    {"LANE": "2"}, {"LANE_ROUTE_MOD": "1"}, {}):
            with self.subTest(env=env):
                self.assertFalse(marker.eligible(env))


if __name__ == "__main__":
    unittest.main()
