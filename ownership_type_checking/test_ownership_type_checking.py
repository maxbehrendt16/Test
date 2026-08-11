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
    def test_tier3_only_override_is_downgraded(self):
        result = {
            "determined_type": "APT",
            "decision": "Override",
            "confidence": "High",
            "evidence_tier_used": "Tier 3",
            "reasoning": "A leasing website says apply now.",
            "sources": ["https://example.com"],
            "archetype_flag": "",
        }
        fixed = otc._enforce_tier3_override_guardrail(result)
        self.assertEqual(fixed["decision"], "Not Enough Info")
        self.assertEqual(fixed["confidence"], "Low")

    def test_tier1_override_is_left_alone(self):
        result = {
            "determined_type": "APT",
            "decision": "Override",
            "confidence": "High",
            "evidence_tier_used": "Tier 1",
            "reasoning": "County registry lists it as a rental apartment complex.",
            "sources": ["https://sunbiz.org/x"],
            "archetype_flag": "",
        }
        fixed = otc._enforce_tier3_override_guardrail(result)
        self.assertEqual(fixed["decision"], "Override")

    def test_override_with_matching_determined_type_becomes_confirmed(self):
        result = {"determined_type": "COA", "decision": "Override", "evidence_tier_used": "Tier 1"}
        fixed = otc._reconcile_decision_and_type("COA", result)
        self.assertEqual(fixed["decision"], "Confirmed")

    def test_confirmed_with_mismatched_type_is_corrected_to_db_label(self):
        result = {"determined_type": "APT", "decision": "Confirmed", "evidence_tier_used": "Tier 3"}
        fixed = otc._reconcile_decision_and_type("COA", result)
        self.assertEqual(fixed["determined_type"], "COA")

    def test_confirmed_edge_case_type_is_not_overwritten(self):
        result = {"determined_type": "Edge Case", "decision": "Confirmed", "evidence_tier_used": "Tier 2"}
        fixed = otc._reconcile_decision_and_type("COA", result)
        self.assertEqual(fixed["determined_type"], "Edge Case")

    def test_error_result_never_forces_override(self):
        result = otc._default_error_result("HOA", RuntimeError("boom"))
        self.assertEqual(result["decision"], "Not Enough Info")
        self.assertEqual(result["determined_type"], "HOA")


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
    def _result(self, decision, rules, signal_types, trigger_count=None):
        return {
            "decision": decision,
            "trigger_rules": rules,
            "trigger_types": signal_types,
            "trigger_count": trigger_count if trigger_count is not None else len(rules),
            "archetype_flag": "",
            "is_error": False,
        }

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


if __name__ == "__main__":
    unittest.main()
