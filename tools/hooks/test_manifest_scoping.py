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
import dataclasses
import inspect
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
import typing
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE.parent / "gh"), str(HERE.parent / "onboard")]

import batch_auth  # noqa: E402
import expand_lane_shorthand as els  # noqa: E402
import force_grant  # noqa: E402
import l1_post  # noqa: E402
import manifest_overlay  # noqa: E402
import report_red_main  # noqa: E402
import tasklist_owed  # noqa: E402
import ui_grant  # noqa: E402
from test_manifest_overlay import (  # noqa: E402
    FILLER, PROTOCOL, make_repo, overlay_only_fixture, overlay_text, probe_from, write_overlay)

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

#: harmonic-forge#1635 P1: the hostile-account checkout. The validator binds an
#: overlay-only account only to its own origin's owner
#: (`manifest_overlay._resolve`), so a checkout whose origin is `mallory/app`
#: may legally claim `account = "mallory"`. This V1 sets every field the
#: overlay validator accepts (pitch named change 1: board, milestones,
#: onboarded, min_version and protocol too, not only the identity keys) to a
#: non-default value that matches nothing in the base and nothing the acme
#: checkout's overlays hold, which is what P7(a) checks.
MALLORY_URL = "https://github.com/mallory/app.git"
MALLORY_OVERLAY = ('[project]\nname = "mapp"\nprefix = "M"\nrepo = "mallory/app"\n'
                   'account = "mallory"\nworkspace = "mallory"\nboard_owner = "mallory"\n'
                   'board_number = "77"\nmilestones = true\nactive_milestones = ["mallory-next"]\n'
                   'onboarded = true\nmin_version = "v2031.01.7"\n' + PROTOCOL)
#: harmonic-forge#1635 R1 (operator D89): every optional `Protocol` field, set in
#: the mallory V1's `[project.protocol]` to a value `load_protocol` accepts. The
#: task names are "mallory", an offered login, so an author login read from one
#: (survivor 2's `ci_parity_task`) flips a `find_record` answer. Dict-valued
#: fields are opaque axes: their sub-keys are not recursed.
MALLORY_PROTOCOL = ('needs_gate_adapter = false\nci_parity_task = "mallory"\n'
                    'lane3_env_task = "mallory"\nlane3_preflight_task = "mallory"\n'
                    'rebase_regenerate = { "mallory" = "mallory" }\n'
                    'rebase_provision_task = "mallory"\n'
                    'rebase_visual_baselines = { glob = "mallory/*.png", workflow = "mallory.yml", '
                    'fetch_task = "mallory {run_id}" }\n')
MALLORY_OVERLAY += MALLORY_PROTOCOL
#: V2 for the mallory checkout: the raw widening overlay with account mallory.
MALLORY_WIDENING = WIDENING_OVERLAY.replace('account = "acme"', 'account = "mallory"')


def snapshot(root) -> dict[str, str]:
    """Every file under `root` by relative path, as the hex of its raw bytes
    (harmonic-forge#1635 P6: a `decode('utf-8', 'replace')` made two files that
    differ only in invalid UTF-8 compare equal). The probe runs this function's
    own source, so the unit test and the probe compare the same way."""
    from pathlib import Path as _Path
    top = _Path(root)
    return {str(p.relative_to(top)): p.read_bytes().hex()
            for p in sorted(top.rglob("*")) if p.is_file()}


class SnapshotComparesRawBytes(unittest.TestCase):
    """harmonic-forge#1635 P6 (kill KP6)."""

    def test_files_differing_only_in_invalid_utf8_compare_unequal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            left, right = Path(tmp) / "a", Path(tmp) / "b"
            for side, data in ((left, b"x\xff"), (right, b"x\xfe")):
                side.mkdir()
                (side / "grant.json").write_bytes(data)
            self.assertNotEqual(snapshot(left), snapshot(right))


class OverlayContentNeverChangesGrantScope(unittest.TestCase):
    """harmonic-forge#1635 reforge (sticky-wicket REFORGE, operator D52/D53;
    epoch-2 PATCH P1-P7, operator D58; re-scope R1 and P7(c), operator D89).

    The claim, exactly: over the axes derived from production `Project` and
    `Protocol`, overlay content does not change a decision this program
    drives. It is a good-faith regression tripwire (operator D88), not a proof
    that no branch, mode, LANE value or date anywhere reads the overlay:
    hook-process completeness is harmonic-forge#1663's production refusal.
    The property is noninterference, checked by behavior: one
    decision program runs from inside each of two overlay-only checkouts, with
    the overlay absent (V0), validator-accepted (V1) and raw (V2). Within a
    checkout, every driven decision, every receipt, the IMC label reader's
    arguments and the raw bytes of every grant or receipt file written must be
    identical across the three; each checkout's V0 is its own baseline and
    must equal EXPECTED. The check does not depend on how a read is spelled: a
    read through any spelling that changes a driven decision changes the output.

    Checkouts. `acme`: origin acme/app, V1 the fixture overlay (Z, acme/app,
    account acme), V2 WIDENING_OVERLAY. `mallory` (P1): origin mallory/app, V1
    MALLORY_OVERLAY (M, mallory/app, account mallory, workspace mallory), V2
    MALLORY_WIDENING. The acme V1 is the variant that catches reads through the
    real manifest_overlay API (KD1, KD3, KD5, KD6); its account equals the
    base's, so an account-keyed read is caught only by the mallory V1 (KP1).
    The V2s catch raw reads (KD2, KD4). KD7 reads no overlay: it fails the
    `reached` anchor on V0, not the comparison.

    Validator status (P3): the program records
    `manifest_overlay.project_for_checkout(cwd)` outside the compared output,
    and each variant is asserted: V0 None, V1 its repo, V2 a refusal. A V1
    the validator silently refuses would otherwise degrade into a copy of V0.

    Driven, for every login in LOGINS, key in KEYS and repo in REPOS:
    `manifest_scope.home_project`, `imc_waiver.is_home_repo`, `key_target`,
    `_scan_batch`, `closed_keys` (`--repo` and the bare form, whose resolved
    checkout `cwd_repo` is pinned per checkout), `ui_grant.find_record`,
    `batch_auth._slot_env` (the hook_identity -> manifest_identity credential
    path), and the BATCH, FORCE (plain and reforge), IMC, UI, PARK and SCAN
    authorizers. The IMC label reader is a stub that records the `(repo,
    number)` it is asked about (P4), compared across variants and pinned.
    `apply_project_identity('acme/forge')` is driven once per login (posting
    identity on a projects.toml repo, D23). FORCE reaches its grant write (P2):
    the probe's scratch `HOME` holds a preclose receipt for acme/forge#5, read
    by the real `covered_head` and `reviewed_match`. State is scratch and `now`
    is fixed. `probe_from` builds the child's environment from an allowlist
    (P5), so no `HOME`, `GIT_*`, `GH_ACCT_HOME` or `CLAUDE_*` reaches it.

    Each authorizer writes under its own state directory, named for its
    `grants` entry: plain FORCE under `force/` and reforge FORCE under
    `force_reforge/` (pitch named change 3). `force_grant.grant_path` is
    `<KEY>.json` for every mode and `write_pending` replaces it, so with one
    directory the reforge grant overwrote the plain one before the snapshot
    and the plain grant's bytes were never compared (kill KP2b).

    `reached` (in EXPECTED) names every authorizer whose F5 receipt says it
    wrote, and P7(c) requires a distinct terminal grant file in EXPECTED_FILES
    for each driven declaration, so `reached` means "wrote", not "got past home".

    Completeness (P7). What is derived and what is a hand mapping, stated:
    (a) Derived. The axis set is every field the overlay validator accepts:
    production `Project`'s fields minus `MACHINE_LOCAL_KEYS`, `INSTANCE_KEYS`
    and `OPERATOR_KEYS` (`manifest_overlay._validated`). A dataclass-typed field
    is recursed (R1): `protocol` is one axis per `Protocol` field, and the
    mallory V1 sets every optional one (MALLORY_PROTOCOL). Dict-valued fields
    are opaque axes. Each axis is set by some accepted variant to a
    non-default value that matches nothing in the base and nothing the other
    checkout's accepted variants hold, or is exempted by name in AXIS_EXEMPT.
    A new `Project` or `Protocol` field fails this suite until a variant covers
    it (kills KP7a, KR1b). (b) Hand mapping, checked against (a): every axis
    maps to an offered input kind in OFFERED (its injected values must be among
    LOGINS, KEYS or REPOS) or sits, itself or its parent field, in
    NO_DRIVEN_DECISION with a reason (kill KP7d). (c) The declarations are
    derived: every `authorize_*` function of expand_lane_shorthand, crossed with
    `force_grant._AUTHORIZES` for FORCE and `ui_grant.MODES` for UI. Their
    split into DRIVEN (each with its `grants` entry and one terminal file) and
    NOT_DRIVEN (each with its reason) is a hand mapping that the check holds
    equal to that set; a new mode or authorizer fails, named (kill KP7e).
    `_force_mode`, the hook's hand-coded mode grammar, must parse each
    `_AUTHORIZES` key back to itself.

    Stated residuals (the epoch-2 verdict's, accepted, not hidden):
    1. Inputs not driven stay uncovered, each named: LANE-gated branches
       (`manifest_slots.lane_slot`, survivor 1); the NOT_DRIVEN FORCE and UI
       modes (survivor 3); a date past the pinned `now`; batch_auth's
       merge-side `decide`, `consume` and `_derived_merge_match`; a read of a
       varied axis used as an input kind its injected value is not offered as
       (a repo taken from `ci_parity_task`); a FORCE mode `_force_mode`
       accepts that `_AUTHORIZES` does not list; and any read gated on a
       trigger this program never sends. In the grant-writing hook process,
       harmonic-forge#1663's refusal will close them; it is filed, not built.
    2. The real `gh` label reader is not exercised; only the repo it is asked
       about is observed (P4).
    3. Closed (pitch named change 1): `board_owner`, `board_number` and every
       other accepted non-identity field are now axes the mallory V1 varies.
    4. A hostile author of production code is review's to stop; this suite is
       a tripwire for plausible regressions (threat model good-faith, D88).
    5. A read that cannot change a decision is allowed by design: the property
       is noninterference, not "never opened".

    `seen` is a diagnostic only, and not complete: it records calls to the
    `manifest_overlay` entry points (passed through, never raised) so a failure
    message can name the read. Reads by any other spelling do not appear in it;
    the equality is the assertion."""

    STATE = "{STATE}"
    HOME = "{HOME}"
    #: The inputs offered to each kind of decision (P7(b) checks the variants
    #: against them): logins for find_record and identity, keys for key_target
    #: and the authorizers, repos for home_project/is_home_repo/closed_keys.
    LOGINS = ("acme", "mallory")
    KEYS = ("Z5", "M5", "F5")
    REPOS = ("acme/app", "mallory/app", "acme/forge")
    #: P7(b): each axis whose values a driven decision is offered as input, and
    #: the inputs of that kind.
    OFFERED = {"account": set(LOGINS), "prefix": {k[0] for k in KEYS}, "repo": set(REPOS)}
    #: P7(b): the axes no driven decision takes as input, each with its reason.
    #: They are still varied (P7(a)), so a read of one that changes a driven
    #: decision still changes the compared output.
    NO_DRIVEN_DECISION = {
        "name": "no driven decision takes a project name; a key's prefix, not its name, "
                "selects the repo",
        "workspace": "no driven decision takes a workspace; `_slot_env` selects its slot "
                     "from a repo in REPOS and its result is compared",
        "board_owner": "the board is read by the lane board tools, which take no grant decision",
        "board_number": "the board is read by the lane board tools, which take no grant decision",
        "milestones": "milestone filtering is the lane queue's; no authorizer takes a milestone",
        "active_milestones": "milestone filtering is the lane queue's; no authorizer takes a "
                             "milestone",
        "onboarded": "it gates belt workspaces and slot listing, not a driven decision's input",
        "min_version": "only the doctor reads it",
        # R1: covers every `protocol.<field>` axis. Survivor 2 falsified the old
        # reason ("no authorizer takes a task name"): a mutant can. What holds is
        # narrower, and stated as such.
        "protocol": "no driven decision takes a protocol value as input today; every "
                    "sub-field is still varied (R1), so a read of one that changes a "
                    "driven decision changes the compared output",
    }
    #: P7(c): each production grant-writing declaration (an `authorize_*` function
    #: of expand_lane_shorthand, crossed with its mode set where production
    #: declares one) that the probe drives, and the `grants` entry that drives it.
    DRIVEN = {("batch", None): "batch", ("force", "plain"): "force",
              ("force", "reforge"): "force_reforge", ("imc", None): "imc",
              ("park", None): "park", ("scan", None): "scan", ("ui", "plan"): "ui"}
    #: P7(c): the declarations the probe does not drive, each with its reason.
    NOT_DRIVEN = {
        ("force", mode): "residual 1: not driven (operator D89 built no R2 mode matrix); "
                         "harmonic-forge#1663's refusal will cover the hook process"
        for mode in ("route", "cluster", "gate", "ci-outage")
    } | {("ui", "merge"): "residual 1: not driven (operator D89); harmonic-forge#1663's "
                          "refusal will cover the hook process"}
    #: P7(a): accepted fields no variant varies, each with its reason. Empty:
    #: the mallory V1 varies every one.
    AXIS_EXEMPT: dict[str, str] = {}
    #: The preclose receipt's pass head for acme/forge#5, and the unreviewed
    #: head a reforge line binds.
    HEAD = "c" * 40
    REFORGE = "d" * 12

    #: The overlay variants per checkout; V0 (absent) is implicit.
    VARIANTS = {
        "acme": {"V1": overlay_text(), "V2": WIDENING_OVERLAY},
        "mallory": {"V1": MALLORY_OVERLAY, "V2": MALLORY_WIDENING},
    }
    #: The repo each checkout's origin names: the bare close resolves it.
    CHECKOUT_REPO = {"acme": "acme/app", "mallory": "mallory/app"}

    CODE = (
        "import datetime, json, os, pathlib, sys\n"
        "sys.path.insert(0, " + repr(str(HERE.parent / "gh")) + ")\n"
        "import manifest_overlay\n"
        "try:\n"
        "    validator = getattr(manifest_overlay.project_for_checkout(pathlib.Path.cwd()),\n"
        "                        'repo', None)\n"
        "except Exception as exc:\n"
        "    validator = type(exc).__name__\n"
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
        "label_calls = []\n"
        "def _labels(repo, number):\n"
        "    label_calls.append([repo, number])\n"
        "    return {'tooling-exception'}, ''\n"
        "out = {\n"
        "  'home_project': [getattr(manifest_scope.home_project(r), 'repo', None) for r in REPOS],\n"
        "  'is_home_repo': [imc_waiver.is_home_repo(r) for r in REPOS],\n"
        "  'key_target': [imc_waiver.key_target(k) for k in KEYS],\n"
        "  'scan_batch': [m._scan_batch('BATCH ' + k) for k in KEYS],\n"
        "  'closed_keys': [batch_auth.closed_keys('gh issue close 5 --repo ' + r) for r in REPOS]\n"
        "                 + [batch_auth.closed_keys('gh issue close 5', os.getcwd())],\n"
        "  'cwd_repo': batch_auth.cwd_repo(os.getcwd()),\n"
        "  'find_record': [[bool(ui_grant.find_record([{'login': who, 'body': _body(k)}], k, 'plan'))\n"
        "                   for who in LOGINS] for k in KEYS],\n"
        "  'slot_env': [batch_auth._slot_env(r) for r in REPOS],\n"
        "}\n"
        "out['grants'] = {}\n"
        "out['grants']['batch'] = {k: m.authorize_batch('BATCH ' + k,\n"
        "    state_path=_st / 'batch' / (k + '.json')) for k in KEYS}\n"
        "out['grants']['force'] = {k: m.authorize_force('FORCE ' + k + ': probe', 's', '1', _now)\n"
        "                          for k in KEYS}\n"
        "force_grant.FORCE_STATE_DIR = _st / 'force_reforge'  # its own file: change 3\n"
        "out['grants']['force_reforge'] = {k: m.authorize_force(\n"
        "    'FORCE ' + k + ' reforge ' + REFORGE + ': probe', 's', '1', _now) for k in KEYS}\n"
        "out['grants']['imc'] = {k: m.authorize_imc('BATCH ' + k + ' IMC ' + k, 's', '1',\n"
        "    label_reader=_labels, now=_now) for k in KEYS}\n"
        "out['grants']['ui'] = {k: m.authorize_ui('UI ' + k + ': probe', 's', '1', _now)\n"
        "                       for k in KEYS}\n"
        "out['grants']['park'] = {k: m.authorize_park('PARK ' + k + ': probe', 's', '1', _now)\n"
        "                         for k in KEYS}\n"
        "out['grants']['scan'] = {k: m.authorize_scan('SCAN ' + k[0] + ' open-issues: probe', 's',\n"
        "                                             _now) for k in KEYS}\n"
        "out['label_calls'] = label_calls\n"
        "reached = {'batch': 'authorized F5',\n"
        "           'force': 'pending plain grant written for F5 at ' + HEAD[:12],\n"
        "           'force_reforge': 'pending reforge grant written for F5 at ' + REFORGE,\n"
        "           'imc': 'waiver hook record written for F5',\n"
        "           'ui': 'pending plan grant written', 'park': 'may now be completed',\n"
        "           'scan': 'may be scanned'}\n"
        "out['reached'] = sorted(a for a, text in reached.items()\n"
        "                        if text in (out['grants'][a]['F5'] or ''))\n"
        "identity = {}\n"
        "for who in LOGINS:\n"
        "    manifest_identity._VERIFIED.clear()\n"
        "    manifest_identity._probe_login = lambda *a, _who=who, **k: _who\n"
        "    try:\n"
        "        identity[who] = manifest_identity.apply_project_identity('acme/forge')\n"
        "    except BaseException as exc:\n"
        "        identity[who] = type(exc).__name__\n"
        "out['identity'] = identity\n"
        + inspect.getsource(snapshot) +
        "out['files'] = snapshot(_st)\n"
        "print(json.dumps({'out': out, 'seen': seen, 'validator': validator}))\n")

    #: Each checkout's V0 output, with the scratch paths as `{STATE}` and
    #: `{HOME}`. The grant and receipt bytes are pinned by the cross-variant
    #: equality, and their names by EXPECTED_FILES.
    EXPECTED = {
        "home_project": [None, None, "acme/forge"],
        "is_home_repo": [False, False, True],
        "key_target": [None, None, ["acme/forge", 5]],
        "scan_batch": [None, None, [0, ["F5"]]],
        "closed_keys": [[], [], [["F5", "acme/forge", 5]], []],
        "find_record": [[False, False], [False, False], [True, False]],
        "label_calls": [["acme/forge", 5]],
        "reached": ["batch", "force", "force_reforge", "imc", "park", "scan", "ui"],
        "identity": {"acme": "acme", "mallory": "SystemExit"},
    }

    #: The scratch files V0 writes: the F5 grants and their locks, nothing for Z5 or M5.
    EXPECTED_FILES = ["batch/F5.json", "batch/F5.json.lock", "force/F5.json", "force/F5.json.lock",
                      "force_reforge/F5.json", "force_reforge/F5.json.lock", "imc/F5.json", "park/s/F5.json", "park/s/F5.json.lock",
                      "scan/s/F-9037c5488f26c3b7.json", "scan/s/F-9037c5488f26c3b7.json.lock",
                      "ui/F5.json", "ui/F5.json.lock"]
    #: The credential slot `_slot_env` selects per repo in REPOS: only the home repo has one.
    SLOT_DIRS = [None, None, "{HOME}/gh-slots/acme"]

    def run_variants(self) -> dict[str, dict[str, dict]]:
        """`{checkout: {variant: output}}`, each run from fresh scratch state and a
        fresh scratch `HOME` (holding the acme/forge#5 preclose receipt) at the
        same paths."""
        outputs: dict[str, dict[str, dict]] = {}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            acme, base = overlay_only_fixture(root)
            checkouts = {"acme": acme, "mallory": make_repo(root / "mallory-app", MALLORY_URL)}
            state, home = root / "state", root / "home"
            code = ("import sys\n"
                    f"STATE = {str(state)!r}\nLOGINS, KEYS, REPOS = "
                    f"{self.LOGINS!r}, {self.KEYS!r}, {self.REPOS!r}\n"
                    f"HEAD, REFORGE = {self.HEAD!r}, {self.REFORGE!r}\n" + self.CODE)
            for checkout, repo in checkouts.items():
                overlay = repo / ".claude" / "forge.toml"
                outputs[checkout] = {}
                for name, text in (("V0", None), *self.VARIANTS[checkout].items()):
                    for scratch in (state, home):
                        shutil.rmtree(scratch, ignore_errors=True)
                        scratch.mkdir()
                    self.write_receipt(home)
                    if text is None:
                        overlay.unlink(missing_ok=True)
                    else:
                        write_overlay(repo, text)
                    raw = probe_from(repo, base, code, home=home).strip().splitlines()[-1]
                    raw = raw.replace(str(state), self.STATE).replace(str(home), self.HOME)
                    outputs[checkout][name] = json.loads(raw)
        return outputs

    def write_receipt(self, home: Path) -> None:
        """A completed one-pass preclose receipt for acme/forge#5 at HEAD, where
        `preclose_check.receipt_path` looks under the probe's `HOME` (P2)."""
        directory = home / ".claude" / "state" / "preclose"
        directory.mkdir(parents=True)
        (directory / "acme_forge_5.json").write_text(json.dumps({
            "repo": "acme/forge", "issue": 5, "reviewed_sha": self.HEAD, "refuters": 2,
            "status": "complete", "pass_history": [{"sha": self.HEAD, "surviving": 0}]}),
            encoding="utf-8")

    def accepted(self, outputs: dict[str, dict[str, dict]]) -> dict[str, list[dict]]:
        """Per checkout, the `[project]` table of every variant the validator accepted
        in the probe (P3's recorded status names the variant's own repo)."""
        found: dict[str, list[dict]] = {}
        for checkout, variants in self.VARIANTS.items():
            found[checkout] = []
            for name, text in variants.items():
                project = tomllib.loads(text)["project"]
                if outputs[checkout][name]["validator"] == project.get("repo"):
                    found[checkout].append(project)
        return found

    @staticmethod
    def overlay_axes() -> list[str]:
        """Every field the overlay validator accepts (pitch named change 1):
        production `Project`'s fields minus the refused MACHINE_LOCAL_KEYS,
        INSTANCE_KEYS and OPERATOR_KEYS (`manifest_overlay._validated`). R1
        (operator D89): a field whose type is a dataclass, or Optional of one,
        is not one opaque axis but one dotted axis per entry of that dataclass's
        `__dataclass_fields__` (`protocol.<field>`, the fields `load_protocol`
        reads). Types are resolved at the `Project` level only. Imported, never
        copied."""
        refused = {*manifest_overlay.MACHINE_LOCAL_KEYS, *manifest_overlay.INSTANCE_KEYS,
                   *manifest_overlay.OPERATOR_KEYS}
        hints = typing.get_type_hints(manifest_overlay.Project)
        axes = []
        for name in set(manifest_overlay.Project.__dataclass_fields__) - refused:
            nested = OverlayContentNeverChangesGrantScope.nested_dataclass(hints.get(name))
            if nested is None:
                axes.append(name)
            else:
                axes += [f"{name}.{sub}" for sub in nested.__dataclass_fields__]
        return sorted(axes)

    @staticmethod
    def nested_dataclass(hint: object) -> type | None:
        """The dataclass `hint` names, alone or in an Optional/union; else None."""
        for candidate in (hint, *typing.get_args(hint)):
            if isinstance(candidate, type) and dataclasses.is_dataclass(candidate):
                return candidate
        return None

    @staticmethod
    def dig(table: object, axis: str) -> object:
        """The value at a dotted `axis` in a parsed `[project]` table, or None."""
        for part in axis.split("."):
            if not isinstance(table, dict):
                return None
            table = table.get(part)
        return table

    @classmethod
    def axis_defaults(cls) -> dict[str, object]:
        """Each axis's production default, recursed as the axes are (pitch named
        change 5): a nested field's default is its own dataclass field's."""
        hints = typing.get_type_hints(manifest_overlay.Project)
        found: dict[str, object] = {}
        for name, field in manifest_overlay.Project.__dataclass_fields__.items():
            found[name] = field.default
            nested = cls.nested_dataclass(hints.get(name))
            for sub, inner in (nested.__dataclass_fields__.items() if nested else ()):
                found[f"{name}.{sub}"] = inner.default
        return found

    def uncovered_axes(self, accepted: dict[str, list[dict]]) -> list[str]:
        """P7(a): the axes no validator-accepted variant sets to a non-default
        value that neither the base nor the other checkout's accepted variants
        hold, less AXIS_EXEMPT."""
        base = tomllib.loads(FILLER)["project"]
        defaults = self.axis_defaults()

        def norm(value: object) -> str:
            return json.dumps(value, sort_keys=True, default=str).casefold()

        uncovered = []
        for key in self.overlay_axes():
            if key in self.AXIS_EXEMPT:
                continue
            held = {norm(self.dig(e, key)) for e in base if self.dig(e, key) is not None}
            held.add(norm(defaults.get(key)))
            if key == "active_milestones":
                held.add(norm([]))  # the default () as TOML would spell it

            def fresh(checkout: str, value: object, key: str = key, held: set = held) -> bool:
                others = {norm(self.dig(o, key)) for other, projects in accepted.items()
                          if other != checkout for o in projects
                          if self.dig(o, key) is not None}
                return value is not None and norm(value) not in held | others

            if not any(fresh(c, self.dig(p, key)) for c, projects in accepted.items()
                       for p in projects):
                uncovered.append(key)
        return uncovered

    def no_driven_reason(self, axis: str) -> str | None:
        """P7(b): the NO_DRIVEN_DECISION reason for `axis`, or for its parent field."""
        return self.NO_DRIVEN_DECISION.get(axis) or self.NO_DRIVEN_DECISION.get(
            axis.split(".", 1)[0] if "." in axis else "")

    @staticmethod
    def grant_declarations() -> list[tuple[str, str | None]]:
        """P7(c), from production, never from the probe's `grants` keys: every
        `authorize_*` function of expand_lane_shorthand, crossed with the mode set
        production declares for it (FORCE: `force_grant._AUTHORIZES`; UI:
        `ui_grant.MODES`), or `None` where it declares none."""
        modes = {"force": tuple(force_grant._AUTHORIZES), "ui": tuple(ui_grant.MODES)}
        names = sorted(name.removeprefix("authorize_") for name, fn in vars(els).items()
                       if name.startswith("authorize_") and inspect.isfunction(fn)
                       and fn.__module__ == els.__name__)
        return [(name, mode) for name in names for mode in modes.get(name, (None,))]

    def declaration_gaps(self, grants: object) -> list[str]:
        """P7(c): every production declaration is DRIVEN or NOT_DRIVEN (never both),
        every driver is an entry of the probe's `grants`, and every `grants` entry
        drives some declaration. Empty when complete."""
        declared = set(self.grant_declarations())
        gaps = [f"{d}: neither driven nor named in NOT_DRIVEN"
                for d in sorted(declared - set(self.DRIVEN) - set(self.NOT_DRIVEN), key=str)]
        gaps += [f"{d}: in both DRIVEN and NOT_DRIVEN"
                 for d in sorted(set(self.DRIVEN) & set(self.NOT_DRIVEN), key=str)]
        gaps += [f"{d}: not a production declaration"
                 for d in sorted((set(self.DRIVEN) | set(self.NOT_DRIVEN)) - declared, key=str)]
        gaps += [f"grants entry {g!r} drives no declaration"
                 for g in sorted(set(grants) - set(self.DRIVEN.values()))]
        gaps += [f"{d}: its driver {g!r} is not in the probe's grants"
                 for d, g in sorted(self.DRIVEN.items(), key=str) if g not in grants]
        return gaps

    @staticmethod
    def force_parse_gaps() -> list[str]:
        """P7(c), pitch named change 3: `_force_mode` (a hand-coded chain, the
        hook's real mode grammar) parses each `_AUTHORIZES` key back to itself.
        One direction only: a mode the parser accepts that `_AUTHORIZES` does not
        list is a stated residual."""
        args = ([], ["c" * 40], ["c" * 12])
        gaps = []
        for mode in force_grant._AUTHORIZES:
            tokens = [] if mode == "plain" else [mode]
            parsed = [els._force_mode(tokens + arg) for arg in args]
            if not any(isinstance(p, tuple) and p[0] == mode for p in parsed):
                gaps.append(mode)
        return gaps

    def assert_complete(self, outputs: dict[str, dict[str, dict]]) -> None:
        """P7: the variants vary every field an overlay may set (nested ones
        included, R1), every varied field reaches an offered input or is named as
        reaching none, and every production grant declaration is driven, with its
        own terminal file, or named as undriven."""
        accepted = self.accepted(outputs)
        axes = self.overlay_axes()
        uncovered = self.uncovered_axes(accepted)
        self.assertEqual(uncovered, [], (
            "P7(a): no validator-accepted variant sets these overlay fields to a non-default "
            "value that neither the base nor the other checkout holds, and none is exempted "
            f"by name in AXIS_EXEMPT, so no driven decision can see them vary: {uncovered}"))
        self.assertEqual(sorted(set(self.AXIS_EXEMPT) - set(axes)), [],
                         "P7(a): AXIS_EXEMPT names a field the validator does not accept")
        unmapped = [k for k in axes if k not in self.OFFERED and not self.no_driven_reason(k)]
        self.assertEqual(unmapped, [], (
            "P7(b): these varied fields map to no offered input kind (OFFERED) and are not "
            f"named in NO_DRIVEN_DECISION with a reason: {unmapped}"))
        parents = set(axes) | {k.split(".", 1)[0] for k in axes}
        both = sorted(set(self.OFFERED) & set(self.NO_DRIVEN_DECISION))
        stale = sorted((set(self.OFFERED) | set(self.NO_DRIVEN_DECISION)) - parents)
        self.assertEqual((both, stale), ([], []),
                         "P7(b): a field both offered and unreached, or not an overlay field")
        missing = [(checkout, key, project[key]) for checkout, projects in accepted.items()
                   for project in projects for key, values in self.OFFERED.items()
                   if key in project and project[key] not in values]
        self.assertEqual(missing, [], "P7(b): an injected value is offered to no driven decision")
        grants = outputs["acme"]["V0"]["out"]["grants"]
        self.assertEqual(self.declaration_gaps(grants), [], "P7(c): production grant declarations")
        self.assertEqual(self.force_parse_gaps(), [],
                         "P7(c): `_force_mode` does not parse these `_AUTHORIZES` modes")
        claimed: dict[str, str] = {}
        for declaration, authorizer in sorted(self.DRIVEN.items(), key=str):
            terminal = [f for f in self.EXPECTED_FILES if f.startswith(authorizer + "/")
                        and not f.endswith(".lock")
                        and f.rsplit("/", 1)[-1].startswith(("F5.", "F-"))]
            self.assertEqual(len(terminal), 1,
                             f"P7(c): {declaration} (driven as {authorizer}) needs exactly one F5 "
                             f"grant file of its own in EXPECTED_FILES, found {terminal}")
            self.assertNotIn(terminal[0], claimed, f"P7(c): {authorizer} shares {terminal[0]}")
            claimed[terminal[0]] = authorizer

    def assert_noninterference(self, outputs: dict[str, dict[str, dict]]) -> None:
        self.assert_complete(outputs)
        for checkout, variants in outputs.items():
            want = {"V0": None, "V1": self.CHECKOUT_REPO[checkout], "V2": "ManifestError"}
            self.assertEqual({name: out["validator"] for name, out in variants.items()}, want,
                             f"P3: {checkout} validator status per variant")
            v0 = variants["V0"]["out"]
            for name in self.VARIANTS[checkout]:
                with self.subTest(checkout=checkout, variant=name):
                    self.assertEqual(variants[name]["out"], v0,
                                     f"{checkout} {name} differs from V0; manifest_overlay entry "
                                     f"points called (diagnostic only): {variants[name]['seen']}")
            for field, value in self.EXPECTED.items():
                self.assertEqual(v0[field], value, f"{checkout} {field}")
            self.assertEqual(v0["cwd_repo"], self.CHECKOUT_REPO[checkout], "bare close's checkout")
            self.assertEqual([(env or {}).get("GH_CONFIG_DIR") for env in v0["slot_env"]],
                             self.SLOT_DIRS, f"{checkout} slot_env")
            grants = v0["grants"]
            for key in ("Z5", "M5"):
                self.assertEqual(grants["batch"][key], "")
                self.assertIn(f"PARK REFUSED for {key}: no repo prefix matches it", grants["park"][key])
                self.assertIn("no repo maps to its prefix", grants["force"][key])
                self.assertIn("no repo maps to its prefix", grants["ui"][key])
                self.assertEqual(grants["scan"][key],
                                 f"SCAN REFUSED: no repo prefix matches '{key[0]}'. No grant written.")
            self.assertEqual(sorted(v0["files"]), self.EXPECTED_FILES)

    def test_overlay_content_changes_no_grant_decision(self) -> None:
        self.assert_noninterference(self.run_variants())

    def test_an_inherited_sdk_entrypoint_changes_nothing(self) -> None:
        """harmonic-forge#1635 gap 4: under `CLAUDE_CODE_ENTRYPOINT=sdk` every
        authorizer refuses at provenance, which would leave `reached` empty and
        exercise no scope decision; `probe_from`'s allowlist never passes it."""
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_ENTRYPOINT": "sdk"}):
            self.assert_noninterference(self.run_variants())

    def v1_tables(self) -> dict[str, list[dict]]:
        """Each checkout's V1 `[project]` table, as P3 asserts the probe accepts it."""
        return {c: [tomllib.loads(v["V1"])["project"]] for c, v in self.VARIANTS.items()}

    def test_r1_p7a_names_a_new_optional_protocol_field_no_variant_sets(self) -> None:
        """harmonic-forge#1635 R1 (operator D89): `Protocol` is recursed, not one
        opaque axis, so a new optional `Protocol` field no variant sets is named."""
        self.assertIn("protocol.ci_parity_task", self.overlay_axes())
        self.assertNotIn("protocol", self.overlay_axes())
        self.assertEqual(self.uncovered_axes(self.v1_tables()), [])
        new = dataclasses.field(default=None)
        new.name, new.type, new._field_type = "review_owner", "str | None", dataclasses._FIELD
        protocol =self.nested_dataclass(typing.get_type_hints(manifest_overlay.Project)["protocol"])
        with mock.patch.dict(protocol.__dataclass_fields__, {"review_owner": new}):
            self.assertEqual(self.uncovered_axes(self.v1_tables()), ["protocol.review_owner"])

    def test_p7c_names_a_production_mode_no_driver_covers(self) -> None:
        """harmonic-forge#1635 P7(c) (operator D89): the declarations come from
        production, so a FORCE mode added there with no driver and no reason is
        named, and `_force_mode` must parse every listed mode back to itself."""
        grants = sorted(set(self.DRIVEN.values()))
        self.assertEqual(self.declaration_gaps(grants), [])
        self.assertEqual(self.force_parse_gaps(), [])
        with mock.patch.dict(force_grant._AUTHORIZES, {"review": "probe"}):
            self.assertEqual(self.declaration_gaps(grants),
                             ["('force', 'review'): neither driven nor named in NOT_DRIVEN"])
            self.assertEqual(self.force_parse_gaps(), ["review"])


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
