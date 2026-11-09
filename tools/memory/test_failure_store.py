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
            "turn": turn, "tool": tool, "class": klass, "probe": probe,
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

    def test_a_missing_testplan_log_builds_empty(self):
        self.write(self.memory, [])
        self.assertEqual(self.build()["failures"], {"memory": 0, "testplan": 0})
        self.assertEqual(self.query("SELECT count(*) FROM failures"), [(0,)])

    def test_a_missing_memory_log_exits_3_never_1(self):
        """Reforge pass-1 survivor 3: a missing memory log built an empty
        store, so `latest` exited 1 ("no precedent") on a log it never read."""
        self.write(self.testplan, [rec("2026-11-01T00:00:00Z", "l1_post", "cwd", "p")])
        self.assert_exits_3("missing memory log")
        with self.assertRaises(FileNotFoundError):
            self.build()

    def test_schema_has_the_key_index_and_view(self):
        self.write(self.memory, [])
        self.build()
        names = {r[0] for r in self.query("SELECT name FROM sqlite_master")}
        self.assertTrue({"meta", "failures", "overlays", "failure_keys", "failures_key"} <= names)
        index = self.query("SELECT sql FROM sqlite_master WHERE name = 'failures_key'")[0][0]
        self.assertIn("(tool, class, ts)", index)

    def test_build_swaps_in_and_removes_stale_temp_files(self):
        self.write(self.memory, [])
        stale = [self.db.parent / (self.db.name + ".build-4242-abc_123.tmp"),
                 self.db.parent / (self.db.name + ".build-4242-abc_123.tmp-journal")]
        for path in stale:
            path.write_bytes(b"left by a failed build")
        other = self.db.parent / "unrelated.sqlite.keep"
        other.write_bytes(b"x")
        self.build()
        self.assertTrue(self.db.is_file())
        for path in stale:
            self.assertFalse(path.exists())
        self.assertTrue(other.exists())
        self.assertEqual(sorted(p.name for p in self.db.parent.iterdir() if p.name.startswith(self.db.name)),
                         [self.db.name, self.db.name + ".lock"])

    def test_build_never_deletes_a_file_that_is_not_its_temp_file(self):
        """Survivor 1: the sweep was a `<db>.*` glob, so any file sharing the
        database's name prefix was deleted."""
        self.write(self.memory, [])
        kept = [self.db.parent / (self.db.name + suffix)
                for suffix in (".keep", ".abc123", ".bak", ".build-x.tmp", ".build-1-a.tmp.old")]
        for path in kept:
            path.write_bytes(b"not a temp file")
        self.build()
        for path in kept:
            with self.subTest(path=path.name):
                self.assertEqual(path.read_bytes(), b"not a temp file")

    def test_concurrent_rebuilds_never_crash(self):
        """Survivor 1: two builds at once swept each other's live temp file.
        Builds are serialized, so every concurrent `build` and `latest` exits 0
        and leaves no temp file behind."""
        self.write(self.memory, [rec("2026-11-01T00:00:00Z", "l1_post", "cwd", "pwd")])
        argv = [sys.executable, str(HERE / "failure_store.py"), "--db", str(self.db),
                "--memory-log", str(self.memory), "--testplan-log", str(self.testplan),
                "--overlays", str(self.overlays)]
        for round_ in range(4):
            self.write(self.memory, [rec(f"2026-11-02T00:00:0{round_}Z", "l1_post", "cwd", "pwd")], mode="a")
            procs = [subprocess.Popen(argv + (["build"] if i % 2 else ["latest", "--tool", "l1_post",
                                                                         "--class", "cwd"]),
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                     for i in range(8)]
            for proc in procs:
                _out, err = proc.communicate(timeout=60)
                with self.subTest(round=round_):
                    self.assertEqual(proc.returncode, 0, err)
        self.assertEqual(sorted(p.name for p in self.db.parent.iterdir() if p.name.startswith(self.db.name)),
                         [self.db.name, self.db.name + ".lock"])

    def test_a_build_error_exits_3_not_latests_not_found_1(self):
        """Survivor 1: a crashed rebuild exited 1, which `latest` also uses for
        "no stored failure", so a caller read a crash as "no precedent"."""
        argv = ["--db", str(self.db), "--memory-log", str(self.memory), "--testplan-log",
                str(self.testplan), "--overlays", str(self.overlays)]
        for command in (["build"], ["latest", "--tool", "l1_post", "--class", "cwd"]):
            for error in (OSError("disk I/O error"), sqlite3.OperationalError("disk I/O error")):
                with self.subTest(command=command[0], error=type(error).__name__):
                    err = io.StringIO()
                    with mock.patch.object(fs.sqlite3, "connect", side_effect=error), \
                            redirect_stderr(err), redirect_stdout(io.StringIO()):
                        self.assertEqual(fs.main([*argv, *command]), fs.EXIT_ERROR)
                    self.assertNotEqual(fs.EXIT_ERROR, 1)
                    self.assertNotEqual(fs.EXIT_NOT_FOUND, 1)
                    self.assertIn("error", err.getvalue())

    def _main_argv(self) -> list[str]:
        return ["--db", str(self.db), "--memory-log", str(self.memory), "--testplan-log",
                str(self.testplan), "--overlays", str(self.overlays)]

    def assert_exits_3(self, label: str) -> None:
        for command in (["build"], ["latest", "--tool", "l1_post", "--class", "cwd"]):
            with self.subTest(case=label, command=command[0]):
                err = io.StringIO()
                with redirect_stderr(err), redirect_stdout(io.StringIO()):
                    self.assertEqual(fs.main([*self._main_argv(), *command]), fs.EXIT_ERROR,
                                     err.getvalue())
                self.assertIn("error", err.getvalue())

    def test_an_unreadable_log_exits_3_never_1(self):
        """Pass-2 survivor 4: an unreadable log read as empty, so `latest`
        exited 1 ("no precedent") and `build` reported 0 failures."""
        self.write(self.testplan, [rec("2026-11-01T00:00:00Z", "l1_post", "cwd", "p")])
        self.memory.mkdir(parents=True)  # a directory: IsADirectoryError, not FileNotFoundError
        self.assert_exits_3("directory")
        self.memory.rmdir()
        if os.geteuid() != 0:
            self.write(self.memory, [rec("2026-11-01T00:00:00Z", "l1_post", "cwd", "p")])
            self.memory.chmod(0)
            self.addCleanup(self.memory.chmod, 0o600)
            self.assert_exits_3("permission")

    def test_only_a_missing_log_reads_as_empty(self):
        self.write(self.memory, [rec("2026-11-01T00:00:00Z", "l1_post", "cwd", "p")])
        self.assertEqual(fs._read_bytes(self.root / "absent.jsonl"), None)
        self.assertEqual(self.build()["failures"], {"memory": 1, "testplan": 0})

    def test_an_overflowing_integer_field_exits_3_never_1(self):
        """Pass-2 survivor 6: class_list_version = 2**100 raised an uncaught
        OverflowError instead of exiting 3."""
        record = rec("2026-11-01T00:00:00Z", "l1_post", "cwd", "p")
        record["class_list_version"] = 2 ** 100
        self.write(self.memory, [record])
        self.assert_exits_3("overflow")

    def test_a_legacy_command_key_is_not_stored(self):
        record = rec("2026-11-01T00:00:00Z", "l1_post", "cwd", "p")
        record["command"] = "legacy text"
        self.write(self.memory, [record])
        self.build()
        columns = [row[1] for row in self.query("PRAGMA table_info(failures)")]
        self.assertNotIn("command", columns)
        self.assertNotIn("command", fs.COLUMNS)

    def test_db_over_a_source_refuses(self):
        """Survivor 5: `--db` naming a log or an overlay replaced it with the
        SQLite file."""
        self.write(self.memory, [rec("2026-11-01T00:00:00Z", "l1_post", "cwd", "pwd")])
        self.write(self.testplan, [rec("2026-11-01T00:00:00Z")])
        self.overlay("0001.jsonl", [{"log_line": 0, "key_v1": "git-push|target-state"}])
        sources = [self.memory, self.testplan, self.overlays / "0001.jsonl"]
        before = {p: p.read_bytes() for p in sources}
        for db in (*sources, self.memory.with_suffix(""), self.overlays / "store.sqlite",
                   self.root / "operator-memory" / "x" / ".." / "protocol-failures.jsonl"):
            for command in (["build"], ["latest", "--tool", "l1_post", "--class", "cwd"]):
                with self.subTest(db=str(db), command=command[0]):
                    err = io.StringIO()
                    with redirect_stderr(err), redirect_stdout(io.StringIO()):
                        code = fs.main(["--db", str(db), "--memory-log", str(self.memory), "--testplan-log",
                                        str(self.testplan), "--overlays", str(self.overlays), *command])
                    self.assertEqual(code, 2, err.getvalue())
        self.assertEqual({p: p.read_bytes() for p in sources}, before)
        self.assertFalse((self.overlays / "store.sqlite").exists())
        self.assertFalse(self.memory.with_suffix("").exists())

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

    def test_a_store_from_an_older_schema_or_version_is_rebuilt(self):
        """Reforge pass-1 survivor 2: the fingerprint hashed only the logs and
        overlays, so a store built by an older schema (one with a `command`
        column) was served unchanged."""
        self.write(self.memory, [rec("2026-11-01T00:00:00Z", "l1_post", "cwd", "p")])
        old_schema = self.root / "old.sql"
        old_schema.write_text(fs.SCHEMA.read_text(encoding="utf-8") + "\n-- older\n", encoding="utf-8")
        for label, patch in (("schema", mock.patch.object(fs, "SCHEMA", old_schema)),
                             ("version", mock.patch.object(fs, "STORE_VERSION", fs.STORE_VERSION - 1))):
            with self.subTest(older=label):
                with patch:
                    self.build()
                with mock.patch.object(fs, "build", side_effect=AssertionError("rebuilt")):
                    with self.assertRaises(AssertionError):
                        self.latest("l1_post", "cwd")
                self.assertEqual(self.latest("l1_post", "cwd")["probe"], "p")

    def test_an_error_writing_the_result_exits_3(self):
        """Reforge pass-1 survivor 6: the result was printed outside the
        exit-3 handler, so a closed pipe or full disk exited 1."""
        self.write(self.memory, [rec("2026-11-01T00:00:00Z", "l1_post", "cwd", "p")])
        argv = ["--db", str(self.db), "--memory-log", str(self.memory), "--testplan-log", str(self.testplan),
                "--overlays", str(self.overlays)]

        class Broken(io.StringIO):
            def write(self, _text):
                raise OSError("No space left on device")

        for command in (["latest", "--tool", "l1_post", "--class", "cwd"], ["build"]):
            with self.subTest(command=command[0]):
                err = io.StringIO()
                with redirect_stdout(Broken()), redirect_stderr(err):
                    self.assertEqual(fs.main([*argv, *command]), fs.EXIT_ERROR)
                self.assertIn("error", err.getvalue())

    @unittest.skipUnless(Path("/dev/full").exists(), "needs /dev/full")
    def test_a_full_stdout_exits_3_out_of_process(self):
        """Reforge pass-2 survivor 3: a buffered write that fails is flushed
        again at interpreter shutdown, outside the handler, and the process
        exits 120. Only a child process sees the shutdown flush."""
        self.write(self.memory, [rec("2026-11-01T00:00:00Z", "l1_post", "cwd", "p")])
        argv = [sys.executable, str(HERE / "failure_store.py"), "--db", str(self.db),
                "--memory-log", str(self.memory), "--testplan-log", str(self.testplan),
                "--overlays", str(self.overlays)]
        for command in (["latest", "--tool", "l1_post", "--class", "cwd"], ["build"]):
            with self.subTest(command=command[0]), open("/dev/full", "w", encoding="utf-8") as full:
                proc = subprocess.run(argv + command, stdout=full, stderr=subprocess.PIPE, text=True,
                                      timeout=60, check=False)
                self.assertEqual(proc.returncode, fs.EXIT_ERROR, proc.stderr)
                self.assertIn("error", proc.stderr)

    def test_cli_prints_one_json_line(self):
        self.write(self.memory, [rec("2026-11-01T00:00:00Z", "l1_post", "cwd", "p")])
        argv = ["--db", str(self.db), "--memory-log", str(self.memory), "--testplan-log", str(self.testplan),
                "--overlays", str(self.overlays)]
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(fs.main([*argv, "latest", "--tool", "l1_post", "--class", "cwd"]), 0)
        self.assertEqual(json.loads(out.getvalue())["probe"], "p")
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(fs.main([*argv, "latest", "--tool", "l1_post", "--class", "env"]), fs.EXIT_NOT_FOUND)
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
        # Only "" is a skip: an absent, null or false key_v1 is malformed
        # (harmonic-forge#1289 epoch 4).
        self.overlay("0001.jsonl", [{"log_line": 0, "key_v1": ""}, {"log_line": 0, "key_v1": ""}, {"log_line": 1},
                                    {"log_line": 1, "key_v1": None}, {"log_line": 1, "key_v1": False}, "not json",
                                    {"log_line": 1, "key_v1": "no-separator"}])
        counts = self.build()
        self.assertEqual((counts["overlays"], counts["skipped"], counts["overlay_malformed"]), (0, 2, 5))
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



class TestNotFoundExitCode(unittest.TestCase):
    """harmonic-forge#1288 operator ruling: 'no stored failure' is exit 4, never 1."""

    def test_not_found_is_4_and_distinct_from_every_other_code(self):
        self.assertEqual(fs.EXIT_NOT_FOUND, 4)
        self.assertNotIn(fs.EXIT_NOT_FOUND, (0, 1, 2, fs.EXIT_ERROR))


if __name__ == "__main__":
    unittest.main()
