#!/usr/bin/env python3
"""Tests for `manifest_scope.home_project`, the home-repo rule (harmonic-forge#1596).

Scratch manifests are passed as `manifest=`; the partition case reads the
checked-in `projects.toml` on purpose (AC4: the operator's repos are unchanged).
"""
from __future__ import annotations

import ast
import subprocess
import sys
import tempfile
import unittest
import warnings
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import manifest  # noqa: E402
import manifest_scope as ms  # noqa: E402
from manifest_protocol import ManifestError  # noqa: E402


def entry(prefix: str, repo: str, account: str | None, name: str | None = None) -> str:
    acct = f'account = "{account}"\n' if account is not None else ""
    return f'[[project]]\nname = "{name or prefix}"\nprefix = "{prefix}"\nrepo = "{repo}"\n{acct}'


class Scratch(unittest.TestCase):
    def write(self, text: str) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "projects.toml"
        path.write_text(text, encoding="utf-8")
        return path


class HomeProject(Scratch):
    def test_registered_and_owner_equals_account_is_home(self) -> None:
        path = self.write(entry("Z", "acme/app", "acme") + entry("Q", "Acme/Other", "ACME"))
        self.assertEqual(ms.home_project("acme/app", path).prefix, "Z")
        self.assertEqual(ms.home_project("https://github.com/ACME/App.git", path).prefix, "Z")
        self.assertEqual(ms.home_project("acme/other", path).prefix, "Q")

    def test_owner_other_than_account_unregistered_or_malformed_is_not_home(self) -> None:
        path = self.write(entry("Z", "acme/app", "someone") + entry("Q", "acme/q", "acme"))
        for repo in ("acme/app", "acme/unlisted", "not a repo", "a/b/c", ""):
            with self.subTest(repo=repo):
                self.assertIsNone(ms.home_project(repo, path))

    def test_a_non_ascii_account_is_never_home(self) -> None:
        """Pitch revision 4: `"ſcme".casefold()` is `"scme"`; ASCII lower is not."""
        path = self.write(entry("Z", "scme/app", "ſcme"))
        self.assertIsNone(ms.home_project("scme/app", path))

    def test_an_unreadable_or_invalid_manifest_is_never_home(self) -> None:
        invalid = self.write(entry("Z", "acme/app", "acme") + "onboarded = true\n")
        broken = self.write("[[project\n")
        for path in (invalid, broken, invalid.parent / "absent.toml"):
            with self.subTest(path=path.name):
                self.assertIsNone(ms.home_project("acme/app", path))
                self.assertIn("ManifestError", ms.manifest_error(path))
        self.assertIsNone(ms.manifest_error(self.write(entry("Z", "acme/app", "acme"))))


class LivePartition(unittest.TestCase):
    """TC4 / AC4: the home rule on the checked-in manifest is today's partition."""

    def test_h_f_i_o_are_home_and_k_y_d_p_are_not(self) -> None:
        by_prefix = {p.prefix: p.repo for p in manifest.load() if p.repo}
        for prefix in "HFIO":
            with self.subTest(prefix=prefix):
                self.assertIsNotNone(ms.home_project(by_prefix[prefix]))
        for prefix in "KYDP":
            with self.subTest(prefix=prefix):
                self.assertIsNone(ms.home_project(by_prefix[prefix]))


class PrefixValidation(Scratch):
    """Operator ruling 2026-11-25 (second round): one ASCII letter, unique in any case."""

    def test_a_lowercase_twin_of_a_prefix_is_refused(self) -> None:
        path = self.write(entry("H", "acme/a", "acme", "a") + entry("h", "acme/b", "acme", "b"))
        with self.assertRaisesRegex(ManifestError, "claimed by both"):
            manifest.load(path)

    def test_a_non_ascii_letter_is_refused(self) -> None:
        path = self.write(entry("ı", "acme/a", "acme"))
        with self.assertRaisesRegex(ManifestError, "single letter"):
            manifest.load(path)


class OverlayStaysOutOfGrantScope(unittest.TestCase):
    """TC8 / R1: only the overlay's consumers and the resolver name it.
    harmonic-forge#1579 adds the posting-side consumer, manifest_invoking
    (imported by l1_post, gate_ci, gh_issue and preclose_check).

    That grant scope never reads the overlay is checked two ways. By behavior:
    tools/hooks/test_manifest_scoping.py OverlayFunnelNeverReachedByGrantScope
    runs each grant writer's scope decision with every `manifest_overlay` entry
    point made to raise (sticky-wicket PATCH, operator D22). Statically, below:
    no grant writer reaches an overlay consumer through a name that reads it.
    The static check stays (post-verdict 3, operator D33): it covers the PARK
    and SCAN writers and any writer the behavior probe does not name. Its
    consumer set is derived over import edges (harmonic-forge#1635), not kept
    by hand: a whole-module derivation sweeps in six of the seven writers."""

    def grep(self, *args: str) -> list[str]:
        found = subprocess.run(["git", "-C", str(HERE.parents[1]), "grep", *args],
                               capture_output=True, text=True, check=False, timeout=60)
        return found.stdout.split()

    def test_only_four_production_files_name_the_overlay_resolver(self) -> None:
        found = self.grep("-l", "-E", "manifest_overlay|path_project|project_for_checkout", "--",
                          "tools/**/*.py", "skills/**/*.py", ":!**/test_*")
        self.assertEqual(found, ["tools/lane/ci_plan.py",
                                 "tools/onboard/manifest_identity.py",
                                 "tools/onboard/manifest_invoking.py",
                                 "tools/onboard/manifest_overlay.py"])

    WRITERS = ("force_grant", "imc_waiver", "ui_grant", "batch_auth", "park_grant",
               "scan_grant", "manifest_scope")

    def sources(self) -> dict[str, str]:
        """Every non-test `tools/*/*.py` module's source, by module name."""
        return {p.stem: p.read_text(encoding="utf-8")
                for p in sorted(HERE.parents[1].glob("tools/*/*.py"))
                if not p.name.startswith("test_")}

    def test_no_grant_writer_reaches_an_overlay_read(self) -> None:
        """harmonic-forge#1635 gap 2: the consumer set is derived, not kept by
        hand, and it is neither too narrow (the three named consumers) nor too
        broad (no writer)."""
        sources = self.sources()
        consumers = derive_consumers(sources)
        self.assertLessEqual({"ci_plan", "manifest_identity", "manifest_invoking",
                              "manifest_overlay"}, consumers)
        self.assertFalse(consumers & set(self.WRITERS), consumers & set(self.WRITERS))
        for writer in self.WRITERS:
            self.assertEqual(writer_violations(writer, sources[writer], consumers), [])
        for (module, name), why in SAFE_NAMES.items():
            if why.startswith("D23"):
                continue  # posting identity, not grant scope: named as a reader on purpose
            with self.subTest(safe=f"{module}.{name}"):
                tree = ast.parse(sources[module])
                fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name)
                named = {getattr(n, "id", None) or getattr(n, "attr", None) for n in ast.walk(fn)}
                self.assertFalse(named & OVERLAY_READS, f"{module}.{name} names "
                                 f"{named & OVERLAY_READS}")

    def test_a_writer_edge_through_an_overlay_read_fails(self) -> None:
        """TC2: a synthetic writer source, through the same rule the real ones meet."""
        consumers = derive_consumers(self.sources())
        for source in ("from gate_ci import overlay_ci_refusal\n",
                       "import ci_plan\n",
                       "import ci_plan as c\nc.plan()\n",
                       "def f():\n    import manifest_identity\n    manifest_identity.account_for_origin('u')\n"):
            with self.subTest(source=source):
                self.assertTrue(writer_violations("synthetic", source, consumers))
        for source in ("from preclose_check import find_receipt\n",
                       "import gate_ci\ngate_ci.required_checks()\n",
                       "from batch_auth import REPO_PREFIXES\n"):
            with self.subTest(allowed=source):
                self.assertEqual(writer_violations("synthetic", source, consumers), [])

    def test_a_from_import_of_an_overlay_read_fails_from_any_module(self) -> None:
        """TC2: the from-import rule keys on the name, not the module it comes from."""
        consumers = derive_consumers(self.sources())
        for source in ("from batch_auth import registered_repo\n",
                       "from imc_waiver import resolve_repo as r\n"):
            with self.subTest(source=source):
                found = writer_violations("synthetic", source, consumers)
                self.assertTrue(any("from-imports" in v for v in found), found)


#: Names that read the overlay wherever they are defined (a from-import of one
#: fails whatever module it comes from).
OVERLAY_READS = frozenset({"require_onboarded", "refuse_board_ci", "overlay_ci_refusal",
                           "registered_repo", "resolve_repo", "s0_reader", "check_gate_result",
                           "manifest_invoking", "manifest_overlay"})

#: (consumer module, name) a module may use without becoming a consumer: each
#: does not read the overlay, or (D23) is posting identity, not grant scope.
SAFE_NAMES = {
    ("manifest_identity", "slot_env_or_none"):
        "slot_env -> account_for -> by_repo: the base manifest only",
    ("manifest_identity", "repo_for_path_or_none"):
        "project_for_path -> load(manifest): the base manifest only",
    ("manifest_identity", "apply_project_identity"):
        "D23: imc_waiver's posting identity on a projects.toml repo, not grant scope",
    ("preclose_check", "find_receipt"): "receipt reader",
    ("preclose_check", "receipt_path"): "receipt reader",
    ("preclose_check", "read_receipt"): "receipt reader",
    ("preclose_check", "legacy_receipt_dir"): "receipt reader",
    ("preclose_check", "_repo_key"): "receipt reader",
    ("gate_ci", "required_checks"): "required-check read",
    ("gate_ci", "authorizing_conclusion"): "required-check read",
}

#: A module imported but not used through an attribute (bare, or passed on):
#: every name it holds is in reach, so no allowlist entry can cover it.
WHOLE = "*"


def overlay_edges(name: str, source: str) -> list[tuple[str, frozenset[str], int]]:
    """`(imported module, names used, line)` for each import in one module's
    source, at module or function level. Names used are the from-import names,
    or for `import X [as Y]` the `Y.attr` attributes accessed anywhere in the
    module (WHOLE when the alias is also used bare, or not at all)."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", SyntaxWarning)  # a scanned module's own escapes
        tree = ast.parse(source)
    edges: list[tuple[str, frozenset[str], int]] = []
    aliases: dict[str, list[tuple[str, int]]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            edges.append((node.module.split(".")[0],
                          frozenset(a.name for a in node.names), node.lineno))
        elif isinstance(node, ast.ImportFrom):
            for a in node.names:  # `from . import x`: the module is x itself
                edges.append((a.name, frozenset({WHOLE}), node.lineno))
        elif isinstance(node, ast.Import):
            for a in node.names:
                aliases.setdefault(a.asname or a.name.split(".")[0], []).append(
                    (a.name.split(".")[0], node.lineno))
    attrs: dict[str, set[str]] = {alias: set() for alias in aliases}
    under_attr = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
                and node.value.id in aliases):
            attrs[node.value.id].add(node.attr)
            under_attr.add(id(node.value))
    for node in ast.walk(tree):
        if (isinstance(node, ast.Name) and node.id in aliases
                and id(node) not in under_attr):
            attrs[node.id].add(WHOLE)
    for alias, targets in aliases.items():
        used = frozenset(attrs[alias] or {WHOLE})
        edges.extend((module, used, line) for module, line in targets if module != name)
    return [e for e in edges if e[0] != name]


def derive_consumers(sources: dict[str, str]) -> set[str]:
    """The fixed point from `manifest_overlay`: a module is a consumer when one
    of its edges into a consumer uses a name outside SAFE_NAMES."""
    edges = {name: overlay_edges(name, text) for name, text in sources.items()}
    consumers = {"manifest_overlay"}
    while True:
        new = {name for name, found in edges.items() if name not in consumers
               and any(module in consumers and any((module, n) not in SAFE_NAMES for n in used)
                       for module, used, _line in found)}
        if not new:
            return consumers
        consumers |= new


def writer_violations(name: str, source: str, consumers: set[str]) -> list[str]:
    """Why one grant writer's source reaches an overlay read: an edge into a
    consumer through a name outside SAFE_NAMES, or a from-import of a name in
    OVERLAY_READS from any module."""
    found = []
    for module, used, line in overlay_edges(name, source):
        bad = sorted(n for n in used if (module, n) not in SAFE_NAMES)
        if module in consumers and bad:
            found.append(f"{name}:{line} reaches consumer {module} through {bad}")
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom):
            read = sorted({a.name for a in node.names} & OVERLAY_READS)
            if read:
                found.append(f"{name}:{node.lineno} from-imports overlay read {read}")
    return found


if __name__ == "__main__":
    unittest.main()
