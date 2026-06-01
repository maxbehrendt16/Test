import os
import csv
import io
import json
import argparse
import requests
from openai import OpenAI

client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])

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


def classify_properties(input_csv_text: str) -> str:
    response = client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"**Properties to classify:**\n\n{input_csv_text}"},
        ],
        max_tokens=4096,
    )
    return response.choices[0].message.content.strip()


def main():
    parser = argparse.ArgumentParser(description="Classify properties using OpenAI")
    parser.add_argument("--input", required=True, help="Path to input CSV file")
    parser.add_argument("--output", default="classified_results.csv", help="Path to output CSV file")
    args = parser.parse_args()

    with open(args.input, "r", encoding="utf-8") as f:
        input_csv_text = f.read()

    print(f"Read {args.input} — sending to OpenAI for classification...", flush=True)
    result_csv = classify_properties(input_csv_text)

    # Strip markdown code fences if the model wrapped the output
    if result_csv.startswith("```"):
        lines = result_csv.splitlines()
        result_csv = "\n".join(
            line for line in lines if not line.startswith("```")
        ).strip()

    with open(args.output, "w", encoding="utf-8") as f:
        f.write(result_csv)

    print(f"Classification complete. Results written to {args.output}")

    # Print a summary row count
    rows = [r for r in result_csv.splitlines() if r.strip()]
    print(f"Output contains {len(rows) - 1} classified properties (excluding header).")


if __name__ == "__main__":
    main()
