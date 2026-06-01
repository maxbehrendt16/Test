import os
import csv
import io
import argparse
from openai import OpenAI

client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])

BATCH_SIZE = 20

SYSTEM_PROMPT = """You are a property classification agent. You will be given a list of properties, each with the following fields: Property Name, Address, MDU, and RecordID. Your job is to classify each property as an **Apartment**, **COA**, **HOA**, or **Other** by searching the web.

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

4. **Output a CSV file** with the following columns in this exact order:

   RecordID, Property Name, Address, MDU, Classification, Confidence, Key Evidence, Notes

   Rules:
   - **RecordID, Property Name, Address, and MDU must be copied exactly from the input — do not alter, clean, or reformat them.**
   - Classification: one of Apartment | COA | HOA | Other – <subtype>
   - Confidence: High | Medium | Low
   - Key Evidence: the exact keyword or phrase found and the source it was found on (e.g. "homeowners association" — zillow.com)
   - Notes: any conflicts, ambiguities, or multiple properties matching the same name and zip; leave blank if none
   - Wrap all fields in double quotes to handle commas within values

Return ONLY the CSV output with no additional text, explanation, or markdown formatting. Include the header row."""


def strip_fences(text: str) -> str:
    if text.startswith("```"):
        lines = text.splitlines()
        return "\n".join(line for line in lines if not line.startswith("```")).strip()
    return text


def classify_batch(header: str, rows: list[str], batch_num: int, total_batches: int) -> str:
    batch_csv = header + "\n" + "\n".join(rows)
    print(f"  Classifying batch {batch_num}/{total_batches} ({len(rows)} properties)...", flush=True)
    response = client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"**Properties to classify:**\n\n{batch_csv}"},
        ],
        max_tokens=4096,
    )
    result = strip_fences(response.choices[0].message.content.strip())

    # Drop the header row from all batches except the first
    lines = result.splitlines()
    if batch_num > 1 and lines and lines[0].lower().startswith('"recordid'):
        lines = lines[1:]
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Classify properties using OpenAI")
    parser.add_argument("--input", required=True, help="Path to input CSV file")
    parser.add_argument("--output", default="classified_results.csv", help="Path to output CSV file")
    parser.add_argument("--batch-size", type=int, default=BATCH_SIZE, help="Rows per API call (default: 20)")
    args = parser.parse_args()

    with open(args.input, "r", encoding="utf-8-sig") as f:
        lines = [line.rstrip("\n") for line in f if line.strip()]

    if not lines:
        raise SystemExit("Input CSV is empty.")

    header = lines[0]
    data_rows = lines[1:]
    total = len(data_rows)
    batch_size = args.batch_size
    total_batches = (total + batch_size - 1) // batch_size

    print(f"Read {args.input}: {total} properties, splitting into {total_batches} batch(es) of up to {batch_size}.", flush=True)

    output_parts = []
    for i in range(total_batches):
        batch_rows = data_rows[i * batch_size : (i + 1) * batch_size]
        part = classify_batch(header, batch_rows, i + 1, total_batches)
        output_parts.append(part)

    final_csv = "\n".join(output_parts)

    with open(args.output, "w", encoding="utf-8") as f:
        f.write(final_csv + "\n")

    output_lines = [l for l in final_csv.splitlines() if l.strip()]
    classified_count = len(output_lines) - 1  # subtract header
    print(f"\nDone. {classified_count}/{total} properties classified. Results written to {args.output}.")
    if classified_count != total:
        print(f"WARNING: Expected {total} rows but got {classified_count}. Check the log above for any batch errors.")


if __name__ == "__main__":
    main()
