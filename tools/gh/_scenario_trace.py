"""The `Scenario Trace` handoff section's checks, and the fence-aware section scanner
every handoff heading is read through (harmonic-forge#920).

Structure only, by design (the split harmonic-forge#838 drew for `Consumers and
Equivalents`): a trace must carry a real code reference and a `verified-live` marker
on the SAME line, outside code fences and HTML comments. Whether the trace is true and
reaches the changed code is `agents/pitch-inspection.md` check 8's.
"""
from __future__ import annotations

import re

_FENCE = re.compile(r"^[ \t]*```")
_COMMENT = re.compile(r"(?s)<!--.*?-->")
_FENCED_BLOCK = re.compile(r"(?ms)^[ \t]*```.*?^[ \t]*```[ \t]*$")

#: A `path:line` (or `path:a-b`) token. The path must contain a letter, so a dotted quad
#: (`127.0.0.1:8002`) is never one, and must not follow `/`, `@`, `:` or a word character,
#: so a URL authority is not read from the middle.
_REF = re.compile(r"(?<![\w./@:-])((?:[\w.-]+/)*[\w.-]*[A-Za-z_][\w.-]*):(\d+)(?:[-–]\d+)?")
#: Extensions that name source, config or docs. An allowlist, not `\.\w+`: `.com`, `.26` and
#: `.example` also look like extensions and are not files (`app.example.com:8443`, `v5.26:3`).
_CODE_EXTS = {
    "py", "sh", "bash", "zsh", "md", "toml", "json", "yaml", "yml", "js", "mjs", "cjs", "ts",
    "tsx", "jsx", "dart", "php", "go", "rs", "java", "kt", "rb", "sql", "cypher", "html", "css",
    "scss", "txt", "cfg", "ini", "conf", "lock", "service", "timer", "env", "vue", "svelte",
    "c", "h", "cpp", "hpp", "cs", "swift", "lua", "pl", "gradle", "xml", "tf",
}
#: Entry points with no extension that are routinely a trace's first hop.
_BARE_NAMES = {"Makefile", "Dockerfile", "Jenkinsfile", "Procfile", "Gemfile", "Rakefile"}


def section(body: str, heading: str) -> str:
    """Content of the first `### <heading>` line OUTSIDE a code fence, up to the next `###`
    line outside a fence. A heading quoted inside a fence is not a heading, and a `###`
    line inside pasted output does not end a section; the content keeps its fences."""
    lines = body.split("\n")
    in_fence, start = False, None
    for index, line in enumerate(lines):
        if _FENCE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if line.startswith("### "):
            if start is not None:
                return "\n".join(lines[start:index]).strip()
            if line[4:].strip() == heading:
                start = index + 1
    return "\n".join(lines[start:]).strip() if start is not None else ""


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
    """Why `trace` (the section's content) is refused, or None. Prose that the template
    ships inside the section, fenced output and HTML comments never count as evidence."""
    body = _COMMENT.sub("", _FENCED_BLOCK.sub("", trace))
    if not code_refs(body):
        return ("Scenario Trace must cite at least one file:line outside code fences (for a "
                "route or URL, cite the file:line of its handler; a host:port or a version "
                "string is not one)")
    if "verified-live" not in body:
        return "Scenario Trace must mark at least one hop verified-live"
    if not any(code_refs(line) and "verified-live" in line for line in body.split("\n")):
        return ("Scenario Trace needs at least one hop line carrying BOTH a file:line and "
                "verified-live (the marker in surrounding prose does not count)")
    return None
