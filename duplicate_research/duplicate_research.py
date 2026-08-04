"""
CLP Duplicate Property Research Tool.

For each candidate duplicate pair in an input spreadsheet, calls an LLM (with a
web search tool enabled) to research both properties online and decide whether
they are a genuine database duplicate. See the spec this was built from for the
full research procedure and false-positive ruleset embedded in SYSTEM_PROMPT
below. Supports either Anthropic (Claude) or OpenAI as the backend — pick
whichever provider you have an API key for.

Usage:
    export ANTHROPIC_API_KEY=...      # if using Claude
    export OPENAI_API_KEY=...         # if using OpenAI
    python duplicate_research.py --input pairs.xlsx --output results.xlsx
    python duplicate_research.py --input pairs.csv --output results.csv --limit 10   # dry run

The provider is auto-detected from whichever API key env var is set. If both are
set, pass --provider explicitly. Model defaults to DEFAULT_MODELS[provider]; override
with --model.
"""
import argparse
import json
import os
import random
import threading
import time
from pathlib import Path

import pandas as pd
import requests
from bs4 import BeautifulSoup

import anthropic
import openai
from openai import OpenAI

DEFAULT_MODELS = {
    "anthropic": "claude-sonnet-5",
    "openai": "gpt-4o",
}
MAX_SEARCHES_PER_PAIR = 8
MAX_TURNS = 6
URL_FETCH_TIMEOUT = 10
URL_FETCH_MAX_CHARS = 3000

DECISION_LABELS = ["Duplicate", "Not Duplicate", "Not Enough Info"]

SYSTEM_PROMPT = """You are a research assistant for a real estate database team investigating \
candidate duplicate property pairs from a Community Lending Portfolio (CLP) database.

## Objective

You will be given two database records (a "pair") that a separate matching workflow flagged as a \
likely duplicate based on name, address, distance, and unit-count similarity. That similarity score \
is only a weak starting signal — it reflects surface-level similarity on those four inputs alone, \
not a validated duplicate. Your job is to independently research both properties online and decide \
whether they are genuinely the same property (a database duplicate) or two distinct properties that \
were incorrectly flagged as a match.

The default posture is skeptical of "Duplicate." The team would rather leave an ambiguous pair \
unresolved than wrongly remove a real, distinct property. For every pair, exhaustively check the pair \
against the False Positive Ruleset below — actively looking for reasons the two records are NOT the \
same property — before concluding that they are. A shared attribute already present in the database \
(name, unit count, address, property type) is a starting signal, never proof on its own. It is fine to \
conclude "Duplicate" when the evidence genuinely supports it, but that conclusion must survive having \
been checked against every applicable false-positive pattern first.

Never conclude Duplicate based solely on matching fields already present in the database — independent, \
external corroboration is required every time.

## Research procedure

For each pair:

1. Search each record independently — by property name AND by address separately (don't assume the \
database's name is correct). Look for: the property's own official website or HOA/condo association \
site; county property/tax records; real estate listing platforms (Zillow, Redfin, Realtor.com, Compass, \
Apartments.com, Homes.com); HOA/condo association directories; local news or developer press coverage.
2. If you find an authoritative stated total unit count, compare it against BOTH records, not just one. \
The test is whether that total is reasonably close to *both* paired records' unit counts. A total that \
closely matches only ONE of the two records, while the other record's count is substantially different, \
is NOT evidence that the two records are the same property.
3. Determine if there is one governing entity or two. Search for the legal HOA/condo association \
name(s) tied to each address.
4. Check geographic plausibility using the provided DISTANCE_MILES field (already computed — do not \
recalculate it). A large distance combined with different cities/states is a red flag pointing toward \
a coincidental match or data error, not a real duplicate.

Prefer a small number of well-targeted searches over exhaustively crawling many pages — 2-4 searches \
per record is usually enough if well chosen (property name + city, address alone, "[name] homeowners \
association", "[name] units"). If a pair cannot be resolved with confidence after reasonable searching, \
stop and label it Not Enough Info rather than continuing to dig indefinitely.

## False Positive Ruleset (check exhaustively before concluding "Duplicate")

1. **Parent/Child Mismatch** — one record refers to a specific building (e.g. "Building A") while the \
other encompasses the full multi-building complex. Check: large unit-count difference; building-specific \
detail in one name but not the other. Search "[complex name] [Building A]" for that building's own \
confirmed unit count.
2. **Separate Children Within One Complex** — two genuine peer buildings (e.g. "Building A" vs \
"Building B"), not a parent/child pair. Search "[complex name] buildings addresses" to confirm the \
complex is multi-building and get each building's real address/unit count. If both DB unit counts equal \
the exact same total, that's a stronger signal they represent the whole complex rather than two \
individually distinct buildings.
3. **Separate Property Types Within a Master Association** — a master complex comprised of separate \
sub-properties sharing a name but with different property types (e.g. a SFU/HOA section and a separate \
COA section under one community brand). First confirm whether a master association actually exists \
(search "[name] master association", "[name] community association", or the HOA/condo registry) — do \
not assume one exists just from similar names. A confirmed master association does NOT by itself mean \
the two records are the same entity — it may have separate COA/HOA sub-entities underneath with similar \
but not identical unit counts. Only after confirming whether a master association exists should you apply \
the unit-count test: a total close to *both* records points toward one entity (Mislabeled Property), \
while confirmed evidence of two separately named/managed sub-associations supports this archetype. The \
type mismatch alone is never sufficient.
4. **Coincidental Name Match** — two properties share name/unit-count/other factors purely coincidentally \
while being fundamentally different, unrelated developments. Search "[property name] [city 1]" and \
"[property name] [city 2]" independently — unrelated management companies/websites on each side support \
coincidence. A property-type/ownership mismatch alongside a matching name and unit count is a strong tell.
5. **Mislabeled Property** — two properties share a name, but one record's Property Name or type was \
copied from, or confused with, the other. Search the address directly (not the name) to find the \
property's real, independently confirmed name/type. If a single authoritative total is close to *both* \
paired records, that supports treating this as one real property duplicated with a mislabel, not two.
6. **Multi-Use Building** — a single building has multiple properties with different managers, possibly \
different property types (e.g. residential tower over separately-owned commercial/retail). Check: \
identical/near-identical address; different ownership type or drastically different unit counts at the \
same address; different property managers. Search "[building name] condo declaration" or "[address] \
residential commercial".

This list is not exhaustive — if a pair doesn't fit any of these but you find genuine evidence it isn't \
a match, describe the reasoning in your own words as a new archetype rather than forcing it into one of \
the categories above (note that it's a new archetype).

## Guardrails

- When a stated total unit count is found, it only counts as strong evidence of a single shared \
community when it is close to *both* paired records — not just one.
- When in doubt between two decisions, prefer the more conservative one (the one less likely to result \
in a real property being wrongly removed from the database).
- Do not fabricate or guess at sources — if a claim can't be tied to something actually found online, \
don't include it in the evidence summary.
- A pre-fetched URL from the database (labeled "Pre-fetched content") is a head start, not proof — verify \
it actually belongs to the property in question before relying on it.

## Decision labels

Use exactly one of: Duplicate, Not Duplicate, Not Enough Info.

## Final answer

Once you have completed your research, call the submit_assessment tool exactly once with your final \
conclusion for the pair as a whole (not per-record — both records in the pair share the same decision, \
archetype, confidence, evidence summary, and sources). Do not call it before you're done researching, \
and do not just describe your answer in plain text — the tool call is the only way your answer is \
recorded.
"""

SUBMIT_TOOL_DESCRIPTION = (
    "Submit the final duplicate-property research decision for this candidate pair. "
    "Call exactly once, after research is complete."
)

SUBMIT_SCHEMA = {
    "type": "object",
    "properties": {
        "decision": {
            "type": "string",
            "enum": DECISION_LABELS,
        },
        "archetype": {
            "type": "string",
            "description": (
                "Short free-text label for how the decision was reached. For 'Not Duplicate', "
                "generally one of the six false-positive ruleset categories (or a new one, noted "
                "as new, if none fit). For 'Duplicate', describe the nature of the match in your "
                "own words. For 'Not Enough Info', briefly describe what's missing."
            ),
        },
        "confidence": {
            "type": "integer",
            "minimum": 1,
            "maximum": 10,
            "description": "1 = pure guess, 10 = certain, directly confirmed by an authoritative source.",
        },
        "evidence_summary": {
            "type": "string",
            "description": "2-4 sentences explaining the finding in plain language, citing specific facts found.",
        },
        "sources": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Specific URLs or named sources used. Empty list if none.",
        },
    },
    "required": ["decision", "archetype", "confidence", "evidence_summary", "sources"],
    "additionalProperties": False,
}

# --- Anthropic tool shapes ---
ANTHROPIC_WEB_SEARCH_TOOL = {
    "type": "web_search_20250305",
    "name": "web_search",
    "max_uses": MAX_SEARCHES_PER_PAIR,
}
ANTHROPIC_SUBMIT_TOOL = {
    "name": "submit_assessment",
    "description": SUBMIT_TOOL_DESCRIPTION,
    "input_schema": SUBMIT_SCHEMA,
}

# --- OpenAI (Responses API) tool shapes ---
OPENAI_WEB_SEARCH_TOOL = {"type": "web_search"}
OPENAI_SUBMIT_TOOL = {
    "type": "function",
    "name": "submit_assessment",
    "description": SUBMIT_TOOL_DESCRIPTION,
    "parameters": SUBMIT_SCHEMA,
    "strict": True,
}

RETRYABLE_ANTHROPIC_ERRORS = (
    anthropic.RateLimitError,
    anthropic.APIConnectionError,
    anthropic.InternalServerError,
    anthropic.APITimeoutError,
)

RETRYABLE_OPENAI_ERRORS = (
    openai.RateLimitError,
    openai.APIConnectionError,
    openai.InternalServerError,
    openai.APITimeoutError,
)


def call_anthropic_with_backoff(client, **kwargs):
    delay = 2.0
    last_err = None
    for attempt in range(5):
        try:
            return client.messages.create(**kwargs)
        except RETRYABLE_ANTHROPIC_ERRORS as e:
            last_err = e
            sleep_for = delay * (2 ** attempt) + random.uniform(0, 1)
            print(f"    API error ({e.__class__.__name__}), retrying in {sleep_for:.1f}s...", flush=True)
            time.sleep(sleep_for)
    raise last_err


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


def fetch_url_cached(url: str, cache: dict) -> str | None:
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


INTERNAL_COLUMNS = {"_row_index"}


def format_record(row: dict, label: str, url_cache: dict) -> str:
    lines = [f"### {label} (RecordID: {row.get('RecordID', '')})"]
    url_value = None
    for key, value in row.items():
        if key in INTERNAL_COLUMNS or pd.isna(value) or value == "":
            continue
        lines.append(f"- {key}: {value}")
        if "url" in key.lower() and not url_value:
            url_value = str(value)
    if url_value:
        fetched = fetch_url_cached(url_value, url_cache)
        if fetched:
            lines.append(f"- Pre-fetched content from {url_value} (verify it actually belongs to this property):")
            lines.append(f"  \"{fetched}\"")
    return "\n".join(lines)


def build_user_message(record_a: dict, record_b: dict, distance: str, url_cache: dict) -> str:
    parts = [
        "Research the following candidate duplicate pair and determine whether the two records "
        "describe the same real property.",
        "",
        format_record(record_a, "Record A", url_cache),
        "",
        format_record(record_b, "Record B", url_cache),
        "",
        f"DISTANCE_MILES between the two records (already computed, do not recalculate): {distance}",
    ]
    return "\n".join(parts)


def find_tool_use(content_blocks, name):
    for block in content_blocks:
        if getattr(block, "type", None) == "tool_use" and block.name == name:
            return block
    return None


def extract_sources_from_search(content_blocks):
    sources = []
    for block in content_blocks:
        if getattr(block, "type", None) == "web_search_tool_result":
            result = getattr(block, "content", None)
            if isinstance(result, list):
                for item in result:
                    url = getattr(item, "url", None)
                    if url:
                        sources.append(url)
    return sources


def research_pair_anthropic(client, record_a: dict, record_b: dict, distance: str, url_cache: dict, model: str) -> dict:
    messages = [{"role": "user", "content": build_user_message(record_a, record_b, distance, url_cache)}]
    tools = [ANTHROPIC_WEB_SEARCH_TOOL, ANTHROPIC_SUBMIT_TOOL]
    searched_sources = []

    for turn in range(MAX_TURNS):
        is_last_turn = turn == MAX_TURNS - 1
        tool_choice = {"type": "tool", "name": "submit_assessment"} if is_last_turn else {"type": "auto"}
        response = call_anthropic_with_backoff(
            client,
            model=model,
            max_tokens=4096,
            system=SYSTEM_PROMPT,
            messages=messages,
            tools=tools,
            tool_choice=tool_choice,
        )
        searched_sources.extend(extract_sources_from_search(response.content))
        submit_block = find_tool_use(response.content, "submit_assessment")
        if submit_block:
            result = dict(submit_block.input)
            if not result.get("sources"):
                result["sources"] = sorted(set(searched_sources))
            return result

        messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason == "end_turn":
            messages.append({
                "role": "user",
                "content": "Please call submit_assessment now with your final conclusion based on the research so far.",
            })
        # otherwise (e.g. stop_reason == "tool_use" for the server-side web_search tool having
        # already executed within this response) just loop and let the model continue.

    raise RuntimeError("Model did not produce a submit_assessment call within the turn budget")


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


def research_pair_openai(client, record_a: dict, record_b: dict, distance: str, url_cache: dict, model: str) -> dict:
    input_items = [{"role": "user", "content": build_user_message(record_a, record_b, distance, url_cache)}]
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


def research_pair(provider: str, client, record_a: dict, record_b: dict, distance: str, url_cache: dict, model: str) -> dict:
    if provider == "anthropic":
        return research_pair_anthropic(client, record_a, record_b, distance, url_cache, model)
    return research_pair_openai(client, record_a, record_b, distance, url_cache, model)


def load_input(path: str) -> pd.DataFrame:
    if path.lower().endswith((".xlsx", ".xls")):
        df = pd.read_excel(path, dtype=str)
    else:
        df = pd.read_csv(path, dtype=str, encoding="utf-8-sig")
    return df


def group_pairs(df: pd.DataFrame):
    """Yield (group_id, [row_dict, ...], error_reason_or_None) for each Group Number."""
    if "Group Number" not in df.columns:
        raise SystemExit("Input is missing required 'Group Number' column.")
    for group_id, group_df in df.groupby("Group Number", sort=False):
        rows = group_df.to_dict("records")
        error = None
        if len(rows) != 2:
            error = f"Group {group_id} does not have exactly 2 records (found {len(rows)})."
        else:
            for row in rows:
                if not row.get("RecordID") or pd.isna(row.get("RecordID")):
                    error = f"Group {group_id} has a record missing RecordID."
                elif not row.get("Address") or pd.isna(row.get("Address")):
                    error = f"Missing Address field for RecordID {row.get('RecordID')} (Group {group_id})."
        yield str(group_id), rows, error


def load_checkpoint(path: Path) -> dict:
    results = {}
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                record = json.loads(line)
                results[record["group"]] = record
    return results


def append_checkpoint(path: Path, record: dict, lock: threading.Lock):
    with lock:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")


def process_group(provider, client, model, group_id, rows, error, url_cache):
    if error:
        return {
            "group": group_id,
            "record_ids": [r.get("RecordID") for r in rows],
            "decision": "Not Enough Info",
            "archetype": "Malformed input",
            "confidence": 1,
            "evidence_summary": error,
            "sources": [],
            "is_error": True,
        }

    record_a, record_b = rows[0], rows[1]
    distance = record_a.get("DISTANCE_MILES", "")
    try:
        result = research_pair(provider, client, record_a, record_b, distance, url_cache, model)
        decision = result.get("decision")
        if decision not in DECISION_LABELS:
            raise ValueError(f"Model returned invalid decision label: {decision!r}")
        return {
            "group": group_id,
            "record_ids": [record_a.get("RecordID"), record_b.get("RecordID")],
            "decision": decision,
            "archetype": result.get("archetype", ""),
            "confidence": int(result.get("confidence", 1)),
            "evidence_summary": result.get("evidence_summary", ""),
            "sources": result.get("sources", []),
            "is_error": False,
        }
    except Exception as e:
        return {
            "group": group_id,
            "record_ids": [record_a.get("RecordID"), record_b.get("RecordID")],
            "decision": "Not Enough Info",
            "archetype": "Processing error",
            "confidence": 1,
            "evidence_summary": f"{e.__class__.__name__}: {e}",
            "sources": [],
            "is_error": True,
        }


def build_output_df(df: pd.DataFrame, results_by_group: dict) -> pd.DataFrame:
    out = df.copy()
    out["Decision"] = pd.Series([""] * len(out), index=out.index, dtype=object)
    out["Archetype"] = pd.Series([""] * len(out), index=out.index, dtype=object)
    out["Confidence"] = pd.Series([None] * len(out), index=out.index, dtype=object)
    out["Evidence Summary"] = pd.Series([""] * len(out), index=out.index, dtype=object)
    out["Sources"] = pd.Series([""] * len(out), index=out.index, dtype=object)
    for group_id, group_df in out.groupby("Group Number", sort=False):
        result = results_by_group.get(str(group_id))
        if not result:
            continue
        for idx in group_df.index:
            out.at[idx, "Decision"] = result["decision"]
            out.at[idx, "Archetype"] = result["archetype"]
            out.at[idx, "Confidence"] = result["confidence"]
            out.at[idx, "Evidence Summary"] = result["evidence_summary"]
            out.at[idx, "Sources"] = "; ".join(result.get("sources", []))
    return out


def compute_summary(results_by_group: dict) -> dict:
    total = len(results_by_group)
    by_decision = {label: 0 for label in DECISION_LABELS}
    archetype_counts = {}
    confidences = []
    errors = 0
    for result in results_by_group.values():
        by_decision[result["decision"]] = by_decision.get(result["decision"], 0) + 1
        archetype_counts[result["archetype"]] = archetype_counts.get(result["archetype"], 0) + 1
        confidences.append(result["confidence"])
        if result.get("is_error"):
            errors += 1
    avg_confidence = sum(confidences) / len(confidences) if confidences else 0
    return {
        "total_pairs": total,
        "by_decision": by_decision,
        "average_confidence": round(avg_confidence, 2),
        "archetype_counts": archetype_counts,
        "errors": errors,
    }


def print_summary(summary: dict):
    print("\n=== Run Summary ===")
    print(f"Total pairs processed: {summary['total_pairs']}")
    for label, count in summary["by_decision"].items():
        print(f"  {label}: {count}")
    print(f"Average confidence: {summary['average_confidence']}")
    print(f"Errors: {summary['errors']}")
    print("Archetype breakdown:")
    for archetype, count in sorted(summary["archetype_counts"].items(), key=lambda kv: -kv[1]):
        print(f"  {count:>4}  {archetype}")


def write_output(out_df: pd.DataFrame, summary: dict, output_path: str):
    if output_path.lower().endswith((".xlsx", ".xls")):
        summary_rows = [{"Metric": "Total pairs processed", "Value": summary["total_pairs"]}]
        for label, count in summary["by_decision"].items():
            summary_rows.append({"Metric": f"Decision: {label}", "Value": count})
        summary_rows.append({"Metric": "Average confidence", "Value": summary["average_confidence"]})
        summary_rows.append({"Metric": "Errors", "Value": summary["errors"]})
        for archetype, count in sorted(summary["archetype_counts"].items(), key=lambda kv: -kv[1]):
            summary_rows.append({"Metric": f"Archetype: {archetype}", "Value": count})
        summary_df = pd.DataFrame(summary_rows)
        with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
            summary_df.to_excel(writer, sheet_name="Summary", index=False)
            out_df.to_excel(writer, sheet_name="Results", index=False)
    else:
        out_df.to_csv(output_path, index=False)
        summary_path = str(Path(output_path).with_suffix("")) + "_summary.txt"
        with open(summary_path, "w", encoding="utf-8") as f:
            f.write(f"Total pairs processed: {summary['total_pairs']}\n")
            for label, count in summary["by_decision"].items():
                f.write(f"  {label}: {count}\n")
            f.write(f"Average confidence: {summary['average_confidence']}\n")
            f.write(f"Errors: {summary['errors']}\n")
            f.write("Archetype breakdown:\n")
            for archetype, count in sorted(summary["archetype_counts"].items(), key=lambda kv: -kv[1]):
                f.write(f"  {count:>4}  {archetype}\n")
        print(f"Summary also written to {summary_path}")


def resolve_provider(explicit_provider: str | None) -> str:
    # bool(...) rather than an `in os.environ` check: CI providers (e.g. GitHub Actions
    # referencing an unset secret) can set the env var to an empty string rather than
    # omitting it, which should still count as "not configured".
    has_anthropic_key = bool(os.environ.get("ANTHROPIC_API_KEY"))
    has_openai_key = bool(os.environ.get("OPENAI_API_KEY"))

    provider = explicit_provider
    if provider is None:
        if has_anthropic_key and has_openai_key:
            raise SystemExit(
                "Both ANTHROPIC_API_KEY and OPENAI_API_KEY are set — pass --provider anthropic|openai explicitly."
            )
        if has_anthropic_key:
            provider = "anthropic"
        elif has_openai_key:
            provider = "openai"
        else:
            raise SystemExit(
                "Set ANTHROPIC_API_KEY or OPENAI_API_KEY (or pass --provider once one of them is set)."
            )

    if provider == "anthropic" and not has_anthropic_key:
        raise SystemExit("--provider anthropic requires the ANTHROPIC_API_KEY environment variable to be set.")
    if provider == "openai" and not has_openai_key:
        raise SystemExit("--provider openai requires the OPENAI_API_KEY environment variable to be set.")
    return provider


def main():
    parser = argparse.ArgumentParser(description="Research candidate duplicate property pairs via an LLM.")
    parser.add_argument("--input", required=True, help="Path to input CSV or XLSX file")
    parser.add_argument("--output", required=True, help="Path to output CSV or XLSX file (never overwrites input)")
    parser.add_argument("--checkpoint", default=None, help="Checkpoint JSONL path (default: <output>.checkpoint.jsonl)")
    parser.add_argument("--limit", type=int, default=None, help="Only process the first N pairs (dry-run mode)")
    parser.add_argument("--concurrency", type=int, default=1, help="Number of pairs to research in parallel (default: 1)")
    parser.add_argument("--provider", choices=["anthropic", "openai"], default=None,
                         help="LLM backend to use (default: auto-detected from whichever API key env var is set)")
    parser.add_argument("--model", default=None,
                         help="Model id (default: DEFAULT_MODELS[provider], e.g. claude-sonnet-5 or gpt-4o)")
    parser.add_argument("--restart", action="store_true", help="Ignore any existing checkpoint and reprocess everything")
    args = parser.parse_args()

    if str(Path(args.input).resolve()) == str(Path(args.output).resolve()):
        raise SystemExit("Output path must differ from input path.")

    provider = resolve_provider(args.provider)
    model = args.model or DEFAULT_MODELS[provider]
    print(f"Using provider={provider}, model={model}", flush=True)

    checkpoint_path = Path(args.checkpoint) if args.checkpoint else Path(str(args.output) + ".checkpoint.jsonl")

    df = load_input(args.input)
    all_groups = list(group_pairs(df))
    if args.limit is not None:
        all_groups = all_groups[: args.limit]
    total = len(all_groups)
    print(f"Loaded {args.input}: {total} candidate pair(s) to process.", flush=True)

    results_by_group = {} if args.restart else load_checkpoint(checkpoint_path)
    if results_by_group:
        print(f"Resuming from checkpoint: {len(results_by_group)} pair(s) already completed.", flush=True)
    if args.restart and checkpoint_path.exists():
        checkpoint_path.unlink()

    pending = [(gid, rows, err) for gid, rows, err in all_groups if gid not in results_by_group]
    print(f"{len(pending)} pair(s) remaining to process.", flush=True)

    client = anthropic.Anthropic() if provider == "anthropic" else OpenAI()
    url_cache = {}
    checkpoint_lock = threading.Lock()
    done_count = 0

    def run_one(item):
        group_id, rows, error = item
        result = process_group(provider, client, model, group_id, rows, error, url_cache)
        append_checkpoint(checkpoint_path, result, checkpoint_lock)
        return result

    if args.concurrency <= 1:
        for item in pending:
            result = run_one(item)
            results_by_group[result["group"]] = result
            done_count += 1
            print(f"[{done_count}/{len(pending)}] Group {result['group']}: {result['decision']} "
                  f"({result['archetype']})", flush=True)
    else:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
            futures = {pool.submit(run_one, item): item for item in pending}
            for future in as_completed(futures):
                result = future.result()
                results_by_group[result["group"]] = result
                done_count += 1
                print(f"[{done_count}/{len(pending)}] Group {result['group']}: {result['decision']} "
                      f"({result['archetype']})", flush=True)

    processed_group_ids = {gid for gid, _, _ in all_groups}
    scoped_results = {gid: r for gid, r in results_by_group.items() if gid in processed_group_ids}
    out_df = df[df["Group Number"].astype(str).isin(processed_group_ids)] if args.limit is not None else df
    out_df = build_output_df(out_df, scoped_results)
    summary = compute_summary(scoped_results)
    write_output(out_df, summary, args.output)
    print_summary(summary)
    print(f"\nResults written to {args.output}")


if __name__ == "__main__":
    main()
