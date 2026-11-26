#!/usr/bin/env python3
"""harmonic-forge#1565 PASS side (AC2) and #894's lookalike classes as
regressions (AC3): TCs 11-27, 38, 39 and TC 40's PASS half.

Each case starts from TC 1's genuine bound AE at S (posted through the real
`l1_post._post_kind` under a CI outage) plus a sweep, in real temp stores.
Only GitHub is stubbed: `gate_ci._run` answers the gh reads, and
`check_lane3_ready.fetch_comments` returns the thread. Each construction is
built so the named rule is the only one that refuses; the refusal text names it.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import gate_ci  # noqa: E402
import post_lane_discussion as P  # noqa: E402
from test_ci_outage_ae import ISSUE, KEY, REPO, S2, SLUG, World, ci_outage, check_receipt, force_grant, git_repo  # noqa: E402
from test_gate_ci import check, fake_gh  # noqa: E402

R1 = "release record(s) are bound"
R3 = "was edited after it was posted"
R4 = "the release record is for"
R5 = "is not an AE"
R6 = "no check-full receipt"
RED = "visibly red"
ABSENT = fake_gh(required={"verify"})


def pass_body(sha: str, extra: str = "") -> str:
    return f"## Lane 3 Gate Results — {KEY}\n\n**Verdict:** PASS\n**Head-SHA:** {sha}\n\nAll cases ran.{extra}\n"


class PassCase(World, unittest.TestCase):
    def setUp(self) -> None:
        self.build()
        self.grant()
        self.check_full()
        self.post_ae()
        self.ae = self.comments[0]
        self.sweep()

    def gate(self, sha: str | None = None, run=ABSENT, body: str | None = None) -> tuple[bool, str]:
        """`require_green_ci` end to end; on success, the release line read again."""
        body = body or pass_body(sha or self.S)
        with mock.patch.object(gate_ci, "_run", run), \
             mock.patch.object(P.check_lane3_ready, "fetch_comments", lambda r, i: list(self.comments)):
            try:
                P.require_green_ci("gate-result", REPO, body, issue=ISSUE)
            except SystemExit as exc:
                return False, str(exc)
            return True, ci_outage.pass_release(REPO, ISSUE, body, gate_ci.check_gate_result(REPO, body)[1])[1]

    def refused(self, needle: str, **kw) -> None:
        ok, message = self.gate(**kw)
        self.assertFalse(ok)
        self.assertIn(needle, message)

    def edit(self, comment: dict, body: str, rehash: bool = False) -> None:
        comment["body"] = body
        if rehash:
            self.edit_record(posted_sha256=hashlib.sha256(body.rstrip("\n").encode()).hexdigest())

    def edit_record(self, **fields) -> dict:
        [path] = sorted((self.state / "ci-outage").glob("*.json"))
        record = {**json.loads(path.read_text()), **fields}
        path.write_text(json.dumps(record))
        return record

    def drop_records(self) -> None:
        for path in (self.state / "ci-outage").glob("*.json"):
            path.unlink()

    def footer_of(self, comment: dict) -> str:
        return comment["body"][comment["body"].index("<!-- l1-post"):].rstrip("\n")


class PassRelease(PassCase):
    def test_11_a_bound_ae_releases_pending_or_absent_ci(self) -> None:
        pending = fake_gh(checks=[check("verify", None, "in_progress")], required={"verify"})
        for run in (ABSENT, pending):
            ok, line = self.gate(run=run)
            self.assertTrue(ok, line)
            self.assertTrue(line.startswith(f"[GATE] ci-outage: {self.S[:8]} released"), line)

    def test_12_a_later_force_line_on_the_key_still_releases(self) -> None:
        self.grant(mode="plain", cid="950", later=1)
        self.assertTrue(self.gate()[0])

    def test_13_red_ci_and_a_red_detail_naming_the_unfinished_line_refuse(self) -> None:
        self.refused("CI is RED", run=fake_gh(checks=[check("verify", "failure")]))
        red = (f"[GATE] REFUSED: CI is RED for the SHA this PASS names.\n  {self.S[:8]}: "
               f"{ci_outage.UNFINISHED_CI_REFUSAL}:failure")
        with mock.patch.object(gate_ci, "_run", ABSENT), \
             mock.patch.object(ci_outage.check_lane3_ready, "fetch_comments", lambda r, i: self.comments):
            self.assertFalse(ci_outage.pass_release(REPO, ISSUE, pass_body(self.S), red)[0])

    def test_14_the_constant_is_the_real_unfinished_refusal_first_line(self) -> None:
        with mock.patch.object(gate_ci, "_run", ABSENT):
            ok, message = gate_ci.check_gate_result(REPO, pass_body(self.S))
        self.assertFalse(ok)
        self.assertEqual(message.split("\n", 1)[0] + "\n", ci_outage.UNFINISHED_CI_REFUSAL)

    def test_15_a_stale_head_is_refused_even_with_a_record(self) -> None:
        stale = fake_gh(required={"verify"}, pulls=[{"number": 7, "head": "7" * 40, "ref": "feat/12-x",
                                                     "base": "main", "state": "open"}])
        self.refused("not the current head", run=stale)

    def test_16_a_head_sha_shorter_than_40_hex_is_refused(self) -> None:
        short = self.S[:12]
        self.edit_record(sha=short)
        self.sidecar(short, self.forge)
        self.refused("full 40-hex Head-SHA", sha=short)

    def test_38_a_null_forge_snapshot_is_refused(self) -> None:
        self.edit_record(receipt={"source_slug": SLUG, "forge_sha": None})
        self.sidecar(self.S, None, body_forge=None)
        self.refused(R6)

    def test_39_a_later_check_full_against_a_newer_forge_keeps_the_released_one(self) -> None:
        old = check_receipt.check_full_path(SLUG, self.S, self.forge)
        before = old.read_bytes()
        newer = git_repo(self.forge_root)
        self.check_full()
        self.assertEqual(old.read_bytes(), before)
        self.assertTrue(check_receipt.check_full_path(SLUG, self.S, newer).exists())
        self.assertTrue(self.gate()[0])


class Lookalikes(PassCase):
    """#894's lookalike classes, rounds 1-5 and the post-verdict check."""

    def bare_ae(self, prose: str) -> str:
        return self.footered(f"## Lane 1 AE — {KEY}\n\n{prose}\n", "ae", self.S)

    def test_17_a_prose_grant_id_with_no_record(self) -> None:
        self.drop_records()
        self.edit(self.ae, self.bare_ae("ci-override=777;\n**CI substitute:** ci-outage grant "
                                        f"https://github.com/{REPO}/issues/{ISSUE}#issuecomment-777"))
        self.refused(R1)

    def test_18_a_grant_id_only_in_the_pass_body(self) -> None:
        self.drop_records()
        self.refused(R1, body=pass_body(self.S, "\nci-override=777;"))

    def test_19_text_after_the_footer_closes(self) -> None:
        self.edit(self.ae, self.ae["body"].rstrip("\n") + " ci-override=777;\n")
        self.refused(R3)

    def test_20_an_earlier_unrelated_footer(self) -> None:
        decoy = f"<!-- l1-post v1; posted-by=lane3; kind=sweep; sha={self.S} -->\n\n"
        self.edit(self.ae, self.ae["body"].replace("<!-- l1-post", decoy + "<!-- l1-post", 1))
        self.refused(R3)

    def test_21_an_ae_shaped_footer_above_the_real_one(self) -> None:
        text = self.ae["body"]
        decoy = self.footered(text[:text.index("<!-- l1-post")], "ae", self.S, "; ci-override=777")
        self.edit(self.ae, decoy + "\n" + self.footer_of(self.ae) + "\n")
        self.refused(R3)

    def test_21_variant_a_copy_posted_later_is_the_latest_ae(self) -> None:
        self.add(self.ae["body"], cid=self.ae["id"] + 5)
        self.comments.sort(key=lambda c: c["id"])
        self.refused(R1)

    def test_22_unclosed_lookalikes_above_and_on_the_footer_line(self) -> None:
        body = self.ae["body"].replace("<!-- l1-post", f"<!-- l1-post v1; kind=ae; sha={self.S};\n\n<!-- l1-post", 1)
        self.edit(self.ae, body.replace(" -->", " ci-override=777; -->"))
        self.refused(R3)

    def test_23_a_cr_inside_the_footer(self) -> None:
        self.edit(self.ae, self.ae["body"].replace("; checks=", ";\r checks="))
        self.refused(R3)

    def test_24a_a_carry_forward_quoting_the_ae_footer_is_not_an_ae(self) -> None:
        carry = self.add(self.footered(f"## Ready for Lane 3 — {KEY}\n\nFix pushed.\n", "ready-for-l3", S2)
                         + f"> quoted: {self.footer_of(self.ae)}\n")
        self.edit_record(ae_comment_id=carry["id"], sha=S2,
                         posted_sha256=hashlib.sha256(carry["body"].rstrip("\n").encode()).hexdigest())
        self.sidecar(S2, self.forge)
        self.refused(R5, sha=S2)

    def test_24b_a_record_spent_for_one_sha_presented_for_another(self) -> None:
        self.edit_record(sha=S2)
        self.refused(R4)

    def test_25_from_sha_read_as_sha(self) -> None:
        self.edit(self.ae, self.ae["body"].replace(f"sha={self.S};", f"from-sha={S2}; sha={self.S};"), rehash=True)
        self.sidecar(S2, self.forge)
        self.refused(R4, sha=S2)

    def test_26a_a_footer_field_added_to_an_ae_with_no_record(self) -> None:
        self.drop_records()
        self.edit(self.ae, self.ae["body"].replace(" -->", " ci-override=777; -->"))
        self.refused(R1)

    def test_26b_the_bound_aes_footer_sha_rewritten(self) -> None:
        self.edit(self.ae, self.ae["body"].replace(f"sha={self.S};", f"sha={self.S[:12]};"))
        self.refused(R3)

    def test_27_a_tier_r_sweep_authority_with_a_forged_record(self) -> None:
        self.comments.clear()
        sweep = self.sweep(tier="R")
        self.edit_record(ae_comment_id=sweep["id"],
                         posted_sha256=hashlib.sha256(sweep["body"].rstrip("\n").encode()).hexdigest())
        self.refused(R5)


class PassVisibleRed(PassCase):
    """TC 40, PASS half: (a) `verify` running beside a failed job reads pending,
    (b) `verify` not yet created beside a failed job reads absent."""
    SHAPES = {"pending": fake_gh(checks=[check("verify", None, "in_progress"), check("lint", "failure")],
                                 required={"verify"}, run_status="in_progress"),
              "absent": fake_gh(checks=[check("lint", "failure")], required={"verify"})}

    def test_40_a_bound_record_never_releases_a_visible_red_job(self) -> None:
        for state, run in self.SHAPES.items():
            with self.subTest(state=state):
                self.refused(RED, run=run)
                self.assertIn(f"{state} —", self.gate(run=run)[1])

    def test_40_no_record_refuses(self) -> None:
        self.drop_records()
        for state, run in self.SHAPES.items():
            with self.subTest(state=state):
                self.refused(R1, run=run)


if __name__ == "__main__":
    unittest.main()
