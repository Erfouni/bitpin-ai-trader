# -*- coding: utf-8 -*-
"""Latest-decision evidence review: safe projection, honest labels and authenticated rendering."""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

from bitpin import notify as nt  # noqa: E402
import panel_helper as ph  # noqa: E402
import test_panel_web as tpw  # noqa: E402


def validated_review():
    """Synthetic public observations, never an account or a model call."""
    return {"schema_version": 1, "request_id": "a" * 64, "status": "validated", "sources": {
        "bitpin_flow": {"assessment": "supports", "effect": "supports_existing",
                        "reason": "Observed activity supports the existing market view.",
                        "affected_symbols": ["BTC_IRT"], "status": "available",
                        "citations": [{"id": "flow:BTC", "field": "trade_count", "value": 25},
                                      {"id": "flow:BTC", "field": "buy_count", "value": 15}]},
        "defi_daily": {"assessment": "mixed", "effect": "reduces_conviction",
                       "reason": "Daily coverage is incomplete; conviction is lower.",
                       "affected_symbols": ["ETH_IRT"], "status": "available",
                       "citations": [{"id": "defi:daily", "field": "available_sources", "value": 1}]},
        "personal_wallets": {"assessment": "neutral", "effect": "none",
                             "reason": "An attributed address is not independent investor demand.",
                             "affected_symbols": ["ETH_IRT"], "status": "available",
                             "citations": [{"id": "wallet:person:1", "field": "balance_wei", "value": 1250000000}]},
        "custody_wallets": {"assessment": "unavailable", "effect": "none",
                            "reason": "No current custody observation was available.",
                            "affected_symbols": [], "status": "unavailable", "citations": []}}}


class TestReviewProjection(unittest.TestCase):
    def collect(self, review):
        # Keep the notifier's real state readers; replace only its normalized latest-decision boundary.
        v = nt.decision_view({"time": tpw.T0, "decision": {"valid": True, "hold": True}})
        v["evidence_review"] = review
        with tempfile.TemporaryDirectory(prefix="panel_evidence_") as state:
            with mock.patch.object(nt.Notifier, "last_decision_view", return_value=v):
                return ph.collect_bot_state(state, os.path.join(state, "notify"),
                                            os.path.join(state, "absent.json"), tpw.T0)

    def test_validated_review_is_projected_without_raw_observations(self):
        review = validated_review()
        review["raw_content"] = "PRIVATE-SENTINEL"
        review["sources"]["personal_wallets"]["citations"][0]["address"] = "0x" + "ab" * 20
        data = self.collect(review)
        projected = (data.get("last_decision") or {}).get("evidence_review")
        self.assertIsInstance(projected, dict)
        self.assertEqual(projected["sources"]["bitpin_flow"]["citation_count"], 2)
        self.assertEqual(projected["sources"]["defi_daily"]["effect"], "reduces_conviction")
        self.assertNotIn("PRIVATE-SENTINEL", json.dumps(projected))
        self.assertNotIn("0x" + "ab" * 20, json.dumps(projected))
        self.assertNotIn("citations", projected["sources"]["personal_wallets"])

    def test_unvalidated_or_incomplete_review_cannot_be_presented_as_validated(self):
        bad = []
        for status in ("declared", "rejected", None):
            review = validated_review()
            review["status"] = status
            bad.append(review)
        review = validated_review()
        del review["sources"]["custody_wallets"]
        bad.append(review)
        review = validated_review()
        review["schema_version"] = True
        bad.append(review)
        for review in bad:
            self.assertIsNone(self.collect(review)["last_decision"].get("evidence_review"))

    def test_projected_text_and_lists_are_bounded_and_direction_controls_removed(self):
        review = validated_review()
        row = review["sources"]["bitpin_flow"]
        row["reason"] = "\u202e<script>" + "A" * 5000
        row["affected_symbols"] = ["BTC_IRT"] * 1000 + ["0x" + "ab" * 20]
        row["citations"] = row["citations"] * 1000
        row["status"] = "PRIVATE-SENTINEL"
        projected = self.collect(review)["last_decision"].get("evidence_review")
        self.assertIsInstance(projected, dict)
        row = projected["sources"]["bitpin_flow"]
        self.assertLessEqual(len(row["reason"]), 240)
        self.assertNotIn("\u202e", row["reason"])
        self.assertLessEqual(len(row["affected_symbols"]), 12)
        self.assertLessEqual(row["citation_count"], 12)
        self.assertNotIn("PRIVATE-SENTINEL", json.dumps(projected))

    def test_invalid_citation_shape_cannot_add_to_the_validated_count(self):
        for citation in (None, {}, {"id": "flow:BTC", "field": "status"},
                         {"id": [], "field": "status", "value": "ready"},
                         {"id": "flow:BTC", "field": "trade_count", "value": "25"},
                         {"id": "flow:BTC", "field": "trade_count", "value": float("nan")},
                         {"id": "flow:BTC", "field": "trade_count", "value": True}):
            review = validated_review()
            review["sources"]["bitpin_flow"]["citations"] = [citation]
            self.assertIsNone(self.collect(review)["last_decision"].get("evidence_review"))

    def test_code_authored_availability_is_projected_without_raw_status_text(self):
        for status in ("available", "unavailable", "absent", "omitted"):
            review = validated_review()
            review["sources"]["bitpin_flow"]["status"] = status
            projected = self.collect(review)["last_decision"]["evidence_review"]
            self.assertEqual(projected["sources"]["bitpin_flow"].get("source_status"), status)

    def test_valid_numeric_and_null_citations_keep_the_validated_review_and_count(self):
        review = validated_review()
        review["sources"]["personal_wallets"]["citations"].append({
            "id": "wallet:person:1", "field": "change_7d_wei", "value": None})
        projected = self.collect(review)["last_decision"].get("evidence_review")
        self.assertIsInstance(projected, dict)
        self.assertEqual(projected["sources"]["personal_wallets"]["citation_count"], 2)
        self.assertEqual(projected["sources"]["personal_wallets"]["source_status"], "available")
        self.assertNotIn("change_7d_wei", json.dumps(projected))

    def test_real_decision_log_keeps_review_with_its_own_latest_record(self):
        with tempfile.TemporaryDirectory(prefix="panel_evidence_log_") as state:
            path = os.path.join(state, "kimi_decisions.jsonl")
            old = validated_review()
            old["sources"]["bitpin_flow"]["reason"] = "An older declaration must not be used."
            latest = validated_review()
            latest["sources"]["bitpin_flow"]["reason"] = "The latest declaration belongs to this decision."
            records = [{"time": tpw.T0 - 300, "decision": {"valid": True, "hold": True,
                       "analysis": {"evidence_review": old}}},
                       {"time": tpw.T0, "decision": {"valid": True, "hold": True,
                        "analysis": {"evidence_review": latest}}}]
            with open(path, "w", encoding="utf-8") as stream:
                for record in records:
                    stream.write(json.dumps(record) + "\n")
                stream.write("truncated-record\n")
            data = ph.collect_bot_state(state, os.path.join(state, "notify"),
                                        os.path.join(state, "absent.json"), tpw.T0)
            projected = data["last_decision"].get("evidence_review")
            self.assertIsInstance(projected, dict)
            self.assertEqual(projected["sources"]["bitpin_flow"]["reason"],
                             "The latest declaration belongs to this decision.")
            with open(path, "a", encoding="utf-8") as stream:
                stream.write(json.dumps({"time": tpw.T0 + 30, "decision": {
                    "valid": True, "hold": True, "fallback": True}}) + "\n")
            data = ph.collect_bot_state(state, os.path.join(state, "notify"),
                                        os.path.join(state, "absent.json"), tpw.T0 + 30)
            self.assertTrue(data["last_decision"]["fallback"])
            self.assertIsNone(data["last_decision"].get("evidence_review"))


class TestEvidenceReviewCard(tpw.PanelCase):
    def setUp(self):
        super(TestEvidenceReviewCard, self).setUp()
        self.c.cookies["__Host-bplang"] = "en"

    def dashboard_with(self, review):
        data = tpw.status_data()
        data["last_decision"]["evidence_review"] = review
        self.helper.responses["status"] = data

    def test_four_source_groups_show_declared_effect_and_validated_citation_counts(self):
        self.login()
        # Rendering receives the projection, never citation values or raw observations.
        review = ph.evidence_review_view(validated_review())
        self.dashboard_with(review)
        response = self.c.get("/")
        self.assertEqual(response.status, 200)
        for title in ("Public-source review", "Bitpin activity", "Daily DeFi data",
                      "Publicly attributed ETH addresses", "BTC custody addresses"):
            self.assertTrue(title in response.text, title)
        self.assertIn("Supports the existing decision", response.text)
        self.assertIn("Reduces conviction", response.text)
        self.assertIn("2 validated citations", response.text)
        self.assertIn("No declared change", response.text)
        self.assertIn("Declared consideration", response.text)
        self.assertIn("effects are self-reported", response.text)
        self.assertIn("No current custody observation was available.", response.text)
        self.assertIn("Source availability", response.text)
        self.assertIn("Available", response.text)
        self.assertNotIn("balance_wei", response.text)

    def test_historical_missing_review_is_explicit_not_inferred_as_ignored(self):
        self.login()
        self.dashboard_with(None)
        response = self.c.get("/")
        self.assertIn("No validated review was recorded for this decision.", response.text)
        self.assertIn("This does not establish that a source was ignored.", response.text)
        self.assertEqual(response.text.count('class="badge evidence-missing"'), 4)

    def test_review_is_authenticated_and_hostile_text_is_escaped_and_isolated(self):
        review = ph.evidence_review_view(validated_review())
        review["sources"]["bitpin_flow"]["reason"] = "\u202e" + tpw.HOSTILE
        review["sources"]["bitpin_flow"]["affected_symbols"] = [tpw.HOSTILE, "BTC_IRT"]
        self.dashboard_with(review)
        denied = self.c.get("/")
        self.assertEqual(denied.status, 303)
        self.assertNotIn("Public-source review", denied.text)
        self.login()
        response = self.c.get("/")
        self.assertEscaped(response.text)
        self.assertNotIn("\u202e", response.text)
        self.assertIn("<bdi>&lt;script&gt;", response.text)

    def test_persian_review_labels_and_rtl_use_the_existing_panel_design(self):
        self.login()
        self.c.cookies["__Host-bplang"] = "fa"
        review = ph.evidence_review_view(validated_review())
        self.dashboard_with(review)
        response = self.c.get("/")
        self.assertIn('dir="rtl"', response.text)
        self.assertIn(u"بررسی منابع عمومی", response.text)
        self.assertIn(u"اثر اعلام‌شده", response.text)
        self.assertIn('class="evidence-grid"', response.text)
        self.assertIn('href="/research"', response.text)

    def test_malformed_review_fields_cannot_break_dashboard_rendering(self):
        self.login()
        for field, value in (("assessment", []), ("effect", {}), ("citation_count", True),
                             ("reason", None), ("affected_symbols", {})):
            review = ph.evidence_review_view(validated_review())
            review["sources"]["bitpin_flow"][field] = value
            self.dashboard_with(review)
            response = self.c.get("/")
            self.assertEqual(response.status, 200, field)
            self.assertEqual(response.text.count('class="badge evidence-missing"'), 4, field)

    def test_unknown_availability_is_omitted_without_copying_raw_data(self):
        self.login()
        review = ph.evidence_review_view(validated_review())
        review["sources"]["bitpin_flow"]["source_status"] = ["PRIVATE-SENTINEL"]
        self.dashboard_with(review)
        response = self.c.get("/")
        self.assertEqual(response.status, 200)
        self.assertNotIn("PRIVATE-SENTINEL", response.text)
        self.assertTrue("2 validated citations" in response.text)

    def test_absent_and_omitted_sources_show_their_distinct_availability(self):
        self.login()
        review = validated_review()
        for group, status in (("personal_wallets", "absent"), ("custody_wallets", "omitted")):
            review["sources"][group].update(status=status, assessment="unavailable", effect="none", citations=[])
        self.dashboard_with(ph.evidence_review_view(review))
        response = self.c.get("/")
        self.assertEqual(response.status, 200)
        self.assertTrue("Absent from request" in response.text)
        self.assertTrue("Omitted from request" in response.text)
        self.assertNotIn('class="badge evidence-missing"', response.text)
        self.c.cookies["__Host-bplang"] = "fa"
        response = self.c.get("/")
        self.assertTrue(u"در درخواست وجود ندارد" in response.text)
        self.assertTrue(u"در درخواست جا داده نشده" in response.text)

    def test_review_scope_remains_initial_proposal_when_confirmation_cuts_targets(self):
        self.login()
        data = tpw.status_data()
        data["last_decision"]["evidence_review"] = ph.evidence_review_view(validated_review())
        second = ph.evidence_review_view(validated_review())
        second["sources"]["bitpin_flow"]["reason"] = "SECOND-VOTE-REASON"
        data["last_decision"]["targets"] = {"BTC_IRT": 0.0, "USDT_IRT": 1.0}
        data["last_decision"]["confirmation"] = {"kept": {}, "cut": {"BTC_IRT": 0.4},
                                                    "error": None, "evidence_review": second}
        self.helper.responses["status"] = data
        notes = (("en", "This review describes the model's initial proposal. Code checks or a second vote can change "
                         "the final targets."),
                 ("fa", u"این بررسی مربوط به پیشنهاد اولیهٔ مدل است. بررسی‌های کد یا پرسش دوم می‌توانند وزن‌های هدف نهایی را تغییر دهند."))
        for lang, note in notes:
            self.c.cookies["__Host-bplang"] = lang
            response = self.c.get("/")
            self.assertEqual(response.status, 200)
            self.assertTrue(note.replace("'", "&#x27;") in response.text, lang)
            self.assertTrue("Observed activity supports the existing market view." in response.text)
            self.assertNotIn("SECOND-VOTE-REASON", response.text)


if __name__ == "__main__":
    unittest.main()
