#!/usr/bin/env python3
"""Tests for `manifest_overlay.py`: an app repo's `.claude/forge.toml`, reached
only through the per-checkout resolver (harmonic-forge#1527 merge-point reforge).

Every case uses scratch `git init` repos and a scratch base manifest passed as
`manifest=`/`path=` or through `FORGE_PROJECTS_MANIFEST`, with a scratch cwd
restored on cleanup. None reads the live manifest or the repo's own cwd. The
case numbers are the handoff's TC numbers.

`overlay_only_fixture` and `overlay_only_env` are imported by the hook, hygiene
and worktree tests (TC17, TC18), so every consumer is driven against the same
overlay-only repo.
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

import manifest as mf  # noqa: E402
import manifest_identity as mi  # noqa: E402
import manifest_overlay as mo  # noqa: E402
import manifest_validate as mv  # noqa: E402
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
    def test_tc1_overlay_only_resolves_through_every_path_keyed_consumer(self) -> None:
        repo, base = overlay_only_fixture(self.tmp)
        project = mo.project_for_checkout(repo, base)
        self.assertEqual((project.repo, project.checkout), ("acme/app", repo))
        (repo / "sub").mkdir()
        found = mi.project_for_path(repo / "sub", base)
        self.assertEqual((found.repo, found.checkout), ("acme/app", repo))
        with self.env_base(base):
            self.assertEqual(mi.repo_for_path_or_none(repo), "acme/app")
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
                ("path", mi.project_for_path(repo, base)),
                ("worktree", mi.project_for_path(worktree, base)),
                ("lane2", mi.project_for_path(lane2, base))):
            with self.subTest(label):
                self.assertEqual(project.board_number, "2")
                self.assertEqual(project.board_owner, "acme")
                self.assertIs(project.milestones, False)
                self.assertEqual(project.protocol.gate_task, "ci-check")
                self.assertEqual(project.protocol.l1_post_task, "l1-post")
                self.assertEqual(project.checkout, repo)
        self.assertEqual(mf.by_repo(base)["acme/app"].board_number, "1")


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
        self.assertEqual(mi.project_for_path(w / "x", base).board_number, "2")

    def test_tc7b_a_worktrees_own_overlay_is_ignored(self) -> None:
        a = self.repo("a", APP_URL)
        base = self.base(self.registered(a, extra='board_owner = "acme"\nboard_number = "1"\n'))
        w = self.worktree(a, self.tmp / "w")
        write_overlay(w, overlay_text(extra='board_number = "9"\n'))
        self.assertEqual(mo.project_for_checkout(w, base).board_number, "1")
        self.assertEqual(mi.project_for_path(w, base).board_number, "1")


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
        self.assertEqual(mi.project_for_path(y / "sub", base).repo, "acme/y")
        with self.env_base(base):
            self.assertEqual(mi.repo_for_path_or_none(y), "acme/y")


class OriginBinding(OverlayCase):
    def test_tc13a_overlay_only_repo_must_be_the_origin(self) -> None:
        repo = self.repo("x", "https://github.com/evil/x.git",
                         overlay_text(name="x", prefix="Q", repo="vitalharmony/anything",
                                      account=None))
        self.raises(repo, self.base(), "vitalharmony/anything", "evil/x")

    def test_tc13b_no_origin_raises_naming_the_checkout(self) -> None:
        repo = self.repo("x", None, overlay_text())
        self.raises(repo, self.base(), str(repo))

    def test_tc13c_registered_overlay_makes_no_git_call(self) -> None:
        repo = self.repo("app", APP_URL, overlay_text(extra='board_number = "2"\n'))
        base = self.base(self.registered(repo, extra='board_owner = "acme"\n'))
        with mock.patch.object(mo.subprocess, "run", side_effect=AssertionError("git ran")):
            self.assertEqual(mo.project_for_checkout(repo, base).board_number, "2")


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


class ScopeLimitEntrypointsRefuse(OverlayCase):
    def test_tc21_key_keyed_entrypoints_refuse_an_overlay_only_repo(self) -> None:
        repo, base = overlay_only_fixture(self.tmp)
        os.chdir(repo)
        with self.env_base(base):
            with self.assertRaisesRegex(ManifestError, "the registry is closed"):
                mf.require_onboarded_repo("acme/app")
            with self.assertRaisesRegex(ManifestError, r"no \[\[project\]\] entry"):
                mi.account_for("acme/app")


if __name__ == "__main__":
    unittest.main()
