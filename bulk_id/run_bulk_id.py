"""
Bulk ID test runner.

Runs the bulk ID prompt (stored in the OpenAI dashboard) against every property in an
input CSV using the OpenAI Responses API with web search enabled -- one call per
property -- and writes each property's parsed result to an output CSV.

This script does NOT combine runs or classify properties as bulk/retail. The run_id comes
from each input row and is copied to the output; rollup across runs is done separately.

Usage:
    python run_bulk_id.py --input <input.csv> --output <output.csv> [--limit N]

Configuration is read from a .env file next to this script (see .env.example). Values
already set in the environment (e.g. OPENAI_API_KEY from a GitHub secret) take precedence.

Resumable: if the output file already exists, rows whose (record_id, run_id) pair already
has a successful result are skipped. Everything else is processed, and any previously failed
rows for those pairs are replaced.
"""
import argparse
import csv
import json
import os
import random
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import pandas as pd
from dotenv import load_dotenv

import openai
from openai import OpenAI

MAX_RETRIES = 5

# Input column -> prompt variable name.
VARIABLE_MAP = {
    "address": "property_address",
    "master_property name": "property_name",
    "ownership_type": "ownership_type",
    "units": "units",
    "building_count": "building_count",
    "owner": "owner",
    "property_manager": "property_manager",
}
ID_COLUMNS = ["record_id", "dma_code", "run_id"]
ALL_PROMPT_VARIABLES = list(VARIABLE_MAP.values())

# Fields copied straight from the model's parsed JSON.
PARSED_FIELDS = [
    "bulk_internet",
    "retail_internet",
    "bulk_tv",
    "property_address_matched",
    "property_name_matched",
    "not_found",
    "not_found_notes",
    "needs_information",
    "needs_information_notes",
    "summary_findings",
]

RESULT_COLUMNS = [
    *PARSED_FIELDS,
    "evidence_count",
    "evidence_urls",
    "evidence_domains",
    "evidence_json",
    "web_search_urls",
    "web_search_calls",
    "web_search_count",
    "web_open_page_count",
    "web_find_in_page_count",
    "web_search_queries",
    "input_tokens",
    "output_tokens",
    "response_id",
    "model",
    "prompt_id",
    "prompt_version",
    "timestamp",
    "parse_ok",
    "error",
    "raw_response",
]


def load_config():
    load_dotenv(Path(__file__).resolve().parent / ".env", override=False)

    def get(name, default=None):
        value = os.environ.get(name, "").strip()
        return value if value else default

    def as_bool(value):
        return str(value).strip().lower() in ("1", "true", "yes", "y")

    config = {
        "api_key": get("OPENAI_API_KEY"),
        "prompt_id": get("PROMPT_ID", "pmpt_6ad7815d491c81909d2bee5ecda0a244033afdc5acb88564"),
        "prompt_version": get("PROMPT_VERSION", "7"),
        "model": get("MODEL", "gpt-4o"),
        "max_tool_calls": int(get("MAX_TOOL_CALLS", "100")),
        "parallel_tool_calls": as_bool(get("PARALLEL_TOOL_CALLS", "true")),
        "service_tier": get("SERVICE_TIER", "auto"),
        "search_context_size": get("SEARCH_CONTEXT_SIZE", "medium"),
        "max_output_tokens": int(get("MAX_OUTPUT_TOKENS", "4000")),
        "concurrency": int(get("CONCURRENCY", "5")),
        "prompt_variables": [v.strip() for v in get("PROMPT_VARIABLES", ",".join(ALL_PROMPT_VARIABLES)).split(",")
                             if v.strip()],
    }
    if not config["api_key"]:
        sys.exit("OPENAI_API_KEY is not set (add it to .env or the environment).")
    unknown = [v for v in config["prompt_variables"] if v not in ALL_PROMPT_VARIABLES]
    if unknown or not config["prompt_variables"]:
        sys.exit(f"PROMPT_VARIABLES has unknown or no variable names: {', '.join(unknown) or '(empty)'}. "
                 f"Allowed: {', '.join(ALL_PROMPT_VARIABLES)}")
    return config


# ---------------------------------------------------------------------------
# Input / output
# ---------------------------------------------------------------------------

def read_input(path, prompt_variables):
    """Read the input CSV, requiring the ID columns plus the columns behind the prompt's variables."""
    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    df.columns = [c.strip() for c in df.columns]
    required = ID_COLUMNS + [col for col, var in VARIABLE_MAP.items() if var in prompt_variables]
    missing = [c for c in required if c not in df.columns]
    if missing:
        sys.exit(f"Input file is missing required column(s): {', '.join(missing)}")
    return df


def output_columns(input_columns):
    carried = [c for c in input_columns if c not in ("record_id", "dma_code", "run_id")]
    return ["record_id", "dma_code", "run_id", *carried, *RESULT_COLUMNS]


def row_key(row):
    return (str(row["record_id"]).strip(), str(row["run_id"]).strip())


def read_existing(path):
    if not path.exists() or path.stat().st_size == 0:
        return None
    return pd.read_csv(path, dtype=str, keep_default_na=False)


def drop_failed_rows(path, existing, keys):
    """Rewrite the output without previously failed rows for the given (record_id, run_id) keys."""
    failed = existing["error"].str.strip() != ""
    in_keys = existing.apply(lambda r: row_key(r) in keys, axis=1)
    drop = failed & in_keys
    if not drop.any():
        return 0
    tmp = path.with_name(path.name + ".tmp")
    existing[~drop].to_csv(tmp, index=False, encoding="utf-8")
    os.replace(tmp, path)
    return int(drop.sum())


def build_variables(row, names):
    """Build the prompt variables, passing only the ones listed in PROMPT_VARIABLES."""
    return {var: str(row.get(col, "") or "").strip() for col, var in VARIABLE_MAP.items() if var in names}


# ---------------------------------------------------------------------------
# API call
# ---------------------------------------------------------------------------

def is_retryable(exc):
    if isinstance(exc, (openai.RateLimitError, openai.APIConnectionError, openai.APITimeoutError,
                        openai.InternalServerError)):
        return True
    return isinstance(exc, openai.APIStatusError) and exc.status_code >= 500


def call_api(client, config, variables):
    attempt = 0
    while True:
        try:
            return client.responses.create(
                model=config["model"],
                prompt={
                    "id": config["prompt_id"],
                    "version": config["prompt_version"],
                    "variables": variables,
                },
                tools=[{"type": "web_search", "search_context_size": config["search_context_size"]}],
                include=["web_search_call.action.sources"],
                max_tool_calls=config["max_tool_calls"],
                parallel_tool_calls=config["parallel_tool_calls"],
                service_tier=config["service_tier"],
                max_output_tokens=config["max_output_tokens"],
            )
        except Exception as exc:
            if attempt >= MAX_RETRIES or not is_retryable(exc):
                raise
            attempt += 1
            delay = min(2 ** attempt, 60) + random.uniform(0, 1)
            print(f"  retry {attempt}/{MAX_RETRIES} in {delay:.1f}s ({type(exc).__name__}: {exc})", flush=True)
            time.sleep(delay)


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------

FENCE_RE = re.compile(r"```[a-zA-Z0-9_-]*[ \t]*\n?(.*?)```", re.DOTALL)


def json_candidates(text):
    """Yield possible JSON strings: the whole text, then the contents of each code block."""
    yield text.strip()
    for match in FENCE_RE.finditer(text):
        yield match.group(1).strip()


def find_json_object(text):
    """Find a JSON object anywhere in the text, preferring one that has the expected output fields."""
    decoder = json.JSONDecoder()
    fallback = None
    for start in (m.start() for m in re.finditer(r"\{", text)):
        try:
            data, _ = decoder.raw_decode(text, start)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            if any(field in data for field in PARSED_FIELDS) or "evidence" in data:
                return data
            fallback = fallback or data
    return fallback


def parse_output(text):
    if not isinstance(text, str) or not text.strip():
        return None
    for candidate in json_candidates(text):
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
    return find_json_object(text)


def clean_url(url):
    """Drop the utm_source=openai tag that OpenAI adds to cited URLs, so duplicates match."""
    if not url:
        return url
    parts = urlparse(url)
    query = "&".join(q for q in parts.query.split("&") if q and q != "utm_source=openai")
    return parts._replace(query=query).geturl()


def unique(items):
    seen = []
    for item in items:
        if item and item not in seen:
            seen.append(item)
    return seen


def web_search_info(response):
    """Count web search calls by action type, and collect the search queries plus URLs from both the
    search calls and the output's url_citations."""
    calls = 0
    action_counts = {"search": 0, "open_page": 0, "find_in_page": 0}
    queries = []
    urls = []
    for item in response.output or []:
        item_type = getattr(item, "type", None)
        if item_type == "web_search_call":
            calls += 1
            action = getattr(item, "action", None)
            if action is not None:
                action_type = getattr(action, "type", None)
                if action_type in action_counts:
                    action_counts[action_type] += 1
                if action_type == "search":
                    queries.extend(getattr(action, "queries", None) or [])
                    queries.append(getattr(action, "query", None))
                urls.append(getattr(action, "url", None))
                for source in getattr(action, "sources", None) or []:
                    urls.append(getattr(source, "url", None))
        elif item_type == "message":
            for part in getattr(item, "content", None) or []:
                for ann in getattr(part, "annotations", None) or []:
                    if getattr(ann, "type", None) == "url_citation":
                        urls.append(getattr(ann, "url", None))
    return {
        "web_search_urls": " | ".join(unique(clean_url(u) for u in urls)),
        "web_search_calls": calls,
        "web_search_count": action_counts["search"],
        "web_open_page_count": action_counts["open_page"],
        "web_find_in_page_count": action_counts["find_in_page"],
        "web_search_queries": " | ".join(unique(q.strip() for q in queries if q)),
    }


def domain_of(url):
    try:
        netloc = urlparse(url).netloc.lower()
    except (TypeError, ValueError):
        return ""
    return netloc[4:] if netloc.startswith("www.") else netloc


def format_value(value):
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False)
    return value


def evidence_fields(parsed):
    evidence = parsed.get("evidence") if parsed else None
    if not isinstance(evidence, list):
        evidence = [] if evidence is None else [evidence]
    urls, domains = [], []
    for entry in evidence:
        if isinstance(entry, dict):
            url = entry.get("url") or entry.get("source_url") or ""
            urls.append(url)
            domains.append(entry.get("domain") or domain_of(url))
    return {
        "evidence_count": len(evidence),
        "evidence_urls": " | ".join(u for u in urls if u),
        "evidence_domains": " | ".join(d for d in domains if d),
        "evidence_json": json.dumps(evidence, ensure_ascii=False) if parsed else "",
    }


def build_result(response):
    raw = response.output_text or ""
    parsed = parse_output(raw)
    usage = response.usage
    result = {field: format_value(parsed.get(field)) if parsed else "" for field in PARSED_FIELDS}
    result.update(evidence_fields(parsed))
    result.update({
        **web_search_info(response),
        "input_tokens": usage.input_tokens if usage else 0,
        "output_tokens": usage.output_tokens if usage else 0,
        "response_id": response.id,
        "model": response.model,
        "parse_ok": "true" if parsed is not None else "false",
        "error": "",
        "raw_response": raw,
    })
    return result, parsed


def process_property(client, config, row):
    variables = build_variables(row, config["prompt_variables"])
    try:
        response = call_api(client, config, variables)
        result, parsed = build_result(response)
    except Exception as exc:
        result = {col: "" for col in RESULT_COLUMNS}
        result.update({
            "evidence_count": 0,
            "web_search_calls": 0,
            "web_search_count": 0,
            "web_open_page_count": 0,
            "web_find_in_page_count": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "parse_ok": "false",
            "error": f"{type(exc).__name__}: {exc}",
        })
        parsed = None
    result["prompt_id"] = config["prompt_id"]
    result["prompt_version"] = config["prompt_version"]
    result["timestamp"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return variables, result, parsed


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def print_test_detail(row, variables, result, parsed, config):
    print("\n" + "=" * 78)
    print(f"record_id: {row['record_id']}  run_id: {row['run_id']}")
    print("Variables sent:")
    for name, value in variables.items():
        print(f"  {name}: {value!r}")
    model = result.get("model") or "(none)"
    model_flag = "OK" if str(model).startswith(config["model"]) else f"WARNING: expected {config['model']}"
    print(f"Model reported: {model}  [{model_flag}]")
    calls = result.get("web_search_calls", 0)
    print(f"Web search calls: {calls}" + ("" if calls else "  [WARNING: web search did not run]"))
    print(f"  searches={result.get('web_search_count', 0)}, open_page={result.get('web_open_page_count', 0)}, "
          f"find_in_page={result.get('web_find_in_page_count', 0)}")
    print(f"Web search queries: {result.get('web_search_queries') or '(none)'}")
    print(f"Web search URLs: {result.get('web_search_urls') or '(none)'}")
    print(f"Evidence URLs ({result.get('evidence_count', 0)}): {result.get('evidence_urls') or '(none)'}")
    print(f"Tokens: in={result.get('input_tokens')} out={result.get('output_tokens')}")
    if result.get("error"):
        print(f"ERROR: {result['error']}")
    elif parsed is not None:
        print("Parsed output:")
        print(json.dumps(parsed, indent=2, ensure_ascii=False))
    else:
        print("PARSE FAILED. Raw response:")
        print(result.get("raw_response", ""))
    print("=" * 78, flush=True)


def main():
    parser = argparse.ArgumentParser(description="Run the bulk ID prompt against every property in a CSV.")
    parser.add_argument("--input", required=True, help="Input CSV path")
    parser.add_argument("--output", required=True, help="Output CSV path (appended to if it exists)")
    parser.add_argument("--limit", type=int, default=None, help="Only process the first N properties")
    args = parser.parse_args()

    config = load_config()
    df = read_input(args.input, config["prompt_variables"])
    if args.limit is not None:
        df = df.head(args.limit)

    output_path = Path(args.output)
    existing = read_existing(output_path)
    existing_columns = list(existing.columns) if existing is not None else None
    columns = existing_columns or output_columns(df.columns)
    added = [c for c in output_columns(df.columns) if existing_columns and c not in existing_columns]
    if added:
        # Output came from an older version of this script: add the new columns so they aren't dropped.
        columns = existing_columns + added
        existing = existing.reindex(columns=columns, fill_value="")
        existing.to_csv(output_path, index=False, encoding="utf-8")
        print(f"Added {len(added)} new column(s) to existing output: {', '.join(added)}")
    done_keys = set()
    if existing is not None:
        done_keys = {row_key(r) for r in existing.to_dict("records") if not r["error"].strip()}
    todo = [row for row in df.to_dict("records") if row_key(row) not in done_keys]

    total = len(todo)
    skipped = len(df) - total
    run_ids = ", ".join(sorted(set(df["run_id"])))
    print(f"run_id(s) {run_ids}: {len(df)} rows in input, {skipped} already successful in output, "
          f"{total} to process")
    if existing is not None and total:
        replaced = drop_failed_rows(output_path, existing, {row_key(r) for r in todo})
        if replaced:
            print(f"Removed {replaced} previously failed row(s) from output; they will be re-run")
    print(f"Prompt {config['prompt_id']} v{config['prompt_version']}, model={config['model']}, "
          f"concurrency={config['concurrency']}", flush=True)
    print(f"Prompt variables: {', '.join(config['prompt_variables'])}", flush=True)
    if total == 0:
        return

    client = OpenAI(api_key=config["api_key"], max_retries=0, timeout=600)
    write_lock = threading.Lock()
    new_file = existing_columns is None
    stats = {"processed": 0, "successful": 0, "failed": 0, "parse_failures": 0,
             "input_tokens": 0, "output_tokens": 0, "web_search_calls": 0}

    with open(output_path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        if new_file:
            writer.writeheader()
            f.flush()

        with ThreadPoolExecutor(max_workers=max(1, config["concurrency"])) as pool:
            futures = {pool.submit(process_property, client, config, row): row for row in todo}
            for future in as_completed(futures):
                row = futures[future]
                variables, result, parsed = future.result()
                out_row = {**row, **result}

                with write_lock:
                    writer.writerow(out_row)
                    f.flush()

                stats["processed"] += 1
                if result["error"]:
                    stats["failed"] += 1
                else:
                    stats["successful"] += 1
                    if result["parse_ok"] != "true":
                        stats["parse_failures"] += 1
                stats["input_tokens"] += int(result["input_tokens"] or 0)
                stats["output_tokens"] += int(result["output_tokens"] or 0)
                stats["web_search_calls"] += int(result["web_search_calls"] or 0)

                status = "ERROR" if result["error"] else ("parse failed" if result["parse_ok"] != "true" else "ok")
                print(f"{stats['processed']} of {total} complete  "
                      f"(record_id {row['record_id']}, run_id {row['run_id']}: {status})", flush=True)
                if args.limit is not None:
                    print_test_detail(row, variables, result, parsed, config)

    print("\nSummary")
    print(f"  Properties processed:  {stats['processed']}")
    print(f"  Successful:            {stats['successful']}")
    print(f"  Failed:                {stats['failed']}")
    print(f"  Parse failures:        {stats['parse_failures']}")
    print(f"  Total input tokens:    {stats['input_tokens']}")
    print(f"  Total output tokens:   {stats['output_tokens']}")
    print(f"  Total web search calls: {stats['web_search_calls']}")
    print(f"Output written to {output_path}")


if __name__ == "__main__":
    main()
