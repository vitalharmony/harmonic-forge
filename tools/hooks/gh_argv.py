"""The gh argv classifier the close and merge gate decides on
(harmonic-forge#1447 reforge).

Deny by default. The gate does not list the spellings of "close" and "merge"
and allow everything else; it lists the calls that cannot close or merge and
refuses, or binds to a concrete `owner/repo#N` target, everything else.

Three parts:

1. `parse`: a pflag-faithful tokenizer (`--long=v`, `--long v`, short
   clusters such as `-iXPATCH`, `-X=PUT`, `--` ending flags, interspersed
   positionals, group-level persistent flags such as `gh issue -R o/r close`).
   It is driven by `gh_flag_table.json`, generated from the installed gh's own
   `--help` by `gen_gh_flag_table.py` and pinned to that gh version. A token
   the table does not know raises `ArgvError`, which the gate refuses.
2. For `gh api`: the effective method the way gh computes it, and the
   endpoint normalized (scheme and host, a leading `/`, the query string, dot
   segments, percent-encoding, `{owner}/{repo}` placeholders) and classed.
3. `decide`: the decision table. A read, a non-issue/PR endpoint, an
   allowlisted non-closing issue/PR mutation, and a GraphQL document with no
   mutation (or only allowlisted non-closing ones) are allowed. `issue close`,
   `pr merge` and every other issue/PR REST mutation are bound to one target
   for the preclose and phase decisions, or refused when no concrete target
   exists.

This module makes no reads and no decisions about labels: those stay in
`block_missing_preclose_inspection.decide_targets` and
`block_undetermined_phase_close.segment_verdict`, which `close_gate` runs on
the canonical argv a `Target` carries.
"""
from __future__ import annotations

import functools
import json
import re
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path

TABLE_PATH = Path(__file__).resolve().parent / "gh_flag_table.json"
GATED_ROOTS = ("api", "issue", "pr")
GITHUB_HOSTS = ("github.com", "api.github.com")
_BOOL_VALUES = {"1": True, "t": True, "T": True, "TRUE": True, "true": True, "True": True,
                "0": False, "f": False, "F": False, "FALSE": False, "false": False, "False": False}
_NUMBER = re.compile(r"^#?(\d+)$")
_ISSUE_URL = re.compile(r"^/([^/]+)/([^/]+)/(?:issues|pull)/(\d+)/?$")
_PR_URL = re.compile(r"^/([^/]+)/([^/]+)/pull/(\d+)(?:/[^/]*)?/?$")
_LEADING_NUMBER = re.compile(r"^0*(\d+)")
_PLACEHOLDER = re.compile(r"\{(owner|repo|branch)\}")
_STATE_KEYS = frozenset({"state", "state_reason"})
_REST_EXEMPT_REASONS = ("not_planned", "duplicate")
_CLI_EXEMPT_REASONS = ("not planned", "duplicate")

#: GraphQL mutations that cannot close or merge (pitch step 4). The two
#: update mutations pass only with no `state` and no `closed` in the document.
GRAPHQL_MUTATION_ALLOWLIST = frozenset({
    "addComment", "addLabelsToLabelable", "removeLabelsFromLabelable", "updateIssue",
    "updatePullRequest"})
_GRAPHQL_STATEFUL = frozenset({"updateIssue", "updatePullRequest"})


class ArgvError(ValueError):
    """The argv is not one the pinned grammar can parse."""


@functools.lru_cache(maxsize=1)
def load_table() -> dict:
    return json.loads(TABLE_PATH.read_text())


@dataclass
class Parsed:
    """One parsed `gh <path> ...` call. `flags` holds (long name, value) in
    argv order; a boolean flag's value is "true" or "false"."""
    path: tuple[str, ...]
    flags: list[tuple[str, str]] = field(default_factory=list)
    positionals: list[str] = field(default_factory=list)

    def values(self, name: str) -> list[str]:
        return [value for flag, value in self.flags if flag == name]

    def last(self, name: str) -> str | None:
        found = self.values(name)
        return found[-1] if found else None

    def has(self, name: str) -> bool:
        return any(flag == name for flag, _ in self.flags)

    @property
    def help(self) -> bool:
        return any(flag == "--help" and _BOOL_VALUES[value] for flag, value in self.flags)


def _bool(flag: str, value: str) -> str:
    if value not in _BOOL_VALUES:
        raise ArgvError(f"invalid boolean value {value!r} for {flag}")
    return "true" if _BOOL_VALUES[value] else "false"


def _consume(argv: list[str], i: int, spec: dict, parsed: Parsed) -> tuple[int, bool]:
    """Consume the flag token at `argv[i]` the way pflag does. Returns the
    next index and whether flags ended (`--`)."""
    token = argv[i]
    flags = spec["flags"]
    if token == "--":
        return i + 1, True
    if token.startswith("--"):
        name, eq, inline = token[2:].partition("=")
        if not name or name[0] in "-=":
            raise ArgvError(f"bad flag syntax: {token}")
        long = "--" + name
        if long not in flags:
            raise ArgvError(f"unknown flag: {long}")
        if not flags[long]["value"]:
            parsed.flags.append((long, _bool(long, inline) if eq else "true"))
            return i + 1, False
        if eq:
            parsed.flags.append((long, inline))
            return i + 1, False
        if i + 1 >= len(argv):
            raise ArgvError(f"flag needs an argument: {long}")
        parsed.flags.append((long, argv[i + 1]))
        return i + 2, False
    by_short = {spec_["short"]: long for long, spec_ in flags.items() if spec_["short"]}
    shorts = token[1:]
    while shorts:
        char = shorts[0]
        if char not in by_short:
            raise ArgvError(f"unknown shorthand flag: {char!r} in {token}")
        long = by_short[char]
        if len(shorts) > 2 and shorts[1] == "=":
            value = shorts[2:]
            parsed.flags.append((long, value if flags[long]["value"] else _bool(long, value)))
            return i + 1, False
        if not flags[long]["value"]:
            parsed.flags.append((long, "true"))
            shorts = shorts[1:]
            continue
        if len(shorts) > 1:
            parsed.flags.append((long, shorts[1:]))
            return i + 1, False
        if i + 1 >= len(argv):
            raise ArgvError(f"flag needs an argument: -{char}")
        parsed.flags.append((long, argv[i + 1]))
        return i + 2, False
    return i + 1, False


def _is_flag(token: str) -> bool:
    return len(token) > 1 and token.startswith("-")


def parse(argv: list[str], table: dict | None = None) -> Parsed:
    """Parse a gh argv whose root is `api`, `issue` or `pr` (no leading `gh`)."""
    table = table or load_table()
    commands = table["commands"]
    if not argv or argv[0] not in GATED_ROOTS:
        raise ArgvError(f"not a gated root: {argv[:1]}")
    parsed = Parsed(path=(argv[0],))
    spec = commands[argv[0]]
    i, ended = 1, False
    # Group levels: persistent flags may precede the subcommand name (cobra).
    while spec["children"] and i < len(argv):
        token = argv[i]
        if _is_flag(token) and not ended:
            i, ended = _consume(argv, i, spec, parsed)
            if ended:
                raise ArgvError("`--` before a subcommand name")
            continue
        names = {name: name for name in spec["children"]}
        for child in spec["children"]:
            for alias in commands[" ".join((*parsed.path, child))]["aliases"]:
                names.setdefault(alias, child)
        if token not in names:
            raise ArgvError(f"unknown command {token!r} for `gh {' '.join(parsed.path)}`")
        parsed.path = (*parsed.path, names[token])
        spec = commands[" ".join(parsed.path)]
        i += 1
    while i < len(argv):
        token = argv[i]
        if ended or not _is_flag(token):
            parsed.positionals.append(token)
            i += 1
            continue
        i, ended = _consume(argv, i, spec, parsed)
    return parsed


# --- decisions -------------------------------------------------------------

@dataclass(frozen=True)
class Target:
    """A gated close or merge, bound to one `repo#number` (repo None: the
    checkout's, which the preclose decision resolves strictly)."""
    repo: str | None
    number: str
    kind: str  # "issue" or "pr"
    exempt: bool
    tokens: tuple[str, ...]  # canonical `gh issue close`/`gh pr merge` argv
    heads: tuple[str | None, ...] = (None,)


@dataclass(frozen=True)
class Verdict:
    action: str  # "allow", "refuse" or "gate"
    reason: str = ""
    target: Target | None = None


def allow(reason: str) -> Verdict:
    return Verdict("allow", reason)


def refuse(reason: str) -> Verdict:
    return Verdict("refuse", reason)


def _repo_arg(value: str) -> tuple[str | None, str]:
    """`[HOST/]OWNER/REPO` or a URL -> (host or None, owner/repo)."""
    if "://" in value:
        parts = urllib.parse.urlsplit(value)
        host, path = parts.hostname, parts.path.strip("/").split("/")
        if len(path) != 2 or not all(path):
            raise ArgvError(f"unreadable repository {value!r}")
        return host, "/".join(path)
    path = value.split("/")
    if len(path) == 2 and all(path):
        return None, value
    if len(path) == 3 and all(path):
        return path[0], "/".join(path[1:])
    raise ArgvError(f"unreadable repository {value!r}")


def _host_ok(host: str | None) -> bool:
    return host is None or host.lower() in GITHUB_HOSTS


def _resolve_repo(parsed: Parsed, env: dict) -> tuple[str | None, str | None]:
    """(repo or None, refusal or None) from -R/--repo, GH_REPO and GH_HOST."""
    gh_host = (env.get("GH_HOST") or "").strip()
    if gh_host and not _host_ok(gh_host):
        return None, f"GH_HOST={gh_host} names another host; the gate checks github.com only"
    flag = parsed.last("--repo")
    env_repo = (env.get("GH_REPO") or "").strip()
    repo = None
    for source in (flag, env_repo):
        if not source:
            continue
        host, slug = _repo_arg(source)
        if not _host_ok(host):
            return None, f"repository {source!r} is on another host; the gate checks github.com only"
        if repo and repo.lower() != slug.lower():
            return None, f"GH_REPO={env_repo} and --repo {flag} disagree"
        repo = repo or slug
    return repo, None


def _bind_positional(value: str, url_pattern: re.Pattern[str]) -> tuple[str | None, str] | None:
    """(url repo or None, number) for `N`, `#N` or a github.com URL."""
    match = _NUMBER.match(value)
    if match:
        return None, match.group(1)
    if "://" in value:
        parts = urllib.parse.urlsplit(value)
        found = url_pattern.match(parts.path)
        if found and _host_ok(parts.hostname):
            return f"{found.group(1)}/{found.group(2)}", found.group(3)
    return None


def _cli_target(parsed: Parsed, env: dict) -> Verdict:
    kind = "issue" if parsed.path == ("issue", "close") else "pr"
    repo, refusal = _resolve_repo(parsed, env)
    if refusal:
        return refuse(refusal)
    if len(parsed.positionals) > 1:
        return refuse(f"`gh {' '.join(parsed.path)}` with more than one argument cannot be bound to one target")
    number = ""
    if parsed.positionals:
        bound = _bind_positional(parsed.positionals[0], _ISSUE_URL if kind == "issue" else _PR_URL)
        if bound is None:
            return refuse(f"`gh {' '.join(parsed.path)} {parsed.positionals[0]}` names no issue/PR "
                          "number the gate can bind (a branch name or a URL on another host)")
        url_repo, number = bound
        if url_repo:
            if repo and repo.lower() != url_repo.lower():
                return refuse(f"the URL's repository {url_repo} and --repo/GH_REPO {repo} disagree")
            repo = url_repo
    elif kind == "issue":
        return refuse("`gh issue close` with no issue argument cannot be bound to a target")
    base = ["gh", *parsed.path, number] + (["--repo", repo] if repo else [])
    if kind == "issue":
        reasons, dups = parsed.values("--reason"), parsed.values("--duplicate-of")
        others = [f for f, _ in parsed.flags if f not in ("--reason", "--duplicate-of", "--repo")]
        exempt = (not others and len(reasons) + len(dups) == 1
                  and (reasons[0] in _CLI_EXEMPT_REASONS if reasons else bool(_NUMBER.match(dups[0]))))
        extra = [t for r in reasons[-1:] for t in ("--reason", r)]
        extra += [t for d in dups[-1:] for t in ("--duplicate-of", d)]
        return Verdict("gate", target=Target(repo, number, "issue", exempt, tuple(base + extra)))
    extra = []
    for flag in ("--body", "--subject"):
        value = parsed.last(flag)
        if value is not None:
            extra += [flag, value]
    sha = parsed.last("--match-head-commit")
    if sha is not None:
        extra += ["--match-head-commit", sha]
    return Verdict("gate", target=Target(repo, number, "pr", False, tuple(base + extra), (sha,)))


@dataclass(frozen=True)
class ApiField:
    typed: bool  # -F/--field (typed, may read @file) vs -f/--raw-field
    key: str | None  # None: no `=` and not a `key[]` empty array
    value: str

    @property
    def base(self) -> str | None:
        return None if self.key is None else self.key.split("[", 1)[0].strip().lower()

    @property
    def from_file(self) -> bool:
        return self.typed and self.value.startswith("@")


def _api_fields(parsed: Parsed) -> list[ApiField]:
    fields = []
    for flag, raw in parsed.flags:
        if flag not in ("--field", "--raw-field"):
            continue
        key, eq, value = raw.partition("=")
        if not eq and not key.endswith("[]"):
            fields.append(ApiField(flag == "--field", None, raw))
        else:
            fields.append(ApiField(flag == "--field", key, value))
    return fields


@dataclass(frozen=True)
class Endpoint:
    host: str | None
    segments: tuple[str, ...]
    query_keys: tuple[str, ...]
    has_query: bool
    placeholder: bool  # an unresolved {owner}/{repo} remained

    @property
    def graphql(self) -> bool:
        return bool(self.segments) and self.segments[-1].lower() == "graphql"


def normalize_endpoint(endpoint: str, env: dict) -> Endpoint:
    """The endpoint as GitHub routes it: no scheme/host, no leading `/`, no
    query string or fragment, dot segments resolved, percent-decoded, empty
    segments dropped, a GHES `api/v3` prefix dropped, placeholders filled
    from GH_REPO (gh's own source when it is set)."""
    env_repo = (env.get("GH_REPO") or "").strip()
    placeholder = False
    if _PLACEHOLDER.search(endpoint):
        slug = None
        if env_repo:
            try:
                slug = _repo_arg(env_repo)[1]
            except ArgvError:
                slug = None
        if slug:
            owner, repo = slug.split("/")
            endpoint = endpoint.replace("{owner}", owner).replace("{repo}", repo)
        placeholder = bool(_PLACEHOLDER.search(endpoint))
    host = None
    if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", endpoint):
        parts = urllib.parse.urlsplit(endpoint)
        host, path, query = parts.hostname, parts.path, parts.query
    else:
        path, _, rest = endpoint.partition("?")
        path = path.split("#", 1)[0]
        query = rest.split("#", 1)[0]
    segments: list[str] = []
    for raw in urllib.parse.unquote(path).split("/"):
        if raw in ("", "."):
            continue
        if raw == "..":
            if segments:
                segments.pop()
            continue
        segments.append(raw)
    if [s.lower() for s in segments[:2]] == ["api", "v3"]:
        segments = segments[2:]
    keys = tuple(k for k, _ in urllib.parse.parse_qsl(query, keep_blank_values=True))
    return Endpoint(host, tuple(segments), keys, bool(query), placeholder)


@dataclass(frozen=True)
class EndpointClass:
    kind: str  # "issue", "pr" or "other"
    repo: str | None  # owner/repo; None for repositories/<id>
    number: str = ""
    sub: tuple[str, ...] = ()
    by_id: bool = False


def classify_endpoint(ep: Endpoint) -> EndpointClass:
    seg = list(ep.segments)
    low = [s.lower() for s in seg]
    if len(seg) >= 5 and low[0] == "repos" and low[3] in ("issues", "pulls"):
        match = _LEADING_NUMBER.match(seg[4])
        if match:
            return EndpointClass("issue" if low[3] == "issues" else "pr", f"{seg[1]}/{seg[2]}",
                                 match.group(1), tuple(low[5:]))
    if len(seg) >= 4 and low[0] == "repositories" and low[2] in ("issues", "pulls"):
        match = _LEADING_NUMBER.match(seg[3])
        if match:
            return EndpointClass("issue" if low[2] == "issues" else "pr", None, match.group(1),
                                 tuple(low[4:]), by_id=True)
    return EndpointClass("other", None)


def _moves_a_ref(ep: Endpoint) -> bool:
    """A non-GET branch merge (`repos/o/r/merges`, `repositories/<id>/merges`)
    or any git ref write (`.../git/refs...`): either can move a base branch to
    a pull request's head, which GitHub marks merged (preclose pass 1 of the
    reforge, survivor 2). Refused outright, never bound."""
    low = [s.lower() for s in ep.segments]
    if low[:1] == ["repos"] and len(low) >= 4:
        rest = low[3:]
    elif low[:1] == ["repositories"] and len(low) >= 3:
        rest = low[2:]
    else:
        return False
    return rest[:1] == ["merges"] or rest[:2] == ["git", "refs"]


def effective_method(parsed: Parsed) -> str:
    """gh's rule: -X/--method if passed (last wins); else POST when any field
    or --input is present; else GET."""
    method = parsed.last("--method")
    if method is not None:
        return method.upper()
    if parsed.has("--field") or parsed.has("--raw-field") or parsed.has("--input"):
        return "POST"
    return "GET"


def _readable_no_state(parsed: Parsed, ep: Endpoint, fields: list[ApiField]) -> bool:
    """A PATCH body the gate can read, with no state key anywhere: no
    --input, every field has a key, and no field or query key is
    `state`/`state_reason` (the key is read before `=`, so `-F body=@file`
    passes and `-F state=@file` does not)."""
    if parsed.has("--input"):
        return False
    if any(f.key is None for f in fields):
        return False
    keys = [f.base for f in fields] + [k.split("[", 1)[0].strip().lower() for k in ep.query_keys]
    return not any(k in _STATE_KEYS for k in keys)


def _allowlisted(cls: EndpointClass, method: str, parsed: Parsed, ep: Endpoint,
                 fields: list[ApiField]) -> bool:
    """Pitch step 5: issue/PR mutations that cannot close or merge."""
    sub = cls.sub
    if cls.kind == "issue":
        if sub == ("comments",):
            return method == "POST"
        if sub[:1] == ("labels",) and len(sub) <= 2:
            return method in ("POST", "PUT", "DELETE")
        if sub == ("assignees",):
            return method in ("POST", "DELETE")
        if sub[:2] in (("dependencies", "blocked_by"), ("dependencies", "blocking")) and len(sub) <= 3:
            # Not in the pitch's list; gh_issue.py files blocked-by edges on
            # every filing (normal traffic, required test 5).
            return method in ("POST", "DELETE")
    else:
        if sub == ("comments",):
            return method == "POST"
        if sub[:1] == ("reviews",):
            return True
        if sub == ("requested_reviewers",):
            return method in ("POST", "DELETE")
        if sub == ("update-branch",):
            return method == "PUT"
    if sub == ():
        return method == "PATCH" and _readable_no_state(parsed, ep, fields)
    return False


def _rest_exempt(method: str, ep: Endpoint, cls: EndpointClass, parsed: Parsed,
                 fields: list[ApiField]) -> tuple[bool, str | None]:
    """The exact not-planned/duplicate REST close (harmonic-forge#1433): PATCH
    issues/N with exactly `state=closed` and `state_reason=<reason>`, both
    literal, nothing else. Returns (exempt, reason for the phase argv)."""
    if (method != "PATCH" or cls.kind != "issue" or cls.sub or parsed.has("--input")
            or ep.query_keys or len(fields) != 2 or any(f.from_file or f.key is None for f in fields)):
        return False, None
    pairs = {f.key: f.value for f in fields}
    reason = pairs.get("state_reason")
    if pairs.get("state") == "closed" and reason in _REST_EXEMPT_REASONS:
        return True, reason.replace("_", " ")
    return False, None


# --- GraphQL -----------------------------------------------------------------

_GQL_TOKEN = re.compile(r'''
    (?P<ws>[\s,﻿]+)
  | (?P<comment>\#[^\n\r]*)
  | (?P<block>"""(?:\\"""|[^"]|"(?!""))*""")
  | (?P<string>"(?:\\.|[^"\\\n\r])*")
  | (?P<spread>\.\.\.)
  | (?P<punct>[!$&()\:=@\[\]{|}])
  | (?P<name>[_A-Za-z][_0-9A-Za-z]*)
  | (?P<number>-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)
''', re.X)


def _gql_tokens(doc: str) -> list[tuple[str, str]]:
    tokens, i = [], 0
    while i < len(doc):
        match = _GQL_TOKEN.match(doc, i)
        if not match:
            raise ArgvError(f"unreadable GraphQL at offset {i}")
        kind = match.lastgroup
        if kind not in ("ws", "comment"):
            tokens.append((kind, match.group()))
        i = match.end()
    return tokens


def _skip_group(tokens: list[tuple[str, str]], i: int, open_: str, close: str) -> int:
    depth = 0
    while i < len(tokens):
        value = tokens[i][1] if tokens[i][0] == "punct" else None
        if value == open_:
            depth += 1
        elif value == close:
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    raise ArgvError("unbalanced GraphQL document")


def graphql_operations(doc: str) -> list[tuple[str, list[str]]]:
    """[(operation type, top-level field names)] for every operation in the
    document; fragments are skipped. Raises ArgvError on anything else."""
    tokens = _gql_tokens(doc)
    ops: list[tuple[str, list[str]]] = []
    i = 0
    while i < len(tokens):
        kind, value = tokens[i]
        if kind == "name" and value == "fragment":
            while i < len(tokens) and tokens[i] != ("punct", "{"):
                i += 1
            i = _skip_group(tokens, i, "{", "}")
            continue
        op = "query"
        if kind == "name" and value in ("query", "mutation", "subscription"):
            op = value
            i += 1
            if i < len(tokens) and tokens[i][0] == "name":
                i += 1
            if i < len(tokens) and tokens[i] == ("punct", "("):
                i = _skip_group(tokens, i, "(", ")")
            while i < len(tokens) and tokens[i] == ("punct", "@"):
                i += 2
                if i < len(tokens) and tokens[i] == ("punct", "("):
                    i = _skip_group(tokens, i, "(", ")")
        if i >= len(tokens) or tokens[i] != ("punct", "{"):
            raise ArgvError("GraphQL operation has no selection set")
        end = _skip_group(tokens, i, "{", "}")
        names: list[str] = []
        j = i + 1
        while j < end - 1:
            kind, value = tokens[j]
            if kind == "spread":
                raise ArgvError("a fragment spread at an operation's top level")
            if kind != "name":
                raise ArgvError(f"unexpected GraphQL token {value!r}")
            name = value
            j += 1
            if tokens[j] == ("punct", ":"):
                if tokens[j + 1][0] != "name":
                    raise ArgvError("GraphQL alias with no field")
                name = tokens[j + 1][1]
                j += 2
            names.append(name)
            if tokens[j] == ("punct", "("):
                j = _skip_group(tokens, j, "(", ")")
            while tokens[j] == ("punct", "@"):
                j += 2
                if tokens[j] == ("punct", "("):
                    j = _skip_group(tokens, j, "(", ")")
            if tokens[j] == ("punct", "{"):
                j = _skip_group(tokens, j, "{", "}")
        ops.append((op, names))
        i = end
    if not ops:
        raise ArgvError("GraphQL document has no operation")
    return ops


def _graphql_verdict(parsed: Parsed, ep: Endpoint, fields: list[ApiField]) -> Verdict:
    if parsed.has("--input"):
        return refuse("a non-GET `gh api graphql` with an --input body the gate cannot read")
    if ep.has_query:
        return refuse("a non-GET `gh api graphql` with a query string")
    queries = [f for f in fields if f.base == "query"]
    if len(queries) != 1 or queries[0].key != "query":
        return refuse("a `gh api graphql` call needs exactly one inline `query` field the gate can read")
    query = queries[0]
    if query.from_file:
        return refuse("a `gh api graphql` query read from a file the gate cannot see")
    try:
        ops = graphql_operations(query.value)
    except ArgvError as exc:
        return refuse(f"the GraphQL document could not be read ({exc})")
    writes = [(op, names) for op, names in ops if op != "query"]
    if not writes:
        return allow("GraphQL read")
    if any(op != "mutation" for op, _ in writes):
        return refuse("a GraphQL subscription is not a call the gate allows")
    names = {name for _, ns in writes for name in ns}
    unknown = sorted(names - GRAPHQL_MUTATION_ALLOWLIST)
    if unknown:
        return refuse(f"GraphQL mutation(s) {', '.join(unknown)} are not on the non-closing allowlist; "
                      "a close or merge by node id cannot be bound to a target without another read")
    if "$" in query.value:
        return refuse("an allowlisted GraphQL mutation must carry its input inline, with no $variables")
    if names & _GRAPHQL_STATEFUL and re.search(r"\b(state|closed)\b", query.value, re.I):
        return refuse("updateIssue/updatePullRequest with a state or closed input field")
    return allow("allowlisted non-closing GraphQL mutation")


def _api_verdict(parsed: Parsed, env: dict) -> Verdict:
    if len(parsed.positionals) != 1:
        return refuse(f"`gh api` needs exactly one endpoint; got {len(parsed.positionals)}")
    method = effective_method(parsed)
    if method in ("GET", "HEAD"):
        return allow("read")
    if any("method-override" in h.split(":", 1)[0].lower() for h in parsed.values("--header")):
        return refuse("a method-override header on a `gh api` call")
    ep = normalize_endpoint(parsed.positionals[0], env)
    fields = _api_fields(parsed)
    if ep.graphql:
        return _graphql_verdict(parsed, ep, fields)
    cls = classify_endpoint(ep)
    if cls.kind == "other":
        if _moves_a_ref(ep):
            return refuse(f"`{method} {'/'.join(ep.segments)}` can move a branch to a pull request's "
                          "head, which GitHub records as a merge; no target can be bound")
        return allow("not an issue or pull request endpoint")
    if _allowlisted(cls, method, parsed, ep, fields):
        return allow("allowlisted non-closing issue/PR mutation")
    shape = f"{method} {'/'.join(ep.segments)}"
    if cls.by_id:
        return refuse(f"`{shape}` addresses a repository by id; binding it to a repository "
                      "needs another read")
    if ep.placeholder:
        return refuse(f"`{shape}` has a {{owner}}/{{repo}} placeholder the gate cannot bind "
                      "(set GH_REPO or name the repository)")
    host = ep.host or parsed.last("--hostname") or (env.get("GH_HOST") or "").strip() or None
    if not _host_ok(host):
        return refuse(f"`{shape}` goes to {host}; the gate checks github.com only")
    if cls.kind == "pr":
        tokens = ("gh", "pr", "merge", cls.number, "--repo", cls.repo)
        return Verdict("gate", target=Target(cls.repo, cls.number, "pr", False, tokens))
    exempt, reason = _rest_exempt(method, ep, cls, parsed, fields)
    tokens = ("gh", "issue", "close", cls.number, "--repo", cls.repo)
    if reason:
        tokens += ("--reason", reason)
    return Verdict("gate", target=Target(cls.repo, cls.number, "issue", exempt, tokens))


def decide(argv: list[str], env: dict, table: dict | None = None) -> Verdict:
    """The verdict for one gh argv whose root is `api`, `issue` or `pr`
    (aliases already expanded). Any argv the grammar cannot parse refuses."""
    try:
        parsed = parse(argv, table)
    except ArgvError as exc:
        return refuse(f"the gate could not parse this gh call against the pinned grammar "
                      f"(gh {load_table()['gh_version']}): {exc}")
    if parsed.help:
        return allow("help")
    if parsed.path == ("api",):
        try:
            return _api_verdict(parsed, env)
        except ArgvError as exc:
            return refuse(str(exc))
    if parsed.path in (("issue", "close"), ("pr", "merge")):
        try:
            return _cli_target(parsed, env)
        except ArgvError as exc:
            return refuse(str(exc))
    return allow(f"`gh {' '.join(parsed.path)}` does not close an issue or merge a pull request")


# --- aliases ---------------------------------------------------------------

def expand_alias(argv: list[str], alias_list: str) -> list[str] | None:
    """`argv` with argv[0] expanded the way gh expands an alias once, or None
    when argv[0] is not an alias. Raises ArgvError for an expansion gh itself
    would reject and for every `!` (shell) alias."""
    import shlex  # noqa: PLC0415
    for line in alias_list.splitlines():
        name, sep, body = line.partition(":")
        if not sep or name.strip() != argv[0]:
            continue
        body = body.strip()
        if len(body) >= 2 and body[0] == body[-1] and body[0] in "'\"":
            body = body[1:-1]
        if body.startswith("!"):
            # Preclose pass 1 of the reforge, survivor 3: a shell alias's body
            # can run gh by absolute path, past the shim. Refused for every root.
            raise ArgvError(f"`{argv[0]}` is a shell (`!`) alias; its command runs outside the gate "
                            "and is refused")
        extra: list[str] = []
        for i, arg in enumerate(argv[1:]):
            if "$" not in body:
                extra.append(arg)
                continue
            body = body.replace(f"${i + 1}", arg)
        if re.search(r"\$\d", body):
            raise ArgvError(f"not enough arguments for alias {argv[0]!r}")
        try:
            return [*shlex.split(body), *extra]
        except ValueError as exc:
            raise ArgvError(f"unreadable alias {argv[0]!r}: {exc}") from exc
    return None
