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


class OverlayFunnelNeverReachedByGrantScope(unittest.TestCase):
    """harmonic-forge#1579 sticky-wicket PATCH (operator D22/D23): every grant
    writer's scope decision, run from inside an overlay-only checkout with every
    `manifest_overlay` entry point patched to raise, answers not-home / no-target
    without reading the overlay. Production reaches the overlay only through
    `manifest_overlay.<attr>` (test_manifest_scope pins the naming files), so any
    overlay read on these paths trips the patch, whatever its import spelling.

    What each writer decides (harmonic-forge#1635): FORCE, IMC and UI make a
    home-repo decision, and `reached` asserts each got past it. PARK and SCAN
    make none: PARK checks the key against the fixed pattern
    `tasklist_owed.KEY_RE`, SCAN the letter against its fixed map
    `scan_grant.REPOS`; FIXED_MAP asserts those answers and that they are the
    same with the overlay file removed.

    D23's stated limit: imc_waiver posts with `apply_project_identity` on the
    key's projects.toml repo; that call is posting identity, not grant scope, and
    for a registered repo it never falls back to the overlay (checked last)."""

    # Post-verdict tightening (operator D24): the read raises a BaseException
    # subclass, so a writer's `except Exception` cannot swallow it; a direct open
    # of the overlay file trips it too, not only the manifest_overlay entry
    # points; and the bare `gh issue close N` form (repo from the cwd checkout)
    # is exercised. Any read is also recorded, so a swallowed one still fails.
    CODE = (
        "import builtins, io, json, os, pathlib, manifest_overlay\n"
        "class OverlayRead(BaseException):\n"
        "    pass\n"
        "seen = []\n"
        "def boom(*a, **k):\n"
        "    seen.append('overlay read')\n"
        "    raise OverlayRead('overlay read')\n"
        "manifest_overlay.read = manifest_overlay.project_for_checkout = boom\n"
        "manifest_overlay.path_project = boom\n"
        # Post-verdict 2 (Codex): an alias (a symlink or hard link to the
        # overlay) is the same file under another name, so the file's identity
        # is matched too, not only the spelling of its path.
        "_OVERLAY = os.stat(os.path.join(os.getcwd(), '.claude', 'forge.toml'))\n"
        # Post-verdict 3 (Codex): identity is taken from the path AS GIVEN,
        # before any normalization: normpath drops `..` lexically, which names
        # a different file when the `..` follows a symlink.
        "_stat = os.stat\n"
        "def _is_overlay(p, dir_fd=None):\n"
        "    raw = os.fsdecode(p)\n"
        # harmonic-forge#1635 gap 1: a relative path opened against a
        # directory descriptor names a file there, not under the cwd, so its
        # identity is taken with the same descriptor.
        "    if dir_fd is not None and not os.path.isabs(raw):\n"
        "        try:\n"
        "            st = _stat(raw, dir_fd=dir_fd)\n"
        "        except OSError:\n"
        "            return False\n"
        "        return (st.st_dev, st.st_ino) == (_OVERLAY.st_dev, _OVERLAY.st_ino)\n"
        "    try:\n"
        "        st = _stat(raw)\n"
        "        if (st.st_dev, st.st_ino) == (_OVERLAY.st_dev, _OVERLAY.st_ino):\n"
        "            return True\n"
        "    except OSError:\n"
        "        pass\n"
        "    norm = os.path.normpath(os.path.abspath(raw))\n"
        "    return norm.replace(os.sep, '/').endswith('/.claude/forge.toml')\n"
        "_open, _path_open, _os_open = builtins.open, pathlib.Path.open, os.open\n"
        "def guarded_open(file, *a, **k):\n"
        "    if isinstance(file, (str, bytes, os.PathLike)) and _is_overlay(file):\n"
        "        boom()\n"
        "    return _open(file, *a, **k)\n"
        "def guarded_path_open(self, *a, **k):\n"
        "    if _is_overlay(self):\n"
        "        boom()\n"
        "    return _path_open(self, *a, **k)\n"
        # Post-verdict 3 (Codex): a read through os.open is a read too.
        "def guarded_os_open(path, flags, mode=0o777, *, dir_fd=None):\n"
        "    if isinstance(path, (str, bytes, os.PathLike)) and _is_overlay(path, dir_fd):\n"
        "        boom()\n"
        "    return _os_open(path, flags, mode, dir_fd=dir_fd)\n"
        "builtins.open = io.open = guarded_open\n"
        "pathlib.Path.open = guarded_path_open\n"
        "os.open = guarded_os_open\n"
        "sys.path.insert(0, " + repr(str(HERE.parent / "gh")) + ")\n"
        "import manifest_scope, imc_waiver, ui_grant, batch_auth, expand_lane_shorthand as m\n"
        "import manifest_identity\n"
        "out = [manifest_scope.home_project('acme/app') is None,\n"
        "       imc_waiver.is_home_repo('acme/app'),\n"
        "       imc_waiver.key_target('Z5'), imc_waiver.key_target('F5'),\n"
        "       m._scan_batch('BATCH Z5'),\n"
        "       batch_auth.closed_keys('gh issue close 5 --repo acme/app'),\n"
        "       batch_auth.closed_keys('gh issue close 5', os.getcwd()),\n"
        "       ui_grant.find_record([], 'Z5', 'plan')]\n"
        # Post-verdict 2 (Codex): the FORCE authorizer's own scope decision
        # runs too. Z5 is the overlay repo's key, so it refuses before any
        # grant is written.
        "out.append('no repo maps to its prefix' in m.authorize_force('FORCE Z5: probe', 's', '1'))\n"
        # Post-verdict 3 (Codex): a key that DOES map (F5, a projects.toml
        # repo) carries each hook authorizer past the prefix map into its
        # home-repo decision; state is a scratch dir, and no gh call is made.
        # harmonic-forge#1635 gap 3: PARK and SCAN make no home-repo decision,
        # so they are not here; FIXED_MAP below asserts what they do decide.
        "import tempfile, force_grant\n"
        "_st = pathlib.Path(tempfile.mkdtemp())\n"
        "force_grant.FORCE_STATE_DIR = _st / 'force'\n"
        "ui_grant.UI_STATE_DIR = _st / 'ui'\n"
        "imc_waiver.IMC_STATE_DIR = _st / 'imc'\n"
        "mapped = {\n"
        "  'force': m.authorize_force('FORCE F5: probe', 's', '1'),\n"
        "  'imc': m.authorize_imc('BATCH F5 IMC F5', 's', '1', label_reader=lambda r, n: ({'tooling-exception'}, '')),\n"
        "  'ui': m.authorize_ui('UI F5: probe', 's', '1'),\n"
        "}\n"
        # Each one decided past scope: FORCE stops only at its receipt check,
        # the other two write their (scratch) grant.
        "reached = {'force': 'has no preclose receipt', 'imc': 'waiver hook record written',\n"
        "           'ui': 'pending plan grant written'}\n"
        "out.append(sorted(k for k, v in mapped.items() if reached[k] in (v or '')))\n"
        "manifest_identity._probe_login = lambda *a, **k: 'acme'\n"
        "target = imc_waiver.key_target('F5')\n"
        "try:\n"
        "    manifest_identity.apply_project_identity(target[0])\n"
        "    out.append('identity-ok')\n"
        "except OverlayRead as exc:\n"
        "    out.append(str(exc))\n"
        "except BaseException:\n"
        "    out.append('identity-ok')\n"
        "out.append(seen)\n"
        "print(json.dumps(out))\n")

    EXPECTED = [True, False, None, ["acme/forge", 5], None, [], [], None,
                True, ["force", "imc", "ui"], "identity-ok", []]

    #: PARK and SCAN at a fixed `now` and in scratch state, so two runs' receipts
    #: compare equal. Each decides by a fixed map: PARK by `tasklist_owed.KEY_RE`,
    #: SCAN by `scan_grant.REPOS`. Z is the overlay repo's letter.
    FIXED_MAP = (
        "sys.path.insert(1, " + repr(str(HERE.parent / "gh")) + ")\n"
        "import datetime, pathlib, tempfile, park_grant, scan_grant, expand_lane_shorthand as m\n"
        "_st = pathlib.Path(tempfile.mkdtemp())\n"
        "park_grant.PARK_STATE_DIR = _st / 'park'\n"
        "scan_grant.SCAN_STATE_DIR = _st / 'scan'\n"
        "_now = datetime.datetime(2030, 1, 1, tzinfo=datetime.timezone.utc)\n"
        "fixed = {'park_z': m.authorize_park('PARK Z5: probe', 's', '1', _now),\n"
        "         'park_f': m.authorize_park('PARK F5: probe', 's', '1', _now),\n"
        "         'scan_z': m.authorize_scan('SCAN Z open-issues: probe', 's', _now),\n"
        "         'scan_f': m.authorize_scan('SCAN F open-issues: probe', 's', _now)}\n")

    def test_grant_scope_never_reads_the_overlay(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo, base = overlay_only_fixture(Path(tmp).resolve())
            out = json.loads(probe_from(repo, base, "import sys\n" + self.CODE))
        self.assertEqual(out, self.EXPECTED)

    def test_an_inherited_sdk_entrypoint_changes_nothing(self) -> None:
        """harmonic-forge#1635 gap 4: under `CLAUDE_CODE_ENTRYPOINT=sdk` every
        authorizer refuses at provenance, which would leave `reached` empty and
        exercise no scope decision; `probe_from` drops it (and `GH_REPO`)."""
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(os.environ, {"CLAUDE_CODE_ENTRYPOINT": "sdk",
                                             "GH_REPO": "other/thing"}):
            repo, base = overlay_only_fixture(Path(tmp).resolve())
            out = json.loads(probe_from(repo, base, "import sys\n" + self.CODE))
        self.assertEqual(out, self.EXPECTED)

    def test_park_and_scan_decide_by_a_fixed_map_not_the_overlay(self) -> None:
        """harmonic-forge#1635 gap 3: PARK refuses Z5 (no repo prefix matches)
        and grants F5; SCAN refuses Z by its letter map and grants F. Neither
        reads the overlay, and each receipt is the same with the overlay file
        removed, so the answer comes from the fixed map, not from the overlay."""
        guard = self.CODE.split("sys.path.insert(0, ")[0]
        tail = "print(json.dumps({'fixed': fixed, 'seen': seen}))\n"
        with tempfile.TemporaryDirectory() as tmp:
            repo, base = overlay_only_fixture(Path(tmp).resolve())
            present = json.loads(probe_from(repo, base, "import sys\n" + guard
                                            + self.FIXED_MAP + tail))
            (repo / ".claude" / "forge.toml").unlink()
            absent = json.loads(probe_from(repo, base, "import json, sys\n" + self.FIXED_MAP
                                           + "print(json.dumps({'fixed': fixed}))\n"))
        fixed = present["fixed"]
        self.assertEqual(present["seen"], [])
        self.assertIn("PARK REFUSED for Z5: no repo prefix matches it", fixed["park_z"])
        self.assertIn("may now be completed", fixed["park_f"])
        self.assertIn("SCAN REFUSED", fixed["scan_z"])
        self.assertIn("may be scanned", fixed["scan_f"])
        self.assertEqual(absent["fixed"], fixed)

    def test_an_alias_of_the_overlay_is_trapped(self) -> None:
        """The guard itself (post-verdict 2): a symlink and a hard link to the
        overlay are reads of it."""
        with tempfile.TemporaryDirectory() as tmp:
            repo, base = overlay_only_fixture(Path(tmp).resolve())
            overlay = repo / ".claude" / "forge.toml"
            (repo / "sym.toml").symlink_to(overlay)
            os.link(overlay, repo / "hard.toml")
            # Post-verdict 3 (Codex): `..` after a symlink names another file
            # than its lexical normalization; and a read through os.open.
            (repo / "deep" / "x").mkdir(parents=True)
            (repo / "jump").symlink_to(repo / "deep" / "x")
            (repo / "deep" / "forge.toml").symlink_to(overlay)
            guard = self.CODE.split("sys.path.insert(0, ")[0]
            code = ("import sys\n" + guard
                    + "for name in ('sym.toml', 'hard.toml', 'jump/../forge.toml'):\n"
                    + "    try:\n"
                    + "        open(name).close()\n"
                    + "    except OverlayRead:\n"
                    + "        pass\n"
                    + "try:\n"
                    + "    os.close(os.open('sym.toml', os.O_RDONLY))\n"
                    + "except OverlayRead:\n"
                    + "    pass\n"
                    # harmonic-forge#1635 gap 1: a relative name opened
                    # against a directory descriptor.
                    + "d = os.open('.claude', os.O_RDONLY)\n"
                    + "try:\n"
                    + "    os.close(os.open(b'forge.toml', os.O_RDONLY, dir_fd=d))\n"
                    + "except OverlayRead:\n"
                    + "    pass\n"
                    + "finally:\n"
                    + "    os.close(d)\n"
                    + "print(json.dumps(seen))\n")
            out = json.loads(probe_from(repo, base, code))
        self.assertEqual(out, ["overlay read"] * 5)


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
