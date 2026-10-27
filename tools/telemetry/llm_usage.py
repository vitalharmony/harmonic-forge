#!/usr/bin/env python3
"""LLM usage capture into the telemetry store (harmonic-forge#1188).

One normalized `source=llm-usage` event per model call, whatever the provider:
attrs `provider, model, input_uncached, cached_read, cache_write, output,
reasoning_out, est_cost_usd, latency_ms, session_id, run_id, request_id,
attribution` (+ `plan_used_pct_5h/7d` for Codex). Cached and uncached input are
always separate fields, never summed.

- Codex rollouts (`~/.codex/sessions/**/rollout-*.jsonl`): one event per
  `token_count` whose cumulative `total_token_usage` advanced, valued from that
  event's `last_token_usage` (AC3). Codex `input_tokens` includes cached, so
  `input_uncached = input_tokens - cached_input_tokens`.
- xAI (AC6): `normalize_xai()` maps a recorded Chat Completions or Responses
  usage object. Fixture-only: no Grok caller exists yet.
- Claude Code (AC4): NOT BUILT. Its source (transcripts vs OTel) is decided by
  the AC1 comparison; see `harvest_claude`.
- Operator attention (AC11): the shorthand hook's attention lines become
  `source=operator-attention` events, always `validated=false`.

Attribution (AC7): a call takes issue/lane/run of the newest sidecar line
(`attrib.py`) at or before it in its own session; otherwise
`attribution=unassigned`, `issue=None`. No text is ever read for an issue.

Idempotence (AC5): a per-file watermark (inode + byte offset of the last
complete line) means each run reads only new lines, and `subject_id` is stable
(`codex:<rollout id>:<cumulative total_tokens>`), so a re-read is a duplicate
under `emit()`'s `event_id` dedupe.

Rollout contents are untrusted: every line is parsed as JSON data only,
malformed or unknown shapes are counted, never executed or guessed.

CLI: llm_usage.py harvest [--codex-root DIR] [--attention-root DIR]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Iterable, Optional

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import attrib  # noqa: E402
import emit  # noqa: E402

STATE_ENV = "HARMONIC_FORGE_LLM_USAGE_STATE"
CODEX_ROOT = Path.home() / ".codex" / "sessions"
ATTENTION_ENV = "HARMONIC_FORGE_ATTENTION_DIR"
EXTRACTOR = "llm-usage-1"
LEAD_CAP_BYTES = 4096  # AC11 formula, unvalidated

#: Price table, USD per 1M tokens: (uncached input, cached read, cache write, output).
#: An ESTIMATE from list prices, never billing. A model missing here gets
#: est_cost_usd = -1 and is counted as unpriced in the report.
#: Every entry was checked against a published price page (named per block);
#: none is shipped unchecked, so a model's absence means "not yet checked".
#: Cache write is the 5-minute rate. Claude Code also writes 1-hour cache at a
#: higher rate; the Claude harvester (AC4) prices that split itself.
PRICES_VERSION = "2026-10-26.1"
PRICES: dict[str, tuple[float, float, float, float]] = {
    # platform.claude.com/docs/en/about-claude/pricing, read 2026-10-26.
    # Haiku 5.5 is the prompts-up-to-100K-tokens tier.
    "claude-fable-5-1": (10.0, 0.25, 12.5, 50.0),
    "claude-opus-5-5": (4.0, 0.20, 5.0, 20.0),
    "claude-opus-5": (5.0, 0.50, 6.25, 25.0),
    "claude-sonnet-5-5": (2.0, 0.10, 2.5, 10.0),
    "claude-sonnet-5": (2.0, 0.20, 2.5, 10.0),
    "claude-sonnet-4-6": (3.0, 0.30, 3.75, 15.0),
    "claude-haiku-5-5": (0.10, 0.01, 0.125, 0.50),
    "claude-haiku-4-5-20251001": (1.0, 0.10, 1.25, 5.0),
    # Codex (gpt-5.6-terra) runs on a ChatGPT plan with no per-token list
    # price checked yet: unpriced (-1), counted in the report.
}


def state_path() -> Path:
    return Path(os.environ.get(STATE_ENV) or Path.home() / ".claude" / "state" / "llm-usage" / "watermark.json").expanduser()


def attention_dir() -> Path:
    return Path(os.environ.get(ATTENTION_ENV) or Path.home() / ".claude" / "state" / "attention").expanduser()


def est_cost(model: str, input_uncached: int, cached_read: int, cache_write: int, output: int,
             prices: Optional[dict] = None) -> float:
    rate = (PRICES if prices is None else prices).get(model)
    if rate is None:
        return -1
    return round((input_uncached * rate[0] + cached_read * rate[1] + cache_write * rate[2]
                  + output * rate[3]) / 1_000_000, 6)


def _int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


# --- attribution (AC7) -------------------------------------------------------

def attribute(session_id: str, ts: str) -> dict:
    """Issue/lane/run from the newest sidecar line at or before `ts` in the
    same session. No line: unassigned, issue None. Never inferred."""
    line = attrib.latest(session_id, ts) if session_id else None
    if not line or not line.get("issue"):
        return {"attribution": "unassigned", "repo": None, "issue": None,
                "lane": (line or {}).get("lane"), "run_id": (line or {}).get("run_id"),
                "attrib_trigger": None, "attrib_ts": None}
    repo, _, number = str(line["issue"]).rpartition("#")
    return {"attribution": "explicit", "repo": repo or None,
            "issue": int(number) if number.isdigit() else None,
            "lane": line.get("lane"), "run_id": line.get("run_id"),
            "attrib_trigger": line.get("trigger"), "attrib_ts": line.get("ts")}


def to_event(usage: dict, ts: str, subject_id: str, prices: Optional[dict] = None) -> dict:
    """One normalized llm-usage event from a provider-neutral usage dict."""
    who = attribute(usage.get("session_id") or "", ts)
    attrs = {k: usage.get(k) for k in (
        "provider", "model", "input_uncached", "cached_read", "cache_write", "output",
        "reasoning_out", "latency_ms", "session_id", "request_id",
        "plan_used_pct_5h", "plan_used_pct_7d")}
    attrs.update(est_cost_usd=est_cost(usage["model"], usage["input_uncached"], usage["cached_read"],
                                       usage["cache_write"], usage["output"], prices),
                 price_version=PRICES_VERSION, attribution=who["attribution"],
                 run_id=who["run_id"], lane=who["lane"],
                 attrib_trigger=who["attrib_trigger"], attrib_ts=who["attrib_ts"])
    repo = who["repo"]
    lane = who["lane"]
    return {
        "ts": ts, "source": "llm-usage", "repo": repo, "issue": who["issue"],
        "account": repo.split("/")[0] if repo else None, "org": repo.split("/")[0] if repo else None,
        "actor": f"lane{lane}" if lane in ("1", "2", "3") else "unset",
        "event_type": "llm-usage.call", "subject_kind": "llm-call", "subject_id": subject_id,
        "provenance": "hook" if who["attribution"] == "explicit" else "manual",
        "validated": True, "extractor_version": EXTRACTOR,
        "attrs": {k: v for k, v in attrs.items() if v is not None},
    }


# --- Codex (AC3) --------------------------------------------------------------

def codex_usages(lines: Iterable[str], ctx: dict) -> tuple[list[tuple[dict, str, str]], int]:
    """(usage, ts, subject_id) per advancing token_count, and the count of
    unparseable/unknown lines. `ctx` carries rollout id, model and the last
    cumulative total across watermark resumes and is updated in place."""
    out: list[tuple[dict, str, str]] = []
    unknown = 0
    for raw in lines:
        try:
            rec = json.loads(raw)
        except ValueError:
            unknown += 1
            continue
        if not isinstance(rec, dict):
            unknown += 1
            continue
        payload = rec.get("payload") if isinstance(rec.get("payload"), dict) else {}
        kind = rec.get("type")
        if kind == "session_meta":
            ctx["rollout_id"] = payload.get("id") or ctx.get("rollout_id")
            continue
        if kind == "turn_context":
            if isinstance(payload.get("model"), str):
                ctx["model"] = payload["model"]
            continue
        if kind != "event_msg" or payload.get("type") != "token_count":
            continue
        info = payload.get("info")
        if not isinstance(info, dict):  # null info: skipped, AC3
            continue
        total, last = info.get("total_token_usage"), info.get("last_token_usage")
        if not isinstance(total, dict) or not isinstance(last, dict):
            unknown += 1
            continue
        cumulative = _int(total.get("total_tokens"))
        if cumulative <= _int(ctx.get("last_total")):  # unchanged: a rate-limit refresh
            continue
        ctx["last_total"] = cumulative
        limits = payload.get("rate_limits") if isinstance(payload.get("rate_limits"), dict) else {}
        pct = {}
        for window in ("primary", "secondary"):
            item = limits.get(window) if isinstance(limits.get(window), dict) else {}
            minutes = item.get("window_minutes")
            name = "plan_used_pct_5h" if minutes == 300 else "plan_used_pct_7d" if minutes == 10080 else None
            if name and isinstance(item.get("used_percent"), (int, float)):
                pct[name] = float(item["used_percent"])
        cached = _int(last.get("cached_input_tokens"))
        rollout = ctx.get("rollout_id") or "unknown"
        usage = {"provider": "openai-codex", "model": ctx.get("model") or "unknown",
                 "input_uncached": max(_int(last.get("input_tokens")) - cached, 0),
                 "cached_read": cached, "cache_write": _int(last.get("cache_write_input_tokens")),
                 "output": _int(last.get("output_tokens")),
                 "reasoning_out": _int(last.get("reasoning_output_tokens")),
                 "session_id": rollout, "request_id": f"{rollout}:{cumulative}", **pct}
        out.append((usage, rec.get("timestamp") or "", f"codex:{rollout}:{cumulative}"))
    return out, unknown


# --- xAI (AC6, fixture only) ---------------------------------------------------

def normalize_xai(response: dict, session_id: str = "") -> dict:
    """Map a recorded xAI response's usage (Chat Completions or Responses
    shape) onto the normalized usage dict. Cached input is billed separately,
    so it is split out of the prompt/input count, never left in it."""
    usage = response.get("usage") if isinstance(response.get("usage"), dict) else {}
    if "prompt_tokens" in usage:  # Chat Completions
        total_in = _int(usage.get("prompt_tokens"))
        details = usage.get("prompt_tokens_details") or {}
        out_details = usage.get("completion_tokens_details") or {}
        output = _int(usage.get("completion_tokens"))
    else:  # Responses
        total_in = _int(usage.get("input_tokens"))
        details = usage.get("input_tokens_details") or {}
        out_details = usage.get("output_tokens_details") or {}
        output = _int(usage.get("output_tokens"))
    cached = _int(details.get("cached_tokens")) if isinstance(details, dict) else 0
    reasoning = _int(out_details.get("reasoning_tokens")) if isinstance(out_details, dict) else 0
    return {"provider": "xai", "model": str(response.get("model") or "unknown"),
            "input_uncached": max(total_in - cached, 0), "cached_read": cached, "cache_write": 0,
            "output": output, "reasoning_out": reasoning, "session_id": session_id,
            "request_id": str(response.get("id") or "")}


# --- Claude Code (AC4): deferred ------------------------------------------------

def harvest_claude(*_args: Any, **_kwargs: Any) -> None:
    """TODO(harmonic-forge#1188 AC4): build after the AC1 OTel-vs-transcript
    comparison is posted; AC1 decides whether the source is transcripts, OTel,
    or OTel with transcript fallback."""
    raise NotImplementedError("Claude Code harvester waits on harmonic-forge#1188 AC1")


# --- watermark + harvest --------------------------------------------------------

def _load_state() -> dict:
    try:
        state = json.loads(state_path().read_text(encoding="utf-8"))
        return state if isinstance(state, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_state(state: dict) -> None:
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def _new_lines(path: Path, mark: dict) -> tuple[list[str], int, int]:
    """Complete lines after the watermark, plus the new (inode, offset). A
    changed inode or a shrunk file restarts at 0 (rotation)."""
    st = path.stat()
    offset = mark.get("offset", 0) if mark.get("inode") == st.st_ino and mark.get("offset", 0) <= st.st_size else 0
    with path.open("rb") as handle:
        handle.seek(offset)
        data = handle.read()
    end = data.rfind(b"\n") + 1  # a torn last line waits for the next run
    text = data[:end].decode("utf-8", errors="replace")
    return [l for l in text.splitlines() if l.strip()], st.st_ino, offset + end


def harvest_codex(root: Path, prices: Optional[dict] = None) -> dict:
    state = _load_state()
    marks = state.setdefault("codex", {})
    totals = {"events": 0, "written": 0, "duplicate": 0, "rejected": 0, "unknown_lines": 0}
    for path in sorted(root.rglob("rollout-*.jsonl")):
        key = str(path)
        mark = marks.get(key, {})
        try:
            lines, inode, offset = _new_lines(path, mark)
        except OSError:
            continue
        ctx = dict(mark.get("ctx", {})) if mark.get("inode") == inode and offset >= mark.get("offset", 0) else {}
        if not ctx.get("rollout_id"):  # resumed past session_meta: the filename carries it
            ctx["rollout_id"] = path.stem.split("-", 6)[-1] if path.stem.count("-") >= 6 else None
        usages, unknown = codex_usages(lines, ctx)
        events = [to_event(u, ts, sid, prices) for u, ts, sid in usages]
        counts = emit.emit(events) if events else {"written": 0, "duplicate": 0, "rejected": []}
        totals["events"] += len(events)
        totals["written"] += counts["written"]
        totals["duplicate"] += counts["duplicate"]
        totals["rejected"] += len(counts["rejected"])
        totals["unknown_lines"] += unknown
        marks[key] = {"inode": inode, "offset": offset, "ctx": ctx}
    _save_state(state)
    return totals


def attention_minutes(gap_s: float, lead_bytes: int) -> float:
    """AC11 formula, unvalidated: min(gap, 10 min) + 0.5 per 2 KB of the lead
    answered, the lead capped at LEAD_CAP_BYTES."""
    return round(min(max(gap_s, 0) / 60.0, 10.0) + 0.5 * min(max(lead_bytes, 0), LEAD_CAP_BYTES) / 2048.0, 3)


def harvest_attention(root: Path) -> dict:
    state = _load_state()
    marks = state.setdefault("attention", {})
    events = []
    for path in sorted(root.glob("*.jsonl")):
        key = str(path)
        try:
            lines, inode, offset = _new_lines(path, marks.get(key, {}))
        except OSError:
            continue
        for raw in lines:
            try:
                rec = json.loads(raw)
                ts, sid = rec["ts"], rec["session_id"]
                gap, lead = float(rec.get("gap_s") or 0), int(rec.get("lead_bytes") or 0)
            except (ValueError, KeyError, TypeError):
                continue
            who = attribute(sid, ts)
            repo = who["repo"]
            events.append({
                "ts": ts, "source": "operator-attention", "repo": repo, "issue": who["issue"],
                "account": repo.split("/")[0] if repo else None, "org": repo.split("/")[0] if repo else None,
                "actor": "operator", "event_type": "operator-attention.prompt",
                "subject_kind": "prompt", "subject_id": f"{sid}:{ts}", "provenance": "hook",
                "validated": False,  # AC11: no report reads it until the timing comparison is posted
                "extractor_version": EXTRACTOR,
                "attrs": {k: v for k, v in {"minutes": attention_minutes(gap, lead), "gap_s": gap,
                                            "lead_bytes": lead, "session_id": sid,
                                            "lane": who["lane"], "attribution": who["attribution"]}.items()
                          if v is not None},
            })
        marks[key] = {"inode": inode, "offset": offset}
    counts = emit.emit(events) if events else {"written": 0, "duplicate": 0, "rejected": []}
    _save_state(state)
    return {"events": len(events), "written": counts["written"], "duplicate": counts["duplicate"]}


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Harvest LLM usage into the telemetry store")
    sub = parser.add_subparsers(dest="cmd", required=True)
    h = sub.add_parser("harvest")
    h.add_argument("--codex-root", type=Path, default=CODEX_ROOT)
    h.add_argument("--attention-root", type=Path, default=None)
    args = parser.parse_args(argv)
    result = {"codex": harvest_codex(args.codex_root) if args.codex_root.is_dir() else "absent",
              "attention": harvest_attention(args.attention_root or attention_dir())}
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
