#!/usr/bin/env python3
"""Tests for `manifest_overlay.py`: an app repo's `.claude/forge.toml`, reached
only through the per-checkout resolver (harmonic-forge#1527 merge-point reforge).

Every case uses scratch `git init` repos and a scratch base manifest passed as
`manifest=`/`path=` or through `FORGE_PROJECTS_MANIFEST`, with a scratch cwd
restored on cleanup. None reads the live manifest or the repo's own cwd. The
case numbers are the handoff's TC numbers.

The overlay has three consumers (sticky-wicket R1; the third, `manifest_invoking`, is
harmonic-forge#1579's): `account_for_origin(checkout=)` and `ci_plan`, which `consumer`
below drives. Every other path-keyed reader returns its origin/main answer.
`overlay_only_fixture` and `probe_from` are imported by the BATCH tests (TC17)
and the gate-equivalence tests in `tools/hooks/test_overlay_never_reaches_a_gate.py`.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "lane"))

import manifest as mf  # noqa: E402
import manifest_identity as mi  # noqa: E402
import ci_plan  # noqa: E402
import manifest_overlay as mo  # noqa: E402
import manifest_validate as mv  # noqa: E402
import manifest_git as mg  # noqa: E402
from manifest_protocol import ManifestError  # noqa: E402

PROTOCOL = """\
[project.protocol]
worktree_name = "{checkout}-lane{lane}"
l1_post_task = "l1-post"
lane_comment_task = "lane-comment"
gate_checkout_task = "gate-checkout"
lane3_begin_task = "lane3-begin"
lane3_end_task = "lane3-end"
runs_lane3 = true
gate_task = "check"
"""

#: The base every case starts from: name "forge", prefix "F" (TC20), plus the
#: kenekted-ai entry whose slot AC6 compares (TC15).
FILLER = """\
[[project]]
name = "forge"
prefix = "F"
repo = "acme/forge"
account = "acme"
path = "/nonexistent/forge"

[[project]]
name = "kenekted-ai"
prefix = "Y"
repo = "kenekted/kenekted-ai"
account = "harmonicarchitect"
path = "/nonexistent/kenekted-ai"
"""

APP_URL = "https://github.com/acme/app.git"


def overlay_text(name: str = "app", prefix: str = "Z", repo: str = "acme/app",
                 extra: str = "", account: str | None = "acme") -> str:
    acct = f'account = "{account}"\n' if account is not None else ""
    return (f'[project]\nname = "{name}"\nprefix = "{prefix}"\nrepo = "{repo}"\n'
            f"{acct}{textwrap.dedent(extra)}")


def _git(*args: str) -> None:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
                   check=True, capture_output=True, env=env)


def make_repo(root: Path, origin: str | None = None, overlay: str | None = None) -> Path:
    _git("init", "-q", str(root))
    if origin is not None:
        _git("-C", str(root), "remote", "add", "origin", origin)
    if overlay is not None:
        write_overlay(root, overlay)
    return root


def write_overlay(root: Path, text: str) -> Path:
    path = root / ".claude" / "forge.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def overlay_only_fixture(tmp: Path, prefix: str = "Z") -> tuple[Path, Path]:
    """`(repo, base)`: overlay-only `acme/app` (account `acme`) and a base without it."""
    base = tmp / "projects.toml"
    base.write_text(FILLER)
    repo = make_repo(tmp / "app", APP_URL, overlay_text(prefix=prefix))
    return repo, base


def overlay_only_env(case: unittest.TestCase, prefix: str = "Z") -> tuple[Path, Path]:
    """`(repo, slots)`: `overlay_only_fixture` with the base and a scratch slot root
    (holding an `acme` slot) in the environment, `LANE` unset, and the process cwd
    in a scratch dir outside the repo; all restored on cleanup."""
    tmp = tempfile.TemporaryDirectory()
    case.addCleanup(tmp.cleanup)
    root = Path(tmp.name).resolve()
    repo, base = overlay_only_fixture(root, prefix)
    slots = root / "slots"
    (slots / "acme").mkdir(parents=True)
    (root / "outside").mkdir()
    case.addCleanup(os.chdir, os.getcwd())
    os.chdir(root / "outside")
    env = mock.patch.dict(os.environ, {"FORGE_PROJECTS_MANIFEST": str(base),
                                       "GH_ACCT_HOME": str(slots)})
    env.start()
    case.addCleanup(env.stop)
    os.environ.pop("LANE", None)  # restored with the rest of the dict
    return repo, slots


def registered_env(case: unittest.TestCase, overlay: str) -> tuple[Path, Path]:
    """`overlay_only_env`, but the base registers `acme/app` at the repo, whose main
    checkout holds `overlay` (harmonic-forge#1527 pass-1 survivors 3 and 7)."""
    repo, slots = overlay_only_env(case)
    write_overlay(repo, overlay)
    base = Path(os.environ["FORGE_PROJECTS_MANIFEST"])
    base.write_text(FILLER + f'\n[[project]]\nname = "app"\nprefix = "Z"\nrepo = "acme/app"\n'
                    f'account = "acme"\npath = "{repo}"\n')
    return repo, slots


def probe_from(cwd: Path, base: Path, code: str) -> str:
    """stdout of `code` run in a fresh interpreter with its cwd at `cwd` and
    `FORGE_PROJECTS_MANIFEST` at `base`; `tools/hooks` and `tools/onboard` are on
    its path (TC17: a hook process computes its prefix maps at import)."""
    env = {k: v for k, v in os.environ.items() if k != "LANE"}
    env["FORGE_PROJECTS_MANIFEST"] = str(base)
    prelude = (f"import sys\nsys.path[:0] = [{str(HERE.parent / 'hooks')!r}, {str(HERE)!r}]\n")
    result = subprocess.run([sys.executable, "-c", prelude + code], cwd=cwd, env=env,
                            capture_output=True, text=True, timeout=60, check=False)
    if result.returncode:
        raise AssertionError(f"probe failed in {cwd}: {result.stderr}")
    return result.stdout


class OverlayCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()
        self.addCleanup(os.chdir, os.getcwd())
        self.outside = self.tmp / "outside"
        self.outside.mkdir()
        os.chdir(self.outside)

    def repo(self, name: str, origin: str | None = None, overlay: str | None = None) -> Path:
        return make_repo(self.tmp / name, origin, overlay)

    def base(self, extra: str = "", text: str | None = None) -> Path:
        path = self.tmp / "projects.toml"
        path.write_text(FILLER + extra if text is None else text)
        return path

    def registered(self, root: Path, *, extra: str = "", protocol: bool = False,
                   repo: str = "acme/app") -> str:
        """A base `[[project]]` registering `repo` at `root`."""
        return (f'\n[[project]]\nname = "app"\nprefix = "Z"\nrepo = "{repo}"\n'
                f'account = "acme"\npath = "{root}"\n{textwrap.dedent(extra)}'
                + (PROTOCOL if protocol else ""))

    def env_base(self, base: Path):
        return mock.patch.dict(os.environ, {"FORGE_PROJECTS_MANIFEST": str(base)})

    def consumer(self, target: Path, base: Path):
        """What `ci_plan`, the overlay's path-keyed consumer, resolves for `target`."""
        with self.env_base(base):
            return ci_plan.resolve_project(Path(target))

    def raises(self, root: Path, base: Path, *fragments: str) -> str:
        with self.assertRaises(ManifestError) as ctx:
            mo.project_for_checkout(root, base)
        for fragment in fragments:
            self.assertIn(fragment, str(ctx.exception))
        return str(ctx.exception)

    def worktree(self, main: Path, where: Path) -> Path:
        _git("-C", str(main), "commit", "-q", "--allow-empty", "-m", "init")
        _git("-C", str(main), "worktree", "add", "-q", "--detach", str(where))
        return where


class AC1OverlayOnlyResolvesByPath(OverlayCase):
    def test_tc1_overlay_only_resolves_through_its_two_consumers(self) -> None:
        repo, base = overlay_only_fixture(self.tmp)
        project = mo.project_for_checkout(repo, base)
        self.assertEqual((project.repo, project.checkout), ("acme/app", repo))
        (repo / "sub").mkdir()
        found = self.consumer(repo / "sub", base)
        self.assertEqual((found.repo, found.checkout), ("acme/app", repo))
        self.assertEqual(mi.account_for_origin(APP_URL, base, checkout=repo), "acme")
        os.chdir(repo)
        self.assertNotIn("acme/app", mf.by_repo(base))
        self.assertNotIn("acme/app", [p.repo for p in mf.load(base)])
        self.assertNotIn("Z", mf.prefixes(base))
        self.assertNotIn("z", mf.prefix_repos(base))


class AC2InstanceKeys(OverlayCase):
    def test_tc2_instance_keys_refused_top_level_and_in_project(self) -> None:
        base = self.base()
        for key, value in (("forge_root", '"/x"'), ("projects_root", '"/x"'),
                           ("timezone", '"UTC"')):
            for top in (True, False):
                with self.subTest(key=key, top=top):
                    repo = self.repo(f"app-{key}-{top}", APP_URL)
                    text = (f"{key} = {value}\n" + overlay_text() if top
                            else overlay_text(extra=f"{key} = {value}\n"))
                    path = write_overlay(repo, text)
                    self.raises(repo, base, key, "instance.toml", str(path))


class AC3RegisteredOverlayMerges(OverlayCase):
    def test_tc3_per_key_merge_seen_by_every_path_keyed_reader(self) -> None:
        repo = self.repo("app", APP_URL, overlay_text(extra="""\
            board_number = "2"
            [project.protocol]
            gate_task = "ci-check"
            """))
        base = self.base(self.registered(repo, protocol=True, extra="""\
            board_owner = "acme"
            board_number = "1"
            milestones = false
            """))
        worktree = self.worktree(repo, self.tmp / "w")
        lane2 = self.worktree(repo, self.tmp / "app-lane2")
        for label, project in (
                ("checkout", mo.project_for_checkout(repo, base)),
                ("path", self.consumer(repo, base)),
                ("worktree", self.consumer(worktree, base)),
                ("lane2", self.consumer(lane2, base))):
            with self.subTest(label):
                self.assertEqual(project.board_number, "2")
                self.assertEqual(project.board_owner, "acme")
                self.assertIs(project.milestones, False)
                self.assertEqual(project.protocol.gate_task, "ci-check")
                self.assertEqual(project.protocol.l1_post_task, "l1-post")
                self.assertEqual(project.checkout, repo)
        self.assertEqual(mf.by_repo(base)["acme/app"].board_number, "1")
        self.assertEqual(mi.project_for_path(repo, base).board_number, "1")  # R1


class AC4MinVersion(OverlayCase):
    def test_tc4_min_version_parsed_and_exposed(self) -> None:
        repo = self.repo("app", APP_URL, overlay_text(extra='min_version = "v2026.12.1"\n'))
        base = self.base()
        self.assertEqual(mo.project_for_checkout(repo, base).min_version, "v2026.12.1")
        self.assertEqual(mv.parse_version("v2026.12.1"), (2026, 12, 1))
        listed = self.base(text=FILLER.replace('path = "/nonexistent/forge"',
                                               'path = "/nonexistent/forge"\nmin_version = "v2026.12.1"'))
        self.assertEqual(mf.load(listed)[0].min_version, "v2026.12.1")

    def test_tc4_malformed_min_version_raises_in_overlay_and_base(self) -> None:
        repo = self.repo("app", APP_URL, overlay_text(extra='min_version = "2026.12"\n'))
        self.raises(repo, self.base(), "min_version")
        bad = self.base(text=FILLER.replace('path = "/nonexistent/forge"',
                                            'path = "/nonexistent/forge"\nmin_version = "2026.12"'))
        with self.assertRaisesRegex(ManifestError, "min_version"):
            mf.load(bad)


class IssueTC4Unreadable(OverlayCase):
    def test_tc5_malformed_overlay_and_directory_raise(self) -> None:
        base = self.base()
        repo = self.repo("app", APP_URL)
        path = write_overlay(repo, "[project\n")
        self.raises(repo, base, str(path))
        other = self.repo("dir", APP_URL)
        (other / ".claude" / "forge.toml").mkdir(parents=True)
        self.raises(other, base, "forge.toml")


class CheckoutBinding(OverlayCase):
    def test_tc6a_registered_repo_overlay_outside_its_checkout_raises(self) -> None:
        a = self.repo("a", APP_URL)
        b = self.repo("b", APP_URL, overlay_text())
        self.raises(b, self.base(self.registered(a)), str(b), str(a))

    def test_tc6b_registered_checkout_overlay_must_name_its_repo(self) -> None:
        b = self.repo("b", "https://github.com/acme/b.git",
                      overlay_text(name="other", prefix="Q", repo="acme/other"))
        base = self.base(self.registered(b, repo="acme/b"))
        self.raises(b, base, "its overlay must name that repo")


class MainCheckoutRead(OverlayCase):
    def test_tc7a_worktree_reads_the_main_checkouts_overlay(self) -> None:
        a = self.repo("a", APP_URL, overlay_text(extra='board_number = "2"\n'))
        base = self.base(self.registered(a, extra='board_owner = "acme"\nboard_number = "1"\n'))
        w = self.worktree(a, self.tmp / "w")
        (w / "x").mkdir()
        self.assertEqual(mo.project_for_checkout(w, base).board_number, "2")
        self.assertEqual(self.consumer(w / "x", base).board_number, "2")

    def test_tc7b_a_worktrees_own_overlay_is_ignored(self) -> None:
        a = self.repo("a", APP_URL)
        base = self.base(self.registered(a, extra='board_owner = "acme"\nboard_number = "1"\n'))
        w = self.worktree(a, self.tmp / "w")
        write_overlay(w, overlay_text(extra='board_number = "9"\n'))
        self.assertEqual(mo.project_for_checkout(w, base).board_number, "1")
        self.assertEqual(self.consumer(w, base).board_number, "1")


class IdentityGuard(OverlayCase):
    def test_tc8_account_checks(self) -> None:
        base = self.base()
        a = self.repo("a", APP_URL, overlay_text(account="vitalharmony"))
        self.raises(a, base, "owner")
        e = self.repo("e", APP_URL, overlay_text(account="ACME"))
        self.raises(e, base, "owner")
        b = self.repo("b", APP_URL, overlay_text(account="other"))
        self.raises(b, self.base(self.registered(b)), "account")

    def test_tc8d_name_and_prefix_may_only_repeat_the_base(self) -> None:
        repo = self.repo("app", APP_URL)
        base = self.base(self.registered(repo))
        for name, prefix, field in (("app", "Q", "prefix"), ("other", "Z", "name")):
            with self.subTest(field):
                write_overlay(repo, overlay_text(name=name, prefix=prefix))
                self.raises(repo, base, field)
        write_overlay(repo, overlay_text())
        self.assertEqual(mo.project_for_checkout(repo, base).repo, "acme/app")

    def test_tc8e_overlay_only_account_is_case_exact(self) -> None:
        repo = self.repo("e", APP_URL, overlay_text(account="ACME"))
        self.raises(repo, self.base(), "account must be its owner")

    def test_tc8cf_operator_and_machine_local_keys_raise(self) -> None:
        base = self.base()
        repo = self.repo("app", APP_URL)
        write_overlay(repo, overlay_text(extra='token_exception = "x"\n'))
        self.raises(repo, base, "token_exception")
        for key in ("worktree_dir", "path"):
            with self.subTest(key):
                write_overlay(repo, overlay_text(extra=f'{key} = "/x"\n'))
                self.raises(repo, base, key, "machine-local")


class ExplicitRootOnly(OverlayCase):
    def test_tc9_the_cwd_is_never_consulted(self) -> None:
        base = self.base()
        x = self.repo("x", "https://github.com/acme/x.git",
                      overlay_text(name="x", prefix="X", repo="acme/x"))
        y = self.repo("y", "https://github.com/acme/y.git",
                      overlay_text(name="y", prefix="Q", repo="acme/y"))
        os.chdir(x)
        self.assertEqual(mo.project_for_checkout(y, base).repo, "acme/y")
        with self.assertRaisesRegex(ManifestError, "not inside any registered checkout"):
            mi.project_for_path(self.outside, base)
        with self.assertRaisesRegex(ManifestError, "not inside any registered checkout"):
            self.consumer(self.outside, base)
        with self.assertRaisesRegex(ManifestError, r"no \[\[project\]\] entry for acme/x"):
            mi.account_for_origin("https://github.com/acme/x.git", base, checkout=self.outside)
        with self.assertRaises(TypeError):
            mo.project_for_checkout()  # type: ignore[call-arg]


class PathKeyedDiscovery(OverlayCase):
    def test_tc12_a_path_resolves_its_own_checkout_not_the_cwds(self) -> None:
        base = self.base()
        x = self.repo("x", "https://github.com/acme/x.git",
                      overlay_text(name="x", prefix="X", repo="acme/x"))
        y = self.repo("y", "https://github.com/acme/y.git",
                      overlay_text(name="y", prefix="Q", repo="acme/y"))
        (y / "sub").mkdir()
        os.chdir(x)
        self.assertEqual(self.consumer(y / "sub", base).repo, "acme/y")
        self.assertEqual(mi.account_for_origin("https://github.com/acme/y.git", base,
                                               checkout=y / "sub"), "acme")


class OriginBinding(OverlayCase):
    def test_tc13a_overlay_only_repo_must_be_the_origin(self) -> None:
        repo = self.repo("x", "https://github.com/evil/x.git",
                         overlay_text(name="x", prefix="Q", repo="vitalharmony/anything",
                                      account=None))
        self.raises(repo, self.base(), "vitalharmony/anything", "evil/x")

    def test_tc13b_no_origin_raises_naming_the_checkout(self) -> None:
        repo = self.repo("x", None, overlay_text())
        self.raises(repo, self.base(), str(repo))

    def test_tc13c_registered_overlay_is_origin_bound(self) -> None:
        """R4 (round 2 #11), inverting the old TC13(c): a registered path holding a
        clone of another origin never applies its overlay."""
        repo = self.repo("app", APP_URL, overlay_text(extra='board_number = "2"\n'))
        base = self.base(self.registered(repo, extra='board_owner = "acme"\nboard_number = "1"\n'))
        self.assertEqual(mo.project_for_checkout(repo, base).board_number, "2")
        _git("-C", str(repo), "remote", "set-url", "origin", "https://github.com/evil/app.git")
        self.raises(repo, base, "acme/app", "evil/app")
        self.assertEqual(self.consumer(repo, base).board_number, "1")
        _git("-C", str(repo), "remote", "remove", "origin")
        self.raises(repo, base, "no origin remote")


class EmptyBase(OverlayCase):
    def test_tc14_empty_base_refuses_from_any_cwd(self) -> None:
        repo, _ = overlay_only_fixture(self.tmp)
        for text in ("# nothing\n", "project = 1\n"):
            with self.subTest(text=text):
                base = self.base(text=text)
                message = f"{base} declares no [[project]] entries"
                for cwd in (self.outside, repo):
                    os.chdir(cwd)
                    with self.assertRaises(ManifestError) as ctx:
                        mf.load(base)
                    self.assertEqual(str(ctx.exception), message)
                self.assertEqual(self.raises(repo, base), message)


class AC6CwdNeverChangesAGlobalLookup(OverlayCase):
    def snapshot(self):
        return (mf.load(), mf.by_repo(), mf.prefix_repos(),
                mi.slot_env_or_none("kenekted/kenekted-ai"))

    def test_tc15_global_lookups_equal_from_every_hostile_cwd(self) -> None:
        base = self.base()
        slots = self.tmp / "slots"
        with self.env_base(base), mock.patch.dict(os.environ, {"GH_ACCT_HOME": str(slots)}):
            expected = self.snapshot()
            cwds = {
                "malformed": self.repo("m", APP_URL, "[project\n"),
                "instance": self.repo("i", APP_URL, 'forge_root = "/x"\n' + overlay_text()),
                "accountless": self.repo("n", APP_URL, overlay_text(account=None)),
            }
            for label, cwd in cwds.items():
                with self.subTest(label):
                    os.chdir(cwd)
                    self.assertEqual(self.snapshot(), expected)
            gone = self.tmp / "gone"
            gone.mkdir()
            os.chdir(gone)
            os.rmdir(self.tmp / "gone")
            self.assertEqual(self.snapshot(), expected)


class AC7Structural(unittest.TestCase):
    def test_tc16_no_overlay_reach_from_the_loader(self) -> None:
        manifest_text = (HERE / "manifest.py").read_text()
        self.assertIsNone(re.search(r"manifest_overlay|app_root", manifest_text))
        sentinel = "NO_" + "OVERLAY"
        for path in HERE.glob("*.py"):
            self.assertNotIn(sentinel, path.read_text(), path.name)
        overlay_text_ = (HERE / "manifest_overlay.py").read_text()
        self.assertIsNone(re.search(r"Path\.cwd\(\)|os\.getcwd", overlay_text_))


class AC10OneExceptionType(OverlayCase):
    def test_tc19_every_failure_is_a_manifest_error(self) -> None:
        base = self.base()
        cases = {
            "integer prefix": "[project]\nname = \"app\"\nprefix = 7\nrepo = \"acme/app\"\n",
            "invalid utf-8": None,
            "bad template": overlay_text(extra=PROTOCOL.replace(
                '"{checkout}-lane{lane}"', '"{0"')),
        }
        for label, text in cases.items():
            with self.subTest(label):
                repo = self.repo(label.replace(" ", "-"), APP_URL)
                if text is None:
                    path = write_overlay(repo, "")
                    path.write_bytes(b'[project]\nname = "\xff\xfe"\n')
                else:
                    write_overlay(repo, text)
                with self.assertRaises(ManifestError):
                    mo.project_for_checkout(repo, base)
        gone = self.repo("gone", APP_URL, overlay_text())
        root = mo.checkout_root(gone)
        shutil.rmtree(gone)
        with self.assertRaises(ManifestError):
            mo.project_for_checkout(root, base)


class ValidatedAgainstTheBase(OverlayCase):
    def test_tc20_base_clashes_and_validate_refuse(self) -> None:
        base = self.base()
        cases = (
            (overlay_text(name="forge"), "already registered"),
            (overlay_text(prefix="F"), "clashes"),
            (overlay_text(prefix="f"), "clashes"),
            (overlay_text(extra="onboarded = true\n"), "workspace"),
        )
        for index, (text, fragment) in enumerate(cases):
            with self.subTest(fragment=fragment, index=index):
                repo = self.repo(f"app{index}", APP_URL, text)
                self.raises(repo, base, fragment)


class SharedStateIsNeverClaimed(OverlayCase):
    """Pass-1 survivor 1: `workspace` selects a belt queue, as `prefix` selects a BATCH
    key, so an overlay never claims or changes a workspace `projects.toml` declares."""

    def test_r1_an_overlay_never_claims_a_registered_workspace(self) -> None:
        declared = FILLER.replace('path = "/nonexistent/forge"',
                                  'path = "/nonexistent/forge"\nworkspace = "vh"')
        only = self.repo("only", APP_URL, overlay_text(extra='workspace = "VH"\n'))
        self.raises(only, self.base(text=declared), "workspace", "clashes")
        write_overlay(only, overlay_text(extra='workspace = "acme"\n'))
        self.assertEqual(mo.project_for_checkout(only, self.base(text=declared)).workspace, "acme")
        reg = self.repo("reg", APP_URL, overlay_text(extra='workspace = "vh"\n'))
        base = self.base(text=declared + self.registered(reg, extra='workspace = "acme"\n'))
        self.raises(reg, base, "workspace", "may not change it")
        write_overlay(reg, overlay_text(extra='workspace = "acme"\n'))
        self.assertEqual(mo.project_for_checkout(reg, base).workspace, "acme")


class RegisteredOverlayNeedsOnlyItsRepo(OverlayCase):
    """Pass-1 survivor 2: a registered repo's identity keys are "absent or exactly
    equal", so its overlay needs only `repo`; an overlay-only entry needs all three."""

    def test_r2_a_registered_overlay_may_omit_name_and_prefix(self) -> None:
        repo = self.repo("app", APP_URL, '[project]\nrepo = "acme/app"\nmin_version = "v2026.12.1"\n')
        base = self.base(self.registered(repo))
        for project in (mo.project_for_checkout(repo, base), self.consumer(repo, base)):
            with self.subTest(project=project):
                self.assertEqual((project.name, project.prefix, project.min_version),
                                 ("app", "Z", "v2026.12.1"))
        only = self.repo("only", APP_URL, '[project]\nrepo = "acme/app"\nprefix = "Q"\n')
        self.raises(only, self.base(), "missing `name`")


class ARejectedOverlayNeverErasesTheBase(OverlayCase):
    """Pass-1 survivor 7: an overlay refines a base hit; a rejected one leaves the
    checkout as `projects.toml` alone resolves it, and raises only with no base hit."""

    def test_r7_a_rejected_overlay_on_a_registered_checkout_yields_the_base(self) -> None:
        repo = self.repo("app", APP_URL, "[project\n")
        base = self.base(self.registered(repo))
        self.raises(repo, base, "forge.toml")
        self.assertEqual(self.consumer(repo, base).repo, "acme/app")
        only = self.repo("only", APP_URL, "[project\n")
        with self.assertRaisesRegex(ManifestError, "forge.toml"):
            self.consumer(only, base)


class ScopeLimitEntrypointsRefuse(OverlayCase):
    def test_tc21_key_keyed_entrypoints_refuse_an_overlay_only_repo(self) -> None:
        repo, base = overlay_only_fixture(self.tmp)
        os.chdir(repo)
        with self.env_base(base):
            with self.assertRaisesRegex(ManifestError, "the registry is closed"):
                mf.require_onboarded_repo("acme/app")
            with self.assertRaisesRegex(ManifestError, r"no \[\[project\]\] entry"):
                mi.account_for("acme/app")


class OverlayHasTwoConsumers(OverlayCase):
    """Sticky-wicket R1: only `account_for_origin(checkout=)`, `ci_plan` and `manifest_invoking`
    read the overlay; every gate, hook and slot input answers as origin/main did."""

    def test_r1_overlay_only_checkout_is_unknown_to_every_gate_input(self) -> None:
        sys.path.insert(0, str(HERE.parent / "hooks"))
        import hook_identity  # noqa: PLC0415
        repo = self.repo("app", APP_URL, overlay_text(
            extra='onboarded = true\nworkspace = "acmews"\n' + PROTOCOL))
        base = self.base()
        self.assertEqual(self.consumer(repo, base).workspace, "acmews")
        os.chdir(repo)
        with self.env_base(base):
            self.assertIsNone(hook_identity.repo_from_checkout(str(repo)))
            self.assertIsNone(mi.repo_for_path_or_none(repo))
            with self.assertRaises(ManifestError):
                mi.workspace_for(repo)
            with self.assertRaises(ManifestError):
                mi.project_for_path(repo)
            self.assertIsNone(mi.slot_env_or_none("acme/app"))

    def test_r1_structural_allowlist(self) -> None:
        root = HERE.parents[1]
        found = subprocess.run(
            ["git", "-C", str(root), "grep", "-l", "-E",
             "manifest_overlay|path_project|project_for_checkout", "--",
             "tools/**/*.py", "skills/**/*.py", ":!**/test_*"],
            capture_output=True, text=True, check=False).stdout.split()
        self.assertEqual(sorted(found), ["tools/lane/ci_plan.py", "tools/onboard/manifest_identity.py",
                                         "tools/onboard/manifest_invoking.py",
                                         "tools/onboard/manifest_overlay.py"])
        import ast  # noqa: PLC0415
        source = (HERE / "manifest_identity.py").read_text()
        fn = next(n for n in ast.parse(source).body
                  if isinstance(n, ast.FunctionDef) and n.name == "account_for_origin")
        lines = [i for i, line in enumerate(source.splitlines(), 1)
                 if re.search(r"manifest_overlay|path_project|project_for_checkout", line)]
        self.assertTrue(lines)
        self.assertTrue(all(fn.lineno <= i <= fn.end_lineno for i in lines), lines)


class GitTopology(OverlayCase):
    """Sticky-wicket R3: the main checkout is git's own answer, and only a regular
    file is read."""

    def test_r3_separate_git_dir_resolves_from_git_not_a_path_join(self) -> None:
        decoy = self.repo("decoy", "https://github.com/acme/decoy.git",
                          overlay_text(name="decoy", prefix="D", repo="acme/decoy"))
        main = self.tmp / "main"
        _git("init", "-q", "--separate-git-dir", str(decoy / "sep.git"), str(main))
        _git("-C", str(main), "remote", "add", "origin", APP_URL)
        write_overlay(main, overlay_text())
        wt = self.worktree(main, self.tmp / "wt")
        base = self.base()
        self.assertEqual(mg.main_checkout(main), main)
        self.assertEqual(mo.project_for_checkout(main, base).repo, "acme/app")
        with self.assertRaisesRegex(ManifestError, "no main worktree"):
            mg.main_checkout(wt)
        with self.assertRaisesRegex(ManifestError, "no main worktree"):
            mo.project_for_checkout(wt, base)

    def test_r3_submodule_and_bare_worktrees(self) -> None:
        upstream = self.repo("upstream", APP_URL)
        _git("-C", str(upstream), "commit", "-q", "--allow-empty", "-m", "init")
        sup = self.repo("super")
        _git("-C", str(sup), "commit", "-q", "--allow-empty", "-m", "init")
        _git("-c", "protocol.file.allow=always", "-C", str(sup), "submodule", "add", "-q",
             str(upstream), "sub")
        sub = sup / "sub"
        subwt = self.tmp / "subwt"
        _git("-C", str(sub), "worktree", "add", "-q", "--detach", str(subwt))
        self.assertEqual(mg.main_checkout(sub), sub)
        self.assertEqual(mg.main_checkout(subwt), sub)
        bare = self.tmp / "bare.git"
        _git("clone", "-q", "--bare", str(upstream), str(bare))
        barewt = self.tmp / "barewt"
        _git("-C", str(bare), "worktree", "add", "-q", "--detach", str(barewt))
        with self.assertRaisesRegex(ManifestError, "no main worktree"):
            mg.main_checkout(barewt)

    def test_r3_a_fifo_overlay_is_refused_without_reading(self) -> None:
        import signal  # noqa: PLC0415
        repo = self.repo("app", APP_URL)
        (repo / ".claude").mkdir()
        os.mkfifo(repo / ".claude" / "forge.toml")
        signal.signal(signal.SIGALRM, lambda *_: (_ for _ in ()).throw(TimeoutError("stalled")))
        self.addCleanup(signal.signal, signal.SIGALRM, signal.SIG_DFL)
        signal.alarm(10)
        try:
            self.raises(repo, self.base(), "regular file")
        finally:
            signal.alarm(0)

    def test_a_symlinked_overlay_is_refused_not_followed(self) -> None:
        # harmonic-forge#1527 post-verdict finding 1 (operator 2026-11-25).
        repo = self.repo("app", APP_URL)
        (repo / ".claude").mkdir()
        outside = self.tmp / "outside.toml"
        outside.write_text('[project]\nrepo = "acme/app"\n', encoding="utf-8")
        (repo / ".claude" / "forge.toml").symlink_to(outside)
        self.raises(repo, self.base(), "cannot read")

    def test_a_symlinked_claude_directory_is_refused_not_followed(self) -> None:
        # harmonic-forge#1527 one-line post-verdict: the parent directory too.
        repo = self.repo("app", APP_URL)
        elsewhere = self.tmp / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "forge.toml").write_text('[project]\nrepo = "acme/app"\n', encoding="utf-8")
        (repo / ".claude").symlink_to(elsewhere, target_is_directory=True)
        self.raises(repo, self.base(), "cannot read")


class RemainingGuardsArePinned(OverlayCase):
    """Sticky-wicket R5: one killing test per guard round 2 found unpinned."""

    def test_r5_account_for_origin_refuses_a_checkout_of_another_repo(self) -> None:
        repo, base = overlay_only_fixture(self.tmp)
        self.assertEqual(mi.account_for_origin(APP_URL, base, checkout=repo), "acme")
        with self.assertRaisesRegex(ManifestError, r"no \[\[project\]\] entry for acme/other"):
            mi.account_for_origin("https://github.com/acme/other.git", base, checkout=repo)

    def test_r5_a_nested_clone_never_displaces_the_base_hit(self) -> None:
        outer = self.repo("app", APP_URL)
        base = self.base(self.registered(outer))
        nested = make_repo(outer / "vendor" / "x", "https://github.com/acme/x.git",
                           overlay_text(name="x", prefix="X", repo="acme/x"))
        self.assertEqual(mo.project_for_checkout(nested, base).repo, "acme/x")
        hit = mi.project_for_path(nested, base)
        self.assertEqual(hit.repo, "acme/app")
        self.assertEqual(mo.path_project(nested, hit, base).repo, "acme/app")
        self.assertEqual(self.consumer(nested, base).repo, "acme/app")

    def test_r5_origin_of_ignores_the_callers_git_location_env(self) -> None:
        repo = self.repo("app", APP_URL)
        other = self.repo("other", "https://github.com/evil/x.git")
        with mock.patch.dict(os.environ, {"GIT_DIR": str(other / ".git"),
                                          "GIT_WORK_TREE": str(other)}):
            self.assertEqual(mg.origin_of(repo), "acme/app")


if __name__ == "__main__":
    unittest.main()
