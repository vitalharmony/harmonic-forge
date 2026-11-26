"""harmonic-forge#1578: every post-create failure of a filing is reported and
exits 1. One class per acceptance criterion, each driving `gh_issue.main()`
with `TestPostCreateFailureTable._drive`'s patch set (test_gh_issue.py)."""
from __future__ import annotations

import contextlib
import io
import itertools
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gh_issue  # noqa: E402
import test_gh_issue as base  # noqa: E402

#: The table's attributes, never its class: a TestCase bound here would run twice.
ROWS, BOARD_ARGS = base.TestPostCreateFailureTable.ROWS, base.TestPostCreateFailureTable.BOARD_ARGS
PARKING, EXCEPTION = base.TestPostCreateFailureTable.PARKING, base.TestPostCreateFailureTable.EXCEPTION
FORGE = base.FORGE
URL = "https://github.com/o/r/issues/42"
RECORDED = ["--repo", FORGE, "--milestone", "0.3", "--handoff-exception",
            "deferred-design-record", "--handoff-exception-reason", "design waits"]
POSTED = json.dumps({"id": 1, "user": {"login": "vitalharmony"}})
CTX = {"project_id": "PVT_1", "fields": []}
#: `_contained`'s step label for each ROWS call.
LABELS = {"handoff_owed.record": "obligation record", "_post_deferral": "R-0352 deferral",
          "_discharge_exception": "R-0039 exception record", "add_to_board": "board add"}


def _drive(argv, post_stdout="", **over):
    """`main()` with every external edge patched; `over` replaces a patch
    target's kwargs, or drops the patch when given None (the real code runs)."""
    targets = {
        "gh_issue.find_prior_owners": {"return_value": []},
        "gh_issue.fetch_milestones": {"return_value": base.FORGE_MILESTONES},
        "gh_issue.apply_project_identity": {},
        "gh_issue.create_issue": {"return_value": URL},
        "gh_issue._route_at_filing": {"return_value": None},
        "gh_issue.resolve_board_for_repo": {"return_value": ("o", "1")},
        "gh_issue._run": {"return_value": base._completed(post_stdout)},
        "gh_issue.handoff_owed.record": {"return_value": True},
        "gh_issue.handoff_owed.discharge": {"return_value": 1},
        "gh_issue.add_to_board": {"return_value": True},
    }
    for key, value in over.items():
        targets[f"gh_issue.{key}"] = value
    mocks = {}
    with contextlib.ExitStack() as stack:
        stack.enter_context(patch.object(sys, "argv", ["gh_issue.py", "--title", "t"]
                                         + argv + BOARD_ARGS))
        for target, kw in targets.items():
            if kw is not None:
                mocks[target.split(".")[-1]] = stack.enter_context(patch(target, **kw))
        stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        err = stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
        try:
            code = gh_issue.main()
        except SystemExit as exc:
            code = exc.code
    return code, err.getvalue(), mocks


def _line_after(err: str, marker: str) -> str:
    lines = err.splitlines()
    return next(b for a, b in itertools.pairwise(lines) if marker in a)


class AC1FailedDischargeExitsOne(unittest.TestCase):
    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root ignores modes")
    def test_exception_path_with_a_real_unwritable_store(self):
        import handoff_owed
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = Path(tmp.name) / "owed"
        with patch.object(handoff_owed, "OWED_DIR", store):
            handoff_owed.record(FORGE, 42, URL, key="s1")
            store.chmod(0o500)
            self.addCleanup(store.chmod, 0o700)
            code, err, _ = _drive(EXCEPTION, **{"handoff_owed.discharge": None})
            self.assertEqual(len(handoff_owed.outstanding("s1")), 1)
        self.assertEqual(code, 1, err)
        repair = _line_after(err, "handoff obligation could not be cleared")
        self.assertIn("python3 -c", repair)
        self.assertIn(f'handoff_owed.discharge("{FORGE}", 42) < 0', repair)
        self.assertNotIn("l1-post", err)

    def test_deferral_path_prints_the_defer_existing_retry(self):
        code, err, _ = _drive(PARKING, **{"handoff_owed.discharge": {"return_value": -1}})
        self.assertEqual(code, 1, err)
        self.assertIn("--defer-existing 42", _line_after(err, "could not be cleared"))
        self.assertIn("R-0352 deferral", err)

    def test_defer_existing_exits_one(self):
        def run(cmd, check=False):
            joined = " ".join(cmd)
            if "/comments?" in joined:
                return base._completed("## R-0352 deferral: parked in `Later`\n")
            return base._completed(f"https://github.com/{FORGE}/issues/42\tLater\n")
        with patch.object(sys, "argv", ["gh_issue.py", "--repo", FORGE, "--defer-existing", "42"]
                          + base.DEFER), \
             patch("gh_issue.fetch_milestones", return_value=base.FORGE_MILESTONES), \
             patch("gh_issue.apply_project_identity"), patch("gh_issue._run", side_effect=run), \
             patch("gh_issue.handoff_owed.discharge", return_value=-1), \
             contextlib.redirect_stdout(io.StringIO()), \
             contextlib.redirect_stderr(io.StringIO()) as err:
            code = gh_issue.main()
        self.assertEqual(code, 1)
        self.assertIn("--defer-existing 42", _line_after(err.getvalue(), "could not be cleared"))

    def test_an_unwritten_deferral_watch_entry_exits_one(self):
        """Operator ruling 2026-11-25: F1564's `record_deferral` returning False."""
        with patch("gh_issue.deferral_store.record_deferral", return_value=False):
            code, err, mocks = _drive(RECORDED, post_stdout=POSTED)
        self.assertEqual(code, 1, err)
        self.assertIn("deferral watch record: the watch entry was not written, so the belt does "
                      "not watch it; python3 tools/gh/record_deferral.py --repo "
                      f"{FORGE} --issue 42 --operator-login vitalharmony", err)
        mocks["add_to_board"].assert_called_once()

    def test_an_unreadable_login_exits_one(self):
        with patch("gh_issue.deferral_store.record_deferral", return_value=True):
            code, err, mocks = _drive(RECORDED, post_stdout="")
        self.assertEqual(code, 1, err)
        self.assertIn("deferral watch record: the comment's login could not be read, so the belt "
                      f"does not watch it; run record_deferral.py --repo {FORGE} --issue 42 "
                      "--operator-login with the login that posted the comment", err)
        self.assertNotIn("<login>", err)  # sticky verdict P2: no placeholder
        mocks["discharge"].assert_called_once_with(FORGE, 42)


class AC2EveryFieldIsAttempted(unittest.TestCase):
    def _board(self, select):
        with patch("gh_issue._run_board", return_value=base._completed('{"id": "IT_1"}')), \
             patch("gh_issue._fetch_project_context", return_value=CTX), \
             patch("gh_issue._set_single_select", side_effect=select) as sel, \
             patch("gh_issue._set_tier", return_value=True) as tier:
            code, err, _ = _drive(PARKING, add_to_board=None)
        return code, err, [c.args[3:5] for c in sel.call_args_list], tier

    def test_a_failed_status_write_still_attempts_tier_theme_venture(self):
        code, err, fields, tier = self._board(lambda *a: a[3] not in ("Status", "Theme"))
        self.assertEqual(code, 1, err)
        tier.assert_called_once_with("IT_1", "PVT_1", [], "fast")
        self.assertEqual(fields, [("Status", gh_issue.STATUS_OPTION_NAME),
                                  ("Theme", "Tooling"), ("Venture", "Platform")])
        self.assertIn(f"- Status={gh_issue.STATUS_OPTION_NAME}: not written", err)
        self.assertIn("- Theme=Tooling: not written", err)

    def test_a_raising_status_write_still_attempts_the_rest(self):
        def select(*a):
            if a[3] == "Status":
                raise OSError("could not start gh")
            return True
        code, err, fields, tier = self._board(select)
        self.assertEqual(code, 1, err)
        tier.assert_called_once()
        self.assertEqual([f for f, _ in fields], ["Status", "Theme", "Venture"])
        self.assertIn(f"Status={gh_issue.STATUS_OPTION_NAME} raised OSError", err)


class AC3NoStepRaisesPastTheSummary(unittest.TestCase):
    ERRORS = (OSError("could not start gh"),
              UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte"),
              RecursionError("maximum recursion depth exceeded"))

    def test_each_row_step_raising_is_one_unfinished_step(self):
        for call, argv, *_rest in ROWS.values():
            for exc in self.ERRORS:
                with self.subTest(call=call, exc=type(exc).__name__):
                    code, err, mocks = _drive(argv, **{call: {"side_effect": exc}})
                    self.assertEqual(code, 1, err)
                    self.assertIn(f"{LABELS[call]} raised {type(exc).__name__}", err)
                    self.assertIn(f"[GH]   - {LABELS[call]}: ", err)
                    mocks["record"].assert_called_once()
                    mocks["add_to_board"].assert_called_once()
                    self.assertEqual(mocks["add_to_board"].call_args.args[3:6],
                                     ("fast", "Tooling", "Platform"))

    def test_a_raising_board_lookup_is_an_unfinished_step(self):
        code, err, mocks = _drive(PARKING, resolve_board_for_repo={
            "side_effect": UnicodeDecodeError("utf-8", b"\xff", 0, 1, "bad")})
        self.assertEqual(code, 1, err)
        self.assertIn("board resolution raised UnicodeDecodeError", err)
        self.assertIn("- board resolution:", err)
        mocks["discharge"].assert_called_once()


class AC4SummaryMatchesWhatRan(unittest.TestCase):
    def test_an_unmapped_board_names_the_skipped_steps(self):
        code, err, mocks = _drive(PARKING, resolve_board_for_repo={
            "side_effect": SystemExit("[GH] No project board mapped")})
        self.assertEqual(code, 1, err)
        self.assertIn("board add and Status/Tier/Theme/Venture not attempted", err)
        self.assertNotIn("every other step ran", err)
        mocks["add_to_board"].assert_not_called()

    def test_a_failed_board_add_names_the_skipped_fields(self):
        with patch("gh_issue._run_board", return_value=base._completed("", returncode=1)), \
             patch("gh_issue._find_existing_item", return_value=None):
            code, err, _ = _drive(PARKING, add_to_board=None)
        self.assertEqual(code, 1, err)
        self.assertIn("- board add: not added, so Status/Tier/Theme/Venture not attempted; add "
                      + URL, err)
        self.assertNotIn("every other step ran", err)


class Round1Survivors(unittest.TestCase):
    """Preclose round 1's eight survivors: each post-create step, Path and the
    repair builds included, is its own entry with the repair for what ran."""
    BLOCKED = ["--repo", FORGE, "--milestone", "0.3", "--handoff-exception", "blocked-on-prerequisite",
               "--handoff-exception-reason", "waits", "--blocked-by", "o/b#1"]

    def _board(self, select=lambda *a: True, ctx=None, fields=()):
        ctx = ctx or {"return_value": {"project_id": "PVT_1", "fields": list(fields)}}
        with patch("gh_issue._run_board", return_value=base._completed('{"id": "IT_1"}')), \
             patch("gh_issue._fetch_project_context", **ctx), \
             patch("gh_issue._set_single_select", side_effect=select) as sel:
            code, err, _ = _drive(PARKING, add_to_board=None, _route_at_filing={"return_value": "2"})
        return code, err, [c.args[3] for c in sel.call_args_list]

    def test_1_a_raising_path_write_or_context_fetch_after_the_add_is_named(self):
        def select(*a):
            if a[3] == "Path":
                raise OSError("could not start gh")
            return True
        code, err, fields = self._board(select)
        self.assertEqual((code, fields), (1, ["Path", "Status", "Tier", "Theme", "Venture"]), err)
        self.assertIn("- Path=2: raised OSError; set it on the board by hand", err)
        code, err, _ = self._board(ctx={"side_effect": TypeError("list indices")})
        self.assertEqual(code, 1, err)
        self.assertIn("- board add: raised TypeError; the item is on board #1 but its fields could not be read", err)
        self.assertNotIn(f"add {URL} to board #1 by hand", err)

    def test_2_a_missing_field_or_option_names_that_not_a_printed_command(self):
        fields = [{"name": "Status", "id": "F1", "options": [{"name": "Todo", "id": "O1"}]},
                  {"name": "Tier", "id": "F2", "options": [{"name": "fast", "id": "O2"}]},
                  {"name": "Path", "id": "F3", "options": [{"name": "2", "id": "O3"}]},
                  {"name": "Theme", "id": "F4", "options": [{"name": "Other", "id": "O4"}]}]
        with patch("gh_issue._run_board", return_value=base._completed('{"id": "IT_1"}')), \
             patch("gh_issue._fetch_project_context", return_value={"project_id": "P", "fields": fields}):
            code, err, _ = _drive(PARKING, add_to_board=None)
        self.assertEqual(code, 1, err)
        self.assertIn("- Theme=Tooling: not written; the Theme field has no 'Tooling' option", err)
        self.assertIn("- Venture=Platform: not written; the board has no Venture field", err)
        self.assertNotIn("printed above", err)

    def test_3_a_rejected_path_write_exits_one(self):
        code, err, _ = self._board(lambda *a: a[3] != "Path")
        self.assertEqual(code, 1, err)
        self.assertIn("- Path=2: not written; the board has no Path field", err)

    def _route_rerun(self):
        """The printed route repair for a Path 2 filing whose record failed."""
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "routing"))
        import route_store  # noqa: PLC0415
        with patch.object(route_store, "record", side_effect=RuntimeError("store down")):
            code, err, _ = _drive(PARKING + ["--body", "edits `tools/hooks/x.py`"], _route_at_filing=None)
        self.assertEqual(code, 1, err)
        entry = next(e for e in err.splitlines() if "- route filing checkpoint: " in e)
        return route_store, entry.split("- route filing checkpoint: not recorded; ", 1)[1]

    def _run_rerun(self, rerun, before=None):
        import shlex
        import subprocess
        import emit  # noqa: PLC0415
        with tempfile.TemporaryDirectory() as tmp, \
                patch.dict(os.environ, {emit.STORE_ENV: tmp, emit.TESTING_ENV: "1"}):
            if before is not None:
                before()
            subprocess.run(shlex.split(rerun), check=True, env=os.environ)
            import route_store  # noqa: PLC0415
            return route_store.current(f"{FORGE}#42"), route_store.evaluated_path(f"{FORGE}#42")

    def test_4_an_unrecorded_filing_checkpoint_is_named_with_a_rerun_that_records_it(self):
        _, rerun = self._route_rerun()
        current, path = self._run_rerun(rerun)
        self.assertEqual((current["attrs"]["checkpoint"], path), ("filing", 2))

    def test_4b_the_route_repair_never_loosens_a_stricter_route(self):
        """Sticky verdict P2 (round 2 survivor 2): path 1 recorded since the
        filing stays 1 after the printed repair runs (tighten-only)."""
        route_store, rerun = self._route_rerun()
        import gates  # noqa: PLC0415

        def stricter():
            route_store.record(f"{FORGE}#42", gates.Route(1, "handoff", "stricter"),
                               decider="handoff", checkpoint="handoff")
        _, path = self._run_rerun(rerun, before=stricter)
        self.assertEqual(path, 1)

    def test_5_a_failed_prerequisite_lookup_names_the_skipped_steps(self):
        code, err, mocks = _drive(self.BLOCKED, _run={"return_value": base._completed("", returncode=1)})
        self.assertEqual(code, 1, err)
        self.assertIn("the blocked_by link to o/b#1 is not confirmed on the issue (lookup, write "
                      "or re-read failed, see the [GH] line), so the exception comment and the "
                      "obligation clear were not attempted; confirm or add it", err)
        self.assertNotIn("repair printed above", err)
        self.assertNotIn("gh api", err)
        mocks["discharge"].assert_not_called()

    def test_6_a_raising_deferral_watch_is_its_own_step(self):
        with patch("gh_issue.deferral_store.record_deferral", side_effect=OSError("disk")):
            code, err, mocks = _drive(RECORDED, post_stdout=POSTED)
        self.assertEqual(code, 1, err)
        self.assertIn("deferral watch record raised OSError", err)
        self.assertIn("- deferral watch record: raised OSError; python3 tools/gh/record_deferral.py", err)
        self.assertNotIn("- R-0039 exception record", err)
        mocks["discharge"].assert_called_once_with(FORGE, 42)

    def test_8_a_raising_repair_build_is_still_one_entry(self):
        with patch("gh_issue.handoff_owed.discharge_command", side_effect=ValueError("bad route")):
            code, err, mocks = _drive(PARKING, **{"handoff_owed.record": {"return_value": False}})
        self.assertEqual(code, 1, err)
        self.assertIn("- obligation record: not stored, so no Stop hook will name it; "
                      "(the repair command could not be built: ValueError", err)
        mocks["add_to_board"].assert_called_once()


#: Sticky verdict P2: an entry is "<step>: <what did not happen>; <repair>".
ENTRY = re.compile(r"^(?P<step>[^:;]+): (?P<what>[^;]+); (?P<repair>.+)$")
#: One shlex-quoted word: bare safe characters, or '...' with shlex's '"'"' for a quote.
_WORD = r"(?:[\w@%+=:,./-]+|'(?:[^']|'\"'\"')*')"
_REPO = r"[\w.-]+/[\w.-]+"
#: The closed set, each matched whole: the discharge and route `python3 -c`
#: programs (checked by `_program_ok`), the `--defer-existing` retry, and the
#: deferral watch record (a command when the login was read, else in words).
CLOSED = re.compile(
    rf"python3 -c (?P<prog>{_WORD})"
    rf"|python3 \"\$\{{HARMONIC_FORGE_ROOT:-\$HOME/harmonic-forge\}}/tools/gh/gh_issue\.py\" "
    rf"--repo {_REPO} --defer-existing \d+ --deferral-trigger {_WORD} --deferral-owner {_WORD}"
    rf"|(?:python3 tools/gh/|run )record_deferral\.py --repo {_REPO} --issue \d+ --operator-login(?: {_WORD})?")
#: The only calls a closed-set program makes, and the one it exists to make.
_CALLS = {"sys.path.insert", "sys.exit", "gates.Route"}
_TARGETS = {"handoff_owed.discharge", "route_store.record_if_not_looser"}
#: What the rest of a repair, once its closed-set commands are cut, must not hold:
#: a shell metacharacter or quote, a flag, a script, or a command word (a prose
#: `;` stays: a command after one is still caught by its word or flag).
NOT_WORDS = re.compile(r"[`$|&<>\"'\\]|(?<![\w])--?[A-Za-z]|\S\.(?:py|sh)\b"
                       r"|\b(?:gh|git|mise|python3?|bash|sh|zsh|curl|wget|rm|mv|cp|sudo|cd|echo|jq"
                       r"|l[123]-post)\b")


def _program_ok(word: str) -> bool:
    """A `python3 -c` program is a closed-set repair only when its statements are
    imports and calls, and every call is the discharge or route record or a helper."""
    import ast  # noqa: PLC0415
    import shlex  # noqa: PLC0415
    tree = ast.parse(shlex.split(word)[0])
    if not all(isinstance(s, (ast.Import, ast.Expr)) for s in tree.body):
        return False
    if any(a.name not in {"sys", "handoff_owed", "gates", "route_store"}
           for s in tree.body if isinstance(s, ast.Import) for a in s.names):
        return False
    calls = [ast.unparse(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)]
    return set(calls) <= _CALLS | _TARGETS and sum(c in _TARGETS for c in calls) == 1


def _entries(err: str) -> list[str]:
    return [line.split("[GH]   - ", 1)[1] for line in err.splitlines() if line.startswith("[GH]   - ")]


class P2RepairsAreAClosedSet(unittest.TestCase):
    """Sticky verdict P2: every ROWS failure and every board early return ends in
    an entry whose repair is one of four commands or words, never a placeholder."""

    def assert_closed(self, entry: str) -> None:
        match = ENTRY.match(entry)
        self.assertIsNotNone(match, entry)
        repair = match["repair"]
        self.assertNotRegex(entry, r"<[\w .-]+>", "a <placeholder>")
        self.assertNotIn("route_store.record(", entry)
        rest = []
        at = 0
        for command in CLOSED.finditer(repair):
            if command["prog"] is not None:
                self.assertTrue(_program_ok(command["prog"]), command[0])
            rest.append(repair[at:command.start()])
            at = command.end()
        rest = " ".join(rest + [repair[at:]])
        # The review finding: a shell command outside the four passed, since only a
        # `python3` was checked. Anything not cut above must be plain words.
        self.assertNotRegex(rest, NOT_WORDS, f"not one of the four commands or plain words: {repair}")

    def _board(self, **edges):
        defaults = {"_run_board": {"return_value": base._completed('{"id": "IT_1"}')},
                    "_find_existing_item": {"return_value": None},
                    "_fetch_project_context": {"return_value": CTX},
                    "_set_single_select": {"return_value": True},
                    "_set_tier": {"return_value": True}}
        return {"add_to_board": None, "_route_at_filing": {"return_value": "2"}, **defaults, **edges}

    def test_every_summary_entry_is_in_the_closed_set(self):
        oops = OSError("could not start gh")
        scenarios = {}
        for call, argv, *_rest in ROWS.values():
            scenarios[f"{call} fails"] = (argv, {call: {"return_value": False}}, "")
            scenarios[f"{call} raises"] = (argv, {call: {"side_effect": oops}}, "")
        scenarios.update({
            "board add rejected": (PARKING, self._board(_run_board={
                "return_value": base._completed("", returncode=1)}), ""),
            "item id unreadable": (PARKING, self._board(_run_board={"return_value": base._completed("x")}), ""),
            "no project context": (PARKING, self._board(_fetch_project_context={"return_value": None}), ""),
            "project context raises": (PARKING, self._board(_fetch_project_context={"side_effect": oops}), ""),
            "field writes fail": (PARKING, self._board(_set_single_select={"return_value": False},
                                                       _set_tier={"side_effect": oops}), ""),
            "unmapped board": (PARKING, {"resolve_board_for_repo": {"side_effect": SystemExit("no board")},
                                         "_route_at_filing": {"return_value": "2"}}, ""),
            "no board configured": (PARKING, {"resolve_board_for_repo": {"return_value": ("", "")}}, ""),
            "login unread": (RECORDED, {"handoff_owed.discharge": {"return_value": -1}}, ""),
            "link fails": (Round1Survivors.BLOCKED, {"_run": {"return_value": base._completed("", 1)}}, ""),
            "exception post fails": (EXCEPTION, {"_run": {"return_value": base._completed("", 1)}}, ""),
            "deferral clear fails": (PARKING, {"handoff_owed.discharge": {"return_value": -1}}, ""),
            "obligation record fails (exception)": (EXCEPTION, {"handoff_owed.record": {"return_value": False}}, ""),
            "no issue number": (PARKING, {"create_issue": {"return_value": "https://github.com/o/r/pull/7"}}, ""),
        })
        for name, (argv, over, post) in scenarios.items():
            with self.subTest(scenario=name), \
                    patch("gh_issue.deferral_store.record_deferral", return_value=True):
                code, err, _ = _drive(argv, post_stdout=post, **over)
                entries = _entries(err)
                self.assertEqual(code, 1, err)
                self.assertTrue(entries, err)
                for entry in entries:
                    self.assert_closed(entry)

    def test_the_route_entry_is_in_the_closed_set(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "routing"))
        import route_store  # noqa: PLC0415
        with patch.object(route_store, "record", side_effect=RuntimeError("store down")):
            code, err, _ = _drive(PARKING + ["--body", "edits `tools/hooks/x.py`"], _route_at_filing=None)
        self.assertEqual(code, 1, err)
        route = [e for e in _entries(err) if e.startswith("route filing checkpoint")]
        self.assertEqual(len(route), 1, err)
        self.assert_closed(route[0])


class P3NotAttemptedListsComeFromOneHelper(unittest.TestCase):
    def test_an_unmapped_board_with_a_computed_route_names_path(self):
        code, err, _ = _drive(PARKING, resolve_board_for_repo={"side_effect": SystemExit("no board")},
                              _route_at_filing={"return_value": "2"})
        self.assertEqual(code, 1, err)
        self.assertIn("so the board add and Path/Status/Tier/Theme/Venture not attempted", err)

    def test_a_board_early_return_with_a_route_names_path(self):
        with patch("gh_issue._run_board", return_value=base._completed("", returncode=1)), \
             patch("gh_issue._find_existing_item", return_value=None):
            code, err, _ = _drive(PARKING, add_to_board=None, _route_at_filing={"return_value": "2"})
        self.assertEqual(code, 1, err)
        self.assertIn("- board add: not added, so Path/Status/Tier/Theme/Venture not attempted", err)

    def test_only_requested_fields_are_listed(self):
        self.assertEqual(gh_issue._not_attempted(gh_issue._requested(None, None, "T", None)),
                         "Status/Theme not attempted")


class P4AnUnparseableIssueNumberIsAnEntry(unittest.TestCase):
    def test_a_url_with_no_issue_number_exits_one_naming_the_skipped_steps(self):
        code, err, mocks = _drive(PARKING, create_issue={"return_value": "https://github.com/o/r/pull/7"},
                                  _post_deferral={"return_value": True})
        self.assertEqual(code, 1, err)
        self.assertIn("- issue number: none could be read from https://github.com/o/r/pull/7, so the "
                      "obligation record, the route filing checkpoint and the R-0352 deferral were "
                      "not attempted", err)
        # The review finding: the entry says these were skipped, so a call to any is a lie.
        mocks["record"].assert_not_called()
        mocks["_route_at_filing"].assert_not_called()
        mocks["_post_deferral"].assert_not_called()
        mocks["add_to_board"].assert_called_once()


if __name__ == "__main__":
    unittest.main()
