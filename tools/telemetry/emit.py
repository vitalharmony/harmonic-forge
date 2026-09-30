#!/usr/bin/env python3
"""Write schema-v1 events to the telemetry store (harmonic-forge#828, ADR-009).

`emit()` validates an event against `schema_v1.json`, derives its
deterministic `event_id`, and appends it to
`<store>/events/<account>/<org>/<source>/<YYYY-MM>.jsonl` unless that id is
already in the partition -- so re-extraction is idempotent (design.md AC10).

`ingest_archive()` loads harmonic-forge#826's gzip archive envelopes as
`source=archive` events, using the envelope's `record_hash` as the subject
so a record archived twice (at-least-once) becomes one event.

The store root is `$HARMONIC_FORGE_TELEMETRY_STORE`, default
`~/Harmonic_Projects/telemetry-store` (the private repo's checkout).
Payloads are ids, kinds, timestamps and hashes -- never bodies (ADR-009 §5).
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Iterable, Optional

HERE = Path(__file__).resolve().parent
SCHEMA = json.loads((HERE / "schema_v1.json").read_text(encoding="utf-8"))
STORE_ENV = "HARMONIC_FORGE_TELEMETRY_STORE"
#: attrs keys that would carry free text are refused rather than stored.
_BODY_KEYS = {"body", "text", "message", "quote", "content", "description", "notes"}


class SchemaError(ValueError):
    pass


def store_root() -> Path:
    return Path(os.environ.get(STORE_ENV) or Path.home() / "Harmonic_Projects/telemetry-store").expanduser()


def event_id(source: str, subject_id: Optional[str], event_type: str, ts: str) -> str:
    return hashlib.sha256("\x1f".join([source, subject_id or "", event_type, ts]).encode("utf-8")).hexdigest()


def validate(event: dict[str, Any]) -> None:
    missing = [c for c in SCHEMA["required"] if c != "event_id" and not event.get(c)]
    if missing:
        raise SchemaError(f"missing required: {', '.join(missing)}")
    if event["source"] not in SCHEMA["columns"]["source"]:
        raise SchemaError(f"unknown source {event['source']!r}")
    prov = event.get("provenance")
    if prov is not None and prov not in SCHEMA["columns"]["provenance"]:
        raise SchemaError(f"unknown provenance {prov!r}")
    bad = _BODY_KEYS & set((event.get("attrs") or {}))
    if bad:
        raise SchemaError(f"attrs may not carry bodies: {', '.join(sorted(bad))}")


def _partition(event: dict[str, Any]) -> Path:
    return (store_root() / "events" / event["account"] / event["org"] / event["source"]
            / f"{event['ts'][:7]}.jsonl")


def emit(events: Iterable[dict[str, Any]]) -> dict[str, int]:
    """Append new events; returns {"written": n, "duplicate": m}. Raises
    SchemaError on an invalid event before anything in the batch is written."""
    batch = []
    for raw in events:
        event = {"schema_version": SCHEMA["version"], **raw}
        validate(event)
        event["event_id"] = event.get("event_id") or event_id(
            event["source"], event.get("subject_id"), event["event_type"], event["ts"])
        batch.append(event)
    counts = {"written": 0, "duplicate": 0}
    by_part: dict[Path, list[dict[str, Any]]] = {}
    for event in batch:
        by_part.setdefault(_partition(event), []).append(event)
    for part, items in by_part.items():
        part.parent.mkdir(parents=True, exist_ok=True)
        with part.with_name(part.name + ".lock").open("a") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                seen = set()
                if part.exists():
                    with part.open(encoding="utf-8") as handle:
                        seen = {json.loads(l)["event_id"] for l in handle if l.strip()}
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


def ingest_archive(archive_root: Path) -> dict[str, int]:
    """Load every harmonic-forge#826 archive envelope as a `source=archive` event."""
    import gzip
    events = []
    for part in sorted(archive_root.rglob("*.jsonl.gz")):
        with gzip.open(part, "rt", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                env = json.loads(line)
                events.append({
                    "ts": env["archived_at"], "source": "archive",
                    "account": env["account"], "org": env["org"], "repo": env.get("repo"),
                    "event_type": f"archived.{env['source']}", "actor": "hook:archive",
                    "subject_kind": "record_hash", "subject_id": env.get("record_hash"),
                    "provenance": "archive", "validated": True, "extractor_version": "ingest-1",
                    "attrs": {"archive_source": env["source"]},
                })
    return emit(events)
