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
from typing import NamedTuple

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


# Detail-line field labels ING has used, tried in order. The bank renames these between
# statement formats (e.g. the merchant line has been both "Tranzactie la" and "Terminal"),
# so each logical field matches on any known synonym rather than one fixed label. Add a
# synonym here — don't hardcode a single label at the call site — when a new export renames
# a field.
DETAIL_LABELS: dict[str, list[str]] = {
    "settlement":   ["Data finalizarii (decontarii)", "Data"],
    "auth_date":    ["Data autorizarii"],
    "auth_number":  ["Numar autorizare"],
    "reference":    ["Referinta"],
    "card":         ["Numar card"],
    "merchant":     ["Tranzactie la", "Terminal"],
    "counterparty": ["Beneficiar", "Ordonator"],
    "dest_account": ["In contul"],
    "note":         ["Detalii"],
}

# Romanian IBANs encode the bank in characters 5–8 (e.g. RO28 **RNCB** 0106…). We use
# that to tell apart transfers to accounts held at *other* banks — e.g. a loan repaid
# into a BCR account vs. moving money between one's own ING accounts — by appending the
# bank's short name to vendor_norm, so each becomes a distinct, separately-taggable
# "merchant". Unknown bank codes fall back to the 4-letter code itself. Extend as needed.
OWN_BANK_CODE = "INGB"  # the account holder's own bank; transfers here aren't "external"
IBAN_BANK_CODES: dict[str, str] = {
    "RNCB": "BCR",
    "BTRL": "BT",
    "BRDE": "BRD",
    "RZBR": "Raiffeisen",
    "BACX": "UniCredit",
    "CECE": "CEC",
    "BREL": "Libra",
    "OTPV": "OTP",
    "PIRB": "First Bank",
}


def external_bank(account: str | None) -> str | None:
    """Short name of a transfer's destination bank, or None for own-bank / non-IBAN
    accounts. Returns None for ING (own bank) and for internal account numbers that
    aren't RO IBANs, so only transfers to another bank get a vendor_norm suffix."""
    if not account:
        return None
    m = re.match(r"RO\d{2}([A-Z]{4})", account.strip().upper())
    if not m or m.group(1) == OWN_BANK_CODE:
        return None
    return IBAN_BANK_CODES.get(m.group(1), m.group(1))


# Honorifics ING prefixes to the account-holder name ("Titular cont: DL Mihai Gociu").
_HONORIFICS = {"DL", "DNA", "D-NA", "DOMNUL", "DOAMNA"}


def _name_tokens(name: str) -> frozenset:
    """Uppercase word set of a name, minus honorifics — order-insensitive for matching."""
    words = re.sub(r"[^\w\s]", " ", name.upper()).split()
    return frozenset(w for w in words if w not in _HONORIFICS)


def is_self_transfer(beneficiary: str | None, holder: str | None) -> bool:
    """True when a transfer's beneficiary is the account holder themselves (same name,
    any word order). Used to split a self-transfer by destination bank while leaving
    transfers to other people/organizations named plainly."""
    if not beneficiary or not holder:
        return False
    return _name_tokens(beneficiary) == _name_tokens(holder)

# Country codes ING appends before the city in the newer single-spaced merchant format.
# "RO" (domestic) covers almost everything; the rest cover foreign card transactions.
_COUNTRY_CODES = "NL GB IE US FR IT ES AT BE CH PL CZ HU BG GR PT DK SE NO FI IL TR AE".split()


def detail(d: dict, field: str) -> str | None:
    """Value of a logical detail field, matched against any label ING uses for it."""
    for label in DETAIL_LABELS[field]:
        if d.get(label):
            return d[label]
    return None


def strip_country_suffix(name: str) -> str:
    """Drop a trailing "<COUNTRY_CODE> <city>" from the single-spaced merchant format.

    Domestic merchants always carry "RO", so we cut at its last occurrence — this avoids
    tripping on "DE"/"LA"/etc. that appear inside city names (e.g. "OARTA DE SUS"). Foreign
    card transactions ("Disney Plus NL Hoofddorp") use another country code, handled as a
    fallback. Names in the old double-spaced format have no country token left by this point
    and are returned unchanged.
    """
    matches = list(re.finditer(r"\s+RO\s+\S", name))
    if not matches:
        pattern = r"\s+(?:" + "|".join(_COUNTRY_CODES) + r")\s+\S"
        matches = list(re.finditer(pattern, name))
    return name[:matches[-1].start()].strip() if matches else name


def normalize_vendor(raw: str) -> str:
    # Two export formats append a country/city suffix after the merchant name:
    #   old: separated by 2+ spaces          -> "LUKOIL  RO  CLUJ"
    #   new: single-spaced with country code  -> "KIMY S BAKERY SRL RO OARTA DE SUS"
    name = re.split(r"\s{2,}", raw)[0].strip()   # old format: cut at the gap
    name = strip_country_suffix(name)            # new format: cut at the country code
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


class Columns(NamedTuple):
    """Column indices, in *data-row* coordinates, for the fields the parser reads."""
    date: int      # transaction date, e.g. "01 iulie 2026" — marks a new transaction
    details: int   # transaction type on the start row; "key:value" detail lines after
    debit: int     # money out
    credit: int    # money in


# Header cell text ING uses for each logical column (compared lowercased, exact match).
HEADER_LABELS = {
    "date": "data",
    "details": "detalii tranzactie",
    "debit": "debit",
    "credit": "credit",
}

# Used only when a statement has no recognizable header row.
LEGACY_COLUMNS = Columns(date=0, details=3, debit=5, credit=6)


def find_date_column(rows: list[list[str]]) -> int:
    """Column in which transaction rows actually carry their Romanian date.

    Each transaction starts with a row whose date cell reads e.g. "01 iulie 2026". We
    locate that column empirically instead of assuming col 0, so the mapping tolerates a
    leading-column shift. Returns the most frequent such column (0 if none is found).
    """
    counts: Counter = Counter()
    for row in rows:
        for i, cell in enumerate(row):
            if parse_date_ro(cell.strip()):
                counts[i] += 1
    return counts.most_common(1)[0][0] if counts else 0


def detect_columns(rows: list[list[str]]) -> Columns:
    """Map each field to its data-row column by reading the header, not by fixed order.

    ING has shipped several statement layouts whose columns sit in different positions
    (and one where a "Balanta" column was inserted). Rather than hardcode indices, we
    find the header row and look each column up by its label ("Data", "Detalii
    tranzactie", "Debit", "Credit").

    One quirk needs correcting: the newest export shifts the *entire header row*
    sideways relative to the data rows (its "Data" label sits a column to the right of
    where the dates themselves appear), so header indices don't line up with the data.
    We measure that shift by comparing the header's date column against where dates
    actually occur, then translate every header index into data-row coordinates. Falls
    back to the legacy fixed layout when there is no recognizable header.
    """
    date_col = find_date_column(rows)
    for row in rows:
        cells = [c.strip().lower() for c in row]
        if HEADER_LABELS["debit"] in cells and HEADER_LABELS["credit"] in cells:
            # Distance the header row is shifted from the data rows (0 when aligned).
            shift = (cells.index(HEADER_LABELS["date"]) - date_col
                     if HEADER_LABELS["date"] in cells else 0)

            def data_col(field: str) -> int:
                label = HEADER_LABELS[field]
                if label in cells:
                    return cells.index(label) - shift
                return getattr(LEGACY_COLUMNS, field)

            return Columns(
                date=date_col,
                details=data_col("details"),
                debit=data_col("debit"),
                credit=data_col("credit"),
            )
    return LEGACY_COLUMNS


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
    cols = detect_columns(rows)
    transactions: list[dict] = []

    # The account holder ("Titular cont: DL Mihai Gociu"), used to detect self-transfers.
    account_holder: str | None = None

    # State for the transaction currently being assembled
    cur_type: str | None = None
    cur_date_str: str | None = None
    cur_amount: float | None = None
    cur_direction: str | None = None
    cur_details: dict[str, str] = {}

    def flush() -> None:
        nonlocal cur_type, cur_date_str, cur_amount, cur_direction, cur_details
        if not cur_type:
            return

        d = cur_details
        settlement = (
            parse_date_dmy(detail(d, "settlement") or "")
            or parse_date_ro(cur_date_str or "")
        )
        auth_date = parse_date_dmy(detail(d, "auth_date") or "")
        auth_number = detail(d, "auth_number")
        reference = detail(d, "reference")

        card_raw = detail(d, "card") or ""
        card = re.sub(r"[* ]+", "", card_raw).strip() or None

        vendor_raw = detail(d, "merchant")
        vendor_norm = normalize_vendor(vendor_raw) if vendor_raw else None

        # Beneficiary for transfers out, Ordonator for incoming
        beneficiary = detail(d, "counterparty")

        # Use beneficiary as vendor_norm for transfers so vendor_tags can tag them.
        # When the money goes to an account at another bank, append that bank's short
        # name (e.g. "Gociu Mihai BCR") so it's a separate merchant from same-name
        # transfers to one's own ING accounts.
        if vendor_norm is None and beneficiary:
            vendor_norm = normalize_vendor(beneficiary)
            if is_self_transfer(beneficiary, account_holder):
                bank = external_bank(detail(d, "dest_account"))
                if bank:
                    vendor_norm = f"{vendor_norm} {bank}"

        note = detail(d, "note")
        detail_parts = []
        if note:
            detail_parts.append(note)
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
        while len(row) <= max(cols):
            row.append("")

        # The holder line ("Titular cont: <name>") sits above each page's transactions,
        # in its own column — scan the whole row rather than assuming a fixed position.
        if account_holder is None:
            for cell in row:
                if cell.strip().startswith("Titular cont"):
                    account_holder = cell.split(":", 1)[1].strip()
                    break

        date_cell = row[cols.date].strip()
        detail_cell = row[cols.details].strip()
        debit = row[cols.debit].strip()
        credit = row[cols.credit].strip()

        if parse_date_ro(date_cell):
            flush()
            # Newest exports append a trailer of pending/unsettled transactions under a
            # second header with its own column shift; those date-rows carry no type in
            # the details column. A real transaction always does, so skip typeless rows.
            if not detail_cell:
                cur_type = None
                continue
            cur_date_str = date_cell
            cur_type = detail_cell
            cur_amount = parse_amount(debit if debit else credit)
            cur_direction = "debit" if debit else "credit"
            cur_details = {}
        elif cur_type is not None and ":" in detail_cell:
            key, _, val = detail_cell.partition(":")
            cur_details.setdefault(key.strip(), val.strip())

    flush()
    assign_dedup_keys(transactions)
    return transactions


def recompute_categories(con: sqlite3.Connection) -> int:
    """Recompute every transaction's category from scratch, in precedence order:

        1. transaction type      (TYPE_CATEGORIES)
        2. vendor_tags default   (per-merchant, overrides the type)

    Category is purely derived from those inputs and never hand-authored (see
    CLAUDE.md), so this is idempotent: editing/deleting a tag and re-running restores
    the correct state. Returns the number of POS transactions that ended up categorized.
    """
    # 1. Base category from transaction type.
    con.execute("UPDATE transactions SET category = NULL")
    for typ, cat in TYPE_CATEGORIES.items():
        con.execute("UPDATE transactions SET category = ? WHERE type = ?", (cat, typ))

    # 2. Per-merchant default from vendor_tags (overrides the type category).
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


# Backwards-compatible name kept so existing callers (app.py) still work.
apply_vendor_tags = recompute_categories


def ensure_schema(con: sqlite3.Connection) -> None:
    """Create tables/columns/index, migrating older databases in place."""
    con.executescript(SCHEMA)
    cols = {r[1] for r in con.execute("PRAGMA table_info(transactions)")}
    for col in ("auth_number", "dedup_key"):
        if col not in cols:
            con.execute(f"ALTER TABLE transactions ADD COLUMN {col} TEXT")
    # Drop the retired amount-rules table (superseded by destination-bank vendor_norm
    # splitting); harmless no-op once it's gone.
    con.execute("DROP TABLE IF EXISTS vendor_amount_rules")
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

    # Refresh parse-derived display fields on rows that already existed. dedup_key is
    # stable (it doesn't depend on vendor_norm), so re-importing after a parsing change
    # (e.g. the destination-bank vendor_norm suffix) updates existing rows in place
    # rather than requiring a rebuild. Category is left to recompute_categories.
    con.executemany(
        """UPDATE transactions
           SET vendor_raw = :vendor_raw, vendor_norm = :vendor_norm,
               beneficiary = :beneficiary, details = :details
           WHERE dedup_key = :dedup_key""",
        transactions,
    )
    con.commit()

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
    tagged = recompute_categories(con)
    if tagged:
        print(f"Categorized {tagged} POS transactions from vendor tags/rules")
    con.close()

    print(f"Saved → {DB_PATH}")
    print_summary(txns, inserted, skipped)
