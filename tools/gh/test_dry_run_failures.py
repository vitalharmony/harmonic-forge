#!/usr/bin/env python3
"""harmonic-forge#1544 (reforged): a failed `l1_post --dry-run` is recorded under
l1_post's resolved `(repo, issue, kind)` and the session that ran it, surfaced
once to that session only, and cleared by `_dry_run_clean`."""
from __future__ import annotations

import argparse
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "hooks"))
import _dry_run_failures as drf  # noqa: E402
import surface_dry_run_failures as hook  # noqa: E402

L1_POST = HERE / "l1_post.py"
REPO = "vitalharmony/harmonic-forge"
SID = "test-session-1544"


def ns(**kw) -> argparse.Namespace:
    base = {"repo": REPO, "issue": 7, "kind": "ae", "sha": "a" * 40, "dry_run": True}
    return argparse.Namespace(**{**base, **kw})


class Base(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = tmp.name
        patcher = mock.patch.dict(os.environ, {"HOME": self.home, "CLAUDE_CODE_SESSION_ID": SID})
        patcher.start()
        self.addCleanup(patcher.stop)

    def wrapped(self, main, resolve_repo=None):
        """drf.run(main) in-process: (exit code, stderr text)."""
        err = io.StringIO()
        code = 0
        with redirect_stderr(err):
            try:
                drf.run(main, resolve_repo)
            except SystemExit as exc:
                code = exc.code
        return code, err.getvalue()

    def hook(self, session_id: str | None = SID) -> str:
        out = io.StringIO()
        payload = json.dumps({"session_id": session_id} if session_id else {})
        with redirect_stdout(out), mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": ""}):
            self.assertEqual(hook.main(io.StringIO(payload)), 0)
        return out.getvalue()


class Recording(Base):
    def test_a_failed_parsed_dry_run_records_one_entry(self) -> None:
        def main():
            drf.bind(ns())
            raise SystemExit("[l1-post] overlap with feat/1505")
        self.wrapped(main)
        entries = drf.pending(SID)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0][1]["text"], "[l1-post] overlap with feat/1505")
        self.assertEqual((entries[0][1]["repo"], entries[0][1]["kind"]), (REPO, "ae"))

    def test_a_wait_exit_is_recorded_as_a_wait(self) -> None:
        """Epoch-1 pass 1 PATCH: exit 3 is an unread blocker too (a CI wait, or
        _carry_forward's read/fetch error), so it is recorded with its code."""
        def main():
            drf.bind(ns())
            sys.stderr.write("WAIT: CI is pending on aaaaaaaaaaaa\n")
            raise SystemExit(drf.WAIT_EXIT)
        self.wrapped(main)
        (_, data), = drf.pending(SID)
        self.assertEqual(data["exit"], 3)
        self.assertIn("WAIT: CI is pending", data["text"])

    def test_a_parsed_real_post_records_nothing(self) -> None:
        def main():
            drf.bind(ns(dry_run=False))
            raise SystemExit("[l1-post] refused")
        self.wrapped(main)
        self.assertEqual(drf.pending(SID), [])

    def test_an_argparse_exit_records_one_unparsed_entry(self) -> None:
        def main():
            sys.stderr.write("l1_post.py: error: the following arguments are required: --branch\n")
            raise SystemExit(2)
        self.wrapped(main)
        entries = drf.pending(SID)
        self.assertEqual([e[1]["kind"] for e in entries], ["unparsed"])
        self.assertIn("--branch", entries[0][1]["text"])

    def test_a_stale_binding_is_never_this_runs(self) -> None:
        drf.bind(ns())  # an earlier in-process call's Namespace

        def main():
            raise SystemExit(2)  # this run fails before parsing
        self.wrapped(main)
        self.assertEqual([e[1]["kind"] for e in drf.pending(SID)], ["unparsed"])

    def test_the_wrappers_own_resolve_failing_leaves_the_exit_byte_identical(self) -> None:
        def main():
            drf.bind(ns(repo=None))
            sys.stderr.write("the original stderr\n")
            raise SystemExit("[l1-post] the original refusal")

        def resolve_repo(_value):
            raise SystemExit("[l1-post] --repo was omitted and the invoking checkout has no readable origin")
        code, err = self.wrapped(main, resolve_repo)
        self.assertEqual((code, err), ("[l1-post] the original refusal", "the original stderr\n"))
        self.assertEqual(len(drf.pending(SID)), 1)

    def test_no_session_writes_nothing(self) -> None:
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": ""}):
            def main():
                drf.bind(ns())
                raise SystemExit("[l1-post] refused")
            self.wrapped(main)
        self.assertFalse(drf.state_dir().exists())

    def test_a_newer_record_is_a_separate_file_from_the_one_read(self) -> None:
        drf.record(REPO, 7, "ae", "a" * 40, "first")
        (read_path, _), = drf.pending(SID)
        drf.record(REPO, 7, "ae", "b" * 40, "second")
        drf.consume(read_path)
        self.assertEqual([e[1]["text"] for e in drf.pending(SID)], ["second"])

    def test_pending_skips_a_malformed_entry_and_keeps_the_valid_one(self) -> None:
        drf.record(REPO, 7, "ae", "a" * 40, "valid")
        (drf.session_dir(SID) / "o_r#7-ae.1.json").write_text("{not json")
        self.assertEqual([e[1]["text"] for e in drf.pending(SID)], ["valid"])


class Clearing(Base):
    def at(self, start_ns: int):
        """Act as a run that started at `start_ns`."""
        return mock.patch.object(drf, "_started", start_ns)

    def test_a_failure_from_a_run_started_after_the_clean_one_survives(self) -> None:
        """Epoch-1 pass 2: the latest-STARTED run wins, whatever order the
        failure and the clean land in."""
        with self.at(2_000), redirect_stdout(io.StringIO()):
            drf.clean("ae", REPO, 7, "a" * 40)          # clean run started at 2000
        with self.at(3_000):
            drf.record(REPO, 7, "ae", "b" * 40, "newer failure")  # started later
        self.assertEqual([e[1]["text"] for e in drf.pending(SID)], ["newer failure"])

    def test_a_failure_from_a_run_started_before_the_clean_one_is_superseded(self) -> None:
        """The older run fails after the newer clean run has already finished."""
        with self.at(2_000), redirect_stdout(io.StringIO()):
            drf.clean("ae", REPO, 7, "a" * 40)
        with self.at(1_000):
            drf.record(REPO, 7, "ae", "b" * 40, "stale failure")  # started first, wrote last
        self.assertEqual(drf.pending(SID), [])

    def test_a_clean_run_never_erases_a_failure_from_a_later_started_run(self) -> None:
        with self.at(3_000):
            drf.record(REPO, 7, "ae", "b" * 40, "newer failure")
        with self.at(2_000), redirect_stdout(io.StringIO()):
            drf.clean("ae", REPO, 7, "a" * 40)
        self.assertEqual([e[1]["text"] for e in drf.pending(SID)], ["newer failure"])

    def test_clean_never_touches_another_sessions_entry(self) -> None:
        drf.record(REPO, 7, "ae", "a" * 40, "other session's failure", sid="other-session")
        with redirect_stdout(io.StringIO()):
            drf.clean("ae", REPO, 7, "a" * 40)
        self.assertEqual(len(drf.pending("other-session")), 1)

    def test_dry_run_clean_clears_an_entry_recorded_without_repo(self) -> None:
        """Recorded by a run that omitted --repo, cleared by one that spells it out."""
        def main():
            drf.bind(ns(repo=None))
            raise SystemExit("[l1-post] refused")
        self.wrapped(main, resolve_repo=lambda value: value or REPO)
        self.assertEqual(len(drf.pending(SID)), 1)
        import l1_post
        out = io.StringIO()
        with mock.patch.object(l1_post._preflight, "record"), redirect_stdout(out):
            l1_post._dry_run_clean("ae", REPO, 7, "c" * 40)
        self.assertEqual(drf.pending(SID), [])
        self.assertEqual(out.getvalue(), f"[l1-post] dry run clean: ae {REPO}#7 @ {'c' * 12}\n")


class ShaWriteback(Base):
    def test_a_failure_after_sha_resolution_records_the_resolved_sha(self) -> None:
        """l1_post writes the resolved SHA back onto the bound Namespace."""
        import l1_post
        argv = ["l1_post.py", "--repo", REPO, "--issue", "7", "--kind", "ae-and-sweep",
                "--sha", "HEAD", "--branch", "x", "--ae-file", "/nonexistent-ae.md",
                "--sweep-file", "/nonexistent-sweep.md", "--spec-comment", "1", "--dry-run"]
        with mock.patch.object(sys, "argv", argv), \
                mock.patch.object(l1_post, "apply_project_identity", lambda repo: None), \
                mock.patch.object(l1_post, "resolve_sha", lambda value: "f" * 40):
            code, _ = self.wrapped(l1_post.main, l1_post.resolve_repo)
        self.assertNotIn(code, (None, 0))
        (_, data), = drf.pending(SID)
        self.assertEqual(data["sha"], "f" * 40)


class Hook(Base):
    def test_a_long_failure_keeps_its_tail(self) -> None:
        """Epoch-1 pass 2: argparse's error: line is at the end of a long usage block."""
        drf.record(REPO, 7, "ae", "b" * 40, "usage: " + "x" * 700 + " error: the following arguments are required: --sha")
        ctx = json.loads(self.hook())["hookSpecificOutput"]["additionalContext"]
        self.assertIn("required: --sha", ctx)
        self.assertIn("usage: ", ctx)

    def test_a_wait_entry_is_shown_as_a_wait(self) -> None:
        drf.record(REPO, 7, "ae", "b" * 40, "WAIT: CI is pending", code=3)
        ctx = json.loads(self.hook())["hookSpecificOutput"]["additionalContext"]
        self.assertIn(f"l1_post dry run must WAIT (exit 3) for {REPO}#7 ae", ctx)

    def test_an_entry_is_injected_once_to_its_own_session(self) -> None:
        drf.record(REPO, 7, "ae", "b" * 40, "[l1-post] refused")
        ctx = json.loads(self.hook())["hookSpecificOutput"]["additionalContext"]
        self.assertIn(f"l1_post dry run FAILED for {REPO}#7 ae @ bbbbbbbbbbbb: [l1-post] refused", ctx)
        self.assertEqual(self.hook(), "")

    def test_another_session_sees_nothing_and_leaves_the_entry(self) -> None:
        drf.record(REPO, 7, "ae", "b" * 40, "[l1-post] refused")
        self.assertEqual(self.hook("some-other-session"), "")
        self.assertEqual(len(drf.pending(SID)), 1)

    def test_a_failed_consume_still_emits_every_line(self) -> None:
        drf.record(REPO, 7, "ae", "a" * 40, "first")
        drf.record(REPO, 8, "ae", "b" * 40, "second")
        calls = []

        def consume(path):
            calls.append(path)
            if len(calls) == 2:
                raise OSError("disk")
            path.unlink()
        with mock.patch.object(drf, "consume", consume):
            ctx = json.loads(self.hook())["hookSpecificOutput"]["additionalContext"]
        self.assertIn("first", ctx)
        self.assertIn("second", ctx)

    def test_a_malformed_payload_or_store_injects_nothing(self) -> None:
        drf.session_dir(SID).mkdir(parents=True)
        (drf.session_dir(SID) / "o_r#7-ae.1.json").write_text("{not json")
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(hook.main(io.StringIO("not json")), 0)
        self.assertEqual(out.getvalue(), "")


class LaneWiring(unittest.TestCase):
    def test_the_lane_hook_set_wires_it_as_this_repo_does(self) -> None:
        """Epoch-1 pass 2: lane sessions in other repos get hooks only from
        tools/lane/lane_hooks.json (harmonic-forge#1307); byte-identical to this
        repo's entry so the two run once, not twice."""
        def commands(path):
            data = json.loads(path.read_text(encoding="utf-8"))
            return [h["command"] for block in data["hooks"]["UserPromptSubmit"]
                    for h in block["hooks"] if "surface_dry_run_failures.py" in h["command"]]
        lane = commands(ROOT / "tools" / "lane" / "lane_hooks.json")
        project = commands(ROOT / ".claude" / "settings.json")
        self.assertEqual(len(lane), 1)
        self.assertEqual(lane, project)


class ThroughL1PostMain(Base):
    """The real entry point, as a subprocess: no --repo, an abbreviated --dry."""

    def l1_post(self, *argv: str) -> subprocess.CompletedProcess:
        env = {k: v for k, v in os.environ.items() if k not in ("GH_TOKEN", "GITHUB_TOKEN")}
        # LANE=1 with an empty HOME: apply_project_identity refuses locally
        # (no lane token) instead of reaching the real gh.
        env.update(HOME=self.home, CLAUDE_CODE_SESSION_ID=SID, LANE="1")
        return subprocess.run([sys.executable, str(L1_POST), *argv], cwd=ROOT, env=env,
                              capture_output=True, text=True, timeout=120)

    def test_a_refusal_before_resolution_records_under_the_resolved_repo(self) -> None:
        result = self.l1_post("--issue", "7", "--kind", "rework", "--sha", "HEAD",
                              "--branch", "x", "--dry")
        self.assertEqual(result.returncode, 1, result.stderr)
        (_, data), = drf.pending(SID)
        self.assertEqual((data["repo"], data["issue"], data["kind"]), (REPO, "7", "rework"))
        self.assertIn("--rework-kind", data["text"])

    def test_a_refusal_after_resolution_records_once(self) -> None:
        result = self.l1_post("--issue", "7", "--kind", "handoff", "--sha", "HEAD", "--branch", "x",
                              "--file", "/nonexistent.md", "--plan-first", "false", "--dry-run")
        self.assertEqual(result.returncode, 1, result.stderr)
        (_, data), = drf.pending(SID)
        self.assertEqual((data["repo"], data["kind"]), (REPO, "handoff"))


if __name__ == "__main__":
    unittest.main()
