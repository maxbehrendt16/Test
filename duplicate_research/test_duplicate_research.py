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


class DuplicateFlagsTests(unittest.TestCase):
    def _row(self, group, record_id, address, name, units, hw, costar, fa):
        return {"Group Number": group, "RecordID": record_id, "Address": address,
                "Master_Property Name": name, "Master_Units_50+": units,
                "In HW": hw, "In Costar": costar, "In FA": fa}

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
        self.assertTrue((out["Unit Counts Within 10"] == "Yes").all())
        self.assertTrue((out["Both In Hotwire"] == "Yes").all())
        self.assertTrue((out["Both In CoStar"] == "Yes").all())
        self.assertTrue((out["Both In First American"] == "Yes").all())

    def test_mismatches_and_one_sided_db_membership(self):
        df = pd.DataFrame([
            self._row("1", "a", "1 Main St", "Vizcaya", "96", "1", "0", "1"),
            self._row("1", "b", "2 Other Ave", "Vizcaya Condo", "127", "0", "0", "0"),
        ])
        results = {"1": {"group": "1", "decision": "Duplicate", "archetype": "Separate Buildings",
                         "confidence": 6, "evidence_summary": "es", "sources": [], "is_error": False}}
        out = dr.build_output_df(df, results)
        self.assertTrue((out["Address Match"] == "No").all())
        self.assertTrue((out["Name Match"] == "No").all())
        self.assertTrue((out["Unit Counts Within 10"] == "No").all())  # |96-127| = 31 > 10
        self.assertTrue((out["Both In Hotwire"] == "No").all())  # only one side is 1
        self.assertTrue((out["Both In CoStar"] == "No").all())   # neither side is 1
        self.assertTrue((out["Both In First American"] == "No").all())  # only one side is 1

    def test_flags_blank_for_non_duplicate_decisions(self):
        df = pd.DataFrame([
            self._row("1", "a", "1 Main St", "X", "100", "1", "1", "1"),
            self._row("1", "b", "1 Main St", "X", "100", "1", "1", "1"),
        ])
        results = {"1": {"group": "1", "decision": "Not Duplicate", "archetype": "Coincidental Name Match",
                         "confidence": 7, "evidence_summary": "es", "sources": [], "is_error": False}}
        out = dr.build_output_df(df, results)
        for col in ["Address Match", "Name Match", "Unit Counts Within 10",
                    "Both In Hotwire", "Both In CoStar", "Both In First American"]:
            self.assertTrue((out[col] == "").all(), f"{col} should be blank for a non-Duplicate decision")

    def test_missing_source_columns_do_not_crash(self):
        # A dataset without In HW/In Costar/In FA/Master_Units_50+ at all (e.g. sample_pairs.csv).
        df = pd.DataFrame([
            {"Group Number": "1", "RecordID": "a", "Address": "1 Main St"},
            {"Group Number": "1", "RecordID": "b", "Address": "1 Main St"},
        ])
        results = {"1": {"group": "1", "decision": "Duplicate", "archetype": "Same Building",
                         "confidence": 8, "evidence_summary": "es", "sources": [], "is_error": False}}
        out = dr.build_output_df(df, results)
        self.assertTrue((out["Unit Counts Within 10"] == "").all())
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
