"""harmonic-forge#1230: the gate evaluator (AC1-AC4), the route store and
`route set`/`route show` (AC5), and the schema's `route` source (AC6)."""
import io
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "telemetry"))
import emit  # noqa: E402
import gates  # noqa: E402
import route_cli  # noqa: E402
import route_store  # noqa: E402
from gates import Scope, evaluate  # noqa: E402

FORGE = "vitalharmony/harmonic-forge"
HRSE = "vitalharmony/hrse"


class Gates(unittest.TestCase):
    """AC1: one fixture per gate; first match wins; empty scope is path 1."""

    def check(self, scope, path, gate):
        route = evaluate(scope)
        self.assertEqual((route.path, route.gate_id), (path, gate), route.reason)

    def test_cross_account(self):
        self.check(Scope("kenekted/kenekted-ai", {"kenekted/kenekted-ai": ["src/x.py"]}), 1, "cross-account")
        self.check(Scope("LeasePAL-ML/LeasePAL-App-Prototype",
                         {"LeasePAL-ML/LeasePAL-App-Prototype": ["app.py"]},
                         labels=["tooling-exception"]), 1, "cross-account")

    def test_hrse_product(self):
        self.check(Scope(HRSE, {HRSE: ["backend/app/x.py"]}, labels=["tooling-exception"]), 1, "hrse-product")

    def test_authorization_is_deep(self):
        route = evaluate(Scope(FORGE, {FORGE: ["tools/gh/x.py"]}, threat_model="hostile-author"))
        self.assertEqual((route.path, route.gate_id, route.tier_floor), (2, "authorization", "deep"))

    def test_lane_machinery(self):
        self.check(Scope(FORGE, {FORGE: ["tools/hooks/x.py"]}, runner_eligible=True), 2, "lane-machinery")

    def test_runner(self):
        self.check(Scope(FORGE, {FORGE: ["tools/telemetry/x.py"]}, runner_eligible=True), 3, "runner")

    def test_te_label(self):
        self.check(Scope(FORGE, {FORGE: ["tools/telemetry/x.py"]}, labels=["tooling-exception"]), 2, "te-label")

    def test_default(self):
        self.check(Scope(FORGE, {FORGE: ["tools/telemetry/x.py"]}), 1, "default")

    def test_first_match_wins(self):
        # gate 2 (hrse product) before gate 4 (lane machinery) on one half
        self.check(Scope(HRSE, {HRSE: [".claude/settings.json", "frontend/src/x.tsx"]}), 1, "hrse-product")

    def test_empty_scope_fails_closed(self):
        self.check(Scope(FORGE), 1, "no-scope")
        self.check(Scope(FORGE, {FORGE: []}, labels=["tooling-exception"]), 1, "no-scope")


class Halves(unittest.TestCase):
    """AC2: each half gated alone, the strictest wins."""

    def test_hrse_backend_half_pulls_to_path_1(self):
        route = evaluate(Scope(FORGE, {FORGE: ["tools/gh/x.py"], HRSE: ["backend/app/x.py"]}))
        self.assertEqual((route.path, route.gate_id), (1, "hrse-product"))

    def test_two_tooling_halves_stay_path_2(self):
        route = evaluate(Scope(FORGE, {FORGE: ["tools/hooks/x.py"], HRSE: ["scripts/x.py"]},
                               labels=["tooling-exception"]))
        self.assertEqual(route.path, 2)

    def test_half_matching_lane_machinery_and_backend_is_path_1(self):
        route = evaluate(Scope(HRSE, {HRSE: ["backend/app/x.py", ".claude/settings.json"]}))
        self.assertEqual((route.path, route.gate_id), (1, "hrse-product"))

    def test_half_with_no_files_is_ignored_and_empty_is_path_1(self):
        route = evaluate(Scope(FORGE, {FORGE: [], HRSE: []}))
        self.assertEqual((route.path, route.gate_id), (1, "no-scope"))


class FailClosedEdges(unittest.TestCase):
    """Pass-1 survivors #1-#4 and #10: every unrecognized or missing input is strict."""

    def test_issue_repo_is_gated(self):
        route = evaluate(Scope("LeasePAL-ML/LeasePAL-App-Prototype", {FORGE: ["tools/telemetry/x.py"]},
                               runner_eligible=True))
        self.assertEqual((route.path, route.gate_id), (1, "cross-account"))

    def test_repo_case_is_folded(self):
        self.assertEqual(evaluate(Scope("leasepal-ml/leasepal-app-prototype",
                                        {"leasepal-ml/leasepal-app-prototype": ["a.py"]},
                                        runner_eligible=True)).path, 1)
        route = evaluate(Scope("vitalharmony/HRSE", {"vitalharmony/HRSE": ["backend/app/x.py"]},
                               labels=["tooling-exception"]))
        self.assertEqual((route.path, route.gate_id), (1, "hrse-product"))

    def test_threat_spelling_is_folded(self):
        for spelling in ("Hostile-Author", "hostile_author", " HOSTILE AUTHOR "):
            route = evaluate(Scope(FORGE, {FORGE: ["tools/telemetry/x.py"]},
                                   threat_model=spelling, runner_eligible=True))
            self.assertEqual(route.gate_id, "authorization", spelling)

    def test_unknown_threat_is_never_runner(self):
        route = evaluate(Scope(FORGE, {FORGE: ["tools/telemetry/x.py"]},
                               threat_model="unclear", runner_eligible=True))
        self.assertNotEqual(route.path, 3)

    def test_good_faith_any_spelling_may_reach_runner(self):
        route = evaluate(Scope(FORGE, {FORGE: ["tools/telemetry/x.py"]},
                               threat_model="Good_Faith", runner_eligible=True))
        self.assertEqual(route.path, 3)

    def test_dot_prefixed_product_path(self):
        route = evaluate(Scope(HRSE, {HRSE: ["./backend/app/x.py"]}, runner_eligible=True))
        self.assertEqual((route.path, route.gate_id), (1, "hrse-product"))

    def test_empty_half_beside_runner_half_is_path_1(self):
        route = evaluate(Scope(FORGE, {FORGE: ["tools/telemetry/x.py"], HRSE: []}, runner_eligible=True))
        self.assertEqual((route.path, route.gate_id), (1, "empty-half"))


class Hostile(unittest.TestCase):
    """AC3: hostile-author is never path 3."""

    def test_never_runner(self):
        route = evaluate(Scope(FORGE, {FORGE: ["tools/telemetry/x.py"]},
                               threat_model="hostile-author", runner_eligible=True))
        self.assertNotEqual(route.path, 3)
        self.assertEqual(route.gate_id, "authorization")


class RunnerInput(unittest.TestCase):
    """AC4: runner eligibility is an input, default False, never computed here."""

    def test_default_false(self):
        self.assertFalse(Scope(FORGE).runner_eligible)

    def test_gates_never_calls_the_runner(self):
        source = (HERE / "gates.py").read_text()
        for name in ("te_queue", "item_failures", "preflight", "subprocess"):
            self.assertNotIn(name, source.split('"""', 2)[-1])


class Store(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        patcher = mock.patch.dict(os.environ, {emit.STORE_ENV: str(self.tmp / "store")})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.key = f"{FORGE}#900"

    def test_record_and_current(self):
        route_store.record(self.key, gates.Route(2, "te-label", "r"), "te-label", "filing")
        self.assertEqual(route_store.current(self.key)["attrs"]["path"], 2)

    def test_recheck_never_loosens(self):
        route_store.record(self.key, gates.Route(1, "hrse-product", "r"), "hrse-product", "filing")
        route_store.record(self.key, gates.Route(3, "runner", "r"), "runner", "handoff")
        attrs = route_store.current(self.key)["attrs"]
        self.assertEqual((attrs["path"], attrs["shadow_path"]), (1, 3))

    def test_late_looser_writer_cannot_replace_tightening(self):
        """Pass-1 #5: order of append never loosens; the tightest event holds."""
        route_store.record(self.key, gates.Route(2, "te-label", "r"), "te-label", "filing")
        route_store.record(self.key, gates.Route(1, "override", "r"), "override", "override")
        route_store.record(self.key, gates.Route(2, "te-label", "r"), "te-label", "pre-merge")
        self.assertEqual(route_store.current(self.key)["attrs"]["path"], 1)

    def test_same_second_writes_are_both_stored(self):
        """Pass-1 #5: two writes in one second are distinct events, not a duplicate."""
        route_store.record(self.key, gates.Route(2, "te-label", "r"), "te-label", "filing")
        route_store.record(self.key, gates.Route(2, "te-label", "r"), "te-label", "filing")
        self.assertEqual(len(route_store.load(self.key)), 2)

    def test_unstored_write_raises(self):
        """Pass-1 #5: a write emit does not store is an error, never a success."""
        with mock.patch.object(emit, "emit", return_value={"rejected": [], "written": 0, "duplicate": 1}):
            with self.assertRaises(RuntimeError):
                route_store.record(self.key, gates.Route(1, "override", "r"), "override", "override")

    def test_corrupt_line_raises(self):
        """Pass-1 #7: an unreadable event line fails closed instead of being skipped."""
        route_store.record(self.key, gates.Route(1, "override", "r"), "override", "override")
        part = next((self.tmp / "store").glob("events/*/*/route/*.jsonl"))
        with part.open("a") as handle:
            handle.write('{"torn": \n')
        with self.assertRaises(RuntimeError):
            route_store.current(self.key)

    def test_no_root_parameter(self):
        """Pass-1 #6: one store for reads and writes; no root override exists."""
        import inspect
        for fn in (route_store.load, route_store.current, route_store.record):
            self.assertNotIn("root", inspect.signature(fn).parameters)

    def test_tighter(self):
        """Pass-1 #9: tighter() is the strictest-path rule current() applies."""
        self.assertEqual(route_store.tighter(1, 3), 1)
        self.assertEqual(route_store.tighter(3, 2), 2)
        with mock.patch.object(route_store, "tighter", side_effect=max):
            route_store.record(self.key, gates.Route(1, "override", "r"), "override", "override")
            route_store.record(self.key, gates.Route(3, "runner", "r"), "runner", "handoff")
            self.assertEqual(route_store.current(self.key)["attrs"]["path"], 3)

    def test_route_set_refuses_loosening_with_no_history(self):
        """Pass-1 #8: no recorded route reads as path 1."""
        with redirect_stderr(io.StringIO()) as err:
            self.assertEqual(route_cli.cmd_set(self.key, 3, "faster"), 1)
        self.assertIn("refused", err.getvalue())
        self.assertIsNone(route_store.current(self.key))

    def test_recheck_tightens(self):
        route_store.record(self.key, gates.Route(2, "te-label", "r"), "te-label", "filing")
        route_store.record(self.key, gates.Route(1, "hrse-product", "r"), "hrse-product", "pre-merge")
        self.assertEqual(route_store.current(self.key)["attrs"]["path"], 1)

    def test_route_set_override_and_show(self):
        """AC5: an override event names the operator and reason."""
        route_store.record(self.key, gates.Route(2, "te-label", "r"), "te-label", "filing")
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(route_cli.cmd_set(self.key, 1, "operator wants 3-lane"), 0)
        event = route_store.current(self.key)
        self.assertEqual((event["actor"], event["attrs"]["decider"], event["attrs"]["path"]),
                         ("operator", "override", 1))
        self.assertIn("operator wants 3-lane", event["attrs"]["reason"])
        out = io.StringIO()
        with redirect_stdout(out):
            route_cli.cmd_show(self.key)
        self.assertIn("path 1", out.getvalue())
        self.assertIn("decider override", out.getvalue())

    def test_route_set_refuses_loosening(self):
        """AC5: route set only tightens."""
        route_store.record(self.key, gates.Route(1, "hrse-product", "r"), "hrse-product", "filing")
        with redirect_stderr(io.StringIO()) as err:
            self.assertEqual(route_cli.cmd_set(self.key, 3, "faster"), 1)
        self.assertIn("refused", err.getvalue())
        self.assertEqual(route_store.current(self.key)["attrs"]["path"], 1)

    def test_bad_key(self):
        with self.assertRaises(ValueError):
            route_store.split_key("F1230")


class Schema(unittest.TestCase):
    """AC6: the telemetry schema accepts source=route."""

    def test_route_source(self):
        self.assertIn("route", emit.SCHEMA["columns"]["source"])


if __name__ == "__main__":
    unittest.main()
