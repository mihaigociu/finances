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
   re-run, never deletes, skips rows already present. Re-import also **refreshes** the
   parse-derived display fields (`vendor_norm`, `vendor_raw`, `beneficiary`, `details`) on
   rows that already exist (matched by the stable `dedup_key`), so a change to the parsing
   logic propagates to old rows on the next import without a rebuild.
   ```bash
   .venv/bin/python import.py [path/to/export.csv]   # defaults to the bundled April CSV
   ```
   The same logic is exposed in the dashboard: the header **Import CSV** button →
   `POST /api/import` (multipart `file`), which loads `import.py` via importlib and calls
   `parse_transactions` → `backup_db` → `import_to_db` → `recompute_categories`. Prefer
   editing `import.py`; the endpoint reuses it rather than duplicating parsing.
   (`apply_vendor_tags` remains as a backwards-compatible alias of `recompute_categories`.)
2. **`tag.py`** — send still-untagged `vendor_norm` values to the Claude API to assign a
   spending category; caches results in `vendor_tags` and applies them to transactions.
   Needs `ANTHROPIC_API_KEY`. Runs against whatever POS merchants lack a category.
3. **`app.py`** — Flask JSON API + static dashboard in `static/` (vanilla JS + Chart.js,
   no build step). Tabs: Overview, Monthly, Transactions, Merchants.

## Data model (`transactions.db`)

- **`transactions`** — one row per bank transaction. Category is derived, never
  hand-entered — it's **recomputed** (`recompute_categories` in `import.py`) from two
  layers, the second overriding the first: (1) transaction *type* (`TYPE_CATEGORIES`),
  (2) `vendor_tags` per-merchant default. Because it's purely derived, editing/deleting a
  tag and re-running restores the correct state.
- **`vendor_tags`** — `(vendor_norm, category, confirmed)`. The only hand-authored state.
  `confirmed=0` = guessed by `tag.py`; `confirmed=1` = set by hand in the Merchants tab.
  Preserved across re-imports. Split-by-bank (see below) means one counterparty can appear
  as several `vendor_norm`s (`Gociu Mihai`, `Gociu Mihai BCR`), each tagged independently.
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
- **Detail-line field labels also vary between layouts** — ING renames the `key:value`
  lines inside a transaction block (e.g. the merchant line is `Tranzactie la:` in older
  exports but `Terminal:` in the newest). `import.py` looks each field up via the
  `DETAIL_LABELS` synonym map (through the `detail()` helper), never a single hardcoded
  label — add a synonym there when a new export renames one; don't hardcode at the call
  site. The newest `Terminal:` values are also single-spaced with a `<COUNTRY_CODE> <city>`
  suffix (vs the old double-space separator), which `normalize_vendor` /
  `strip_country_suffix` trim so merchants still normalize to the same brand and match
  `vendor_tags`. Symptom of getting this wrong: `vendor_norm` is `NULL` and the dashboard
  shows the transaction *type* (e.g. `Cumparare POS`) in place of the merchant.
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
- **Self-transfers are split by destination bank.** When a transfer's beneficiary *is the
  account holder* (`is_self_transfer` compares the `Titular cont:` name to the beneficiary,
  order-insensitive) and the money goes to an account at another bank, `vendor_norm` gets a
  short bank-name suffix from the destination IBAN's bank code (`external_bank` +
  `IBAN_BANK_CODES`, e.g. `RNCB` → `BCR`). So `Gociu Mihai` (own ING accounts) and `Gociu
  Mihai BCR` (a loan repaid into a BCR account) become separate, separately-taggable
  merchants. Only self-transfers are suffixed — transfers to other people/orgs keep plain
  names. Applies on (re-)import; requires re-importing the CSV to re-split existing rows.
- `tag.py` pins `model="claude-opus-4-7"` — outdated; bump to a current model when touched.

## API endpoints (`app.py`)
`/api/years`, `/api/cards`, `/api/categories`, `/api/summary`, `/api/monthly`,
`/api/vendors`, `/api/transactions` (paginated, searchable), `/api/merchants` (+ `POST
/api/merchants/tag` to categorize a merchant → writes `vendor_tags` confirmed=1 and calls
`recompute_categories` so the new default propagates to all its transactions), `POST
/api/import` (upload a CSV to import). Spending endpoints share `spending_filter()`.
