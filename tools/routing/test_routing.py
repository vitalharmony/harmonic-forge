"""harmonic-forge#1230: the gate evaluator (AC1-AC4), the route store and
`route set`/`route show` (AC5), and the schema's `route` source (AC6)."""
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
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

    def test_authorization_before_lane_machinery(self):
        """Pass-2 #6: gate 3 wins over gate 4 and keeps the deep floor."""
        route = evaluate(Scope(FORGE, {FORGE: ["tools/hooks/x.py"]}, threat_model="hostile-author"))
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

    def test_cross_account_half_under_forge_issue(self):
        route = evaluate(Scope(FORGE, {FORGE: ["tools/telemetry/x.py"], "kenekted/kenekted-ai": ["src/x.py"]},
                               runner_eligible=True))
        self.assertEqual((route.path, route.gate_id), (1, "cross-account"))

    def test_unlisted_foreign_repo_is_path_1(self):
        """Pass-2 #5: matched by owner, so a new client repo is covered."""
        route = evaluate(Scope("kenekted/kenekted-mobile", {"kenekted/kenekted-mobile": ["a.py"]},
                               runner_eligible=True))
        self.assertEqual((route.path, route.gate_id), (1, "cross-account"))

    def test_bare_repo_key_is_path_1(self):
        """Pass-2 #3: a non-canonical repo name never reaches a looser gate."""
        route = evaluate(Scope(HRSE, {"hrse": ["backend/app/x.py"]}, labels=["tooling-exception"]))
        self.assertEqual(route.path, 1)

    def test_malformed_home_repo_name_is_path_1(self):
        for repo in ("vitalharmony/..", "vitalharmony/.", "vitalharmony/bad name", "vitalharmony/"):
            route = evaluate(Scope(repo, {repo: ["a.py"]}, runner_eligible=True))
            self.assertEqual((route.path, route.gate_id), (1, "cross-account"), repo)

    def test_absolute_or_climbing_path_is_path_1(self):
        """Pass-2 #2: a path that is not repo-relative fails closed."""
        for path in ("/backend/requirements.txt", "/frontend/src/x.ts", "../hrse/backend/x.py"):
            route = evaluate(Scope(HRSE, {HRSE: [path]}, runner_eligible=True))
            self.assertEqual(route.path, 1, path)

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

    def test_newest_evaluation_is_current(self):
        """Pass-2 #1 (operator ruling B): a later checkpoint replaces an earlier one."""
        route_store.record(self.key, gates.Route(1, "no-scope", "r"), "no-scope", "filing")
        route_store.record(self.key, gates.Route(2, "te-label", "r"), "te-label", "handoff")
        attrs = route_store.current(self.key)["attrs"]
        self.assertEqual((attrs["path"], attrs["checkpoint"]), (2, "handoff"))
        self.assertNotIn("shadow_path", attrs)

    def test_override_is_a_floor_over_later_evaluations(self):
        """Ruling B: a later, looser evaluation is held at the override and shadowed."""
        route_store.record(self.key, gates.Route(1, "override", "r"), "override", "override")
        route_store.record(self.key, gates.Route(3, "runner", "r"), "runner", "handoff")
        attrs = route_store.current(self.key)["attrs"]
        self.assertEqual((attrs["path"], attrs["decider"], attrs["shadow_path"]), (1, "override", 3))

    def test_equal_path_override_keeps_evaluated_tier(self):
        """Post-verdict: a tie keeps the evaluated gate and its deep Tier floor."""
        route = evaluate(Scope(FORGE, {FORGE: ["tools/hooks/x.py"]}, threat_model="hostile-author"))
        route_store.record(self.key, route, route.gate_id, "handoff")
        route_store.record(self.key, gates.Route(2, "override", "r"), "override", "override")
        attrs = route_store.current(self.key)["attrs"]
        self.assertEqual((attrs["path"], attrs["gate_id"], attrs["tier_floor"]), (2, "authorization", "deep"))

    def test_stricter_evaluation_beats_looser_override(self):
        route_store.record(self.key, gates.Route(2, "override", "r"), "override", "override")
        route_store.record(self.key, gates.Route(1, "hrse-product", "r"), "hrse-product", "pre-merge")
        self.assertEqual(route_store.current(self.key)["attrs"]["gate_id"], "hrse-product")

    def test_late_looser_writer_cannot_replace_tightening(self):
        """Pass-1 #5: an override holds whatever is appended after it."""
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

    def test_key_case_is_one_history(self):
        """Pass-2 #4: an alias key reads and writes the same history."""
        event = route_store.record(f"Vitalharmony/HARMONIC-FORGE#900", gates.Route(1, "override", "r"),
                                   "override", "override")
        self.assertEqual(event["repo"], FORGE)
        self.assertEqual(route_store.current(self.key)["attrs"]["path"], 1)

    def test_mixed_case_stored_history_is_read(self):
        """A history written before keys were folded still reads under the folded key."""
        route_store.record(self.key, gates.Route(2, "te-label", "r"), "te-label", "filing")
        part = next((self.tmp / "store").glob("events/*/*/route/*.jsonl"))
        event = json.loads(part.read_text().splitlines()[0])
        event.update(repo="VitalHarmony/Harmonic-Forge", subject_id="route:legacy")
        event["attrs"].update(path=1, decider="override", ns=event["attrs"]["ns"] + 1)
        with part.open("a") as handle:
            handle.write(json.dumps(event) + "\n")
        self.assertEqual(route_store.current(self.key)["attrs"]["path"], 1)

    def test_path_outside_range_is_refused(self):
        """Pass-2 #7: only 1, 2 and 3 are routes."""
        for bad in (0, 4, True):
            with self.assertRaises(ValueError):
                route_store.record(self.key, gates.Route(bad, "x", "r"), "x", "filing")

    def test_stored_path_outside_range_raises(self):
        """The valid range is 1 to 3: 0 and 4 each raise, each on its own key
        (harmonic-forge#1234; setUp gives one store per test)."""
        for offset, bad in enumerate((0, 4)):
            with self.subTest(path=bad):
                key = f"{FORGE}#{910 + offset}"
                route_store.record(key, gates.Route(1, "override", "r"), "override", "override")
                part = next((self.tmp / "store").glob("events/*/*/route/*.jsonl"))
                event = json.loads(part.read_text().splitlines()[-1])
                event["attrs"]["path"] = bad
                with part.open("a") as handle:
                    handle.write(json.dumps(event) + "\n")
                with self.assertRaises(RuntimeError):
                    route_store.current(key)

    def test_override_keeps_the_evaluated_tier_floor(self):
        """harmonic-forge#1234 AC1: a stricter override reports the deep floor."""
        route_store.record(self.key, gates.Route(2, "authorization", "r", tier_floor="deep"),
                           "authorization", "handoff")
        route_store.record(self.key, gates.Route(1, "override", "r"), "override", "override")
        attrs = route_store.current(self.key)["attrs"]
        self.assertEqual((attrs["path"], attrs["tier_floor"], attrs["shadow_gate"]), (1, "deep", "authorization"))

    def test_override_over_an_evaluation_with_no_floor_gains_none(self):
        """harmonic-forge#1234 AC2: no floor to carry means none appears."""
        route_store.record(self.key, gates.Route(2, "te-label", "r"), "te-label", "filing")
        route_store.record(self.key, gates.Route(1, "override", "r"), "override", "override")
        self.assertNotIn("tier_floor", route_store.current(self.key)["attrs"])

    def test_bad_key(self):
        with self.assertRaises(ValueError):
            route_store.split_key("F1230")


class AtomicCompareAndAppend(unittest.TestCase):
    """harmonic-forge#1238: `record_if_not_looser` holds a per-issue lock across
    the read and the append. Ordered by events, never by sleeps."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        patcher = mock.patch.dict(os.environ, {emit.STORE_ENV: str(self.tmp / "store")})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.key = f"{FORGE}#1238"

    def _completes(self, fn, timeout=5.0):
        thread = threading.Thread(target=fn, daemon=True)
        thread.start()
        thread.join(timeout)
        return not thread.is_alive()

    def _race(self, looser_first: bool):
        """The looser writer reads its baseline and pauses; the stricter writer runs
        in the gap. Without a lock the stricter finishes first and the looser then
        appends over it; with one, the stricter waits for the looser to finish."""
        real = route_store.evaluated_path
        read_done, go = threading.Event(), threading.Event()

        def paused(key):
            value = real(key)
            if threading.current_thread().name == "looser":
                read_done.set()
                go.wait(10)
            return value

        def write(path):
            route_store.record_if_not_looser(self.key, gates.Route(path, "t", "r"), "t", "handoff")

        with mock.patch.object(route_store, "evaluated_path", paused):
            looser = threading.Thread(name="looser", target=write, args=(2,), daemon=True)
            stricter = threading.Thread(name="stricter", target=write, args=(1,), daemon=True)
            if looser_first:
                looser.start()
                self.assertTrue(read_done.wait(5))
                stricter.start()
                stricter.join(0.5)  # finished without a lock; still waiting on it with one
                go.set()
            else:
                stricter.start()
                stricter.join(5)
                looser.start()
                self.assertTrue(read_done.wait(5))
                go.set()
            looser.join(5)
            stricter.join(5)
        self.assertFalse(looser.is_alive() or stricter.is_alive())
        return real(self.key)

    def test_stricter_wins_when_the_looser_reads_first(self):
        """AC1/AC4."""
        self.assertEqual(self._race(looser_first=True), 1)

    def test_stricter_wins_when_it_runs_first(self):
        self.assertEqual(self._race(looser_first=False), 1)

    def test_lock_is_released_when_the_append_raises(self):
        """AC2: an exception inside the lock leaves it free."""
        with mock.patch.object(route_store, "_append", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                route_store.record_if_not_looser(self.key, gates.Route(2, "t", "r"), "t", "handoff")
        done = self._completes(lambda: route_store.record_if_not_looser(
            self.key, gates.Route(2, "t", "r"), "t", "handoff"))
        self.assertTrue(done, "the lock leaked after an exception")

    def test_lock_is_released_when_the_holder_is_killed(self):
        """AC2: a killed holder releases the lock."""
        code = ("import sys; sys.path.insert(0, %r); import route_store\n"
                "with route_store._route_lock(%r):\n"
                "    print('locked', flush=True)\n"
                "    import time; time.sleep(60)\n") % (str(HERE), self.key)
        proc = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True,
                                env=dict(os.environ))
        self.addCleanup(proc.stdout.close)
        self.addCleanup(proc.kill)
        self.assertEqual(proc.stdout.readline().strip(), "locked")
        proc.kill()
        proc.wait(5)
        done = self._completes(lambda: route_store.record_if_not_looser(
            self.key, gates.Route(2, "t", "r"), "t", "handoff"))
        self.assertTrue(done, "a killed holder left the lock held")

    def test_writers_on_different_issues_do_not_block_each_other(self):
        """AC3."""
        held, release = threading.Event(), threading.Event()

        def hold():
            with route_store._route_lock(f"{FORGE}#1"):
                held.set()
                release.wait(10)

        holder = threading.Thread(target=hold, daemon=True)
        holder.start()
        self.assertTrue(held.wait(5))
        done = self._completes(lambda: route_store.record_if_not_looser(
            f"{FORGE}#2", gates.Route(2, "t", "r"), "t", "handoff"))
        release.set()
        holder.join(5)
        self.assertTrue(done, "a writer on another issue was blocked")

    def test_key_spelling_does_not_change_the_lock(self):
        """AC3: `Owner/Repo#5` and `owner/repo#5` contend on one lock."""
        held, release, got = threading.Event(), threading.Event(), threading.Event()

        def hold():
            with route_store._route_lock("Vitalharmony/Harmonic-Forge#5"):
                held.set()
                release.wait(10)

        def take():
            with route_store._route_lock("vitalharmony/harmonic-forge#5"):
                got.set()

        holder = threading.Thread(target=hold, daemon=True)
        holder.start()
        self.assertTrue(held.wait(5))
        taker = threading.Thread(target=take, daemon=True)
        taker.start()
        self.assertFalse(got.wait(0.5), "the lock path ignored key normalization")
        release.set()
        self.assertTrue(got.wait(5))
        holder.join(5)
        taker.join(5)


class MonotonicTimestamps(unittest.TestCase):
    """harmonic-forge#1240: `ns` is strictly increasing per issue whatever the
    wall clock does, for every writer that goes through `record`."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        patcher = mock.patch.dict(os.environ, {emit.STORE_ENV: str(self.tmp / "store")})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.key = f"{FORGE}#1240"

    def _clock(self, *values):
        return mock.patch.object(route_store.time, "time_ns", side_effect=list(values))

    def _completes(self, fn, timeout=5.0):
        thread = threading.Thread(target=fn, daemon=True)
        thread.start()
        thread.join(timeout)
        return not thread.is_alive()

    def _ns(self, key):
        return [int(e["attrs"]["ns"]) for e in route_store.load(key)]

    def test_a_backward_clock_step_does_not_reorder_events(self):
        """AC1: a looser write at 100, the clock steps back to 50, a stricter write."""
        with self._clock(100, 50):
            route_store.record(self.key, gates.Route(2, "t", "r"), "t", "handoff")
            route_store.record(self.key, gates.Route(1, "t", "r"), "t", "handoff")
        first, second = self._ns(self.key)
        self.assertGreater(second, first)
        self.assertEqual(route_store.evaluated_path(self.key), 1)

    def test_every_writer_is_ordered_not_only_the_comparing_one(self):
        """AC2: an override-style and a filing-style write go through `record`."""
        with self._clock(100, 50, 25):
            route_store.record(self.key, gates.Route(3, "filing", "r"), "filing", "filing")
            route_store.record(self.key, gates.Route(1, "override", "r"), "override", "override")
            route_store.record(self.key, gates.Route(2, "handoff", "r"), "handoff", "handoff")
        stamps = self._ns(self.key)
        self.assertEqual(stamps, sorted(stamps))
        self.assertEqual(len(set(stamps)), 3)

    def test_concurrent_writers_get_distinct_increasing_stamps(self):
        """AC2, concurrent: with a constant clock and the first writer paused after
        its read, a lock-free `record` would give both writers the same `ns`."""
        real = route_store.load
        read_done, go = threading.Event(), threading.Event()

        def paused(key):
            value = real(key)
            if threading.current_thread().name == "first":
                read_done.set()
                go.wait(10)
            return value

        def write(path):
            route_store.record(self.key, gates.Route(path, "t", "r"), "t", "handoff")

        with self._clock(*([100] * 8)), mock.patch.object(route_store, "load", paused):
            first = threading.Thread(name="first", target=write, args=(2,), daemon=True)
            second = threading.Thread(name="second", target=write, args=(1,), daemon=True)
            first.start()
            self.assertTrue(read_done.wait(5))
            second.start()
            second.join(0.5)  # finished without a lock; still waiting on it with one
            go.set()
            first.join(5)
            second.join(5)
        self.assertFalse(first.is_alive() or second.is_alive())
        stamps = sorted(self._ns(self.key))
        self.assertEqual(len(stamps), 2)
        self.assertGreater(stamps[1], stamps[0])

    def test_record_if_not_looser_still_completes(self):
        """AC3: it calls the lock-free `_append`, so it does not self-deadlock."""
        done = self._completes(lambda: route_store.record_if_not_looser(
            self.key, gates.Route(2, "t", "r"), "t", "handoff"))
        self.assertTrue(done, "record_if_not_looser deadlocked on its own lock")

    def test_another_issue_is_not_clamped_to_this_issues_stamps(self):
        """AC4, ordering pinned: key A at clock 100, then key B at clock 50 keeps 50."""
        other = f"{FORGE}#1241"
        with self._clock(100, 50):
            route_store.record(self.key, gates.Route(2, "t", "r"), "t", "handoff")
            route_store.record(other, gates.Route(2, "t", "r"), "t", "handoff")
        self.assertEqual(self._ns(other), [50])

    def test_an_increasing_clock_is_stamped_as_is(self):
        with self._clock(100, 200):
            route_store.record(self.key, gates.Route(2, "t", "r"), "t", "handoff")
            route_store.record(self.key, gates.Route(1, "t", "r"), "t", "handoff")
        self.assertEqual(self._ns(self.key), [100, 200])


class Schema(unittest.TestCase):
    """AC6: the telemetry schema accepts source=route."""

    def test_route_source(self):
        self.assertIn("route", emit.SCHEMA["columns"]["source"])


if __name__ == "__main__":
    unittest.main()
