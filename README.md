# Finances

A self-hosted personal finance dashboard for **ING Bank Romania** accounts. Import your
CSV transaction exports, automatically categorize spending, and explore it through an
interactive dashboard. Runs entirely on your machine — your financial data never leaves it.

## Features

- **CSV import** — upload ING exports straight from the dashboard (or via CLI). Imports are
  incremental and de-duplicated, so re-importing overlapping statements never creates
  duplicates. ING has shipped several different CSV layouts over the years; the parser
  detects columns and detail-line labels by name, so old and new exports both work.
- **Categorization** — transactions get a category from their transaction type
  automatically; merchants (POS purchases) are categorized by hand in the **Merchants**
  tab, and your choices are remembered and survive future imports.
- **Transfers show up as taggable "merchants"** — the beneficiary of a transfer becomes the
  merchant name, so people, charities and insurers can be categorized like any shop.
  Transfers to *yourself* are additionally split by destination bank (e.g. `Gociu Mihai` vs
  `Gociu Mihai BCR`), so money moved to your own account at another bank can be tagged
  separately — typically one as `savings` (excluded from spending) and the other as `loan`.
- **Dashboard** — four tabs: **Overview** (KPIs, spending-by-category donut, top vendors),
  **Monthly** (stacked-bar and per-category line trends), **Transactions** (searchable list
  with month and category filters, plus a *Group by vendor* view that totals spending per
  merchant), and **Merchants** (categorization). Year and card filters apply everywhere.

## Tech stack

Python + Flask (JSON API), SQLite (storage), and a dependency-free vanilla-JS frontend with
[Chart.js](https://www.chartjs.org/). No build step.

## Setup

```bash
git clone https://github.com/mihaigociu/finances.git
cd finances

python3 -m venv .venv
source .venv/bin/activate
pip install flask

python app.py                      # → http://localhost:5000
```

Python 3.9+. Flask is the only dependency; it's declared in `Pipfile` (`flask>=3.1`) if
you prefer `pipenv install`.

## Usage

### 1. Import transactions

Export your transactions from ING as CSV, then either:

- **From the dashboard:** click **Import CSV** in the header and select the file, or
- **From the CLI:** `python import.py path/to/export.csv`

Re-importing is safe — only new transactions are added, and the database is backed up
first (`transactions.db.bak-<timestamp>`, the five most recent are kept).

### 2. Categorize merchants

New POS merchants start uncategorized (and are excluded from spending totals until tagged).
Tag them in the **Merchants** tab: filter to *Untagged* and pick a category from the
dropdown. A merchant's tag takes priority over the category implied by transaction type.

A transaction's category is always *derived* — recomputed from its type plus the merchant's
tag — so it is never edited directly. Fixing a merchant's tag fixes all of its
transactions, past and future.

### What counts as spending

Spending totals cover money actually spent, so they exclude `credit_line` (money
borrowed), `income`, and `savings` (money moved to your own accounts). Loan repayments and
bank fees *do* count — they are real costs. Amounts are assumed to be in **RON**.

## Data & privacy

All data stays local. The CSV exports and the `transactions.db` database are **git-ignored**
and never committed — they contain personal information (name, national ID, account numbers,
and full transaction history).

## Project layout

| File | Purpose |
|------|---------|
| `app.py` | Flask server: JSON API + CSV upload endpoint |
| `import.py` | CSV parsing and incremental, de-duplicated import |
| `static/` | Dashboard frontend (`index.html`, `app.js`, `style.css`) |
| `Pipfile` | Dependency declarations |
| `CLAUDE.md` | Architecture notes and conventions for contributors |
