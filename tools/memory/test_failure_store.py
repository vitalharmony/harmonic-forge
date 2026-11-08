#!/usr/bin/env python3
"""Tests for failure_store.py (harmonic-forge#1288, AC6-AC8).

Every log, overlay and database is synthetic and lives in a temp folder;
nothing reads or writes the operator's real store.
"""
from __future__ import annotations

import io
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import failure_store as fs  # noqa: E402

REPO = HERE.parents[1]


def rec(ts: str, tool: str | None = None, klass: str | None = None, probe: str | None = None,
        session: str | None = None, turn: str | None = None, rule_ref: str | None = None) -> dict:
    return {"ts": ts, "caught_by": "self", "rule_existed": "none", "rule_ref": rule_ref,
            "expected": "x", "happened": "y", "lane": "1", "repo": None, "issue": 7,
            "session": session, "turns_lost": None, "work_discarded": None, "account": None,
            "turn": turn, "tool": tool, "class": klass, "probe": probe, "command": None,
            "class_list_version": 1 if klass else None}


class Store(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.memory = self.root / "operator-memory" / "protocol-failures.jsonl"
        self.testplan = self.root / "testplan" / "protocol-failures.jsonl"
        self.overlays = self.root / "operator-memory" / "failure-overlays"
        self.db = self.root / "operator-memory" / "failure-store.sqlite"
        self.logs = {"memory": self.memory, "testplan": self.testplan}
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("CLAUDE_CODE_SESSION_ID", None)

    def write(self, path: Path, rows: list, mode: str = "x") -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open(mode, encoding="utf-8") as handle:
            for row in rows:
                handle.write((row if isinstance(row, str) else json.dumps(row)) + "\n")

    def overlay(self, name: str, rows: list[dict]) -> None:
        self.write(self.overlays / name, rows)

    def build(self) -> dict:
        return fs.build(self.db, self.logs, self.overlays)

    def latest(self, tool: str, klass: str) -> dict | None:
        return fs.latest(self.db, self.logs, self.overlays, tool, klass)

    def query(self, sql: str, *params) -> list[tuple]:
        con = sqlite3.connect(self.db)
        try:
            return con.execute(sql, params).fetchall()
        finally:
            con.close()


class Build(Store):
    """AC7."""

    def test_build_reads_both_logs(self):
        self.write(self.memory, [rec("2026-11-01T00:00:00Z", "l1_post", "cwd", "pwd"),
                                 "torn {", "", rec("2026-11-02T00:00:00Z")])
        self.write(self.testplan, [rec("2026-11-03T00:00:00Z", "pytest", "local-check", "pytest -x")])
        counts = self.build()
        self.assertEqual(counts["failures"], {"memory": 2, "testplan": 1})
        self.assertEqual(counts["malformed"], 1)
        rows = self.query("SELECT log, line, tool, class, issue, class_list_version FROM failures ORDER BY log, line")
        self.assertEqual(rows, [("memory", 0, "l1_post", "cwd", "7", 1), ("memory", 3, None, None, "7", None),
                                ("testplan", 0, "pytest", "local-check", "7", 1)])

    def test_missing_logs_build_an_empty_store(self):
        self.assertEqual(self.build()["failures"], {"memory": 0, "testplan": 0})
        self.assertEqual(self.query("SELECT count(*) FROM failures"), [(0,)])

    def test_schema_has_the_key_index_and_view(self):
        self.build()
        names = {r[0] for r in self.query("SELECT name FROM sqlite_master")}
        self.assertTrue({"meta", "failures", "overlays", "failure_keys", "failures_key"} <= names)
        index = self.query("SELECT sql FROM sqlite_master WHERE name = 'failures_key'")[0][0]
        self.assertIn("(tool, class, ts)", index)

    def test_build_swaps_in_and_removes_stale_temp_files(self):
        self.db.parent.mkdir(parents=True)
        stale = self.db.parent / (self.db.name + ".abc123")
        stale.write_bytes(b"left by a failed build")
        other = self.db.parent / "unrelated.sqlite.keep"
        other.write_bytes(b"x")
        self.build()
        self.assertTrue(self.db.is_file())
        self.assertFalse(stale.exists())
        self.assertTrue(other.exists())
        self.assertEqual(sorted(p.name for p in self.db.parent.iterdir() if p.name.startswith(self.db.name)),
                         [self.db.name])

    def test_build_never_changes_a_log(self):
        self.write(self.memory, [rec("2026-11-01T00:00:00Z", "l1_post", "cwd", "pwd")])
        self.overlay("0001.jsonl", [{"log_line": 0, "key_v1": "git-push|target-state"}])
        before = self.memory.read_bytes()
        self.build()
        self.latest("git-push", "target-state")
        self.assertEqual(self.memory.read_bytes(), before)

    def test_store_source_opens_logs_read_only(self):
        source = Path(fs.__file__).read_text(encoding="utf-8")
        for pattern in (r"""open\([^)]*["'](?:w|a|r\+|x)[bt]?["']""", r"""["'](?:w|a|r\+|x)[bt]?["']""",
                        r"write_text", r"write_bytes"):
            with self.subTest(pattern=pattern):
                self.assertIsNone(re.search(pattern, source))


class Latest(Store):
    """AC7: the precedent-citation lookup."""

    def test_latest_returns_the_newest_with_its_probe(self):
        self.write(self.memory, [rec("2026-11-01T00:00:00Z", "l1_post", "cwd", "old probe"),
                                 rec("2026-11-03T00:00:00Z", "l1_post", "cwd", "new probe"),
                                 rec("2026-11-04T00:00:00Z", "l1_post", "args", "other class")])
        self.write(self.testplan, [rec("2026-11-02T00:00:00Z", "l1_post", "cwd", "testplan probe")])
        row = self.latest("l1_post", "cwd")
        self.assertEqual((row["log"], row["line"], row["probe"]), ("memory", 1, "new probe"))
        self.assertIsNone(self.latest("git-push", "cwd"))

    def test_latest_rebuilds_when_log_grew(self):
        self.write(self.memory, [rec("2026-11-01T00:00:00Z", "l1_post", "cwd", "first")])
        self.build()
        self.write(self.memory, [rec("2026-11-05T00:00:00Z", "l1_post", "cwd", "grown")], mode="a")
        self.assertEqual(self.latest("l1_post", "cwd")["probe"], "grown")
        self.write(self.testplan, [rec("2026-11-06T00:00:00Z", "l1_post", "cwd", "testplan")])
        self.assertEqual(self.latest("l1_post", "cwd")["probe"], "testplan")

    def test_latest_rebuilds_when_an_overlay_changed(self):
        self.write(self.memory, [rec("2026-11-01T00:00:00Z", "l1_post", "cwd", "p")])
        self.build()
        self.overlay("0001.jsonl", [{"log_line": 0, "key_v1": "git-push|env"}])
        self.assertEqual(self.latest("git-push", "env")["line"], 0)

    def test_latest_does_not_rebuild_when_nothing_changed(self):
        self.write(self.memory, [rec("2026-11-01T00:00:00Z", "l1_post", "cwd", "p")])
        self.build()
        with mock.patch.object(fs, "build", side_effect=AssertionError("rebuilt")):
            self.assertIsNotNone(self.latest("l1_post", "cwd"))

    def test_cli_prints_one_json_line(self):
        self.write(self.memory, [rec("2026-11-01T00:00:00Z", "l1_post", "cwd", "p")])
        argv = ["--db", str(self.db), "--memory-log", str(self.memory), "--testplan-log", str(self.testplan),
                "--overlays", str(self.overlays)]
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(fs.main([*argv, "latest", "--tool", "l1_post", "--class", "cwd"]), 0)
        self.assertEqual(json.loads(out.getvalue())["probe"], "p")
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(fs.main([*argv, "latest", "--tool", "l1_post", "--class", "env"]), 1)
            self.assertEqual(fs.main([*argv, "build"]), 0)


class Overlays(Store):
    """AC8: relabel without rewriting."""

    def setUp(self) -> None:
        super().setUp()
        self.write(self.memory, [rec("2026-11-01T00:00:00Z", "l1_post", "cwd", "own probe"),
                                 rec("2026-11-02T00:00:00Z")])
        self.write(self.testplan, [rec("2026-11-03T00:00:00Z", "pytest", "local-check", "p")])

    def key(self, log: str, line: int) -> tuple:
        return self.query("SELECT tool, class, probe, own_tool, own_class, overlaid FROM failure_keys "
                          "WHERE log = ? AND line = ?", log, line)[0]

    def test_overlay_rekeys_while_the_record_and_log_stay_unchanged(self):
        before = self.memory.read_bytes()
        self.overlay("0001.jsonl", [{"log_line": 0, "key_v1": "git-push|target-state", "probe": "ls-remote"},
                                    {"log_line": 1, "key_v1": "l2_post|body"}])
        self.build()
        self.assertEqual(self.key("memory", 0), ("git-push", "target-state", "ls-remote", "l1_post", "cwd", 1))
        self.assertEqual(self.key("memory", 1), ("l2_post", "body", None, None, None, 1))
        self.assertEqual(self.query("SELECT tool, class, probe FROM failures WHERE log = 'memory' AND line = 0"),
                         [("l1_post", "cwd", "own probe")])
        self.assertEqual(self.memory.read_bytes(), before)
        self.assertEqual(self.latest("git-push", "target-state")["line"], 0)
        self.assertIsNone(self.latest("l1_post", "cwd"))

    def test_overlay_without_probe_keeps_the_records_probe(self):
        self.overlay("0001.jsonl", [{"log_line": 0, "key_v1": "git-push|env"}])
        self.build()
        self.assertEqual(self.key("memory", 0)[:3], ("git-push", "env", "own probe"))

    def test_latest_overlay_wins(self):
        self.overlay("0001.jsonl", [{"log_line": 0, "key_v1": "git-push|env"},
                                    {"log_line": 0, "key_v1": "gh-api|args"}])
        self.overlay("0002.jsonl", [{"log_line": 0, "key_v1": "kill_check|receipt"}])
        self.overlay("0000.jsonl", [{"log_line": 0, "key_v1": "l2_post|shell"}])
        self.build()
        self.assertEqual(self.key("memory", 0)[:2], ("kill_check", "receipt"))

    def test_last_row_within_a_file_wins(self):
        self.overlay("0001.jsonl", [{"log_line": 0, "key_v1": "git-push|env"},
                                    {"log_line": 0, "key_v1": "gh-api|args"}])
        self.build()
        self.assertEqual(self.key("memory", 0)[:2], ("gh-api", "args"))

    def test_empty_key_rekeys_nothing_and_is_counted(self):
        self.overlay("0001.jsonl", [{"log_line": 0, "key_v1": ""}, {"log_line": 1}, "not json",
                                    {"log_line": 1, "key_v1": "no-separator"}])
        counts = self.build()
        self.assertEqual((counts["overlays"], counts["skipped"], counts["overlay_malformed"]), (0, 2, 2))
        self.assertEqual(self.key("memory", 0), ("l1_post", "cwd", "own probe", "l1_post", "cwd", 0))
        out = io.StringIO()
        with redirect_stdout(out):
            fs.main(["--db", str(self.db), "--memory-log", str(self.memory), "--testplan-log", str(self.testplan),
                     "--overlays", str(self.overlays), "build"])
        self.assertIn("2 skipped (empty key_v1)", out.getvalue())

    def test_overlay_addresses_the_named_log(self):
        self.overlay("0001.jsonl", [{"log_line": 0, "key_v1": "pytest|env", "log": "testplan"}])
        self.build()
        self.assertEqual(self.key("testplan", 0)[:2], ("pytest", "env"))
        self.assertEqual(self.key("memory", 0)[:2], ("l1_post", "cwd"))


class ForgeHoldsNoData(Store):
    """AC6."""

    def fake_forge(self, name: str, git_file: bool = False) -> Path:
        forge = self.root / name
        (forge / "tools" / "memory").mkdir(parents=True)
        (forge / "tools" / "memory" / "capture_failure.py").write_text("# fake\n", encoding="utf-8")
        if git_file:
            (forge / ".git").write_text("gitdir: /elsewhere\n", encoding="utf-8")
        else:
            (forge / ".git").mkdir()
        return forge

    def refused(self, db: Path, command: list[str]) -> None:
        err = io.StringIO()
        with redirect_stderr(err), redirect_stdout(io.StringIO()):
            code = fs.main(["--db", str(db), "--memory-log", str(self.memory), "--testplan-log",
                            str(self.testplan), "--overlays", str(self.overlays), *command])
        self.assertEqual(code, 2)
        self.assertIn("harmonic-forge", err.getvalue())
        self.assertFalse(db.exists())
        self.assertEqual(list(db.parent.glob(db.name + "*")) if db.parent.is_dir() else [], [])

    def test_db_inside_a_forge_checkout_refuses(self):
        self.write(self.memory, [rec("2026-11-01T00:00:00Z", "l1_post", "cwd", "p")])
        for db in (self.fake_forge("forge") / "failure-store.sqlite",
                   self.fake_forge("worktree", git_file=True) / "x" / "failure-store.sqlite",
                   REPO / "failure-store-probe.sqlite"):
            for command in (["build"], ["latest", "--tool", "l1_post", "--class", "cwd"]):
                with self.subTest(db=db, command=command[0]):
                    self.refused(db, command)

    def test_db_through_a_symlink_into_forge_refuses(self):
        link = self.root / "looks-safe"
        link.symlink_to(self.fake_forge("forge-target"))
        self.refused(link / "failure-store.sqlite", ["build"])

    def test_temp_file_is_guarded(self):
        forge = self.fake_forge("forge-tmp")
        real = tempfile.mkstemp

        def into_forge(**kwargs):
            return real(dir=forge, prefix=kwargs.get("prefix"))

        with mock.patch.object(fs.tempfile, "mkstemp", side_effect=into_forge):
            with self.assertRaises(fs.Refused):
                self.build()
        self.assertEqual(list(forge.glob("failure-store.sqlite*")), [])

    def test_gitignore_covers_failure_data(self):
        lines = (REPO / ".gitignore").read_text(encoding="utf-8").splitlines()
        for pattern in ("*.sqlite", "*.sqlite3", "protocol-failures*.jsonl", "failure-overlays/"):
            with self.subTest(pattern=pattern):
                self.assertIn(pattern, lines)

    def test_no_committed_file_is_failure_data(self):
        listed = subprocess.run(["git", "-C", str(REPO), "ls-files"], capture_output=True, text=True,
                                check=True).stdout.splitlines()
        data = [p for p in listed if re.search(r"(protocol-failures[^/]*\.jsonl|\.sqlite3?|failure-overlays/)$|"
                                               r"failure-overlays/", p)]
        self.assertEqual(data, [])


if __name__ == "__main__":
    unittest.main()
