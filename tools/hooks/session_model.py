#!/usr/bin/env python3
"""The Claude Code session's CURRENT model, shared by the tier hooks.

harmonic-forge#656. Claude Code hook payloads carry no model field except on
`SessionStart`, so every hook that needs "which model is this session on"
has to reconstruct it. Before this module, `model_tier_gate.py` read only the
newest assistant `message.model`, which lags a `/model` switch by one turn:
an operator who runs `/model opus` and immediately resends a trigger would
still be read as Sonnet. The trigger check (`tier_model_trigger_check.py`),
the Stop backstop (`tier_model_stop_backstop.py`) and the edit gate all read
it here, so the three cannot disagree.

Resolution order (AC4):

1. The transcript tail, newest entry wins, among three shapes verified in
   real 2.1.270 transcripts:
   - `{"type":"attachment","attachment":{"type":"model","identity":{"modelId":
     "claude-opus-5[1m]", ...}}}` -- written when the model changes;
   - a user entry whose content is `<local-command-stdout>Set model to
     `Opus 5 (1M context)` for this session only</local-command-stdout>`.
     Older builds wrote the name in ANSI bold (`\\x1b[1m...\\x1b[22m`) instead
     of backticks; both are parsed. This is the only shape present in the
     instant between `/model` and the next prompt;
   - an assistant `message.model` (`claude-sonnet-5`), skipping
     `<synthetic>` and sidechain entries.
2. The model a `SessionStart` hook recorded from its payload
   (`record_session_model.py`), for a fresh session with no model-bearing
   transcript entry yet -- the `--model` launch flag case.
3. The effective settings `model`: project `.claude/settings.local.json`,
   project `.claude/settings.json`, then `~/.claude/settings.json`.

Returns None when nothing resolves. Claude Code only: Codex and Gemini lanes
carry their model differently and are not covered by this module.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

SESSION_MODEL_DIR = Path.home() / ".cache" / "harmonic-forge" / "session_model"

_SET_MODEL_RE = re.compile(r"Set model to (?:`([^`]+)`|\x1b\[1m(.+?)\x1b\[22m)")
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_LOCAL_STDOUT = "<local-command-stdout>"


def _tail_lines(path: str, chunk_size: int = 65536, max_bytes: int = 4 << 20):
    """Yield lines from the end of a file backwards, in bounded chunks.

    harmonic-forge#314 (C4): this used `readlines()`, pulling the whole
    transcript into memory on every Edit/Write/MultiEdit call to use only
    the last model-bearing line. Local transcripts reach 107 MB, so that
    cost was paid on every code-writing tool call in a long session.

    Gives up after `max_bytes`. A transcript whose last 4 MB carries no
    model-bearing line then falls through to the caller's next source.

    Moved here from `model_tier_gate.py` (harmonic-forge#656), which imports
    it back, so there is one bounded reader rather than two.
    """
    with open(path, "rb") as handle:
        handle.seek(0, os.SEEK_END)
        position = handle.tell()
        remainder = b""
        scanned = 0
        while position > 0 and scanned < max_bytes:
            read_size = min(chunk_size, position)
            position -= read_size
            scanned += read_size
            handle.seek(position)
            block = handle.read(read_size) + remainder
            parts = block.split(b"\n")
            # parts[0] may be a partial line whose head is in a chunk we
            # have not read yet -- hold it back until we have that chunk.
            remainder = parts[0] if position > 0 else b""
            tail = parts[1:] if position > 0 else parts
            for line in reversed(tail):
                if line.strip():
                    yield line.decode("utf-8", "replace")


def _text_content(message: dict) -> str | None:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts = [b.get("text", "") for b in content
                 if isinstance(b, dict) and b.get("type") == "text"]
        return "".join(texts) if texts else None
    return None


def model_from_entry(entry: dict) -> str | None:
    """The model an individual transcript entry asserts, or None."""
    if not isinstance(entry, dict) or entry.get("isSidechain"):
        return None
    attachment = entry.get("attachment")
    if isinstance(attachment, dict) and attachment.get("type") == "model":
        model_id = (attachment.get("identity") or {}).get("modelId")
        if isinstance(model_id, str) and model_id:
            return model_id
    message = entry.get("message")
    if not isinstance(message, dict):
        return None
    if entry.get("type") == "user" or message.get("role") == "user":
        text = _text_content(message)
        # Anchored to the START of the content: a compaction summary or a
        # pasted transcript quoting "Set model to" is not a /model command.
        if text and text.lstrip().startswith(_LOCAL_STDOUT):
            match = _SET_MODEL_RE.search(text)
            if match:
                return (match.group(1) or match.group(2)).strip()
        return None
    model = message.get("model")
    if isinstance(model, str) and model and model != "<synthetic>":
        return model
    return None


def transcript_model(transcript_path: str) -> str | None:
    """The newest model-bearing transcript entry's model, or None."""
    if not transcript_path:
        return None
    try:
        for line in _tail_lines(transcript_path):
            if "model" not in line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            model = model_from_entry(entry)
            if model:
                return model
    except OSError:
        return None
    return None


def record_path(session_id: str, record_dir: Path | None = None) -> Path | None:
    if not isinstance(session_id, str) or not _SESSION_ID_RE.match(session_id):
        return None
    return (record_dir or SESSION_MODEL_DIR) / session_id


def recorded_model(session_id: str | None, record_dir: Path | None = None) -> str | None:
    path = record_path(session_id or "", record_dir)
    if path is None:
        return None
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return value or None


def _project_root(cwd: str) -> Path | None:
    if not cwd:
        return None
    current = Path(cwd).resolve()
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def _settings_model(path: Path) -> str | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    model = data.get("model") if isinstance(data, dict) else None
    return model if isinstance(model, str) and model.strip() else None


def settings_model(cwd: str, home: Path | None = None) -> str | None:
    root = _project_root(cwd)
    candidates = []
    if root is not None:
        candidates += [root / ".claude" / "settings.local.json",
                       root / ".claude" / "settings.json"]
    candidates.append((home or Path.home()) / ".claude" / "settings.json")
    for path in candidates:
        model = _settings_model(path)
        if model:
            return model
    return None


def current_model(transcript_path: str | None, cwd: str | None,
                  session_id: str | None = None, *,
                  record_dir: Path | None = None,
                  home: Path | None = None) -> str | None:
    """See the module docstring for the order. Never raises."""
    try:
        return (transcript_model(transcript_path or "")
                or recorded_model(session_id, record_dir)
                or settings_model(cwd or "", home))
    except Exception:  # noqa: BLE001 -- a model read must never crash a hook
        return None


def family(model: str | None) -> str | None:
    """`claude-opus-5[1m]`, `Opus 5 (1M context)` and `opus` all give `opus`."""
    if not model:
        return None
    lowered = model.lower()
    for name in ("fable", "opus", "sonnet", "haiku"):
        if name in lowered:
            return name
    return None
