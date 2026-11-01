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
        event = route_store.record(self.key, gates.Route(3, "runner", "r"), "runner", "handoff")
        self.assertEqual((event["attrs"]["path"], event["attrs"]["shadow_path"]), (1, 3))
        self.assertEqual(route_store.current(self.key)["attrs"]["path"], 1)

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
