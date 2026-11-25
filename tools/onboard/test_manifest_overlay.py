#!/usr/bin/env python3
"""Tests for `manifest_overlay.py`: an app repo's `.claude/forge.toml` merged
over `projects.toml` (harmonic-forge#1527).

Every case uses scratch `git init` repos and a scratch base manifest, and
either an explicit `app_root=` or a scratch cwd restored on cleanup. None reads
the live manifest or the repo's own cwd."""
from __future__ import annotations

import os
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

FILLER = """\
[[project]]
name = "other"
prefix = "O"
repo = "other/o"
account = "other"
path = "/nonexistent/other"
"""


def _git(*args: str) -> None:
    subprocess.run(["git", *args], check=True, capture_output=True)


class OverlayCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()
        cwd = os.getcwd()
        self.addCleanup(os.chdir, cwd)
        self.outside = self.tmp / "outside"
        self.outside.mkdir()
        os.chdir(self.outside)

    def repo(self, name: str, origin: str | None = None, overlay: str | None = None) -> Path:
        root = self.tmp / name
        _git("init", "-q", str(root))
        if origin is not None:
            _git("-C", str(root), "remote", "add", "origin", origin)
        if overlay is not None:
            self.write_overlay(root, overlay)
        return root

    @staticmethod
    def write_overlay(root: Path, text: str) -> Path:
        path = root / ".claude" / "forge.toml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(text))
        return path

    def base(self, text: str = FILLER) -> Path:
        path = self.tmp / "projects.toml"
        path.write_text(textwrap.dedent(text))
        return path

    def assertRaisesWith(self, fragments: tuple[str, ...], fn, *args, **kwargs) -> str:
        with self.assertRaises(ManifestError) as caught:
            fn(*args, **kwargs)
        message = str(caught.exception)
        for fragment in fragments:
            self.assertIn(fragment, message)
        return message


ACME_ONLY = """\
[project]
name = "app"
prefix = "Z"
repo = "acme/app"
account = "acme"
"""


class OverlayOnlyRepoLoads(OverlayCase):  # case 1, AC1/TC1
    def test_an_overlay_only_repo_loads_in_every_consumer(self) -> None:
        repo = self.repo("app", "https://github.com/acme/app.git", ACME_ONLY)
        base = self.base()
        project = {p.repo: p for p in mf.load(base, app_root=repo)}["acme/app"]
        self.assertEqual(project.checkout, repo)
        self.assertEqual(project.prefix, "Z")
        self.assertEqual(mi.project_for_path(repo, base).repo, "acme/app")
        os.chdir(repo)
        self.assertIn("acme/app", mf.by_repo(base))
        self.assertEqual(mf.prefix_repos(base)["z"], "acme/app")


class InstanceKeysRefused(OverlayCase):  # case 2, AC2/TC2
    def test_forge_root_names_instance_toml_and_the_overlay(self) -> None:
        base = self.base()
        for label, text in (("top", 'forge_root = "/x"\n' + ACME_ONLY),
                            ("inside", ACME_ONLY + 'forge_root = "/x"\n')):
            with self.subTest(label):
                repo = self.repo(f"app-{label}", "https://github.com/acme/app.git", text)
                self.assertRaisesWith(
                    ("forge_root", "instance.toml", str(repo / ".claude" / "forge.toml")),
                    mf.load, base, app_root=repo)


class PerKeyMerge(OverlayCase):  # case 3, AC3/TC3
    def test_the_overlay_wins_per_key_and_base_keys_are_kept(self) -> None:
        repo = self.repo("app", "https://github.com/acme/app.git")
        base = self.base(FILLER + textwrap.dedent(f"""\
            [[project]]
            name = "app"
            prefix = "Z"
            repo = "acme/app"
            account = "acme"
            path = "{repo}"
            board_owner = "acme"
            board_number = "1"
            milestones = false
            workspace = "w"
            """) + PROTOCOL)
        self.write_overlay(repo, '[project]\nname = "app"\nprefix = "Z"\nrepo = "acme/app"\n'
                                 'board_number = "2"\n'
                                 '[project.protocol]\ngate_task = "ci-check"\n')
        project = {p.repo: p for p in mf.load(base, app_root=repo)}["acme/app"]
        self.assertEqual(project.board_number, "2")
        self.assertIs(project.milestones, False)
        self.assertEqual(project.workspace, "w")
        self.assertEqual(project.protocol.gate_task, "ci-check")
        self.assertEqual(project.protocol.l1_post_task, "l1-post")
        self.assertEqual([p.name for p in mf.load(base, app_root=repo)], ["other", "app"])


class MinVersion(OverlayCase):  # case 4, AC4
    def test_min_version_is_readable_and_parsed(self) -> None:
        repo = self.repo("app", "https://github.com/acme/app.git",
                         ACME_ONLY + 'min_version = "v2026.12.1"\n')
        project = {p.repo: p for p in mf.load(self.base(), app_root=repo)}["acme/app"]
        self.assertEqual(project.min_version, "v2026.12.1")
        self.assertEqual(mo.parse_version(project.min_version), (2026, 12, 1))

    def test_a_malformed_min_version_raises(self) -> None:
        repo = self.repo("app", "https://github.com/acme/app.git",
                         ACME_ONLY + 'min_version = "2026.12"\n')
        self.assertRaisesWith(("min_version", "2026.12"), mf.load, self.base(), app_root=repo)


class MalformedOverlay(OverlayCase):  # case 5, TC4
    def test_invalid_toml_names_the_file(self) -> None:
        repo = self.repo("app", "https://github.com/acme/app.git", "[project\n")
        self.assertRaisesWith((str(repo / ".claude" / "forge.toml"),),
                              mf.load, self.base(), app_root=repo)

    def test_a_directory_at_the_path_raises(self) -> None:
        repo = self.repo("app", "https://github.com/acme/app.git")
        (repo / ".claude" / "forge.toml").mkdir(parents=True)
        self.assertRaisesWith((str(repo / ".claude" / "forge.toml"),),
                              mf.load, self.base(), app_root=repo)


def _registered(path: Path, extra: str = "") -> str:
    return FILLER + textwrap.dedent(f"""\
        [[project]]
        name = "app"
        prefix = "Z"
        repo = "acme/app"
        account = "acme"
        path = "{path}"
        board_number = "1"
        """) + extra


class CheckoutBinding(OverlayCase):  # case 6
    def test_an_overlay_for_a_registered_repo_elsewhere_raises(self) -> None:
        a = self.repo("a", "https://github.com/acme/app.git")
        b = self.repo("b", "https://github.com/acme/app.git",
                      '[project]\nname = "app"\nprefix = "Z"\nrepo = "acme/app"\n'
                      'board_number = "9"\n')
        self.assertRaisesWith((str(a), str(b / ".claude" / "forge.toml")),
                              mf.load, self.base(_registered(a)), app_root=b)


class MainCheckoutRead(OverlayCase):  # case 7
    def setUp(self) -> None:
        super().setUp()
        self.a = self.repo("a", "https://github.com/acme/app.git")
        _git("-C", str(self.a), "-c", "user.email=t@t", "-c", "user.name=t",
             "commit", "-q", "--allow-empty", "-m", "init")
        self.w = self.tmp / "w"
        _git("-C", str(self.a), "worktree", "add", "-q", str(self.w))
        self.base_path = self.base(_registered(self.a))
        self.overlay = '[project]\nname = "app"\nprefix = "Z"\nrepo = "acme/app"\nboard_number = "2"\n'

    def board(self, **kwargs) -> str:
        return {p.repo: p for p in mf.load(self.base_path, **kwargs)}["acme/app"].board_number

    def test_the_main_checkouts_overlay_applies_in_a_worktree(self) -> None:
        self.write_overlay(self.a, self.overlay)
        self.assertEqual(mo.main_checkout(self.w), self.a)
        self.assertEqual(self.board(app_root=self.w), "2")
        os.chdir(self.w)
        self.assertEqual(self.board(), "2")

    def test_the_worktrees_own_copy_is_ignored(self) -> None:
        self.write_overlay(self.w, self.overlay)
        self.assertEqual(self.board(app_root=self.w), "1")
        self.write_overlay(self.a, self.overlay.replace('"2"', '"3"'))
        self.assertEqual(self.board(app_root=self.w), "3")


class IdentityGuard(OverlayCase):  # case 8
    def test_an_overlay_only_account_other_than_the_owner_raises(self) -> None:
        for account in ("vitalharmony", "ACME"):  # (a), (e)
            with self.subTest(account=account):
                repo = self.repo(f"app-{account}", "https://github.com/acme/app.git",
                                 ACME_ONLY.replace('"acme"\n', f'"{account}"\n'))
                self.assertRaisesWith(("account", "acme"), mf.load, self.base(), app_root=repo)

    def test_a_registered_repos_identity_cannot_change(self) -> None:
        repo = self.repo("app", "https://github.com/acme/app.git")
        base = self.base(_registered(repo))
        same = {"repo": "acme/app", "name": "app", "prefix": "Z", "account": "acme"}

        def overlay(**changed: str) -> str:
            return "[project]\n" + "".join(f'{k} = "{v}"\n' for k, v in {**same, **changed}.items())
        for key, value in (("account", "other"), ("prefix", "Q"), ("name", "other")):  # (b), (d)
            with self.subTest(key=key):
                self.write_overlay(repo, overlay(**{key: value}))
                self.assertRaisesWith((key, "may not change"), mf.load, base, app_root=repo)
        self.write_overlay(repo, overlay())
        self.assertEqual({p.repo: p for p in mf.load(base, app_root=repo)}["acme/app"].prefix, "Z")

    def test_token_exception_is_refused(self) -> None:  # (c)
        repo = self.repo("app", "https://github.com/acme/app.git",
                         ACME_ONLY + 'token_exception = "why"\n')
        self.assertRaisesWith(("token_exception",), mf.load, self.base(), app_root=repo)


class DiscoveryPrecedence(OverlayCase):  # case 9
    def test_app_root_wins_over_the_cwd(self) -> None:
        repo = self.repo("app", "https://github.com/acme/app.git", ACME_ONLY)
        self.assertIn("acme/app", {p.repo for p in mf.load(self.base(), app_root=repo)})

    def test_the_cwd_walk_finds_the_overlay(self) -> None:
        repo = self.repo("app", "https://github.com/acme/app.git", ACME_ONLY)
        (repo / "sub").mkdir()
        os.chdir(repo / "sub")
        self.assertIn("acme/app", {p.repo for p in mf.load(self.base())})

    def test_outside_any_checkout_the_base_is_unchanged(self) -> None:
        self.assertEqual([p.repo for p in mf.load(self.base())], ["other/o"])

    def test_a_machine_local_path_raises(self) -> None:
        repo = self.repo("app", "https://github.com/acme/app.git", ACME_ONLY + 'path = "/x"\n')
        self.assertRaisesWith(("path", "machine-local"), mf.load, self.base(), app_root=repo)


class PathKeyedDiscovery(OverlayCase):  # case 12, change A
    def test_a_path_resolves_through_its_own_checkouts_overlay(self) -> None:
        def only(letter: str) -> str:
            low = letter.lower()
            return (f'[project]\nname = "{low}"\nprefix = "{letter}"\n'
                    f'repo = "acme/{low}"\naccount = "acme"\n')
        x = self.repo("x", "https://github.com/acme/x.git", only("X"))
        y = self.repo("y", "https://github.com/acme/y.git", only("Y"))
        (y / "sub").mkdir()
        base = self.base()
        os.chdir(x)
        self.assertEqual(mi.project_for_path(y / "sub", base).repo, "acme/y")
        with mock.patch.dict(os.environ, {"FORGE_PROJECTS_MANIFEST": str(base)}):
            self.assertEqual(mi.repo_for_path_or_none(y), "acme/y")


class OriginBinding(OverlayCase):  # case 13
    VH = '[project]\nname = "a"\nprefix = "Z"\nrepo = "vitalharmony/anything"\naccount = "vitalharmony"\n'

    def test_an_overlay_only_repo_must_be_the_checkouts_origin(self) -> None:  # (a)
        repo = self.repo("evil", "https://github.com/evil/x.git", self.VH)
        self.assertRaisesWith(("vitalharmony/anything", "evil/x"),
                              mf.load, self.base(), app_root=repo)

    def test_no_origin_remote_raises_naming_the_checkout(self) -> None:  # (b)
        repo = self.repo("bare", None, self.VH)
        self.assertRaisesWith((str(repo),), mf.load, self.base(), app_root=repo)

    def test_a_match_found_overlay_makes_no_git_call(self) -> None:  # (c)
        repo = self.repo("app", "https://github.com/acme/app.git",
                         '[project]\nname = "app"\nprefix = "Z"\nrepo = "acme/app"\n')
        base = self.base(_registered(repo))
        with mock.patch.object(mo.subprocess, "run", side_effect=AssertionError("git called")):
            self.assertIn("acme/app", {p.repo for p in mf.load(base, app_root=repo)})


class BaseWithNoProjects(OverlayCase):  # case 14
    def test_an_overlay_alone_loads_over_an_empty_base(self) -> None:
        repo = self.repo("app", "https://github.com/acme/app.git", ACME_ONLY)
        base = self.base("# no projects\n")
        self.assertEqual([p.repo for p in mf.load(base, app_root=repo)], ["acme/app"])

    def test_an_empty_base_with_no_overlay_raises(self) -> None:
        self.assertRaisesWith(("declares no [[project]] entries",), mf.load,
                              self.base("# no projects\n"))

    def test_a_non_array_project_raises_naming_the_base(self) -> None:
        base = self.base("project = 1\n")
        self.assertRaisesWith((str(base),), mf.load, base)


if __name__ == "__main__":
    unittest.main()
