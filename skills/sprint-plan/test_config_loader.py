import json
import tempfile
import unittest
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).parent))
import config_loader as loader

HOME = {"engagement":"x","doc_paths":["docs/PRIORITIES.md"],"board_owner":"x","board_fields":{"priority":"Priority","sequence":"Sequence","tier":"Tier"},"repos":[{"prefix":"H","repo":"x/hrse","short":"hrse","board":1,"default":True}]}

class LoaderTests(unittest.TestCase):
 def write(self, root, value, name=".claude/sprint-plan.config.json"):
  path=root/name; path.parent.mkdir(parents=True,exist_ok=True); path.write_text(json.dumps(value)); return path
 def test_override_wins(self):
  with tempfile.TemporaryDirectory() as d:
   root=Path(d); path=self.write(root, HOME); self.assertEqual(loader.resolve(root, str(path))["engagement"],"x")
 def test_upward_and_tier(self):
  with tempfile.TemporaryDirectory() as d:
   root=Path(d); self.write(root, HOME); child=root/"a/b"; child.mkdir(parents=True); self.assertEqual(loader.resolve(child)["board_fields"]["tier"],"Tier")
 def test_lane_rejected(self):
  with tempfile.TemporaryDirectory() as d:
   root=Path(d)/"thing-lane2"; root.mkdir(); self.write(root, HOME); self.assertRaises(loader.ConfigError, loader.resolve, root)
 def test_empty_docs_and_defaults_rejected(self):
  with tempfile.TemporaryDirectory() as d:
   root=Path(d); bad=dict(HOME); bad["doc_paths"]=[]; path=self.write(root,bad); self.assertRaises(loader.ConfigError, loader.resolve, root)
 def test_member_indirection(self):
  with tempfile.TemporaryDirectory() as d:
   base=Path(d); home=base/"home"; member=base/"member"; home.mkdir(); member.mkdir()
   self.write(home, HOME); self.write(member,{"engagement":"x","home_repo":"x/hrse"})
   self.write(member,{"home_checkout":str(home)},".claude/sprint-plan.local.json")
   self.assertEqual(loader.resolve(member)["engagement"],"x")
 def test_missing_field_names_path(self):
  with tempfile.TemporaryDirectory() as d:
   root=Path(d); bad=dict(HOME); bad.pop("repos"); self.write(root,bad)
   with self.assertRaisesRegex(loader.ConfigError, r"\$\.repos"): loader.resolve(root)
 def test_zero_and_multiple_defaults_rejected(self):
  with tempfile.TemporaryDirectory() as d:
   root=Path(d); bad=dict(HOME); bad["repos"]=[dict(HOME["repos"][0], default=False)]; self.write(root,bad)
   self.assertRaises(loader.ConfigError, loader.resolve, root)
   bad["repos"]=[dict(HOME["repos"][0]), dict(HOME["repos"][0], prefix="F")]; self.write(root,bad)
   self.assertRaises(loader.ConfigError, loader.resolve, root)
 def test_invalid_override_is_named(self):
  with tempfile.TemporaryDirectory() as d:
   with self.assertRaisesRegex(loader.ConfigError, "invalid override path"):
    loader.resolve(Path(d), "missing.json")
 def test_no_config(self):
  with tempfile.TemporaryDirectory() as d: self.assertRaises(loader.ConfigError, loader.resolve, Path(d))
if __name__ == "__main__": unittest.main()
