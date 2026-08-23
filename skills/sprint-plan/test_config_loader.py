import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import config_loader as loader


HOME = {
    "engagement": "x",
    "doc_paths": ["docs/PRIORITIES.md"],
    "board_owner": "x",
    "board_fields": {"priority": "Priority", "sequence": "Sequence", "tier": "Tier"},
    "repos": [{"prefix": "H", "repo": "x/hrse", "short": "hrse", "board": 1, "default": True}],
}


class LoaderTests(unittest.TestCase):
    def write(self, root, value, name=".claude/sprint-plan.config.json"):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))
        return path

    def test_override_wins(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = self.write(root, HOME)
            self.assertEqual(loader.resolve(root, str(path))["engagement"], "x")

    def test_upward_and_tier(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write(root, HOME)
            child = root / "a/b"
            child.mkdir(parents=True)
            self.assertEqual(loader.resolve(child)["board_fields"]["tier"], "Tier")

    def test_both_lane_suffixes_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            for suffix in ("lane2", "lane3"):
                root = Path(directory) / f"thing-{suffix}"
                root.mkdir()
                self.write(root, HOME)
                with self.assertRaises(loader.ConfigError):
                    loader.resolve(root)

    def test_empty_docs_and_defaults_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bad = dict(HOME)
            bad["doc_paths"] = []
            self.write(root, bad)
            with self.assertRaises(loader.ConfigError):
                loader.resolve(root)

    def test_member_indirection(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            home = base / "home"
            member = base / "member"
            home.mkdir()
            member.mkdir()
            self.write(home, HOME)
            self.write(member, {"engagement": "x", "home_repo": "x/hrse"})
            self.write(member, {"home_checkout": str(home)}, ".claude/sprint-plan.local.json")
            self.assertEqual(loader.resolve(member)["engagement"], "x")

    def test_missing_field_names_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bad = dict(HOME)
            bad.pop("repos")
            self.write(root, bad)
            with self.assertRaisesRegex(loader.ConfigError, r"\$\.repos"):
                loader.resolve(root)

    def test_schema_rejects_unknown_home_field(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bad = dict(HOME, unexpected=True)
            self.write(root, bad)
            with self.assertRaisesRegex(loader.ConfigError, r"\$\.unexpected"):
                loader.resolve(root)

    def test_schema_rejects_invalid_nested_property(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bad = dict(HOME)
            bad["repos"] = [dict(HOME["repos"][0], board=0)]
            self.write(root, bad)
            with self.assertRaisesRegex(loader.ConfigError, r"\$\.repos\[0\]\.board"):
                loader.resolve(root)

    def test_schema_requires_absolute_local_checkout(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "local.json"
            with self.assertRaisesRegex(loader.ConfigError, r"\$\.home_checkout"):
                loader.validate({"home_checkout": "relative"}, path)

    def test_schema_is_loadable_and_defines_all_shapes(self):
        schema = json.loads(loader.SCHEMA.read_text())
        self.assertEqual(schema["$schema"], "https://json-schema.org/draft/2020-12/schema")
        self.assertEqual({shape["title"] for shape in schema["oneOf"]}, {"home config", "member config", "local config"})

    def test_zero_and_multiple_defaults_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bad = dict(HOME)
            bad["repos"] = [dict(HOME["repos"][0], default=False)]
            self.write(root, bad)
            with self.assertRaises(loader.ConfigError):
                loader.resolve(root)
            bad["repos"] = [dict(HOME["repos"][0]), dict(HOME["repos"][0], prefix="F")]
            self.write(root, bad)
            with self.assertRaises(loader.ConfigError):
                loader.resolve(root)

    def test_invalid_override_is_named(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(loader.ConfigError, "invalid override path"):
                loader.resolve(Path(directory), "missing.json")

    def test_no_config(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(loader.ConfigError):
                loader.resolve(Path(directory))


if __name__ == "__main__":
    unittest.main()
