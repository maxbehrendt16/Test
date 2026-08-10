import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path

import pandas as pd

import duplicate_research as dr


class SanitizeForExcelTests(unittest.TestCase):
    def test_strips_illegal_control_characters(self):
        poisoned = "Thus, this is a \x02Separate Buildings\x03 duplicate."
        cleaned = dr.sanitize_for_excel(poisoned)
        self.assertEqual(cleaned, "Thus, this is a Separate Buildings duplicate.")

    def test_keeps_tab_newline_and_carriage_return(self):
        text = "line one\nline two\ttabbed\rcarriage"
        self.assertEqual(dr.sanitize_for_excel(text), text)

    def test_non_string_values_pass_through(self):
        self.assertEqual(dr.sanitize_for_excel(8), 8)
        self.assertIsNone(dr.sanitize_for_excel(None))

    def test_write_output_does_not_crash_on_poisoned_evidence(self):
        # This reproduces the actual production crash: a control character in the LLM's
        # evidence text made openpyxl's writer raise IllegalCharacterError after all 10
        # pairs had already been (expensively) researched.
        df = pd.DataFrame([
            {"Group Number": "10", "RecordID": "1", "Address": "1 Metzger Dr"},
            {"Group Number": "10", "RecordID": "2", "Address": "2 Keimel Ct"},
        ])
        results = {"10": {"group": "10", "decision": "Duplicate", "archetype": "Separate Buildings",
                           "confidence": 8, "sources": [],
                           "evidence_summary": "Thus, this is a \x02Separate Buildings\x03 duplicate.",
                           "is_error": False}}
        out_df = dr.build_output_df(df, results)
        summary = dr.compute_summary(results)
        with tempfile.TemporaryDirectory() as tmp:
            output_path = str(Path(tmp) / "results.xlsx")
            dr.write_output(out_df, summary, output_path)  # must not raise
            written = pd.read_excel(output_path, sheet_name="Results")
            self.assertIn("Separate Buildings duplicate.", written["Evidence Summary"].iloc[0])


class FormatRecordFieldExclusionTests(unittest.TestCase):
    def test_master_units_20_plus_is_never_shown(self):
        row = {"RecordID": "1", "Master_Units_50+": "355", "Master_Units_20+": "402"}
        text = dr.format_record(row, "Record A", {})
        self.assertIn("Master_Units_50+: 355", text)
        self.assertNotIn("402", text)
        self.assertNotIn("Master_Units_20+", text)

    def test_exclusion_is_case_insensitive(self):
        row = {"RecordID": "1", "master_units_20+": "999"}
        text = dr.format_record(row, "Record A", {})
        self.assertNotIn("999", text)


class BuildUserMessageDistanceTests(unittest.TestCase):
    def test_distance_present_is_included_verbatim(self):
        msg = dr.build_user_message({"RecordID": "1"}, {"RecordID": "2"}, "0.08", {})
        self.assertIn("DISTANCE_MILES between the two records", msg)
        self.assertIn("0.08", msg)
        self.assertNotIn("was not provided", msg)

    def test_distance_missing_column_does_not_claim_data_is_missing(self):
        # Real production files sometimes have no DISTANCE_MILES column at all (dict.get()
        # returns "" via process_group's default) -- this must not read as a blocking gap.
        msg = dr.build_user_message({"RecordID": "1"}, {"RecordID": "2"}, "", {})
        self.assertIn("was not provided", msg)
        self.assertIn("never on its own a reason to conclude Not Enough Info", msg)

    def test_distance_nan_treated_same_as_missing(self):
        msg = dr.build_user_message({"RecordID": "1"}, {"RecordID": "2"}, float("nan"), {})
        self.assertIn("was not provided", msg)


class GroupPairsTests(unittest.TestCase):
    def test_valid_pair(self):
        df = pd.DataFrame([
            {"Group Number": "8", "RecordID": "1", "Address": "1 Main St"},
            {"Group Number": "8", "RecordID": "2", "Address": "2 Main St"},
        ])
        groups = list(dr.group_pairs(df))
        self.assertEqual(len(groups), 1)
        group_id, rows, error = groups[0]
        self.assertEqual(group_id, "8")
        self.assertEqual(len(rows), 2)
        self.assertIsNone(error)

    def test_wrong_row_count_flagged(self):
        df = pd.DataFrame([
            {"Group Number": "9", "RecordID": "1", "Address": "1 Main St"},
        ])
        _, _, error = list(dr.group_pairs(df))[0]
        self.assertIn("does not have exactly 2 records", error)

    def test_missing_address_flagged(self):
        df = pd.DataFrame([
            {"Group Number": "10", "RecordID": "1", "Address": "1 Main St"},
            {"Group Number": "10", "RecordID": "2", "Address": None},
        ])
        _, _, error = list(dr.group_pairs(df))[0]
        self.assertIn("Missing Address", error)

    def test_missing_record_id_flagged(self):
        df = pd.DataFrame([
            {"Group Number": "11", "RecordID": "1", "Address": "1 Main St"},
            {"Group Number": "11", "RecordID": None, "Address": "2 Main St"},
        ])
        _, _, error = list(dr.group_pairs(df))[0]
        self.assertIn("missing RecordID", error)


class CheckpointTests(unittest.TestCase):
    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "checkpoint.jsonl"
            lock = __import__("threading").Lock()
            record = {"group": "8", "decision": "Duplicate", "archetype": "test", "confidence": 8,
                      "evidence_summary": "x", "sources": [], "record_ids": ["1", "2"], "is_error": False}
            dr.append_checkpoint(path, record, lock)
            loaded = dr.load_checkpoint(path)
            self.assertEqual(loaded["8"]["decision"], "Duplicate")

    def test_load_missing_file_returns_empty(self):
        loaded = dr.load_checkpoint(Path("/nonexistent/checkpoint.jsonl"))
        self.assertEqual(loaded, {})


class VerifyCitedTotalArithmeticTests(unittest.TestCase):
    def _record(self, units):
        return {"Master_Units_50+": units}

    def test_overrides_crestview_style_incoherent_total(self):
        # 74 and 120 vs a cited total of 277: 27%/43%/70% off -- none within threshold.
        result = {"decision": "Not Duplicate", "archetype": "Separate Children Within One Complex",
                  "confidence": 8, "evidence_summary": "orig", "cited_total_units": "277"}
        out = dr._verify_cited_total_arithmetic(self._record("74"), self._record("120"), result)
        self.assertEqual(out["decision"], "Not Enough Info")
        self.assertEqual(out["archetype"], "Cited total does not match either record or their sum")
        self.assertLessEqual(out["confidence"], 3)
        self.assertIn("orig", out["evidence_summary"])

    def test_does_not_override_costa_del_sol_style_close_match(self):
        # 768 exact match, 739 within ~4% -- well within threshold via record A alone.
        result = {"decision": "Duplicate", "archetype": "Separate Buildings",
                  "confidence": 8, "evidence_summary": "orig", "cited_total_units": "768"}
        out = dr._verify_cited_total_arithmetic(self._record("768"), self._record("739"), result)
        self.assertEqual(out["decision"], "Duplicate")
        self.assertEqual(out["archetype"], "Separate Buildings")

    def test_does_not_override_ocean_grove_style_looser_but_real_match(self):
        # 200 and 208 vs 251: ~20% and ~17% off -- B is within the 25% threshold.
        result = {"decision": "Duplicate", "archetype": "Separate Buildings",
                  "confidence": 6, "evidence_summary": "orig", "cited_total_units": "251"}
        out = dr._verify_cited_total_arithmetic(self._record("200"), self._record("208"), result)
        self.assertEqual(out["decision"], "Duplicate")

    def test_passthrough_when_no_total_cited(self):
        result = {"decision": "Duplicate", "archetype": "Separate Buildings",
                  "confidence": 8, "evidence_summary": "orig", "cited_total_units": ""}
        out = dr._verify_cited_total_arithmetic(self._record("74"), self._record("120"), result)
        self.assertEqual(out["decision"], "Duplicate")

    def test_passthrough_for_archetypes_not_covered(self):
        # Same Building/Mislabeled Property/etc. don't hinge on a cited total this way.
        result = {"decision": "Duplicate", "archetype": "Same Building",
                  "confidence": 8, "evidence_summary": "orig", "cited_total_units": "277"}
        out = dr._verify_cited_total_arithmetic(self._record("74"), self._record("120"), result)
        self.assertEqual(out["decision"], "Duplicate")

    def test_passthrough_when_unit_counts_missing_or_non_numeric(self):
        result = {"decision": "Not Duplicate", "archetype": "Separate Children Within One Complex",
                  "confidence": 8, "evidence_summary": "orig", "cited_total_units": "277"}
        out = dr._verify_cited_total_arithmetic(self._record(""), self._record("120"), result)
        self.assertEqual(out["decision"], "Not Duplicate")

    def test_process_group_applies_override_end_to_end(self):
        rows = [{"RecordID": "5657202", "Address": "11 Lilly Ln", "Master_Units_50+": "74"},
                {"RecordID": "16416", "Address": "1 Azalea Ln", "Master_Units_50+": "120"}]

        def fake_research_pair(provider, client, record_a, record_b, distance, url_cache, model):
            return {"decision": "Not Duplicate", "archetype": "Separate Children Within One Complex",
                    "confidence": 8, "evidence_summary": "unit counts fit as fractions of 277",
                    "sources": [], "flagged_record_id": "", "cited_total_units": "277"}

        original = dr.research_pair
        dr.research_pair = fake_research_pair
        try:
            result = dr.process_group("anthropic", object(), "m", "7", rows, None, {})
        finally:
            dr.research_pair = original
        self.assertEqual(result["decision"], "Not Enough Info")
        self.assertEqual(result["archetype"], "Cited total does not match either record or their sum")
        self.assertLessEqual(result["confidence"], 3)


class CapConfidenceWhenTotalMissingTests(unittest.TestCase):
    def _record(self, units):
        return {"Master_Units_50+": units}

    def test_caps_crestview_style_no_total_and_big_gap(self):
        # 74 vs 120 (38% gap), no cited total, confidence 8 -- should be capped.
        result = {"decision": "Duplicate", "archetype": "Separate Buildings", "confidence": 8,
                  "evidence_summary": "orig", "cited_total_units": ""}
        out = dr._cap_confidence_when_total_missing(self._record("74"), self._record("120"), result)
        self.assertEqual(out["confidence"], dr.NO_TOTAL_CONFIDENCE_CAP)
        self.assertIn("orig", out["evidence_summary"])
        self.assertIn("Confidence capped", out["evidence_summary"])

    def test_no_cap_when_records_already_close(self):
        # 355 vs 356 -- effectively identical, no total needed to trust that.
        result = {"decision": "Duplicate", "archetype": "Separate Buildings", "confidence": 8,
                  "evidence_summary": "orig", "cited_total_units": ""}
        out = dr._cap_confidence_when_total_missing(self._record("355"), self._record("356"), result)
        self.assertEqual(out["confidence"], 8)

    def test_no_cap_when_total_was_cited(self):
        # Handled by _verify_cited_total_arithmetic instead -- this function should defer.
        result = {"decision": "Duplicate", "archetype": "Separate Buildings", "confidence": 8,
                  "evidence_summary": "orig", "cited_total_units": "768"}
        out = dr._cap_confidence_when_total_missing(self._record("74"), self._record("120"), result)
        self.assertEqual(out["confidence"], 8)

    def test_no_cap_for_archetypes_not_covered(self):
        result = {"decision": "Duplicate", "archetype": "Same Building", "confidence": 8,
                  "evidence_summary": "orig", "cited_total_units": ""}
        out = dr._cap_confidence_when_total_missing(self._record("74"), self._record("120"), result)
        self.assertEqual(out["confidence"], 8)

    def test_does_not_raise_confidence_already_below_cap(self):
        result = {"decision": "Not Enough Info", "archetype": "Separate Buildings", "confidence": 3,
                  "evidence_summary": "orig", "cited_total_units": ""}
        out = dr._cap_confidence_when_total_missing(self._record("74"), self._record("120"), result)
        self.assertEqual(out["confidence"], 3)

    def test_process_group_applies_cap_end_to_end(self):
        rows = [{"RecordID": "5657202", "Address": "11 Lilly Ln", "Master_Units_50+": "74"},
                {"RecordID": "16416", "Address": "1 Azalea Ln", "Master_Units_50+": "120"}]

        def fake_research_pair(provider, client, record_a, record_b, distance, url_cache, model):
            return {"decision": "Duplicate", "archetype": "Separate Buildings", "confidence": 8,
                    "evidence_summary": "townhouse complexes often vary by phase",
                    "sources": [], "flagged_record_id": "", "cited_total_units": ""}

        original = dr.research_pair
        dr.research_pair = fake_research_pair
        try:
            result = dr.process_group("anthropic", object(), "m", "7", rows, None, {})
        finally:
            dr.research_pair = original
        self.assertEqual(result["confidence"], dr.NO_TOTAL_CONFIDENCE_CAP)
        self.assertEqual(result["decision"], "Duplicate")  # decision untouched, only confidence capped


class CorrectChildArchetypeTests(unittest.TestCase):
    def _record(self, units):
        return {"Master_Units_50+": units}

    def test_corrects_costa_del_sol_style_parent_child_mislabel(self):
        # 768 exact, 739 within ~4% -- both close to the cited 768 total.
        result = {"decision": "Not Duplicate", "archetype": "Parent/Child Mismatch", "confidence": 7,
                  "evidence_summary": "orig", "cited_total_units": "768"}
        out = dr._correct_child_archetype_when_total_supports_duplicate(
            self._record("768"), self._record("739"), result)
        self.assertEqual(out["decision"], "Duplicate")
        self.assertEqual(out["archetype"], "Separate Buildings")
        self.assertLessEqual(out["confidence"], 6)
        self.assertIn("orig", out["evidence_summary"])

    def test_corrects_separate_children_the_same_way(self):
        result = {"decision": "Not Duplicate", "archetype": "Separate Children Within One Complex",
                  "confidence": 8, "evidence_summary": "orig", "cited_total_units": "768"}
        out = dr._correct_child_archetype_when_total_supports_duplicate(
            self._record("768"), self._record("739"), result)
        self.assertEqual(out["decision"], "Duplicate")

    def test_leaves_genuine_parent_child_alone(self):
        # A true parent/child case: child is a small fraction (50), total (700) is close to
        # neither the child's own count nor a coincidentally-large sibling.
        result = {"decision": "Not Duplicate", "archetype": "Parent/Child Mismatch", "confidence": 8,
                  "evidence_summary": "orig", "cited_total_units": "700"}
        out = dr._correct_child_archetype_when_total_supports_duplicate(
            self._record("50"), self._record("700"), result)
        self.assertEqual(out["decision"], "Not Duplicate")  # only Record B is close -- not both

    def test_passthrough_when_no_total_cited(self):
        result = {"decision": "Not Duplicate", "archetype": "Parent/Child Mismatch", "confidence": 8,
                  "evidence_summary": "orig", "cited_total_units": ""}
        out = dr._correct_child_archetype_when_total_supports_duplicate(
            self._record("50"), self._record("700"), result)
        self.assertEqual(out["decision"], "Not Duplicate")

    def test_passthrough_for_archetypes_not_covered(self):
        result = {"decision": "Duplicate", "archetype": "Same Building", "confidence": 8,
                  "evidence_summary": "orig", "cited_total_units": "768"}
        out = dr._correct_child_archetype_when_total_supports_duplicate(
            self._record("768"), self._record("739"), result)
        self.assertEqual(out["archetype"], "Same Building")


class CapConfidenceWithoutNamingSignalTests(unittest.TestCase):
    def test_caps_when_naming_signal_not_confirmed(self):
        # The exact reported case: two identically-named records, 88 units each, total 205 --
        # plausible-looking math but no naming signal distinguishing them as siblings.
        result = {"decision": "Not Duplicate", "archetype": "Separate Children Within One Complex",
                  "confidence": 8, "evidence_summary": "orig", "naming_signal_confirmed": "no"}
        out = dr._cap_confidence_without_naming_signal(result)
        self.assertEqual(out["confidence"], dr.NAMING_SIGNAL_CONFIDENCE_CAP)
        self.assertIn("orig", out["evidence_summary"])
        self.assertIn("naming/documentary signal", out["evidence_summary"])

    def test_caps_when_field_omitted_entirely(self):
        # Missing the field should be treated the same as "no", not as an implicit "yes".
        result = {"decision": "Not Duplicate", "archetype": "Parent/Child Mismatch", "confidence": 9,
                  "evidence_summary": "orig"}
        out = dr._cap_confidence_without_naming_signal(result)
        self.assertEqual(out["confidence"], dr.NAMING_SIGNAL_CONFIDENCE_CAP)

    def test_no_cap_when_signal_confirmed(self):
        result = {"decision": "Not Duplicate", "archetype": "Separate Children Within One Complex",
                  "confidence": 9, "evidence_summary": "orig", "naming_signal_confirmed": "yes"}
        out = dr._cap_confidence_without_naming_signal(result)
        self.assertEqual(out["confidence"], 9)

    def test_no_cap_for_other_archetypes(self):
        result = {"decision": "Duplicate", "archetype": "Same Building", "confidence": 9,
                  "evidence_summary": "orig", "naming_signal_confirmed": "no"}
        out = dr._cap_confidence_without_naming_signal(result)
        self.assertEqual(out["confidence"], 9)

    def test_does_not_raise_confidence_already_below_cap(self):
        result = {"decision": "Not Duplicate", "archetype": "Parent/Child Mismatch", "confidence": 4,
                  "evidence_summary": "orig", "naming_signal_confirmed": "no"}
        out = dr._cap_confidence_without_naming_signal(result)
        self.assertEqual(out["confidence"], 4)

    def test_process_group_applies_cap_end_to_end(self):
        rows = [{"RecordID": "1", "Address": "1 Main St", "Master_Units_50+": "88"},
                {"RecordID": "2", "Address": "2 Main St", "Master_Units_50+": "88"}]

        def fake_research_pair(provider, client, record_a, record_b, distance, url_cache, model):
            return {"decision": "Not Duplicate", "archetype": "Separate Children Within One Complex",
                    "confidence": 8, "evidence_summary": "both show 88 units against a 205 total",
                    "sources": [], "flagged_record_id": "", "cited_total_units": "205",
                    "naming_signal_confirmed": "no"}

        original = dr.research_pair
        dr.research_pair = fake_research_pair
        try:
            result = dr.process_group("anthropic", object(), "m", "1", rows, None, {})
        finally:
            dr.research_pair = original
        self.assertEqual(result["confidence"], dr.NAMING_SIGNAL_CONFIDENCE_CAP)
        self.assertEqual(result["archetype"], "Separate Children Within One Complex")


class CleanEvidenceSummaryTests(unittest.TestCase):
    def test_strips_markdown_links_to_label_text(self):
        text = "Confirmed via the HOA site ([costadelsolassociation.com](https://www.costadelsolassociation.com/about))."
        cleaned = dr._clean_evidence_summary(text)
        self.assertNotIn("http", cleaned)
        self.assertIn("costadelsolassociation.com", cleaned)

    def test_collapses_paragraph_breaks(self):
        text = "First sentence.\n\nSecond paragraph.\n\nThird paragraph."
        cleaned = dr._clean_evidence_summary(text)
        self.assertNotIn("\n", cleaned)
        self.assertIn("First sentence.", cleaned)
        self.assertIn("Third paragraph.", cleaned)

    def test_truncates_overly_long_text_at_sentence_boundary(self):
        sentence = "This is a filler sentence used to pad out the evidence summary well past the limit. "
        text = sentence * 20
        cleaned = dr._clean_evidence_summary(text)
        self.assertLessEqual(len(cleaned), dr.MAX_EVIDENCE_SUMMARY_CHARS + len(" [truncated for length]") + 5)
        self.assertIn("[truncated for length]", cleaned)
        self.assertTrue(cleaned.replace(" [truncated for length]", "").strip().endswith("."))

    def test_short_text_untouched(self):
        text = "A short, normal evidence summary."
        self.assertEqual(dr._clean_evidence_summary(text), text)

    def test_empty_text_passthrough(self):
        self.assertEqual(dr._clean_evidence_summary(""), "")


class ProcessGroupErrorTests(unittest.TestCase):
    def test_malformed_group_short_circuits_without_client(self):
        rows = [{"RecordID": "1", "Address": "1 Main St"}]
        result = dr.process_group(None, None, None, "9", rows, "some error reason", {})
        self.assertEqual(result["decision"], "Not Enough Info")
        self.assertTrue(result["is_error"])
        self.assertEqual(result["evidence_summary"], "some error reason")


class ResolveProviderTests(unittest.TestCase):
    def _clear_keys(self):
        os.environ.pop("ANTHROPIC_API_KEY", None)
        os.environ.pop("OPENAI_API_KEY", None)

    def test_auto_detects_anthropic(self):
        self._clear_keys()
        os.environ["ANTHROPIC_API_KEY"] = "x"
        try:
            self.assertEqual(dr.resolve_provider(None), "anthropic")
        finally:
            self._clear_keys()

    def test_auto_detects_openai(self):
        self._clear_keys()
        os.environ["OPENAI_API_KEY"] = "x"
        try:
            self.assertEqual(dr.resolve_provider(None), "openai")
        finally:
            self._clear_keys()

    def test_both_keys_require_explicit_provider(self):
        self._clear_keys()
        os.environ["ANTHROPIC_API_KEY"] = "x"
        os.environ["OPENAI_API_KEY"] = "y"
        try:
            with self.assertRaises(SystemExit):
                dr.resolve_provider(None)
            self.assertEqual(dr.resolve_provider("openai"), "openai")
        finally:
            self._clear_keys()

    def test_no_keys_raises(self):
        self._clear_keys()
        with self.assertRaises(SystemExit):
            dr.resolve_provider(None)

    def test_explicit_provider_without_matching_key_raises(self):
        self._clear_keys()
        os.environ["OPENAI_API_KEY"] = "x"
        try:
            with self.assertRaises(SystemExit):
                dr.resolve_provider("anthropic")
        finally:
            self._clear_keys()


class BuildOutputAndSummaryTests(unittest.TestCase):
    def setUp(self):
        self.df = pd.DataFrame([
            {"Group Number": "8", "RecordID": "1", "Address": "1 Main St"},
            {"Group Number": "8", "RecordID": "2", "Address": "2 Main St"},
            {"Group Number": "9", "RecordID": "3", "Address": "3 Main St"},
            {"Group Number": "9", "RecordID": "4", "Address": "4 Main St"},
        ])
        self.results = {
            "8": {"group": "8", "decision": "Duplicate", "archetype": "Mislabeled Property",
                  "confidence": 8, "evidence_summary": "es1", "sources": ["a.com", "b.com"], "is_error": False},
            "9": {"group": "9", "decision": "Not Duplicate", "archetype": "Coincidental Name Match",
                  "confidence": 6, "evidence_summary": "es2", "sources": [], "is_error": False},
        }

    def test_build_output_df_mirrors_both_rows(self):
        out = dr.build_output_df(self.df, self.results)
        group_8 = out[out["Group Number"] == "8"]
        self.assertTrue((group_8["Decision"] == "Duplicate").all())
        self.assertTrue((group_8["Confidence"] == 8).all())
        self.assertEqual(group_8.iloc[0]["Sources"], "a.com; b.com")

    def test_compute_summary(self):
        summary = dr.compute_summary(self.results)
        self.assertEqual(summary["total_pairs"], 2)
        self.assertEqual(summary["by_decision"]["Duplicate"], 1)
        self.assertEqual(summary["by_decision"]["Not Duplicate"], 1)
        self.assertEqual(summary["by_decision"]["Not Enough Info"], 0)
        self.assertEqual(summary["average_confidence"], 7.0)
        self.assertEqual(summary["errors"], 0)

    def test_missing_flagged_record_id_is_backward_compatible(self):
        # Older checkpoint records won't have this key at all.
        out = dr.build_output_df(self.df, self.results)
        self.assertTrue((out["Flagged As Anomaly"] == "").all())


class NamesMatchTests(unittest.TestCase):
    def test_type_spelled_out_vs_abbreviated_matches(self):
        self.assertEqual(dr._names_match("Jollywood HOA", "Jollywood Homeowners Association"), "Yes")

    def test_bare_name_vs_name_with_suffix_matches(self):
        self.assertEqual(dr._names_match("Jollywood", "Jollywood HOA"), "Yes")

    def test_conflicting_entity_types_is_a_mismatch(self):
        self.assertEqual(dr._names_match("Jollywood HOA", "Jollywood COA"), "No")

    def test_genuinely_different_names_is_a_mismatch(self):
        self.assertEqual(dr._names_match("Hollywood", "Jollywood"), "No")

    def test_association_inc_suffix_ignored(self):
        self.assertEqual(dr._names_match("Le Baron Condominium Association, Inc.", "The Le Baron"), "Yes")

    def test_blank_when_either_side_missing(self):
        self.assertEqual(dr._names_match("", "Jollywood"), "")
        self.assertEqual(dr._names_match(None, "Jollywood"), "")


class NameMatchForPairTests(unittest.TestCase):
    def test_uses_cart_property_name_clean_directly_when_present(self):
        # A batch with CART_PROPERTY_NAME_CLEAN should compare that field directly (no fuzzy
        # HOA/COA-synonym or filler-word logic) rather than falling back to Master_Property Name.
        row_a = pd.Series({"CART_PROPERTY_NAME_CLEAN": "RIVERVIEW", "Master_Property Name": "Riverview HOA"})
        row_b = pd.Series({"CART_PROPERTY_NAME_CLEAN": "RIVERVIEW", "Master_Property Name": "Totally Different"})
        self.assertEqual(dr._name_match_for_pair(row_a, row_b), "Yes")

    def test_cart_property_name_clean_mismatch(self):
        row_a = pd.Series({"CART_PROPERTY_NAME_CLEAN": "RIVERVIEW", "Master_Property Name": "Riverview HOA"})
        row_b = pd.Series({"CART_PROPERTY_NAME_CLEAN": "VILLAGE KAUFMAN", "Master_Property Name": "Riverview HOA"})
        self.assertEqual(dr._name_match_for_pair(row_a, row_b), "No")

    def test_falls_back_to_fuzzy_names_match_when_column_absent(self):
        row_a = pd.Series({"Master_Property Name": "Jollywood"})
        row_b = pd.Series({"Master_Property Name": "Jollywood HOA"})
        self.assertEqual(dr._name_match_for_pair(row_a, row_b), "Yes")


class DistanceBucketTests(unittest.TestCase):
    def test_buckets_boundaries(self):
        self.assertEqual(dr._distance_bucket("0.049"), "<0.05mi")
        self.assertEqual(dr._distance_bucket("0.05"), "0.05-0.2mi")
        self.assertEqual(dr._distance_bucket("0.19"), "0.05-0.2mi")
        self.assertEqual(dr._distance_bucket("0.2"), "0.2-0.5mi")
        self.assertEqual(dr._distance_bucket("0.49"), "0.2-0.5mi")
        self.assertEqual(dr._distance_bucket("0.5"), "0.5mi+")
        self.assertEqual(dr._distance_bucket("12"), "0.5mi+")

    def test_missing_value_is_unknown(self):
        self.assertEqual(dr._distance_bucket(""), "Unknown")
        self.assertEqual(dr._distance_bucket(None), "Unknown")


class DuplicateFlagsTests(unittest.TestCase):
    def _row(self, group, record_id, address, name, units, hw, costar, fa, ownership=""):
        return {"Group Number": group, "RecordID": record_id, "Address": address,
                "Master_Property Name": name, "Master_Units_50+": units,
                "In HW": hw, "In Costar": costar, "In FA": fa,
                "Master_Ownership Type": ownership}

    def test_all_flags_true_for_clean_duplicate(self):
        df = pd.DataFrame([
            self._row("1", "a", "1 Main St", "Vizcaya", "100", "1", "1", "1"),
            self._row("1", "b", "1 Main St", "Vizcaya", "105", "1", "1", "1"),
        ])
        results = {"1": {"group": "1", "decision": "Duplicate", "archetype": "Same Building",
                         "confidence": 9, "evidence_summary": "es", "sources": [], "is_error": False}}
        out = dr.build_output_df(df, results)
        self.assertTrue((out["Address Match"] == "Yes").all())
        self.assertTrue((out["Name Match"] == "Yes").all())
        self.assertTrue((out["Unit Count Diff 1-10"] == "Yes").all())  # |100-105| = 5
        self.assertTrue((out["Unit Count Diff > 10"] == "No").all())
        self.assertTrue((out["Both In Hotwire"] == "Yes").all())
        self.assertTrue((out["Both In CoStar"] == "Yes").all())
        self.assertTrue((out["Both In First American"] == "Yes").all())

    def test_mismatches_and_one_sided_db_membership(self):
        df = pd.DataFrame([
            self._row("1", "a", "1 Main St", "Vizcaya", "96", "1", "0", "1"),
            self._row("1", "b", "2 Other Ave", "Meridian Place", "127", "0", "0", "0"),
        ])
        results = {"1": {"group": "1", "decision": "Duplicate", "archetype": "Separate Buildings",
                         "confidence": 6, "evidence_summary": "es", "sources": [], "is_error": False}}
        out = dr.build_output_df(df, results)
        self.assertTrue((out["Address Match"] == "No").all())
        self.assertTrue((out["Name Match"] == "No").all())
        self.assertTrue((out["Unit Count Diff 1-10"] == "No").all())
        self.assertTrue((out["Unit Count Diff > 10"] == "Yes").all())  # |96-127| = 31 > 10
        self.assertTrue((out["Both In Hotwire"] == "No").all())  # only one side is 1
        self.assertTrue((out["Both In CoStar"] == "No").all())   # neither side is 1
        self.assertTrue((out["Both In First American"] == "No").all())  # only one side is 1

    def test_unit_count_diff_flags_neither_true_on_exact_match(self):
        df = pd.DataFrame([
            self._row("1", "a", "1 Main St", "X", "100", "1", "1", "1"),
            self._row("1", "b", "1 Main St", "X", "100", "1", "1", "1"),
        ])
        results = {"1": {"group": "1", "decision": "Duplicate", "archetype": "Same Building",
                         "confidence": 8, "evidence_summary": "es", "sources": [], "is_error": False}}
        out = dr.build_output_df(df, results)
        self.assertTrue((out["Unit Count Diff 1-10"] == "No").all())
        self.assertTrue((out["Unit Count Diff > 10"] == "No").all())

    def test_flags_populated_for_non_duplicate_decisions_too(self):
        # Cross-check flags (address/name/units/ownership/db-membership/master source) compare
        # the two records directly and don't depend on the LLM's decision, so they should now
        # populate for every pair in the Results tab -- not just confirmed Duplicates.
        df = pd.DataFrame([
            self._row("1", "a", "1 Main St", "X", "100", "1", "1", "1"),
            self._row("1", "b", "1 Main St", "X", "100", "1", "1", "1"),
        ])
        results = {"1": {"group": "1", "decision": "Not Duplicate", "archetype": "Coincidental Name Match",
                         "confidence": 7, "evidence_summary": "es", "sources": [], "is_error": False}}
        out = dr.build_output_df(df, results)
        for col in ["Address Match", "Name Match", "Unit Count Diff 1-10", "Unit Count Diff > 10",
                    "Both In Hotwire", "Both In CoStar", "Both In First American", "Same Master Source"]:
            self.assertTrue((out[col] != "").all(), f"{col} should be populated for a non-Duplicate decision")
        self.assertTrue((out["Address Match"] == "Yes").all())
        self.assertTrue((out["Unit Count Diff 1-10"] == "No").all())  # exact match, diff == 0
        self.assertTrue((out["Unit Count Diff > 10"] == "No").all())

    def test_missing_source_columns_do_not_crash(self):
        # A dataset without In HW/In Costar/In FA/Master_Units_50+ at all (e.g. sample_pairs.csv).
        df = pd.DataFrame([
            {"Group Number": "1", "RecordID": "a", "Address": "1 Main St"},
            {"Group Number": "1", "RecordID": "b", "Address": "1 Main St"},
        ])
        results = {"1": {"group": "1", "decision": "Duplicate", "archetype": "Same Building",
                         "confidence": 8, "evidence_summary": "es", "sources": [], "is_error": False}}
        out = dr.build_output_df(df, results)
        self.assertTrue((out["Unit Count Diff 1-10"] == "").all())
        self.assertTrue((out["Unit Count Diff > 10"] == "").all())
        self.assertTrue((out["Both In Hotwire"] == "No").all())
        self.assertTrue((out["Master Source"] == "Other").all())

    def test_master_source_priority_and_per_record_computation(self):
        df = pd.DataFrame([
            self._row("1", "a", "1 Main St", "X", "100", "1", "1", "1"),   # Hotwire wins
            self._row("1", "b", "1 Main St", "X", "100", "0", "1", "1"),   # CoStar wins
        ])
        results = {"1": {"group": "1", "decision": "Duplicate", "archetype": "Same Building",
                         "confidence": 8, "evidence_summary": "es", "sources": [], "is_error": False}}
        out = dr.build_output_df(df, results)
        self.assertEqual(out[out["RecordID"] == "a"].iloc[0]["Master Source"], "Hotwire")
        self.assertEqual(out[out["RecordID"] == "b"].iloc[0]["Master Source"], "CoStar")

    def test_master_source_computed_regardless_of_decision(self):
        df = pd.DataFrame([
            self._row("1", "a", "1 Main St", "X", "100", "0", "0", "1"),
            self._row("1", "b", "2 Other Ave", "Y", "999", "0", "0", "0"),
        ])
        results = {"1": {"group": "1", "decision": "Not Duplicate", "archetype": "Coincidental Name Match",
                         "confidence": 7, "evidence_summary": "es", "sources": [], "is_error": False}}
        out = dr.build_output_df(df, results)
        self.assertEqual(out[out["RecordID"] == "a"].iloc[0]["Master Source"], "First American")
        self.assertEqual(out[out["RecordID"] == "b"].iloc[0]["Master Source"], "Other")

    def test_different_ownership_type_flag(self):
        df = pd.DataFrame([
            self._row("1", "a", "1 Main St", "X", "100", "1", "1", "1", ownership="HOA"),
            self._row("1", "b", "1 Main St", "X", "100", "1", "1", "1", ownership="COA"),
        ])
        results = {"1": {"group": "1", "decision": "Duplicate", "archetype": "Separate Buildings",
                         "confidence": 7, "evidence_summary": "es", "sources": [], "is_error": False}}
        out = dr.build_output_df(df, results)
        self.assertTrue((out["Different Ownership Type"] == "Yes").all())

    def test_same_ownership_type_flag(self):
        df = pd.DataFrame([
            self._row("1", "a", "1 Main St", "X", "100", "1", "1", "1", ownership="HOA"),
            self._row("1", "b", "1 Main St", "X", "100", "1", "1", "1", ownership="HOA"),
        ])
        results = {"1": {"group": "1", "decision": "Duplicate", "archetype": "Same Building",
                         "confidence": 8, "evidence_summary": "es", "sources": [], "is_error": False}}
        out = dr.build_output_df(df, results)
        self.assertTrue((out["Different Ownership Type"] == "No").all())

    def test_ownership_type_blank_when_missing(self):
        df = pd.DataFrame([
            self._row("1", "a", "1 Main St", "X", "100", "1", "1", "1", ownership=""),
            self._row("1", "b", "1 Main St", "X", "100", "1", "1", "1", ownership="HOA"),
        ])
        results = {"1": {"group": "1", "decision": "Duplicate", "archetype": "Same Building",
                         "confidence": 8, "evidence_summary": "es", "sources": [], "is_error": False}}
        out = dr.build_output_df(df, results)
        self.assertTrue((out["Different Ownership Type"] == "").all())

    def test_name_match_uses_cart_property_name_clean_when_present(self):
        # Master_Property Name looks like a mismatch, but CART_PROPERTY_NAME_CLEAN (the
        # upstream-cleaned field) matches exactly -- the cleaned field should win.
        df = pd.DataFrame([
            self._row("1", "a", "1 Main St", "Riverview Homeowners Assoc", "100", "1", "1", "1"),
            self._row("1", "b", "1 Main St", "Totally Different Name", "100", "1", "1", "1"),
        ])
        df["CART_PROPERTY_NAME_CLEAN"] = ["RIVERVIEW", "RIVERVIEW"]
        results = {"1": {"group": "1", "decision": "Duplicate", "archetype": "Same Building",
                         "confidence": 8, "evidence_summary": "es", "sources": [], "is_error": False}}
        out = dr.build_output_df(df, results)
        self.assertTrue((out["Name Match"] == "Yes").all())

    def test_name_match_cart_property_name_clean_mismatch(self):
        df = pd.DataFrame([
            self._row("1", "a", "1 Main St", "Riverview", "100", "1", "1", "1"),
            self._row("1", "b", "1 Main St", "Riverview", "100", "1", "1", "1"),
        ])
        df["CART_PROPERTY_NAME_CLEAN"] = ["RIVERVIEW", "VILLAGE KAUFMAN"]
        results = {"1": {"group": "1", "decision": "Duplicate", "archetype": "Same Building",
                         "confidence": 8, "evidence_summary": "es", "sources": [], "is_error": False}}
        out = dr.build_output_df(df, results)
        self.assertTrue((out["Name Match"] == "No").all())

    def test_same_master_source_true_when_matching_and_not_other(self):
        df = pd.DataFrame([
            self._row("1", "a", "1 Main St", "X", "100", "1", "0", "0"),  # Hotwire
            self._row("1", "b", "1 Main St", "X", "100", "1", "0", "0"),  # Hotwire
        ])
        results = {"1": {"group": "1", "decision": "Duplicate", "archetype": "Same Building",
                         "confidence": 8, "evidence_summary": "es", "sources": [], "is_error": False}}
        out = dr.build_output_df(df, results)
        self.assertTrue((out["Same Master Source"] == "Yes").all())

    def test_same_master_source_false_when_both_other(self):
        # Both fall back to "Other" -- matching, but explicitly excluded per spec.
        df = pd.DataFrame([
            self._row("1", "a", "1 Main St", "X", "100", "0", "0", "0"),
            self._row("1", "b", "1 Main St", "X", "100", "0", "0", "0"),
        ])
        results = {"1": {"group": "1", "decision": "Duplicate", "archetype": "Same Building",
                         "confidence": 8, "evidence_summary": "es", "sources": [], "is_error": False}}
        out = dr.build_output_df(df, results)
        self.assertTrue((out["Same Master Source"] == "No").all())

    def test_same_master_source_false_when_sources_differ(self):
        df = pd.DataFrame([
            self._row("1", "a", "1 Main St", "X", "100", "1", "0", "0"),  # Hotwire
            self._row("1", "b", "1 Main St", "X", "100", "0", "1", "0"),  # CoStar
        ])
        results = {"1": {"group": "1", "decision": "Duplicate", "archetype": "Same Building",
                         "confidence": 8, "evidence_summary": "es", "sources": [], "is_error": False}}
        out = dr.build_output_df(df, results)
        self.assertTrue((out["Same Master Source"] == "No").all())


class DuplicateFlagSummaryTests(unittest.TestCase):
    def _row(self, group, record_id, address, name, units, hw, costar, fa, ownership=""):
        return {"Group Number": group, "RecordID": record_id, "Address": address,
                "Master_Property Name": name, "Master_Units_50+": units,
                "In HW": hw, "In Costar": costar, "In FA": fa,
                "Master_Ownership Type": ownership}

    def test_counts_one_row_per_pair_not_per_record(self):
        df = pd.DataFrame([
            # Pair 1: Duplicate, everything matches
            self._row("1", "a", "1 Main St", "X", "100", "1", "0", "0", ownership="HOA"),
            self._row("1", "b", "1 Main St", "X", "100", "1", "0", "0", ownership="HOA"),
            # Pair 2: Duplicate, addresses/names/ownership differ, units far apart
            self._row("2", "c", "1 Main St", "X", "50", "0", "0", "0", ownership="HOA"),
            self._row("2", "d", "2 Other Ave", "Y", "500", "0", "0", "0", ownership="COA"),
            # Pair 3: Not Duplicate -- must not count toward the flag breakdown at all
            self._row("3", "e", "9 Ninth St", "Z", "10", "0", "0", "0"),
            self._row("3", "f", "9 Ninth St", "Z", "10", "0", "0", "0"),
        ])
        results = {
            "1": {"group": "1", "decision": "Duplicate", "archetype": "Same Building",
                  "confidence": 9, "evidence_summary": "es", "sources": [], "is_error": False},
            "2": {"group": "2", "decision": "Duplicate", "archetype": "Separate Buildings",
                  "confidence": 5, "evidence_summary": "es", "sources": [], "is_error": False},
            "3": {"group": "3", "decision": "Not Duplicate", "archetype": "Coincidental Name Match",
                  "confidence": 7, "evidence_summary": "es", "sources": [], "is_error": False},
        }
        out = dr.build_output_df(df, results)
        summary = dr.compute_duplicate_flag_summary(out)
        self.assertEqual(summary["total_duplicate_pairs"], 2)  # not 4 (rows) or 3 (all pairs)
        self.assertEqual(summary["flags"]["Address Match"], {"Yes": 1, "No": 1, "Unknown": 0})
        self.assertEqual(summary["flags"]["Name Match"], {"Yes": 1, "No": 1, "Unknown": 0})
        # Pair 1: units 100 vs 100 (exact match, diff == 0) trips neither bucket.
        # Pair 2: units 50 vs 500 (diff == 450) trips only the > 10 bucket.
        self.assertEqual(summary["flags"]["Unit Count Diff 1-10"], {"Yes": 0, "No": 2, "Unknown": 0})
        self.assertEqual(summary["flags"]["Unit Count Diff > 10"], {"Yes": 1, "No": 1, "Unknown": 0})
        self.assertEqual(summary["flags"]["Different Ownership Type"], {"Yes": 1, "No": 1, "Unknown": 0})

    def test_flag_breakdown_excludes_non_duplicate_pairs_even_though_flags_are_populated(self):
        # build_output_df now populates the cross-check flags for every pair regardless of
        # decision, but the Summary tab's aggregation must still be scoped to Duplicate pairs
        # only -- this exercises a Not Duplicate pair whose per-row flags are non-blank.
        df = pd.DataFrame([
            self._row("1", "a", "1 Main St", "X", "100", "1", "1", "1"),
            self._row("1", "b", "1 Main St", "X", "100", "1", "1", "1"),
        ])
        results = {"1": {"group": "1", "decision": "Not Duplicate", "archetype": "Coincidental Name Match",
                         "confidence": 7, "evidence_summary": "es", "sources": [], "is_error": False}}
        out = dr.build_output_df(df, results)
        self.assertTrue((out["Address Match"] == "Yes").all())  # per-row flag IS populated
        summary = dr.compute_duplicate_flag_summary(out)
        self.assertEqual(summary["total_duplicate_pairs"], 0)
        self.assertEqual(summary["flags"]["Address Match"], {"Yes": 0, "No": 0, "Unknown": 0})

    def test_no_duplicates_gives_empty_breakdown(self):
        df = pd.DataFrame([
            self._row("1", "a", "1 Main St", "X", "100", "0", "0", "0"),
            self._row("1", "b", "1 Main St", "X", "100", "0", "0", "0"),
        ])
        results = {"1": {"group": "1", "decision": "Not Duplicate", "archetype": "Coincidental Name Match",
                         "confidence": 7, "evidence_summary": "es", "sources": [], "is_error": False}}
        out = dr.build_output_df(df, results)
        summary = dr.compute_duplicate_flag_summary(out)
        self.assertEqual(summary["total_duplicate_pairs"], 0)

    def test_both_master_source_other_counts_pairs_not_records(self):
        df = pd.DataFrame([
            # Pair 1: both Hotwire -- not "both Other"
            self._row("1", "a", "1 Main St", "X", "100", "1", "0", "0"),
            self._row("1", "b", "1 Main St", "X", "100", "1", "0", "0"),
            # Pair 2: both no DB flags set -- both fall back to "Other"
            self._row("2", "c", "1 Main St", "X", "50", "0", "0", "0"),
            self._row("2", "d", "2 Other Ave", "Y", "500", "0", "0", "0"),
            # Pair 3: one Other, one Hotwire -- must not count as "both Other"
            self._row("3", "e", "9 Ninth St", "Z", "10", "0", "0", "0"),
            self._row("3", "f", "9 Ninth St", "Z", "10", "1", "0", "0"),
        ])
        results = {gid: {"group": gid, "decision": "Duplicate", "archetype": "Separate Buildings",
                         "confidence": 7, "evidence_summary": "es", "sources": [], "is_error": False}
                   for gid in ("1", "2", "3")}
        out = dr.build_output_df(df, results)
        summary = dr.compute_duplicate_flag_summary(out)
        self.assertEqual(summary["total_duplicate_pairs"], 3)
        self.assertEqual(summary["both_master_source_other"], 1)

    def test_same_master_source_by_value_breaks_out_each_source(self):
        df = pd.DataFrame([
            # Pair 1: both Hotwire
            self._row("1", "a", "1 Main St", "X", "100", "1", "0", "0"),
            self._row("1", "b", "1 Main St", "X", "100", "1", "0", "0"),
            # Pair 2: both CoStar
            self._row("2", "c", "1 Main St", "X", "100", "0", "1", "0"),
            self._row("2", "d", "1 Main St", "X", "100", "0", "1", "0"),
            # Pair 3: both First American
            self._row("3", "e", "1 Main St", "X", "100", "0", "0", "1"),
            self._row("3", "f", "1 Main St", "X", "100", "0", "0", "1"),
            # Pair 4: both Other -- must not count toward any specific source
            self._row("4", "g", "1 Main St", "X", "100", "0", "0", "0"),
            self._row("4", "h", "1 Main St", "X", "100", "0", "0", "0"),
            # Pair 5: sources differ -- must not count toward any specific source
            self._row("5", "i", "1 Main St", "X", "100", "1", "0", "0"),
            self._row("5", "j", "1 Main St", "X", "100", "0", "1", "0"),
        ])
        results = {gid: {"group": gid, "decision": "Duplicate", "archetype": "Separate Buildings",
                         "confidence": 7, "evidence_summary": "es", "sources": [], "is_error": False}
                   for gid in ("1", "2", "3", "4", "5")}
        out = dr.build_output_df(df, results)
        summary = dr.compute_duplicate_flag_summary(out)
        self.assertEqual(summary["same_master_source_by_value"],
                          {"Hotwire": 1, "CoStar": 1, "First American": 1})
        self.assertEqual(summary["both_master_source_other"], 1)

    def test_archetype_breakdown_scoped_to_duplicates_only(self):
        df = pd.DataFrame([
            self._row("1", "a", "1 Main St", "X", "100", "0", "0", "0"),
            self._row("1", "b", "1 Main St", "X", "100", "0", "0", "0"),
            self._row("2", "c", "1 Main St", "X", "100", "0", "0", "0"),
            self._row("2", "d", "1 Main St", "X", "100", "0", "0", "0"),
            self._row("3", "e", "1 Main St", "X", "100", "0", "0", "0"),
            self._row("3", "f", "1 Main St", "X", "100", "0", "0", "0"),
        ])
        results = {
            "1": {"group": "1", "decision": "Duplicate", "archetype": "Separate Buildings",
                  "confidence": 8, "evidence_summary": "es", "sources": [], "is_error": False},
            "2": {"group": "2", "decision": "Duplicate", "archetype": "Same Building",
                  "confidence": 8, "evidence_summary": "es", "sources": [], "is_error": False},
            "3": {"group": "3", "decision": "Not Duplicate", "archetype": "Parent/Child Mismatch",
                  "confidence": 8, "evidence_summary": "es", "sources": [], "is_error": False},
        }
        out = dr.build_output_df(df, results)
        summary = dr.compute_duplicate_flag_summary(out)
        self.assertEqual(summary["archetype_breakdown"], {"Separate Buildings": 1, "Same Building": 1})

    def test_distance_buckets_computed_from_distance_miles_column(self):
        df = pd.DataFrame([
            self._row("1", "a", "1 Main St", "X", "100", "0", "0", "0"),
            self._row("1", "b", "1 Main St", "X", "100", "0", "0", "0"),
            self._row("2", "c", "1 Main St", "X", "100", "0", "0", "0"),
            self._row("2", "d", "1 Main St", "X", "100", "0", "0", "0"),
        ])
        df["DISTANCE_MILES"] = ["0.03", "0.03", "1.2", "1.2"]
        results = {gid: {"group": gid, "decision": "Duplicate", "archetype": "Separate Buildings",
                         "confidence": 8, "evidence_summary": "es", "sources": [], "is_error": False}
                   for gid in ("1", "2")}
        out = dr.build_output_df(df, results)
        summary = dr.compute_duplicate_flag_summary(out)
        self.assertEqual(summary["distance_buckets"]["<0.05mi"], 1)
        self.assertEqual(summary["distance_buckets"]["0.5mi+"], 1)

    def test_distance_buckets_none_when_column_absent(self):
        df = pd.DataFrame([
            self._row("1", "a", "1 Main St", "X", "100", "0", "0", "0"),
            self._row("1", "b", "1 Main St", "X", "100", "0", "0", "0"),
        ])
        results = {"1": {"group": "1", "decision": "Duplicate", "archetype": "Separate Buildings",
                         "confidence": 8, "evidence_summary": "es", "sources": [], "is_error": False}}
        out = dr.build_output_df(df, results)
        summary = dr.compute_duplicate_flag_summary(out)
        self.assertIsNone(summary["distance_buckets"])


class PctHelperTests(unittest.TestCase):
    def test_formats_percentage_with_fraction(self):
        self.assertEqual(dr._pct(8, 10), "80% (8/10)")

    def test_rounds_to_nearest_whole_percent(self):
        self.assertEqual(dr._pct(1, 3), "33% (1/3)")

    def test_zero_total_does_not_divide_by_zero(self):
        self.assertEqual(dr._pct(0, 0), "0% (0/0)")

    def test_zero_count(self):
        self.assertEqual(dr._pct(0, 5), "0% (0/5)")

    def test_full_count(self):
        self.assertEqual(dr._pct(5, 5), "100% (5/5)")


class SummaryRenderingTests(unittest.TestCase):
    def test_print_summary_shows_family_archetypes_not_full_breakdown(self):
        summary = {
            "total_pairs": 4,
            "by_decision": {"Duplicate": 2, "Not Duplicate": 2, "Not Enough Info": 0},
            "average_confidence": 6.5,
            "archetype_counts": {
                "Parent/Child Mismatch": 1,
                "Separate Children Within One Complex": 1,
                "Coincidental Name Match": 2,  # should NOT appear in the printed summary
            },
            "errors": 0,
        }
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            dr.print_summary(summary)
        output = buf.getvalue()
        self.assertIn("Parent/Child Mismatch: 25% (1/4)", output)
        self.assertIn("Separate Children Within One Complex: 25% (1/4)", output)
        self.assertNotIn("Coincidental Name Match", output)
        self.assertIn("Duplicate: 50% (2/4)", output)

    def test_write_output_xlsx_summary_sheet_uses_percentages(self):
        df = pd.DataFrame([
            {"Group Number": "1", "RecordID": "a", "Address": "1 Main St",
             "Master_Property Name": "X", "Master_Units_50+": "100"},
            {"Group Number": "1", "RecordID": "b", "Address": "1 Main St",
             "Master_Property Name": "X", "Master_Units_50+": "100"},
        ])
        results = {"1": {"group": "1", "decision": "Duplicate", "archetype": "Same Building",
                         "confidence": 8, "evidence_summary": "es", "sources": [], "is_error": False}}
        out = dr.build_output_df(df, results)
        summary = dr.compute_summary(results)
        summary["duplicate_flags"] = dr.compute_duplicate_flag_summary(out)
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "results.xlsx")
            dr.write_output(out, summary, path)
            summary_df = pd.read_excel(path, sheet_name="Summary")
        metrics = dict(zip(summary_df["Metric"], summary_df["Value"]))
        self.assertEqual(metrics["Decision: Duplicate"], "100% (1/1)")
        self.assertIn("Both Master Source = Other", metrics)
        self.assertIn("Same Master Source = Hotwire", metrics)
        self.assertIn("Same Master Source = CoStar", metrics)
        self.assertIn("Same Master Source = First American", metrics)
        self.assertNotIn("Both In Hotwire", metrics)
        self.assertNotIn("Both In CoStar", metrics)
        self.assertNotIn("Both In First American", metrics)
        self.assertNotIn("Archetype: Same Building", metrics)  # no per-archetype rows anymore

    def test_same_master_source_by_value_rendered_in_print_and_write_output(self):
        df = pd.DataFrame([
            {"Group Number": "1", "RecordID": "a", "Address": "1 Main St",
             "Master_Property Name": "X", "Master_Units_50+": "100", "In HW": "1"},
            {"Group Number": "1", "RecordID": "b", "Address": "1 Main St",
             "Master_Property Name": "X", "Master_Units_50+": "100", "In HW": "1"},
        ])
        results = {"1": {"group": "1", "decision": "Duplicate", "archetype": "Same Building",
                         "confidence": 8, "evidence_summary": "es", "sources": [], "is_error": False}}
        out = dr.build_output_df(df, results)
        summary = dr.compute_summary(results)
        summary["duplicate_flags"] = dr.compute_duplicate_flag_summary(out)

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            dr.print_summary(summary)
        printed = buf.getvalue()
        self.assertIn("Same Master Source = Hotwire: 100% (1/1)", printed)
        self.assertIn("Same Master Source = CoStar: 0% (0/1)", printed)
        self.assertNotIn("Both In Hotwire", printed)

        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "results.xlsx")
            dr.write_output(out, summary, path)
            summary_df = pd.read_excel(path, sheet_name="Summary")
        metrics = dict(zip(summary_df["Metric"], summary_df["Value"]))
        self.assertEqual(metrics["Same Master Source = Hotwire"], "100% (1/1)")

    def test_summary_shows_inverted_flag_labels_and_new_sections(self):
        df = pd.DataFrame([
            {"Group Number": "1", "RecordID": "a", "Address": "1 Main St",
             "Master_Property Name": "X", "Master_Units_50+": "100", "DISTANCE_MILES": "0.03"},
            {"Group Number": "1", "RecordID": "b", "Address": "2 Other Ave",
             "Master_Property Name": "Y", "Master_Units_50+": "50", "DISTANCE_MILES": "0.03"},
        ])
        results = {"1": {"group": "1", "decision": "Duplicate", "archetype": "Same Building",
                         "confidence": 8, "evidence_summary": "es", "sources": [], "is_error": False}}
        out = dr.build_output_df(df, results)
        summary = dr.compute_summary(results)
        summary["duplicate_flags"] = dr.compute_duplicate_flag_summary(out)

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            dr.print_summary(summary)
        printed = buf.getvalue()
        self.assertIn("Address Mismatch: 100% (1/1)", printed)
        self.assertIn("Name Mismatch: 100% (1/1)", printed)
        self.assertIn("Unit Count Diff > 10: 100% (1/1)", printed)
        self.assertIn("Unit Count Diff 1-10: 0% (0/1)", printed)
        self.assertNotIn("Address Match:", printed)
        self.assertNotIn("Name Match:", printed)
        self.assertIn("Same Building: 100% (1/1)", printed)
        self.assertIn("<0.05mi: 100% (1/1)", printed)

        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "results.xlsx")
            dr.write_output(out, summary, path)
            summary_df = pd.read_excel(path, sheet_name="Summary")
        metrics = dict(zip(summary_df["Metric"], summary_df["Value"]))
        self.assertEqual(metrics["Address Mismatch"], "100% (1/1)")
        self.assertEqual(metrics["Archetype (Duplicates only): Same Building"], "100% (1/1)")
        self.assertEqual(metrics["Distance: <0.05mi"], "100% (1/1)")

    def test_print_and_write_output_do_not_crash_without_flag_summary(self):
        # Older call sites (or tests) that never set summary["duplicate_flags"] must still work.
        dr.print_summary({"total_pairs": 0, "by_decision": {l: 0 for l in dr.DECISION_LABELS},
                           "average_confidence": 0, "archetype_counts": {}, "errors": 0})


class FlaggedRecordIdTests(unittest.TestCase):
    def setUp(self):
        self.df = pd.DataFrame([
            {"Group Number": "10", "RecordID": "31599054", "Address": "2 Keimel Court"},
            {"Group Number": "10", "RecordID": "107054", "Address": "1 Metzger Drive"},
        ])

    def test_flagged_row_marked_the_other_left_blank(self):
        results = {"10": {"group": "10", "decision": "Not Duplicate", "archetype": "Mislabeled Property",
                           "confidence": 7, "evidence_summary": "es", "sources": [],
                           "flagged_record_id": "31599054", "is_error": False}}
        out = dr.build_output_df(self.df, results)
        flagged = out[out["RecordID"] == "31599054"].iloc[0]
        other = out[out["RecordID"] == "107054"].iloc[0]
        self.assertEqual(flagged["Flagged As Anomaly"], "Yes")
        self.assertEqual(other["Flagged As Anomaly"], "")

    def test_process_group_drops_flagged_id_not_in_pair(self):
        rows = [{"RecordID": "31599054", "Address": "2 Keimel Court"},
                {"RecordID": "107054", "Address": "1 Metzger Drive"}]

        def fake_research_pair(provider, client, record_a, record_b, distance, url_cache, model):
            return {"decision": "Not Duplicate", "archetype": "Mislabeled Property", "confidence": 7,
                    "evidence_summary": "es", "sources": [], "flagged_record_id": "Record A"}

        original = dr.research_pair
        dr.research_pair = fake_research_pair
        try:
            result = dr.process_group("anthropic", object(), "m", "10", rows, None, {})
        finally:
            dr.research_pair = original
        self.assertEqual(result["flagged_record_id"], "")

    def test_process_group_keeps_valid_flagged_id(self):
        rows = [{"RecordID": "31599054", "Address": "2 Keimel Court"},
                {"RecordID": "107054", "Address": "1 Metzger Drive"}]

        def fake_research_pair(provider, client, record_a, record_b, distance, url_cache, model):
            return {"decision": "Not Duplicate", "archetype": "Mislabeled Property", "confidence": 7,
                    "evidence_summary": "es", "sources": [], "flagged_record_id": "31599054"}

        original = dr.research_pair
        dr.research_pair = fake_research_pair
        try:
            result = dr.process_group("anthropic", object(), "m", "10", rows, None, {})
        finally:
            dr.research_pair = original
        self.assertEqual(result["flagged_record_id"], "31599054")


if __name__ == "__main__":
    unittest.main()
