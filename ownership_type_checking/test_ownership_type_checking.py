import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

import ownership_type_checking as otc


class FormatPropertyBulkFieldExclusionTests(unittest.TestCase):
    def test_bulk_fields_never_shown_to_the_model(self):
        # Real reported failure: the model read "% Bulk Overall: 0.5" (a bulk INTERNET/TV/phone
        # service contract field in this broadband-competition dataset) as "only 50% single-
        # owned" and used that to reject an otherwise-satisfied §4.1 override. These fields are
        # irrelevant to ownership type and must never reach the model at all.
        row = {
            "RecordID": "446701",
            "Master_Property Name": "Cross Creek Apartments",
            "Bulk Flag": "Bulk",
            "Bulk Package Type": "Bulk - Double Play",
            "% Bulk Overall": "0.5",
        }
        text = otc.format_property(row, [], {})
        self.assertNotIn("Bulk", text)
        self.assertNotIn("0.5", text)


class ComputeTriggersTests(unittest.TestCase):
    def test_apt_name_mismatch_on_real_keyword(self):
        row = {"Master_Ownership Type": "COA", "Master_Property Name": "Oakwood Apartment Corp."}
        triggers = otc.compute_triggers(row)
        rules = [t["rule"] for t in triggers]
        self.assertIn("Type-Name Mismatch (APT)", rules)

    def test_apt_name_mismatch_matches_coincidental_substring(self):
        # "Captains" contains the substring "apt" -- Section 3 says this is expected
        # behavior, not a bug, so the trigger must still fire.
        row = {"Master_Ownership Type": "COA", "Master_Property Name": "Captains Quarters"}
        triggers = otc.compute_triggers(row)
        rules = [t["rule"] for t in triggers]
        self.assertIn("Type-Name Mismatch (APT)", rules)

    def test_no_trigger_when_ownership_already_matches_name(self):
        row = {"Master_Ownership Type": "APT", "Master_Property Name": "Riverside Apartments"}
        triggers = otc.compute_triggers(row)
        rules = [t["rule"] for t in triggers]
        self.assertNotIn("Type-Name Mismatch (APT)", rules)

    def test_barton_village_multi_trigger(self):
        # From the spec's worked example: HOA-typed property named "...Flats I" with a
        # 385-unit / 1-building ratio -- both Type-Name Mismatch (APT) and High
        # Unit-to-Building Ratio should fire.
        row = {
            "Master_Ownership Type": "HOA",
            "Master_Property Name": "Barton Village - Flats I",
            "Master_Units_50+": "385",
            "Master_Building Count_50+": "1",
        }
        triggers = otc.compute_triggers(row)
        rules = {t["rule"] for t in triggers}
        self.assertIn("Type-Name Mismatch (APT)", rules)
        self.assertIn("High Unit-to-Building Ratio", rules)
        self.assertEqual(len(otc.summarize_trigger_types(triggers)), 2)

    def test_low_unit_to_building_ratio_falls_back_to_20plus(self):
        row = {
            "Master_Ownership Type": "COA",
            "Master_Property Name": "Some Condo",
            "Master_Units_50+": "",
            "Master_Building Count_50+": "",
            "Master_Units_20+": "8",
            "Master_Building Count_20+": "4",
        }
        triggers = otc.compute_triggers(row)
        rules = [t["rule"] for t in triggers]
        self.assertIn("Low Unit-to-Building Ratio", rules)

    def test_high_floor_count_excludes_apt_and_coa(self):
        row = {"Master_Ownership Type": "APT", "Master_Property Name": "Something", "Master_Floor Count": "10"}
        self.assertEqual(otc.compute_triggers(row), [])

    def test_has_fees_ignores_zero(self):
        row = {"Master_Ownership Type": "APT", "Master_Property Name": "Something", "Master_Monthly Association Fees": "0"}
        rules = [t["rule"] for t in otc.compute_triggers(row)]
        self.assertNotIn("Has Fees", rules)

    def test_has_fees_fires_on_nonzero(self):
        row = {"Master_Ownership Type": "APT", "Master_Property Name": "Something", "Master_Monthly Association Fees": "125"}
        rules = [t["rule"] for t in otc.compute_triggers(row)]
        self.assertIn("Has Fees", rules)

    def test_has_leasing_info_never_fires_for_apt(self):
        row = {"Master_Ownership Type": "APT", "Master_Property Name": "Something", "Leasing Company": "Acme Mgmt"}
        rules = [t["rule"] for t in otc.compute_triggers(row)]
        self.assertNotIn("Has Leasing Info", rules)

    def test_has_leasing_info_fires_for_coa(self):
        row = {"Master_Ownership Type": "COA", "Master_Property Name": "Something", "Leasing Company": "Acme Mgmt"}
        rules = [t["rule"] for t in otc.compute_triggers(row)]
        self.assertIn("Has Leasing Info", rules)


class GuardrailTests(unittest.TestCase):
    def test_tier3_only_override_without_exception_claim_is_downgraded(self):
        result = {
            "determined_type": "APT",
            "decision": "Override",
            "confidence": "High",
            "evidence_tier_used": "Tier 3",
            "reasoning": "A leasing website says apply now.",
            "sources": ["https://example.com"],
            "tier3_exception_invoked": "no",
        }
        fixed = otc._enforce_tier3_override_guardrail({}, result)
        self.assertEqual(fixed["decision"], "Not Enough Info")
        self.assertEqual(fixed["confidence"], "Low")
        self.assertFalse(fixed["tier3_exception_used"])

    def test_tier1_override_is_left_alone(self):
        result = {
            "determined_type": "APT",
            "decision": "Override",
            "confidence": "High",
            "evidence_tier_used": "Tier 1",
            "reasoning": "County registry lists it as a rental apartment complex.",
            "sources": ["https://sunbiz.org/x"],
        }
        fixed = otc._enforce_tier3_override_guardrail({}, result)
        self.assertEqual(fixed["decision"], "Override")
        self.assertFalse(fixed["tier3_exception_used"])

    def test_override_with_matching_determined_type_becomes_confirmed(self):
        result = {"determined_type": "COA", "decision": "Override", "evidence_tier_used": "Tier 1"}
        fixed = otc._reconcile_decision_and_type("COA", result)
        self.assertEqual(fixed["decision"], "Confirmed")

    def test_confirmed_with_mismatched_type_is_corrected_to_db_label(self):
        result = {"determined_type": "APT", "decision": "Confirmed", "evidence_tier_used": "Tier 3"}
        fixed = otc._reconcile_decision_and_type("COA", result)
        self.assertEqual(fixed["determined_type"], "COA")

    def test_not_enough_info_with_mismatched_type_is_corrected_to_db_label(self):
        result = {"determined_type": "APT", "decision": "Not Enough Info", "evidence_tier_used": "Tier 3"}
        fixed = otc._reconcile_decision_and_type("COA", result)
        self.assertEqual(fixed["determined_type"], "COA")

    def test_error_result_never_forces_override(self):
        result = otc._default_error_result("HOA", RuntimeError("boom"))
        self.assertEqual(result["decision"], "Not Enough Info")
        self.assertEqual(result["determined_type"], "HOA")


class StructuralEdgeCaseGuardrailTests(unittest.TestCase):
    def test_non_none_edge_case_forces_confirmed_and_db_label(self):
        # This is the real Arbor Hills Apartments failure: the model correctly identified a
        # housing cooperative, then used the co-op's own association fees (backwards) to argue
        # for overriding COA -> APT anyway. The guardrail must force it back regardless.
        result = {
            "determined_type": "APT",
            "decision": "Override",
            "confidence": "High",
            "evidence_tier_used": "Mixed",
            "reasoning": (
                "Confirmed as a cooperative (co-op) ownership structure, not COA. Clear evidence "
                "of monthly association fees supports this being an apartment. Overriding to APT."
            ),
            "structural_edge_case": "housing_cooperative",
        }
        fixed = otc._enforce_structural_edge_case_guardrail("COA", result)
        self.assertEqual(fixed["decision"], "Confirmed")
        self.assertEqual(fixed["determined_type"], "COA")
        self.assertIn("housing_cooperative", fixed["reasoning"])

    def test_none_edge_case_is_left_alone(self):
        result = {"determined_type": "APT", "decision": "Override", "structural_edge_case": "none"}
        fixed = otc._enforce_structural_edge_case_guardrail("HOA", result)
        self.assertEqual(fixed["decision"], "Override")
        self.assertEqual(fixed["determined_type"], "APT")

    def test_missing_edge_case_field_is_left_alone(self):
        result = {"determined_type": "APT", "decision": "Override"}
        fixed = otc._enforce_structural_edge_case_guardrail("HOA", result)
        self.assertEqual(fixed["decision"], "Override")

    def test_edge_case_that_already_agrees_does_not_rewrite_reasoning(self):
        result = {
            "determined_type": "COA", "decision": "Confirmed", "structural_edge_case": "housing_cooperative",
            "reasoning": "This is a housing cooperative; DB label kept as-is.",
        }
        fixed = otc._enforce_structural_edge_case_guardrail("COA", result)
        self.assertEqual(fixed["reasoning"], "This is a housing cooperative; DB label kept as-is.")

    def test_all_edge_case_labels_other_than_none_are_forced(self):
        for label in otc.STRUCTURAL_EDGE_CASE_LABELS:
            if label == "none":
                continue
            result = {"determined_type": "APT", "decision": "Override", "structural_edge_case": label}
            fixed = otc._enforce_structural_edge_case_guardrail("HOA", result)
            self.assertEqual(fixed["decision"], "Confirmed", msg=f"label={label}")
            self.assertEqual(fixed["determined_type"], "HOA", msg=f"label={label}")


class CoopMentionGuardrailTests(unittest.TestCase):
    def test_override_mentioning_cooperative_is_forced_confirmed(self):
        # Real reported concern: reasoning raises a co-op possibility ("not enough evidence to
        # confirm") without setting structural_edge_case, and a weak Override built on other
        # evidence slips through. This backstop catches it directly from the reasoning text.
        result = {
            "determined_type": "APT",
            "decision": "Override",
            "structural_edge_case": "none",
            "reasoning": "This may be a cooperative, but there wasn't enough evidence to confirm; leaning APT based on leasing activity.",
        }
        fixed = otc._enforce_coop_mention_guardrail("COA", result)
        self.assertEqual(fixed["decision"], "Confirmed")
        self.assertEqual(fixed["determined_type"], "COA")

    def test_override_mentioning_co_op_hyphenated_is_forced_confirmed(self):
        result = {"determined_type": "APT", "decision": "Override", "reasoning": "Possibly a co-op structure here."}
        fixed = otc._enforce_coop_mention_guardrail("HOA", result)
        self.assertEqual(fixed["decision"], "Confirmed")
        self.assertEqual(fixed["determined_type"], "HOA")

    def test_override_with_no_coop_mention_is_left_alone(self):
        result = {"determined_type": "APT", "decision": "Override", "reasoning": "County registry confirms rental apartments."}
        fixed = otc._enforce_coop_mention_guardrail("HOA", result)
        self.assertEqual(fixed["decision"], "Override")

    def test_confirmed_mentioning_coop_is_left_alone(self):
        # Already Confirmed -- nothing to force.
        result = {"determined_type": "HOA", "decision": "Confirmed", "reasoning": "This is a housing cooperative."}
        fixed = otc._enforce_coop_mention_guardrail("HOA", result)
        self.assertEqual(fixed["decision"], "Confirmed")


class ProcessPropertyIntegrationTests(unittest.TestCase):
    """End-to-end through process_property() with research_property() mocked -- covers the two
    real misclassifications reported against a prior batch, to lock in the fix as a regression
    test rather than only testing the guardrails in isolation."""

    def test_arbor_hills_style_coop_is_not_overridden(self):
        row = {
            "RecordID": "135831",
            "Master_Property Name": "Arbor Hills Apartments",
            "Master_Ownership Type": "COA",
            "Master_Monthly Association Fees": "450",
        }
        fake_result = {
            "determined_type": "APT",
            "decision": "Override",
            "confidence": "High",
            "evidence_tier_used": "Mixed",
            "reasoning": "Confirmed as a housing cooperative with monthly fees supporting APT.",
            "sources": ["https://redfin.com/x", "https://realtor.com/x"],
            "structural_edge_case": "housing_cooperative",
            "tier3_exception_invoked": "no",
            "tier3_independent_source_count": 0,
            "tier3_contradicting_evidence": "not_applicable",
            "tier3_internal_db_corroboration": "",
            "tier3_structural_edge_case_ruled_out": "not_applicable",
            "tier3_exception_direction": "not_applicable",
            "tier3_reverse_attempt2_exhausted": "not_applicable",
            "ownership_concentration": "not_applicable",
            "reverse_conversion_detected": "not_applicable",
            "multi_name_all_agree": "not_applicable",
            "tier3_sales_listing_search_performed": "not_applicable",
            "tier3_sales_evidence_found": "not_applicable",
            "ownership_concentration_verified_externally": "not_applicable",
            "ownership_concentration_contradicting_evidence": "not_applicable",
            "tier3_dual_association_search_performed": "not_applicable",
            "tier3_entity_name_registry_search_performed": "not_applicable",
        }
        with mock.patch("ownership_type_checking.research_property", return_value=fake_result), \
             mock.patch("ownership_type_checking.fetch_url_cached", return_value=None):
            result = otc.process_property(None, "gpt-4o", row, {})
        self.assertEqual(result["decision"], "Confirmed")
        self.assertEqual(result["determined_type"], "COA")
        self.assertEqual(result["decision_display"], "Confirmed")

    def test_cross_creek_style_property_with_null_fee_can_now_override(self):
        row = {
            "RecordID": "446701",
            "Master_Property Name": "Cross Creek Apartments",
            "Master_Ownership Type": "HOA",
            "Master_Units_50+": "80",
            "Master_Building Count_50+": "40",
            "Master_Monthly Association Fees": "",
            "Leasing Company": "Advanced Precision",
        }
        fake_result = {
            "determined_type": "APT",
            "decision": "Override",
            "confidence": "High",
            "evidence_tier_used": "Tier 3",
            "reasoning": "Tier-3 corroborated override: single owner, no sales history found.",
            "sources": ["https://crosscreekapts.com", "https://apartments.com/x", "https://apartmentratings.com/x"],
            "structural_edge_case": "none",
            "tier3_exception_invoked": "yes",
            "tier3_exception_direction": "to_apt",
            "tier3_reverse_attempt2_exhausted": "not_applicable",
            "tier3_independent_source_count": 3,
            "tier3_name_address_anchor_confirmed": "yes",
            "tier3_partial_tier12_support": "not_applicable",
            "tier3_contradicting_evidence": "no",
            "tier3_internal_db_corroboration": "Master_Monthly Association Fees is null despite 80 units",
            "tier3_structural_edge_case_ruled_out": "yes",
            "ownership_concentration": "not_applicable",
            "reverse_conversion_detected": "not_applicable",
            "multi_name_all_agree": "not_applicable",
            "tier3_sales_listing_search_performed": "yes",
            "tier3_sales_evidence_found": "no",
            "ownership_concentration_verified_externally": "not_applicable",
            "ownership_concentration_contradicting_evidence": "not_applicable",
            "tier3_dual_association_search_performed": "yes",
            "tier3_entity_name_registry_search_performed": "not_applicable",
            "_searched_queries": ["Cross Creek Apartments units for sale"],
        }
        with mock.patch("ownership_type_checking.research_property", return_value=fake_result), \
             mock.patch("ownership_type_checking.fetch_url_cached", return_value=None):
            result = otc.process_property(None, "gpt-4o", row, {})
        self.assertEqual(result["decision"], "Override")
        self.assertEqual(result["determined_type"], "APT")
        self.assertEqual(result["confidence"], "Medium")
        self.assertEqual(result["decision_display"], "Changed from HOA to APT")

    def test_casa_gataway_style_property_with_populated_fee_can_override_to_hoa(self):
        # Real reported failure: DB says APT, but multiple Tier-3 sources and a real, populated
        # association fee corroborate HOA/COA. This is the reverse-direction §4.1 exception
        # (to_coa_hoa) -- gated on a genuinely exhausted Attempt 2, not a parallel shortcut.
        row = {
            "RecordID": "912345",
            "Master_Property Name": "Casa Gataway",
            "Master_Ownership Type": "APT",
            "Master_Monthly Association Fees": "461",
        }
        fake_result = {
            "determined_type": "HOA",
            "decision": "Override",
            "confidence": "High",
            "evidence_tier_used": "Tier 3",
            "reasoning": "Tier-3 corroborated reverse override: multiple listings describe this as an HOA community with a monthly association fee, and a thorough state business registry / county records search found no APT-supporting Tier 1/2 evidence.",
            "sources": ["https://realtor.com/x", "https://zillow.com/x", "https://homes.com/x"],
            "structural_edge_case": "none",
            "tier3_exception_invoked": "yes",
            "tier3_exception_direction": "to_coa_hoa",
            "tier3_reverse_attempt2_exhausted": "yes",
            "tier3_independent_source_count": 3,
            "tier3_name_address_anchor_confirmed": "yes",
            "tier3_partial_tier12_support": "not_applicable",
            "tier3_contradicting_evidence": "no",
            "tier3_internal_db_corroboration": "Master_Monthly Association Fees is populated ($461), consistent with an HOA",
            "tier3_structural_edge_case_ruled_out": "yes",
            "ownership_concentration": "not_applicable",
            "reverse_conversion_detected": "not_applicable",
            "multi_name_all_agree": "not_applicable",
            "tier3_sales_listing_search_performed": "not_applicable",
            "tier3_sales_evidence_found": "not_applicable",
            "ownership_concentration_verified_externally": "not_applicable",
            "ownership_concentration_contradicting_evidence": "not_applicable",
            "tier3_dual_association_search_performed": "not_applicable",
            "tier3_entity_name_registry_search_performed": "not_applicable",
        }
        with mock.patch("ownership_type_checking.research_property", return_value=fake_result), \
             mock.patch("ownership_type_checking.fetch_url_cached", return_value=None):
            result = otc.process_property(None, "gpt-4o", row, {})
        self.assertEqual(result["decision"], "Override")
        self.assertEqual(result["determined_type"], "HOA")
        self.assertEqual(result["confidence"], "Medium")
        self.assertEqual(result["decision_display"], "Changed from APT to HOA")

    def test_reverse_direction_override_without_attempt2_exhausted_is_downgraded(self):
        # The reverse direction requires an explicit, genuinely exhausted Attempt 2 -- if the
        # model didn't actually exhaust it, the override must not be allowed to stand.
        row = {
            "RecordID": "912345",
            "Master_Property Name": "Casa Gataway",
            "Master_Ownership Type": "APT",
            "Master_Monthly Association Fees": "461",
        }
        fake_result = {
            "determined_type": "HOA",
            "decision": "Override",
            "confidence": "High",
            "evidence_tier_used": "Tier 3",
            "reasoning": "Tier-3 sources describe this as an HOA community.",
            "sources": ["https://realtor.com/x", "https://zillow.com/x", "https://homes.com/x"],
            "structural_edge_case": "none",
            "tier3_exception_invoked": "yes",
            "tier3_exception_direction": "to_coa_hoa",
            "tier3_reverse_attempt2_exhausted": "no",
            "tier3_independent_source_count": 3,
            "tier3_name_address_anchor_confirmed": "yes",
            "tier3_partial_tier12_support": "not_applicable",
            "tier3_contradicting_evidence": "no",
            "tier3_internal_db_corroboration": "Master_Monthly Association Fees is populated ($461), consistent with an HOA",
            "tier3_structural_edge_case_ruled_out": "yes",
            "ownership_concentration": "not_applicable",
            "reverse_conversion_detected": "not_applicable",
            "multi_name_all_agree": "not_applicable",
            "tier3_sales_listing_search_performed": "not_applicable",
            "tier3_sales_evidence_found": "not_applicable",
            "ownership_concentration_verified_externally": "not_applicable",
            "ownership_concentration_contradicting_evidence": "not_applicable",
            "tier3_dual_association_search_performed": "not_applicable",
            "tier3_entity_name_registry_search_performed": "not_applicable",
        }
        with mock.patch("ownership_type_checking.research_property", return_value=fake_result), \
             mock.patch("ownership_type_checking.fetch_url_cached", return_value=None):
            result = otc.process_property(None, "gpt-4o", row, {})
        self.assertEqual(result["decision"], "Not Enough Info")
        self.assertEqual(result["determined_type"], "APT")


class Tier3ExceptionGuardrailTests(unittest.TestCase):
    def _clean_override(self, **overrides):
        result = {
            "determined_type": "APT",
            "decision": "Override",
            "confidence": "High",
            "evidence_tier_used": "Tier 3",
            "reasoning": "Single owner, single leasing office, no MLS sales history found.",
            "sources": ["https://crosscreekapts.com", "https://apartments.com/x", "https://apartmentratings.com/x"],
            "tier3_exception_invoked": "yes",
            "tier3_exception_direction": "to_apt",
            "tier3_reverse_attempt2_exhausted": "not_applicable",
            "tier3_independent_source_count": 3,
            "tier3_name_address_anchor_confirmed": "yes",
            "tier3_partial_tier12_support": "not_applicable",
            "tier3_contradicting_evidence": "no",
            "tier3_internal_db_corroboration": "Master_Monthly Association Fees is null despite 80 units",
            "tier3_structural_edge_case_ruled_out": "yes",
            "ownership_concentration": "not_applicable",
            "reverse_conversion_detected": "not_applicable",
            "multi_name_all_agree": "not_applicable",
            "tier3_sales_listing_search_performed": "yes",
            "tier3_sales_evidence_found": "no",
            "ownership_concentration_verified_externally": "not_applicable",
            "ownership_concentration_contradicting_evidence": "not_applicable",
            "tier3_dual_association_search_performed": "yes",
            "tier3_entity_name_registry_search_performed": "not_applicable",
            "_searched_queries": ["county assessor parcel records for sale"],
        }
        result.update(overrides)
        return result

    def _large_row(self, **overrides):
        row = {
            "Master_Monthly Association Fees": "",
            "Master_Units_50+": "80",
        }
        row.update(overrides)
        return row

    def test_all_four_conditions_hold_allows_override_capped_at_medium(self):
        # This is the Cross Creek Apartments worked example from spec §11.
        row = self._large_row()
        result = self._clean_override()
        fixed = otc._enforce_tier3_override_guardrail(row, result)
        self.assertEqual(fixed["decision"], "Override")
        self.assertEqual(fixed["confidence"], "Medium")
        self.assertTrue(fixed["tier3_exception_used"])

    def test_partial_tier12_support_allows_one_failing_condition(self):
        # The new relaxed-threshold rule: a single (insufficient-alone) Tier 1/2 source plus
        # Tier 3 evidence only needs 3 of the 4 conditions, not all 4.
        row = self._large_row()
        result = self._clean_override(
            evidence_tier_used="Mixed",
            tier3_partial_tier12_support="yes",
            tier3_structural_edge_case_ruled_out="no",  # condition 4 fails -- the one allowed gap
        )
        fixed = otc._enforce_tier3_override_guardrail(row, result)
        self.assertEqual(fixed["decision"], "Override")
        self.assertEqual(fixed["confidence"], "Medium")
        self.assertTrue(fixed["tier3_exception_used"])

    def test_partial_tier12_support_still_fails_with_two_failing_conditions(self):
        # Only ONE condition is allowed to fail under the relaxed threshold -- two is still
        # too many even with partial Tier 1/2 support.
        row = self._large_row()
        result = self._clean_override(
            evidence_tier_used="Mixed",
            tier3_partial_tier12_support="yes",
            tier3_structural_edge_case_ruled_out="no",
            tier3_contradicting_evidence="yes",
        )
        fixed = otc._enforce_tier3_override_guardrail(row, result)
        self.assertEqual(fixed["decision"], "Not Enough Info")

    def test_partial_tier12_support_without_mixed_tier_does_not_relax(self):
        # tier3_partial_tier12_support alone isn't enough -- evidence_tier_used must also be
        # 'Mixed' (a pure 'Tier 3' claim with this flag set is an inconsistent self-report, and
        # the strict all-four bar still applies).
        row = self._large_row()
        result = self._clean_override(
            evidence_tier_used="Tier 3",
            tier3_partial_tier12_support="yes",
            tier3_structural_edge_case_ruled_out="no",
        )
        fixed = otc._enforce_tier3_override_guardrail(row, result)
        self.assertEqual(fixed["decision"], "Not Enough Info")

    def test_mixed_tier_without_partial_support_flag_does_not_relax(self):
        row = self._large_row()
        result = self._clean_override(
            evidence_tier_used="Mixed",
            tier3_partial_tier12_support="no",
            tier3_structural_edge_case_ruled_out="no",
        )
        fixed = otc._enforce_tier3_override_guardrail(row, result)
        self.assertEqual(fixed["decision"], "Not Enough Info")

    def test_mixed_tier_override_not_invoking_exception_is_left_alone(self):
        # A "Mixed"-tier Override that isn't invoking the §4.1 exception at all is relying on
        # ordinary Tier 1/2 corroboration for a ordinary override -- not this guardrail's concern.
        row = self._large_row()
        result = self._clean_override(evidence_tier_used="Mixed", tier3_exception_invoked="no")
        fixed = otc._enforce_tier3_override_guardrail(row, result)
        self.assertEqual(fixed["decision"], "Override")
        self.assertFalse(fixed["tier3_exception_used"])

    def test_newly_built_property_can_still_qualify(self):
        # There is no minimum-age/build-year requirement for this exception -- a recently
        # built investor-owned rental community should qualify exactly like an old one, as
        # long as the four real conditions are met.
        row = self._large_row(**{"Master_Original Build Year": str(int(time.strftime("%Y")) - 1)})
        result = self._clean_override()
        fixed = otc._enforce_tier3_override_guardrail(row, result)
        self.assertEqual(fixed["decision"], "Override")
        self.assertEqual(fixed["confidence"], "Medium")

    def test_fewer_listed_sources_than_claimed_count_now_fails_condition_one(self):
        # Real reported failures ("Mountain Ridge Garden Homes Apartments," "Castle Apartments
        # Condominium Association, Inc."): `sources` listed only 1-2 URLs while
        # tier3_independent_source_count claimed 3+ ("multiple independent listing platforms"),
        # and the override went through anyway. An earlier version of this tool deliberately let
        # this pass (on the theory sources might legitimately list fewer than examined) -- that
        # gap is exactly what these failures exploited, so `sources` is now the authoritative,
        # code-checked floor: it must itself contain 3+ distinct URLs.
        row = self._large_row()
        result = self._clean_override(sources=["https://crosscreekapts.com", "https://apartments.com/x"])
        fixed = otc._enforce_tier3_override_guardrail(row, result)
        self.assertEqual(fixed["decision"], "Not Enough Info")

    def test_three_distinct_listed_sources_satisfies_condition_one(self):
        row = self._large_row()
        result = self._clean_override(
            sources=["https://crosscreekapts.com", "https://apartments.com/x", "https://apartmentratings.com/x"]
        )
        fixed = otc._enforce_tier3_override_guardrail(row, result)
        self.assertEqual(fixed["decision"], "Override")
        self.assertEqual(fixed["confidence"], "Medium")
        self.assertTrue(fixed["tier3_exception_used"])

    def test_duplicate_urls_do_not_count_toward_the_distinct_source_floor(self):
        row = self._large_row()
        result = self._clean_override(
            sources=["https://crosscreekapts.com", "https://crosscreekapts.com", "https://apartments.com/x"]
        )
        fixed = otc._enforce_tier3_override_guardrail(row, result)
        self.assertEqual(fixed["decision"], "Not Enough Info")

    def test_zero_listed_sources_fails_even_with_a_claimed_count(self):
        # The floor: a self-reported count with literally no sources cited at all is an
        # unsupported claim and should still fail, even though the bar is much lower than 3.
        row = self._large_row()
        result = self._clean_override(sources=[])
        fixed = otc._enforce_tier3_override_guardrail(row, result)
        self.assertEqual(fixed["decision"], "Not Enough Info")

    def test_exception_not_invoked_is_downgraded_even_with_strong_evidence(self):
        row = self._large_row()
        result = self._clean_override(tier3_exception_invoked="no")
        fixed = otc._enforce_tier3_override_guardrail(row, result)
        self.assertEqual(fixed["decision"], "Not Enough Info")
        self.assertFalse(fixed["tier3_exception_used"])

    def test_fewer_than_three_sources_fails_condition_one(self):
        row = self._large_row()
        result = self._clean_override(sources=["https://crosscreekapts.com"], tier3_independent_source_count=1)
        fixed = otc._enforce_tier3_override_guardrail(row, result)
        self.assertEqual(fixed["decision"], "Not Enough Info")

    def test_no_name_address_anchor_fails_condition_one(self):
        # Real reported failure: "Dearlove Manor Apts" was overridden to APT because Tier 3
        # sources confirmed real rental apartments at the DB's address -- but none of those
        # sources ever named "Dearlove Manor Apts" specifically; they describe a different,
        # unrelated complex. With no anchor tying the name to the address, those address-only
        # sources are not evidence about this record at all.
        row = self._large_row()
        result = self._clean_override(tier3_name_address_anchor_confirmed="no")
        fixed = otc._enforce_tier3_override_guardrail(row, result)
        self.assertEqual(fixed["decision"], "Not Enough Info")

    def test_missing_name_address_anchor_confirmation_fails_condition_one(self):
        row = self._large_row()
        result = self._clean_override(tier3_name_address_anchor_confirmed="not_applicable")
        fixed = otc._enforce_tier3_override_guardrail(row, result)
        self.assertEqual(fixed["decision"], "Not Enough Info")

    def test_anchor_plus_address_only_sources_still_allows_override(self):
        # The refined rule: not every source needs to match both name and address -- only ONE
        # needs to (the anchor). Once that anchor exists, other sources describing only the
        # address (without repeating the name) still count toward the 3+ source requirement.
        # E.g. Zillow names the property at the DB address (the anchor); two other sites just
        # describe apartments at that same address without using the name.
        row = self._large_row()
        result = self._clean_override(
            tier3_name_address_anchor_confirmed="yes",
            tier3_independent_source_count=3,
            reasoning=(
                "Zillow names the property at the DB's address (anchor); two other listing sites "
                "separately describe apartments at that same address without repeating the name."
            ),
        )
        fixed = otc._enforce_tier3_override_guardrail(row, result)
        self.assertEqual(fixed["decision"], "Override")
        self.assertEqual(fixed["confidence"], "Medium")
        self.assertTrue(fixed["tier3_exception_used"])

    def test_contradicting_evidence_fails_condition_two(self):
        row = self._large_row()
        result = self._clean_override(tier3_contradicting_evidence="yes")
        fixed = otc._enforce_tier3_override_guardrail(row, result)
        self.assertEqual(fixed["decision"], "Not Enough Info")

    def test_no_internal_corroboration_cited_fails_condition_three(self):
        row = self._large_row()
        result = self._clean_override(tier3_internal_db_corroboration="")
        fixed = otc._enforce_tier3_override_guardrail(row, result)
        self.assertEqual(fixed["decision"], "Not Enough Info")

    def test_fee_corroboration_contradicted_by_populated_fee_backstop(self):
        # Model claims a null fee corroborates single ownership, but the row's actual fee
        # field is populated -- direct contradiction the backstop must catch.
        row = self._large_row(**{"Master_Monthly Association Fees": "350"})
        result = self._clean_override()
        fixed = otc._enforce_tier3_override_guardrail(row, result)
        self.assertEqual(fixed["decision"], "Not Enough Info")
        self.assertIn("populated", fixed["reasoning"])

    def test_structural_edge_case_not_ruled_out_fails_condition_four(self):
        row = self._large_row()
        result = self._clean_override(tier3_structural_edge_case_ruled_out="no")
        fixed = otc._enforce_tier3_override_guardrail(row, result)
        self.assertEqual(fixed["decision"], "Not Enough Info")

    def test_exception_never_applies_to_tier1_or_tier2_overrides(self):
        # The exception is specifically about the Tier-3-only case; a real Tier 1 override
        # shouldn't even look at the tier3_* fields.
        row = self._large_row()
        result = self._clean_override(evidence_tier_used="Tier 1", tier3_exception_invoked="no")
        fixed = otc._enforce_tier3_override_guardrail(row, result)
        self.assertEqual(fixed["decision"], "Override")
        self.assertFalse(fixed["tier3_exception_used"])

    def test_forward_direction_requires_sales_listing_search_gate(self):
        # New absolute gate: the forward exception cannot apply without an explicit search for
        # individual unit SALE listings -- claiming "no contradicting evidence" without having
        # looked isn't good enough.
        row = self._large_row()
        result = self._clean_override(tier3_sales_listing_search_performed="no")
        fixed = otc._enforce_tier3_override_guardrail(row, result)
        self.assertEqual(fixed["decision"], "Not Enough Info")

    def test_forward_direction_sales_evidence_found_fails_condition_two(self):
        # Real reported failure ("Sky Nashville"): reasoning described the property as planned
        # for individual sale, yet still claimed no contradicting evidence -- tier3_sales_
        # evidence_found must override a self-serving tier3_contradicting_evidence="no" claim.
        row = self._large_row()
        result = self._clean_override(
            tier3_sales_evidence_found="yes",
            reasoning="Entitled development planned for for-sale condos/townhomes; no conflicting evidence found confirming APT.",
        )
        fixed = otc._enforce_tier3_override_guardrail(row, result)
        self.assertEqual(fixed["decision"], "Not Enough Info")

    def test_reverse_direction_is_not_gated_by_sales_listing_search(self):
        # The sales-listing search gate is specific to the forward (to_apt) direction -- the
        # reverse direction has its own, different gate (tier3_reverse_attempt2_exhausted).
        row = self._large_row(**{"Master_Monthly Association Fees": "350"})
        result = self._clean_override(
            determined_type="HOA",
            tier3_exception_direction="to_coa_hoa",
            tier3_reverse_attempt2_exhausted="yes",
            tier3_sales_listing_search_performed="not_applicable",
            tier3_sales_evidence_found="not_applicable",
            tier3_internal_db_corroboration="Master_Monthly Association Fees is $350/month, a real recurring fee",
        )
        fixed = otc._enforce_tier3_override_guardrail(row, result)
        self.assertEqual(fixed["decision"], "Override")

    def test_forward_direction_owner_field_corroboration_fails_condition_three(self):
        # Real reported concern ("Medley Johns Creek"): the DB's own Owner/Cleaned Owner field
        # is not reliable enough to use as condition-3 corroboration -- a majority-but-not-full
        # owner is often still the DB's sole listed Owner. Only a null/blank fee counts now.
        row = self._large_row()
        result = self._clean_override(
            tier3_internal_db_corroboration="Owner is Ascentris, LLC with no per-unit variation"
        )
        fixed = otc._enforce_tier3_override_guardrail(row, result)
        self.assertEqual(fixed["decision"], "Not Enough Info")

    def test_forward_direction_fee_based_corroboration_still_works(self):
        row = self._large_row()
        result = self._clean_override(
            tier3_internal_db_corroboration="Master_Monthly Association Fees is null despite 80 units"
        )
        fixed = otc._enforce_tier3_override_guardrail(row, result)
        self.assertEqual(fixed["decision"], "Override")

    def test_self_reported_search_performed_without_a_real_query_fails(self):
        # Real reported failures ("Mountain Ridge Garden Homes Apartments," "Castle Apartments
        # Condominium Association, Inc."): reasoning asserted "no sale listings found" without
        # any actual targeted sale-listing search query having been issued. The self-reported
        # tier3_sales_listing_search_performed="yes" is no longer sufficient on its own.
        row = self._large_row()
        result = self._clean_override(_searched_queries=["Cross Creek Apartments reviews"])
        fixed = otc._enforce_tier3_override_guardrail(row, result)
        self.assertEqual(fixed["decision"], "Not Enough Info")

    def test_genuine_sale_query_for_a_different_property_does_not_count(self):
        row = self._large_row(**{"Address": "100 Main St", "Master_Property Name": "Cross Creek Apartments"})
        result = self._clean_override(_searched_queries=["456 Oak Ave condo for sale"])
        fixed = otc._enforce_tier3_override_guardrail(row, result)
        self.assertEqual(fixed["decision"], "Not Enough Info")

    def test_genuine_sale_query_matching_the_address_satisfies_the_gate(self):
        row = self._large_row(**{"Address": "100 Main St", "Master_Property Name": "Cross Creek Apartments"})
        result = self._clean_override(_searched_queries=["100 Main St condo for sale"])
        fixed = otc._enforce_tier3_override_guardrail(row, result)
        self.assertEqual(fixed["decision"], "Override")

    def test_missing_dual_association_search_fails(self):
        # Real reported bug: both Mountain Ridge (DB: COA) and Castle Apartments (DB: COA) had
        # reasoning that only checked for HOA documents ("no HOA"), never a condominium
        # association -- despite the DB itself listing COA. Absence of one type's evidence must
        # never be treated as absence of any association.
        row = self._large_row()
        result = self._clean_override(tier3_dual_association_search_performed="no")
        fixed = otc._enforce_tier3_override_guardrail(row, result)
        self.assertEqual(fixed["decision"], "Not Enough Info")

    def test_legal_entity_name_requires_registry_search(self):
        # Real reported failure: "Castle Apartments Condominium Association, Inc." was overridden
        # to APT without ever running a Sunbiz-style registry search for that exact entity name.
        row = self._large_row(**{"Master_Property Name": "Castle Apartments Condominium Association, Inc."})
        result = self._clean_override(tier3_entity_name_registry_search_performed="not_applicable")
        fixed = otc._enforce_tier3_override_guardrail(row, result)
        self.assertEqual(fixed["decision"], "Not Enough Info")

    def test_legal_entity_name_registry_search_performed_allows_override(self):
        row = self._large_row(**{"Master_Property Name": "Castle Apartments Condominium Association, Inc."})
        result = self._clean_override(
            tier3_entity_name_registry_search_performed="yes",
            _searched_queries=["Castle Apartments units for sale"],
        )
        fixed = otc._enforce_tier3_override_guardrail(row, result)
        self.assertEqual(fixed["decision"], "Override")

    def test_registry_search_gate_is_not_applicable_for_ordinary_names(self):
        row = self._large_row(**{"Master_Property Name": "Cross Creek Apartments"})
        result = self._clean_override(
            tier3_entity_name_registry_search_performed="not_applicable",
            _searched_queries=["Cross Creek Apartments units for sale"],
        )
        fixed = otc._enforce_tier3_override_guardrail(row, result)
        self.assertEqual(fixed["decision"], "Override")

    def test_registry_search_gate_matches_owners_association_and_bare_condominium_variants(self):
        for name in ["Willow Owners Association, Inc.", "Willow Condominium, Inc."]:
            with self.subTest(name=name):
                row = self._large_row(**{"Master_Property Name": name})
                result = self._clean_override(tier3_entity_name_registry_search_performed="no")
                fixed = otc._enforce_tier3_override_guardrail(row, result)
                self.assertEqual(fixed["decision"], "Not Enough Info")

    def test_registry_search_gate_does_not_fire_on_casual_condo_or_apartments_naming(self):
        # Casual use of "condo" or "apartments" in a name is never evidence either way (§5.2) --
        # this gate is specifically about the full formal legal-entity string.
        row = self._large_row(**{"Master_Property Name": "Sunny Condo Apartments"})
        result = self._clean_override(
            tier3_entity_name_registry_search_performed="not_applicable",
            _searched_queries=["Sunny Condo Apartments units for sale"],
        )
        fixed = otc._enforce_tier3_override_guardrail(row, result)
        self.assertEqual(fixed["decision"], "Override")


class SalesEvidenceGuardrailTests(unittest.TestCase):
    """Absolute rule per a real reported failure ("Sky Nashville"): finding evidence of
    individual unit sales, sale listings, or units planned/entitled for individual sale directly
    rules out an APT conclusion, regardless of mechanism -- but the reverse isn't true (rental
    listings at an HOA/COA don't rule out HOA/COA)."""

    def _override(self, **overrides):
        result = {
            "decision": "Override",
            "determined_type": "APT",
            "reasoning": "Marketed as a rental community.",
            "tier3_sales_evidence_found": "no",
            "ownership_concentration_verified_externally": "not_applicable",
            "ownership_concentration_contradicting_evidence": "not_applicable",
            "tier3_dual_association_search_performed": "not_applicable",
            "tier3_entity_name_registry_search_performed": "not_applicable",
        }
        result.update(overrides)
        return result

    def test_sales_evidence_found_blocks_override_to_apt(self):
        result = self._override(tier3_sales_evidence_found="yes")
        fixed = otc._enforce_sales_evidence_guardrail("HOA", result)
        self.assertEqual(fixed["decision"], "Confirmed")
        self.assertEqual(fixed["determined_type"], "HOA")
        self.assertTrue(fixed["sales_evidence_override_blocked"])

    def test_no_sales_evidence_leaves_override_alone(self):
        result = self._override(tier3_sales_evidence_found="no")
        fixed = otc._enforce_sales_evidence_guardrail("HOA", result)
        self.assertEqual(fixed["decision"], "Override")
        self.assertEqual(fixed["determined_type"], "APT")

    def test_confirmed_decision_is_left_alone(self):
        result = self._override(decision="Confirmed", determined_type="HOA", tier3_sales_evidence_found="yes")
        fixed = otc._enforce_sales_evidence_guardrail("HOA", result)
        self.assertEqual(fixed["decision"], "Confirmed")

    def test_does_not_apply_when_override_target_is_not_apt(self):
        # Rule is deliberately asymmetric -- only fires in the APT direction.
        result = self._override(determined_type="HOA", tier3_sales_evidence_found="yes")
        fixed = otc._enforce_sales_evidence_guardrail("APT", result)
        self.assertEqual(fixed["determined_type"], "HOA")

    def test_sky_nashville_end_to_end(self):
        # Full regression, mirroring the exact reported failure: reasoning describes the property
        # as an entitled development planned for for-sale condos/townhomes, and then claims no
        # conflicting evidence was found -- the override must not stand.
        row = {
            "RecordID": "88110022",
            "Master_Property Name": "Sky Nashville",
            "Master_Ownership Type": "HOA",
            "Master_Monthly Association Fees": "",
        }
        fake_result = {
            "determined_type": "APT",
            "decision": "Override",
            "confidence": "Medium",
            "evidence_tier_used": "Tier 3",
            "reasoning": (
                "Sky Nashville is an entitled development planned for for-sale condos/townhomes. "
                "No conflicting evidence was found confirming APT."
            ),
            "sources": ["https://skynashville.com/x", "https://apartments.com/x", "https://apartmentratings.com/x"],
            "structural_edge_case": "none",
            "tier3_exception_invoked": "yes",
            "tier3_exception_direction": "to_apt",
            "tier3_reverse_attempt2_exhausted": "not_applicable",
            "tier3_independent_source_count": 3,
            "tier3_name_address_anchor_confirmed": "yes",
            "tier3_partial_tier12_support": "not_applicable",
            "tier3_contradicting_evidence": "no",
            "tier3_internal_db_corroboration": "Master_Monthly Association Fees is null",
            "tier3_structural_edge_case_ruled_out": "yes",
            "ownership_concentration": "not_applicable",
            "reverse_conversion_detected": "not_applicable",
            "multi_name_all_agree": "not_applicable",
            "tier3_sales_listing_search_performed": "yes",
            "tier3_sales_evidence_found": "yes",
            "ownership_concentration_verified_externally": "not_applicable",
            "ownership_concentration_contradicting_evidence": "not_applicable",
            "tier3_dual_association_search_performed": "not_applicable",
            "tier3_entity_name_registry_search_performed": "not_applicable",
        }
        with mock.patch("ownership_type_checking.research_property", return_value=fake_result), \
             mock.patch("ownership_type_checking.fetch_url_cached", return_value=None):
            result = otc.process_property(None, "gpt-4o", row, {})
        self.assertEqual(result["decision"], "Confirmed")
        self.assertEqual(result["determined_type"], "HOA")
        self.assertTrue(result["sales_evidence_override_blocked"])


class SearchQueryExtractionTests(unittest.TestCase):
    """Plumbing: research_property() must inject the model's ACTUAL issued search queries into
    the result (not just the self-reported search-performed fields), so guardrails can verify a
    genuinely targeted search happened."""

    class _FakeAction:
        # Mirrors the real OpenAI SDK's ActionSearch/ActionOpenPage/ActionFind union -- action
        # type defaults to "search" since that's the case this module cares about; query/queries
        # default to None/[] like the real (mostly-optional) fields.
        def __init__(self, query=None, queries=None, type="search", **kwargs):
            self.type = type
            self.query = query
            self.queries = queries
            for key, value in kwargs.items():
                setattr(self, key, value)

    class _FakeItem:
        def __init__(self, type_, **kwargs):
            self.type = type_
            for key, value in kwargs.items():
                setattr(self, key, value)

    def test_extract_openai_search_queries_pulls_query_text(self):
        items = [
            self._FakeItem("web_search_call", action=self._FakeAction(query="109 Indigo Road condo for sale")),
            self._FakeItem("web_search_call", action=self._FakeAction(query="Mountain Ridge Garden Homes reviews")),
            self._FakeItem("message", content=[]),
        ]
        queries = otc.extract_openai_search_queries(items)
        self.assertEqual(queries, ["109 Indigo Road condo for sale", "Mountain Ridge Garden Homes reviews"])

    def test_extract_openai_search_queries_reads_the_plural_queries_field_too(self):
        # Real, previously-mishandled bug: the OpenAI SDK's ActionSearch type exposes query text
        # on EITHER `query` (singular) or `queries` (plural list) -- the live API populated
        # `queries` here, and an earlier version of this function only read `query`, so it saw
        # nothing at all and every sale-listing-search gate downgraded regardless of what the
        # model actually searched for.
        items = [
            self._FakeItem(
                "web_search_call",
                action=self._FakeAction(queries=["109 Indigo Road condo for sale", "109 Indigo Road sold"]),
            ),
        ]
        queries = otc.extract_openai_search_queries(items)
        self.assertEqual(queries, ["109 Indigo Road condo for sale", "109 Indigo Road sold"])

    def test_extract_openai_search_queries_reads_both_fields_if_both_are_populated(self):
        items = [
            self._FakeItem(
                "web_search_call",
                action=self._FakeAction(query="109 Indigo Road reviews", queries=["109 Indigo Road for sale"]),
            ),
        ]
        queries = otc.extract_openai_search_queries(items)
        self.assertEqual(queries, ["109 Indigo Road reviews", "109 Indigo Road for sale"])

    def test_extract_openai_search_queries_ignores_non_search_action_types(self):
        # open_page and find_in_page actions don't carry a search query at all.
        items = [
            self._FakeItem("web_search_call", action=self._FakeAction(type="open_page", url="https://x.com")),
            self._FakeItem("web_search_call", action=self._FakeAction(type="find_in_page", pattern="for sale", url="https://x.com")),
        ]
        self.assertEqual(otc.extract_openai_search_queries(items), [])

    def test_extract_openai_search_queries_ignores_non_search_items(self):
        items = [self._FakeItem("message", content=[]), self._FakeItem("function_call", name="submit_assessment")]
        self.assertEqual(otc.extract_openai_search_queries(items), [])

    def test_research_property_injects_searched_queries_into_result(self):
        class _FakeResponse:
            def __init__(self, output, id_):
                self.output = output
                self.id = id_

        submit_call = SearchQueryExtractionTests._FakeItem(
            "function_call",
            name="submit_assessment",
            arguments=json.dumps({"determined_type": "APT", "decision": "Confirmed", "sources": []}),
        )
        response = _FakeResponse(
            output=[
                self._FakeItem("web_search_call", action=self._FakeAction("109 Indigo Road condo for sale")),
                submit_call,
            ],
            id_="resp_1",
        )
        with mock.patch("ownership_type_checking.call_openai_with_backoff", return_value=response):
            result = otc.research_property(None, {}, [], {}, "gpt-4o")
        self.assertEqual(result["_searched_queries"], ["109 Indigo Road condo for sale"])


class MountainRidgeAndCastleApartmentsRegressionTests(unittest.TestCase):
    """End-to-end regressions replaying the two exact reported false positives: both DB-listed
    COA, both overridden to APT via §4.1 despite failing multiple of its own conditions."""

    def test_mountain_ridge_end_to_end(self):
        # Only 1 URL actually listed in Sources despite the reasoning claiming "multiple
        # independent listing platforms" (condition 1), reasoning only mentions checking for HOA
        # despite the DB listing COA (dual-association search), and no genuine targeted
        # sale-listing search query was ever issued (condition 2's absolute gate).
        row = {
            "RecordID": "5233990",
            "Master_Property Name": "Mountain Ridge Garden Homes Apartments",
            "Address": "109 Indigo Road, Hackettstown, NJ 07840-4541, USA",
            "Master_Ownership Type": "COA",
            "Master_Monthly Association Fees": "",
        }
        fake_result = {
            "determined_type": "APT",
            "decision": "Override",
            "confidence": "Medium",
            "evidence_tier_used": "Tier 3",
            "reasoning": (
                "Multiple independent listing platforms name 'Mountain Ridge Garden Homes "
                "Apartments' at the address, indicating a rental community with no sale listings "
                "or HOA documents."
            ),
            "sources": ["https://www.apartmenthomeliving.com/apartment-finder/Mountain-Ridge-Garden-Homes-Apartments"],
            "structural_edge_case": "none",
            "tier3_exception_invoked": "yes",
            "tier3_exception_direction": "to_apt",
            "tier3_reverse_attempt2_exhausted": "not_applicable",
            "tier3_independent_source_count": 3,
            "tier3_name_address_anchor_confirmed": "yes",
            "tier3_partial_tier12_support": "not_applicable",
            "tier3_contradicting_evidence": "no",
            "tier3_internal_db_corroboration": "Master_Monthly Association Fees is null",
            "tier3_structural_edge_case_ruled_out": "yes",
            "ownership_concentration": "not_applicable",
            "reverse_conversion_detected": "not_applicable",
            "multi_name_all_agree": "not_applicable",
            "tier3_sales_listing_search_performed": "yes",
            "tier3_sales_evidence_found": "no",
            "ownership_concentration_verified_externally": "not_applicable",
            "ownership_concentration_contradicting_evidence": "not_applicable",
            "tier3_dual_association_search_performed": "no",
            "tier3_entity_name_registry_search_performed": "not_applicable",
            "_searched_queries": ["Mountain Ridge Garden Homes Apartments rental reviews"],
        }
        with mock.patch("ownership_type_checking.research_property", return_value=fake_result), \
             mock.patch("ownership_type_checking.fetch_url_cached", return_value=None):
            result = otc.process_property(None, "gpt-4o", row, {})
        self.assertEqual(result["decision"], "Not Enough Info")
        self.assertEqual(result["determined_type"], "COA")

    def test_castle_apartments_end_to_end(self):
        # Only 2 URLs listed (condition 1), no genuine sale-listing search query issued
        # (condition 2's absolute gate), reasoning only checked for HOA despite DB listing COA
        # (dual-association search), and the property's own name is an explicit legal-entity
        # string that was never checked against a state business registry.
        row = {
            "RecordID": "5364133",
            "Master_Property Name": "CASTLE APARTMENTS CONDOMINIUM ASSOCIATION, INC.",
            "Address": "10907 Southwest 88th Street, Florida 33176-1230, USA",
            "Master_Ownership Type": "COA",
            "Master_Monthly Association Fees": "",
        }
        fake_result = {
            "determined_type": "APT",
            "decision": "Override",
            "confidence": "Medium",
            "evidence_tier_used": "Tier 3",
            "reasoning": (
                "Independent Tier-3 sources describe 'Castle Apts Condo' as a rental property "
                "with no HOA. At least one source ties the name and address together, and no "
                "sale listings or individual ownership evidence exist."
            ),
            "sources": [
                "https://www.homes.com/property/10907-sw-88th-st-miami-fl-unit-426/x",
                "https://www.apartments.com/10907-n-kendall-dr-miami-fl/x",
            ],
            "structural_edge_case": "none",
            "tier3_exception_invoked": "yes",
            "tier3_exception_direction": "to_apt",
            "tier3_reverse_attempt2_exhausted": "not_applicable",
            "tier3_independent_source_count": 3,
            "tier3_name_address_anchor_confirmed": "yes",
            "tier3_partial_tier12_support": "not_applicable",
            "tier3_contradicting_evidence": "no",
            "tier3_internal_db_corroboration": "Master_Monthly Association Fees is null",
            "tier3_structural_edge_case_ruled_out": "yes",
            "ownership_concentration": "not_applicable",
            "reverse_conversion_detected": "not_applicable",
            "multi_name_all_agree": "not_applicable",
            "tier3_sales_listing_search_performed": "yes",
            "tier3_sales_evidence_found": "no",
            "ownership_concentration_verified_externally": "not_applicable",
            "ownership_concentration_contradicting_evidence": "not_applicable",
            "tier3_dual_association_search_performed": "no",
            "tier3_entity_name_registry_search_performed": "not_applicable",
            "_searched_queries": ["Castle Apts Condo rental reviews"],
        }
        with mock.patch("ownership_type_checking.research_property", return_value=fake_result), \
             mock.patch("ownership_type_checking.fetch_url_cached", return_value=None):
            result = otc.process_property(None, "gpt-4o", row, {})
        self.assertEqual(result["decision"], "Not Enough Info")
        self.assertEqual(result["determined_type"], "COA")


class DecisionDisplayTests(unittest.TestCase):
    def test_confirmed_displays_as_confirmed(self):
        result = {"decision": "Confirmed", "determined_type": "COA"}
        self.assertEqual(otc.decision_display("COA", result), "Confirmed")

    def test_not_enough_info_displays_as_confirmed(self):
        # Not Enough Info always means "keep the DB label" -- it collapses to the same
        # display value as Confirmed; the thin-evidence nature shows up via confidence/reasoning.
        result = {"decision": "Not Enough Info", "determined_type": "COA"}
        self.assertEqual(otc.decision_display("COA", result), "Confirmed")

    def test_override_displays_as_changed_from_x_to_y(self):
        result = {"decision": "Override", "determined_type": "APT"}
        self.assertEqual(otc.decision_display("COA", result), "Changed from COA to APT")

    def test_override_with_no_actual_change_displays_as_confirmed(self):
        result = {"decision": "Override", "determined_type": "COA"}
        self.assertEqual(otc.decision_display("COA", result), "Confirmed")


class CheckpointRoundTripTests(unittest.TestCase):
    def test_load_after_append_keys_by_property_id(self):
        import threading
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "checkpoint.jsonl"
            lock = threading.Lock()
            otc.append_checkpoint(path, {"property_id": "605", "decision": "Confirmed"}, lock)
            otc.append_checkpoint(path, {"property_id": "51537", "decision": "Confirmed"}, lock)
            loaded = otc.load_checkpoint(path)
            self.assertEqual(set(loaded.keys()), {"605", "51537"})

    def test_missing_checkpoint_file_returns_empty(self):
        self.assertEqual(otc.load_checkpoint(Path("/nonexistent/path.jsonl")), {})


class SummaryTests(unittest.TestCase):
    def _result(self, decision, rules, signal_types, trigger_count=None, tier3_exception_used=False):
        return {
            "decision": decision,
            "trigger_rules": rules,
            "trigger_types": signal_types,
            "trigger_count": trigger_count if trigger_count is not None else len(rules),
            "is_error": False,
            "tier3_exception_used": tier3_exception_used,
        }

    def test_tier3_exception_overrides_are_tracked_separately(self):
        results = {
            "1": self._result("Override", ["Has Leasing Info"], ["Marketing"], tier3_exception_used=True),
            "2": self._result("Override", ["Type-Name Mismatch (APT)"], ["Naming"], tier3_exception_used=False),
            "3": self._result("Confirmed", [], []),
        }
        summary = otc.compute_summary(results)
        self.assertEqual(summary["tier3_exception_overrides"], 1)

    def test_override_rate_by_rule(self):
        results = {
            "1": self._result("Confirmed", ["Type-Name Mismatch (APT)"], ["Naming"]),
            "2": self._result("Override", ["Type-Name Mismatch (APT)"], ["Naming"]),
            "3": self._result("Confirmed", ["High Floor Count"], ["Structural"]),
        }
        summary = otc.compute_summary(results)
        self.assertEqual(summary["override_rate_by_rule"]["Type-Name Mismatch (APT)"], {"total": 2, "override": 1})
        self.assertEqual(summary["override_rate_by_rule"]["High Floor Count"], {"total": 1, "override": 0})

    def test_by_decision_counts(self):
        results = {
            "1": self._result("Confirmed", [], []),
            "2": self._result("Not Enough Info", [], []),
        }
        summary = otc.compute_summary(results)
        self.assertEqual(summary["by_decision"]["Confirmed"], 1)
        self.assertEqual(summary["by_decision"]["Not Enough Info"], 1)
        self.assertEqual(summary["by_decision"]["Override"], 0)


class BuildOutputDfTests(unittest.TestCase):
    def test_no_property_id_trigger_or_archetype_columns_and_correct_order(self):
        df = pd.DataFrame([{"RecordID": "605", "Master_Property Name": "Barton Village - Flats I"}])
        result = {
            "605": {
                "db_listed_type": "HOA",
                "determined_type": "HOA",
                "decision": "Confirmed",
                "decision_display": "Confirmed",
                "confidence": "Low",
                "evidence_tier_used": "Tier 3",
                "reasoning": "Ambiguous lease-up phase evidence; keeping DB label.",
                "sources": ["https://example.com"],
            }
        }
        out = otc.build_output_df(df, result)
        new_columns = [c for c in out.columns if c not in df.columns]
        self.assertEqual(
            new_columns,
            ["DB Listed Type", "Determined Type", "Decision", "Confidence", "Evidence Tier Used", "Reasoning", "Sources"],
        )
        for forbidden in ("Property ID", "Trigger Rule(s)", "Trigger Count", "Trigger Types", "Archetype Flag"):
            self.assertNotIn(forbidden, out.columns)
        self.assertEqual(out.at[0, "Decision"], "Confirmed")

    def test_override_row_shows_changed_from_x_to_y(self):
        df = pd.DataFrame([{"RecordID": "1", "Master_Property Name": "Something"}])
        result = {
            "1": {
                "db_listed_type": "HOA",
                "determined_type": "APT",
                "decision": "Override",
                "decision_display": "Changed from HOA to APT",
                "confidence": "High",
                "evidence_tier_used": "Tier 1",
                "reasoning": "County registry confirms rental apartment complex.",
                "sources": [],
            }
        }
        out = otc.build_output_df(df, result)
        self.assertEqual(out.at[0, "Decision"], "Changed from HOA to APT")


class SummaryStatsTests(unittest.TestCase):
    def _result(self, db_type, determined_type, decision):
        return {"db_listed_type": db_type, "determined_type": determined_type, "decision": decision}

    def test_confirmed_and_changed_fractions_are_of_total(self):
        results = {
            "1": self._result("HOA", "HOA", "Confirmed"),
            "2": self._result("HOA", "HOA", "Not Enough Info"),
            "3": self._result("HOA", "APT", "Override"),
            "4": self._result("COA", "COA", "Confirmed"),
        }
        stats = otc.compute_summary_stats(results)
        self.assertEqual(stats["total"], 4)
        self.assertEqual(stats["confirmed"], 3)  # Confirmed, Not Enough Info (no real change), Confirmed
        self.assertEqual(stats["changed"], 1)
        self.assertEqual(stats["changes_by_transition"], {"HOA to APT": 1})

    def test_transition_breakdown_only_includes_transitions_that_occur(self):
        results = {
            "1": self._result("HOA", "APT", "Override"),
            "2": self._result("HOA", "APT", "Override"),
            "3": self._result("APT", "COA", "Override"),
            "4": self._result("COA", "COA", "Confirmed"),
        }
        stats = otc.compute_summary_stats(results)
        self.assertEqual(stats["changed"], 3)
        self.assertEqual(stats["changes_by_transition"], {"HOA to APT": 2, "APT to COA": 1})
        self.assertNotIn("APT to HOA", stats["changes_by_transition"])
        self.assertNotIn("COA to HOA", stats["changes_by_transition"])

    def test_no_changes_produces_empty_transition_breakdown(self):
        results = {"1": self._result("HOA", "HOA", "Confirmed")}
        stats = otc.compute_summary_stats(results)
        self.assertEqual(stats["changed"], 0)
        self.assertEqual(stats["changes_by_transition"], {})

    def test_summary_df_rows_and_percentages(self):
        results = {
            "1": self._result("HOA", "APT", "Override"),
            "2": self._result("HOA", "APT", "Override"),
            "3": self._result("APT", "COA", "Override"),
            "4": self._result("COA", "COA", "Confirmed"),
        }
        no_master_source = {"Hotwire": 0, "CoStar": 0, "First American": 0, "Other": 0}
        df = otc.build_summary_stats_df(otc.compute_summary_stats(results), no_master_source)
        rows = list(df.itertuples(index=False, name=None))
        self.assertEqual(rows[0], ("Confirmed", "25% (1/4)"))
        self.assertEqual(rows[1], ("Changed", "75% (3/4)"))
        # Sub-rows are a fraction of the CHANGED count (3), not the total (4).
        self.assertIn(("  APT to COA", "33% (1/3)"), rows)
        self.assertIn(("  HOA to APT", "67% (2/3)"), rows)

    def test_write_output_xlsx_puts_summary_sheet_first(self):
        from openpyxl import load_workbook

        df = pd.DataFrame([
            {"RecordID": "1", "Master_Property Name": "A"},
            {"RecordID": "2", "Master_Property Name": "B"},
        ])
        results = {
            "1": {
                "db_listed_type": "HOA", "determined_type": "APT", "decision": "Override",
                "decision_display": "Changed from HOA to APT", "confidence": "Medium",
                "evidence_tier_used": "Tier 3", "reasoning": "x", "sources": [],
            },
            "2": {
                "db_listed_type": "COA", "determined_type": "COA", "decision": "Confirmed",
                "decision_display": "Confirmed", "confidence": "High",
                "evidence_tier_used": "Tier 1", "reasoning": "y", "sources": [],
            },
        }
        out_df = otc.build_output_df(df, results)
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "results.xlsx")
            otc.write_output(out_df, results, path)
            wb = load_workbook(path)
            self.assertEqual(wb.sheetnames, ["Summary", "Results"])
            summary_df = pd.read_excel(path, sheet_name="Summary")
            self.assertEqual(summary_df.loc[summary_df["Metric"] == "Confirmed", "Value"].iloc[0], "50% (1/2)")
            self.assertEqual(summary_df.loc[summary_df["Metric"] == "Changed", "Value"].iloc[0], "50% (1/2)")


class MasterSourceBreakdownTests(unittest.TestCase):
    def test_priority_hotwire_beats_costar_and_fa(self):
        row = {"In HW": "1", "In Costar": "1", "In FA": "1"}
        self.assertEqual(otc._master_source(row), "Hotwire")

    def test_priority_costar_beats_fa_when_not_in_hw(self):
        row = {"In HW": "0", "In Costar": "1", "In FA": "1"}
        self.assertEqual(otc._master_source(row), "CoStar")

    def test_first_american_when_only_fa_flag_set(self):
        row = {"In HW": "0", "In Costar": "0", "In FA": "1"}
        self.assertEqual(otc._master_source(row), "First American")

    def test_other_when_no_flags_set(self):
        row = {"In HW": "0", "In Costar": "0", "In FA": "0"}
        self.assertEqual(otc._master_source(row), "Other")

    def test_other_when_flags_missing_entirely(self):
        self.assertEqual(otc._master_source({}), "Other")

    def test_breakdown_only_counts_changed_rows(self):
        out_df = pd.DataFrame([
            {"RecordID": "1", "Decision": "Changed from HOA to APT", "In HW": "1", "In Costar": "0", "In FA": "0"},
            {"RecordID": "2", "Decision": "Changed from COA to APT", "In HW": "0", "In Costar": "1", "In FA": "0"},
            {"RecordID": "3", "Decision": "Confirmed", "In HW": "1", "In Costar": "0", "In FA": "0"},
        ])
        counts = otc.compute_master_source_breakdown(out_df)
        self.assertEqual(counts, {"Hotwire": 1, "CoStar": 1, "First American": 0, "Other": 0})

    def test_breakdown_includes_all_four_labels_even_at_zero(self):
        out_df = pd.DataFrame([{"RecordID": "1", "Decision": "Confirmed", "In HW": "1"}])
        counts = otc.compute_master_source_breakdown(out_df)
        self.assertEqual(set(counts.keys()), {"Hotwire", "CoStar", "First American", "Other"})
        self.assertEqual(counts["Hotwire"], 0)

    def test_summary_df_master_source_rows_are_fraction_of_changed(self):
        results = {
            "1": self._result_stub("HOA", "APT", "Override"),
            "2": self._result_stub("COA", "APT", "Override"),
        }
        master_source_counts = {"Hotwire": 1, "CoStar": 1, "First American": 0, "Other": 0}
        df = otc.build_summary_stats_df(otc.compute_summary_stats(results), master_source_counts)
        rows = list(df.itertuples(index=False, name=None))
        self.assertIn(("  Hotwire", "50% (1/2)"), rows)
        self.assertIn(("  CoStar", "50% (1/2)"), rows)
        self.assertIn(("  First American", "0% (0/2)"), rows)
        self.assertIn(("  Other", "0% (0/2)"), rows)

    @staticmethod
    def _result_stub(db_type, determined_type, decision):
        return {"db_listed_type": db_type, "determined_type": determined_type, "decision": decision}


class HoaCoaNamingMatchTests(unittest.TestCase):
    """§4.2-adjacent tiebreaker: once an APT -> COA/HOA override already stands, align
    determined_type to an HOA/COA keyword in the property's own name, since naming is the more
    reliable signal for which of the two once the override itself is already justified."""

    def test_hoa_named_property_overridden_to_coa_is_corrected_to_hoa(self):
        row = {"Master_Property Name": "Willow Creek Homeowners Association"}
        result = {"decision": "Override", "determined_type": "COA", "reasoning": "Tier 3 evidence supports a condo/HOA structure."}
        fixed = otc._enforce_hoa_coa_naming_match(row, "APT", result)
        self.assertEqual(fixed["determined_type"], "HOA")

    def test_coa_named_property_overridden_to_hoa_is_corrected_to_coa(self):
        row = {"Master_Property Name": "Riverside Condos"}
        result = {"decision": "Override", "determined_type": "HOA", "reasoning": "Tier 3 evidence supports a condo/HOA structure."}
        fixed = otc._enforce_hoa_coa_naming_match(row, "APT", result)
        self.assertEqual(fixed["determined_type"], "COA")

    def test_matching_name_and_type_is_left_alone(self):
        row = {"Master_Property Name": "Willow Creek HOA"}
        result = {"decision": "Override", "determined_type": "HOA", "reasoning": "Matches."}
        fixed = otc._enforce_hoa_coa_naming_match(row, "APT", result)
        self.assertEqual(fixed["determined_type"], "HOA")

    def test_name_with_no_hoa_or_coa_keyword_leaves_models_pick_alone(self):
        row = {"Master_Property Name": "Casa Gataway"}
        result = {"decision": "Override", "determined_type": "COA", "reasoning": "Tier 3 evidence."}
        fixed = otc._enforce_hoa_coa_naming_match(row, "APT", result)
        self.assertEqual(fixed["determined_type"], "COA")

    def test_name_with_both_keywords_is_ambiguous_and_leaves_models_pick_alone(self):
        row = {"Master_Property Name": "Lakeside Condo Homeowners Community"}
        result = {"decision": "Override", "determined_type": "HOA", "reasoning": "Tier 3 evidence."}
        fixed = otc._enforce_hoa_coa_naming_match(row, "APT", result)
        self.assertEqual(fixed["determined_type"], "HOA")

    def test_does_not_apply_when_db_type_is_not_apt(self):
        # Only the APT -> COA/HOA direction is in scope here -- a COA<->HOA override starting
        # from a non-APT DB label (or a forward to_apt override) is untouched by this check.
        row = {"Master_Property Name": "Willow Creek Homeowners Association"}
        result = {"decision": "Override", "determined_type": "COA", "reasoning": "Tier 1/2 evidence."}
        fixed = otc._enforce_hoa_coa_naming_match(row, "HOA", result)
        self.assertEqual(fixed["determined_type"], "COA")

    def test_does_not_apply_to_confirmed_decisions(self):
        row = {"Master_Property Name": "Willow Creek Homeowners Association"}
        result = {"decision": "Confirmed", "determined_type": "APT", "reasoning": "No contradicting evidence found."}
        fixed = otc._enforce_hoa_coa_naming_match(row, "APT", result)
        self.assertEqual(fixed["determined_type"], "APT")

    def test_does_not_apply_when_override_target_is_apt(self):
        row = {"Master_Property Name": "Willow Creek Homeowners Association"}
        result = {"decision": "Override", "determined_type": "APT", "reasoning": "Tier 3 corroborated override."}
        fixed = otc._enforce_hoa_coa_naming_match(row, "HOA", result)
        self.assertEqual(fixed["determined_type"], "APT")

    def test_casa_gataway_style_override_corrected_to_named_type_end_to_end(self):
        # End-to-end regression: the model settles on COA via the §4.2 reverse-direction
        # exception, but the property is actually named as an HOA -- the naming tiebreaker
        # should correct the final label without disturbing the override itself.
        row = {
            "RecordID": "912345",
            "Master_Property Name": "Casa Gataway Hoa",
            "Master_Ownership Type": "APT",
            "Master_Monthly Association Fees": "461",
        }
        fake_result = {
            "determined_type": "COA",
            "decision": "Override",
            "confidence": "High",
            "evidence_tier_used": "Tier 3",
            "reasoning": "Tier-3 corroborated reverse override: multiple listings and a real fee support a condo/HOA structure.",
            "sources": ["https://realtor.com/x", "https://zillow.com/x", "https://homes.com/x"],
            "structural_edge_case": "none",
            "tier3_exception_invoked": "yes",
            "tier3_exception_direction": "to_coa_hoa",
            "tier3_reverse_attempt2_exhausted": "yes",
            "tier3_independent_source_count": 3,
            "tier3_name_address_anchor_confirmed": "yes",
            "tier3_partial_tier12_support": "not_applicable",
            "tier3_contradicting_evidence": "no",
            "tier3_internal_db_corroboration": "Master_Monthly Association Fees is populated ($461)",
            "tier3_structural_edge_case_ruled_out": "yes",
            "ownership_concentration": "not_applicable",
            "reverse_conversion_detected": "not_applicable",
            "multi_name_all_agree": "not_applicable",
            "tier3_sales_listing_search_performed": "not_applicable",
            "tier3_sales_evidence_found": "not_applicable",
            "ownership_concentration_verified_externally": "not_applicable",
            "ownership_concentration_contradicting_evidence": "not_applicable",
            "tier3_dual_association_search_performed": "not_applicable",
            "tier3_entity_name_registry_search_performed": "not_applicable",
        }
        with mock.patch("ownership_type_checking.research_property", return_value=fake_result), \
             mock.patch("ownership_type_checking.fetch_url_cached", return_value=None):
            result = otc.process_property(None, "gpt-4o", row, {})
        self.assertEqual(result["decision"], "Override")
        self.assertEqual(result["determined_type"], "HOA")
        self.assertEqual(result["decision_display"], "Changed from APT to HOA")


class FunctionalOwnershipGuardrailTests(unittest.TestCase):
    """§2.1: the DB label reflects who we'd have to sell to, not legal structure. Rule A
    ('single_owner_full_bulk') forces APT despite a legal condo/HOA declaration; Rule B
    ('individual_owner_present') keeps COA/HOA the moment even one unit is individually owned."""

    def test_rule_a_forces_apt_despite_legal_coa_label(self):
        result = {
            "decision": "Confirmed",
            "determined_type": "COA",
            "reasoning": "Legally a condo, but 100% single-owned with one leasing office.",
            "ownership_concentration": "single_owner_full_bulk",
            "reverse_conversion_detected": "not_applicable",
            "multi_name_all_agree": "not_applicable",
            "tier3_sales_listing_search_performed": "yes",
            "tier3_sales_evidence_found": "no",
            "ownership_concentration_verified_externally": "yes",
            "ownership_concentration_contradicting_evidence": "no",
            "tier3_dual_association_search_performed": "not_applicable",
            "tier3_entity_name_registry_search_performed": "not_applicable",
            "_searched_queries": ["county assessor for sale records"],
        }
        fixed = otc._enforce_functional_ownership_guardrail({}, "COA", result)
        self.assertEqual(fixed["determined_type"], "APT")
        self.assertEqual(fixed["decision"], "Override")
        self.assertTrue(fixed["functional_apt_override_used"])
        self.assertFalse(fixed["reverse_conversion_used"])

    def test_rule_a_with_reverse_conversion_is_tracked_separately(self):
        result = {
            "decision": "Confirmed",
            "determined_type": "HOA",
            "reasoning": "Historical individual sales, but county records now show one owner.",
            "ownership_concentration": "single_owner_full_bulk",
            "reverse_conversion_detected": "yes",
            "multi_name_all_agree": "not_applicable",
            "tier3_sales_listing_search_performed": "yes",
            "tier3_sales_evidence_found": "no",
            "ownership_concentration_verified_externally": "yes",
            "ownership_concentration_contradicting_evidence": "not_applicable",
            "tier3_dual_association_search_performed": "not_applicable",
            "tier3_entity_name_registry_search_performed": "not_applicable",
            "_searched_queries": ["county assessor for sale records"],
        }
        fixed = otc._enforce_functional_ownership_guardrail({}, "HOA", result)
        self.assertEqual(fixed["determined_type"], "APT")
        self.assertTrue(fixed["functional_apt_override_used"])
        self.assertTrue(fixed["reverse_conversion_used"])

    def test_rule_a_already_correct_is_left_alone_but_still_tracked(self):
        # The model already got it right on its own -- no correction needed, but this is still
        # the "Legally Condo, Functionally Apartment" pattern and should still be tracked.
        result = {
            "decision": "Override",
            "determined_type": "APT",
            "confidence": "High",
            "reasoning": "100% single-owned, no individual sales.",
            "ownership_concentration": "single_owner_full_bulk",
            "reverse_conversion_detected": "no",
            "multi_name_all_agree": "not_applicable",
            "tier3_sales_listing_search_performed": "yes",
            "tier3_sales_evidence_found": "no",
            "ownership_concentration_verified_externally": "yes",
            "ownership_concentration_contradicting_evidence": "no",
            "tier3_dual_association_search_performed": "not_applicable",
            "tier3_entity_name_registry_search_performed": "not_applicable",
            "_searched_queries": ["county assessor for sale records"],
        }
        fixed = otc._enforce_functional_ownership_guardrail({}, "HOA", result)
        self.assertEqual(fixed["determined_type"], "APT")
        self.assertTrue(fixed["functional_apt_override_used"])
        self.assertFalse(fixed["reverse_conversion_used"])

    def test_rule_a_is_a_no_op_when_db_already_apt(self):
        # No legal-condo tension to resolve if the DB already says APT -- nothing to track.
        result = {
            "decision": "Confirmed",
            "determined_type": "APT",
            "reasoning": "Single-owned APT, no association.",
            "ownership_concentration": "single_owner_full_bulk",
            "reverse_conversion_detected": "not_applicable",
            "multi_name_all_agree": "not_applicable",
            "tier3_sales_listing_search_performed": "not_applicable",
            "tier3_sales_evidence_found": "not_applicable",
            "ownership_concentration_verified_externally": "not_applicable",
            "ownership_concentration_contradicting_evidence": "not_applicable",
            "tier3_dual_association_search_performed": "not_applicable",
            "tier3_entity_name_registry_search_performed": "not_applicable",
        }
        fixed = otc._enforce_functional_ownership_guardrail({}, "APT", result)
        self.assertEqual(fixed["determined_type"], "APT")
        self.assertFalse(fixed["functional_apt_override_used"])

    def test_rule_b_forces_away_from_apt_back_to_db_label(self):
        result = {
            "decision": "Override",
            "determined_type": "APT",
            "reasoning": "Mostly bulk-owned by one investor, but one unit was individually sold.",
            "ownership_concentration": "individual_owner_present",
            "reverse_conversion_detected": "not_applicable",
            "multi_name_all_agree": "not_applicable",
            "tier3_sales_listing_search_performed": "not_applicable",
            "tier3_sales_evidence_found": "not_applicable",
            "ownership_concentration_verified_externally": "not_applicable",
            "ownership_concentration_contradicting_evidence": "not_applicable",
            "tier3_dual_association_search_performed": "not_applicable",
            "tier3_entity_name_registry_search_performed": "not_applicable",
        }
        fixed = otc._enforce_functional_ownership_guardrail({}, "COA", result)
        self.assertEqual(fixed["determined_type"], "COA")
        self.assertEqual(fixed["decision"], "Confirmed")

    def test_rule_b_with_db_already_apt_falls_back_to_not_enough_info(self):
        # Self-contradictory model output (db is APT, yet an individual owner was found) --
        # can't tell whether it should be COA or HOA, so fall back to the safe default rather
        # than guess.
        result = {
            "decision": "Confirmed",
            "determined_type": "APT",
            "reasoning": "One unit found individually owned.",
            "ownership_concentration": "individual_owner_present",
            "reverse_conversion_detected": "not_applicable",
            "multi_name_all_agree": "not_applicable",
            "tier3_sales_listing_search_performed": "not_applicable",
            "tier3_sales_evidence_found": "not_applicable",
            "ownership_concentration_verified_externally": "not_applicable",
            "ownership_concentration_contradicting_evidence": "not_applicable",
            "tier3_dual_association_search_performed": "not_applicable",
            "tier3_entity_name_registry_search_performed": "not_applicable",
        }
        fixed = otc._enforce_functional_ownership_guardrail({}, "APT", result)
        self.assertEqual(fixed["determined_type"], "APT")
        self.assertEqual(fixed["decision"], "Not Enough Info")

    def test_rule_b_does_not_disturb_a_legitimate_coa_to_hoa_relabel(self):
        # determined_type isn't APT here at all -- this guardrail has nothing to do with an
        # ordinary COA<->HOA relabeling and must not interfere.
        result = {
            "decision": "Override",
            "determined_type": "HOA",
            "reasoning": "Individually owned units, governed as an HOA not a COA.",
            "ownership_concentration": "individual_owner_present",
            "reverse_conversion_detected": "not_applicable",
            "multi_name_all_agree": "not_applicable",
            "tier3_sales_listing_search_performed": "not_applicable",
            "tier3_sales_evidence_found": "not_applicable",
            "ownership_concentration_verified_externally": "not_applicable",
            "ownership_concentration_contradicting_evidence": "not_applicable",
            "tier3_dual_association_search_performed": "not_applicable",
            "tier3_entity_name_registry_search_performed": "not_applicable",
        }
        fixed = otc._enforce_functional_ownership_guardrail({}, "COA", result)
        self.assertEqual(fixed["determined_type"], "HOA")
        self.assertEqual(fixed["decision"], "Override")

    def test_not_applicable_concentration_is_a_no_op(self):
        result = {
            "decision": "Confirmed",
            "determined_type": "COA",
            "reasoning": "Ordinary case.",
            "ownership_concentration": "not_applicable",
            "reverse_conversion_detected": "not_applicable",
            "multi_name_all_agree": "not_applicable",
            "tier3_sales_listing_search_performed": "not_applicable",
            "tier3_sales_evidence_found": "not_applicable",
            "ownership_concentration_verified_externally": "not_applicable",
            "ownership_concentration_contradicting_evidence": "not_applicable",
            "tier3_dual_association_search_performed": "not_applicable",
            "tier3_entity_name_registry_search_performed": "not_applicable",
        }
        fixed = otc._enforce_functional_ownership_guardrail({}, "COA", result)
        self.assertEqual(fixed["determined_type"], "COA")
        self.assertEqual(fixed["decision"], "Confirmed")

    def test_structural_edge_case_takes_precedence_over_rule_a(self):
        # A housing co-op that happens to be 100% single-owned must still never be overridden --
        # structural edge cases (never overridden, per §5.7) take precedence over §2.1.
        result = {
            "decision": "Confirmed",
            "determined_type": "COA",
            "reasoning": "This is a housing cooperative.",
            "structural_edge_case": "housing_cooperative",
            "ownership_concentration": "single_owner_full_bulk",
            "reverse_conversion_detected": "not_applicable",
            "multi_name_all_agree": "not_applicable",
            "tier3_sales_listing_search_performed": "not_applicable",
            "tier3_sales_evidence_found": "not_applicable",
            "ownership_concentration_verified_externally": "not_applicable",
            "ownership_concentration_contradicting_evidence": "not_applicable",
            "tier3_dual_association_search_performed": "not_applicable",
            "tier3_entity_name_registry_search_performed": "not_applicable",
        }
        fixed = otc._enforce_functional_ownership_guardrail({}, "COA", result)
        self.assertEqual(fixed["determined_type"], "COA")
        self.assertEqual(fixed["decision"], "Confirmed")
        self.assertNotIn("functional_apt_override_used", fixed)

    def test_coop_mention_in_reasoning_takes_precedence_over_rule_a(self):
        # Same protection, but via the free-text co-op-mention backstop rather than the
        # structured structural_edge_case field.
        result = {
            "decision": "Confirmed",
            "determined_type": "COA",
            "reasoning": "This may be a cooperative, though not fully confirmed.",
            "ownership_concentration": "single_owner_full_bulk",
            "reverse_conversion_detected": "not_applicable",
            "multi_name_all_agree": "not_applicable",
            "tier3_sales_listing_search_performed": "not_applicable",
            "tier3_sales_evidence_found": "not_applicable",
            "ownership_concentration_verified_externally": "not_applicable",
            "ownership_concentration_contradicting_evidence": "not_applicable",
            "tier3_dual_association_search_performed": "not_applicable",
            "tier3_entity_name_registry_search_performed": "not_applicable",
        }
        fixed = otc._enforce_functional_ownership_guardrail({}, "COA", result)
        self.assertEqual(fixed["determined_type"], "COA")
        self.assertNotIn("functional_apt_override_used", fixed)

    def test_real_populated_fee_unconditionally_blocks_rule_a(self):
        # Real reported failure ("Paradise Gardens One"): the model's own reasoning stated "a
        # registered HOA exists" and "ownership is bulk-held, but not enough for override," yet
        # ownership_concentration was still set to single_owner_full_bulk and the override stood.
        # A real, populated fee is a hard, code-only block -- it doesn't depend on any
        # self-reported field and can't be talked around.
        row = {"Master_Monthly Association Fees": "70"}
        result = {
            "decision": "Override",
            "determined_type": "APT",
            "reasoning": "Conflicting evidence: a registered HOA exists, and a Tier 3 source shows a development with HOA fees, but assessor data contradicts the residential structure. Ownership is bulk-held, but not enough for override.",
            "ownership_concentration": "single_owner_full_bulk",
            "reverse_conversion_detected": "not_applicable",
            "tier3_sales_listing_search_performed": "yes",
            "ownership_concentration_verified_externally": "yes",
            "ownership_concentration_contradicting_evidence": "no",
            "tier3_dual_association_search_performed": "not_applicable",
            "tier3_entity_name_registry_search_performed": "not_applicable",
        }
        fixed = otc._enforce_functional_ownership_guardrail(row, "HOA", result)
        self.assertEqual(fixed["decision"], "Not Enough Info")
        self.assertEqual(fixed["determined_type"], "HOA")

    def test_zero_or_blank_fee_does_not_block_rule_a(self):
        row = {"Master_Monthly Association Fees": ""}
        result = {
            "decision": "Confirmed",
            "determined_type": "COA",
            "reasoning": "100% single-owned.",
            "ownership_concentration": "single_owner_full_bulk",
            "reverse_conversion_detected": "no",
            "tier3_sales_listing_search_performed": "yes",
            "ownership_concentration_verified_externally": "yes",
            "ownership_concentration_contradicting_evidence": "no",
            "tier3_dual_association_search_performed": "not_applicable",
            "tier3_entity_name_registry_search_performed": "not_applicable",
            "_searched_queries": ["county assessor for sale records"],
        }
        fixed = otc._enforce_functional_ownership_guardrail(row, "COA", result)
        self.assertEqual(fixed["determined_type"], "APT")
        self.assertEqual(fixed["decision"], "Override")

    def test_db_owner_field_citation_is_not_external_verification(self):
        # Real reported failure ("The Falls of Portofino"): reasoning cited "DB 'Owner' is Prime
        # Group, satisfying the criteria for functional override to APT" -- the DB's own field is
        # not external verification, and ownership_concentration_verified_externally must be
        # explicitly 'yes' (never left at its default) for Rule A to apply.
        row = {}
        result = {
            "decision": "Override",
            "determined_type": "APT",
            "reasoning": "DB 'Owner' is Prime Group, satisfying the criteria for functional override to APT.",
            "ownership_concentration": "single_owner_full_bulk",
            "reverse_conversion_detected": "not_applicable",
            "tier3_sales_listing_search_performed": "yes",
            "ownership_concentration_verified_externally": "no",
            "ownership_concentration_contradicting_evidence": "no",
            "tier3_dual_association_search_performed": "not_applicable",
            "tier3_entity_name_registry_search_performed": "not_applicable",
        }
        fixed = otc._enforce_functional_ownership_guardrail(row, "HOA", result)
        self.assertEqual(fixed["decision"], "Not Enough Info")
        self.assertEqual(fixed["determined_type"], "HOA")

    def test_contradicting_evidence_blocks_rule_a(self):
        row = {}
        result = {
            "decision": "Override",
            "determined_type": "APT",
            "reasoning": "Bulk-owned, but a registered HOA entity was also found.",
            "ownership_concentration": "single_owner_full_bulk",
            "reverse_conversion_detected": "not_applicable",
            "tier3_sales_listing_search_performed": "yes",
            "ownership_concentration_verified_externally": "yes",
            "ownership_concentration_contradicting_evidence": "yes",
            "tier3_dual_association_search_performed": "not_applicable",
            "tier3_entity_name_registry_search_performed": "not_applicable",
        }
        fixed = otc._enforce_functional_ownership_guardrail(row, "HOA", result)
        self.assertEqual(fixed["decision"], "Not Enough Info")
        self.assertEqual(fixed["determined_type"], "HOA")

    def test_rule_a_without_sales_listing_search_is_blocked(self):
        row = {}
        result = {
            "decision": "Override",
            "determined_type": "APT",
            "reasoning": "Bulk-owned, no individual listings found.",
            "ownership_concentration": "single_owner_full_bulk",
            "reverse_conversion_detected": "not_applicable",
            "tier3_sales_listing_search_performed": "no",
            "ownership_concentration_verified_externally": "yes",
            "ownership_concentration_contradicting_evidence": "no",
            "tier3_dual_association_search_performed": "not_applicable",
            "tier3_entity_name_registry_search_performed": "not_applicable",
        }
        fixed = otc._enforce_functional_ownership_guardrail(row, "HOA", result)
        self.assertEqual(fixed["decision"], "Not Enough Info")

    def test_rule_a_gate_failure_does_not_disturb_a_decision_that_was_never_override(self):
        # If the model's own decision already wasn't Override/APT (e.g. it agreed with its own
        # "not enough for override" finding), a failing gate must not force anything -- there's
        # nothing to downgrade.
        row = {}
        result = {
            "decision": "Confirmed",
            "determined_type": "HOA",
            "reasoning": "Bulk-held, but not enough for override.",
            "ownership_concentration": "single_owner_full_bulk",
            "reverse_conversion_detected": "not_applicable",
            "tier3_sales_listing_search_performed": "no",
            "ownership_concentration_verified_externally": "no",
            "ownership_concentration_contradicting_evidence": "not_applicable",
            "tier3_dual_association_search_performed": "not_applicable",
            "tier3_entity_name_registry_search_performed": "not_applicable",
        }
        fixed = otc._enforce_functional_ownership_guardrail(row, "HOA", result)
        self.assertEqual(fixed["decision"], "Confirmed")
        self.assertEqual(fixed["determined_type"], "HOA")


class FunctionalOwnershipIntegrationTests(unittest.TestCase):
    """End-to-end through process_property() -- Rule A and Rule B as a real batch run would
    exercise them, including the required ownership_concentration validation."""

    def test_fully_bulk_owned_condo_building_overrides_to_apt(self):
        row = {
            "RecordID": "555001",
            "Master_Property Name": "Lakeview Condominiums",
            "Master_Ownership Type": "COA",
            "Master_Monthly Association Fees": "0",
        }
        fake_result = {
            "determined_type": "COA",
            "decision": "Confirmed",
            "confidence": "High",
            "evidence_tier_used": "Tier 2",
            "reasoning": (
                "County parcel records show all 40 units held by a single LLC, one centralized "
                "leasing office manages the whole building, and no unit is individually listed "
                "or sold."
            ),
            "sources": ["https://county-assessor.example.gov/parcel/555001"],
            "structural_edge_case": "none",
            "tier3_exception_invoked": "no",
            "tier3_exception_direction": "not_applicable",
            "tier3_reverse_attempt2_exhausted": "not_applicable",
            "tier3_independent_source_count": 0,
            "tier3_contradicting_evidence": "not_applicable",
            "tier3_internal_db_corroboration": "",
            "tier3_structural_edge_case_ruled_out": "not_applicable",
            "ownership_concentration": "single_owner_full_bulk",
            "reverse_conversion_detected": "no",
            "multi_name_all_agree": "not_applicable",
            "tier3_sales_listing_search_performed": "yes",
            "tier3_sales_evidence_found": "no",
            "ownership_concentration_verified_externally": "yes",
            "ownership_concentration_contradicting_evidence": "no",
            "tier3_dual_association_search_performed": "not_applicable",
            "tier3_entity_name_registry_search_performed": "not_applicable",
            "_searched_queries": ["Lakeview Condominiums units for sale"],
        }
        with mock.patch("ownership_type_checking.research_property", return_value=fake_result), \
             mock.patch("ownership_type_checking.fetch_url_cached", return_value=None):
            result = otc.process_property(None, "gpt-4o", row, {})
        self.assertEqual(result["decision"], "Override")
        self.assertEqual(result["determined_type"], "APT")
        self.assertEqual(result["decision_display"], "Changed from COA to APT")
        self.assertTrue(result["functional_apt_override_used"])

    def test_one_individually_owned_unit_keeps_coa_despite_bulk_ownership(self):
        row = {
            "RecordID": "555002",
            "Master_Property Name": "Lakeview Condominiums",
            "Master_Ownership Type": "COA",
            "Master_Monthly Association Fees": "210",
        }
        fake_result = {
            "determined_type": "APT",
            "decision": "Override",
            "confidence": "High",
            "evidence_tier_used": "Tier 2",
            "reasoning": (
                "39 of 40 units are held by a single investor, but county records show unit "
                "12B individually owned and occupied by its owner."
            ),
            "sources": ["https://county-assessor.example.gov/parcel/555002"],
            "structural_edge_case": "none",
            "tier3_exception_invoked": "no",
            "tier3_exception_direction": "not_applicable",
            "tier3_reverse_attempt2_exhausted": "not_applicable",
            "tier3_independent_source_count": 0,
            "tier3_contradicting_evidence": "not_applicable",
            "tier3_internal_db_corroboration": "",
            "tier3_structural_edge_case_ruled_out": "not_applicable",
            "ownership_concentration": "individual_owner_present",
            "reverse_conversion_detected": "not_applicable",
            "multi_name_all_agree": "not_applicable",
            "tier3_sales_listing_search_performed": "not_applicable",
            "tier3_sales_evidence_found": "not_applicable",
            "ownership_concentration_verified_externally": "not_applicable",
            "ownership_concentration_contradicting_evidence": "not_applicable",
            "tier3_dual_association_search_performed": "not_applicable",
            "tier3_entity_name_registry_search_performed": "not_applicable",
        }
        with mock.patch("ownership_type_checking.research_property", return_value=fake_result), \
             mock.patch("ownership_type_checking.fetch_url_cached", return_value=None):
            result = otc.process_property(None, "gpt-4o", row, {})
        self.assertEqual(result["decision"], "Confirmed")
        self.assertEqual(result["determined_type"], "COA")
        self.assertFalse(result["functional_apt_override_used"])

    def test_paradise_gardens_one_end_to_end(self):
        # Full regression, mirroring the exact reported failure: a real $70/month fee on file,
        # and the model's own reasoning stating a registered HOA exists and "ownership is
        # bulk-held, but not enough for override" -- yet ownership_concentration was still set to
        # single_owner_full_bulk. The fee-based backstop alone must block this.
        row = {
            "RecordID": "19392336",
            "Master_Property Name": "Paradise Gardens One",
            "Master_Ownership Type": "HOA",
            "Master_Monthly Association Fees": "70",
        }
        fake_result = {
            "determined_type": "APT",
            "decision": "Override",
            "confidence": "Low",
            "evidence_tier_used": "Mixed",
            "reasoning": (
                "Conflicting evidence: a registered HOA exists, and a Tier 3 source shows a "
                "development with HOA fees, but assessor data contradicts the residential "
                "structure. Ownership is bulk-held, but not enough for override."
            ),
            "sources": ["https://www.redfin.com/x", "https://search.sunbiz.org/x"],
            "structural_edge_case": "none",
            "tier3_exception_invoked": "no",
            "tier3_exception_direction": "not_applicable",
            "tier3_reverse_attempt2_exhausted": "not_applicable",
            "tier3_independent_source_count": 0,
            "tier3_contradicting_evidence": "not_applicable",
            "tier3_internal_db_corroboration": "",
            "tier3_structural_edge_case_ruled_out": "not_applicable",
            "ownership_concentration": "single_owner_full_bulk",
            "reverse_conversion_detected": "not_applicable",
            "multi_name_all_agree": "not_applicable",
            "tier3_sales_listing_search_performed": "yes",
            "tier3_sales_evidence_found": "no",
            "ownership_concentration_verified_externally": "yes",
            "ownership_concentration_contradicting_evidence": "no",
            "tier3_dual_association_search_performed": "not_applicable",
            "tier3_entity_name_registry_search_performed": "not_applicable",
        }
        with mock.patch("ownership_type_checking.research_property", return_value=fake_result), \
             mock.patch("ownership_type_checking.fetch_url_cached", return_value=None):
            result = otc.process_property(None, "gpt-4o", row, {})
        self.assertEqual(result["decision"], "Not Enough Info")
        self.assertEqual(result["determined_type"], "HOA")
        self.assertEqual(result["decision_display"], "Confirmed")

    def test_falls_of_portofino_end_to_end(self):
        # Full regression: reasoning cites the DB's own Owner field as if it were external
        # verification of 100% single ownership -- ownership_concentration_verified_externally
        # must be explicitly 'yes' and is not, so Rule A cannot apply.
        row = {
            "RecordID": "85213875",
            "Master_Property Name": "The Falls of Portofino",
            "Master_Ownership Type": "HOA",
            "Master_Monthly Association Fees": "",
            "Owner": "Prime Group",
        }
        fake_result = {
            "determined_type": "APT",
            "decision": "Override",
            "confidence": "High",
            "evidence_tier_used": "Tier 1",
            "reasoning": (
                "This property is legally structured as an HOA but operates as a single-owner, "
                "centrally managed rental community with no units individually owned or listed. "
                "DB 'Owner' is Prime Group, satisfying the criteria for functional override to APT."
            ),
            "sources": ["https://www.realtor.com/x", "https://www.apartments.com/x"],
            "structural_edge_case": "none",
            "tier3_exception_invoked": "no",
            "tier3_exception_direction": "not_applicable",
            "tier3_reverse_attempt2_exhausted": "not_applicable",
            "tier3_independent_source_count": 0,
            "tier3_contradicting_evidence": "not_applicable",
            "tier3_internal_db_corroboration": "",
            "tier3_structural_edge_case_ruled_out": "not_applicable",
            "ownership_concentration": "single_owner_full_bulk",
            "reverse_conversion_detected": "not_applicable",
            "multi_name_all_agree": "not_applicable",
            "tier3_sales_listing_search_performed": "yes",
            "tier3_sales_evidence_found": "no",
            "ownership_concentration_verified_externally": "no",
            "ownership_concentration_contradicting_evidence": "no",
            "tier3_dual_association_search_performed": "not_applicable",
            "tier3_entity_name_registry_search_performed": "not_applicable",
        }
        with mock.patch("ownership_type_checking.research_property", return_value=fake_result), \
             mock.patch("ownership_type_checking.fetch_url_cached", return_value=None):
            result = otc.process_property(None, "gpt-4o", row, {})
        self.assertEqual(result["decision"], "Not Enough Info")
        self.assertEqual(result["determined_type"], "HOA")

    def test_medley_johns_creek_end_to_end(self):
        # Full regression: the model's DB-Owner-derived corroboration ("owned by Ascentris, LLC")
        # is no longer valid condition-3 evidence for the forward Tier-3 exception.
        row = {
            "RecordID": "24959331",
            "Master_Property Name": "Medley Johns Creek",
            "Master_Ownership Type": "HOA",
            "Master_Monthly Association Fees": "",
            "Owner": "Ascentris, LLC",
        }
        fake_result = {
            "determined_type": "APT",
            "decision": "Override",
            "confidence": "Medium",
            "evidence_tier_used": "Tier 3",
            "reasoning": (
                "Medley Johns Creek is functionally an apartment complex with centralized "
                "leasing, owned by Ascentris, LLC. No HOA governance evidence exists, and the DB "
                "shows no association fee. Multiple Tier 3 sources confirm rental nature, meeting "
                "all conditions for the Tier-3 exception."
            ),
            "sources": [
                "https://johnscreekga.gov/news/x",
                "https://www.ajc.com/business/x",
                "https://apartments.com/x",
            ],
            "structural_edge_case": "none",
            "tier3_exception_invoked": "yes",
            "tier3_exception_direction": "to_apt",
            "tier3_reverse_attempt2_exhausted": "not_applicable",
            "tier3_independent_source_count": 3,
            "tier3_name_address_anchor_confirmed": "yes",
            "tier3_partial_tier12_support": "not_applicable",
            "tier3_contradicting_evidence": "no",
            "tier3_internal_db_corroboration": "Owner is Ascentris, LLC with no per-unit variation",
            "tier3_structural_edge_case_ruled_out": "yes",
            "ownership_concentration": "not_applicable",
            "reverse_conversion_detected": "not_applicable",
            "multi_name_all_agree": "not_applicable",
            "tier3_sales_listing_search_performed": "yes",
            "tier3_sales_evidence_found": "no",
            "ownership_concentration_verified_externally": "not_applicable",
            "ownership_concentration_contradicting_evidence": "not_applicable",
            "tier3_dual_association_search_performed": "not_applicable",
            "tier3_entity_name_registry_search_performed": "not_applicable",
        }
        with mock.patch("ownership_type_checking.research_property", return_value=fake_result), \
             mock.patch("ownership_type_checking.fetch_url_cached", return_value=None):
            result = otc.process_property(None, "gpt-4o", row, {})
        self.assertEqual(result["decision"], "Not Enough Info")
        self.assertEqual(result["determined_type"], "HOA")

    def test_missing_ownership_concentration_field_is_a_validation_error(self):
        row = {"RecordID": "555003", "Master_Ownership Type": "COA"}
        fake_result = {
            "determined_type": "COA",
            "decision": "Confirmed",
            "confidence": "High",
            "evidence_tier_used": "None",
            "reasoning": "No research done.",
            "sources": [],
            "structural_edge_case": "none",
            "tier3_exception_invoked": "no",
            "tier3_exception_direction": "not_applicable",
            "tier3_reverse_attempt2_exhausted": "not_applicable",
            "tier3_independent_source_count": 0,
            "tier3_contradicting_evidence": "not_applicable",
            "tier3_internal_db_corroboration": "",
            "tier3_structural_edge_case_ruled_out": "not_applicable",
            "reverse_conversion_detected": "not_applicable",
            "multi_name_all_agree": "not_applicable",
            "tier3_sales_listing_search_performed": "not_applicable",
            "tier3_sales_evidence_found": "not_applicable",
            "ownership_concentration_verified_externally": "not_applicable",
            "ownership_concentration_contradicting_evidence": "not_applicable",
            "tier3_dual_association_search_performed": "not_applicable",
            "tier3_entity_name_registry_search_performed": "not_applicable",
            # ownership_concentration deliberately omitted
        }
        with mock.patch("ownership_type_checking.research_property", return_value=fake_result), \
             mock.patch("ownership_type_checking.fetch_url_cached", return_value=None):
            result = otc.process_property(None, "gpt-4o", row, {})
        self.assertEqual(result["decision"], "Not Enough Info")
        self.assertTrue(result["is_error"])


class MasterPlannedCommunityGuardrailTests(unittest.TestCase):
    """A structural-edge-case backstop that doesn't rely on the model's self-report: it scans
    the model's own reasoning, and independently re-fetches cited source URLs, for "master
    planned community" phrasing or explicit for-rent-and-for-sale housing language -- the real
    Baumgardner Ranch failure, where the cited source said exactly this but the model's summary
    never engaged with it."""

    def _override(self, **overrides):
        result = {
            "decision": "Override",
            "determined_type": "APT",
            "reasoning": "Marketed as a rental apartment community with no HOA evidences.",
            "sources": ["https://integratedcommunitydevelopment.com/baumgardner/"],
        }
        result.update(overrides)
        return result

    def test_master_planned_phrase_in_reasoning_blocks_override(self):
        row = {"Master_Property Name": "Baumgardner Ranch"}
        result = self._override(
            reasoning="This is a master planned community with a single leasing office."
        )
        with mock.patch("ownership_type_checking.fetch_url_cached", return_value=None) as fetch:
            fixed = otc._enforce_master_planned_community_guardrail(row, "HOA", result, {})
        fetch.assert_not_called()  # already caught from reasoning text -- no need to fetch
        self.assertEqual(fixed["decision"], "Confirmed")
        self.assertEqual(fixed["determined_type"], "HOA")
        self.assertTrue(fixed["master_planned_override_blocked"])

    def test_for_rent_and_for_sale_phrase_in_reasoning_blocks_override(self):
        row = {"Master_Property Name": "Baumgardner Ranch"}
        result = self._override(reasoning="The community offers homes for rent and for sale.")
        fixed = otc._enforce_master_planned_community_guardrail(row, "HOA", result, {})
        self.assertEqual(fixed["decision"], "Confirmed")
        self.assertEqual(fixed["determined_type"], "HOA")

    def test_master_planned_phrase_only_in_cited_source_still_blocks_override(self):
        # The real Baumgardner Ranch failure: the model's own reasoning never mentions it, but
        # the cited source page does. Re-fetching sources.get() must catch this.
        row = {"Master_Property Name": "Baumgardner Ranch"}
        result = self._override()
        fetched_text = (
            "It is ICD's goal to provide a multitude of high quality housing options to meet "
            "the needs of the community including for rent and for sale homes. Baumgardner "
            "Ranch is a master planned community in Cloverdale, CA."
        )
        with mock.patch("ownership_type_checking.fetch_url_cached", return_value=fetched_text):
            fixed = otc._enforce_master_planned_community_guardrail(row, "HOA", result, {})
        self.assertEqual(fixed["decision"], "Confirmed")
        self.assertEqual(fixed["determined_type"], "HOA")
        self.assertTrue(fixed["master_planned_override_blocked"])

    def test_clean_source_content_leaves_override_alone(self):
        row = {"Master_Property Name": "Cross Creek Apartments"}
        result = self._override()
        fetched_text = "Cross Creek Apartments -- apply now, floor plans, leasing office on site."
        with mock.patch("ownership_type_checking.fetch_url_cached", return_value=fetched_text):
            fixed = otc._enforce_master_planned_community_guardrail(row, "HOA", result, {})
        self.assertEqual(fixed["decision"], "Override")
        self.assertEqual(fixed["determined_type"], "APT")
        self.assertFalse(fixed["master_planned_override_blocked"])

    def test_unfetchable_source_fails_soft_and_leaves_override_alone(self):
        # fetch_url_cached() already fails soft (returns None) on network errors -- this
        # guardrail must not treat that as a match, and must not crash.
        row = {"Master_Property Name": "Cross Creek Apartments"}
        result = self._override()
        with mock.patch("ownership_type_checking.fetch_url_cached", return_value=None):
            fixed = otc._enforce_master_planned_community_guardrail(row, "HOA", result, {})
        self.assertEqual(fixed["decision"], "Override")

    def test_confirmed_decision_is_left_alone_without_fetching(self):
        row = {"Master_Property Name": "Baumgardner Ranch"}
        result = self._override(decision="Confirmed", determined_type="HOA")
        with mock.patch("ownership_type_checking.fetch_url_cached", return_value=None) as fetch:
            fixed = otc._enforce_master_planned_community_guardrail(row, "HOA", result, {})
        fetch.assert_not_called()
        self.assertEqual(fixed["decision"], "Confirmed")

    def test_baumgardner_ranch_end_to_end(self):
        # Full regression, mocking research_property() and fetch_url_cached() together, mirroring
        # the real reported failure exactly.
        row = {
            "RecordID": "46822730",
            "Master_Property Name": "Baumgardner Ranch",
            "Master_Ownership Type": "HOA",
            "Master_Monthly Association Fees": "",
        }
        fake_result = {
            "determined_type": "APT",
            "decision": "Override",
            "confidence": "Medium",
            "evidence_tier_used": "Tier 3",
            "reasoning": (
                "Tier-3 corroborated override: Baumgardner Ranch is marketed as a rental "
                "apartment community with no HOA evidences. Development is by a single entity, "
                "indicating functionally as APT."
            ),
            "sources": [
                "https://integratedcommunitydevelopment.com/baumgardner/",
                "https://www.apartmenthomeliving.com/apartment-finder/Baumgardner-Ranch",
                "https://www.apartments.com/baumgardner-ranch",
            ],
            "structural_edge_case": "none",
            "tier3_exception_invoked": "yes",
            "tier3_exception_direction": "to_apt",
            "tier3_reverse_attempt2_exhausted": "not_applicable",
            "tier3_independent_source_count": 3,
            "tier3_name_address_anchor_confirmed": "yes",
            "tier3_partial_tier12_support": "not_applicable",
            "tier3_contradicting_evidence": "no",
            "tier3_internal_db_corroboration": "Master_Monthly Association Fees is null",
            "tier3_structural_edge_case_ruled_out": "yes",
            "ownership_concentration": "not_applicable",
            "reverse_conversion_detected": "not_applicable",
            "multi_name_all_agree": "not_applicable",
            "tier3_sales_listing_search_performed": "yes",
            "tier3_sales_evidence_found": "no",
            "ownership_concentration_verified_externally": "not_applicable",
            "ownership_concentration_contradicting_evidence": "not_applicable",
            "tier3_dual_association_search_performed": "yes",
            "tier3_entity_name_registry_search_performed": "not_applicable",
            "_searched_queries": ["Baumgardner Ranch units for sale"],
        }
        fetched_text = (
            "It is ICD's goal to provide a multitude of high quality housing options to meet "
            "the needs of the community including for rent and for sale homes. Baumgardner "
            "Ranch is a master planned community."
        )
        with mock.patch("ownership_type_checking.research_property", return_value=fake_result), \
             mock.patch("ownership_type_checking.fetch_url_cached", return_value=fetched_text):
            result = otc.process_property(None, "gpt-4o", row, {})
        self.assertEqual(result["decision"], "Confirmed")
        self.assertEqual(result["determined_type"], "HOA")
        self.assertEqual(result["decision_display"], "Confirmed")
        self.assertTrue(result["master_planned_override_blocked"])


class MultiNameGuardrailTests(unittest.TestCase):
    """Comma-separated combined-name records (e.g. "White Oak Villas, South Cottage Village")
    need independent, agreeing evidence for EVERY sub-name before an override is allowed to
    stand."""

    def _override(self, **overrides):
        result = {
            "decision": "Override",
            "determined_type": "APT",
            "reasoning": "Both sub-names confirmed as apartments.",
            "multi_name_all_agree": "yes",
            "tier3_sales_listing_search_performed": "not_applicable",
            "tier3_sales_evidence_found": "not_applicable",
            "ownership_concentration_verified_externally": "not_applicable",
            "ownership_concentration_contradicting_evidence": "not_applicable",
            "tier3_dual_association_search_performed": "not_applicable",
            "tier3_entity_name_registry_search_performed": "not_applicable",
        }
        result.update(overrides)
        return result

    def test_single_name_property_is_a_no_op(self):
        row = {"Master_Property Name": "Cross Creek Apartments"}
        result = self._override(multi_name_all_agree="no")
        fixed = otc._enforce_multi_name_guardrail(row, "HOA", result)
        self.assertEqual(fixed["decision"], "Override")

    def test_multi_name_all_agree_yes_allows_override(self):
        row = {"Master_Property Name": "White Oak Villas, South Cottage Village"}
        result = self._override(multi_name_all_agree="yes")
        fixed = otc._enforce_multi_name_guardrail(row, "HOA", result)
        self.assertEqual(fixed["decision"], "Override")
        self.assertEqual(fixed["determined_type"], "APT")

    def test_multi_name_disagreement_blocks_override(self):
        row = {"Master_Property Name": "White Oak Villas, South Cottage Village"}
        result = self._override(
            multi_name_all_agree="no",
            reasoning="White Oak Villas confirmed apartments, but South Cottage Village is a genuine HOA.",
        )
        fixed = otc._enforce_multi_name_guardrail(row, "HOA", result)
        self.assertEqual(fixed["decision"], "Not Enough Info")
        self.assertEqual(fixed["determined_type"], "HOA")
        self.assertTrue(fixed["multi_name_blocked"])

    def test_multi_name_not_applicable_blocks_override(self):
        # The model failed to even recognize this as a multi-name record (left the field at its
        # default) -- must still be blocked, since agreement was never actually confirmed.
        row = {"Master_Property Name": "White Oak Villas, South Cottage Village"}
        result = self._override(multi_name_all_agree="not_applicable")
        fixed = otc._enforce_multi_name_guardrail(row, "HOA", result)
        self.assertEqual(fixed["decision"], "Not Enough Info")

    def test_confirmed_decision_is_left_alone(self):
        row = {"Master_Property Name": "White Oak Villas, South Cottage Village"}
        result = self._override(decision="Confirmed", determined_type="HOA", multi_name_all_agree="no")
        fixed = otc._enforce_multi_name_guardrail(row, "HOA", result)
        self.assertEqual(fixed["decision"], "Confirmed")

    def test_three_way_combined_name_also_requires_agreement(self):
        row = {"Master_Property Name": "Oak Villas, Cottage Village, Pine Terrace"}
        result = self._override(multi_name_all_agree="no")
        fixed = otc._enforce_multi_name_guardrail(row, "HOA", result)
        self.assertEqual(fixed["decision"], "Not Enough Info")

    def test_white_oak_villas_end_to_end_disagreement_keeps_hoa(self):
        row = {
            "RecordID": "700100",
            "Master_Property Name": "White Oak Villas, South Cottage Village",
            "Master_Ownership Type": "HOA",
            "Master_Monthly Association Fees": "175",
        }
        fake_result = {
            "determined_type": "APT",
            "decision": "Override",
            "confidence": "High",
            "evidence_tier_used": "Tier 2",
            "reasoning": (
                "White Oak Villas confirmed as a single-owner apartment complex via county "
                "parcel records; South Cottage Village nearby is a legally distinct, individually "
                "owned HOA and was not confirmed as apartments."
            ),
            "sources": ["https://county-assessor.example.gov/parcel/700100"],
            "structural_edge_case": "none",
            "tier3_exception_invoked": "no",
            "tier3_exception_direction": "not_applicable",
            "tier3_reverse_attempt2_exhausted": "not_applicable",
            "tier3_independent_source_count": 0,
            "tier3_contradicting_evidence": "not_applicable",
            "tier3_internal_db_corroboration": "",
            "tier3_structural_edge_case_ruled_out": "not_applicable",
            "ownership_concentration": "not_applicable",
            "reverse_conversion_detected": "not_applicable",
            "multi_name_all_agree": "no",
            "tier3_sales_listing_search_performed": "not_applicable",
            "tier3_sales_evidence_found": "not_applicable",
            "ownership_concentration_verified_externally": "not_applicable",
            "ownership_concentration_contradicting_evidence": "not_applicable",
            "tier3_dual_association_search_performed": "not_applicable",
            "tier3_entity_name_registry_search_performed": "not_applicable",
        }
        with mock.patch("ownership_type_checking.research_property", return_value=fake_result), \
             mock.patch("ownership_type_checking.fetch_url_cached", return_value=None):
            result = otc.process_property(None, "gpt-4o", row, {})
        self.assertEqual(result["decision"], "Not Enough Info")
        self.assertEqual(result["determined_type"], "HOA")
        self.assertEqual(result["decision_display"], "Confirmed")
        self.assertTrue(result["multi_name_blocked"])

    def test_white_oak_villas_end_to_end_agreement_allows_apt(self):
        row = {
            "RecordID": "700101",
            "Master_Property Name": "White Oak Villas, South Cottage Village",
            "Master_Ownership Type": "HOA",
            "Master_Monthly Association Fees": "",
        }
        fake_result = {
            "determined_type": "APT",
            "decision": "Override",
            "confidence": "High",
            "evidence_tier_used": "Tier 2",
            "reasoning": (
                "Both White Oak Villas and South Cottage Village independently confirmed as a "
                "single-owner apartment complex via county parcel records."
            ),
            "sources": ["https://county-assessor.example.gov/parcel/700101"],
            "structural_edge_case": "none",
            "tier3_exception_invoked": "no",
            "tier3_exception_direction": "not_applicable",
            "tier3_reverse_attempt2_exhausted": "not_applicable",
            "tier3_independent_source_count": 0,
            "tier3_contradicting_evidence": "not_applicable",
            "tier3_internal_db_corroboration": "",
            "tier3_structural_edge_case_ruled_out": "not_applicable",
            "ownership_concentration": "not_applicable",
            "reverse_conversion_detected": "not_applicable",
            "multi_name_all_agree": "yes",
            "tier3_sales_listing_search_performed": "not_applicable",
            "tier3_sales_evidence_found": "not_applicable",
            "ownership_concentration_verified_externally": "not_applicable",
            "ownership_concentration_contradicting_evidence": "not_applicable",
            "tier3_dual_association_search_performed": "not_applicable",
            "tier3_entity_name_registry_search_performed": "not_applicable",
        }
        with mock.patch("ownership_type_checking.research_property", return_value=fake_result), \
             mock.patch("ownership_type_checking.fetch_url_cached", return_value=None):
            result = otc.process_property(None, "gpt-4o", row, {})
        self.assertEqual(result["decision"], "Override")
        self.assertEqual(result["determined_type"], "APT")
        self.assertEqual(result["decision_display"], "Changed from HOA to APT")


if __name__ == "__main__":
    unittest.main()
