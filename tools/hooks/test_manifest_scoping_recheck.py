#!/usr/bin/env python3
"""The home-repo rule holds where a grant is posted or spent, not only where it
is written (harmonic-forge#1596, sticky-wicket PATCH items 1, 6, 4, 5a, 5b).

`projects.toml` can change between the operator's line and the record or the
spend, so FORCE `record`/`require` and UI `record` re-check it; the login rule
and the account grammar live in one place each. Every case runs in a fresh
interpreter (`probe`) with a scratch manifest and a scratch `HOME`.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE.parent / "onboard")]

import manifest  # noqa: E402
from manifest_protocol import ManifestError  # noqa: E402
from test_manifest_scoping import ADOPTER, probe  # noqa: E402

#: Makes Z (acme/app) registered but no longer home: owner `acme`, account `bob`.
DEMOTE = ("mpath = Path(os.environ['FORGE_PROJECTS_MANIFEST'])\n"
          "mpath.write_text(mpath.read_text().replace('account = \"acme\"', 'account = \"bob\"'))\n"
          "os.environ['LANE'] = '1'\n"
          "calls = []\n"
          "def spy(argv, **kw):\n"
          "    import subprocess\n"
          "    calls.append(argv)\n"
          "    return subprocess.CompletedProcess(argv, 1, '', 'spy')\n"
          "def refusal(fn, *a, **k):\n"
          "    try:\n"
          "        fn(*a, **k)\n"
          "    except BaseException as exc:  # SystemExit or GrantRefused\n"
          "        return str(exc)\n"
          "    return 'accepted'\n")


class LoginComparison(unittest.TestCase):
    def test_item1_a_ui_record_by_a_differently_cased_login_is_accepted(self) -> None:
        out = probe(self, ADOPTER, (
            "import ui_grant\n"
            "body = ui_grant.heading('Z5', 'plan') + '\\n' + ui_grant.marker('Z5', 'plan', 'x')\n"
            "print(json.dumps(bool(ui_grant.find_record([{'login': 'Acme', 'body': body}],"
            " 'Z5', 'plan'))))\n"))
        self.assertTrue(out)


class AccountGrammar(unittest.TestCase):
    def test_item6_a_non_login_account_is_a_manifest_error(self) -> None:
        for account in ("5", '"a b"'):
            with self.subTest(account=account), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "projects.toml"
                path.write_text(ADOPTER.replace('account = "acme"', f"account = {account}"),
                                encoding="utf-8")
                with self.assertRaisesRegex(ManifestError, "is not a GitHub login"):
                    manifest.load(path)


class RecheckedAtPostAndSpend(unittest.TestCase):
    def test_item4_force_record_and_require_refuse_once_the_repo_is_not_home(self) -> None:
        out = probe(self, ADOPTER, (
            "import force_grant, imc_waiver\n"
            "force_grant.FORCE_STATE_DIR = tmp / 'force'\n"
            "force_grant.covered_head = lambda repo, issue: 'c' * 40\n"
            "written = m.authorize_force('FORCE Z5: r', 'sess', '1')\n" + DEMOTE +
            "rec = refusal(force_grant.record, 'Z5', runner=spy)\n"
            "path = force_grant.grant_path('Z5')\n"
            "imc_waiver.write_json_atomic(path, {**imc_waiver.read_json(path), 'status': 'usable'})\n"
            "req = refusal(force_grant.require, 'acme/app', 5, 'c' * 40)\n"
            "print(json.dumps({'written': written, 'record': rec, 'require': req,"
            " 'calls': calls}))\n"))
        self.assertIn("pending plain grant written for Z5", out["written"])
        self.assertIn("no longer a home repo", out["record"])
        self.assertIn("no longer a home repo", out["require"])
        self.assertEqual(out["calls"], [])

    def test_item5a_ui_record_refuses_and_posts_nothing_once_the_repo_is_not_home(self) -> None:
        out = probe(self, ADOPTER, (
            "import ui_grant\n"
            "ui_grant.UI_STATE_DIR = tmp / 'ui'\n"
            "written = m.authorize_ui('UI Z5: r', 'sess', '1')\n" + DEMOTE +
            "rec = refusal(ui_grant.record, 'Z5', runner=spy)\n"
            "print(json.dumps({'written': written, 'record': rec, 'calls': calls}))\n"))
        self.assertIn("pending plan grant written for Z5", out["written"])
        self.assertIn("no longer a home repo", out["record"])
        self.assertEqual(out["calls"], [])


class OptedInRepos(unittest.TestCase):
    def test_item5b_a_sibling_under_the_owner_that_is_not_home_is_never_a_merge_target(self) -> None:
        sibling = '[[project]]\nname = "other"\nprefix = "W"\nrepo = "acme/other"\naccount = "bob"\n'
        out = probe(self, ADOPTER + sibling, (
            "import subprocess, ui_grant\n"
            "ok = lambda argv, **kw: subprocess.CompletedProcess(argv, 0, 'sha', '')\n"
            "print(json.dumps(ui_grant.opted_in_repos('acme', runner=ok)))\n"))
        self.assertEqual(out, ["acme/app"])


if __name__ == "__main__":
    unittest.main()
