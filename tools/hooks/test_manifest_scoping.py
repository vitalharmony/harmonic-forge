#!/usr/bin/env python3
"""BATCH, IMC, FORCE, UI and `closed_keys` scope by the home-repo rule, and an
unreadable `projects.toml` grants nothing (harmonic-forge#1596). TC numbers are
the handoff's.

A hook computes its prefix maps at import, so each scratch-manifest case runs
in a fresh interpreter (`probe`) with `FORGE_PROJECTS_MANIFEST` at the scratch
file and `HOME` at a scratch dir: no case can reach a live grant store.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE.parent / "gh"), str(HERE.parent / "onboard")]

import batch_auth  # noqa: E402
import expand_lane_shorthand as els  # noqa: E402
import force_grant  # noqa: E402
import l1_post  # noqa: E402
import report_red_main  # noqa: E402
import tasklist_owed  # noqa: E402
import ui_grant  # noqa: E402
from test_manifest_overlay import overlay_only_fixture, probe_from  # noqa: E402

ADOPTER = ('[[project]]\nname = "app"\nprefix = "Z"\nrepo = "acme/app"\naccount = "acme"\n'
           'workspace = "acme"\nboard_owner = "acme"\nboard_number = "1"\n')
FOREIGN = '[[project]]\nname = "other"\nprefix = "K"\nrepo = "other/thing"\naccount = "someone"\n'
INVALID = ADOPTER.replace('workspace = "acme"\n', "") + "onboarded = true\n"
PRELUDE = ("import json, os, sys, tempfile\nfrom pathlib import Path\nos.isatty = lambda fd: True\n"
           f"sys.path[:0] = [{str(HERE)!r}, {str(HERE.parent / 'gh')!r}, "
           f"{str(HERE.parent / 'onboard')!r}]\n"
           "import batch_auth as ba, expand_lane_shorthand as m\n"
           "ba.merge_ci_state = lambda *a, **k: ('green', 'faked', 'f' * 40)\n"
           "tmp = Path(tempfile.mkdtemp())\n")


def probe(case: unittest.TestCase, manifest_text: str | None, code: str) -> dict:
    """`code`'s printed JSON, run with a scratch manifest (None: an absent one)."""
    tmp = tempfile.TemporaryDirectory()
    case.addCleanup(tmp.cleanup)
    root = Path(tmp.name)
    manifest = root / "projects.toml"
    if manifest_text is not None:
        manifest.write_text(manifest_text, encoding="utf-8")
    env = {k: v for k, v in os.environ.items()
           if k not in ("LANE", "CLAUDE_CODE_ENTRYPOINT", "GH_REPO")}
    env.update(FORGE_PROJECTS_MANIFEST=str(manifest), HOME=str(root),
               HARMONIC_FORGE_TESTING="1")
    result = subprocess.run([sys.executable, "-B", "-c", PRELUDE + code], cwd=root, env=env,
                            capture_output=True, text=True, timeout=120, check=False)
    case.assertEqual(result.returncode, 0, result.stderr)
    return json.loads(result.stdout.strip().splitlines()[-1])


class AdopterHomeRepo(unittest.TestCase):
    """TC1 / TC5 / TC6 / TC7 under a valid scratch manifest."""

    CODE = (
        "out = {'z': m.authorize_batch('BATCH Z5', state_path=tmp / 'z.json')}\n"
        "ba.link_pr('Z5', 'acme/app', 77, state_path=tmp / 'z.json')\n"
        "out['merge'] = ba.decide('gh pr merge 77 --match-head-commit ' + 'f' * 40 + "
        "' --repo acme/app --squash', state_path=tmp / 'z.json')\n"
        "out['q'] = m.authorize_batch('BATCH Q5', state_path=tmp / 'q.json')\n"
        "out['q_written'] = (tmp / 'q.json').exists()\n"
        "out['closed'] = ba.closed_keys('gh issue close 5 --repo acme/app')\n"
        "out['closed_k'] = ba.closed_keys('gh issue close 5 --repo other/thing')\n"
        "import force_grant, imc_waiver, ui_grant\n"
        "force_grant.FORCE_STATE_DIR = tmp / 'force'\n"
        "force_grant.covered_head = lambda repo, issue: 'c' * 40\n"
        "out['force_z'] = m.authorize_force('FORCE Z5: r', 'sess', '1')\n"
        "out['force_z_written'] = (tmp / 'force' / 'Z5.json').exists()\n"
        "out['force_k'] = m.authorize_force('FORCE K5: r', 'sess', '1')\n"
        "out['home'] = [imc_waiver.is_home_repo(r) for r in ('acme/app', 'other/thing')]\n"
        "body = ui_grant.heading('Z5', 'plan') + '\\n' + ui_grant.marker('Z5', 'plan', 'x')\n"
        "out['ui'] = [bool(ui_grant.find_record([{'login': who, 'body': body}], 'Z5', 'plan'))\n"
        "             for who in ('acme', 'vitalharmony')]\n"
        "print(json.dumps(out))\n")

    def setUp(self) -> None:
        self.out = probe(self, ADOPTER + FOREIGN, self.CODE)

    def test_tc1_batch_on_an_adopter_home_repo_authorizes_its_merge(self) -> None:
        self.assertIn("authorized Z5", self.out["z"])
        self.assertEqual(self.out["merge"][0], "allow", self.out["merge"])
        self.assertEqual((self.out["q"], self.out["q_written"]), ("", False))

    def test_tc5_closed_keys_retires_on_a_home_repo_only(self) -> None:
        self.assertEqual(self.out["closed"], [["Z5", "acme/app", 5]])
        self.assertEqual(self.out["closed_k"], [])

    def test_tc6_force_writes_for_a_home_key_and_refuses_a_foreign_one(self) -> None:
        self.assertIn("pending plain grant written for Z5", self.out["force_z"])
        self.assertTrue(self.out["force_z_written"])
        self.assertIn("cross-account repo", self.out["force_k"])
        self.assertEqual(self.out["home"], [True, False])

    def test_tc7_a_ui_record_is_read_from_the_home_account(self) -> None:
        self.assertEqual(self.out["ui"], [True, False])


class UnreadableManifestGrantsNothing(unittest.TestCase):
    """TC2 / AC2: invalid, unparseable or absent `projects.toml`: an empty map,
    no key, a refusal naming `projects.toml`, and no state written."""

    CODE = ("print(json.dumps({'map': ba.REPO_PREFIXES, 'err': ba.REPO_PREFIXES_ERROR,\n"
            "    'scan': m._scan_batch('BATCH H7 Z5'),\n"
            "    'receipt': m.authorize_batch('BATCH H7 Z5', state_path=tmp / 's.json'),\n"
            "    'written': (tmp / 's.json').exists()}))\n")

    def test_tc2_no_prefix_is_a_batch_key_and_batch_refuses_out_loud(self) -> None:
        for name, text in (("invalid", INVALID), ("unparseable", "[[project\n"), ("absent", None)):
            with self.subTest(manifest=name):
                out = probe(self, text, self.CODE)
                self.assertEqual(out["map"], {})
                self.assertIn("ManifestError", out["err"])
                self.assertIsNone(out["scan"])
                self.assertIn("BATCH REFUSED: projects.toml could not be read", out["receipt"])
                self.assertFalse(out["written"])


class OverlayIsNeverHome(unittest.TestCase):
    """TC3 / AC3: from inside an overlay-only checkout, the overlay repo is no
    home repo and no BATCH key; the base's own home repo still is."""

    CODE = ("import json, manifest_scope, expand_lane_shorthand as m\n"
            "sys.path.insert(0, " + repr(str(HERE.parent / "gh")) + ")\n"
            "import imc_waiver\n"
            "print(json.dumps([manifest_scope.home_project('acme/app') is None,\n"
            "    imc_waiver.is_home_repo('acme/app'), imc_waiver.is_home_repo('acme/forge'),\n"
            "    m._scan_batch('BATCH Z5')]))\n")

    def test_tc3_the_overlay_repo_gets_no_home_scope(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo, base = overlay_only_fixture(Path(tmp).resolve())
            out = json.loads(probe_from(repo, base, "import sys\n" + self.CODE))
        self.assertEqual(out, [True, False, True, None])


#: harmonic-forge#1635 reforge (operator D52): V2, the raw widening overlay. It
#: claims the base repo's name and prefix (F), its account, workspace and
#: board. manifest_overlay's validator refuses these claims, so a writer that
#: reads through the real manifest_overlay API sees no overlay at all, and on a
#: read that falls back to the overlay only on a miss, its F is shadowed by the
#: base's F. It is written raw, as a hostile clone would ship it, and catches
#: raw reads only (KD2, KD4). It complements V1, it does not replace it.
WIDENING_OVERLAY = ('[project]\nname = "forge"\nprefix = "F"\nrepo = "acme/app"\n'
                    'account = "acme"\nworkspace = "acme"\nboard_owner = "acme"\n'
                    'board_number = "1"\nhome = true\n')


class OverlayContentNeverChangesGrantScope(unittest.TestCase):
    """harmonic-forge#1635 reforge (sticky-wicket REFORGE, operator D52/D53):
    overlay content cannot change a driven grant decision. The property is
    noninterference, checked by behavior: one decision program runs from inside
    an overlay-only checkout three times, with the overlay absent (V0), the
    fixture overlay (V1) and WIDENING_OVERLAY (V2). Every driven decision,
    every receipt and the bytes of every grant or receipt file written must be
    identical across the three, and V0 must equal EXPECTED. The check does not
    depend on how a read is spelled: a read through any spelling that changes
    a driven decision changes the output.

    V1 is the validator-accepted widening overlay (claims letter Z, repo
    acme/app, account acme). It is not benign and must never be dropped: it
    is the variant that catches reads through the real manifest_overlay API,
    the plausible regression class (KD1, KD3, KD5, KD6 are killed by V1 only).
    V2 is the raw widening the validator refuses; it catches raw reads (KD2,
    KD4, killed by V1 and V2). KD7 reads no overlay: it fails the `reached`
    anchor in EXPECTED on V0, not the three-way comparison.

    Driven: `manifest_scope.home_project`, `imc_waiver.is_home_repo` and
    `key_target` (Z5, F5); `_scan_batch`, `authorize_batch` and `closed_keys`
    (`--repo` and bare forms); `ui_grant.find_record`; FORCE, IMC, UI, PARK
    and SCAN receipts for Z5/F5 (Z, F); `batch_auth._slot_env('acme/app')`, the
    hook_identity -> manifest_identity credential path; and imc_waiver's
    posting identity (`apply_project_identity`, D23: posting identity on a
    projects.toml repo, not grant scope). State is scratch and `now` is fixed.

    `reached` (in EXPECTED) shows the program exercises decisions: FORCE, IMC
    and UI each got past their home-repo decision for F5.

    Stated residuals (the reforge verdict's, not hidden):
    1. Input coverage. Only the decision paths and inputs driven here are
       compared. A writer path this program never calls, or a read gated on a
       trigger it never sends (a hostile author's own key), is not caught. That
       gap shrinks by adding driven inputs, not by adding recognizers. Not
       driven: batch_auth's merge-side account checks (`issue_key`,
       `_derived_merge_match`, `decide`, `consume`); IMC's own scope check for
       Z5 (`imc_z` is refused at the BATCH-syntax step first, so IMC's
       `key_target`/`is_home_repo` are covered only by the direct calls); and
       `apply_project_identity`, which is called but cannot vary, because
       every variant claims account acme and `_probe_login` is stubbed.
    2. A hostile author in production code is review's to stop, not a test's.
       This suite is a tripwire for plausible regressions.
    3. A read that cannot change a decision is permitted by design: the
       property is noninterference, not "never opened".

    `seen` is a diagnostic only, and not complete: it records calls to the
    `manifest_overlay` entry points (passed through, never raised) so a failure
    message can name the read. Reads by any other spelling do not appear in it;
    the equality is the assertion."""

    STATE = "{STATE}"

    CODE = (
        "import datetime, json, os, pathlib, sys\n"
        "sys.path.insert(0, " + repr(str(HERE.parent / "gh")) + ")\n"
        "import manifest_overlay\n"
        "seen = []\n"
        "def _record(name, fn):\n"
        "    def wrapper(*a, **k):\n"
        "        seen.append(name)\n"
        "        return fn(*a, **k)\n"
        "    return wrapper\n"
        "for _name in ('read', 'project_for_checkout', 'path_project'):\n"
        "    setattr(manifest_overlay, _name, _record(_name, getattr(manifest_overlay, _name)))\n"
        "import batch_auth, force_grant, imc_waiver, manifest_identity, manifest_scope\n"
        "import park_grant, scan_grant, ui_grant, expand_lane_shorthand as m\n"
        "_st = pathlib.Path(STATE)\n"
        "_now = datetime.datetime(2030, 1, 1, tzinfo=datetime.timezone.utc)\n"
        "imc_waiver._now = batch_auth._now = lambda: _now\n"
        "force_grant.FORCE_STATE_DIR = _st / 'force'\n"
        "ui_grant.UI_STATE_DIR = _st / 'ui'\n"
        "imc_waiver.IMC_STATE_DIR = _st / 'imc'\n"
        "park_grant.PARK_STATE_DIR = _st / 'park'\n"
        "scan_grant.SCAN_STATE_DIR = _st / 'scan'\n"
        "def _body(key):\n"
        "    return ui_grant.heading(key, 'plan') + '\\n' + ui_grant.marker(key, 'plan', 'x')\n"
        "def _labels(repo, number):\n"
        "    return {'tooling-exception'}, ''\n"
        "out = {\n"
        "  'home_project': [getattr(manifest_scope.home_project(r), 'repo', None)\n"
        "                   for r in ('acme/app', 'acme/forge')],\n"
        "  'is_home_repo': [imc_waiver.is_home_repo(r) for r in ('acme/app', 'acme/forge')],\n"
        "  'key_target': [imc_waiver.key_target(k) for k in ('Z5', 'F5')],\n"
        "  'scan_batch': [m._scan_batch(p) for p in ('BATCH Z5', 'BATCH F5')],\n"
        "  'batch': [m.authorize_batch('BATCH Z5', state_path=_st / 'batch-z.json'),\n"
        "            m.authorize_batch('BATCH F5', state_path=_st / 'batch-f.json')],\n"
        "  'closed_keys': [batch_auth.closed_keys('gh issue close 5 --repo acme/app'),\n"
        "                  batch_auth.closed_keys('gh issue close 5', os.getcwd()),\n"
        "                  batch_auth.closed_keys('gh issue close 5 --repo acme/forge')],\n"
        "  'find_record': [bool(ui_grant.find_record([{'login': 'acme', 'body': _body(k)}],\n"
        "                                            k, 'plan')) for k in ('Z5', 'F5')],\n"
        "  'slot_env': batch_auth._slot_env('acme/app'),\n"
        "}\n"
        "out['grants'] = {\n"
        "  'force_z': m.authorize_force('FORCE Z5: probe', 's', '1', _now),\n"
        "  'force_f': m.authorize_force('FORCE F5: probe', 's', '1', _now),\n"
        "  'imc_z': m.authorize_imc('BATCH Z5 IMC Z5', 's', '1', label_reader=_labels, now=_now),\n"
        "  'imc_f': m.authorize_imc('BATCH F5 IMC F5', 's', '1', label_reader=_labels, now=_now),\n"
        "  'ui_z': m.authorize_ui('UI Z5: probe', 's', '1', _now),\n"
        "  'ui_f': m.authorize_ui('UI F5: probe', 's', '1', _now),\n"
        "  'park_z': m.authorize_park('PARK Z5: probe', 's', '1', _now),\n"
        "  'park_f': m.authorize_park('PARK F5: probe', 's', '1', _now),\n"
        "  'scan_z': m.authorize_scan('SCAN Z open-issues: probe', 's', _now),\n"
        "  'scan_f': m.authorize_scan('SCAN F open-issues: probe', 's', _now),\n"
        "}\n"
        "reached = {'force_f': 'has no preclose receipt', 'imc_f': 'waiver hook record written',\n"
        "           'ui_f': 'pending plan grant written'}\n"
        "out['reached'] = sorted(k for k, v in reached.items() if v in (out['grants'][k] or ''))\n"
        "manifest_identity._probe_login = lambda *a, **k: 'acme'\n"
        "try:\n"
        "    out['identity'] = manifest_identity.apply_project_identity('acme/forge')\n"
        "except BaseException as exc:\n"
        "    out['identity'] = type(exc).__name__\n"
        "out['files'] = {str(p.relative_to(_st)): p.read_bytes().decode('utf-8', 'replace')\n"
        "                for p in sorted(_st.rglob('*')) if p.is_file()}\n"
        "print(json.dumps({'out': out, 'seen': seen}))\n")

    #: V0's output with the scratch state path as `{STATE}`. The grant and
    #: receipt bytes are pinned by the cross-variant equality, and their names here.
    EXPECTED = {
        "home_project": [None, "acme/forge"],
        "is_home_repo": [False, True],
        "key_target": [None, ["acme/forge", 5]],
        "scan_batch": [None, [0, ["F5"]]],
        "closed_keys": [[], [], [["F5", "acme/forge", 5]]],
        "find_record": [False, True],
        "slot_env": None,
        "reached": ["force_f", "imc_f", "ui_f"],
        "identity": "acme",
    }
    SCAN_Z = "SCAN REFUSED: no repo prefix matches 'Z'. No grant written."

    def run_variants(self) -> dict[str, dict]:
        """The program's output from the overlay-only checkout per overlay state,
        each from fresh scratch state at the same path."""
        outputs = {}
        with tempfile.TemporaryDirectory() as tmp:
            repo, base = overlay_only_fixture(Path(tmp).resolve())
            overlay = repo / ".claude" / "forge.toml"
            benign = overlay.read_text(encoding="utf-8")
            state = Path(tmp).resolve() / "state"
            code = "import sys\nSTATE = " + repr(str(state)) + "\n" + self.CODE
            for name, text in (("V0", None), ("V1", benign), ("V2", WIDENING_OVERLAY)):
                shutil.rmtree(state, ignore_errors=True)
                state.mkdir()
                if text is None:
                    overlay.unlink(missing_ok=True)
                else:
                    overlay.write_text(text, encoding="utf-8")
                raw = json.loads(probe_from(repo, base, code).strip().splitlines()[-1])
                outputs[name] = json.loads(json.dumps(raw).replace(str(state), self.STATE))
        return outputs

    def assert_noninterference(self, outputs: dict[str, dict]) -> None:
        v0 = outputs["V0"]["out"]
        for name in ("V1", "V2"):
            with self.subTest(variant=name):
                self.assertEqual(outputs[name]["out"], v0,
                                 f"{name} differs from V0; manifest_overlay entry points "
                                 f"called (diagnostic only): {outputs[name]['seen']}")
        for field, value in self.EXPECTED.items():
            self.assertEqual(v0[field], value, field)
        grants = v0["grants"]
        self.assertEqual(grants["scan_z"], self.SCAN_Z)
        self.assertIn("may be scanned", grants["scan_f"])
        self.assertIn("PARK REFUSED for Z5: no repo prefix matches it", grants["park_z"])
        self.assertIn("may now be completed", grants["park_f"])
        self.assertIn("no repo maps to its prefix", grants["force_z"])
        self.assertIn("no repo maps to its prefix", grants["ui_z"])
        self.assertIn("authorized F5", v0["batch"][1])
        self.assertEqual(v0["batch"][0], "")
        self.assertEqual(sorted(v0["files"]), self.EXPECTED_FILES)

    #: The scratch files V0 writes: the F5 grants and their locks, nothing for Z5.
    EXPECTED_FILES = ["batch-f.json", "batch-f.json.lock", "imc/F5.json", "park/s/F5.json",
                      "park/s/F5.json.lock", "scan/s/F-9037c5488f26c3b7.json",
                      "scan/s/F-9037c5488f26c3b7.json.lock", "ui/F5.json", "ui/F5.json.lock"]

    def test_overlay_content_changes_no_grant_decision(self) -> None:
        self.assert_noninterference(self.run_variants())

    def test_an_inherited_sdk_entrypoint_changes_nothing(self) -> None:
        """harmonic-forge#1635 gap 4: under `CLAUDE_CODE_ENTRYPOINT=sdk` every
        authorizer refuses at provenance, which would leave `reached` empty and
        exercise no scope decision; `probe_from` drops it."""
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_ENTRYPOINT": "sdk"}):
            self.assert_noninterference(self.run_variants())


class LiveForeignKeysStillRefuse(unittest.TestCase):
    """TC4 / AC4: on the checked-in manifest K/Y/D/P keys refuse IMC, FORCE and UI
    as cross-account, exactly as before."""

    def test_tc4_k_y_d_p_refuse_imc_force_and_ui(self) -> None:
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        tmp = Path(scratch.name)
        with mock.patch.object(force_grant, "FORCE_STATE_DIR", tmp / "force"), \
                mock.patch.object(ui_grant, "UI_STATE_DIR", tmp / "ui"), \
                mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CLAUDE_CODE_ENTRYPOINT", None)
            os.environ.pop("FORGE_PROJECTS_MANIFEST", None)
            for letter in "KYDP":
                with self.subTest(letter=letter):
                    imc = els.authorize_imc(f"BATCH {letter}5 IMC {letter}5", "s", "1",
                                            label_reader=lambda repo, n: ({"x"}, ""))
                    self.assertIn("cross-account repo", imc)
                    self.assertIn("cross-account repo", els.authorize_force(f"FORCE {letter}5: r", "s", "1"))
                    self.assertIn("cross-account repo", els.authorize_ui(f"UI {letter}5: r", "s", "1"))
        self.assertFalse(any(tmp.rglob("*.json")))


class UiChangeCheckFailsClosed(unittest.TestCase):
    """Pitch revision 1: an unreadable manifest refuses the handoff post rather
    than reading the repo as not-home and skipping the `**UI change:**` check."""

    def test_an_unreadable_manifest_refuses_the_post(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(os.environ, {"FORGE_PROJECTS_MANIFEST": f"{tmp}/absent.toml"}):
            with self.assertRaisesRegex(SystemExit, "projects.toml could not be read"):
                l1_post.validate_ui_brand("no lead line", "acme/app", 5, comments=[])

    def test_a_readable_manifest_skips_a_repo_that_is_not_home(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "projects.toml"
            path.write_text(FOREIGN, encoding="utf-8")
            with mock.patch.dict(os.environ, {"FORGE_PROJECTS_MANIFEST": str(path)}):
                self.assertIsNone(l1_post.validate_ui_brand("x", "other/thing", 5, comments=[]))


class EmptyMapReadersSayWhy(unittest.TestCase):
    """Pitch revision 2: report_red_main and tasklist_owed name the manifest
    error instead of going silent on an empty map."""

    def test_report_red_main_and_tasklist_owed_name_the_error(self) -> None:
        why = "ManifestError: no manifest at /x"
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(report_red_main, "REPO_PREFIXES", {}), \
                mock.patch.object(report_red_main, "REPO_PREFIXES_ERROR", why):
            context = report_red_main.handle(Path(tmp) / "s.json")
        self.assertIn(why, context["hookSpecificOutput"]["additionalContext"])
        err = io.StringIO()
        with mock.patch.object(batch_auth, "REPO_PREFIXES", {}), \
                mock.patch.object(batch_auth, "REPO_PREFIXES_ERROR", why), \
                contextlib.redirect_stderr(err):
            self.assertIsNone(tasklist_owed.key_for("acme/app", 5))
        self.assertIn(why, err.getvalue())


if __name__ == "__main__":
    unittest.main()
