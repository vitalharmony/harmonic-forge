"""The gh argv shapes the lane tools emit, read statically from `tools/`
(harmonic-forge#1447 reforge, required test 5).

Every list literal in a non-test Python file under `tools/` whose leading
string constants spell `gh api|issue|pr ...` (or start at `api|issue|pr`
inside a `_gh(...)`/`gh_json(...)`-style call) is rendered to one concrete
argv: a constant stays as written, an f-string or name is filled with a
sample value chosen by the variable's name (`repo` -> `o/r`, `number` -> `5`).
A name bound at module level to a string constant renders as that constant
(so the real GraphQL documents are classified). A list with a starred
element is skipped: its full argv is not knowable statically.

`test_gh_argv.NormalTraffic` runs every rendered argv through
`gh_argv.decide` and requires `allow` or a `gate` (a bound close or merge the
tool really makes). A refusal is a blocker: either the classifier or the
calling tool is wrong.
"""
from __future__ import annotations

import ast
import re
import warnings
from pathlib import Path

TOOLS = Path(__file__).resolve().parent.parent
ROOTS = ("api", "issue", "pr")
#: Calls whose positional args are a gh argv without the leading `gh`.
GH_CALLS = re.compile(r"(^|_)(gh|gh_json|run_gh|gh_api|_gh_json|gh_text|gh_call)$")


def _sample(name: str) -> str:
    low = name.lower()
    if "repo" in low or low in ("slug", "owner_repo", "nwo"):
        return "o/r"
    if "sha" in low or "head" in low or "oid" in low:
        return "a" * 40
    if re.search(r"(number|issue|^pr$|pr_|_pr|num|^n$|^i$|comment_id|^cid$|ident)", low):
        return "5"
    if "label" in low:
        return "bug"
    if "query" in low or "gql" in low:
        return "query{viewer{login}}"
    if "path" in low or "endpoint" in low or "url" in low:
        return "repos/o/r/issues/5"
    if "body" in low or "file" in low:
        return "body.md"
    return "v"


def _constants(tree: ast.Module) -> dict[str, str]:
    found: dict[str, str] = {}
    for node in tree.body:
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)):
            value = _render(node.value, found)
            if value is not None and isinstance(node.value, (ast.Constant, ast.JoinedStr, ast.BinOp)):
                found[node.targets[0].id] = value
    return found


def _render(node: ast.AST, consts: dict[str, str] | None = None) -> str | None:
    consts = consts or {}
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name) and node.id in consts:
        return consts[node.id]
    if isinstance(node, ast.JoinedStr):
        parts = []
        for value in node.values:
            if isinstance(value, ast.Constant):
                parts.append(str(value.value))
            else:
                inner = value.value if isinstance(value, ast.FormattedValue) else value
                if isinstance(inner, ast.Name) and inner.id in consts:
                    parts.append(consts[inner.id])
                else:
                    parts.append(_sample(ast.unparse(inner)))
        return "".join(parts)
    if isinstance(node, (ast.Name, ast.Attribute, ast.Subscript, ast.Call)):
        return _sample(ast.unparse(node))
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _render(node.left, consts), _render(node.right, consts)
        if left is not None and right is not None:
            return left + right
    if isinstance(node, ast.BinOp):
        return _sample(ast.unparse(node))
    return None


def _argv(elements: list[ast.AST], consts: dict[str, str]) -> list[str] | None:
    out: list[str] = []
    for element in elements:
        if isinstance(element, ast.Starred):
            return None
        rendered = _render(element, consts)
        if rendered is None:
            return None
        out.append(rendered)
    return out


def shapes(root: Path = TOOLS) -> list[tuple[str, list[str]]]:
    """[(file:line, argv without `gh`)] for every gh api/issue/pr argv found."""
    found: list[tuple[str, list[str]]] = []
    for path in sorted(root.rglob("*.py")):
        if (path.name.startswith("test_") or "__pycache__" in path.parts
                or path.name in ("gh_argv.py", "gh_traffic.py")):
            continue
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", SyntaxWarning)
                tree = ast.parse(path.read_text())
        except (SyntaxError, UnicodeDecodeError):
            continue
        consts = _constants(tree)
        for node in ast.walk(tree):
            elements = None
            if isinstance(node, (ast.List, ast.Tuple)):
                elements = list(node.elts)
                head = _render(elements[0], consts) if elements else None
                if head is None or not (head == "gh" or head.endswith("/gh")):
                    continue
                elements = elements[1:]
            elif isinstance(node, ast.Call):
                name = ast.unparse(node.func).rsplit(".", 1)[-1]
                elements = list(node.args)
                first = _render(elements[0], consts) if elements else None
                if first == "gh":
                    elements = elements[1:]  # run("gh", "api", ...)
                elif not GH_CALLS.search(name):
                    continue
                elif name.endswith("gh_api"):
                    elements = [ast.Constant("api"), *elements]  # _gh_api("--method", ...)
            if not elements:
                continue
            argv = _argv(elements, consts)
            if not argv or argv[0] not in ROOTS:
                continue
            if argv[0] == "api" and all(a.startswith("-") for a in argv[1:]):
                continue  # a prefix the caller appends the endpoint to later
            found.append((f"{path.relative_to(root.parent)}:{node.lineno}", argv))
    return found
