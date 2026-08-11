"""
CLP Ownership Type Verification Tool.

For each CLP database property flagged as having a possibly-incorrect ownership type
(APT / COA / HOA), calls an LLM (with a web search tool enabled) to research the
property online and either confirm the existing DB label or determine the correct
type -- with reasoning, sourced evidence, and a confidence/archetype tag.

This mirrors the architecture of the prior duplicate-detection tool
(checkpointing keyed off a stable ID, dry-run/--limit mode, structured decisioning
via a forced tool call, deterministic post-hoc guardrails, "Not Enough Info" as a
first-class outcome) but the decision space here is simpler: three mutually
exclusive categories instead of open-ended duplicate matching. Uses OpenAI only
(the Responses API with the built-in web_search tool), per the same OPENAI_API_KEY
used for the prior property-type classification pass.

The six trigger rules (Type-Name Mismatch x3, Low/High Unit-to-Building Ratio, High
Floor Count, Has Fees, Has Leasing Info) are NOT expected in the input file -- the
tool computes which rule(s) each row matches itself, directly from the row's own
columns, before any research happens. See compute_triggers().

Usage:
    export OPENAI_API_KEY=...
    python ownership_type_checking.py --input properties.xlsx --output results.xlsx
    python ownership_type_checking.py --input properties.xlsx --output results.xlsx --batch-size 25   # dry run

Staged rollout (per spec Section 8.1): given the ~25.8K population, run this in
small, gradually-increasing batches rather than one continuous job. --batch-size
caps how many *new* (not-yet-checkpointed) properties get processed on a given
invocation -- re-running the same command with the same --output/--checkpoint
resumes where the last batch left off, keyed by property_id (RecordID), never by
row position, so batches can be re-run, expanded, or processed out of order without
double-processing. Suggested progression: 10-100 properties (spot-check in full),
then a few hundred, capping at 2,000 per run through the early stages.
"""
import argparse
import json
import os
import random
import re
import threading
import time
from pathlib import Path

import pandas as pd
import requests
from bs4 import BeautifulSoup

import openai
from openai import OpenAI

DEFAULT_MODEL = "gpt-4o"
MAX_SEARCHES_PER_PROPERTY = 8
MAX_TURNS = 6
URL_FETCH_TIMEOUT = 10
URL_FETCH_MAX_CHARS = 3000

DECISION_LABELS = ["Confirmed", "Override", "Not Enough Info", "Structural Edge Case"]
DETERMINED_TYPE_LABELS = ["APT", "COA", "HOA", "Edge Case"]
EVIDENCE_TIER_LABELS = ["Tier 1", "Tier 2", "Tier 3", "Mixed", "None"]
CONFIDENCE_LABELS = ["High", "Medium", "Low"]

# Canonical archetype strings from the spec's known-failure-mode catalog (Section 5) --
# kept as a suggested list in the tool description, not a hard enum, since Section 6
# allows a new free-text description when a property genuinely fits none of these.
ARCHETYPE_LABELS = [
    "Marketing Language Trap",
    "Lease-Up Phase",
    "Investor Bulk Ownership",
    "Mixed-Use Development",
    "Stale/Renamed",
    "Fee Miscoding",
    "Housing Cooperative",
    "Condo-Hotel/Timeshare",
    "Manufactured Home Community",
    "Senior/Student Housing Naming Convention",
]

SYSTEM_PROMPT = """You are a research assistant verifying property ownership-type records in a \
Community Lending Portfolio (CLP) database.

## Objective and critical framing

**The field you are verifying is `Master_Ownership Type` (APT / COA / HOA).** This property was \
selected for review because of a naming pattern, a unit-to-building ratio, a floor count, a fee \
value, or a leasing listing -- one or more automated trigger rules. You will be told which \
rule(s) fired and why, but **the trigger reason itself is never evidence.** It explains why this \
property is being looked at, not what the answer is. Do not let the trigger's implication bias \
your reading of ambiguous evidence toward confirming it -- that is circular reasoning, and it is \
the single biggest known failure mode of a prior automated pass at this same task.

A separate field, `Master_Property Type` (SFU/MDU), may appear in the data you're given. That \
field describes physical building structure and is a different concept entirely -- it is context \
only. Never confuse it with `Master_Ownership Type`, and never let it influence your decision \
about ownership type.

**Default posture: confirm the DB label.** Based on prior QC work, the overwhelming majority of \
flagged properties (roughly 97%) have a correct DB label -- only the naming pattern, ratio, or fee \
value that triggered review turned out not to indicate an actual misclassification. Only override \
the DB label when Tier 1 or Tier 2 evidence (see below) directly contradicts it, corroborated by a \
second independent source. When in doubt, don't change the label.

## Evidence hierarchy

**Tier 1 -- Legal/authoritative** (can independently justify an override, with one corroborating source):
- County recorder's Declaration of Condominium / CC&Rs / Master Deed language
- State business registry entity type (e.g. "Condominium Association Inc.", "Homeowners Association Inc.")
- County tax assessor's official use-code / property-class field

**Tier 2 -- Structural** (strong corroborating evidence, rarely sufficient alone):
- County GIS parcel map: single parcel for the whole complex (APT-leaning) vs. one parcel per unit (COA/HOA-leaning)
- Officially published unit/building count from the community's own site, HOA management company, or local news (not a listing aggregator)

**Tier 3 -- Corroborating only, never sufficient alone:**
- MLS/Zillow/Realtor.com listings, individual sale histories
- The property's own marketing/leasing website ("apply now," "leasing office," "floor plans")
- General web search snippets, forum mentions, local news human-interest coverage

**Rule: an override requires at least one Tier 1 or Tier 2 source, corroborated by a second \
independent source of Tier 1 or 2.** A single Tier 3 source is NEVER sufficient to override the DB \
label, no matter how confident it sounds. Tier 3 evidence can support a decision already justified \
by Tier 1/2, but cannot drive one on its own. This is enforced in code as well as here -- an \
Override decision resting only on Tier 3 evidence will be automatically downgraded to Not Enough \
Info regardless of what you submit, so there's no benefit to stretching Tier 3 evidence into an \
override.

**Corroboration means independence, not repetition.** Three aggregator sites all repeating the \
same "leasing office" claim is not independent corroboration -- it's one fact restated three times. \
Independent corroboration means two *different kinds* of source (e.g. a county registry entry AND \
a separate GIS parcel record).

## Known failure modes -- check every one of these before concluding Override

1. **Marketing language is not ownership structure (most important).** A "leasing office," "apply \
now," or "schedule a tour" listing is evidence of current rental *operation*, not legal ownership \
structure. A huge share of individually-deeded COA/HOA units are professionally managed and rented \
out by investors. Leasing-company presence must NEVER be treated as evidence a property is legally \
an APT. This is almost certainly the dominant cause of a prior tool's high error rate.
2. **Naming is a trigger, not evidence.** The property name is never evidence for or against a \
classification -- only the reason this property is being checked.
3. **Lease-up phase ambiguity.** New COA/HOA developments often run a leasing/reservation office \
before individual units are sold and recorded. They are legally COA/HOA (declaration recorded) but \
look identical online to a true rental APT. Check recording/construction dates against the absence \
of sales history before treating "no MLS history" as APT-confirming.
4. **Investor/institutional bulk ownership.** Some COA/HOA communities have most units owned by one \
investor/fund and rented as a block, sometimes marketed under a single leasing brand -- can look \
exactly like a single-owner APT. Parcel-level records (Tier 2) distinguish this from a true APT.
5. **Mixed-use/multi-component developments.** One branded development may contain multiple legally \
distinct components (e.g. an apartment tower plus a separate townhome HOA phase). Confirm which \
specific address/parcel the DB record refers to before classifying the whole named development.
6. **Stale or renamed properties.** If a name-based search returns nothing or inconsistent results, \
re-search by address and check whether the property has been renamed (common after condo \
conversions) before concluding evidence is unavailable.
7. **Structural edge cases outside APT/COA/HOA:** condo-hotels/timeshares (legal condo declaration \
but fractional/hotel-style operation), manufactured home communities (own the structure, lease the \
land), senior/student housing (colloquially "apartments" regardless of legal structure -- check \
structure independently, don't let the naming convention drive the call), and age-restricted/ \
master-planned communities using "Apartments" purely as a marketing brand for what's legally a COA. \
Housing cooperatives (individually-sold shares in a corporation, often "... Apartment Corp." or \
"... Apartments, Inc." in the Northeast) are a recurring pattern that looks like a rental APT from \
aggregator listings but is legally a COA-like structure -- flag with the Housing Cooperative \
archetype rather than treating "Apartments" in the name as confirming.
8. **Fee field miscoding.** Before treating fee presence as COA/HOA evidence, sanity-check it isn't \
a one-time deposit, a data-entry artifact, or a fee belonging to a different nearby property from a \
prior dedup issue in the CLP DB. If the fee amount/structure looks legitimate and recurring, treat \
it as Tier-2-ish supporting evidence, not decisive alone.

## Multi-trigger properties

If more than one trigger rule fired for this property, that is a meaningfully stronger prior that \
something may actually be wrong -- especially when the triggers are different *signal types* \
(e.g. one structural + one naming, rather than two naming rules that likely share the same root \
cause). For a multi-trigger property, do not settle for a quick Attempt-1 "Confirmed" on thin \
evidence -- run the full Attempt 2 targeted search before concluding. This does not lower the \
evidence bar for an override -- it only means you should look harder and be less willing to settle \
for Not Enough Info before trying the targeted searches.

## Verification process

1. **Attempt 1 -- broad search:** property name + address. Identify the development, note initial \
(mostly Tier 3) signals.
2. **Attempt 2 -- targeted search (only if Attempt 1 is inconclusive or contradicts the DB label):** \
county tax assessor/GIS parcel lookup for the specific address; county recorder search for a \
Declaration of Condominium/CC&Rs/HOA covenant; state business registry search for the governing \
entity's name and type.
3. **Decide:**
   - DB label confirmed by evidence found, or no contradicting evidence found -> **Confirmed**
   - Tier 1/2 evidence contradicts the DB label, corroborated by a second independent Tier 1/2 source -> **Override**
   - Evidence is mixed, thin, Tier-3-only, contradictory, or genuinely ambiguous even after Attempt 2 -> **Not Enough Info** (keep DB label, low confidence). When in doubt, don't change the label.
   - Property doesn't fit the three-way taxonomy at all (condo-hotel, manufactured home community, etc.) -> **Structural Edge Case**
4. Prefer a small number of well-targeted searches (2-4 is usually enough) over exhaustively \
crawling many pages. If a property cannot be resolved with confidence after Attempt 2, stop and \
label it Not Enough Info rather than digging indefinitely.

## Confidence

Use the full range -- if most properties in a batch land at High, that's a sign confidence is being \
inflated, not that the evidence was unusually clean across the board.
- **High:** Tier 1/2 evidence directly and specifically confirms this record, no remaining gap.
- **Medium:** real, relevant evidence found and leans toward the decision, but with a genuine gap, \
an unverified assumption, or reliance on well-corroborated Tier 3 evidence alone.
- **Low:** thin, mixed, Tier-3-only, or genuinely ambiguous evidence. This is the expected, normal \
outcome for most Not Enough Info calls -- not a score to avoid.

## Archetype flag

If a known failure-mode pattern applies (regardless of your decision), name it -- e.g. "Marketing \
Language Trap", "Lease-Up Phase", "Investor Bulk Ownership", "Mixed-Use Development", \
"Stale/Renamed", "Fee Miscoding", "Housing Cooperative", "Condo-Hotel/Timeshare", "Manufactured \
Home Community", "Senior/Student Housing Naming Convention". Combine multiple with "; " if more \
than one applies. Leave empty if none apply. Only write a new free-text label when a property \
genuinely fits none of these, phrased as a reusable pattern description, not pair-specific detail.

## Final answer

Once research is complete, call the submit_assessment tool exactly once with your final conclusion \
for this property. Do not call it before you're done researching, and do not just describe your \
answer in plain text -- the tool call is the only way your answer is recorded.
"""

SUBMIT_TOOL_DESCRIPTION = (
    "Submit the final ownership-type verification decision for this property. "
    "Call exactly once, after research is complete."
)

SUBMIT_SCHEMA = {
    "type": "object",
    "properties": {
        "determined_type": {
            "type": "string",
            "enum": DETERMINED_TYPE_LABELS,
            "description": (
                "Your concluded ownership type: the same value as the DB label (Confirmed), the "
                "corrected value (Override), or 'Edge Case' when the property doesn't fit the "
                "three-way taxonomy (Structural Edge Case decision)."
            ),
        },
        "decision": {
            "type": "string",
            "enum": DECISION_LABELS,
        },
        "confidence": {
            "type": "string",
            "enum": CONFIDENCE_LABELS,
        },
        "evidence_tier_used": {
            "type": "string",
            "enum": EVIDENCE_TIER_LABELS,
            "description": "The strongest tier of evidence actually relied on. 'None' if no relevant evidence was found at all.",
        },
        "reasoning": {
            "type": "string",
            "description": (
                "STRICT LIMIT: 1-2 sentences, plain language -- this is read at scale, not a "
                "research memo. State the specific facts found and how they support the decision."
            ),
        },
        "sources": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Specific URLs or named sources used. Empty list if none.",
        },
        "archetype_flag": {
            "type": "string",
            "description": (
                "One or more of the canonical failure-mode tags (semicolon-separated if more than "
                "one), or a new short reusable pattern description if none fit. Empty string if "
                "no archetype applies."
            ),
        },
    },
    "required": ["determined_type", "decision", "confidence", "evidence_tier_used", "reasoning", "sources", "archetype_flag"],
    "additionalProperties": False,
}

OPENAI_WEB_SEARCH_TOOL = {"type": "web_search"}
OPENAI_SUBMIT_TOOL = {
    "type": "function",
    "name": "submit_assessment",
    "description": SUBMIT_TOOL_DESCRIPTION,
    "parameters": SUBMIT_SCHEMA,
    "strict": True,
}

RETRYABLE_OPENAI_ERRORS = (
    openai.RateLimitError,
    openai.APIConnectionError,
    openai.InternalServerError,
    openai.APITimeoutError,
)


def call_openai_with_backoff(client, **kwargs):
    delay = 2.0
    last_err = None
    for attempt in range(5):
        try:
            return client.responses.create(**kwargs)
        except RETRYABLE_OPENAI_ERRORS as e:
            last_err = e
            sleep_for = delay * (2 ** attempt) + random.uniform(0, 1)
            print(f"    API error ({e.__class__.__name__}), retrying in {sleep_for:.1f}s...", flush=True)
            time.sleep(sleep_for)
    raise last_err


# --- Trigger rule computation (Section 3 of the spec) -----------------------------------
#
# The input file does not arrive with trigger labels attached -- these six rules are
# computed here, directly from each row's own columns, before any research happens.

NAME_APT_KEYWORDS = ("apartment", "apt", "flats", "flat", "lofts", "loft")
NAME_HOA_KEYWORDS = ("homeowner", "hoa")
NAME_COA_KEYWORDS = ("condo", "coa")

SIGNAL_NAMING = "Naming"
SIGNAL_STRUCTURAL = "Structural"
SIGNAL_GOVERNANCE = "Governance-Billing"
SIGNAL_MARKETING = "Marketing"


def _norm_text(value) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    return str(value).strip()


def _parse_number(value):
    try:
        if value is None or (isinstance(value, float) and pd.isna(value)) or str(value).strip() == "":
            return None
        return float(str(value).replace(",", "").replace("$", ""))
    except (TypeError, ValueError):
        return None


def _contains_any(text: str, keywords) -> bool:
    lowered = text.lower()
    return any(kw in lowered for kw in keywords)


def _first_present(row: dict, *keys):
    """First non-null/non-empty value among the given keys, or None."""
    for key in keys:
        val = row.get(key)
        num = _parse_number(val)
        if num is not None:
            return num
    return None


def compute_unit_building_ratio(row: dict):
    """Returns (ratio_or_None, note). Falls back from the _50+ fields to _20+ when null,
    per Section 3. Cross-checks against Building Count Bin only to surface a note for the
    model's context -- it never gates whether a rule fires."""
    units = _first_present(row, "Master_Units_50+", "Master_Units_20+")
    buildings = _first_present(row, "Master_Building Count_50+", "Master_Building Count_20+")
    if units is None or buildings is None or buildings == 0:
        return None, None
    ratio = units / buildings
    note = None
    bin_value = _norm_text(row.get("Building Count Bin"))
    if bin_value:
        note = f"Building Count Bin on file: '{bin_value}' (sanity cross-check against computed ratio)."
    return ratio, note


def compute_triggers(row: dict) -> list:
    """Returns a list of {'rule': str, 'signal_type': str} dicts for every one of the six
    trigger rules this property matches. All six are checks against Master_Ownership Type,
    never Master_Property Type (Section 2's critical framing)."""
    ownership = _norm_text(row.get("Master_Ownership Type")).upper()
    name = _norm_text(row.get("Master_Property Name"))
    triggers = []

    if ownership != "APT" and _contains_any(name, NAME_APT_KEYWORDS):
        triggers.append({"rule": "Type-Name Mismatch (APT)", "signal_type": SIGNAL_NAMING})
    if ownership != "HOA" and _contains_any(name, NAME_HOA_KEYWORDS):
        triggers.append({"rule": "Type-Name Mismatch (HOA)", "signal_type": SIGNAL_NAMING})
    if ownership != "COA" and _contains_any(name, NAME_COA_KEYWORDS):
        triggers.append({"rule": "Type-Name Mismatch (COA)", "signal_type": SIGNAL_NAMING})

    ratio, _ = compute_unit_building_ratio(row)
    if ratio is not None:
        if ownership != "HOA" and ratio < 5:
            triggers.append({"rule": "Low Unit-to-Building Ratio", "signal_type": SIGNAL_STRUCTURAL})
        if ownership not in ("COA", "APT") and ratio > 1:
            triggers.append({"rule": "High Unit-to-Building Ratio", "signal_type": SIGNAL_STRUCTURAL})

    floor_count = _parse_number(row.get("Master_Floor Count"))
    if floor_count is not None and ownership not in ("APT", "COA") and floor_count > 3:
        triggers.append({"rule": "High Floor Count", "signal_type": SIGNAL_STRUCTURAL})

    # "is not null" per the spec, with a zero value treated as "no fee on file" rather than
    # a real fee -- a $0 fee doesn't carry the governance signal a real recurring fee does.
    fee = _parse_number(row.get("Master_Monthly Association Fees"))
    if ownership not in ("HOA", "COA") and fee is not None and fee != 0:
        triggers.append({"rule": "Has Fees", "signal_type": SIGNAL_GOVERNANCE})

    leasing_present = any(
        _norm_text(row.get(col)) for col in ("Leasing Company", "Leasing Company Contact Name", "Leasing Company Phone")
    )
    if ownership != "APT" and leasing_present:
        triggers.append({"rule": "Has Leasing Info", "signal_type": SIGNAL_MARKETING})

    return triggers


def summarize_trigger_types(triggers: list) -> list:
    """Unique signal types represented, in first-seen order -- used to distinguish
    corroborating overlap (different signal types) from redundant overlap (same type,
    e.g. two naming rules) per Section 3.1."""
    seen = []
    for t in triggers:
        if t["signal_type"] not in seen:
            seen.append(t["signal_type"])
    return seen


# --- Building the research prompt --------------------------------------------------------

def fetch_url_cached(url: str, cache: dict) -> str:
    if url in cache:
        return cache[url]
    text = None
    try:
        resp = requests.get(url, timeout=URL_FETCH_TIMEOUT, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        for tag in soup(["script", "style"]):
            tag.decompose()
        text = " ".join(soup.get_text(separator=" ").split())[:URL_FETCH_MAX_CHARS]
    except Exception as e:
        text = None
        print(f"    Could not pre-fetch {url}: {e}", flush=True)
    cache[url] = text
    return text


# Only these mapped columns (Section 10.1) are shown to the model for reasoning -- the tool
# reads the full row for logging/output purposes, but reasoning is scoped to this subset so
# a stray column in the ~170-column export can't quietly influence the decision.
REASONING_FIELDS = [
    ("RecordID", "Property ID"),
    ("Address", "Address"),
    ("Master_Property Name", "Property Name"),
    ("Master_Ownership Type", "DB-listed Ownership Type (the field being verified)"),
    ("Master_Property Type", "Physical Structure Type (context only -- NOT the field being verified)"),
    ("Master_Units_50+", "Unit Count (50+ threshold)"),
    ("Master_Units_20+", "Unit Count (20+ threshold, fallback)"),
    ("Master_Building Count_50+", "Building Count (50+ threshold)"),
    ("Master_Building Count_20+", "Building Count (20+ threshold, fallback)"),
    ("Building Count Bin", "Building Count Bin (sanity cross-check)"),
    ("Master_Floor Count", "Floor Count"),
    ("Master_Monthly Association Fees", "Monthly Association Fees"),
    ("Leasing Company", "Leasing Company"),
    ("Leasing Company Contact Name", "Leasing Company Contact"),
    ("Leasing Company Phone", "Leasing Company Phone"),
    ("Owner", "Owner"),
    ("Cleaned Owner", "Cleaned Owner"),
    ("Property Manager", "Property Manager"),
    ("Cleaned Property Manager", "Cleaned Property Manager"),
    ("Developer Name", "Developer Name"),
    ("Cleaned Developer", "Cleaned Developer"),
    ("Master_Senior Flag", "Senior Flag"),
    ("Master_Student Flag", "Student Flag"),
    ("Master_Gated HOA Flag", "Gated HOA Flag"),
    ("Bulk Flag", "Bulk Ownership Flag"),
    ("Bulk Package Type", "Bulk Package Type"),
    ("% Bulk Overall", "% Bulk Overall"),
    ("LLM_Property Name", "Prior verified research -- Property Name"),
    ("LLM_Property URL", "Prior verified research -- Property URL"),
    ("LLM_Address", "Prior verified research -- Address"),
    ("LLM_Property Contact #1", "Prior verified research -- Contact #1"),
    ("LLM_Monthly HOA/COA fees", "Prior verified research -- Monthly HOA/COA fees"),
    ("Consolidated Macro Amenities", "Amenities"),
    ("Consolidated Luxury Amenities Count", "Luxury Amenities Count"),
]


def format_property(row: dict, triggers: list, url_cache: dict) -> str:
    lines = [f"### Property (RecordID: {row.get('RecordID', '')})"]
    url_value = None
    for col, label in REASONING_FIELDS:
        value = row.get(col)
        if value is None or (isinstance(value, float) and pd.isna(value)) or _norm_text(value) == "":
            continue
        lines.append(f"- {label}: {value}")
        if col == "LLM_Property URL":
            url_value = str(value)
    if url_value:
        fetched = fetch_url_cached(url_value, url_cache)
        if fetched:
            lines.append(f"- Pre-fetched content from prior-research URL {url_value} (verify it actually belongs to this property):")
            lines.append(f"  \"{fetched}\"")
    _, ratio_note = compute_unit_building_ratio(row)
    if ratio_note:
        lines.append(f"- {ratio_note}")

    lines.append("")
    lines.append(f"### Trigger rule(s) that flagged this property for review ({len(triggers)} total)")
    if triggers:
        for t in triggers:
            lines.append(f"- {t['rule']} (signal type: {t['signal_type']})")
    else:
        lines.append("- (none computed -- verify the DB label on general merits)")
    multi_trigger_types = summarize_trigger_types(triggers)
    if len(multi_trigger_types) > 1:
        lines.append(
            f"This property has {len(triggers)} trigger(s) spanning {len(multi_trigger_types)} distinct "
            f"signal types ({', '.join(multi_trigger_types)}) -- treat this as an elevated prior and run "
            f"the full Attempt 2 targeted search rather than settling for a quick Attempt-1 Confirmed."
        )
    lines.append(
        "Remember: the trigger rule(s) above explain why this property is being checked, not what the "
        "answer is. They are never themselves evidence for or against the DB label."
    )
    return "\n".join(lines)


def build_user_message(row: dict, triggers: list, url_cache: dict) -> str:
    return (
        "Research the following property and verify its Master_Ownership Type against the evidence "
        "hierarchy and process in your instructions.\n\n" + format_property(row, triggers, url_cache)
    )


def find_function_call(output_items, name):
    for item in output_items:
        if getattr(item, "type", None) == "function_call" and item.name == name:
            return item
    return None


def extract_openai_sources(output_items):
    sources = []
    for item in output_items:
        if getattr(item, "type", None) != "message":
            continue
        for part in getattr(item, "content", []) or []:
            for annotation in getattr(part, "annotations", []) or []:
                url = getattr(annotation, "url", None)
                if url:
                    sources.append(url)
    return sources


def research_property(client, row: dict, triggers: list, url_cache: dict, model: str) -> dict:
    input_items = [{"role": "user", "content": build_user_message(row, triggers, url_cache)}]
    tools = [OPENAI_WEB_SEARCH_TOOL, OPENAI_SUBMIT_TOOL]
    previous_response_id = None
    searched_sources = []

    for turn in range(MAX_TURNS):
        is_last_turn = turn == MAX_TURNS - 1
        tool_choice = {"type": "function", "name": "submit_assessment"} if is_last_turn else "auto"
        kwargs = dict(
            model=model,
            instructions=SYSTEM_PROMPT,
            input=input_items,
            tools=tools,
            tool_choice=tool_choice,
        )
        if previous_response_id:
            kwargs["previous_response_id"] = previous_response_id
        response = call_openai_with_backoff(client, **kwargs)
        previous_response_id = response.id
        searched_sources.extend(extract_openai_sources(response.output))

        submit_call = find_function_call(response.output, "submit_assessment")
        if submit_call:
            result = json.loads(submit_call.arguments)
            if not result.get("sources"):
                result["sources"] = sorted(set(searched_sources))
            return result

        input_items = [{
            "role": "user",
            "content": "Please call submit_assessment now with your final conclusion based on the research so far.",
        }]

    raise RuntimeError("Model did not produce a submit_assessment call within the turn budget")


# --- Deterministic guardrails --------------------------------------------------------------
# These mirror the prior dedup tool's approach: prose instructions alone haven't reliably
# stopped certain failure patterns, so the most safety-critical rules in the spec are also
# enforced in code as a backstop, not just requested in the prompt.

def _enforce_tier3_override_guardrail(result: dict) -> dict:
    """Section 4's rule -- 'a single Tier 3 source is never sufficient to override the DB
    label' -- restated as code. An Override resting on Tier 3-only evidence is downgraded to
    Not Enough Info rather than trusted at face value."""
    if result.get("decision") != "Override":
        return result
    if result.get("evidence_tier_used") != "Tier 3":
        return result
    result = dict(result)
    original = result.get("reasoning", "")
    result["decision"] = "Not Enough Info"
    result["confidence"] = "Low"
    result["reasoning"] = (
        f"Automatically downgraded: the model concluded Override on Tier 3 evidence alone, which per "
        f"the evidence hierarchy can never independently justify a label change. Original reasoning: {original}"
    )
    return result


def _reconcile_decision_and_type(db_type: str, result: dict) -> dict:
    """Self-contradiction check, mirroring the dedup tool's hard consistency check: an Override
    whose determined_type matches the DB label isn't actually an override (fix to Confirmed), and
    a Confirmed whose determined_type differs from the DB label is a direct contradiction (fix
    determined_type back to the DB label -- Confirmed means the DB label stands)."""
    result = dict(result)
    decision = result.get("decision")
    determined = result.get("determined_type")
    if decision == "Override" and determined == db_type:
        result["decision"] = "Confirmed"
    elif decision == "Confirmed" and determined not in (db_type, "Edge Case"):
        result["determined_type"] = db_type
    return result


def _default_error_result(db_type: str, error: Exception) -> dict:
    """Any exception, timeout, or unparseable model output defaults to Not Enough Info and
    keeps the existing DB label -- never a forced override, per Section 8."""
    return {
        "determined_type": db_type,
        "decision": "Not Enough Info",
        "confidence": "Low",
        "evidence_tier_used": "None",
        "reasoning": f"{error.__class__.__name__}: {error}",
        "sources": [],
        "archetype_flag": "Processing error",
    }


def process_property(client, model: str, row: dict, url_cache: dict) -> dict:
    property_id = str(row.get("RecordID", ""))
    db_type = _norm_text(row.get("Master_Ownership Type")).upper()
    triggers = compute_triggers(row)
    trigger_types = summarize_trigger_types(triggers)

    try:
        result = research_property(client, row, triggers, url_cache, model)
        if result.get("decision") not in DECISION_LABELS:
            raise ValueError(f"Model returned invalid decision label: {result.get('decision')!r}")
        if result.get("determined_type") not in DETERMINED_TYPE_LABELS:
            raise ValueError(f"Model returned invalid determined_type: {result.get('determined_type')!r}")
        result = _enforce_tier3_override_guardrail(result)
        result = _reconcile_decision_and_type(db_type, result)
        is_error = False
    except Exception as e:
        result = _default_error_result(db_type, e)
        is_error = True

    return {
        "property_id": property_id,
        "db_listed_type": db_type,
        "trigger_rules": [t["rule"] for t in triggers],
        "trigger_count": len(triggers),
        "trigger_types": trigger_types,
        "determined_type": result.get("determined_type", db_type),
        "decision": result.get("decision", "Not Enough Info"),
        "confidence": result.get("confidence", "Low"),
        "evidence_tier_used": result.get("evidence_tier_used", "None"),
        "reasoning": result.get("reasoning", ""),
        "sources": result.get("sources", []),
        "archetype_flag": result.get("archetype_flag", ""),
        "is_error": is_error,
    }


# --- I/O, checkpointing, batching -----------------------------------------------------------

def load_input(path: str) -> pd.DataFrame:
    if path.lower().endswith((".xlsx", ".xls")):
        df = pd.read_excel(path, dtype=str)
    else:
        df = pd.read_csv(path, dtype=str, encoding="utf-8-sig")
    if "RecordID" not in df.columns:
        raise SystemExit("Input is missing required 'RecordID' column.")
    return df


def load_checkpoint(path: Path) -> dict:
    results = {}
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                record = json.loads(line)
                results[record["property_id"]] = record
    return results


def append_checkpoint(path: Path, record: dict, lock: threading.Lock):
    with lock:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")


ILLEGAL_XLSX_CHARS_RE = re.compile(r"[\x00-\x08\x0B\x0C\x0E-\x1F]")


def sanitize_for_excel(value):
    if isinstance(value, str):
        return ILLEGAL_XLSX_CHARS_RE.sub("", value)
    return value


def sanitize_df_for_excel(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    for col in df.columns:
        if df[col].dtype == object:
            df[col] = df[col].map(sanitize_for_excel)
    return df


def build_output_df(df: pd.DataFrame, results_by_id: dict) -> pd.DataFrame:
    out = df.copy()
    out["Property ID"] = out["RecordID"].astype(str)
    for col in ["DB Listed Type", "Trigger Rule(s)", "Trigger Count", "Trigger Types", "Determined Type",
                "Decision", "Confidence", "Evidence Tier Used", "Reasoning", "Sources", "Archetype Flag"]:
        out[col] = pd.Series([""] * len(out), index=out.index, dtype=object)

    for idx in out.index:
        result = results_by_id.get(str(out.at[idx, "RecordID"]))
        if not result:
            continue
        out.at[idx, "DB Listed Type"] = result["db_listed_type"]
        out.at[idx, "Trigger Rule(s)"] = "; ".join(result["trigger_rules"])
        out.at[idx, "Trigger Count"] = result["trigger_count"]
        out.at[idx, "Trigger Types"] = "; ".join(result["trigger_types"])
        out.at[idx, "Determined Type"] = result["determined_type"]
        out.at[idx, "Decision"] = result["decision"]
        out.at[idx, "Confidence"] = result["confidence"]
        out.at[idx, "Evidence Tier Used"] = result["evidence_tier_used"]
        out.at[idx, "Reasoning"] = result["reasoning"]
        out.at[idx, "Sources"] = "; ".join(result.get("sources", []))
        out.at[idx, "Archetype Flag"] = result["archetype_flag"]
    return out


def compute_summary(results_by_id: dict) -> dict:
    total = len(results_by_id)
    by_decision = {label: 0 for label in DECISION_LABELS}
    archetype_counts = {}
    by_rule = {}          # rule -> {"total": n, "override": n}
    by_trigger_count = {}  # count -> {"total": n, "override": n}
    by_signal_type = {}    # signal type -> {"total": n, "override": n}
    errors = 0

    for result in results_by_id.values():
        by_decision[result["decision"]] = by_decision.get(result["decision"], 0) + 1
        if result.get("archetype_flag"):
            for tag in result["archetype_flag"].split(";"):
                tag = tag.strip()
                if tag:
                    archetype_counts[tag] = archetype_counts.get(tag, 0) + 1
        if result.get("is_error"):
            errors += 1

        is_override = result["decision"] == "Override"
        for rule in result["trigger_rules"]:
            entry = by_rule.setdefault(rule, {"total": 0, "override": 0})
            entry["total"] += 1
            if is_override:
                entry["override"] += 1
        for stype in result["trigger_types"]:
            entry = by_signal_type.setdefault(stype, {"total": 0, "override": 0})
            entry["total"] += 1
            if is_override:
                entry["override"] += 1
        count_bucket = "3+" if result["trigger_count"] >= 3 else str(result["trigger_count"])
        entry = by_trigger_count.setdefault(count_bucket, {"total": 0, "override": 0})
        entry["total"] += 1
        if is_override:
            entry["override"] += 1

    return {
        "total_properties": total,
        "by_decision": by_decision,
        "archetype_counts": archetype_counts,
        "override_rate_by_rule": by_rule,
        "override_rate_by_trigger_count": by_trigger_count,
        "override_rate_by_signal_type": by_signal_type,
        "errors": errors,
    }


EXPECTED_OVERRIDE_RATE = 0.0285  # ~733 / 25,800 per the spec's corrected scoping (Section 1)


def _pct(count: int, total: int) -> str:
    if not total:
        return f"0% ({count}/{total})"
    return f"{round(count / total * 100)}% ({count}/{total})"


def print_summary(label: str, summary: dict):
    total = summary["total_properties"]
    print(f"\n=== {label} ({total} properties) ===")
    for decision, count in summary["by_decision"].items():
        print(f"  {decision}: {_pct(count, total)}")
    overrides = summary["by_decision"].get("Override", 0)
    override_rate = overrides / total if total else 0
    print(f"  Overall override rate: {_pct(overrides, total)} (expected ~{EXPECTED_OVERRIDE_RATE * 100:.2f}%)")
    if total and override_rate > EXPECTED_OVERRIDE_RATE * 3:
        print(
            "  WARNING: override rate is running dramatically higher than the expected ~2.85% -- "
            "this likely means the tool is repeating the prior tool's mistake of over-trusting "
            "Tier 3 marketing evidence. Tighten the prompt before scaling up (Section 9 QC step)."
        )
    print(f"  Errors: {_pct(summary['errors'], total)}")

    if summary["archetype_counts"]:
        print("  Archetype flags:")
        for tag, count in sorted(summary["archetype_counts"].items(), key=lambda kv: -kv[1]):
            print(f"    {tag}: {_pct(count, total)}")

    print("  Override rate by trigger rule:")
    for rule, counts in sorted(summary["override_rate_by_rule"].items()):
        print(f"    {rule}: {_pct(counts['override'], counts['total'])}")

    print("  Override rate by signal type (sanity check -- Structural should run higher than Naming/Marketing):")
    for stype, counts in sorted(summary["override_rate_by_signal_type"].items()):
        print(f"    {stype}: {_pct(counts['override'], counts['total'])}")
    structural = summary["override_rate_by_signal_type"].get("Structural")
    naming = summary["override_rate_by_signal_type"].get("Naming")
    if structural and naming and structural["total"] and naming["total"]:
        structural_rate = structural["override"] / structural["total"]
        naming_rate = naming["override"] / naming["total"]
        if structural_rate < naming_rate:
            print(
                "  WARNING: Structural override rate is NOT higher than Naming override rate -- "
                "this is the opposite of what the spec expects if the tool is weighting evidence "
                "correctly (naming/leasing triggers should be least reliable)."
            )

    print("  Override rate by trigger count:")
    for count_bucket, counts in sorted(summary["override_rate_by_trigger_count"].items()):
        print(f"    {count_bucket} trigger(s): {_pct(counts['override'], counts['total'])}")


def write_output(out_df: pd.DataFrame, output_path: str):
    out_df = sanitize_df_for_excel(out_df)
    if output_path.lower().endswith((".xlsx", ".xls")):
        out_df.to_excel(output_path, index=False)
    else:
        out_df.to_csv(output_path, index=False)


def main():
    parser = argparse.ArgumentParser(description="Verify CLP ownership-type records via an LLM.")
    parser.add_argument("--input", required=True, help="Path to input CSV or XLSX file")
    parser.add_argument("--output", required=True, help="Path to output CSV or XLSX file (never overwrites input)")
    parser.add_argument("--checkpoint", default=None, help="Checkpoint JSONL path (default: <output>.checkpoint.jsonl)")
    parser.add_argument("--batch-size", type=int, default=None,
                         help="Only process up to N new (not-yet-checkpointed) properties this run (staged rollout)")
    parser.add_argument("--limit", type=int, default=None, help="Alias for --batch-size")
    parser.add_argument("--concurrency", type=int, default=1, help="Number of properties to research in parallel (default: 1)")
    parser.add_argument("--model", default=DEFAULT_MODEL, help=f"OpenAI model id (default: {DEFAULT_MODEL})")
    parser.add_argument("--restart", action="store_true", help="Ignore any existing checkpoint and reprocess everything")
    args = parser.parse_args()

    if str(Path(args.input).resolve()) == str(Path(args.output).resolve()):
        raise SystemExit("Output path must differ from input path.")
    if not os.environ.get("OPENAI_API_KEY"):
        raise SystemExit("Set the OPENAI_API_KEY environment variable.")

    batch_size = args.batch_size if args.batch_size is not None else args.limit
    checkpoint_path = Path(args.checkpoint) if args.checkpoint else Path(str(args.output) + ".checkpoint.jsonl")

    df = load_input(args.input)
    all_rows = df.to_dict("records")
    total = len(all_rows)
    print(f"Loaded {args.input}: {total} property row(s).", flush=True)

    results_by_id = {} if args.restart else load_checkpoint(checkpoint_path)
    if results_by_id:
        print(f"Resuming from checkpoint: {len(results_by_id)} property(ies) already completed.", flush=True)
    if args.restart and checkpoint_path.exists():
        checkpoint_path.unlink()

    pending = [row for row in all_rows if str(row.get("RecordID")) not in results_by_id]
    if batch_size is not None:
        pending = pending[:batch_size]
    print(f"{len(pending)} property(ies) to process this run.", flush=True)

    client = OpenAI()
    url_cache = {}
    checkpoint_lock = threading.Lock()
    done_count = 0
    batch_results = {}

    def run_one(row):
        result = process_property(client, args.model, row, url_cache)
        append_checkpoint(checkpoint_path, result, checkpoint_lock)
        return result

    if args.concurrency <= 1:
        for row in pending:
            result = run_one(row)
            results_by_id[result["property_id"]] = result
            batch_results[result["property_id"]] = result
            done_count += 1
            print(f"[{done_count}/{len(pending)}] {result['property_id']}: {result['decision']} "
                  f"({result['determined_type']}, {result['confidence']})", flush=True)
    else:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
            futures = {pool.submit(run_one, row): row for row in pending}
            for future in as_completed(futures):
                result = future.result()
                results_by_id[result["property_id"]] = result
                batch_results[result["property_id"]] = result
                done_count += 1
                print(f"[{done_count}/{len(pending)}] {result['property_id']}: {result['decision']} "
                      f"({result['determined_type']}, {result['confidence']})", flush=True)

    processed_ids = {str(row.get("RecordID")) for row in all_rows}
    cumulative_results = {pid: r for pid, r in results_by_id.items() if pid in processed_ids}
    out_df = build_output_df(df, cumulative_results)
    write_output(out_df, args.output)

    if batch_results:
        print_summary("This batch", compute_summary(batch_results))
    print_summary("Cumulative (all properties processed so far, this input file)", compute_summary(cumulative_results))
    print(f"\nResults written to {args.output}")
    print(f"Checkpoint: {checkpoint_path}")


if __name__ == "__main__":
    main()
