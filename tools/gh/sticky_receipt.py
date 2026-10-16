#!/usr/bin/env python3
"""Sticky-wicket receipts for repeat reworks (harmonic-forge#997).

`3-lane-protocol.md`'s sticky-wicket trigger is countable: two consecutive
non-PASS Lane 3 gate results on one issue. Nothing enforced it, so a third
rework could post with no fresh look at the approach (openclaw-projects#35 took
five). `l1_post.py --kind rework` now refuses once the last two gate results on
the issue are both non-PASS, unless this module holds a receipt recording the
sticky-wicket verdict through the newer of them.

A gate result is read with the same two signals the belt uses
(`watch_lane_posts._is_gate_report`, `gate_ci.verdict_of`). It counts as
non-PASS unless its verdict is exactly `PASS`: FAIL, BLOCKED, CONFLICT and an
unreadable verdict all fail closed.

The receipt records the verdict (PATCH or REFORGE), the gate-result comment it
covers (`through`), and the URL of the sticky-wicket report Lane 1 posted,
verified through the API to be a comment on the same issue. PATCH is also the
exit when both results were environment blocks (an outage, say): the receipt
says sticky-wicket looked, not that the approach changed.

One exemption (harmonic-forge#1019): two BLOCKED results at one Head-SHA, with
the third-newest not also BLOCKED there, do not refuse an AE or sweep attesting
that same SHA, and do not wake the belt. Nothing was patched between them, so
there is no approach to judge (hrse#2379). A rework, handoff or ready-for-l3
after the pair still refuses, and a third block at that SHA is owed as before.

    sticky_receipt.py record --repo OWNER/REPO --issue N --verdict PATCH \\
        --through <gate-result comment id> --comment-url <sticky-wicket report URL>
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

VERDICTS = ("PATCH", "REFORGE")


def receipt_dir() -> Path:
    return Path.home() / ".claude" / "state" / "sticky"


def receipt_path(repo: str, issue: int) -> Path:
    return receipt_dir() / f"{repo.replace('/', '_')}_{issue}.json"


def read(repo: str, issue: int) -> dict | None:
    """A receipt that cannot be read, or names another issue, is no receipt."""
    try:
        data = json.loads(receipt_path(repo, issue).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("repo") != repo or data.get("issue") != issue:
        return None
    return data


def _gh_json(path: str) -> object:
    result = subprocess.run(["gh", "api", "--paginate", path], capture_output=True, text=True,
                            check=False)
    if result.returncode != 0:
        raise SystemExit(f"sticky-receipt: cannot read {path}: {result.stderr.strip()}")
    try:
        return json.loads(result.stdout)
    except ValueError:
        # `--paginate` may emit several arrays back to back.
        decoder, index, items = json.JSONDecoder(), 0, []
        text = result.stdout
        while index < len(text):
            while index < len(text) and text[index].isspace():
                index += 1
            if index >= len(text):
                break
            page, index = decoder.raw_decode(text, index)
            items.extend(page if isinstance(page, list) else [page])
        return items


def parse_pages(text: str) -> list[dict]:
    """Every comment from `gh api --paginate` output, which may be several
    concatenated JSON arrays."""
    decoder, index, items = json.JSONDecoder(), 0, []
    text = text or ""
    while index < len(text):
        while index < len(text) and text[index].isspace():
            index += 1
        if index >= len(text):
            break
        page, index = decoder.raw_decode(text, index)
        items.extend(page if isinstance(page, list) else [page])
    return items


def gate_results(comments: list[dict]) -> list[tuple[int, bool]]:
    """`(comment id, is_pass)` for every Lane 3 gate result, oldest first."""
    import gate_ci  # noqa: PLC0415
    import watch_lane_posts as belt  # noqa: PLC0415

    found = []
    for comment in comments:
        body = comment.get("body") or ""
        classified = belt._classify(body)
        detail = classified[1] if classified else ""
        if belt._is_gate_report(detail, body):
            found.append((int(comment["id"]), gate_ci.verdict_of(body) == "PASS"))
    return found


def gate_details(comments: list[dict]) -> list[tuple[int, str | None, str | None]]:
    """`(comment id, verdict, gated SHA)` for every Lane 3 gate result, oldest first.

    The same classification as `gate_results`; `verdict` is `gate_ci.verdict_of`
    and the SHA is `gate_ci.gated_sha` (None when the report names none)."""
    import gate_ci  # noqa: PLC0415
    import watch_lane_posts as belt  # noqa: PLC0415

    found = []
    for comment in comments:
        body = comment.get("body") or ""
        classified = belt._classify(body)
        detail = classified[1] if classified else ""
        if belt._is_gate_report(detail, body):
            found.append((int(comment["id"]), gate_ci.verdict_of(body), gate_ci.gated_sha(body)))
    return found


def _same_sha(a: str, b: str) -> bool:
    """A gate result may carry a short SHA; compare by prefix both ways."""
    return a.startswith(b) or b.startswith(a)


def same_sha_retry(details: list[tuple[int, str | None, str | None]]) -> str | None:
    """harmonic-forge#1019: the SHA when the last two gate results are both BLOCKED
    at one non-empty Head-SHA and the third-newest is not also BLOCKED there."""
    if len(details) < 2:
        return None
    (_, v1, s1), (_, v2, s2) = details[-2], details[-1]
    if v1 != "BLOCKED" or v2 != "BLOCKED" or not s1 or not s2 or not _same_sha(s1, s2):
        return None
    if len(details) >= 3:
        _, v0, s0 = details[-3]
        if v0 == "BLOCKED" and s0 and _same_sha(s0, s2):
            return None
    return s2


#: The posts that change no code, so a same-SHA blocked retry may precede them.
RETRY_EXEMPT_KINDS = ("ae", "sweep", "ae-and-sweep")


def owed(comments: list[dict]) -> tuple[int, int] | None:
    """`(older, newer)` gate-result ids when the last two are both non-PASS."""
    results = gate_results(comments)
    if len(results) < 2:
        return None
    (older, older_pass), (newer, newer_pass) = results[-2], results[-1]
    if older_pass or newer_pass:
        return None
    return older, newer


def covered(repo: str, issue: int, newer: int) -> bool:
    receipt = read(repo, issue)
    return bool(receipt and receipt.get("verdict") in VERDICTS
                and int(receipt.get("through") or 0) >= newer)


def refusal(repo: str, issue: int, comments: list[dict], *, kind: str | None = None,
            attested_sha: str | None = None) -> str | None:
    """Why a rework on this issue must not post yet, or None."""
    pair = owed(comments)
    if pair is None or covered(repo, issue, pair[1]):
        return None
    if kind in RETRY_EXEMPT_KINDS and attested_sha:
        retry = same_sha_retry(gate_details(comments))
        if retry and _same_sha(retry, attested_sha):
            return None
    older, newer = pair
    return (f"{repo}#{issue}: the last two Lane 3 gate results (issuecomment-{older}, "
            f"issuecomment-{newer}) are both non-PASS, so sticky-wicket is owed before another "
            "rework (3-lane-protocol.md's countable trigger). Run the sticky-wicket agent, post its "
            "verdict, then:\n  python3 ~/harmonic-forge/tools/gh/sticky_receipt.py record "
            f"--repo {repo} --issue {issue} --verdict <PATCH|REFORGE> --through {newer} "
            "--comment-url <the posted verdict's URL>")


def _verify_comment(repo: str, issue: int, url: str, through: int, verdict: str) -> str:
    """The report must be a comment on this issue, posted after the gate result it
    covers, that names sticky-wicket and states the recorded verdict."""
    match = re.search(r"(?:issuecomment-|/issues/comments/)(\d+)", url or "")
    if not match:
        raise SystemExit("sticky-receipt: --comment-url must name an issue comment")
    data = _gh_json(f"repos/{repo}/issues/comments/{match.group(1)}")
    if not isinstance(data, dict) or not str(data.get("issue_url", "")).endswith(f"/issues/{issue}"):
        raise SystemExit(f"sticky-receipt: {url} is not a comment on {repo}#{issue}")
    if int(data.get("id") or 0) <= through:
        raise SystemExit(f"sticky-receipt: {url} predates gate result issuecomment-{through}")
    body = str(data.get("body") or "")
    if not re.search(r"sticky[- ]wicket", body, re.I) or not re.search(rf"\b{verdict}\b", body):
        raise SystemExit(f"sticky-receipt: {url} does not name sticky-wicket and the verdict {verdict}")
    return str(data.get("html_url") or url)


def record(repo: str, issue: int, verdict: str, through: int, comment_url: str) -> Path:
    if verdict not in VERDICTS:
        raise SystemExit(f"sticky-receipt: verdict must be one of {', '.join(VERDICTS)}")
    comments = _gh_json(f"repos/{repo}/issues/{issue}/comments")
    if through not in {cid for cid, _ in gate_results(comments if isinstance(comments, list) else [])}:
        raise SystemExit(f"sticky-receipt: issuecomment-{through} is not a gate result on {repo}#{issue}")
    canonical = _verify_comment(repo, issue, comment_url, through, verdict)
    directory = receipt_dir()
    directory.mkdir(parents=True, exist_ok=True)
    payload = {"repo": repo, "issue": issue, "verdict": verdict, "through": through,
               "comment_url": canonical,
               "recorded_at": datetime.datetime.now(datetime.timezone.utc).isoformat()}
    handle = tempfile.NamedTemporaryFile("w", dir=directory, delete=False, suffix=".tmp")
    try:
        json.dump(payload, handle, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    finally:
        handle.close()
    path = receipt_path(repo, issue)
    os.replace(handle.name, path)
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    rec = sub.add_parser("record", help="Record the sticky-wicket verdict through a gate result.")
    rec.add_argument("--repo", required=True)
    rec.add_argument("--issue", type=int, required=True)
    rec.add_argument("--verdict", required=True, choices=VERDICTS)
    rec.add_argument("--through", type=int, required=True,
                     help="The newest gate-result comment id the verdict covers.")
    rec.add_argument("--comment-url", required=True,
                     help="The posted sticky-wicket report on the same issue.")
    args = parser.parse_args()
    path = record(args.repo, args.issue, args.verdict, args.through, args.comment_url)
    print(f"sticky-receipt: {args.verdict} recorded for {args.repo}#{args.issue} through "
          f"issuecomment-{args.through}\n  {path}")


if __name__ == "__main__":
    main()
