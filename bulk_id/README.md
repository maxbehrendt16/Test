# Bulk ID test runner

Runs the bulk ID prompt (stored in the OpenAI dashboard) against every property in an input CSV, one Responses API call per property with web search on, and writes one parsed row per property to an output CSV. It does not combine runs or classify bulk vs. retail. The `run_id` comes from each input row and is copied to the output.

## Run from GitHub (one button)
1. Upload your input CSV into this `bulk_id/` folder.
2. Go to **Actions → Bulk ID Test Run → Run workflow**.
3. Enter the file name, optionally a limit (use `3` for a test run), and the concurrency (how many properties run in parallel, default 5).
4. Download `<input name>_results.csv` and `<input name>_results_snowflake.csv` from the run's **Artifacts**. For a limited test run, the log also prints each property's variables, model, web search calls and parsed output.

The `_snowflake` file has the columns of the Snowflake `BULK_PROPERTIES` table (`record_id, dma_code, address, property_name, run_id, prompt, created_at, response_id, response_text, web_search_urls, status, error, total_tokens, input_tokens, output_tokens`). It is rebuilt from the full main output at the end of every run.

The API key comes from the repo's `OPENAI_API_KEY` secret. Other settings come from `.env.example`. `PROMPT_VARIABLES` lists which variables the prompt takes; only those are sent.

### Owen's Prompt
**Actions → Owen's Prompt** works the same way but runs the alternate prompt set at the top of `.github/workflows/bulk_id_test_prompt.yml` (currently `pmpt_6ad7b7273d3481958e7eceb3143d9e9f0a6be6c2a973741d`, version 1, which only takes `property_name` and `property_address`). Its output is `<input name>_test_prompt_results.csv`. All other settings come from `.env.example`.

## Run locally
```
pip install -r requirements.txt
cp .env.example .env   # then fill in OPENAI_API_KEY
python run_bulk_id.py --input input_run1.csv --output input_run1_results.csv --limit 3
```
Input columns: `record_id, dma_code, run_id`, plus the columns behind the prompt's variables (`address, master_property name, ownership_type, units, building_count, owner, property_manager` for the main prompt; just `address, master_property name` for Owen's Prompt). Extra columns are carried through to the output.

If the output file already exists, rows whose (record_id, run_id) pair already succeeded are skipped. Everything else is processed, and previously failed rows are replaced.
