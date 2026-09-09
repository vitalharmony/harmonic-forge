#!/usr/bin/env python3
"""First test harness for sync_rules.py (harmonic-forge#541).

**Why there wasn't one.** sync_rules.py is the oldest tool in the repo and sits
at the root rather than under `tools/`, so `run_tests.py`'s discovery -- which
walks named leaf directories under `tools/` -- never reached it. It has been
the least-tested and most load-bearing script here: every project's rules,
agents, and skills arrive through it.

**Why the tests live here rather than beside their subject.** `run_tests.py`
collects `tools/<leaf>/test_*.py` and nothing else, and its own docstring warns
that a file present on disk but absent from the suite "looks exactly like all
tests pass". A `test_sync_rules.py` at the repo root would be exactly that
file. `onboard/` is the right leaf: `forge_onboard.py` already owns "bring a
repo under the protocol", which is what sync_rules.py does to a checkout.

The subject is imported by path, since the repo root is not on sys.path for a
test discovered two directories down.
"""
from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("sync_rules", _ROOT / "sync_rules.py")
assert _spec and _spec.loader
sync_rules = importlib.util.module_from_spec(_spec)
sys.modules["sync_rules"] = sync_rules
_spec.loader.exec_module(sync_rules)


class ManifestLoadTests(unittest.TestCase):
    """`load_skill_manifest` — the absent/declared/malformed three-way split."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.project = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def _write(self, text: str) -> None:
        target = sync_rules.manifest_path(self.project)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    def test_absent_manifest_returns_none_not_empty_list(self) -> None:
        """The distinction this whole issue exists to preserve.

        `[]` and `None` are both falsy; a caller testing truthiness treats an
        undeclared repo as one declaring nothing, which is the #540 defect.
        """
        result = sync_rules.load_skill_manifest(self.project)
        self.assertIsNone(result)
        self.assertIsNot(result, [])

    def test_declared_skills_are_returned(self) -> None:
        self._write('skills = ["belt-and-suspenders", "impl-worktree"]\n')
        self.assertEqual(
            sync_rules.load_skill_manifest(self.project),
            ["belt-and-suspenders", "impl-worktree"],
        )

    def test_explicit_empty_list_is_a_declaration_not_an_absence(self) -> None:
        self._write("skills = []\n")
        self.assertEqual(sync_rules.load_skill_manifest(self.project), [])

    def test_malformed_toml_raises_rather_than_falling_back(self) -> None:
        self._write("skills = [unclosed\n")
        with self.assertRaises(sync_rules.ManifestError):
            sync_rules.load_skill_manifest(self.project)

    def test_unknown_key_raises(self) -> None:
        """`skill = [...]` parses fine and declares nothing — the typo trap."""
        self._write('skill = ["belt-and-suspenders"]\n')
        with self.assertRaises(sync_rules.ManifestError):
            sync_rules.load_skill_manifest(self.project)

    def test_missing_skills_key_raises(self) -> None:
        self._write("# nothing here\n")
        with self.assertRaises(sync_rules.ManifestError):
            sync_rules.load_skill_manifest(self.project)

    def test_non_string_entries_raise(self) -> None:
        self._write("skills = [3]\n")
        with self.assertRaises(sync_rules.ManifestError):
            sync_rules.load_skill_manifest(self.project)

    def test_duplicate_entries_raise(self) -> None:
        self._write('skills = ["impl-worktree", "impl-worktree"]\n')
        with self.assertRaises(sync_rules.ManifestError):
            sync_rules.load_skill_manifest(self.project)


class VerifyProjectTests(unittest.TestCase):
    """`verify_project` — manifest present/absent x link present/missing/wrong."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.project = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.claude = self.project / ".claude"

    def _declare(self, skills: list[str]) -> None:
        target = sync_rules.manifest_path(self.project)
        target.parent.mkdir(parents=True, exist_ok=True)
        rendered = ", ".join(f'"{s}"' for s in skills)
        target.write_text(f"skills = [{rendered}]\n", encoding="utf-8")

    def _link_rules_and_agents(self) -> None:
        """Everything except skills, so a skill finding is isolated."""
        for name in sync_rules.UNIVERSAL_RULE_FILES:
            target = self.claude / "rules" / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.symlink_to(sync_rules.RULES_DIR / name)
        for name in sync_rules._universal_agent_files():
            target = self.claude / "agents" / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.symlink_to(sync_rules.AGENTS_DIR / name)

    def _link_skill(self, name: str) -> None:
        target = self.claude / "skills" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.symlink_to(sync_rules.SKILLS_DIR / name, target_is_directory=True)

    def test_absent_manifest_is_never_green(self) -> None:
        """A fully-linked checkout still fails, because nothing declares intent.

        This is the load-bearing assertion of harmonic-forge#541: were the
        absent case to return EXIT_OK, `--verify` would report green in
        precisely the nine checkouts #540 measured broken.
        """
        self._link_rules_and_agents()
        self.assertEqual(
            sync_rules.verify_project(self.project), sync_rules.EXIT_CANNOT_RUN
        )

    def test_absent_manifest_is_distinct_from_drift(self) -> None:
        """Undeclared and broken are different states with different exit codes."""
        self.assertNotEqual(sync_rules.EXIT_CANNOT_RUN, sync_rules.EXIT_DRIFT)
        self.assertNotEqual(sync_rules.EXIT_CANNOT_RUN, sync_rules.EXIT_OK)

    def test_declared_and_linked_verifies(self) -> None:
        self._declare(["_stub"])
        self._link_rules_and_agents()
        self._link_skill("_stub")
        self.assertEqual(sync_rules.verify_project(self.project), sync_rules.EXIT_OK)

    def test_declared_but_missing_link_reports_drift(self) -> None:
        self._declare(["_stub"])
        self._link_rules_and_agents()
        self.assertEqual(sync_rules.verify_project(self.project), sync_rules.EXIT_DRIFT)

    def test_link_pointing_at_the_wrong_target_reports_drift(self) -> None:
        """Identity, not plausibility — a foreign dir with a SKILL.md is not the skill."""
        self._declare(["_stub"])
        self._link_rules_and_agents()
        decoy = self.project / "decoy"
        decoy.mkdir()
        (decoy / "SKILL.md").write_text("not the platform's\n", encoding="utf-8")
        target = self.claude / "skills" / "_stub"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.symlink_to(decoy, target_is_directory=True)
        self.assertEqual(sync_rules.verify_project(self.project), sync_rules.EXIT_DRIFT)

    def test_missing_rule_link_reports_drift_even_with_skills_correct(self) -> None:
        self._declare(["_stub"])
        self._link_rules_and_agents()
        self._link_skill("_stub")
        (self.claude / "rules" / sync_rules.UNIVERSAL_RULE_FILES[0]).unlink()
        self.assertEqual(sync_rules.verify_project(self.project), sync_rules.EXIT_DRIFT)

    def test_empty_declaration_with_links_verifies(self) -> None:
        self._declare([])
        self._link_rules_and_agents()
        self.assertEqual(sync_rules.verify_project(self.project), sync_rules.EXIT_OK)

    def test_malformed_manifest_cannot_run(self) -> None:
        target = sync_rules.manifest_path(self.project)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("skills = [oops\n", encoding="utf-8")
        self._link_rules_and_agents()
        self.assertEqual(
            sync_rules.verify_project(self.project), sync_rules.EXIT_CANNOT_RUN
        )

    def test_verify_creates_nothing(self) -> None:
        """AC1's 'without linking or mutating anything'.

        `link_project` calls mkdir(parents=True); the verify path must not, or
        a checkout with no .claude/ at all would be silently given one — four of
        #540's nine were in exactly that state.
        """
        before = sorted(p.name for p in self.project.iterdir())
        sync_rules.verify_project(self.project)
        self.assertEqual(sorted(p.name for p in self.project.iterdir()), before)
        self.assertFalse(self.claude.exists())


class CliSurfaceTests(unittest.TestCase):
    """`--verify` is reachable and documented; `--pull` is untouched."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.project = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_verify_appears_in_help(self) -> None:
        """The regression that hid verify_links() for its whole life: the
        functions existed and were reachable only as a side effect of --project,
        so `--verify` appeared zero times in --help."""
        import contextlib
        import io

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            with self.assertRaises(SystemExit):
                sync_rules.main(["--help"])
        self.assertIn("--verify", buffer.getvalue())

    def test_verify_without_project_cannot_run(self) -> None:
        self.assertEqual(
            sync_rules.main(["--verify"]), sync_rules.EXIT_CANNOT_RUN
        )

    def test_verify_on_nonexistent_path_cannot_run(self) -> None:
        self.assertEqual(
            sync_rules.main(
                ["--verify", "--project", str(self.project / "nope")]
            ),
            sync_rules.EXIT_CANNOT_RUN,
        )

    def test_verify_via_cli_does_not_link(self) -> None:
        code = sync_rules.main(["--verify", "--project", str(self.project)])
        self.assertEqual(code, sync_rules.EXIT_CANNOT_RUN)
        self.assertFalse((self.project / ".claude").exists())

    def test_no_arguments_cannot_run(self) -> None:
        import contextlib
        import io

        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(sync_rules.main([]), sync_rules.EXIT_CANNOT_RUN)


if __name__ == "__main__":
    unittest.main()
