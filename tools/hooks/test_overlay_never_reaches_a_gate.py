#!/usr/bin/env python3
"""A checkout's `.claude/forge.toml` never reaches a gate (harmonic-forge#1527
sticky-wicket R2).

The fork decoy (round 2 finding #1): a fork clone whose origin-bound overlay
names the fork. If the overlay reached a close hook's repo resolution, a bare
`gh issue close N` would check the fork's labels while gh closes the upstream
issue, skipping the gate. These tests pin that the close hooks' repo and
credential resolution in such a checkout are exactly what they are with no
overlay at all, which is origin/main's resolution: the overlay is read by the
launcher and `ci_plan` only.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "onboard"))

import block_data_migration_close as migration  # noqa: E402
import block_missing_preclose_inspection as preclose  # noqa: E402
import block_undetermined_phase_close as phase  # noqa: E402
import close_gate  # noqa: E402
import hook_identity  # noqa: E402
from test_manifest_overlay import _git, write_overlay  # noqa: E402

UPSTREAM = "vitalharmony/hrse"
BASE = f"""\
[[project]]
name = "hrse"
prefix = "H"
repo = "{UPSTREAM}"
account = "vitalharmony"
path = "/nonexistent/hrse"

[accounts.vitalharmony]
lane_slots = true
"""
FORK_OVERLAY = ('[project]\nname = "hrse-fork"\nprefix = "Q"\nrepo = "evil/hrse"\n'
                'account = "evil"\n')


class ForkDecoy(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name).resolve()
        base = root / "projects.toml"
        base.write_text(BASE)
        self.slots = root / "slots"
        for account in ("vitalharmony", "evil"):
            (self.slots / account).mkdir(parents=True)
        self.fork = root / "hrse"
        _git("init", "-q", str(self.fork))
        _git("-C", str(self.fork), "remote", "add", "origin", "https://github.com/evil/hrse.git")
        _git("-C", str(self.fork), "remote", "add", "upstream",
             f"https://github.com/{UPSTREAM}.git")
        self.overlay = write_overlay(self.fork, FORK_OVERLAY)
        env = mock.patch.dict(os.environ, {"FORGE_PROJECTS_MANIFEST": str(base),
                                           "GH_ACCT_HOME": str(self.slots)})
        env.start()
        self.addCleanup(env.stop)
        for name in ("LANE", "GH_TOKEN", "GITHUB_TOKEN", "GH_CONFIG_DIR", "GH_REPO"):
            os.environ.pop(name, None)  # restored with the rest of the dict
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(self.fork)
        # The overlay is valid and origin-bound: it is exactly what the launcher accepts.
        import manifest_overlay  # noqa: PLC0415
        self.assertEqual(manifest_overlay.project_for_checkout(self.fork, base).repo, "evil/hrse")

    def test_r2_bare_close_repo_falls_through_to_gh_repo_view(self) -> None:
        for hook in (migration, preclose, phase):
            with self.subTest(hook=hook.__name__):
                asked: list[tuple] = []

                def stub(*args, cwd=None, repo=None):
                    asked.append(args)
                    return f"{UPSTREAM}\n" if args[:2] == ("repo", "view") else None
                with mock.patch.object(hook, "_gh", side_effect=stub):
                    self.assertEqual(hook.resolve_repo(None, str(self.fork)), UPSTREAM)
                self.assertEqual([a[:2] for a in asked], [("repo", "view")])

    def _gate_reads(self) -> tuple[str | None, list[tuple]]:
        """`decide_target` for a bare close in the fork, with every `gh` the gate
        runs recorded as (argv, GH_CONFIG_DIR, GH_TOKEN)."""
        seen: list[tuple] = []

        def run(argv, **kw):
            env = kw.get("env")
            seen.append((tuple(argv[1:]), None if env is None else env.get("GH_CONFIG_DIR"),
                         None if env is None else env.get("GH_TOKEN")))
            out = f"{UPSTREAM}\n" if argv[1:3] == ("repo", "view") else "data-migration\n"
            return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")
        fake = types.SimpleNamespace(run=run, CompletedProcess=subprocess.CompletedProcess)
        with mock.patch.object(hook_identity, "subprocess", fake), \
                mock.patch.object(close_gate.STATE, "gh_bin", sys.executable):
            denied = migration.decide_target(None, "5", str(self.fork))
        return denied, seen

    def test_fork_decoy_gate_resolution_equals_origin_main(self) -> None:
        with_overlay = self._gate_reads()
        self.overlay.unlink()
        without = self._gate_reads()
        self.assertEqual(with_overlay, without)
        denied, seen = with_overlay
        self.assertIn(f"{UPSTREAM}#5", denied or "")
        self.assertEqual(seen[0][1:], (None, None))  # the repo view inherits, as on origin/main
        self.assertEqual(seen[1], (("api", f"repos/{UPSTREAM}/issues/5", "--jq", ".labels[].name"),
                                   str(self.slots / "vitalharmony"), None))
        self.assertNotIn("evil", repr(seen))


if __name__ == "__main__":
    unittest.main()
