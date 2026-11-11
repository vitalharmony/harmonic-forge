"""Tests for the effect registry (harmonic-forge#1290), one class per decision."""
from __future__ import annotations

import json
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[0] / "gh"))
sys.path.insert(0, str(HERE.parents[0] / "precheck"))

import _preflight  # noqa: E402
import effects  # noqa: E402
import replay_harness  # noqa: E402

REGISTRY = effects.load()


def classify(command: str):
    import shlex
    return effects.classify_outside(shlex.split(command), REGISTRY)


class Fixture:
    """A throwaway forge root: files, plus a registry file naming them."""

    def __init__(self, files: dict[str, str], registry: str):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        for rel, text in files.items():
            path = self.root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        self.base = self.root / "effects.toml"
        self.base.write_text(textwrap.dedent(registry), encoding="utf-8")

    def load(self, repo_root=None):
        return effects.load(repo_root, base=self.base, forge_root=self.root)

    def close(self):
        self._tmp.cleanup()


SCRIPT = "#!/usr/bin/env python3\nprint('x')\nif __name__ == \"__main__\":\n    pass\n"


def fixture(registry: str, files: dict[str, str] | None = None) -> Fixture:
    return Fixture(files if files is not None else {"tools/gh/t.py": SCRIPT}, registry)


def tool(modes: str, path: str = "tools/gh/t.py", extra: str = "") -> str:
    return f'[[tool]]\npath = "{path}"\n{extra}\n' + textwrap.dedent(modes)


class TestDeclaration(unittest.TestCase):
    """Decision 4: every non-test script is a tool, a library or data."""

    def test_forge_scripts_are_all_declared(self):
        self.assertEqual(effects.declaration_problems(REGISTRY), [])

    def test_undeclared_script_fails(self):
        fx = fixture(tool('[[tool.mode]]\nname = "m"\nmatch = []\nclass = "read"\ncite = ["tools/gh/t.py:1"]\n'),
                     {"tools/gh/t.py": SCRIPT, "tools/lane/new_tool.py": SCRIPT,
                      "tools/gh/test_t.py": "", "tools/gh/README.md": ""})
        self.addCleanup(fx.close)
        problems = effects.declaration_problems(fx.load(), fx.root)
        self.assertEqual(problems, ["undeclared script: tools/lane/new_tool.py"])

    def test_main_block_declared_as_library_fails(self):
        fx = fixture('[[library]]\npath = "tools/gh/t.py"\n')
        self.addCleanup(fx.close)
        problems = effects.declaration_problems(fx.load(), fx.root)
        self.assertEqual(len(problems), 1)
        self.assertIn("__main__ block", problems[0])

    def test_extensionless_declared_as_library_fails(self):
        fx = fixture('[[library]]\npath = "tools/lane/runme"\n', {"tools/lane/runme": "#!/bin/sh\n"})
        self.addCleanup(fx.close)
        problems = effects.declaration_problems(fx.load(), fx.root)
        self.assertEqual(len(problems), 1)
        self.assertIn("extensionless", problems[0])

    def test_sh_library_names_its_source_site(self):
        files = {"tools/lane/_lib.sh": "f() { :; }\n"}
        fx = fixture('[[library]]\npath = "tools/lane/_lib.sh"\n', files)
        self.addCleanup(fx.close)
        with self.assertRaisesRegex(effects.RegistryError, "sourced_by"):
            fx.load()
        ok = fixture('[[library]]\npath = "tools/lane/_lib.sh"\nsourced_by = "tools/lane/_lib.sh:1"\n', files)
        self.addCleanup(ok.close)
        self.assertEqual(effects.declaration_problems(ok.load(), ok.root), [])


class TestGhReadShape(unittest.TestCase):
    """Decision 4: a gh call is a read only on `_gh_permitted`'s allowlist."""

    def test_field_without_method_is_external_publish(self):
        verdict = classify("gh api -f x=y repos/o/r/issues")
        self.assertEqual((verdict.family, verdict.klass), ("gh", "external-publish"))

    def test_input_is_external_publish(self):
        verdict = classify("gh api --input f repos/o/r/issues")
        self.assertEqual((verdict.family, verdict.klass), ("gh", "external-publish"))

    def test_plain_api_read_is_read(self):
        verdict = classify("gh api repos/o/r/issues")
        self.assertEqual((verdict.family, verdict.klass, verdict.read), ("gh", "read", True))

    def test_verdict_equals_the_hooks_allowlist(self):
        cases = [["api", "repos/o/r"], ["api", "-X", "POST", "repos/o/r"], ["api", "-XPOST", "x"],
                 ["issue", "view", "1"], ["issue", "edit", "1"], ["pr", "create"], ["pr", "list"],
                 ["search", "issues", "x"], ["workflow", "run", "ci"], ["release", "create", "v1"],
                 ["api", "graphql", "-f", "query=mutation { x }"], ["api", "graphql", "-f", "query=query { x }"],
                 ["-R", "o/r", "issue", "view", "1"], []]
        for args in cases:
            with self.subTest(args=args):
                verdict = classify(" ".join(["gh", *[f"'{a}'" for a in args]]))
                self.assertEqual(verdict.read, effects.gh_hook._gh_permitted(args))


class TestFamilyMatching(unittest.TestCase):
    """Decision 4: the family is never read from a token's position."""

    def assertFamily(self, command, family, klass=None):
        verdict = classify(command)
        self.assertIsNotNone(verdict, command)
        self.assertEqual(verdict.family, family, command)
        if klass:
            self.assertEqual(verdict.klass, klass, command)
        return verdict

    def test_git_push_spellings(self):
        for command in ("git push", "git -C x push", "git -c k=v push", "env A=1 git push",
                        "A=1 git push origin main", "/usr/bin/git push"):
            self.assertFamily(command, "git push", "external-publish")

    def test_gh_pr_merge_spellings(self):
        for command in ("gh pr merge 5", "gh -R o/r pr merge 5", "gha vh pr merge 5",
                        "gh-as vh gh pr merge 5", "gh api -X PUT repos/o/r/pulls/5/merge"):
            verdict = self.assertFamily(command, "gh pr merge", "irreversible")
            self.assertIn("batch_gate.py", verdict.guard)
            self.assertEqual(verdict.guard_kind, "hook")

    def test_gh_issue_close_is_guarded(self):
        for command in ("gh issue close 5", "gh --repo o/r issue close 5"):
            verdict = self.assertFamily(command, "gh issue close", "external-publish")
            self.assertIn("batch_gate.py", verdict.guard)

    def test_shell_escape(self):
        self.assertFamily("bash -c 'git push'", "git push", "external-publish")
        self.assertFamily("sh -c 'cd x && gh -R o/r pr merge 5'", "gh pr merge", "irreversible")
        self.assertFamily("xargs git push", "git push", "external-publish")
        # Fail-closed: a gh name inside a shell escape takes gh's highest family.
        self.assertFamily("bash -c 'gh issue view 1'", "gh pr merge", "irreversible")
        self.assertFamily("eval 'curl -s https://x'", "curl, wget", "external-publish")

    def test_ungated(self):
        for command in ("git status", "ls", "git log --oneline", "python3 x.py"):
            self.assertIsNone(classify(command), command)


class TestCurlWget(unittest.TestCase):
    """Decision 4: curl and wget read only on the flag allowlist."""

    def test_reads(self):
        for command in ("curl https://x", "curl -sSfL https://x", "curl -o out --max-time=5 https://x",
                        "wget -q https://x", "wget -nv -O f https://x"):
            self.assertEqual(classify(command).klass, "read", command)

    def test_writes(self):
        for command in ("curl -d x https://x", "curl -sXPOST https://x", "curl --json x https://x",
                        "curl --frobnicate https://x", "wget --post-data=x https://x",
                        "curl -H 'A: b' https://x", "curl https://x https://y", "curl"):
            verdict = classify(command)
            self.assertEqual((verdict.family, verdict.klass), ("curl, wget", "external-publish"), command)


class TestPodman(unittest.TestCase):
    def test_read_and_write(self):
        self.assertEqual(classify("podman ps").klass, "read")
        self.assertEqual(classify("docker inspect x").klass, "read")
        for command in ("podman --root ps rm x", "podman rm x", "docker run x", "podman"):
            verdict = classify(command)
            self.assertEqual((verdict.family, verdict.klass), ("podman, docker", "shared-state"), command)


class TestUnmatchedCall(unittest.TestCase):
    """Decision 3: a call no mode matches takes the tool's highest-class mode."""

    MODES = """
    [[tool.mode]]
    name = "low"
    match = ["low"]
    class = "local-write"
    cite = ["tools/gh/t.py:1"]
    [[tool.mode]]
    name = "high"
    match = ["high"]
    class = "{high}"
    gap = "g"
    cite = ["tools/gh/t.py:1"]
    [[tool.mode]]
    name = "mid"
    match = ["mid"]
    class = "shared-state"
    gap = "g"
    cite = ["tools/gh/t.py:1"]
    """

    def test_plain_tool(self):
        fx = fixture(tool(self.MODES.replace("{high}", "external-publish")))
        self.addCleanup(fx.close)
        self.assertEqual(effects.resolve_mode(fx.load(), "tools/gh/t.py", ["other"])["name"], "high")
        self.assertEqual(effects.resolve_mode(fx.load(), "tools/gh/t.py", ["low"])["name"], "low")

    def test_wrapper(self):
        fx = fixture(tool(self.MODES.replace("{high}", "irreversible"), extra="wrapper = true"))
        self.addCleanup(fx.close)
        self.assertEqual(effects.resolve_mode(fx.load(), "tools/gh/t.py", ["--", "gh", "x"])["name"], "high")

    def test_exact_match_is_exact(self):
        mode = effects.resolve_mode(REGISTRY, "tools/gh/gh_shim", ["auth", "token"])
        self.assertEqual(mode["class"], "read")
        mode = effects.resolve_mode(REGISTRY, "tools/gh/gh_shim", ["auth", "token", "--extra"])
        self.assertEqual(mode["class"], "shared-state")

    def test_alias_resolves_to_its_target(self):
        mode = effects.resolve_mode(REGISTRY, "tools/lane/lane1-rebase", ["repo", "1", "b"])
        self.assertEqual(mode["name"], "run")


class TestSchema(unittest.TestCase):
    """Decision 6 and receipt binding: the loader refuses, never defaults."""

    def refuses(self, modes: str, pattern: str):
        fx = fixture(tool(modes))
        self.addCleanup(fx.close)
        with self.assertRaisesRegex(effects.RegistryError, pattern):
            fx.load()

    BASE_MODE = '[[tool.mode]]\nname = "m"\nmatch = []\ncite = ["tools/gh/t.py:1"]\n'

    def test_gated_mode_needs_precheck_guard_or_gap(self):
        self.refuses(self.BASE_MODE + 'class = "shared-state"\n', "exactly one of")

    def test_gap_and_guard_together_refuse(self):
        self.refuses(self.BASE_MODE + 'class = "shared-state"\ngap = "g"\nguard_kind = "hook"\nguard = "h"\n',
                     "exactly one of")

    def test_unknown_class_refuses(self):
        self.refuses(self.BASE_MODE + 'class = "mostly-read"\n', "unknown class")

    def test_unknown_guard_kind_refuses(self):
        self.refuses(self.BASE_MODE + 'class = "shared-state"\nguard_kind = "vibes"\nguard = "h"\n',
                     "unknown guard_kind")

    def test_catch_all_mode_must_be_last(self):
        self.refuses(self.BASE_MODE + 'class = "read"\n' + self.BASE_MODE.replace('"m"', '"n"').replace(
            "[]", '["x"]') + 'class = "read"\n', "must be last")

    def test_citation_past_end_refuses(self):
        self.refuses(self.BASE_MODE.replace(":1", ":99") + 'class = "read"\n', "past the end")

    PRECHECK = ('[[tool.mode]]\nname = "check"\nmatch = ["--check"]\nclass = "local-write"\n'
                'cite = ["tools/gh/t.py:1"]\n' + BASE_MODE + 'class = "shared-state"\nprecheck = "check"\n')

    def test_precheck_without_binds_refuses(self):
        self.refuses(self.PRECHECK, "needs binds")

    def test_unknown_binds_token_refuses(self):
        self.refuses(self.PRECHECK + 'binds = ["vibes"]\nbinds_cite = "tools/gh/t.py:1"\n', "binds must be")

    def test_binds_without_cite_refuses(self):
        self.refuses(self.PRECHECK + 'binds = ["head_sha"]\n', "binds_cite")

    def test_empty_binds_is_the_ceiling(self):
        fx = fixture(tool(self.PRECHECK + "binds = []\n"))
        self.addCleanup(fx.close)
        self.assertEqual(fx.load()["tools"][0]["modes"][1]["binds"], [])

    def test_precheck_must_name_another_mode(self):
        self.refuses(self.BASE_MODE + 'class = "shared-state"\nprecheck = "nope"\nbinds = []\n',
                     "names no other mode")

    def test_validates_shell_or_provenance_refuses(self):
        self.refuses(self.PRECHECK + 'binds = []\nvalidates = [{class = "provenance", cite = "tools/gh/t.py:1"}]\n',
                     "never claimed")

    def test_gaps_lists_exactly_the_gap_entries(self):
        rows = effects.gaps(REGISTRY).splitlines()[2:]
        expected = sum(1 for t in REGISTRY["tools"] for m in t["modes"] if "gap" in m) + \
            sum(1 for o in REGISTRY["outside"] if "gap" in o)
        self.assertEqual(len(rows), expected)
        for tool_entry in REGISTRY["tools"]:
            for mode in tool_entry["modes"]:
                listed = any(r.startswith(f"| `{Path(tool_entry['path']).name}` | {mode['name']} |") for r in rows)
                self.assertEqual(listed, "gap" in mode, (tool_entry["path"], mode["name"]))
        for entry in REGISTRY["outside"]:
            self.assertEqual(any(r.startswith(f"| `{entry['family']}` |") for r in rows), "gap" in entry)
        for tool_entry in REGISTRY["tools"]:
            for mode in tool_entry["modes"]:
                if effects.RANK[mode["class"]] >= effects.GATED and "gap" not in mode:
                    self.assertTrue("precheck" in mode or "guard" in mode)


class TestExtension(unittest.TestCase):
    """Decision 7: a repo extension adds tools; it never redefines or adds outside entries."""

    def repo(self, text: str) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / ".claude").mkdir()
        (root / "scripts").mkdir()
        (root / "scripts" / "gate.py").write_text(SCRIPT, encoding="utf-8")
        (root / ".claude" / "effects.toml").write_text(textwrap.dedent(text), encoding="utf-8")
        return root

    MODE = '[[tool.mode]]\nname = "m"\nmatch = []\nclass = "read"\ncite = ["scripts/gate.py:1"]\n'

    def test_no_extension_is_the_base(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.assertEqual(len(effects.load(tmp.name)["tools"]), len(REGISTRY["tools"]))

    def test_extension_adds_a_tool(self):
        root = self.repo(tool(self.MODE, path="scripts/gate.py"))
        registry = effects.load(root)
        self.assertEqual(len(registry["tools"]), len(REGISTRY["tools"]) + 1)
        self.assertEqual(effects.resolve_mode(registry, "scripts/gate.py", [])["class"], "read")
        self.assertEqual(effects.declaration_problems(registry, root, dirs=()), [])

    def test_redefining_a_forge_tool_refuses(self):
        root = self.repo(tool(self.MODE.replace("scripts/gate.py:1", "tools/gh/l1_post.py:1"),
                              path="tools/gh/l1_post.py"))
        with self.assertRaisesRegex(effects.RegistryError, "declared twice"):
            effects.load(root)

    def test_outside_table_refuses(self):
        root = self.repo('[[outside]]\nfamily = "rm"\nprograms = ["rm"]\nclass = "irreversible"\n'
                         'read_shape = "none"\ngap = "g"\n')
        with self.assertRaisesRegex(effects.RegistryError, r"\[\[tool\]\] and \[\[library\]\] only"):
            effects.load(root)


class TestGenerated(unittest.TestCase):
    """Decision 1 and AC2: the generated artifacts equal a fresh render."""

    def test_preflight_table_equals_inventory(self):
        lines = _preflight.__doc__.splitlines()
        start = lines.index("| Tool | Mode | Class | Precheck, guard or gap |")
        table = []
        for line in lines[start:]:
            if not line.startswith("|"):
                break
            table.append(line)
        self.assertEqual("\n".join(table), effects.inventory(REGISTRY),
                         "regenerate with: python3 tools/registry/effects.py inventory")

    def test_committed_layer_set_equals_a_fresh_render(self):
        committed = (effects.FORGE_ROOT / "tools/precheck/layersets/registry-v1.json").read_text(encoding="utf-8")
        self.assertEqual(committed, effects.layer_set_document(REGISTRY),
                         "regenerate with: python3 tools/registry/effects.py layerset")

    def test_layer_set_shape(self):
        path = effects.FORGE_ROOT / "tools/precheck/layersets/registry-v1.json"
        parsed = replay_harness.load_layer_set(path)
        design = json.loads(effects.DESIGN_LAYER_SET.read_text(encoding="utf-8"))
        document = json.loads(path.read_text(encoding="utf-8"))
        gate_index = [layer["name"] for layer in design["layers"]].index("gate")
        layer = document["layers"][gate_index]
        self.assertEqual((layer["name"], layer["claims_basis"], layer["module"]), ("registry", "validated", None))
        self.assertEqual(layer["bar"], design["layers"][gate_index]["bar"])
        self.assertEqual([lay["name"] for lay in parsed["layers"]], ["lint", "registry", "provenance"])
        cited = {f"{tool}|{klass}" for tool, klass, cite in effects.claims(REGISTRY) if cite}
        for claim in layer["claims"]:
            self.assertNotIn(claim.split("|")[1], effects.NEVER_CLAIMED)
            self.assertIn(claim, cited)
        self.assertIn("gh-pr-merge|receipt", layer["claims"])
        self.assertIn("git-push|receipt", layer["claims"])

    def test_cli(self):
        import contextlib
        import io
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(effects.main(["validate"]), 0)
        self.assertIn("effects: valid", out.getvalue())


if __name__ == "__main__":
    unittest.main()
