"""The `Scenario Trace` handoff section's checks (harmonic-forge#920).

Structure only, by design (the split harmonic-forge#838 drew for `Consumers and
Equivalents`): a trace must carry a real code reference and a `verified-live` marker on
the SAME hop line, outside code fences and HTML comments. Whether the trace is true and
reaches the changed code is `agents/pitch-inspection.md` check 8's.

Deliberately small. The sticky-wicket ruling on #920 (two preclose passes that kept
finding Markdown-lexing edge cases) was PATCH with the parsing shrunk, not grown:
- the section is LOCATED by span (between the Root Cause and Design Alternatives
  headings), never by parsing fences, so every other heading keeps `heading_content`'s
  original regex and AC2 ("every other heading check is unchanged") holds;
- fence and comment stripping are best-effort, scoped to this section, and fail toward
  REFUSAL: an unclosed fence or comment hides the rest of the section;
- `verified-live` is a whole token; prose negation is the reader's (pitch-inspection's)
  to catch, not a regex's.
"""
from __future__ import annotations

import re

ROOT_CAUSE = "### Root Cause / Entry Point"
TRACE = "### Scenario Trace"
NEXT = "### Design Alternatives Considered"

_FENCE_OPEN = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_FENCE_CLOSE = re.compile(r"^ {0,3}(`{3,}|~{3,})[ \t]*$")
_COMMENT = re.compile(r"(?s)<!--.*?-->")

#: A `path:line` (or `path:a-b`) token. The path must contain a letter (so a dotted quad
#: is never one) and must not continue a word, a dotted name, `@` or `:` to its left. A
#: leading `/`, `~/` or `./` is part of the path. The line number must not continue into a
#: word or a dotted number (`neo4j/neo4j:5.26`, `ghcr.io/o/i:1.4` are image tags).
_REF = re.compile(
    r"(?<![\w.@:-])((?:~/|\./|/)?(?:[\w.-]+/)*[\w.-]*[A-Za-z_][\w.-]*):(\d+)(?:[-–]\d+)?"
    r"(?![\w]|\.\d)")
#: Extensions that name source, config or docs: an allowlist, because `.com`, `.26` and
#: `.example` also look like extensions (`app.example.com:8443`, `v5.26:3`).
_CODE_EXTS = {
    "py", "sh", "bash", "zsh", "md", "toml", "json", "yaml", "yml", "js", "mjs", "cjs", "ts",
    "tsx", "jsx", "dart", "php", "go", "rs", "java", "kt", "rb", "sql", "cypher", "html", "css",
    "scss", "txt", "cfg", "ini", "conf", "lock", "service", "timer", "env", "vue", "svelte",
    "c", "h", "cpp", "hpp", "cs", "swift", "lua", "pl", "gradle", "xml", "tf",
}
#: Entry points with no extension that are routinely a trace's first hop.
_BARE_NAMES = {"Makefile", "Dockerfile", "Jenkinsfile", "Procfile", "Gemfile", "Rakefile"}
_MARKER = re.compile(r"(?<![\w-])verified-live(?![\w-])")


def trace_section(body: str) -> str:
    """The Scenario Trace section's content, or "" when it is absent: the LAST
    `### Scenario Trace` line between the first Root Cause heading and the first
    Design Alternatives heading after it, up to that heading. A `###` line inside pasted
    output does not end it, and a quote of the heading elsewhere is not in the span."""
    lines = body.split("\n")
    start = next((i for i, line in enumerate(lines) if line.rstrip() == ROOT_CAUSE), None)
    if start is None:
        return ""
    end = next((i for i in range(start + 1, len(lines)) if lines[i].rstrip() == NEXT), None)
    if end is None:
        return ""
    span = lines[start + 1:end]
    found = [i for i, line in enumerate(span) if line.rstrip() == TRACE]
    return "\n".join(span[found[-1] + 1:]).strip() if found else ""


def strip_fences(text: str) -> str:
    """Blank the lines of every fenced block (``` or ~~~; the closer is the same character
    and at least as long); an unclosed fence blanks the rest. Line count is preserved."""
    out: list[str] = []
    fence: tuple[str, int] | None = None
    for line in text.split("\n"):
        if fence is None:
            opened = _FENCE_OPEN.match(line)
            if opened:
                fence = (opened.group(1)[0], len(opened.group(1)))
                out.append("")
            else:
                out.append(line)
            continue
        out.append("")
        closer = _FENCE_CLOSE.match(line)
        if closer and closer.group(1)[0] == fence[0] and len(closer.group(1)) >= fence[1]:
            fence = None
    return "\n".join(out)


def strip_comments(text: str) -> str:
    """Blank every HTML comment, keeping its newlines so no two lines are spliced into one;
    an unclosed `<!--` blanks the rest."""
    text = _COMMENT.sub(lambda match: "\n" * match.group(0).count("\n"), text)
    cut = text.find("<!--")
    return text if cut < 0 else text[:cut] + "\n" * text[cut:].count("\n")


def _is_code_ref(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    if name in _BARE_NAMES or name.startswith("."):
        return True
    if "." in name and name.rsplit(".", 1)[-1].lower() in _CODE_EXTS:
        return True
    return "/" in path and "." not in name  # extensionless script in a directory: tools/lane/lane1


def code_refs(text: str) -> list[str]:
    return [m.group(0) for m in _REF.finditer(text) if _is_code_ref(m.group(1))]


def trace_refusal(trace: str) -> str | None:
    """Why `trace` (the section's content) is refused, or None."""
    body = strip_comments(strip_fences(trace))
    if not code_refs(body):
        return ("Scenario Trace must cite at least one file:line outside code fences (for a "
                "route or URL, cite the file:line of its handler; a host:port or a version "
                "string is not one)")
    if not _MARKER.search(body):
        return "Scenario Trace must mark at least one hop verified-live"
    if not any(code_refs(line) and _MARKER.search(line) for line in body.split("\n")):
        return ("Scenario Trace needs at least one hop line carrying BOTH a file:line and "
                "verified-live (the marker in surrounding prose does not count)")
    return None
