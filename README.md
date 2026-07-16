# Finances

A self-hosted personal finance dashboard for **ING Bank Romania** accounts. Import your
CSV transaction exports, automatically categorize spending, and explore it through an
interactive dashboard. Runs entirely on your machine — your financial data never leaves it.

## Features

- **CSV import** — upload ING exports straight from the dashboard (or via CLI). Imports are
  incremental and de-duplicated, so re-importing overlapping statements never creates
  duplicates. The parser auto-detects ING's different CSV column layouts.
- **Automatic categorization** — transactions are categorized by type, and merchants
  (POS purchases) are classified into spending categories using the Claude API.
- **Manual categorization** — review and correct any merchant's category by hand in the
  **Merchants** tab; your choices are remembered and survive future imports.
- **Dashboard** — spending by category (donut), monthly trends (stacked bar + line charts),
  and a searchable, filterable transaction list. Filter everything by year and card.

## Tech stack

Python + Flask (JSON API), SQLite (storage), and a dependency-free vanilla-JS frontend with
[Chart.js](https://www.chartjs.org/). No build step.

## Setup

```bash
git clone https://github.com/mihaigociu/finances.git
cd finances

python3 -m venv .venv
source .venv/bin/activate
pip install flask anthropic        # anthropic only needed for tag.py

python app.py                      # → http://localhost:5000
```

Python 3.9+.

## Usage

### 1. Import transactions

Export your transactions from ING as CSV, then either:

- **From the dashboard:** click **Import CSV** in the header and select the file, or
- **From the CLI:** `python import.py path/to/export.csv`

Re-importing is safe — only new transactions are added.

### 2. Categorize merchants

New POS merchants start uncategorized (and are excluded from spending totals until tagged):

- **Automatically** with the Claude API (requires `ANTHROPIC_API_KEY`):
  ```bash
  export ANTHROPIC_API_KEY=sk-...
  python tag.py
  ```
- **By hand** in the **Merchants** tab — filter to *Untagged*, pick a category from the
  dropdown. Manual tags are marked confirmed and take priority.

## Data & privacy

All data stays local. The CSV exports and the `transactions.db` database are **git-ignored**
and never committed — they contain personal information (name, national ID, account numbers,
and full transaction history). Amounts are assumed to be in **RON**.

## Project layout

| File | Purpose |
|------|---------|
| `app.py` | Flask server: JSON API + CSV upload endpoint |
| `import.py` | CSV parsing and incremental, de-duplicated import |
| `tag.py` | Claude-based merchant categorization |
| `static/` | Dashboard frontend (`index.html`, `app.js`, `style.css`) |
| `CLAUDE.md` | Architecture notes and conventions for contributors |
