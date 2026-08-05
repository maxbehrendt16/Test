import json
import os
import tempfile
import unittest
from pathlib import Path

import pandas as pd

import duplicate_research as dr


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


if __name__ == "__main__":
    unittest.main()
