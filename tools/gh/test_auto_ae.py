"""harmonic-forge#874: the post-time check behind R-0378 (the operator's
`/auto-ae` toggle) -- what an AE claiming auto-AE must show before it posts,
and that an auto-AE never carries forward to a new SHA."""
import hashlib
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))

import _auto_ae as auto  # noqa: E402
import check_lane3_ready as clr  # noqa: E402
import l1_post  # noqa: E402

REPO, ISSUE, KEY = "vitalharmony/hrse", 42, "H42"
SHA = "a" * 40
OTHER = "b" * 40
NOW = datetime(2026, 10, 4, 18, 0, tzinfo=timezone.utc)
READY_ID, SPEC_ID = 200, 210


def _footered(prefix: str, kind: str, extra: str = "") -> str:
    digest = hashlib.sha256(prefix.rstrip("\n").encode()).hexdigest()
    return f"{prefix}\n\n<!-- l1-post v1; kind={kind};{extra} body-sha256={digest} -->"


def ready(comment_id: int = READY_ID, sha: str = SHA) -> dict:
    return {"id": comment_id, "body": _footered("## ready-for-l3 — H42", "ready-for-l3", f" sha={sha};")}


def spec(comment_id: int = SPEC_ID, tier: str | None = "W", edited: bool = False,
         marker: bool = True, tier_line: str | None = None, extra: str = "") -> dict:
    # The declaration form harmonic-forge#947 requires: the colon, then the letter.
    lines = ["## Lane 3 Test Spec — H42", "", "**Cases:** 2 cases.", ""]
    if tier_line is not None:
        lines.append(tier_line)
    elif tier:
        lines.append(f"Proposed ceiling **{tier}**. Write tier: **{tier}** throughout.")
    if extra:
        lines.append(extra)
    lines += ["", "### Test cases", "1. TC1 — read the list.", "2. TC2 — dismiss and undo."]
    body = _footered("\n".join(lines), "spec", " posted-by=LANE3;")
    if not marker:  # a footer naming the kind, with no digest
        body = body.split("\n\n<!--")[0] + "\n\n<!-- l1-post v1; kind=spec; posted-by=LANE3 -->"
    if edited:
        body = body.replace("2 cases.", "3 cases.")
    return {"id": comment_id, "body": body}


def ae_body(spec_id: int = SPEC_ID, cite: str = "auto-AE (R-0378)") -> str:
    authorized = (f"**Authorized:** {cite}: Lane 1 approved Lane 3's spec "
                  f"issuecomment-{spec_id} under the operator's /auto-ae toggle." if cite
                  else "**Authorized:** the operator, in chat.")
    return f"## AE — H42\n\n{authorized}\n**Next:** Lane 3 executes TC1–TC2."


def sweep_body(tier: str = "W") -> str:
    return (f"## Gate-readiness sweep — H42\n\n**Readiness:** ready.\n\nWrite tier: {tier}\n\n"
            "### Test cases\n1. TC1 — ready.\n2. TC2 — ready.\n")


class AutoAeCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.toggle = self.dir / "auto-ae.json"
        self.batch = self.dir / "batch-authorized.json"
        self.set_lease(KEY)
        self.set_toggle(True)

    def set_toggle(self, on: bool | None) -> None:
        if on is None:
            self.toggle.write_text("{not json")
        else:
            self.toggle.write_text(json.dumps({"on": on, "lane": "1", "leases_at_set": {KEY: self.lease_expiry},
                                               "expires_at": (NOW + timedelta(hours=12)).isoformat()}))

    def set_lease(self, key: str, expires: datetime | None = None, consumed: bool = False) -> None:
        expires = expires or NOW + timedelta(hours=2)
        self.lease_expiry = expires.isoformat()
        self.batch.write_text(json.dumps({key: {
            "authorized_at": NOW.isoformat(), "expires_at": expires.isoformat(),
            "targets": [{"action": "gh pr merge", "consumed": consumed, "consumed_by": None,
                         "repo": None, "pr_number": None}]}}))

    def refusal(self, ae: str | None = None, sweep: str | None = None, comments=None, *,
                sha: str = SHA, spec_comment: int | None = SPEC_ID, prod_run: bool = False,
                ack: str | None = None, no_sweep: bool = False) -> str | None:
        return auto.auto_ae_refusal(
            ae if ae is not None else ae_body(), None if no_sweep else (sweep or sweep_body()),
            REPO, ISSUE, sha, comments if comments is not None else [ready(), spec()],
            spec_comment=spec_comment, prod_run=prod_run, ack_no_pr_required=ack, now=NOW,
            toggle_state=self.toggle, batch_state=self.batch)


class CitesAutoAeTests(unittest.TestCase):
    def test_the_authorized_line_naming_auto_ae_is_a_claim(self):
        self.assertTrue(auto.cites_auto_ae(ae_body()))

    def test_the_rule_id_alone_or_the_name_alone_is_still_a_claim(self):
        self.assertTrue(auto.cites_auto_ae(ae_body(cite="R-0378")))
        self.assertTrue(auto.cites_auto_ae(ae_body(cite="auto-AE")))

    def test_a_mention_off_the_authorized_line_is_not_a_claim(self):
        body = ae_body(cite="") + "\n\nThis is not an auto-AE (R-0378)."
        self.assertFalse(auto.cites_auto_ae(body))

    def test_a_fenced_authorized_line_is_quoted_evidence(self):
        body = "## AE — H42\n\n```\n**Authorized:** auto-AE (R-0378)\n```\n**Authorized:** the operator."
        self.assertFalse(auto.cites_auto_ae(body))


class AutoAeRefusalTests(AutoAeCase):
    def test_everything_in_place_posts(self):
        self.assertIsNone(self.refusal())

    def test_auto_ae_with_the_toggle_off_is_refused(self):
        self.set_toggle(False)
        self.assertIn("auto-AE is off", self.refusal())

    def test_an_unreadable_toggle_state_is_off(self):
        self.set_toggle(None)
        self.assertIn("auto-AE is off", self.refusal())

    def test_auto_ae_for_an_issue_outside_any_lease_is_refused(self):
        self.set_lease("H43")
        self.assertIn("not under a live BATCH lease", self.refusal())

    def test_an_expired_lease_is_refused(self):
        self.set_lease(KEY, expires=NOW - timedelta(minutes=1))
        self.assertIn("not under a live BATCH lease", self.refusal())

    def test_a_lease_whose_merge_is_spent_is_refused(self):
        self.set_lease(KEY, consumed=True)
        self.assertIn("not under a live BATCH lease", self.refusal())

    def test_auto_ae_with_a_tier_p_sweep_is_refused(self):
        self.assertIn("sweep mentions Tier P", self.refusal(sweep=sweep_body("P")))

    def test_a_tier_p_spec_is_refused_whole(self):
        reason = self.refusal(comments=[ready(), spec(tier="P")])
        self.assertIn("Tier P", reason)

    def test_the_plain_colon_tier_form_also_posts(self):
        self.assertIsNone(self.refusal(comments=[ready(), spec(tier_line="Write tier: R")]))

    def test_a_bolded_or_backticked_tier_p_is_refused(self):
        for line in ("Write tier **P** for TC3.", "TC3 runs at Tier `P`.", "Ceiling: **P**"):
            with self.subTest(line=line):
                self.assertIn("Tier P", self.refusal(comments=[ready(), spec(extra=line)]))

    def test_a_tier_p_case_heading_is_refused(self):
        # The form a real data-migration spec uses (cross-family finding).
        line = "### TC7 — the migration itself (Tier P, HITL-approved)"
        self.assertIn("Tier P", self.refusal(comments=[ready(), spec(extra=line)]))

    def test_a_fenced_tier_p_is_not_hidden(self):
        fenced = "```\nTC3: Write tier: P\n```"
        self.assertIn("Tier P", self.refusal(comments=[ready(), spec(extra=fenced)]))

    def test_a_newer_spec_without_a_footer_supersedes_the_footered_one(self):
        heading_only = {"id": SPEC_ID + 5, "body": "## Lane 3 Test Spec — H42\n\nRevised. Write tier: **W**."}
        reason = self.refusal(comments=[ready(), spec(), heading_only])
        self.assertIn(str(SPEC_ID + 5), reason)

    def test_an_issue_leased_after_auto_ae_was_turned_on_is_refused(self):
        self.toggle.write_text(json.dumps({"on": True, "lane": "1", "leases_at_set": {"H43": self.lease_expiry},
                                           "expires_at": (NOW + timedelta(hours=12)).isoformat()}))
        self.assertIn("was not under a lease when auto-AE was turned on", self.refusal())

    def test_a_renewed_lease_for_the_same_key_is_refused(self):
        self.set_lease(KEY, expires=NOW + timedelta(hours=5))  # a new grant, new identity
        self.assertIn("renewed after auto-AE was turned on", self.refusal())

    def test_an_expired_toggle_is_off(self):
        self.toggle.write_text(json.dumps({"on": True, "lane": "1", "leases_at_set": {KEY: self.lease_expiry},
                                           "expires_at": (NOW - timedelta(minutes=1)).isoformat()}))
        self.assertIn("auto-AE is off", self.refusal())

    def test_an_old_list_shaped_state_is_off(self):
        self.toggle.write_text(json.dumps({"on": True, "leases_at_set": [KEY],
                                           "expires_at": (NOW + timedelta(hours=1)).isoformat()}))
        self.assertIn("auto-AE is off", self.refusal())

    def test_slash_list_and_table_tier_p_forms_are_refused(self):
        for extra in ("Write tier: W/P", "Write tier R, W and P",
                      "| TC | Tier |\n|---|---|\n| TC1 | W |\n| TC7 | P |",
                      "| TC | Tier |\n|---|---|\n| TC1 | W |\n| TC7 | P",
                      "| Tier | TC |\n|---|---|\n| P | TC7 |",
                      "TC | Tier\n---|---\nTC7 | P",
                      "run --tier\tp", "run --tier='p'", "run --tier_p", "run --TIER__P_",
                      "run --'tier'=p", 'run --ti"er"=p', "run --t\\ier=p"):
            with self.subTest(extra=extra):
                self.assertIn("Tier P", self.refusal(comments=[ready(), spec(extra=extra)]))

    def test_a_fenced_declaration_cannot_supply_the_tier(self):
        fenced = "```\nWrite tier: W\n```"
        self.assertIn("no single unfenced", self.refusal(comments=[ready(), spec(tier=None, extra=fenced)]))

    def test_two_disagreeing_declarations_are_refused(self):
        self.assertIn("no single unfenced", self.refusal(
            comments=[ready(), spec(tier_line="Write tier: R", extra="Write tier: W")]))

    def test_a_cypher_lowercase_p_is_not_a_tier(self):
        line = "Proposed ceiling **W**: raw Cypher `SET p.promoted_at = datetime()` on its own node."
        self.assertIsNone(self.refusal(comments=[ready(), spec(tier_line=line + " Write tier: **W**.")]))

    def test_a_spec_revision_posted_as_a_discussion_supersedes(self):
        revision = {"id": SPEC_ID + 7, "body": _footered("## Lane 3 Test Spec — H42\n\nRevised. Write tier: **W**.",
                                                          "discussion", " posted-by=LANE3;")}
        reason = self.refusal(comments=[ready(), spec(), revision], spec_comment=SPEC_ID)
        self.assertIn(str(SPEC_ID + 7), reason)

    def test_a_spec_stating_no_tier_is_refused(self):
        self.assertIn("no single unfenced", self.refusal(comments=[ready(), spec(tier=None)]))

    def test_a_standalone_ae_is_refused(self):
        self.assertIn("ae-and-sweep", self.refusal(no_sweep=True))

    def test_a_production_run_is_refused(self):
        self.assertIn("--prod-run", self.refusal(prod_run=True))

    def test_an_operator_acknowledgment_is_refused(self):
        self.assertIn("--ack-no-pr-required", self.refusal(ack="merged"))

    def test_no_spec_after_the_newest_ready_for_l3_is_refused(self):
        # A spec for an older round, before the newest ready-for-l3, is stale.
        comments = [ready(100), spec(150), ready(READY_ID)]
        self.assertIn("no Lane 3 spec", self.refusal(comments=comments, spec_comment=150))

    def test_no_ready_for_l3_is_refused(self):
        self.assertIn("no Lane 1 ready-for-l3", self.refusal(comments=[spec()]))

    def test_an_ae_at_another_sha_is_refused(self):
        self.assertIn("not the AE's", self.refusal(sha=OTHER))

    def test_the_wrong_spec_comment_is_refused(self):
        self.assertIn("not the newest Lane 3 spec", self.refusal(spec_comment=999))

    def test_an_edited_spec_is_refused(self):
        self.assertIn("edited", self.refusal(comments=[ready(), spec(edited=True)]))

    def test_a_spec_without_a_digest_is_refused(self):
        self.assertIn("no body-sha256", self.refusal(comments=[ready(), spec(marker=False)]))

    def test_an_authorized_line_not_naming_the_spec_is_refused(self):
        self.assertIn("does not name the spec", self.refusal(ae=ae_body(spec_id=999)))

    def test_two_authorized_lines_refuse(self):
        body = ae_body() + "\n**Authorized:** the operator, in chat."
        self.assertIn("more than one Authorized", self.refusal(ae=body))


class CarryForwardTests(unittest.TestCase):
    def test_an_auto_ae_never_carries_forward(self):
        authority = {"id": 10, "body": _footered(ae_body(), "ae", f" sha={SHA};")}
        comments = [authority, ready(11, OTHER)]
        self.assertIsNone(clr.carry_forward(comments, authority, OTHER))

    def test_an_operator_ae_still_carries_forward(self):
        authority = {"id": 10, "body": _footered(ae_body(cite=""), "ae", f" sha={SHA};")}
        comments = [authority, ready(11, OTHER)]
        self.assertEqual(clr.carry_forward(comments, authority, OTHER)["id"], 11)


class ValidateAutoAeTests(unittest.TestCase):
    def test_an_operator_ae_reads_nothing(self):
        with patch("check_lane3_ready.fetch_comments", side_effect=AssertionError("no fetch")):
            l1_post.validate_auto_ae(ae_body(cite=""), sweep_body(), REPO, ISSUE, SHA, SPEC_ID, False, None)

    def test_a_refused_auto_ae_posts_nothing(self):
        with patch("check_lane3_ready.fetch_comments", return_value=[ready(), spec()]), \
             patch.object(auto, "auto_ae_refusal", return_value="auto-AE is off"), \
             self.assertRaises(SystemExit):
            l1_post.validate_auto_ae(ae_body(), sweep_body(), REPO, ISSUE, SHA, SPEC_ID, False, None)

    def test_an_accepted_auto_ae_passes(self):
        with patch("check_lane3_ready.fetch_comments", return_value=[ready(), spec()]), \
             patch.object(auto, "auto_ae_refusal", return_value=None):
            l1_post.validate_auto_ae(ae_body(), sweep_body(), REPO, ISSUE, SHA, SPEC_ID, False, None)


if __name__ == "__main__":
    unittest.main()
