#!/usr/bin/env python3
"""Tests for never_refuse_sample.py (harmonic-forge#1289).

Every transcript is synthetic and lives in a temp folder; token-shaped strings
are built by concatenation. Nothing here reads the operator's transcripts or
memory store.
"""
from __future__ import annotations

import fnmatch
import hashlib
import inspect
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import never_refuse_sample as nrs  # noqa: E402
import replay_harness as rh  # noqa: E402
import secret_scan  # noqa: E402

REPO = HERE.parents[1]
HOME = "/home/fixture-user"
UNTIL = "2026-12-01T00:00:00Z"
GH_TOKEN = "gh" + "p_" + "A1b2" * 9


def call(cid: str, command: str, ts: str = "2026-11-01T00:00:00Z", cwd: str = "/srv/work",
         version: str = "2.0.0", session: str = "s1") -> dict:
    return {"type": "assistant", "timestamp": ts, "cwd": cwd, "sessionId": session,
            "isSidechain": False, "version": version,
            "message": {"content": [{"type": "tool_use", "id": cid, "name": "Bash",
                                     "input": {"command": command}}]}}


def result(cid: str, text: str = "ok", is_error: bool = False, decision: str | None = None,
           stdout: bool | None = None, ts: str = "2026-11-01T00:00:01Z", content=None) -> dict:
    record = {"type": "user", "timestamp": ts, "version": "2.0.0",
              "message": {"content": [{"type": "tool_result", "tool_use_id": cid, "is_error": is_error,
                                       "content": text if content is None else content}]}}
    if decision:
        record["permissionDecision"] = {"decision": decision, "source": "hook"}
    if stdout if stdout is not None else not is_error:
        record["toolUseResult"] = {"stdout": text, "stderr": ""}
    else:
        record["toolUseResult"] = "Error: " + text
    return record


def ran(cid: str, command: str, **kw) -> list[dict]:
    return [call(cid, command, **kw), result(cid)]


def filler(count: int, prefix: str = "f") -> list[dict]:
    rows: list[dict] = []
    for i in range(count):
        rows += ran(f"{prefix}{i}", f"ls /srv/{prefix}{i}")
    return rows


class Base(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.root = self.tmp / "projects"
        self.out = self.tmp / "store" / "never-refuse" / "never-refuse-sample-v1.jsonl"
        env = mock.patch.dict(os.environ, {"HOME": HOME})
        env.start()
        self.addCleanup(env.stop)

    def transcript(self, records: list[dict], project: str = "proj", name: str = "s1.jsonl",
                   subagent: str | None = None) -> Path:
        folder = self.root / project
        if subagent:
            folder = folder / Path(name).stem / "subagents"
            name = subagent
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / name
        with path.open("a", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record) + "\n")
        return path

    def pool(self) -> tuple[dict, dict]:
        corpus = nrs.read_corpus(self.root, nrs._parse_ts(UNTIL))
        return nrs.build_pool(corpus, HOME, os.getuid())

    def derive(self, *extra: str, out: Path | None = None) -> tuple[int, str, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        argv = ["derive", "--root", str(self.root), "--out", str(out or self.out), "--until", UNTIL, *extra]
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = nrs.main(argv)
        return code, stdout.getvalue(), stderr.getvalue()

    def rows(self, out: Path | None = None) -> list[dict]:
        text = (out or self.out).read_text(encoding="utf-8")
        return [json.loads(line) for line in text.splitlines()]

    def manifest(self, out: Path | None = None) -> dict:
        return json.loads(nrs.manifest_path(out or self.out).read_text(encoding="utf-8"))

    def assertNothingWritten(self) -> None:
        folder = self.out.parent
        self.assertFalse(folder.exists() and any(folder.iterdir()), "derive wrote something")


def classify(command: str, cwd: str | None = "/srv/work") -> str | None:
    return nrs.classify(command, cwd, HOME)


# --------------------------------------------------------------------------
# 1. The ran-check.

class RanCheck(Base):
    def test_unseen_refusal_text_is_excluded(self):
        self.transcript(filler(2) + [call("x", "ls /srv/refused"),
                                     result("x", "Permission for this action has been denied", is_error=True)])
        pool, stats = self.pool()
        self.assertNotIn("ls /srv/refused", pool)
        self.assertEqual(stats["excluded_by_bucket"], {"error:Permission for": 1})

    def test_nonzero_exit_is_kept(self):
        self.transcript(filler(1) + [call("x", "ls /srv/missing"),
                                     result("x", "Exit code 1\nls: no such file", is_error=True)])
        pool, stats = self.pool()
        self.assertIn("ls /srv/missing", pool)
        self.assertEqual(stats["excluded_by_bucket"], {})

    def test_any_refused_result_excludes_a_repeated_id(self):
        self.transcript(filler(1) + [call("x", "ls /srv/twice"), result("x", "fine"),
                                     result("x", "Blocked: not now", is_error=True)])
        pool, stats = self.pool()
        self.assertNotIn("ls /srv/twice", pool)
        self.assertEqual(stats["multi_result_ids"], 1)
        self.assertEqual(stats["excluded_by_bucket"], {"error:Blocked: not": 1})

    def test_call_without_result_is_excluded(self):
        self.transcript(filler(1) + [call("x", "ls /srv/orphan")])
        pool, stats = self.pool()
        self.assertNotIn("ls /srv/orphan", pool)
        self.assertEqual(stats["excluded_by_bucket"], {"no_result": 1})

    def test_cancelled_is_excluded(self):
        self.transcript(filler(1) + [call("x", "ls /srv/cancel"), result("x", "fine", decision="cancelled")])
        pool, stats = self.pool()
        self.assertNotIn("ls /srv/cancel", pool)
        self.assertEqual(stats["excluded_by_bucket"], {"decision:cancelled": 1})

    def test_decision_joins_by_tool_result_id(self):
        refused = result("x", "fine", decision="reject")
        refused["tool_use_id"] = "not-the-call"
        self.transcript(filler(1) + [call("x", "ls /srv/rejected"), refused])
        pool, stats = self.pool()
        self.assertNotIn("ls /srv/rejected", pool)
        self.assertEqual(stats["excluded_by_bucket"], {"decision:reject": 1})

    def test_subagent_transcripts_are_read(self):
        self.transcript(filler(1))
        self.transcript(ran("sub1", "ls /srv/from-subagent"), subagent="agent-a1.jsonl")
        pool, stats = self.pool()
        self.assertIn("ls /srv/from-subagent", pool)
        self.assertEqual(stats["files"], 2)

    def test_exclusion_bucket_never_carries_arg_text(self):
        self.transcript(filler(1) + [call("x", "ls /srv/b"),
                                     result("x", "Blocked: '/x/" + GH_TOKEN, is_error=True)])
        _pool, stats = self.pool()
        labels = list(stats["excluded_by_bucket"])
        self.assertEqual(labels, ["error:Blocked: <arg>"])
        for label in labels:
            self.assertNotIn("/", label)
            self.assertEqual(secret_scan.findings(label), [])


# --------------------------------------------------------------------------
# 2. The per-version shape guard.

class ShapeGuard(Base):
    def test_version_with_zero_joined_results_refuses(self):
        self.transcript(filler(3) + [call("b1", "ls /srv/b", version="9.9.9")])
        code, _out, err = self.derive("--size", "1")
        self.assertEqual(code, 3)
        self.assertIn("version 9.9.9", err)
        self.assertNothingWritten()

    def test_version_without_stdout_results_refuses(self):
        self.transcript(filler(3) + [call("b1", "ls /srv/b", version="9.9.9"),
                                     result("b1", "fine", stdout=False)])
        code, _out, err = self.derive("--size", "1")
        self.assertEqual(code, 3)
        self.assertIn("version 9.9.9", err)
        self.assertNothingWritten()

    def test_unrecognized_result_shape_refuses(self):
        self.transcript(filler(3) + [call("b1", "ls /srv/b"), result("b1", content={"text": "fine"})])
        code, _out, err = self.derive("--size", "1")
        self.assertEqual(code, 3)
        self.assertIn("unrecognized shape", err)
        self.assertNothingWritten()

    def test_version_without_decisions_is_accepted(self):
        self.transcript(filler(3) + filler(2, prefix="v"))
        for record in self.root.joinpath("proj", "s1.jsonl").read_text().splitlines():
            self.assertNotIn("permissionDecision", record)
        code, _out, _err = self.derive("--size", "5")
        self.assertEqual(code, 0)
        self.assertEqual(len(self.rows()), 5)


# --------------------------------------------------------------------------
# 3-4. The parser as the classifier sees it, and the classifier holes.

class Classifier(unittest.TestCase):
    def assertNotOrdinary(self, command: str, cwd: str | None = "/srv/work", reason: str | None = None):
        with self.assertRaises(nrs.NotOrdinary) as caught:
            classify(command, cwd)
        if reason:
            self.assertEqual(str(caught.exception), reason)

    def test_output_process_substitution_is_not_ordinary(self):
        self.assertNotOrdinary("ls >(cat)", cwd="/tmp/a", reason="process-substitution-write")

    def test_loop_body_program_is_classified(self):
        self.assertEqual(classify('for f in /tmp/a /tmp/b; do rm "$f"; done'), "scratch-write")
        self.assertNotOrdinary('for f in a; do rm "$f"; done')

    def test_shell_c_body_redirect_is_seen(self):
        self.assertNotOrdinary('bash -c "cat f > ~/x"', cwd="/tmp/a", reason="redirect")
        self.assertEqual(classify('bash -c "cat f > /tmp/x"'), "scratch-write")

    def test_quoted_substitution_is_classified(self):
        self.assertNotOrdinary('echo "$(rm -rf ~/x)"', cwd="/tmp/a")
        self.assertNotOrdinary('echo "`rm -rf ~/x`"', cwd="/tmp/a")

    def test_cd_rebases_relative_operands(self):
        self.assertNotOrdinary("cd ~/repo && rm -rf x", cwd="/tmp/a")
        self.assertEqual(classify("cd /tmp/a && rm -rf y", cwd=HOME), "scratch-write")

    def test_subshell_cd_does_not_leak(self):
        self.assertNotOrdinary("(cd /tmp/a); rm -rf y", cwd=HOME)
        self.assertEqual(classify("(cd /tmp/a && rm -rf y)", cwd=HOME), "scratch-write")

    def test_piped_cd_unknown_directory(self):
        self.assertNotOrdinary("cd /tmp/a | true; rm -rf y", cwd=HOME)

    def test_dotdot_out_of_tmp_is_not_scratch(self):
        self.assertNotOrdinary("rm -rf /tmp/../home/x", cwd=HOME)

    def test_mktemp_assignment_binds_scratch(self):
        self.assertEqual(classify("d=$(mktemp -d); echo hi > $d/x", cwd=HOME), "scratch-write")
        self.assertEqual(classify('d="$(mktemp -d)"; echo hi > "$d/x"', cwd=HOME), "scratch-write")

    def test_non_mktemp_substitution_does_not_bind(self):
        self.assertNotOrdinary("x=$(cat f); echo hi > $x/y", cwd=HOME)

    def test_bound_variable_cannot_climb_out_of_tmp(self):
        self.assertNotOrdinary("d=$(mktemp -d); rm -rf $d/../../home/x", cwd=HOME)

    def test_mktemp_with_a_shared_target_is_not_ordinary(self):
        self.assertNotOrdinary("mktemp -p ~/shared", reason="mktemp")
        self.assertEqual(classify("mktemp -d"), "scratch-write")
        self.assertEqual(classify("mktemp --tmpdir=/tmp/a /tmp/a/x.XXXX"), "scratch-write")

    def test_gh_api_glued_and_equals_forms_are_not_read(self):
        for form in ("gh api -XPOST repos/o/r/issues", "gh api --method=POST repos/o/r/issues",
                     "gh api repos/o/r/issues -fx=y", "gh api repos/o/r/issues --field=a=b"):
            with self.subTest(form=form):
                self.assertNotOrdinary(form)
        self.assertEqual(classify("gh api repos/o/r/issues --jq .body"), "read")

    def test_gh_api_rule_loads_from_cross_family_brief(self):
        rule = nrs.gh_api_allowlisted
        self.assertEqual(rule.__name__, "_gh_api_allowlisted")
        self.assertEqual(Path(rule.__code__.co_filename).resolve(),
                         (REPO / "tools" / "lane" / "build_cross_family_brief.py").resolve())
        self.assertEqual(list(inspect.signature(rule).parameters), ["tokens"])

    def test_read_programs_that_write_are_excluded(self):
        for form in ("sort -o ~/f a", "sort -ro ~/f a", "uniq a ~/b", "find . -fprint ~/x",
                     "git diff --output=~/x", "awk '{print > \"f\"}' a", "awk '{system(\"rm x\")}' a",
                     "tree -o ~/x", "sed -ni s/a/b/ f"):
            with self.subTest(form=form):
                self.assertNotOrdinary(form)

    def test_dash_h_is_not_a_probe(self):
        self.assertNotOrdinary("tar -h")
        self.assertEqual(classify("tar --help"), "probe")

    def test_shared_state_command_is_excluded(self):
        for form in ("git push", "gh pr merge 1", "sed -i s/a/b/ f", "find . -delete", "echo x > ~/f",
                     "rm -rf ~/x", "cp a ~/b", "python3 x.py", "sh x.sh"):
            with self.subTest(form=form):
                self.assertNotOrdinary(form, cwd="/tmp/a")

    def test_unassigned_variable_target_is_not_scratch(self):
        self.assertNotOrdinary("echo hi > $X/y", cwd="/tmp/a")
        self.assertNotOrdinary("rm -rf ${X}", cwd="/tmp/a")

    def test_other_users_home_is_not_scratch(self):
        self.assertNotOrdinary("rm ~other/x", cwd="/tmp/a")

    def test_classes(self):
        self.assertEqual(classify("grep -n x f | head -5"), "read")
        self.assertEqual(classify("ls 2>&1 | tee /tmp/a/x"), "scratch-write")
        self.assertEqual(classify("diff <(ls a) <(ls b)"), "read")
        self.assertEqual(classify("ls > /dev/null"), "read")
        self.assertEqual(classify("git log --oneline -3"), "read")
        self.assertEqual(classify("gh pr view 3"), "read")
        self.assertEqual(classify("x=1"), None)
        self.assertEqual(classify("cat <<EOF > /tmp/x\nhello\nEOF"), "scratch-write")

    def test_depth_is_capped(self):
        nested = "ls"
        for _ in range(6):
            nested = f"bash -c {json.dumps(nested)}"
        self.assertNotOrdinary(nested, reason="depth")


class Unparseable(Base):
    def test_unparseable_is_excluded_and_counted(self):
        self.transcript(filler(1) + ran("x", 'echo "unterminated'))
        pool, stats = self.pool()
        self.assertNotIn('echo "unterminated', pool)
        self.assertEqual(stats["unparseable"], 1)


# --------------------------------------------------------------------------
# 5. The secret scan: never a stored hit.

class SecretScan(Base):
    def test_secret_shaped_command_is_never_written(self):
        self.transcript(filler(3) + ran("x", "echo " + GH_TOKEN))
        code, _out, _err = self.derive("--size", "3")
        self.assertEqual(code, 0)
        self.assertNotIn(GH_TOKEN, self.out.read_text(encoding="utf-8"))
        manifest = self.manifest()
        self.assertEqual(manifest["secret_dropped"].get("github-token"), 1)
        self.assertEqual(manifest["secret_hits_stored"], 0)

    def test_text_after_a_masked_path_is_still_scanned(self):
        opaque = "Ab1" * 12
        self.transcript(filler(1) + ran("x", f"cat /srv/work/{opaque}", cwd="/srv/work"))
        pool, stats = self.pool()
        self.assertNotIn(f"cat /srv/work/{opaque}", pool)
        self.assertEqual(stats["secret_dropped"], {"opaque-token": 1})

    def test_rescan_aborts_before_write(self):
        self.transcript(filler(2) + ran("x", "echo " + GH_TOKEN))
        with mock.patch.object(nrs, "drop_secrets", lambda distinct, home, uid: (distinct, {})):
            code, _out, err = self.derive("--size", "3")
        self.assertEqual(code, 3)
        self.assertIn("re-scan", err)
        self.assertNothingWritten()

    def test_scratchpad_path_command_survives_masking(self):
        slug = "-home-user-Harmonic-Projects-HRSE2"
        command = f"ls /tmp/claude-{os.getuid()}/{slug}/scratchpad"
        self.assertTrue(secret_scan.findings(command), "fixture must be secret-shaped unmasked")
        self.transcript(filler(1) + ran("x", command), project=slug)
        pool, stats = self.pool()
        self.assertIn(command, pool)
        self.assertEqual(stats["secret_dropped"], {})


# --------------------------------------------------------------------------
# 6. Determinism.

class Determinism(Base):
    def test_tie_break_is_ts_then_path_then_line(self):
        same = "ls /srv/shared"
        self.transcript(ran("b1", same, session="sB"), project="b-proj")
        self.transcript(ran("a1", same, session="sA"), project="a-proj")
        pool, _stats = self.pool()
        call_id, meta, _cls = pool[same]
        self.assertEqual((call_id, meta["session"]), ("a1", "sA"))

    def test_same_inputs_same_sample(self):
        records = filler(20)
        pairs = [records[i:i + 2] for i in range(0, len(records), 2)]
        self.transcript([r for pair in pairs for r in pair], project="one")
        code1, _o, _e = self.derive("--size", "5")
        # The same calls, written in reverse line order: the same pool.
        self.root = self.tmp / "projects2"
        self.transcript([r for pair in reversed(pairs) for r in pair], project="one")
        second = self.tmp / "second" / "never-refuse-sample-v1.jsonl"
        code2, _o, _e = self.derive("--size", "5", out=second)
        self.assertEqual((code1, code2), (0, 0))
        self.assertEqual(self.out.read_bytes(), second.read_bytes())
        self.assertEqual(self.manifest()["sha256"], self.manifest(second)["sha256"])
        self.assertEqual(self.manifest()["sha256"], hashlib.sha256(self.out.read_bytes()).hexdigest())


# --------------------------------------------------------------------------
# 7. The frozen sample and crash order.

class Frozen(Base):
    def test_crash_before_manifest_leaves_no_sample(self):
        self.transcript(filler(3))
        real = nrs._write_temp

        def failing(folder, data):
            if b'"secret_hits_stored"' in data:
                raise OSError("injected manifest write failure")
            return real(folder, data)

        with mock.patch.object(nrs, "_write_temp", failing):
            code, _out, _err = self.derive("--size", "2")
        self.assertEqual(code, 3)
        self.assertFalse(self.out.exists())
        self.assertFalse(nrs.manifest_path(self.out).exists())

    def test_orphan_sample_is_named_on_rerun(self):
        self.transcript(filler(3))
        self.out.parent.mkdir(parents=True)
        self.out.write_text("{}\n", encoding="utf-8")
        code, _out, err = self.derive("--size", "2")
        self.assertEqual(code, 2)
        self.assertIn("orphan sample", err)
        self.assertEqual(self.out.read_text(encoding="utf-8"), "{}\n")

        memory = self.tmp / "store" / "protocol-failures.jsonl"
        memory.write_text("", encoding="utf-8")
        stderr = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(stderr):
            code = rh.main(["score", "--layers", str(HERE / "layersets" / "design-v1.json"),
                            "--sample", str(self.out), "--db", str(self.tmp / "store" / "fs.sqlite"),
                            "--memory-log", str(memory), "--testplan-log", str(self.tmp / "none.jsonl"),
                            "--overlays", str(self.tmp / "store" / "overlays")])
        self.assertEqual(code, 2)
        self.assertIn("orphan sample", stderr.getvalue())

    def test_orphan_manifest_is_named(self):
        self.transcript(filler(3))
        self.out.parent.mkdir(parents=True)
        nrs.manifest_path(self.out).write_text("{}", encoding="utf-8")
        code, _out, err = self.derive("--size", "2")
        self.assertEqual(code, 2)
        self.assertIn("orphan manifest", err)

    def test_leftover_temp_refuses(self):
        self.transcript(filler(3))
        self.out.parent.mkdir(parents=True)
        (self.out.parent / "never-refuse-sampleab12cd.tmp").write_text("", encoding="utf-8")
        code, _out, err = self.derive("--size", "2")
        self.assertEqual(code, 2)
        self.assertIn("leftover temp", err)
        self.assertFalse(self.out.exists())

    def test_temp_names_match_gitignore(self):
        self.transcript(filler(3))
        patterns = [line.strip() for line in (REPO / ".gitignore").read_text().splitlines()
                    if line.strip() and not line.startswith("#")]
        made: list[Path] = []
        real = nrs._write_temp

        def recording(folder, data):
            made.append(real(folder, data))
            return made[-1]

        with mock.patch.object(nrs, "_write_temp", recording):
            code, _out, _err = self.derive("--size", "2")
        self.assertEqual(code, 0)
        self.assertEqual(len(made), 2)
        for path in made:
            self.assertTrue(any(fnmatch.fnmatch(path.name, p) for p in patterns), path.name)
        self.assertEqual(oct(self.out.stat().st_mode & 0o777), "0o600")

    def test_default_size_is_10000(self):
        args = nrs.build_parser().parse_args(["derive"])
        self.assertEqual((args.size, args.seed), (10000, 1279))

    def test_existing_sample_is_never_overwritten(self):
        self.transcript(filler(3))
        self.assertEqual(self.derive("--size", "2")[0], 0)
        before = self.out.read_bytes()
        code, _out, err = self.derive("--size", "3")
        self.assertEqual(code, 2)
        self.assertIn("already derived", err)
        self.assertEqual(self.out.read_bytes(), before)

    def test_pool_smaller_than_size_writes_nothing(self):
        self.transcript(filler(3))
        code, _out, err = self.derive("--size", "4")
        self.assertEqual(code, 3)
        self.assertIn("fewer than --size", err)
        self.assertNothingWritten()

    def test_wrong_name_refuses(self):
        self.transcript(filler(3))
        code, _out, _err = self.derive("--size", "2", out=self.tmp / "sample.jsonl")
        self.assertEqual(code, 2)

    def test_manifest_counts_and_rows(self):
        self.transcript(filler(3) + ran("w", "echo hi > /tmp/a/x") + [call("r", "ls /srv/r"),
                                                                    result("r", "fine", decision="reject")])
        code, out, _err = self.derive("--size", "4")
        self.assertEqual(code, 0)
        manifest = self.manifest()
        self.assertEqual((manifest["bash_calls"], manifest["ran"], manifest["distinct"]), (5, 4, 4))
        self.assertEqual(manifest["excluded_by_bucket"], {"decision:reject": 1})
        self.assertEqual(manifest["pool_by_class"], {"read": 3, "scratch-write": 1})
        self.assertEqual(manifest["sample_by_class"], {"read": 3, "scratch-write": 1})
        self.assertEqual(manifest["versions"], {"2.0.0": {"calls": 5, "joined": 5, "ok_with_stdout": 5}})
        rows = self.rows()
        self.assertEqual([r["n"] for r in rows], [1, 2, 3, 4])
        self.assertEqual(set(rows[0]), {"n", "id", "ts", "session", "sidechain", "version", "cwd", "class",
                                        "command"})
        self.assertNotIn("ls /srv/f0", out)


# --------------------------------------------------------------------------
# 13. No data in forge.

class NoDataInForge(Base):
    def test_out_inside_a_forge_checkout_refuses(self):
        forge = self.tmp / "forge"
        (forge / ".git").mkdir(parents=True)
        (forge / "tools" / "memory").mkdir(parents=True)
        (forge / "tools" / "memory" / "capture_failure.py").write_text("", encoding="utf-8")
        self.transcript(filler(3))
        out = forge / "data" / "never-refuse-sample-v1.jsonl"
        code, _out, err = self.derive("--size", "2", out=out)
        self.assertEqual(code, 2)
        self.assertIn("inside a harmonic-forge checkout", err)
        self.assertFalse((forge / "data").exists())

    def test_gitignore_covers_the_sample_name(self):
        patterns = [line.strip() for line in (REPO / ".gitignore").read_text().splitlines()
                    if line.strip() and not line.startswith("#")]
        for name in ("never-refuse-sample-v1.jsonl", "never-refuse-sample-v1.jsonl.manifest.json",
                     "never-refuse-samplek3j2_x.tmp"):
            with self.subTest(name=name):
                self.assertTrue(any(fnmatch.fnmatch(name, p) for p in patterns))

    def test_suite_reads_no_real_data(self):
        banned = ("." + "claude/projects", "." + "claude\" / \"projects", "operator" + "-memory",
                  "DEFAULT_" + "ROOT", "DEFAULT_" + "OUT", "DEFAULT_" + "DB", "DEFAULT_" + "LOGS",
                  "DEFAULT_" + "OVERLAYS", "MEMORY_" + "STORE")
        for path in sorted(HERE.glob("test_*.py")):
            text = path.read_text(encoding="utf-8")
            for word in banned:
                with self.subTest(file=path.name, word=word):
                    self.assertNotIn(word, text)


if __name__ == "__main__":
    unittest.main()
