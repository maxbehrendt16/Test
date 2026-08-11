import json
import tempfile
import unittest
from pathlib import Path

import pandas as pd

import ownership_type_checking as otc


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


class Tier3ExceptionGuardrailTests(unittest.TestCase):
    def _clean_override(self, **overrides):
        result = {
            "determined_type": "APT",
            "decision": "Override",
            "confidence": "High",
            "evidence_tier_used": "Tier 3",
            "reasoning": "Single owner, single leasing office, no MLS sales in 36 years.",
            "sources": ["https://crosscreekapts.com", "https://apartments.com/x", "https://apartmentratings.com/x"],
            "tier3_exception_invoked": "yes",
            "tier3_independent_source_count": 3,
            "tier3_contradicting_evidence": "no",
            "tier3_property_age_sufficient": "yes",
            "tier3_internal_db_corroboration": "Master_Monthly Association Fees is null despite 80 units and 36 years old",
            "tier3_structural_edge_case_ruled_out": "yes",
        }
        result.update(overrides)
        return result

    def _old_large_row(self, **overrides):
        row = {
            "Master_Build Year": str(otc._current_year() - 36),
            "Master_Monthly Association Fees": "",
            "Master_Units_50+": "80",
        }
        row.update(overrides)
        return row

    def test_all_five_conditions_hold_allows_override_capped_at_medium(self):
        # This is the Cross Creek Apartments worked example from spec §11.
        row = self._old_large_row()
        result = self._clean_override()
        fixed = otc._enforce_tier3_override_guardrail(row, result)
        self.assertEqual(fixed["decision"], "Override")
        self.assertEqual(fixed["confidence"], "Medium")
        self.assertTrue(fixed["tier3_exception_used"])

    def test_exception_not_invoked_is_downgraded_even_with_strong_evidence(self):
        row = self._old_large_row()
        result = self._clean_override(tier3_exception_invoked="no")
        fixed = otc._enforce_tier3_override_guardrail(row, result)
        self.assertEqual(fixed["decision"], "Not Enough Info")
        self.assertFalse(fixed["tier3_exception_used"])

    def test_fewer_than_three_sources_fails_condition_one(self):
        row = self._old_large_row()
        result = self._clean_override(sources=["https://crosscreekapts.com"], tier3_independent_source_count=1)
        fixed = otc._enforce_tier3_override_guardrail(row, result)
        self.assertEqual(fixed["decision"], "Not Enough Info")

    def test_contradicting_evidence_fails_condition_two(self):
        row = self._old_large_row()
        result = self._clean_override(tier3_contradicting_evidence="yes")
        fixed = otc._enforce_tier3_override_guardrail(row, result)
        self.assertEqual(fixed["decision"], "Not Enough Info")

    def test_self_reported_age_insufficient_fails_condition_three(self):
        row = self._old_large_row()
        result = self._clean_override(tier3_property_age_sufficient="no")
        fixed = otc._enforce_tier3_override_guardrail(row, result)
        self.assertEqual(fixed["decision"], "Not Enough Info")

    def test_recent_build_year_backstop_overrides_a_false_age_claim(self):
        # Model claims age is sufficient, but the row's own Master_Build Year says the
        # property is only 2 years old -- the deterministic backstop must catch this even
        # though every self-reported field looks clean.
        row = self._old_large_row(**{"Master_Build Year": str(otc._current_year() - 2)})
        result = self._clean_override()
        fixed = otc._enforce_tier3_override_guardrail(row, result)
        self.assertEqual(fixed["decision"], "Not Enough Info")
        self.assertIn("years old", fixed["reasoning"])

    def test_no_internal_corroboration_cited_fails_condition_four(self):
        row = self._old_large_row()
        result = self._clean_override(tier3_internal_db_corroboration="")
        fixed = otc._enforce_tier3_override_guardrail(row, result)
        self.assertEqual(fixed["decision"], "Not Enough Info")

    def test_fee_corroboration_contradicted_by_populated_fee_backstop(self):
        # Model claims a null fee corroborates single ownership, but the row's actual fee
        # field is populated -- direct contradiction the backstop must catch.
        row = self._old_large_row(**{"Master_Monthly Association Fees": "350"})
        result = self._clean_override()
        fixed = otc._enforce_tier3_override_guardrail(row, result)
        self.assertEqual(fixed["decision"], "Not Enough Info")
        self.assertIn("populated", fixed["reasoning"])

    def test_structural_edge_case_not_ruled_out_fails_condition_five(self):
        row = self._old_large_row()
        result = self._clean_override(tier3_structural_edge_case_ruled_out="no")
        fixed = otc._enforce_tier3_override_guardrail(row, result)
        self.assertEqual(fixed["decision"], "Not Enough Info")

    def test_exception_never_applies_to_tier1_or_tier2_overrides(self):
        # The exception is specifically about the Tier-3-only case; a real Tier 1 override
        # shouldn't even look at the tier3_* fields.
        row = self._old_large_row()
        result = self._clean_override(evidence_tier_used="Tier 1", tier3_exception_invoked="no")
        fixed = otc._enforce_tier3_override_guardrail(row, result)
        self.assertEqual(fixed["decision"], "Override")
        self.assertFalse(fixed["tier3_exception_used"])


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


if __name__ == "__main__":
    unittest.main()
