# Bulk ID test runner

Runs the bulk ID prompt (stored in the OpenAI dashboard) against every property in an input CSV, one Responses API call per property with web search on, and writes one parsed row per property to an output CSV. It does not combine runs or classify bulk vs. retail.

## Run from GitHub (one button)
1. Upload your input CSV into this `bulk_id/` folder.
2. Go to **Actions → Bulk ID Test Run → Run workflow**.
3. Enter the file name, pick the run ID (1–4), and optionally a limit (use `3` for a test run).
4. Download `bulk_id_run<N>.csv` from the run's **Artifacts**. For a limited test run, the log also prints each property's variables, model, web search calls and parsed output.

The API key comes from the repo's `OPENAI_API_KEY` secret. Other settings come from `.env.example`.

## Run locally
```
pip install -r requirements.txt
cp .env.example .env   # then fill in OPENAI_API_KEY
python run_bulk_id.py --input input.csv --run-id 1 --output bulk_id_run1.csv --limit 3
```
If the output file already exists, record_ids already in it are skipped and the rest are appended.
