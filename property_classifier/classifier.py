import os
import csv
import io
import argparse
from openai import OpenAI

client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])

BATCH_SIZE = 20

# The model only needs to return the 4 classification columns, keyed by row index.
# Input fields (RecordID, Property Name, Address, MDU) are always taken from the
# original parsed CSV — never from the model output.
SYSTEM_PROMPT = """You are a property classification agent. You will be given a list of properties, each with the following fields: RowIndex, Property Name, Address, MDU, and RecordID. Your job is to classify each property as an **Apartment**, **COA**, **HOA**, or **Other** by searching the web.

Follow these steps for each property:

1. **Search for the property using whatever identifying information is available:**
   - If a full address is provided, search using the full address.
   - If no address is provided, search using the property name and zip code only.

2. **Find and check the following sources:**

   **Property Website (check first):**
   The official first-party site for the property, community, or development. It serves residents (owners/renters), prospective residents, and employees, and provides information such as amenities, contact details, leasing info, or resident portals. It must directly represent the property — not a listing, aggregator, or social media site. Common indicators of a legitimate property website:
   - URL contains the property or community name
   - Site includes amenities, floor plans, contact info, or a resident portal
   - Site has leasing or HOA/COA-specific content

   Do NOT treat the following as the property website:
   - Listing sites (Zillow, Apartments.com, Realtor.com, Redfin, Compass)
   - Aggregators (HOA-USA, ApartmentList)
   - Social media pages (Facebook, Instagram)
   - Property management company homepages (only count if the page is dedicated to that specific property)

   **Aggregator Listings (check second):**
   Search for the property on Zillow, Apartments.com, Realtor.com, Redfin, and Compass. When searching these sites, always include the full street address in the query to ensure results are scoped to that exact property. Do not use results that match on name alone or show a different address.

3. **Apply this classification logic:**

   **Step 1 — Check for Other first:**
   Before anything else, scan for keywords that indicate the property is not a standard residential community:

   | Keywords found | Classification |
   |---|---|
   | "hotel," "resort," "inn," "suites," "nightly rate," "book a room," "check-in," "check-out" | **Other – Hotel/Resort** |
   | "assisted living," "memory care," "senior living," "nursing home," "skilled nursing," "independent living," "continuing care" | **Other – Assisted Living/Senior Facility** |
   | "vacation rental," "short-term rental," "Airbnb," "VRBO" | **Other – Vacation/Short-Term Rental** |
   | "co-living," "coliving," "shared living" | **Other – Co-Living** |

   If any of these keywords are found → classify as **Other – <subtype>**. Stop here.

   **Step 2 — Rent vs. Own:**
   - Look for rental keywords: "for rent," "lease," "month-to-month," "apply now," "rent starting at"
   - If found → classify as **Apartment**. Stop here.
   - If listings show "for sale," "buy," or owner language → proceed to Step 3.

   **Step 3 — HOA vs. COA (for-sale properties only):**
   Search the property website and aggregator listings for these explicit keywords:

   | Keywords found | Classification |
   |---|---|
   | "condominium association," "condo association," "COA," "condominium owners association," "master deed," "condo declaration" | **COA** |
   | "homeowners association," "HOA," "homeowner association," "CC&Rs," "declaration of covenants," "subdivision" | **HOA** |

   - If both sets of keywords appear, flag as ambiguous in the Notes field.
   - If neither set appears, note low confidence and make a best guess based on whatever association language is present.

4. **Output format:**
   Return a CSV with exactly these columns in this order:
   RowIndex, Classification, Confidence, Key Evidence, Notes

   Rules:
   - RowIndex: copy exactly from the input
   - Classification: one of Apartment | COA | HOA | Other – <subtype>
   - Confidence: High | Medium | Low
   - Key Evidence: the exact keyword or phrase found and the source (e.g. "homeowners association" — zillow.com)
   - Notes: any conflicts or ambiguities; leave blank if none
   - Wrap all fields in double quotes
   - Include the header row
   - Return ONLY the CSV — no extra text, explanation, or markdown"""


def strip_fences(text: str) -> str:
    if text.startswith("```"):
        lines = text.splitlines()
        return "\n".join(line for line in lines if not line.startswith("```")).strip()
    return text


def classify_batch(batch: list[dict], batch_num: int, total_batches: int) -> dict[int, dict]:
    """Send a batch to OpenAI and return {row_index: {Classification, Confidence, Key Evidence, Notes}}."""
    # Build a minimal CSV with RowIndex so the model can key its output
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=["RowIndex", "RecordID", "Property Name", "Address", "MDU"])
    writer.writeheader()
    for row in batch:
        writer.writerow({
            "RowIndex": row["_row_index"],
            "RecordID": row.get("RecordID", ""),
            "Property Name": row.get("Property Name", ""),
            "Address": row.get("Address", ""),
            "MDU": row.get("MDU", ""),
        })
    input_csv = buf.getvalue()

    print(f"  Classifying batch {batch_num}/{total_batches} ({len(batch)} properties)...", flush=True)
    response = client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"**Properties to classify:**\n\n{input_csv}"},
        ],
        max_tokens=4096,
    )
    raw = strip_fences(response.choices[0].message.content.strip())

    results = {}
    reader = csv.DictReader(io.StringIO(raw))
    for parsed_row in reader:
        try:
            idx = int(parsed_row["RowIndex"])
        except (KeyError, ValueError):
            continue
        results[idx] = {
            "Classification": parsed_row.get("Classification", ""),
            "Confidence": parsed_row.get("Confidence", ""),
            "Key Evidence": parsed_row.get("Key Evidence", ""),
            "Notes": parsed_row.get("Notes", ""),
        }
    return results


def main():
    parser = argparse.ArgumentParser(description="Classify properties using OpenAI")
    parser.add_argument("--input", required=True, help="Path to input CSV file")
    parser.add_argument("--output", default="classified_results.csv", help="Path to output CSV file")
    parser.add_argument("--batch-size", type=int, default=BATCH_SIZE, help="Rows per API call (default: 20)")
    args = parser.parse_args()

    with open(args.input, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        input_rows = list(reader)

    if not input_rows:
        raise SystemExit("Input CSV is empty.")

    # Tag each row with a stable numeric index for matching model output
    for i, row in enumerate(input_rows):
        row["_row_index"] = i

    total = len(input_rows)
    batch_size = args.batch_size
    total_batches = (total + batch_size - 1) // batch_size
    print(f"Read {args.input}: {total} properties, splitting into {total_batches} batch(es) of up to {batch_size}.", flush=True)

    all_results: dict[int, dict] = {}
    for i in range(total_batches):
        batch = input_rows[i * batch_size : (i + 1) * batch_size]
        batch_results = classify_batch(batch, i + 1, total_batches)
        all_results.update(batch_results)

    # Write output — input fields always come from the original parsed CSV
    out_fields = ["RecordID", "Property Name", "Address", "MDU",
                  "Classification", "Confidence", "Key Evidence", "Notes"]
    missing = 0
    with open(args.output, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=out_fields, quoting=csv.QUOTE_ALL)
        writer.writeheader()
        for row in input_rows:
            idx = row["_row_index"]
            classification = all_results.get(idx, {})
            if not classification:
                missing += 1
                print(f"  WARNING: No result returned for row {idx} ({row.get('Property Name', '')})", flush=True)
            writer.writerow({
                "RecordID": row.get("RecordID", ""),
                "Property Name": row.get("Property Name", ""),
                "Address": row.get("Address", ""),
                "MDU": row.get("MDU", ""),
                "Classification": classification.get("Classification", ""),
                "Confidence": classification.get("Confidence", ""),
                "Key Evidence": classification.get("Key Evidence", ""),
                "Notes": classification.get("Notes", ""),
            })

    classified = total - missing
    print(f"\nDone. {classified}/{total} properties classified. Results written to {args.output}.")
    if missing:
        print(f"WARNING: {missing} row(s) had no model output — check the log above.")


if __name__ == "__main__":
    main()
