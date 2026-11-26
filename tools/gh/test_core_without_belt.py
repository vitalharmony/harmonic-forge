"""Core modules run with every operator path blocked from import (harmonic-forge#1569).

The shared AC2 harness of #1569's children, created by harmonic-forge#1590 (D4). A child
issue extends it in two places and changes nothing else:

* `MODULES` names a core module that must import with the belt absent.
* `CHECKS` registers (through `@check`) a function that calls the module's functions
  named in the child's AC2 and returns a JSON-serializable result, which must be the
  same with the operator paths blocked and present.

The checks prove AC2's same-values requirement and nothing more. They do not catch a
core-to-operator import that returns later: that is `hardcode_scan.py --check`'s job
(AC1). Where a child moved its belt-reaching path out of core, no such path is left in
core for a check to call.

Every import and every check runs in its own subprocess (a fresh `sys.modules` and a
fresh module state such as `_etag_cache._IDENTITY`), with `HOME` set to a new tempdir
so a patched read can never write the live `~/.claude/state/belt/` caches. The blocker
is a `sys.meta_path` finder that raises ImportError for any module whose file matches a
glob from `hardcode_scan.load_operator_globs` (tools/rules/operator_paths.toml).
"""
from __future__ import annotations

import importlib.abc
import importlib.machinery
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "tools" / "rules"))
sys.path.insert(0, str(HERE))
import hardcode_scan  # noqa: E402

#: Core modules that must import with every operator path blocked.
MODULES: tuple[str, ...] = (
    "_etag_cache",  # harmonic-forge#1590
    "afk_toggle",  # harmonic-forge#1591
)

#: name -> a function run in a child; its result must match blocked and unblocked.
CHECKS: dict[str, Callable[[], object]] = {}


def check(fn: Callable[[], object]) -> Callable[[], object]:
    CHECKS[fn.__name__] = fn
    return fn


class OperatorBlocker(importlib.abc.MetaPathFinder):
    """Raises ImportError for a module whose file is an operator path."""

    def __init__(self, globs: list[str]):
        self.globs = globs

    def find_spec(self, fullname, path=None, target=None):
        spec = importlib.machinery.PathFinder.find_spec(fullname, path)
        if spec is None or not spec.origin:
            return None
        try:
            rel = Path(spec.origin).resolve().relative_to(ROOT).as_posix()
        except ValueError:
            return None
        if hardcode_scan.is_operator(rel, self.globs):
            raise ImportError(f"operator path blocked: {rel} ({fullname})")
        return None


# --- checks (each child appends below its own issue's heading) -------------------

# harmonic-forge#1590: `_etag_cache.conditional_get` reached `gh_as_raw` in the belt.
# Both shapes run: with a counter (the belt's) and without one (gate_ci.s0_reader's).
def _etag_reads(with_counter: bool) -> dict:
    import _etag_cache

    requests = []

    def fake_run(cmd, **kw):
        argv = cmd[3:]
        path = [a for a in argv if not a.startswith("-") and a not in ("api", "GET")][0]
        inm = argv[argv.index("-H") + 1].split(": ", 1)[1] if "-H" in argv else None
        requests.append([cmd[0], cmd[1], path, inm])
        if path == "user":
            out, rc = 'HTTP/2.0 200 OK\r\n\r\n{"login": "someone"}', 0
        elif inm == 'W/"v1"':
            out, rc = 'HTTP/2.0 304 Not Modified\r\nEtag: "v1"\r\n\r\n', 1
        else:
            out, rc = 'HTTP/2.0 200 OK\r\nEtag: W/"v1"\r\n\r\n{"n": 1}', 0
        return type("P", (), {"returncode": rc, "stdout": out, "stderr": ""})()

    class Counter:  # duck-typed: the belt's CallCounter is not importable here
        def __init__(self):
            self.recorded, self.headers = 0, 0
            self.calls_304 = self.calls_conditional = self.calls_403_secondary = 0

        def record(self, argv):
            self.recorded += 1

        def record_headers(self, account, headers):
            self.headers += 1

    counter = Counter() if with_counter else None
    real, subprocess.run = subprocess.run, fake_run
    try:
        reads = [list(_etag_cache.conditional_get("acct", "repos/o/r/issues/1", counter))
                 for _ in range(2)]
    finally:
        subprocess.run = real
    return {"reads": reads, "requests": requests,
            "counter": vars(counter) if counter else None}


@check
def etag_conditional_get() -> object:
    return _etag_reads(True)


@check
def etag_conditional_get_no_counter() -> object:
    return _etag_reads(False)


# harmonic-forge#1591: afk_toggle imported belt_heartbeat for the idle-cutoff helpers,
# which moved to the belt. Its remaining functions must return the same values.
sys.path.insert(0, str(ROOT / "tools" / "hooks"))


@check
def afk_toggle_state() -> object:
    import afk_toggle

    out = [afk_toggle.is_on(), afk_toggle.last_action_age("1", now=2000.0)]
    out.append(afk_toggle.record_lane_action("1", now=1000.0))
    out.append(afk_toggle.record_lane_action("l2", now=1500.0))
    out += [afk_toggle.last_action_age("1", now=2000.0),
            afk_toggle.last_action_age("2", now=2000.0)]
    out.append(afk_toggle.toggle({"prompt": "/afk on", "session_id": "s", "cwd": "."},
                                 "cli", now=0.0) is not None)
    out += [afk_toggle.is_on(), afk_toggle.last_action_age("1", now=2000.0)]
    return out


# --- child entry point and the tests ----------------------------------------------

def _child(blocked: bool, kind: str, name: str) -> None:
    if blocked:
        sys.meta_path.insert(0, OperatorBlocker(hardcode_scan.load_operator_globs(ROOT)))
    if kind == "import":
        __import__(name)
        print(json.dumps({"imported": name}))
    else:
        print(json.dumps(CHECKS[name]()))


def _run(blocked: bool, kind: str, name: str) -> subprocess.CompletedProcess:
    with tempfile.TemporaryDirectory() as home:
        env = {**os.environ, "HOME": home}
        return subprocess.run(
            [sys.executable, "-B", str(Path(__file__).resolve()), "--child",
             "1" if blocked else "0", kind, name],
            capture_output=True, text=True, env=env, cwd=home, timeout=120)


class CoreWithoutBeltTests(unittest.TestCase):
    def test_the_blocker_blocks_the_belt(self):
        proc = _run(True, "import", "belt_mechanics")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("operator path blocked: tools/gh/belt_mechanics.py", proc.stderr)

    def test_each_core_module_imports_with_the_belt_blocked(self):
        for name in MODULES:
            with self.subTest(module=name):
                proc = _run(True, "import", name)
                self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_each_check_runs_the_same_blocked_and_unblocked(self):
        for name in CHECKS:
            with self.subTest(check=name):
                blocked, present = _run(True, "check", name), _run(False, "check", name)
                self.assertEqual(blocked.returncode, 0, blocked.stderr)
                self.assertEqual(present.returncode, 0, present.stderr)
                self.assertEqual(json.loads(blocked.stdout), json.loads(present.stdout))

    def test_etag_conditional_get_serves_the_cached_body_on_a_304(self):
        proc = _run(True, "check", "etag_conditional_get")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        result = json.loads(proc.stdout)
        self.assertEqual(result["reads"], [[200, '{"n": 1}', False], [304, '{"n": 1}', True]])
        self.assertEqual(result["requests"][-1][3], 'W/"v1"')
        self.assertEqual(result["counter"]["calls_304"], 1)

    def test_etag_conditional_get_without_a_counter_runs_with_the_belt_blocked(self):
        # gate_ci.s0_reader's shape: no counter (the harmonic-forge#1590 round 1 survivor).
        proc = _run(True, "check", "etag_conditional_get_no_counter")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        result = json.loads(proc.stdout)
        self.assertEqual(result["reads"], [[200, '{"n": 1}', False], [304, '{"n": 1}', True]])
        self.assertIsNone(result["counter"])


if __name__ == "__main__":
    if sys.argv[1:2] == ["--child"]:
        _child(sys.argv[2] == "1", sys.argv[3], sys.argv[4])
    else:
        unittest.main()
