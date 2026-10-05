#!/usr/bin/env python3
"""Write schema-v1 events to the telemetry store (harmonic-forge#828, ADR-009).

`emit()` validates each event against `schema_v1.json`, derives its
deterministic `event_id`, and appends it to
`<store>/events/<account>/<org>/<source>/<YYYY-MM>.jsonl` unless that id is
already present -- so re-extraction is idempotent (design.md AC10).

Never-drop (ADR-009 §3): an event whose account/org/repo cannot be resolved
is written under `unresolved`, not refused. An event that fails validation
for any other reason (unknown source, a body in attrs, a bad ts, no
subject_id) is rejected *on its own* -- the rest of its batch is written --
and returned in `rejected` with its reason, never its payload.

`event_id` covers source, repo, issue, subject_id, event_type and the
UTC-normalized ts; it deliberately excludes account/org, and the dedupe read
also covers the matching `unresolved` partition, so an event re-extracted
after its repo is onboarded is not written twice.

`ingest_archive()` loads harmonic-forge#826's gzip archive envelopes as
`source=archive` events keyed on the envelope's `record_hash` (not
`archived_at`), so a record archived twice becomes one event.

The store root is `$HARMONIC_FORGE_TELEMETRY_STORE`, default
`~/Harmonic_Projects/telemetry-store` (the private repo's checkout).
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

HERE = Path(__file__).resolve().parent
SCHEMA = json.loads((HERE / "schema_v1.json").read_text(encoding="utf-8"))
STORE_ENV = "HARMONIC_FORGE_TELEMETRY_STORE"
UNRESOLVED = "unresolved"
#: attrs key tokens that would carry free text are refused rather than stored.
_BODY_TOKENS = {"body", "text", "message", "quote", "content", "description", "notes",
                "excerpt", "snippet", "summary", "comment", "prose"}
#: ...except these, which are identifiers, not text.
_ID_SUFFIXES = ("_id", "_ids", "_count", "_url", "_sha", "_kind")
_ATTR_STR_MAX = 200


class SchemaError(ValueError):
    pass


#: The operator's real telemetry store -- the one literal for it
#: (harmonic-forge#907, mirroring belt_candidates.REAL_CANDIDATES_DIR).
REAL_STORE = Path.home() / "Harmonic_Projects" / "telemetry-store"
TESTING_ENV = "HARMONIC_FORGE_TESTING"


def store_root() -> Path:
    return Path(os.environ.get(STORE_ENV) or REAL_STORE).expanduser()


def _refuses_real_store(root: Path) -> bool:
    """Whether a write to `root` must be refused because this is a test process
    and `root` is the operator's real store (harmonic-forge#907, the
    harmonic-forge#865 guard applied to telemetry).

    In-process, `unittest` being imported marks a test (no production writer
    imports it); a writer a test launched as a subprocess is marked by the
    inherited `HARMONIC_FORGE_TESTING=1`."""
    under_test = "unittest" in sys.modules or os.environ.get(TESTING_ENV) == "1"
    if not under_test:
        return False
    try:
        return Path(root).resolve() == Path(REAL_STORE).resolve()
    except OSError:
        return True  # cannot tell: refuse, never write the real store from a test


def event_id(source: str, repo: Optional[str], issue: Any, subject_id: Optional[str],
             event_type: str, ts: str) -> str:
    parts = [source, repo or "", "" if issue is None else str(issue), subject_id or "", event_type, ts]
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()


def normalize_ts(ts: Any) -> str:
    if not isinstance(ts, str) or not ts:
        raise SchemaError(f"ts must be an ISO 8601 string, got {type(ts).__name__}")
    try:
        parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        raise SchemaError(f"ts is not ISO 8601: {ts[:40]!r}") from None
    if parsed.tzinfo is None:
        raise SchemaError("ts has no timezone; a naive time is ambiguous")
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _body_key(key: str) -> bool:
    lowered = key.lower()
    if lowered.endswith(_ID_SUFFIXES):
        return False
    return bool(_BODY_TOKENS & set(re.split(r"[^a-z]+", lowered)))


def validate(event: dict[str, Any]) -> None:
    """Raise SchemaError for anything but an unresolved account/org/repo,
    which `emit()` fills in rather than refusing."""
    for col in ("source", "event_type", "subject_id"):
        if not event.get(col):
            raise SchemaError(f"missing required: {col}")
    if event["source"] not in SCHEMA["columns"]["source"]:
        raise SchemaError(f"unknown source {event['source']!r}")
    prov = event.get("provenance")
    if prov is not None and prov not in SCHEMA["columns"]["provenance"]:
        raise SchemaError(f"unknown provenance {prov!r}")
    normalize_ts(event.get("ts"))
    attrs = event.get("attrs") or {}
    if not isinstance(attrs, dict):
        raise SchemaError("attrs must be an object")
    for key, value in attrs.items():
        if _body_key(key):
            raise SchemaError(f"attrs may not carry bodies: {key}")
        if value is not None and not isinstance(value, (str, int, float, bool)):
            raise SchemaError(f"attrs.{key} must be a scalar, not {type(value).__name__}")
        if isinstance(value, str) and len(value) > _ATTR_STR_MAX:
            raise SchemaError(f"attrs.{key} is {len(value)} chars; >{_ATTR_STR_MAX} reads as a body")


def _partition(root: Path, account: str, org: str, source: str, ts: str) -> Path:
    return root / "events" / account / org / source / f"{ts[:7]}.jsonl"


def _read_ids(part: Path) -> set[str]:
    """Ids already in a partition. A line that doesn't parse (a torn write, a
    merge conflict marker) is skipped with a warning naming it, never allowed
    to wedge every later write to the partition."""
    seen: set[str] = set()
    if not part.exists():
        return seen
    with part.open(encoding="utf-8", errors="replace") as handle:
        for lineno, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                seen.add(json.loads(line)["event_id"])
            except (ValueError, KeyError, TypeError):
                print(f"[telemetry-emit] skipping unparseable line {part}:{lineno}", file=sys.stderr)
    return seen


def prepare(raw: dict[str, Any]) -> dict[str, Any]:
    """Validate and complete one event; raises SchemaError."""
    event = {"schema_version": SCHEMA["version"], **raw}
    validate(event)
    event["ts"] = normalize_ts(event["ts"])
    for col in ("account", "org", "repo"):
        if not event.get(col):
            event[col] = UNRESOLVED
    if not event.get("event_id"):
        event["event_id"] = event_id(event["source"], event["repo"], event.get("issue"),
                                     event["subject_id"], event["event_type"], event["ts"])
    return event


def emit(events: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Append new events. Returns {"written", "duplicate", "rejected"}, where
    `rejected` lists (index, event_type, reason) -- never the payload."""
    root = store_root()
    counts: dict[str, Any] = {"written": 0, "duplicate": 0, "rejected": []}
    if _refuses_real_store(root):
        print("[telemetry] test process: not writing the real telemetry store "
              f"(set {STORE_ENV}) -- harmonic-forge#907", file=sys.stderr)
        return counts
    by_part: dict[Path, list[dict[str, Any]]] = {}
    for index, raw in enumerate(events):
        try:
            event = prepare(raw)
        except SchemaError as exc:
            counts["rejected"].append((index, str(raw.get("event_type", "")), str(exc)))
            continue
        part = _partition(root, event["account"], event["org"], event["source"], event["ts"])
        by_part.setdefault(part, []).append(event)
    for part, items in by_part.items():
        part.parent.mkdir(parents=True, exist_ok=True)
        with part.with_name(part.name + ".lock").open("a") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                seen = _read_ids(part)
                first = items[0]
                unresolved = _partition(root, UNRESOLVED, UNRESOLVED, first["source"], first["ts"])
                if unresolved != part:
                    seen |= _read_ids(unresolved)
                fresh = []
                for e in items:
                    if e["event_id"] in seen:
                        counts["duplicate"] += 1
                        continue
                    seen.add(e["event_id"])
                    fresh.append(e)
                if fresh:
                    with part.open("a", encoding="utf-8") as handle:
                        handle.write("".join(json.dumps(e, sort_keys=True) + "\n" for e in fresh))
                        handle.flush()
                        os.fsync(handle.fileno())
                counts["written"] += len(fresh)
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    return counts


def ingest_archive(archive_root: Path) -> dict[str, Any]:
    """Load every harmonic-forge#826 archive envelope as a `source=archive`
    event. Identity is the envelope's record_hash (recomputed from the record
    when an envelope lacks one), never archived_at."""
    import gzip
    events = []
    for part in sorted(archive_root.rglob("*.jsonl.gz")):
        with gzip.open(part, "rt", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    env = json.loads(line)
                except ValueError:
                    print(f"[telemetry-emit] skipping unparseable archive line in {part}", file=sys.stderr)
                    continue
                record_hash = env.get("record_hash") or hashlib.sha256(
                    json.dumps(env.get("record"), sort_keys=True).encode("utf-8")).hexdigest()
                repo = env.get("repo") or UNRESOLVED
                event_type = f"archived.{env.get('source', 'unknown')}"
                events.append({
                    "event_id": event_id("archive", repo, None, record_hash, event_type, ""),
                    "ts": env.get("archived_at"), "source": "archive",
                    "account": env.get("account"), "org": env.get("org"), "repo": repo,
                    "event_type": event_type, "actor": "hook:archive",
                    "subject_kind": "record_hash", "subject_id": record_hash,
                    "provenance": "archive", "validated": True, "extractor_version": "ingest-2",
                    "attrs": {"archive_source": str(env.get("source", ""))},
                })
    return emit(events)
