#!/usr/bin/env python3
"""Parse ING Bank Romania transaction CSV and import into SQLite (incremental).

Re-importing is safe: each transaction gets a stable `dedup_key`, and rows already
present are skipped (INSERT OR IGNORE) instead of duplicated. Nothing is deleted.

Usage:
    python import.py [path/to/export.csv]
"""

from __future__ import annotations

import csv
import re
import shutil
import sqlite3
import sys
from collections import Counter
from datetime import date, datetime
from pathlib import Path

DEFAULT_CSV = Path("Tranzactii_24-04-2026_12-07-55.csv")
CSV_PATH = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_CSV
DB_PATH = Path("transactions.db")

MONTHS_RO = {
    "ianuarie": 1, "februarie": 2, "martie": 3, "aprilie": 4,
    "mai": 5, "iunie": 6, "iulie": 7, "august": 8,
    "septembrie": 9, "octombrie": 10, "noiembrie": 11, "decembrie": 12,
}

RO_DATE_RE = re.compile(
    r"^(\d{1,2})\s+(ianuarie|februarie|martie|aprilie|mai|iunie|iulie|"
    r"august|septembrie|octombrie|noiembrie|decembrie)\s+(\d{4})$",
    re.IGNORECASE,
)

# Categories auto-assigned from transaction type (no vendor lookup needed)
TYPE_CATEGORIES: dict[str, str] = {
    "Retragere numerar": "cash",
    "Rata Credit": "loan",
    "Suma transferata din linia de credit": "credit_line",
    "Incasare": "income",
    "Prima asigurare de viata (credite)": "insurance",
    "Prima asigurare ING Credit Protect": "insurance",
    "Taxe si comisioane": "bank_fees",
    "Recalculari aferente creditului": "loan",
    "Actualizare dobanda": "loan",
    "Realimentare Extra'ROL Home'Bank": "credit_line",
    "Transfer Home'Bank": "transfer",
    "Plata debit direct": "transfer",
}

SCHEMA = """\
CREATE TABLE IF NOT EXISTS transactions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    settlement_date DATE    NOT NULL,
    auth_date       DATE,
    type            TEXT    NOT NULL,
    amount          REAL,
    direction       TEXT    NOT NULL,
    card            TEXT,
    vendor_raw      TEXT,
    vendor_norm     TEXT,
    beneficiary     TEXT,
    details         TEXT,
    category        TEXT,
    notes           TEXT,
    auth_number     TEXT,
    dedup_key       TEXT
);

CREATE TABLE IF NOT EXISTS vendor_tags (
    vendor_norm TEXT PRIMARY KEY,
    category    TEXT NOT NULL,
    confirmed   INTEGER DEFAULT 0
);
"""


def parse_date_ro(s: str) -> date | None:
    m = RO_DATE_RE.match(s.strip())
    if not m:
        return None
    day, month_name, year = m.groups()
    return date(int(year), MONTHS_RO[month_name.lower()], int(day))


def parse_date_dmy(s: str) -> date | None:
    for fmt in ("%d-%m-%Y", "%d.%m.%Y"):
        try:
            return datetime.strptime(s.strip(), fmt).date()
        except ValueError:
            pass
    return None


def parse_amount(s: str) -> float | None:
    if not s or not s.strip():
        return None
    try:
        return float(s.strip().replace(".", "").replace(",", "."))
    except ValueError:
        return None


def normalize_vendor(raw: str) -> str:
    # Drop country/city suffix (everything after the first double-space)
    name = re.split(r"\s{2,}", raw)[0].strip()
    # Strip common Romanian legal suffixes
    name = re.sub(r"\s+S\.?\s*R\.?\s*L\.?$", "", name, flags=re.IGNORECASE).strip()
    name = re.sub(r"\s+S\.?\s*A\.?$", "", name, flags=re.IGNORECASE).strip()
    # Skip leading tokens that are codes (contain digits), then collect alpha tokens
    # until we hit another digit token — this handles "P038 LUKOIL CL C2" → "LUKOIL CL"
    # and "MEGAIMAGE 1072 SG Col" → "MEGAIMAGE"
    tokens = name.split()
    i = 0
    while i < len(tokens) and re.search(r"\d", tokens[i]):
        i += 1
    brand: list[str] = []
    for tok in tokens[i:]:
        if re.search(r"\d", tok):
            break
        brand.append(tok)
    return " ".join(brand) if brand else name


def read_csv(filepath: Path) -> list[list[str]]:
    for enc in ("utf-8-sig", "utf-8", "windows-1252", "latin-1"):
        try:
            text = filepath.read_text(encoding=enc)
            return list(csv.reader(text.splitlines()))
        except UnicodeDecodeError:
            continue
    raise ValueError(f"Cannot decode {filepath}")


def detect_amount_columns(rows: list[list[str]]) -> tuple[int, int]:
    """Locate the Debit/Credit column indices from the header row.

    ING has shipped at least two layouts: the older one puts Debit at col 5,
    the newer one (with a Balanta column) puts it at col 4. Credit has stayed at
    col 6 but we read it from the header too. Falls back to the legacy 5/6.
    """
    for row in rows:
        cells = [c.strip().lower() for c in row]
        if "debit" in cells and "credit" in cells:
            return cells.index("debit"), cells.index("credit")
    return 5, 6


def natural_key(t: dict) -> str:
    """Identity of a transaction, independent of when/how many times it's imported.

    Card transactions carry a unique authorization number ("Numar autorizare") +
    authorization date — the most reliable identity. Everything else (transfers,
    credit-line draws, fees, income) falls back to a composite of its stable fields
    plus the bank reference when present.
    """
    if t.get("auth_number"):
        return f"A|{t['auth_number']}|{t['auth_date'] or ''}|{t['amount']}"
    return "N|" + "|".join([
        t["settlement_date"] or "",
        t["type"] or "",
        f"{t['amount']}",
        t["direction"] or "",
        t.get("beneficiary") or "",
        t.get("reference") or "",
    ])


def assign_dedup_keys(transactions: list[dict]) -> None:
    """Set a unique dedup_key on each transaction.

    Some genuinely distinct rows share a natural key (e.g. two identical credit-line
    draws on the same day with no reference). We disambiguate them with an occurrence
    index: the Nth row sharing a natural key gets suffix "#N". Because a full statement
    export always lists the same identical rows in the same order, re-importing an
    overlapping period reproduces the same keys and skips them cleanly.
    """
    seen: Counter[str] = Counter()
    for t in transactions:
        nk = natural_key(t)
        t["dedup_key"] = f"{nk}#{seen[nk]}"
        seen[nk] += 1


def parse_transactions(filepath: Path) -> list[dict]:
    rows = read_csv(filepath)
    debit_col, credit_col = detect_amount_columns(rows)
    transactions: list[dict] = []

    # State for the transaction currently being assembled
    cur_type: str | None = None
    cur_date_str: str | None = None
    cur_amount: float | None = None
    cur_direction: str | None = None
    cur_details: dict[str, str] = {}

    def flush() -> None:
        nonlocal cur_type, cur_date_str, cur_amount, cur_direction, cur_details
        if cur_type is None:
            return

        d = cur_details
        settlement = (
            parse_date_dmy(d.get("Data finalizarii (decontarii)", ""))
            or parse_date_dmy(d.get("Data", ""))
            or parse_date_ro(cur_date_str or "")
        )
        auth_date = parse_date_dmy(d.get("Data autorizarii", ""))
        auth_number = d.get("Numar autorizare") or None
        reference = d.get("Referinta") or None

        card_raw = d.get("Numar card", "")
        card = re.sub(r"[* ]+", "", card_raw).strip() or None

        vendor_raw = d.get("Tranzactie la", None)
        vendor_norm = normalize_vendor(vendor_raw) if vendor_raw else None

        # Beneficiary for transfers out, Ordonator for incoming
        beneficiary = d.get("Beneficiar") or d.get("Ordonator") or None

        # Use beneficiary as vendor_norm for transfers so vendor_tags can tag them
        if vendor_norm is None and beneficiary:
            vendor_norm = normalize_vendor(beneficiary)

        detail_parts = []
        if "Detalii" in d:
            detail_parts.append(d["Detalii"])
        if reference:
            detail_parts.append("Ref:" + reference)
        detail_text = " | ".join(detail_parts) or None

        category = TYPE_CATEGORIES.get(cur_type)

        transactions.append({
            "settlement_date": settlement.isoformat() if settlement else None,
            "auth_date": auth_date.isoformat() if auth_date else None,
            "type": cur_type,
            "amount": cur_amount,
            "direction": cur_direction,
            "card": card,
            "vendor_raw": vendor_raw,
            "vendor_norm": vendor_norm,
            "beneficiary": beneficiary,
            "details": detail_text,
            "category": category,
            "notes": None,
            "auth_number": auth_number,
            "reference": reference,  # transient: used only for the dedup key
        })

        cur_type = None
        cur_details = {}

    for row in rows:
        while len(row) <= max(6, debit_col, credit_col):
            row.append("")

        col0 = row[0].strip()
        col3 = row[3].strip()
        debit = row[debit_col].strip()
        credit = row[credit_col].strip()

        if parse_date_ro(col0):
            flush()
            cur_date_str = col0
            cur_type = col3
            cur_amount = parse_amount(debit if debit else credit)
            cur_direction = "debit" if debit else "credit"
            cur_details = {}
        elif cur_type is not None and ":" in col3:
            key, _, val = col3.partition(":")
            cur_details.setdefault(key.strip(), val.strip())

    flush()
    assign_dedup_keys(transactions)
    return transactions


def apply_vendor_tags(con: sqlite3.Connection) -> int:
    """Fill in category from vendor_tags for any POS transaction still untagged."""
    con.execute(
        """UPDATE transactions
           SET category = (
               SELECT vt.category FROM vendor_tags vt
               WHERE vt.vendor_norm = transactions.vendor_norm
           )
           WHERE vendor_norm IS NOT NULL
             AND EXISTS (
                 SELECT 1 FROM vendor_tags vt
                 WHERE vt.vendor_norm = transactions.vendor_norm
             )"""
    )
    con.commit()
    (count,) = con.execute(
        "SELECT COUNT(*) FROM transactions WHERE category IS NOT NULL AND type = 'Cumparare POS'"
    ).fetchone()
    return count


def ensure_schema(con: sqlite3.Connection) -> None:
    """Create tables/columns/index, migrating older databases in place."""
    con.executescript(SCHEMA)
    cols = {r[1] for r in con.execute("PRAGMA table_info(transactions)")}
    for col in ("auth_number", "dedup_key"):
        if col not in cols:
            con.execute(f"ALTER TABLE transactions ADD COLUMN {col} TEXT")
    con.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_txn_dedup ON transactions(dedup_key)"
    )
    con.commit()


def import_to_db(transactions: list[dict], db_path: Path) -> tuple[sqlite3.Connection, int, int]:
    con = sqlite3.connect(db_path)
    ensure_schema(con)

    # Rows imported before dedup keys existed can't be matched reliably (their auth
    # number was never stored), so a partial migration would duplicate them. Rebuild
    # the transactions table once from the CSV; vendor_tags is left untouched.
    legacy = con.execute(
        "SELECT COUNT(*) FROM transactions WHERE dedup_key IS NULL"
    ).fetchone()[0]
    if legacy:
        print(f"  Migrating {legacy} legacy row(s): rebuilding transactions "
              f"(vendor_tags preserved)")
        con.execute("DELETE FROM transactions")
        con.commit()

    before = con.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    con.executemany(
        """INSERT OR IGNORE INTO transactions
               (settlement_date, auth_date, type, amount, direction, card,
                vendor_raw, vendor_norm, beneficiary, details, category, notes,
                auth_number, dedup_key)
           VALUES (:settlement_date, :auth_date, :type, :amount, :direction, :card,
                   :vendor_raw, :vendor_norm, :beneficiary, :details, :category, :notes,
                   :auth_number, :dedup_key)""",
        transactions,
    )
    con.commit()
    after = con.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    inserted = after - before
    skipped = len(transactions) - inserted
    return con, inserted, skipped


KEEP_BACKUPS = 5


def backup_db(db_path: Path) -> Path | None:
    if not db_path.exists():
        return None
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    bak = db_path.with_suffix(f".db.bak-{stamp}")
    shutil.copy2(db_path, bak)
    # Keep only the most recent KEEP_BACKUPS (timestamped names sort chronologically).
    backups = sorted(db_path.parent.glob(f"{db_path.name}.bak-*"))
    for old in backups[:-KEEP_BACKUPS]:
        old.unlink(missing_ok=True)
    return bak


def print_summary(transactions: list[dict], inserted: int, skipped: int) -> None:
    total = len(transactions)
    by_type: dict[str, int] = {}
    for t in transactions:
        by_type[t["type"]] = by_type.get(t["type"], 0) + 1

    w = 48
    print(f"\n{'─' * (w + 22)}")
    print(f"  Rows in CSV        : {total}")
    print(f"  Inserted (new)     : {inserted}")
    print(f"  Skipped (existing) : {skipped}")
    print(f"\n  Breakdown by type:")
    for typ, count in sorted(by_type.items(), key=lambda x: -x[1]):
        cat = TYPE_CATEGORIES.get(typ, "(needs vendor tag)")
        print(f"    {count:4d}  {typ:<{w}} → {cat}")
    print(f"{'─' * (w + 22)}\n")


if __name__ == "__main__":
    if not CSV_PATH.exists():
        sys.exit(f"CSV not found: {CSV_PATH}")

    print(f"Parsing {CSV_PATH} ...")
    txns = parse_transactions(CSV_PATH)
    print(f"Found {len(txns)} transactions")

    bak = backup_db(DB_PATH)
    if bak:
        print(f"Backed up existing DB → {bak}")

    con, inserted, skipped = import_to_db(txns, DB_PATH)
    tagged = apply_vendor_tags(con)
    if tagged:
        print(f"Applied vendor_tags to {tagged} POS transactions")
    con.close()

    print(f"Saved → {DB_PATH}")
    print_summary(txns, inserted, skipped)
