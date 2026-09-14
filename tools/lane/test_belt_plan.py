"""harmonic-forge#659 AC2 -- `belt_plan.py` is the one source of the arming calls."""
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "belt_plan.py"
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "gh"))
import belt_plan  # noqa: E402
import watch_lane_posts  # noqa: E402


def _run(lane):
    env = {k: v for k, v in os.environ.items() if k != "LANE"}
    if lane is not None:
        env["LANE"] = lane
    return subprocess.run([sys.executable, str(SCRIPT)], env=env,
                          capture_output=True, text=True)


class PlanOutput(unittest.TestCase):
    def test_monitor_command_is_built_from_the_table(self):
        for lane in ("1", "2", "3"):
            with self.subTest(lane=lane):
                out = _run(lane)
                self.assertEqual(out.returncode, 0, out.stderr)
                plan = json.loads(out.stdout)
                expected = ("python3 ~/harmonic-forge/tools/gh/watch_lane_posts.py "
                            + " ".join(watch_lane_posts.CANONICAL_BELTS[lane][0]["argv"]))
                self.assertEqual(plan["monitor"], {
                    "command": expected,
                    "description": f"Lane {lane} belt",
                    "timeout_ms": 1800000,
                })

    def test_command_follows_a_table_change_rather_than_a_retyped_copy(self):
        """Mutating the table must change the output; a retyped string would not."""
        original = watch_lane_posts.CANONICAL_BELTS["2"][0]["argv"]
        try:
            watch_lane_posts.CANONICAL_BELTS["2"][0]["argv"] = ["--sentinel-flag"]
            self.assertTrue(belt_plan.canonical_calls("2")["monitor"]["command"]
                            .endswith("watch_lane_posts.py --sentinel-flag"))
        finally:
            watch_lane_posts.CANONICAL_BELTS["2"][0]["argv"] = original

    def test_loop_is_the_literal_invocation(self):
        plan = json.loads(_run("3").stdout)
        self.assertEqual(plan["loop"], {"skill": "loop",
                                        "args": "10m proactively find work to do"})

    def test_report_template_names_both_ids(self):
        report = json.loads(_run("1").stdout)["report"]
        self.assertIn("monitor task id", report)
        self.assertIn("loop job id", report)

    def test_no_lane_3_sweep_in_the_plan(self):
        self.assertNotIn("--sweep-for", _run("3").stdout)


class RefusesWithoutALane(unittest.TestCase):
    def test_invalid_or_missing_lane_exits_2(self):
        for lane in (None, "", "0", "4", "lane3", "l3"):
            with self.subTest(lane=lane):
                out = _run(lane)
                self.assertEqual(out.returncode, 2)
                self.assertEqual(out.stdout, "")
                self.assertIn("not 1, 2 or 3", out.stderr)

    def test_canonical_calls_raises_for_an_invalid_lane(self):
        with self.assertRaises(KeyError):
            belt_plan.canonical_calls("4")


if __name__ == "__main__":
    unittest.main()
