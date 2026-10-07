"""harmonic-forge#917 AC5 -- `forge-onboard`'s `skills` check, hermetic.

`sync_rules` is a stand-in module, so the check's own logic is what is under
test: an absent manifest FAILs, an unreadable one FAILs, an unlinked declared
skill FAILs with the verifier's own line, and a linked or empty declaration
passes. The live AC5 run against the real checkouts is in the issue thread.
"""
import sys
import tempfile
import types
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import forge_onboard as fo  # noqa: E402
import forge_onboard_skills as fos  # noqa: E402
from manifest import Project  # noqa: E402


class _ManifestError(RuntimeError):
    pass


def _sync_rules(declared, linked=True, raises=None):
    module = types.SimpleNamespace(ManifestError=_ManifestError)

    def load_skill_manifest(_root):
        if raises:
            raise _ManifestError(raises)
        return declared

    def verify_skill_dir(_target, _names):
        if not linked:
            print("[MISSING] .claude/skills/belt-and-suspenders", file=sys.stderr)
        return linked

    def verify_links(_root, _names):
        raise AssertionError("rules and agents are check_directives' job")

    module.load_skill_manifest = load_skill_manifest
    module.expected_skill_names = lambda declared_names: sorted(declared_names or [])
    module.verify_links = verify_links
    module._verify_skill_dir = verify_skill_dir
    return module


class SkillsCheck(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.checkout = Path(self._tmp.name) / "repo"
        self.checkout.mkdir()
        self.project = Project(name="repo", prefix="R", path=str(self.checkout),
                               onboarded=True, workspace="vh")

    def run_check(self, sync_rules, project=None):
        return fos.check_skills(project or self.project, fo.Check, Path("/unused"),
                                sync_rules=sync_rules)

    def test_no_manifest_fails(self):
        result = self.run_check(_sync_rules(None))
        self.assertEqual(result.status, fo.FAIL)
        self.assertIn("platform-skills.toml", result.detail)

    def test_an_unlinked_declared_skill_fails_naming_it(self):
        result = self.run_check(_sync_rules(["belt-and-suspenders"], linked=False))
        self.assertEqual(result.status, fo.FAIL)
        self.assertIn("belt-and-suspenders", result.detail)

    def test_an_unreadable_manifest_fails(self):
        result = self.run_check(_sync_rules(None, raises="conflict marker"))
        self.assertEqual(result.status, fo.FAIL)
        self.assertIn("conflict marker", result.detail)

    def test_linked_skills_pass(self):
        result = self.run_check(_sync_rules(["belt-and-suspenders", "sprint-plan"]))
        self.assertEqual((result.status, result.detail),
                         (fo.OK, "2 declared skill(s) linked"))

    def test_an_explicit_empty_declaration_passes(self):
        self.assertEqual(self.run_check(_sync_rules([])).status, fo.OK)

    def test_no_checkout_skips(self):
        project = Project(name="p", prefix="P")
        self.assertEqual(self.run_check(_sync_rules(None), project).status, fo.SKIP)

    def test_a_broken_rule_link_is_not_a_skills_failure(self):
        """The fake's verify_links raises; the check must never call it."""
        self.assertEqual(self.run_check(_sync_rules(["sprint-plan"])).status, fo.OK)

    def test_the_check_is_registered(self):
        self.assertIn(fo.check_skills, fo.CHECKS)


if __name__ == "__main__":
    unittest.main()
