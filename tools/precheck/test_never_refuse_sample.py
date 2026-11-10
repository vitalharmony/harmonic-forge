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

    def derive(self, *extra: str, out: Path | None = None, size: int = nrs.SIZE) -> tuple[int, str, str]:
        """`size` patches the pinned sample size: there is no `--size` option."""
        stdout, stderr = io.StringIO(), io.StringIO()
        argv = ["derive", "--root", str(self.root), "--out", str(out or self.out), "--until", UNTIL, *extra]
        with redirect_stdout(stdout), redirect_stderr(stderr), mock.patch.object(nrs, "SIZE", size):
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


def classify(command: str) -> str | None:
    return nrs.classify(command)


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
        self.assertEqual(stats["excluded_by_bucket"], {"error:Blocked:": 1})

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
        self.assertEqual(labels, ["error:Blocked:"])
        for label in labels:
            self.assertNotIn("/", label)
            self.assertEqual(secret_scan.findings(label), [])


# --------------------------------------------------------------------------
# 2. The per-version shape guard.

class ShapeGuard(Base):
    def test_unrecognized_result_shape_refuses(self):
        self.transcript(filler(3) + [call("b1", "ls /srv/b"), result("b1", content={"text": "fine"})])
        code, _out, err = self.derive(size=1)
        self.assertEqual(code, 3)
        self.assertIn("unrecognized shape", err)
        self.assertNothingWritten()

    def test_version_without_decisions_is_accepted(self):
        self.transcript(filler(3) + filler(2, prefix="v"))
        for record in self.root.joinpath("proj", "s1.jsonl").read_text().splitlines():
            self.assertNotIn("permissionDecision", record)
        code, _out, _err = self.derive(size=5)
        self.assertEqual(code, 0)
        self.assertEqual(len(self.rows()), 5)

    def test_s18_in_flight_call_on_a_new_version_is_reported_not_refused(self):
        # Survivor 18: a still-running call on a just-updated version has no
        # joined result. It fails safe through the ran-check (no_result), so
        # derive writes, and the manifest reports no_result per version.
        self.transcript(filler(3) + [call("b1", "ls /srv/b", version="9.9.9")])
        code, _out, err = self.derive(size=3)
        self.assertEqual(code, 0, err)
        manifest = self.manifest()
        self.assertEqual(manifest["versions"]["9.9.9"],
                         {"calls": 1, "joined": 0, "no_result": 1, "ok_with_stdout": 0})
        self.assertEqual(manifest["excluded_by_bucket"], {"no_result": 1})
        self.assertNotIn("ls /srv/b", self.out.read_text(encoding="utf-8"))

    def test_version_without_stdout_results_is_reported_not_refused(self):
        # The stdout-dict clause tripped the same way on a fresh version.
        self.transcript(filler(3) + [call("b1", "ls /srv/b", version="9.9.9"),
                                     result("b1", "fine", stdout=False)])
        code, _out, err = self.derive(size=4)
        self.assertEqual(code, 0, err)
        self.assertEqual(self.manifest()["versions"]["9.9.9"]["ok_with_stdout"], 0)


# --------------------------------------------------------------------------
# 3-4. The recognizer (harmonic-forge#1289 epoch-2 sticky-wicket REFORGE).

class Recognizer(unittest.TestCase):
    """Each admitted simple command matches a literal row; everything else
    raises. The survivor tests below each name the epoch-2 pass-1 finding
    they pin, and each fails if the rule it tests is removed (the kill
    checks are recorded on the issue)."""

    def assertNotOrdinary(self, command: str, reason: str | None = None):
        with self.assertRaises(nrs.NotOrdinary) as caught:
            classify(command)
        if reason:
            self.assertEqual(str(caught.exception), reason)

    # -- The 12 holes ------------------------------------------------------

    def test_s01_quoted_operator_is_refused_not_lexed(self):
        self.assertNotOrdinary("rm -rf /tmp/a '<' /home/u/repo", reason="quoted-operator")
        for quoted in ("echo 'a;b'", 'echo "a|b"', "echo 'a\nb'", "grep '#' f"):
            with self.subTest(command=quoted):
                self.assertNotOrdinary(quoted, reason="quoted-operator")

    def test_s02_comment_is_refused(self):
        self.assertNotOrdinary("ls /tmp # don't\nrm -rf /home/u/proj", reason="grammar")
        self.assertNotOrdinary("ls # x", reason="grammar")

    def test_s03_env_is_a_refused_prefix(self):
        for command in ("env --chdir=/home/u/repo rm -rf build", "env -C /home/u rm -rf build",
                        "env -S 'rm -rf x'", "env ls"):
            with self.subTest(command=command):
                self.assertNotOrdinary(command, reason="prefix")

    def test_s04_prefix_assignment_is_refused(self):
        for command in ("TMPDIR=/home/u mktemp -d", "GIT_EXTERNAL_DIFF=/tmp/x.sh git diff",
                        "LD_PRELOAD=/tmp/x.so ls", "X=1"):
            with self.subTest(command=command):
                self.assertNotOrdinary(command, reason="prefix")
        for wrapper in ("sudo", "nice", "timeout", "command", "nohup", "time", "stdbuf", "xargs"):
            with self.subTest(wrapper=wrapper):
                self.assertNotOrdinary(f"{wrapper} ls", reason="prefix")

    def test_s05_brace_write_target_is_not_scratch(self):
        self.assertNotOrdinary("rm -rf /tmp/{a,../home/u/repo}", reason="grammar")
        for target in ("/tmp/*", "/tmp/a?", "/tmp/../home/x", "/tmp/a/../../home", "/tmp/./x",
                       "/tmp/a b", "~/x", "/tmp"):
            with self.subTest(target=target):
                self.assertFalse(nrs.is_scratch(target))

    def test_s06_assigned_value_never_binds(self):
        self.assertNotOrdinary('X="/tmp/a /home/u"; rm -rf $X', reason="grammar")
        self.assertNotOrdinary("for X in /tmp/a /home/u; do rm -rf $X; done", reason="grammar")
        self.assertNotOrdinary("for X in /tmp/a /tmp/b; do rm -rf $X; done", reason="grammar")

    def test_s07_relative_write_target_is_refused_whatever_cd_did(self):
        for command in ("cd /tmp/missing; rm -rf build", "cd /tmp/x || rm -rf build",
                        "cd /tmp/x && rm -rf build", "rm -rf build"):
            with self.subTest(command=command):
                self.assertNotOrdinary(command, reason="write-target")
        self.assertEqual(classify("cd /tmp/x && rm -rf /tmp/x/build"), "scratch-write")

    def test_s08_no_substitution_is_ever_admitted(self):
        # The binding row is gone (epoch-4 REFORGE): a `$` outside single quotes refuses.
        for command in ("(D=$(mktemp -d)); rm -rf $D/x", "D=$(mktemp -d); rm -rf $D/x",
                        "A=$(mktemp)\nB=$(mktemp -d)\ncp $A ${B}/y", "echo $(ls)"):
            with self.subTest(command=command):
                self.assertNotOrdinary(command, reason="grammar")

    def test_s10_probe_only_on_allowlisted_programs(self):
        for command in ("python3 -c pass --help", "awk 1 --help", "./x.sh --help", "tar --help",
                        "/bin/ls --help"):
            with self.subTest(command=command):
                self.assertNotOrdinary(command, reason="unclassified")
        for command in ("ls --help", "rm --help", "git --help", "gh --help"):
            with self.subTest(command=command):
                self.assertEqual(classify(command), "probe")

    def test_s11_mktemp_options_go_through_its_row(self):
        for command in ("mktemp -p/home/u", "mktemp -dp/home/u", "mktemp --tmp=/home/u",
                        "mktemp --tmpdir=/tmp/a", "mktemp -p /tmp/a", "mktemp -t x.XXXX"):
            with self.subTest(command=command):
                self.assertNotOrdinary(command, reason="option")
        self.assertNotOrdinary("mktemp /home/u/x.XXXX", reason="mktemp")
        self.assertEqual(classify("mktemp -d"), "scratch-write")
        self.assertEqual(classify("mktemp /tmp/x.XXXX"), "scratch-write")

    def test_s12_ln_needs_two_operands(self):
        self.assertNotOrdinary("ln -s /tmp/foo", reason="unclassified")
        self.assertEqual(classify("ln -s /tmp/foo /tmp/bar"), "scratch-write")

    # -- The 3 rules no test pinned ----------------------------------------

    def test_s13_probe_is_exactly_prog_help(self):
        for command in ("rm -- /home/u/x --help", "rm /home/u/x --help", "rm -rf --help",
                        "git commit -m --help"):
            with self.subTest(command=command), self.assertRaises(nrs.NotOrdinary):
                classify(command)

    def test_s14_cd_dash_never_rebases_a_write(self):
        self.assertNotOrdinary("cd /tmp/a; cd -; rm -rf y", reason="write-target")

    def test_s15_substitution_in_a_redirect_target_is_refused(self):
        self.assertNotOrdinary('cat < "$(rm -rf ~/x)"', reason="grammar")
        self.assertNotOrdinary("cat < $(rm -rf ~/x)", reason="grammar")
        self.assertNotOrdinary("cat < `rm -rf ~/x`", reason="grammar")

    # -- The recognizer's other rows ---------------------------------------

    def test_heredoc_rows(self):
        self.assertEqual(classify("cat <<'EOF' > /tmp/x\nhi $(x) don't\nEOF"), "scratch-write")
        self.assertEqual(classify("cat <<EOF > /tmp/x\nhello\nEOF"), "scratch-write")
        # An unquoted body runs its substitutions.
        self.assertNotOrdinary("cat <<EOF > /tmp/x\n$(rm -rf ~)\nEOF", reason="heredoc")
        # `<<<WORD` is a here-string: the lines after it run.
        self.assertNotOrdinary("cat <<< x\nrm -rf ~\nx", reason="heredoc")

    # -- Epoch-3 heredoc PATCH: an unquoted body must be literal text --------
    # Each seed fails if its character leaves _HEREDOC_BODY_REFUSED.

    def test_e3_unquoted_heredoc_body_with_backtick_is_refused(self):
        self.assertNotOrdinary("cat <<EOF > /tmp/notes.md\nRun `rm -rf ~/proj` first\nEOF", reason="heredoc")
        self.assertEqual(classify("cat <<'EOF' > /tmp/notes.md\nRun `x` first\nEOF"), "scratch-write")

    def test_e3_unquoted_heredoc_body_with_parameter_is_refused(self):
        self.assertNotOrdinary("cat <<EOF > /tmp/x\nhome is $HOME\nEOF", reason="heredoc")
        self.assertNotOrdinary("cat <<EOF > /tmp/x\n${X:=y}\nEOF", reason="heredoc")
        self.assertNotOrdinary("cat <<-EOF > /tmp/x\n\t$((1+1))\n\tEOF", reason="heredoc")

    def test_e3_unquoted_heredoc_body_with_backslash_is_refused(self):
        self.assertNotOrdinary("cat <<EOF > /tmp/x\nline one \\\nEOF\nmore\nEOF", reason="heredoc")
        self.assertNotOrdinary("cat <<EOF > /tmp/x\na\\tb\nEOF", reason="heredoc")

    def test_e3_unquoted_heredoc_literal_body_is_admitted(self):
        self.assertEqual(classify("cat <<EOF > /tmp/x\nplain prose, no specials.\nEOF"), "scratch-write")
        self.assertEqual(classify('cat <<"EOF" > /tmp/x\n$HOME `x` \\\nEOF'), "scratch-write")

    def test_backslash_blank_is_refused(self):
        self.assertNotOrdinary("rm /tmp/a\\ b", reason="grammar")

    # -- Epoch-4 survivors ---------------------------------------------------

    def test_e4_quoted_or_escaped_dollar_never_reads_as_scratch(self):
        # bash removes a file literally named `$X`; inside single quotes the
        # grammar admits the character, so the write-target row must refuse it.
        self.assertNotOrdinary("rm -rf '$X'", reason="write-target")
        self.assertNotOrdinary("echo hi > '$X/y'", reason="redirect")
        for command in ("rm -rf \\$X", 'rm -rf "\\$X"', 'rm -rf "$X"'):
            with self.subTest(command=command):
                self.assertNotOrdinary(command, reason="grammar")

    # -- The epoch-4 REFORGE ------------------------------------------------

    def test_raw_grammar_refuses_each_character_outside_single_quotes_only(self):
        for char in "\\`$#{}":
            for command in (f"echo a{char}b", f'echo "a{char}b"'):
                with self.subTest(command=command):
                    self.assertNotOrdinary(command, reason="grammar")
        for command in ("grep 'a\\b' f", "cat 'a$b'", "echo 'a{b}'"):
            with self.subTest(command=command):
                self.assertEqual(classify(command), "read")

    def test_raw_grammar_has_no_branch_on_a_bash_feature(self):
        # The convergence test: a character class plus the quote state machine.
        self.assertFalse(hasattr(nrs, "lexer_hazards"))
        source = inspect.getsource(nrs.raw_grammar)
        for feature in ('"$("', "comment", "backtick"):
            self.assertNotIn(feature, source)

    def test_a_plain_newline_is_not_refused(self):
        self.assertEqual(classify("ls /srv/a\nls /srv/b"), "read")

    def test_the_binding_row_is_gone(self):
        for name in ("bindings", "_BINDING", "_BOUND_USE"):
            self.assertFalse(hasattr(nrs, name))
        self.assertEqual(list(inspect.signature(nrs.row).parameters), ["argv"])
        self.assertEqual(list(inspect.signature(nrs.is_scratch).parameters), ["word"])

    def test_a_loop_whose_body_sleeps_is_a_poll(self):
        for command in ("while true; do sleep 5; done", "until ls /srv/x; do sleep 1; done",
                        "for i in 1 2 3; do ls /srv/x; sleep 1; done", "ls /srv; while true; do sleep 5; done",
                        "if true; then while true; do sleep 5; done; fi"):
            with self.subTest(command=command):
                self.assertNotOrdinary(command, reason="poll-loop")

    def test_sleep_outside_a_loop_and_a_loop_without_sleep_stay_ordinary(self):
        self.assertEqual(classify("sleep 5"), "read")
        self.assertEqual(classify("ls /srv/a; sleep 1; ls /srv/a"), "read")
        self.assertEqual(classify("for f in a b; do cat /srv/x; done"), "read")

    # -- Pinned guards (epoch-4 pass 2, #5-#8) ------------------------------

    def test_a_gh_read_option_outside_the_row_is_refused(self):
        self.assertNotOrdinary("gh pr view 1 --web", reason="option")

    def test_e4_sed_e_and_w_without_a_space_are_refused(self):
        # Every earlier w/e seed carried a space, so the `$` anchor refused it
        # whether or not SED_SAFE admitted the letter.
        for command in ("sed s/a/b/e f", "sed e f", "sed -n 1e f", "sed s/a/b/wp f",
                        "sed -n 1w f", "sed w f"):
            with self.subTest(command=command):
                self.assertNotOrdinary(command, reason="option")

    def test_shell_and_wrapper_are_never_unwrapped(self):
        self.assertNotOrdinary('bash -c "ls"', reason="unclassified")
        self.assertNotOrdinary("sh -c 'rm -rf /tmp/x'", reason="unclassified")
        self.assertNotOrdinary('sh -c "cat f > /tmp/x"', reason="quoted-operator")

    def test_output_process_substitution_is_not_ordinary(self):
        self.assertNotOrdinary("ls >(cat)", reason="process-substitution-write")

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

    def test_shared_state_command_is_excluded(self):
        for form in ("git push", "gh pr merge 1", "sed -i s/a/b/ f", "find . -delete", "echo x > ~/f",
                     "rm -rf ~/x", "cp a ~/b", "python3 x.py", "sh x.sh"):
            with self.subTest(form=form):
                self.assertNotOrdinary(form)

    def test_unbound_variable_target_is_not_scratch(self):
        self.assertNotOrdinary("echo hi > $X/y", reason="grammar")
        self.assertNotOrdinary("rm -rf ${X}", reason="grammar")
        self.assertNotOrdinary("rm ~other/x", reason="write-target")

    def test_classes(self):
        self.assertEqual(classify("grep -n x f | head -5"), "read")
        self.assertEqual(classify("ls 2>&1 | tee /tmp/a/x"), "scratch-write")
        self.assertEqual(classify("diff <(ls a) <(ls b)"), "read")
        self.assertEqual(classify("ls > /dev/null"), "read")
        self.assertEqual(classify("git log --oneline -3"), "read")
        self.assertEqual(classify("gh pr view 3"), "read")
        self.assertEqual(classify("for f in a b; do cat /srv/x; done"), "read")

    def test_no_function_computes_a_shell_effect(self):
        # The convergence test, by construction: the interpreter helpers are gone.
        for name in ("resolve_path", "strip_invocation_prefix", "_mktemp_scratch", "_is_probe",
                     "substitution_bodies", "MAX_DEPTH"):
            with self.subTest(name=name):
                self.assertFalse(hasattr(nrs, name))
        self.assertEqual(list(inspect.signature(nrs.classify).parameters), ["command"])


class FailClosed(Base):
    """harmonic-forge#1289 sticky-wicket REFORGE: every write form the pass-1
    panel and sticky-wicket found is excluded because it is not on an
    allowlist, never because it is on a list of write forms."""

    SEEDS = [
        "ls /tmp # list\nrm -rf /home/u/repo", "git status # x\ngit push origin main",
        "echo ${#X}\nrm -rf ~/repo", "echo a#b; rm -rf /home/u/repo",
        "cp --target-directory=/home/u/repo /tmp/a", "mv -t/home/u/proj /tmp/a",
        'sed -n "w /home/u/out" /tmp/f', 'sed -n "1w /home/u/out" f', 'sed "s/a/b/w /home/u/o" f',
        'sed "1e rm x" f', 'awk -i inplace "{print}" /home/u/f',
        'D="$(mktemp -d)/../../home/u"; rm -rf $D', 'D=/tmp/x rm -rf "$D/"',
        "cd /tmp; D=x; cd ~; rm -rf $D", "mv /tmp/a 2 > /dev/null",
        "awk -f /tmp/p.awk f", "sed -f /tmp/s.sed f", "rg --pre /tmp/x pat .", "date -s 2020-01-01",
        "git grep -O pat", "tree --output /home/u/x", "date 010100002020",
        "python3 tools/gh/preclose_check.py --plan", "git commit -m --dry-run",
        "rm /home/u/x -- --help", "git commit -m --help", "D=/tmp/x; rm -rf $D",
    ]
    ORDINARY = [
        ("ls -la /srv", "read"), ("grep -rn foo .", "read"), ("git log --oneline -5", "read"),
        ("sed -n 1,20p f", "read"), ('sed "s/a/b/g" f', "read"), ("cat a | head -20", "read"),
        ("rm -rf /tmp/x", "scratch-write"), ('find . -name "*.py" -type f', "read"),
        ("date +%F", "read"), ("gh pr view 12 --json title", "read"), ("jq -r .x f", "read"),
        ("ls /tmp 2>/dev/null", "read"), ("mktemp -d", "scratch-write"),
    ]

    def test_every_seed_is_excluded(self):
        for command in self.SEEDS:
            with self.subTest(command=command), self.assertRaises(nrs.NotOrdinary):
                classify(command)

    def test_allowlisted_forms_stay_ordinary(self):
        for command, cls in self.ORDINARY:
            with self.subTest(command=command):
                self.assertEqual(classify(command), cls)

    def test_unlisted_option_on_a_read_program_is_excluded(self):
        # The rule, not an instance: options nobody listed.
        for command in ("ls -Q /srv", "cat --anything /srv/f", "grep --colour=always x f",
                        "grep --color=always x f"):
            with self.subTest(command=command), self.assertRaises(nrs.NotOrdinary) as caught:
                classify(command)
            self.assertEqual(str(caught.exception), "option")


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
        code, _out, _err = self.derive(size=3)
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
            code, _out, err = self.derive(size=3)
        self.assertEqual(code, 3)
        self.assertIn("re-scan", err)
        self.assertNothingWritten()

    def test_rescan_covers_the_whole_line_not_chosen_fields(self):
        rows = [{"n": 1, "command": "ls /srv", "cwd": "/srv", "session": GH_TOKEN, "_slug": "p"}]
        hits = nrs.rescan(rows, {"versions": {}}, HOME, os.getuid())
        self.assertEqual(hits, 1)

    def test_a_cwd_with_a_secret_is_stored_as_null(self):
        meta = {"cwd": "/srv/" + GH_TOKEN, "slug": "p"}
        self.assertIsNone(nrs.stored_cwd(meta, HOME, os.getuid()))
        self.assertEqual(nrs.stored_cwd({"cwd": HOME + "/repo", "slug": "p"}, HOME, os.getuid()), HOME + "/repo")

    def test_a_cwd_is_never_its_own_mask(self):
        self.assertEqual(set(nrs.masks_for({"cwd": "/srv/x", "slug": "p"}, HOME, 1000)),
                         {HOME, "/tmp/claude-1000/p"})

    def test_a_root_mask_is_never_used(self):
        # Epoch-4 #6: a "/" mask would split every base64 run below the token length.
        self.assertNotIn("/", nrs.masks_for({"slug": "p"}, "/", 1000))

    def test_every_bucket_is_from_the_fixed_vocabulary(self):
        for text, want in (("Blocked: not now", "Blocked:"), ("Blocked: raw GitHub", "Blocked: raw"),
                           ("Blocked: " + GH_TOKEN, "Blocked:"), ("anything else", "other"),
                           (GH_TOKEN, "other")):
            with self.subTest(text=text):
                self.assertEqual(nrs.bucket(text), want)

    def test_a_string_is_error_value_fails_the_shape_guard(self):
        # Epoch-4 #5: a string "true" must not pass as a recognized result shape.
        self.assertFalse(nrs._shape_ok({"content": "x", "is_error": "true"}))
        self.assertTrue(nrs._shape_ok({"content": "x", "is_error": True}))

    def test_a_record_after_until_is_not_read(self):
        # Epoch-4 #7: the manifest records --until, so a later call must not enter.
        self.transcript(filler(1) + ran("late", "ls /srv/late", ts="2026-12-05T00:00:00Z"))
        pool, _stats = self.pool()
        self.assertNotIn("ls /srv/late", pool)

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

    def test_earlier_ts_wins_over_earlier_path(self):
        # The earlier call sits in the later-sorted file: ts must decide, not path.
        same = "ls /srv/shared"
        self.transcript(ran("b1", same, ts="2026-11-01T00:00:00Z", session="sB"), project="b-proj")
        self.transcript(ran("a1", same, ts="2026-11-01T00:00:05Z", session="sA"), project="a-proj")
        pool, _stats = self.pool()
        call_id, meta, _cls = pool[same]
        self.assertEqual((call_id, meta["session"]), ("b1", "sB"))

    def test_same_ts_same_file_earlier_line_wins(self):
        same = "ls /srv/shared"
        self.transcript(ran("l2", "ls /srv/other") + ran("l1", same) + ran("l3", same))
        pool, _stats = self.pool()
        self.assertEqual(pool[same][0], "l1")

    def test_same_inputs_same_sample(self):
        records = filler(20)
        pairs = [records[i:i + 2] for i in range(0, len(records), 2)]
        self.transcript([r for pair in pairs for r in pair], project="one")
        code1, _o, _e = self.derive(size=5)
        # The same calls, written in reverse line order: the same pool.
        self.root = self.tmp / "projects2"
        self.transcript([r for pair in reversed(pairs) for r in pair], project="one")
        second = self.tmp / "second" / "never-refuse-sample-v1.jsonl"
        code2, _o, _e = self.derive(size=5, out=second)
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
            code, _out, _err = self.derive(size=2)
        self.assertEqual(code, 3)
        self.assertFalse(self.out.exists())
        self.assertFalse(nrs.manifest_path(self.out).exists())
        left = sorted(p.name for p in self.out.parent.iterdir()) if self.out.parent.exists() else []
        self.assertEqual(left, [], "a failed derive leaves no temp or sample file behind")

    def test_concurrent_derive_never_overwrites_a_frozen_sample(self):
        # Another derive publishes after this one's check and before its publish.
        self.transcript(filler(3))
        real = nrs._write_temp
        frozen = b'{"other":"run"}\n'

        def racing(folder, data):
            path = real(folder, data)
            if not self.out.exists() and b'"secret_hits_stored"' not in data:
                self.out.write_bytes(frozen)
            return path

        with mock.patch.object(nrs, "_write_temp", racing):
            code, _out, err = self.derive(size=2)
        self.assertEqual(code, 2, err)
        self.assertIn("appeared while this derive ran", err)
        self.assertEqual(self.out.read_bytes(), frozen)
        self.assertFalse(nrs.manifest_path(self.out).exists())
        self.assertEqual(sorted(p.name for p in self.out.parent.iterdir()), [self.out.name])

    def test_orphan_sample_is_named_on_rerun(self):
        self.transcript(filler(3))
        self.out.parent.mkdir(parents=True)
        self.out.write_text("{}\n", encoding="utf-8")
        code, _out, err = self.derive(size=2)
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
        code, _out, err = self.derive(size=2)
        self.assertEqual(code, 2)
        self.assertIn("orphan manifest", err)

    def test_leftover_temp_refuses(self):
        self.transcript(filler(3))
        self.out.parent.mkdir(parents=True)
        (self.out.parent / "never-refuse-sampleab12cd.tmp").write_text("", encoding="utf-8")
        code, _out, err = self.derive(size=2)
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
            code, _out, _err = self.derive(size=2)
        self.assertEqual(code, 0)
        self.assertEqual(len(made), 2)
        for path in made:
            self.assertTrue(any(fnmatch.fnmatch(path.name, p) for p in patterns), path.name)
        self.assertEqual(oct(self.out.stat().st_mode & 0o777), "0o600")

    def test_s17_size_is_pinned_at_10000_with_no_option(self):
        # Survivor 17: `derive --size 0` wrote an empty sample whose bars held
        # at 0/0. The option is deleted; the size is a constant.
        self.assertEqual(nrs.SIZE, 10000)
        self.assertEqual(nrs.build_parser().parse_args(["derive"]).seed, 1279)
        self.transcript(filler(3))
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
            nrs.main(["derive", "--root", str(self.root), "--out", str(self.out), "--size", "0"])
        self.assertEqual(caught.exception.code, 2)
        self.assertNothingWritten()

    def test_existing_sample_is_never_overwritten(self):
        self.transcript(filler(3))
        self.assertEqual(self.derive(size=2)[0], 0)
        before = self.out.read_bytes()
        code, _out, err = self.derive(size=3)
        self.assertEqual(code, 2)
        # Refused up front, before the corpus read; not by the publish backstop.
        self.assertIn("and its manifest exist", err)
        self.assertEqual(self.out.read_bytes(), before)

    def test_pool_smaller_than_size_writes_nothing(self):
        self.transcript(filler(3))
        code, _out, err = self.derive(size=4)
        self.assertEqual(code, 3)
        self.assertIn("fewer than the sample size 4", err)
        self.assertNothingWritten()

    def test_wrong_name_refuses(self):
        self.transcript(filler(3))
        code, _out, _err = self.derive(size=2, out=self.tmp / "sample.jsonl")
        self.assertEqual(code, 2)

    def test_manifest_counts_and_rows(self):
        self.transcript(filler(3) + ran("w", "echo hi > /tmp/a/x") + [call("r", "ls /srv/r"),
                                                                    result("r", "fine", decision="reject")])
        code, out, _err = self.derive(size=4)
        self.assertEqual(code, 0)
        manifest = self.manifest()
        self.assertEqual((manifest["bash_calls"], manifest["ran"], manifest["distinct"]), (5, 4, 4))
        self.assertEqual(manifest["excluded_by_bucket"], {"decision:reject": 1})
        self.assertEqual(manifest["pool_by_class"], {"read": 3, "scratch-write": 1})
        self.assertEqual(manifest["sample_by_class"], {"read": 3, "scratch-write": 1})
        self.assertEqual(manifest["versions"], {"2.0.0": {"calls": 5, "joined": 5, "no_result": 0, "ok_with_stdout": 5}})
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
        code, _out, err = self.derive(size=2, out=out)
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
