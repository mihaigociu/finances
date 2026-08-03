# CLAUDE.md

Personal finance dashboard for an **ING Bank Romania** account. Parses the bank's
CSV transaction export into SQLite, categorizes merchants, and serves an interactive
spending dashboard. Single user, runs locally, all amounts in **RON**.

## Running

```bash
.venv/bin/python app.py          # dashboard at http://localhost:5000 (Flask debug, auto-reload)
```

Dependencies (declared in `Pipfile`): `flask` (dashboard) and `anthropic` (only `tag.py`).
The checked-in `.venv` runs Python 3.9.6; `anthropic` isn't installed there yet, so
`tag.py` needs `pip install anthropic` before its first run. No `Pipfile.lock`.

## Data pipeline (3 stages)

1. **`import.py`** — parse a CSV export → `transactions.db`. **Incremental**: safe to
   re-run, never deletes, skips rows already present.
   ```bash
   .venv/bin/python import.py [path/to/export.csv]   # defaults to the bundled April CSV
   ```
   The same logic is exposed in the dashboard: the header **Import CSV** button →
   `POST /api/import` (multipart `file`), which loads `import.py` via importlib and calls
   `parse_transactions` → `backup_db` → `import_to_db` → `apply_vendor_tags`. Prefer editing
   `import.py`; the endpoint reuses it rather than duplicating parsing.
2. **`tag.py`** — send still-untagged `vendor_norm` values to the Claude API to assign a
   spending category; caches results in `vendor_tags` and applies them to transactions.
   Needs `ANTHROPIC_API_KEY`. Runs against whatever POS merchants lack a category.
3. **`app.py`** — Flask JSON API + static dashboard in `static/` (vanilla JS + Chart.js,
   no build step). Tabs: Overview, Monthly, Transactions, Merchants.

## Data model (`transactions.db`)

- **`transactions`** — one row per bank transaction. Category is derived, not entered:
  assigned by transaction *type* at import (see `TYPE_CATEGORIES` in `import.py`), then
  overridden per-merchant via `vendor_tags`.
- **`vendor_tags`** — `(vendor_norm, category, confirmed)`. The **only hand-authored
  state.** `confirmed=0` = guessed by `tag.py`; `confirmed=1` = set by hand in the
  Merchants tab. Preserved across re-imports.
- Key columns: `auth_number` + `dedup_key` (dedup, see below), `vendor_norm` (normalized
  merchant/beneficiary name — the join key to `vendor_tags`), `direction` (debit/credit).

### Categories & what counts as spending
`ALLOWED_CATEGORIES` in `app.py` is the source of truth (also exposed at
`/api/categories`; `app.js` `COLORS` must have an entry per category). Spending totals
**exclude** `NON_SPENDING = ("credit_line", "income", "savings")` — money borrowed, money
in, or money moved to your own accounts. `loan` and `bank_fees` **do** count (real credit
costs). `savings` flags transfers to own accounts (e.g. beneficiary = the account holder).

## Conventions & gotchas (learned the hard way)

- **ING has shipped several CSV layouts** with columns in different positions (older = 7
  cols, Debit col 5; a `Balanta` variant = 8 cols, Debit col 4; a newest one whose amounts
  land at Debit col 6 / Credit col 8). `import.py` maps every column by its **header
  label** (`detect_columns` → `Columns`), never by fixed order — do **not** hardcode
  indices. Two subtleties it handles: (a) the newest export shifts the *entire header row*
  one column right of the data rows, so `detect_columns` measures that shift against the
  date column (`find_date_column`) and translates header indices into data coordinates;
  (b) that export appends a trailer of pending/unsettled transactions under a second header
  with its own shift — the parser skips those (date-rows with no type in the details
  column). Symptom of getting column mapping wrong: all amounts import as `NULL` and nothing
  deduplicates.
- **Dedup key.** Each transaction gets a stable `dedup_key`; import is `INSERT OR IGNORE`.
  Card rows key on `Numar autorizare` + auth date + amount; everything else on a composite
  of date/type/amount/direction/beneficiary/reference. A `#N` occurrence suffix
  disambiguates genuinely-identical rows (e.g. two equal credit-line draws same day). If
  amounts are wrong, keys don't match and re-imports duplicate.
- **`import.py` backs up the DB** (`transactions.db.bak-<timestamp>`) before any write,
  keeping only the most recent `KEEP_BACKUPS` (5).
- **Legacy migration:** rows without a `dedup_key` trigger a one-time rebuild of the
  `transactions` table from the CSV (vendor_tags preserved). Only relevant to pre-dedup DBs.
- **Never hand-edit `transactions.category`** — it's regenerated. Categorize via the
  Merchants tab / `vendor_tags` instead, so it survives re-import.
- `vendor_norm` is also set from the beneficiary for transfers, so counterparties (people,
  charities, insurers) show up as taggable "merchants".
- `tag.py` pins `model="claude-opus-4-7"` — outdated; bump to a current model when touched.

## API endpoints (`app.py`)
`/api/years`, `/api/cards`, `/api/categories`, `/api/summary`, `/api/monthly`,
`/api/vendors`, `/api/transactions` (paginated, searchable), `/api/merchants` (+ `POST
/api/merchants/tag` to categorize a merchant → writes `vendor_tags` confirmed=1 and re-tags
its transactions), `POST /api/import` (upload a CSV to import). Spending endpoints share
`spending_filter()`.
