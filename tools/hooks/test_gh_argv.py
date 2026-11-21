#!/usr/bin/env python3
"""The deny-by-default gh argv classifier (harmonic-forge#1447 reforge).

Required tests 1-5 and 7 of the sticky-wicket prescription: the corpus, the
generated spelling property test, the GH_DEBUG=api oracle, the pinned
flag-table freshness test (with the shim's generated builtins), and the
normal-traffic corpus read from `tools/`. Test 6 (fail closed through the
real shim) is in `tools/gh/test_gh_shim_close_gate.py`.
"""
from __future__ import annotations

import itertools
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import gen_gh_flag_table  # noqa: E402
import gh_argv  # noqa: E402
import gh_traffic  # noqa: E402

CORPUS = HERE / "gh_argv_corpus.json"
BASE_ENV = {"GH_REPO": "", "GH_HOST": ""}


def _verdict(argv: list[str], env: dict | None = None) -> gh_argv.Verdict:
    return gh_argv.decide(argv, {**BASE_ENV, **(env or {})})


class Corpus(unittest.TestCase):
    """Required test 1 (and 7: the pass-1 fixes are rows)."""

    def test_every_row(self) -> None:
        rows = json.loads(CORPUS.read_text())["rows"]
        self.assertGreater(len(rows), 50)
        for row in rows:
            with self.subTest(argv=row["argv"], note=row["note"]):
                verdict = _verdict(row["argv"], row.get("env"))
                self.assertEqual(verdict.action, row["expect"], verdict.reason)
                if row["expect"] == "gate":
                    target = verdict.target
                    self.assertEqual([target.repo, target.number, target.kind, target.exempt],
                                     row["target"])


METHODS = (["-X", "{M}"], ["-X{M}"], ["--method", "{M}"], ["--method={M}"], ["-iX{M}"], ["-iX", "{M}"],
           ["-X={M}"], [])
REST_ENDPOINTS = ("repos/o/r/{P}", "/repos/o/r/{P}", "https://api.github.com/repos/o/r/{P}",
                  "repos/{owner}/{repo}/{P}", "repositories/123/{P}", "repos/o/r/{P}?a=b",
                  "repos/o/r/{P}/", "REPOS/o/r/{P}")
GRAPHQL_ENDPOINTS = ("graphql", "/graphql", "https://api.github.com/graphql", "graphql/", "GraphQL",
                     "graphql?a=b")


def _fields(key: str, value: str) -> list[list[str]]:
    kv = f"{key}={value}"
    return [["-f", kv], ["-F", kv], [f"-f{kv}"], [f"-F{kv}"], [f"-iF{kv}"], [f"--field={kv}"],
            [f"--raw-field={kv}"], ["--field", kv], ["-F", f"{key}=@f"], ["--input", "body.json"],
            ["--input=-"]]


def generated(kind: str) -> list[list[str]]:
    """Required test 2's cross product for one closing call."""
    out = []
    if kind == "close":
        endpoints, methods, fields = REST_ENDPOINTS, ("PATCH", "patch", "POST", "PUT"), _fields("state", "closed")
        path = "issues/5"
    elif kind == "merge":
        endpoints, methods, fields = REST_ENDPOINTS, ("PUT", "put", "POST"), [*_fields("sha", "a"), []]
        path = "pulls/7/merge"
    else:
        name = "closeIssue" if kind == "graphql-close" else "mergePullRequest"
        doc = f'mutation {{ {name}(input:{{id:"x"}}) {{ clientMutationId }} }}'
        endpoints, methods, fields = GRAPHQL_ENDPOINTS, ("POST", "post", "PATCH"), _fields("query", doc)
        path = ""
    for method_form, method, field, endpoint in itertools.product(METHODS, methods, fields, endpoints):
        if not method_form and not field:
            continue  # no method and no field is a GET
        method_tokens = [t.replace("{M}", method) for t in method_form]
        ep = endpoint.replace("{P}", path)
        for order in range(3):
            argv = ([*method_tokens, ep, *field], [ep, *field, *method_tokens], [*field, ep, *method_tokens])[order]
            out.append(["api", *argv])
    return out


class GeneratedSpellings(unittest.TestCase):
    """Required test 2: no spelling of a closing call is allowed."""

    def test_no_closing_spelling_is_allowed(self) -> None:
        for kind in ("close", "merge", "graphql-close", "graphql-merge"):
            argvs = generated(kind)
            self.assertGreater(len(argvs), 100)
            for env in (BASE_ENV, {"GH_REPO": "o/r", "GH_HOST": ""}):
                allowed = [a for a in argvs if gh_argv.decide(a, env).action == "allow"]
                with self.subTest(kind=kind, env=env):
                    self.assertEqual(allowed[:5], [])

    def test_bound_targets_are_the_path_number(self) -> None:
        for argv in generated("close"):
            verdict = gh_argv.decide(argv, {"GH_REPO": "o/r", "GH_HOST": ""})
            if verdict.action == "gate":
                self.assertEqual((verdict.target.repo, verdict.target.number), ("o/r", "5"), argv)


def _real_gh() -> str | None:
    found = gen_gh_flag_table.real_gh()
    return found if found and os.path.exists(found) else None


@unittest.skipUnless(_real_gh(), "gh_absent: no real gh binary on PATH")
class GhOracle(unittest.TestCase):
    """Required test 3: for a sample of generated REST argvs, the real gh
    (GH_DEBUG=api, --hostname nohost.invalid, a dummy token) sends the method
    and path the classifier computes. No request leaves the machine: a full
    URL is rewritten to nohost.invalid as well."""

    def test_sampled_argvs_match_gh(self) -> None:
        rng = random.Random(1447)
        pool = [a for kind in ("close", "merge") for a in generated(kind)
                if "repositories/123" not in " ".join(a)]
        sample = rng.sample(pool, 40)
        work = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, work, True)
        (work / "f").write_text("closed")
        (work / "body.json").write_text("{}")
        env = {k: v for k, v in os.environ.items() if not k.startswith(("GH_", "GITHUB_"))}
        env.update({"GH_DEBUG": "api", "GH_TOKEN": "x", "GH_ENTERPRISE_TOKEN": "x", "GH_REPO": "o/r",
                    "GH_CONFIG_DIR": str(work / "cfg"), "NO_COLOR": "1"})
        for argv in sample:
            argv = [t.replace("https://api.github.com/", "https://nohost.invalid/") for t in argv]
            run = [argv[0], "--hostname", "nohost.invalid", *argv[1:]]
            with self.subTest(argv=run):
                done = subprocess.run([_real_gh(), *run], capture_output=True, text=True, cwd=work, env=env,
                                      input="{}", timeout=30)
                sent = re.search(r"^> ([A-Za-z]+) (\S+) HTTP", done.stderr, re.M)
                self.assertIsNotNone(sent, done.stderr[-500:])
                self.assertIn("nohost.invalid", done.stderr)
                parsed = gh_argv.parse(run)
                self.assertEqual(gh_argv.effective_method(parsed), sent.group(1).upper())
                ep = gh_argv.normalize_endpoint(parsed.positionals[0], {"GH_REPO": "o/r"})
                path = sent.group(2).split("?", 1)[0]
                want = [s for s in path.split("/") if s]
                if want[:2] == ["api", "v3"]:
                    want = want[2:]
                self.assertEqual(list(ep.segments), want)


class FlagTableFreshness(unittest.TestCase):
    """Required test 4 and pitch change 3."""

    def test_shim_builtins_block_is_generated_from_the_table(self) -> None:
        table = gh_argv.load_table()
        shim = gen_gh_flag_table.SHIM.read_text()
        self.assertIn(gen_gh_flag_table.shim_block(table), shim)

    def test_table_matches_the_installed_gh_when_it_is_the_pinned_version(self) -> None:
        gh = _real_gh()
        if gh is None:
            self.skipTest("gh_absent: no real gh binary on PATH")
        pinned = gh_argv.load_table()["gh_version"]
        installed = gen_gh_flag_table.gh_version(gh)
        if installed != pinned:
            self.skipTest(f"gh_version_not_pinned: installed gh {installed} is not the pinned {pinned}")
        self.assertEqual(gen_gh_flag_table.build(gh), gh_argv.load_table(),
                         "the installed gh's grammar changed; run tools/hooks/gen_gh_flag_table.py")


class NormalTraffic(unittest.TestCase):
    """Required test 5: every gh argv shape the lane tools emit is allowed
    (or, for the tools that really close or merge, bound to a target)."""

    def test_every_tool_shape_is_allowed(self) -> None:
        shapes = gh_traffic.shapes()
        self.assertGreater(len(shapes), 80)
        refused = [(loc, argv, v.reason) for loc, argv in shapes
                   for v in [gh_argv.decide(argv, BASE_ENV)] if v.action == "refuse"]
        self.assertEqual(refused, [])

    def test_the_five_live_graphql_readers_are_found_and_allowed(self) -> None:
        """Pitch change 1: GraphQL reads with variables stay allowed."""
        shapes = dict((loc.rsplit(":", 1)[0] + ":" + argv[1], argv) for loc, argv in gh_traffic.shapes()
                      if argv[:2] == ["api", "graphql"])
        for caller in ("tools/gh/gh_issue.py", "tools/gh/item_list_cache.py", "tools/gh/board_fields.py",
                       "tools/gh/repo_hygiene.py"):
            argv = shapes.get(f"{caller}:graphql")
            with self.subTest(caller=caller):
                self.assertIsNotNone(argv)
                self.assertEqual(gh_argv.decide(argv, BASE_ENV).action, "allow")

    def test_the_board_query_documents_are_reads(self) -> None:
        """repo_hygiene passes its document as a parameter, which the static
        reader cannot resolve; classify the real constants."""
        sys.path.insert(0, str(HERE.parent / "gh"))
        import repo_hygiene  # noqa: PLC0415
        for doc in (repo_hygiene._BOARD_ITEMS_QUERY, repo_hygiene._BOARD_ITEMS_QUERY_ORG):
            argv = ["api", "graphql", "-f", f"query={doc}", "-F", "owner=o", "-F", "number=3",
                    "-F", "cursor=c"]
            self.assertEqual(gh_argv.decide(argv, BASE_ENV).action, "allow")


class ParserUnits(unittest.TestCase):
    def test_pflag_forms(self) -> None:
        parsed = gh_argv.parse(["api", "-iXPATCH", "--jq=.x", "-q", ".y", "e", "--", "-f"])
        self.assertEqual(parsed.flags, [("--include", "true"), ("--method", "PATCH"), ("--jq", ".x"),
                                        ("--jq", ".y")])
        self.assertEqual(parsed.positionals, ["e", "-f"])

    def test_value_flag_takes_a_dash_token(self) -> None:
        parsed = gh_argv.parse(["issue", "close", "5", "--comment", "--repo"])
        self.assertEqual(parsed.last("--comment"), "--repo")
        self.assertIsNone(parsed.last("--repo"))

    def test_missing_value_and_unknown_tokens_raise(self) -> None:
        for argv in (["api", "-X"], ["api", "--method"], ["api", "-Z", "x"], ["api", "--nope"],
                     ["issue", "nope"], ["api", "-i=yes", "x"], ["api", "---x"]):
            with self.subTest(argv=argv), self.assertRaises(gh_argv.ArgvError):
                gh_argv.parse(argv)

    def test_subcommand_aliases(self) -> None:
        self.assertEqual(gh_argv.parse(["issue", "ls"]).path, ("issue", "list"))
        self.assertEqual(gh_argv.parse(["pr", "-R", "o/r", "new"]).path, ("pr", "create"))

    def test_alias_expansion_follows_gh(self) -> None:
        aliases = "c: issue close\nd: 'issue close $1 -R o/r'\ns: '!gh issue close 5'\n"
        self.assertEqual(gh_argv.expand_alias(["c", "5"], aliases), ["issue", "close", "5"])
        self.assertEqual(gh_argv.expand_alias(["d", "5", "x"], aliases), ["issue", "close", "5", "-R", "o/r", "x"])
        self.assertEqual(gh_argv.expand_alias(["s"], aliases), ["s"])
        self.assertIsNone(gh_argv.expand_alias(["zz"], aliases))
        with self.assertRaises(gh_argv.ArgvError):
            gh_argv.expand_alias(["d"], aliases)


if __name__ == "__main__":
    unittest.main()
