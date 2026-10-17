#!/usr/bin/env python3
"""Tests for `hrse-gate grant-from-ae` (harmonic-forge#1064).

GitHub, the TTY, the euid and the token file are all stubbed in-process; the
store lives under a temp root. Nothing here reaches the network, `/etc`,
`/var/lib/hrse-gate` or a real terminal.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "gh"))

import hrse_gate_ae as ae  # noqa: E402
import hrse_gate_broker as b  # noqa: E402

REAL_CONFIRM = ae.confirm
REAL_GITHUB = ae.github
REAL_ISATTY = ae.isatty

SHA = "a" * 40
OLD = "c" * 40
SCRIPT = "scripts/1-1892-revive.py"


def ae_body(sha=SHA, script=SCRIPT, apply=False, issue=1892, kind="ae", label=None):
    field = (f"prod-run=issue:{issue},sha:{sha},count-label:{label}" if label else
             f"prod-run=issue:{issue},sha:{sha},script:{script},apply:{'true' if apply else 'false'}")
    return (f"## AE\n**Authorized:** operator\n\n<!-- l1-post v1; kind={kind}; sha={sha}; "
            f"{field}; body-sha256={'0' * 64}; checks=x -->\n")


class _Case(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        (root / "grants").mkdir()
        (root / "receipts").mkdir()
        token = root / "github-token"
        token.write_text("ghp_test\n")
        os.chmod(token, 0o600)
        self.comments = [ae_body()]
        self.pulls = [{"number": 7, "state": "open", "head": {"sha": SHA}}]
        self.main_head = OLD
        self.answer = True
        self.confirm_text = []
        me = os.getuid(), os.getgid()
        real_fstat = os.fstat

        def fstat_as_root(fd):  # the test user stands in for root on the token file
            st = real_fstat(fd)
            return os.stat_result((st.st_mode, st.st_ino, st.st_dev, st.st_nlink, 0,
                                   st.st_gid, st.st_size, st.st_atime, st.st_mtime, st.st_ctime))

        self._patches = [
            mock.patch.object(b, "STORE_ROOT", root),
            mock.patch.object(b, "TOKEN_PATH", token),
            mock.patch.object(b, "_geteuid", return_value=0),
            mock.patch.object(b, "_owner_ids", return_value=me),
            mock.patch.object(ae, "isatty", return_value=True),
            mock.patch.object(ae.os, "fstat", side_effect=fstat_as_root),
            mock.patch.object(ae, "github", side_effect=self._github),
            mock.patch.object(ae, "confirm", side_effect=self._confirm),
        ]
        for p in self._patches:
            p.start()
        ae.bind(b)  # never rely on an earlier test having bound it (pass 2)
        self.root, self.token = root, token

    def tearDown(self):
        for p in reversed(self._patches):
            p.stop()
        self._tmp.cleanup()

    def _github(self, path, token):
        if "/comments?" in path:
            page = int(path.rsplit("page=", 1)[1])
            chunk = self.comments[(page - 1) * 100: page * 100]
            return [{"body": c} for c in chunk]
        if path.endswith("/pulls"):
            return self.pulls
        if path.endswith("/branches/main"):
            return {"commit": {"sha": self.main_head}}
        raise AssertionError(path)

    def _confirm(self, text):
        self.confirm_text.append(text)
        return self.answer

    def mint(self, mode="dry-run", issue="1892", ttl=60):
        return ae.grant_from_ae(b, issue, mode, ttl)

    def refused(self, mode="dry-run", issue="1892"):
        with self.assertRaises(b.Refused) as ctx:
            self.mint(mode, issue)
        self.assertEqual(ctx.exception.code, b.EXIT_REFUSED)
        self.assertEqual(list((self.root / "grants").iterdir()), [], "a refusal minted a grant")
        return str(ctx.exception)

    def spend_dry_run(self, sha=SHA, script=SCRIPT, issue=1892):
        b.grant(str(issue), sha, f"script={script}", 60)
        b.consume(str(issue), sha, f"script={script}")


class Refusals(_Case):
    def test_grant_from_ae_refuses_non_root(self):
        with mock.patch.object(b, "_geteuid", return_value=1000):
            self.assertIn("root only", self.refused())

    def test_grant_from_ae_refuses_without_tty(self):
        with mock.patch.object(ae, "isatty", return_value=False):
            self.assertIn("TTY", self.refused())

    def test_no_prod_run_ae_refuses(self):
        self.comments = ["just a comment"]
        self.assertIn("no --prod-run AE", self.refused())

    def test_mode_mismatch_refuses(self):
        self.comments = [ae_body(apply=False)]
        self.assertIn("mode mismatch", self.refused(mode="apply"))

    def test_conflicting_aes_at_newest_sha_refuse(self):
        self.comments = [ae_body(script="scripts/1-a.py"), ae_body(script="scripts/1-b.py")]
        self.assertIn("ambiguous", self.refused())

    def test_sha_not_open_pr_head_refuses(self):
        for pulls in ([{"number": 7, "state": "closed", "head": {"sha": SHA}}],
                      [{"number": 7, "state": "open", "head": {"sha": OLD}}], []):
            with self.subTest(pulls=pulls):
                self.pulls = pulls
                self.assertIn("stale", self.refused())

    def test_github_unreadable_refuses(self):
        with mock.patch.object(ae, "github", side_effect=b.Refused(b.EXIT_REFUSED, "GitHub cannot be read")):
            self.assertIn("GitHub cannot be read", self.refused())

    def test_comments_past_cap_refuse(self):
        self.comments = ["x"] * (b.MAX_COMMENT_PAGES * 100) + [ae_body()]
        self.assertIn("more than", self.refused())

    def test_ae_for_another_issue_does_not_count(self):
        self.comments = [ae_body(issue=999)]
        self.assertIn("no --prod-run AE", self.refused())

    def test_bad_arguments_refuse(self):
        for issue, mode, ttl in (("0", "dry-run", 60), ("x", "dry-run", 60),
                                 ("1892", "apply,force", 60), ("1892", "dry-run", 0)):
            with self.subTest(args=(issue, mode, ttl)), self.assertRaises(b.Refused):
                ae.grant_from_ae(b, issue, mode, ttl)


class TokenFile(_Case):
    def test_loose_token_file_refuses(self):
        os.chmod(self.token, 0o644)
        self.assertIn("0600", self.refused())

    def test_non_root_token_owner_refuses(self):
        with mock.patch.object(ae.os, "fstat", side_effect=os.stat):
            self.assertIn("root-owned", self.refused())

    def test_missing_token_file_refuses(self):
        self.token.unlink()
        self.assertIn("unreadable", self.refused())

    def test_empty_token_file_refuses(self):
        self.token.write_text("  \n")
        self.assertIn("empty", self.refused())


class Confirmation(_Case):
    def test_empty_and_eof_answers_mint_nothing(self):
        self.answer = False
        self.assertIn("not confirmed", self.refused())

    def test_confirm_reads_only_y_or_yes(self):
        for raw, expected in (("y\n", True), ("YES\n", True), ("\n", False), ("", False),
                              ("n\n", False), ("yep\n", False), (" y \n", True)):
            with self.subTest(raw=raw), \
                    mock.patch("builtins.open", mock.mock_open(read_data=raw)):
                self.assertEqual(REAL_CONFIRM("prompt"), expected)

    def test_confirm_with_no_tty_is_not_consent(self):
        with mock.patch("builtins.open", side_effect=OSError("no tty")):
            self.assertFalse(REAL_CONFIRM("prompt"))

    def test_display_shows_the_action_prominently(self):
        self.mint()
        text = self.confirm_text[-1]
        self.assertIn(SHA, text)
        self.assertIn(SCRIPT, text)
        self.assertIn("\n  DRY RUN\n", text)


class Minting(_Case):
    def test_dry_run_mints_on_yes(self):
        nonce = self.mint()
        self.assertRegex(nonce, r"^[0-9a-f]{32}$")

    def test_record_matches_grant_byte_for_byte(self):
        with mock.patch.object(b.secrets, "token_hex", return_value="1" * 32), \
                mock.patch.object(b, "_now", return_value=b._now()) as now:
            self.mint()
            from_ae = (self.root / "grants" / ("1" * 32 + ".json")).read_bytes()
            (self.root / "grants" / ("1" * 32 + ".json")).unlink()
            b.grant("1892", SHA, f"script={SCRIPT}", 60)
            by_hand = (self.root / "grants" / ("1" * 32 + ".json")).read_bytes()
            self.assertTrue(now.called)
        self.assertEqual(from_ae, by_hand)

    def test_duplicate_live_grant_still_refused(self):
        self.mint()
        with self.assertRaises(b.Refused) as ctx:
            self.mint()
        self.assertEqual(ctx.exception.code, b.EXIT_DUPLICATE)

    def test_main_head_is_accepted_for_a_post_merge_step(self):
        self.pulls = [{"number": 7, "state": "closed", "head": {"sha": OLD}}]
        self.main_head = SHA
        self.mint()
        self.assertIn("current head of main", self.confirm_text[-1])

    def test_older_main_commit_refuses(self):
        self.pulls = [{"number": 7, "state": "closed", "head": {"sha": OLD}}]
        self.main_head = OLD
        self.assertIn("stale", self.refused())

    def test_count_label_mints_as_dry_run(self):
        self.comments = [ae_body(label="Task")]
        self.mint()
        self.assertIn("COUNT", self.confirm_text[-1])


class ApplyNeedsDryRun(_Case):
    def test_dry_run_then_apply_pair_mints_apply(self):
        self.comments = [ae_body(apply=False), ae_body(apply=True)]
        self.spend_dry_run()
        self.mint(mode="apply")
        self.assertIn("APPLY (writes production)", self.confirm_text[-1])

    def test_apply_needs_spent_dry_run_receipt(self):
        self.comments = [ae_body(apply=True)]
        self.assertIn("run the dry run first", self.refused(mode="apply"))

    def test_apply_refuses_without_same_sha_dry_run_receipt(self):
        self.comments = [ae_body(apply=True)]
        self.spend_dry_run(sha=OLD)
        self.assertIn("run the dry run first", self.refused(mode="apply"))
        b.grant("1892", SHA, f"script={SCRIPT},apply", 60)
        b.consume("1892", SHA, f"script={SCRIPT},apply")  # an apply receipt is not a dry run
        self.assertIn("run the dry run first", self.refused(mode="apply"))

    def test_unspent_dry_run_grant_does_not_count(self):
        self.comments = [ae_body(apply=True)]
        b.grant("1892", SHA, f"script={SCRIPT}", 60)
        with self.assertRaises(b.Refused):
            self.mint(mode="apply")


class FooterParsing(unittest.TestCase):
    def test_fenced_or_quoted_footer_is_ignored(self):
        fenced = "text\n```\n" + ae_body() + "```\n"
        quoted = "> " + ae_body().replace("\n", "\n> ")
        prose = "see prod-run=issue:1892,sha:" + SHA + ",script:" + SCRIPT + ",apply:true"
        for body in (fenced, quoted, prose):
            with self.subTest(body=body[:30]):
                self.assertIsNone(ae.footer_action(body))

    def test_non_ae_footer_is_ignored(self):
        self.assertIsNone(ae.footer_action(ae_body(kind="sweep")))

    def test_parity_with_prod_run(self):
        """The grammar is restated here (stdlib-only install). Every footer
        `l1_post.py` can write parses identically to `_prod_run.declared`, and
        on anything else this reader is never MORE permissive than the forge's."""
        import _prod_run
        canonical = [ae_body(), ae_body(apply=True), ae_body(label="Task"),
                     ae_body() + "  \n", ae_body(issue=7, sha=OLD, script="scripts/1-x_y.z.py")]
        for body in canonical:
            with self.subTest(body=body[-80:]):
                self.assertIsNotNone(ae.footer_action(body))
                self.assertEqual(ae.footer_action(body), _prod_run.declared(body))
        off = ["```\n" + ae_body() + "```", "no footer", ae_body(sha="g" * 40),
               ae_body(script="scripts/x.py"), ae_body().replace("apply:false", "apply:maybe"),
               ae_body().replace(f"issue:1892,sha:{SHA}", f"sha:{SHA},issue:1892")]
        for body in off:
            with self.subTest(body=body[-80:]):
                ours = ae.footer_action(body)
                self.assertTrue(ours is None or ours == _prod_run.declared(body))


class PassOneFindings(_Case):
    """harmonic-forge#1064 preclose pass 1: every one is a refusal, never a
    traceback, and nothing is minted."""

    def test_malformed_pull_request_json_refuses(self):
        for pulls in ([None], ["x"], [{"state": "open", "head": "x"}], {"not": "a list"}):
            with self.subTest(pulls=pulls):
                self.pulls = pulls
                self.assertIn("malformed", self.refused()) if isinstance(pulls, list) \
                    else self.assertIn("non-list", self.refused())

    def test_malformed_branch_json_refuses(self):
        self.pulls = []
        for branch in ({"commit": "x"}, "x", None):
            with self.subTest(branch=branch), \
                    mock.patch.object(self, "main_head", None), \
                    mock.patch.object(ae, "github", side_effect=lambda path, token, br=branch:
                                      br if path.endswith("/branches/main") else self._github(path, token)):
                self.assertIn("malformed branch", self.refused())

    def test_blockquoted_or_unclosed_fence_footer_is_ignored(self):
        one_line = "> " + ae_body().strip().splitlines()[-1]
        unclosed = "note\n```\n" + ae_body()
        for body in (one_line, unclosed):
            with self.subTest(body=body[:20]):
                self.assertIsNone(ae.footer_action(body))
                self.comments = [body]
                self.assertIn("no --prod-run AE", self.refused())

    def test_token_read_error_refuses(self):
        with mock.patch.object(ae.os, "read", side_effect=OSError(5, "EIO")):
            self.assertIn("unreadable", self.refused())

    def test_undecodable_tty_answer_is_not_consent(self):
        with mock.patch("builtins.open", side_effect=UnicodeDecodeError("utf-8", b"\xff", 0, 1, "bad")):
            self.assertFalse(REAL_CONFIRM("prompt"))

    def test_huge_issue_number_refuses(self):
        with self.assertRaises(b.Refused) as ctx:
            ae.grant_from_ae(b, "9" * 5000, "dry-run", 60)
        self.assertEqual(ctx.exception.code, b.EXIT_REFUSED)
        self.assertIsNone(ae.footer_action(ae_body(issue=int("9" * 12))))

    def test_real_github_converts_network_errors_to_refusal(self):
        import urllib.error
        real = REAL_GITHUB
        for exc in (urllib.error.URLError("down"), OSError("reset"), ValueError("bad json")):
            with self.subTest(exc=type(exc).__name__), \
                    mock.patch.object(ae.urllib.request, "urlopen", side_effect=exc), \
                    self.assertRaises(b.Refused) as ctx:
                real("repos/vitalharmony/hrse/branches/main", "t")
            self.assertIn("GitHub cannot be read", str(ctx.exception))

    def test_real_isatty_needs_both_stdin_and_stdout(self):
        for fds, expected in (({0, 1}, True), ({0}, False), ({1}, False), (set(), False)):
            with self.subTest(fds=fds), \
                    mock.patch.object(ae.os, "isatty", side_effect=lambda fd, f=fds: fd in f):
                self.assertEqual(REAL_ISATTY(), expected)

    def test_unexpected_exception_in_main_is_a_refusal(self):
        with mock.patch.object(ae, "grant_from_ae", side_effect=KeyError("boom")), \
                mock.patch("sys.stderr") as err, mock.patch("sys.stdout"):
            code = b.main(["grant-from-ae", "1892", "dry-run"])
        self.assertEqual(code, b.EXIT_REFUSED)
        self.assertIn("nothing minted", "".join(c.args[0] for c in err.write.call_args_list))


class ReforgedFooterScanner(unittest.TestCase):
    """Sticky-wicket REFORGE: every footer shape any pass found, in one table."""

    FOOTER = ae_body().strip().splitlines()[-1]

    def test_only_a_bare_final_line_counts(self):
        f = self.FOOTER
        cases = {
            "plain": (ae_body(), True),
            "trailing blank lines": (ae_body() + "\n\n  \n", True),
            "crlf": (ae_body().replace("\n", "\r\n"), True),
            "blockquote one line": ("> " + f, False),
            "blockquote multi": ("> text\n> " + f, False),
            "indented": ("    " + f, False),
            "inline fence prefix": ("```x```" + f, False),
            "inline text prefix": ("see " + f, False),
            "inside closed backticks": ("```\n" + f + "\n```\n", False),
            "inside closed tildes": ("~~~\n" + f + "\n~~~\n", False),
            "unclosed backticks": ("```\n" + f, False),
            "unclosed tildes": ("~~~python\n" + f, False),
            "tilde closed by backticks": ("~~~\n```\n" + f, False),
            "longer fence needs longer close": ("````\n```\n" + f, False),
            "after a closed fence": ("```\ncode\n```\n" + f, True),
            "after a closed tilde fence": ("~~~\ncode\n~~~\n" + f, True),
            "text after footer": (f + "\nmore", False),
            "fence inside a list item": ("- ```bash\n  echo hi\n  ```\n" + f, True),
            "ordered list fence": ("1. ```\n   x\n   ```\n" + f, True),
            "list fence ended by dedent": ("- ```\n  x\ntext\n" + f, True),
            "over-indented top-level closer": ("```\n    ```\n" + f, False),
            "list fence left open": ("- ```\n  x\n  " + f, False),
            "footer with suffix": (f + " trailing", False),
        }
        for name, (body, counts) in cases.items():
            with self.subTest(name=name):
                self.assertEqual(ae.footer_action(body) is not None, counts, name)


class OutcomeReporting(_Case):
    """Sticky-wicket REFORGE: the exit code reflects the store, not the
    exception type."""

    def run_main(self, *argv):
        err = []
        with mock.patch("sys.stderr") as stderr, mock.patch("sys.stdout"):
            stderr.write.side_effect = err.append
            code = b.main(list(argv))
        return code, "".join(err)

    def test_failure_after_the_write_reports_a_possible_grant(self):
        real_fsync = os.fsync

        def fail_after_write(fd):
            real_fsync(fd)
            raise OSError(5, "EIO after write")

        with mock.patch.object(b.os, "fsync", side_effect=fail_after_write):
            code, err = self.run_main("grant-from-ae", "1892", "dry-run")
        nonce = next((self.root / "grants").iterdir()).stem
        self.assertEqual(code, b.EXIT_MINT_UNCERTAIN)
        self.assertIn("MAY be live", err)
        self.assertIn(nonce, err)
        self.assertNotIn("nothing minted", err)

    def test_missing_helper_module_is_a_refusal(self):
        import builtins
        real_import = builtins.__import__

        def no_helper(name, *a, **k):
            if name == "hrse_gate_ae":
                raise ModuleNotFoundError("No module named 'hrse_gate_ae'")
            return real_import(name, *a, **k)

        with mock.patch.dict(sys.modules, {"hrse_gate_ae": None}), \
                mock.patch("builtins.__import__", side_effect=no_helper):
            code, err = self.run_main("grant-from-ae", "1892", "dry-run")
        self.assertEqual(code, b.EXIT_REFUSED)
        self.assertIn("nothing minted", err)

    def test_ctrl_c_at_the_prompt_is_a_refusal(self):
        with mock.patch.object(ae, "confirm", side_effect=KeyboardInterrupt()):
            code, err = self.run_main("grant-from-ae", "1892", "dry-run")
        self.assertEqual(code, b.EXIT_REFUSED)
        self.assertIn("nothing minted", err)
        self.assertEqual(list((self.root / "grants").iterdir()), [])

    def test_a_refusal_keeps_its_own_code_and_message(self):
        self.comments = ["nothing here"]
        code, err = self.run_main("grant-from-ae", "1892", "dry-run")
        self.assertEqual(code, b.EXIT_REFUSED)
        self.assertIn("no --prod-run AE", err)

    def test_duplicate_keeps_exit_duplicate(self):
        self.mint()
        code, _ = self.run_main("grant-from-ae", "1892", "dry-run")
        self.assertEqual(code, b.EXIT_DUPLICATE)

    def test_success_prints_the_nonce(self):
        with mock.patch("sys.stdout") as out, mock.patch("sys.stderr"):
            code = b.main(["grant-from-ae", "1892", "dry-run"])
        self.assertEqual(code, b.EXIT_OK)
        printed = "".join(c.args[0] for c in out.write.call_args_list)
        self.assertRegex(printed, r"[0-9a-f]{32}")


class MintedHolder(OutcomeReporting):
    """Cluster REFORGE: the outcome comes from this run's own recorded nonce."""

    def test_concurrent_mint_during_prompt_is_not_credited(self):
        def other_terminal_mints(text):
            b.grant("1893", SHA, f"script={SCRIPT}", 60)  # someone else's grant
            return False                                  # this run answers n
        with mock.patch.object(ae, "confirm", side_effect=other_terminal_mints):
            code, err = self.run_main("grant-from-ae", "1892", "dry-run")
        self.assertEqual(code, b.EXIT_REFUSED)
        self.assertNotIn("MAY be live", err)
        self.assertIn("nothing minted", err)

    def test_print_failure_after_a_mint_reports_the_nonce(self):
        with mock.patch("builtins.print", side_effect=[BrokenPipeError("stdout gone"), None]) as pr:
            code = b.main(["grant-from-ae", "1892", "dry-run"])
        nonce = next((self.root / "grants").iterdir()).stem
        self.assertEqual(code, b.EXIT_MINT_UNCERTAIN)
        self.assertIn(nonce, str(pr.call_args_list[-1]))

    def test_store_is_never_read_to_infer_the_outcome(self):
        self.assertFalse(hasattr(b, "_grant_nonces"))


class ReceiptKey(_Case):
    def test_dry_run_receipt_for_another_script_does_not_unlock_apply(self):
        self.comments = [ae_body(apply=True)]
        self.spend_dry_run(script="scripts/1-other.py")
        self.assertIn("run the dry run first", self.refused(mode="apply"))

    def test_dry_run_receipt_for_another_issue_does_not_unlock_apply(self):
        self.comments = [ae_body(apply=True)]
        self.spend_dry_run(issue=1893)
        self.assertIn("run the dry run first", self.refused(mode="apply"))


class BrokerWiring(unittest.TestCase):
    def test_parser_offers_the_verb_with_no_path_option(self):
        verb = b._parser()._subparsers._group_actions[0].choices["grant-from-ae"]
        options = sorted(s for a in verb._actions for s in a.option_strings)
        self.assertEqual(options, ["--help", "--ttl-minutes", "-h"])

    def test_consume_never_imports_the_helper(self):
        """The helper is imported in exactly one place, `_grant_from_ae`; the
        NOPASSWD `consume` path and module import never load it."""
        import ast
        tree = ast.parse((HERE / "hrse_gate_broker.py").read_text(encoding="utf-8"))
        owners = []
        for func in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
            for node in ast.walk(func):
                if isinstance(node, ast.Import) and any(a.name == "hrse_gate_ae" for a in node.names):
                    owners.append(func.name)
        top = [n for n in tree.body if isinstance(n, ast.Import)
               and any(a.name == "hrse_gate_ae" for a in n.names)]
        self.assertEqual((owners, top), (["_grant_from_ae"], []))


if __name__ == "__main__":
    unittest.main()
