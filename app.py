#!/usr/bin/env python3
"""Personal finance dashboard — Flask server."""

from __future__ import annotations

import importlib.util
import shutil
import sqlite3
import tempfile
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory
from werkzeug.utils import secure_filename

DB_PATH = Path("transactions.db")

# Reuse the CSV parsing + dedup-insert logic from import.py. It can't be imported by
# name ("import" is a keyword), so load it explicitly from the same directory.
_import_spec = importlib.util.spec_from_file_location(
    "ingimport", str(Path(__file__).resolve().parent / "import.py")
)
ingimport = importlib.util.module_from_spec(_import_spec)
_import_spec.loader.exec_module(ingimport)

# Apply any pending schema migrations at startup (before the first import).
if DB_PATH.exists():
    _boot = sqlite3.connect(DB_PATH)
    ingimport.ensure_schema(_boot)
    _boot.close()

# Categories a merchant can be assigned by hand (matches the frontend palette).
# "savings" flags money moved to one's own accounts — not a real expense, so it's
# excluded from spending totals (see spending_filter).
ALLOWED_CATEGORIES = [
    "groceries", "bakery", "restaurants", "gas", "health", "transport",
    "telecom", "utilities", "subscriptions", "shopping", "education",
    "insurance", "government", "cash", "transfer", "loan", "bank_fees",
    "other", "income", "savings", "credit_line",
]

# Categories that are not real spending and must be kept out of spending totals.
# credit_line = money drawn from a credit line (borrowing, not spending);
# income/savings = money in or moved to own accounts. loan repayments and
# bank_fees ARE real costs, so they count as spending.
NON_SPENDING = ("credit_line", "income", "savings")

app = Flask(__name__, static_folder="static", static_url_path="")
app.config["MAX_CONTENT_LENGTH"] = 32 * 1024 * 1024  # 32 MB cap on uploads


def db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def spending_filter(card: str | None, year: str | None) -> tuple[str, list]:
    placeholders = ",".join("?" for _ in NON_SPENDING)
    clauses = [
        "direction = 'debit'",
        f"category NOT IN ({placeholders})",
        "category IS NOT NULL",
    ]
    params: list = list(NON_SPENDING)
    if card:
        clauses.append("card = ?")
        params.append(card)
    if year:
        clauses.append("strftime('%Y', COALESCE(auth_date, settlement_date)) = ?")
        params.append(year)
    return " AND ".join(clauses), params


@app.route("/")
def index():
    return send_from_directory("static", "index.html")


@app.route("/api/years")
def years():
    con = db()
    rows = con.execute(
        """SELECT DISTINCT strftime('%Y', COALESCE(auth_date, settlement_date)) AS y
           FROM transactions WHERE y IS NOT NULL ORDER BY y DESC"""
    ).fetchall()
    con.close()
    return jsonify([r["y"] for r in rows])


@app.route("/api/cards")
def cards():
    con = db()
    rows = con.execute(
        "SELECT DISTINCT card FROM transactions WHERE card IS NOT NULL ORDER BY card"
    ).fetchall()
    con.close()
    return jsonify([r["card"] for r in rows])


@app.route("/api/summary")
def summary():
    card = request.args.get("card")
    year = request.args.get("year")
    where, params = spending_filter(card, year)
    con = db()
    rows = con.execute(
        f"""SELECT category, ROUND(SUM(amount), 2) AS total, COUNT(*) AS count
            FROM transactions WHERE {where}
            GROUP BY category ORDER BY total DESC""",
        params,
    ).fetchall()
    con.close()
    return jsonify([dict(r) for r in rows])


@app.route("/api/monthly")
def monthly():
    card = request.args.get("card")
    year = request.args.get("year")
    where, params = spending_filter(card, year)
    con = db()
    rows = con.execute(
        f"""SELECT strftime('%Y-%m', COALESCE(auth_date, settlement_date)) AS month,
                   category, ROUND(SUM(amount), 2) AS total
            FROM transactions WHERE {where}
            GROUP BY month, category ORDER BY month""",
        params,
    ).fetchall()
    con.close()
    return jsonify([dict(r) for r in rows])


@app.route("/api/vendors")
def vendors():
    card = request.args.get("card")
    year = request.args.get("year")
    limit = min(50, int(request.args.get("limit", 20)))
    where, params = spending_filter(card, year)
    con = db()
    rows = con.execute(
        f"""SELECT vendor_norm, category, ROUND(SUM(amount), 2) AS total, COUNT(*) AS count
            FROM transactions
            WHERE {where} AND vendor_norm IS NOT NULL
            GROUP BY vendor_norm ORDER BY total DESC LIMIT ?""",
        params + [limit],
    ).fetchall()
    con.close()
    return jsonify([dict(r) for r in rows])


@app.route("/api/transactions")
def transactions():
    card = request.args.get("card")
    year = request.args.get("year")
    month = request.args.get("month")
    category = request.args.get("category")
    group = request.args.get("group")
    search = request.args.get("search", "").strip()
    page = max(1, int(request.args.get("page", 1)))
    per_page = min(100, int(request.args.get("per_page", 50)))

    clauses = ["1=1"]
    params: list = []
    if card:
        clauses.append("card = ?")
        params.append(card)
    if year:
        clauses.append("strftime('%Y', COALESCE(auth_date, settlement_date)) = ?")
        params.append(year)
    if month:
        clauses.append("strftime('%m', COALESCE(auth_date, settlement_date)) = ?")
        params.append(month)
    if category:
        clauses.append("category = ?")
        params.append(category)
    if search:
        clauses.append(
            "(vendor_norm LIKE ? OR vendor_raw LIKE ? OR beneficiary LIKE ? OR details LIKE ?)"
        )
        like = f"%{search}%"
        params.extend([like, like, like, like])

    where = " AND ".join(clauses)
    con = db()

    if group == "vendor":
        # Collapse the same filtered set to one row per vendor (spending = debit sum),
        # ordered biggest-first — e.g. top groceries vendors for a month.
        rows = con.execute(
            f"""SELECT COALESCE(vendor_norm, beneficiary, type) AS vendor,
                       MAX(category) AS category,
                       COUNT(*) AS count,
                       ROUND(SUM(CASE WHEN direction = 'debit' THEN amount ELSE 0 END), 2) AS total
                FROM transactions WHERE {where}
                GROUP BY COALESCE(vendor_norm, beneficiary, type)
                ORDER BY total DESC, count DESC""",
            params,
        ).fetchall()
        con.close()
        return jsonify({"group": "vendor", "vendors": [dict(r) for r in rows]})

    total = con.execute(
        f"SELECT COUNT(*) FROM transactions WHERE {where}", params
    ).fetchone()[0]
    rows = con.execute(
        f"""SELECT settlement_date, auth_date, type, amount, direction, card,
                   vendor_norm, beneficiary, details, category
            FROM transactions WHERE {where}
            ORDER BY COALESCE(auth_date, settlement_date) DESC, id DESC
            LIMIT ? OFFSET ?""",
        params + [per_page, (page - 1) * per_page],
    ).fetchall()
    con.close()
    return jsonify({
        "total": total,
        "page": page,
        "per_page": per_page,
        "transactions": [dict(r) for r in rows],
    })


@app.route("/api/import", methods=["POST"])
def import_csv():
    upload = request.files.get("file")
    if upload is None or not upload.filename:
        return jsonify({"error": "No file uploaded."}), 400

    tmp_dir = Path(tempfile.mkdtemp())
    tmp_path = tmp_dir / (secure_filename(upload.filename) or "upload.csv")
    try:
        upload.save(str(tmp_path))
        txns = ingimport.parse_transactions(tmp_path)
        if not txns:
            return jsonify(
                {"error": "No transactions found — is this an ING CSV export?"}
            ), 400
        ingimport.backup_db(DB_PATH)
        con, inserted, skipped = ingimport.import_to_db(txns, DB_PATH)
        ingimport.apply_vendor_tags(con)
        con.close()
    except Exception as exc:  # surface parse/DB errors to the UI
        return jsonify({"error": f"Import failed: {exc}"}), 500
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    return jsonify({
        "filename": upload.filename,
        "parsed": len(txns),
        "inserted": inserted,
        "skipped": skipped,
    })


@app.route("/api/categories")
def categories():
    return jsonify(ALLOWED_CATEGORIES)


@app.route("/api/merchants")
def merchants():
    search = request.args.get("search", "").strip()
    filt = request.args.get("filter", "all")

    clauses = ["t.vendor_norm IS NOT NULL"]
    params: list = []
    if search:
        clauses.append("t.vendor_norm LIKE ?")
        params.append(f"%{search}%")
    where = " AND ".join(clauses)

    having = ""
    if filt == "untagged":
        having = "HAVING vt.category IS NULL"
    elif filt == "unconfirmed":  # tagged by Claude but not yet human-confirmed
        having = "HAVING vt.category IS NOT NULL AND COALESCE(vt.confirmed, 0) = 0"
    elif filt == "confirmed":
        having = "HAVING COALESCE(vt.confirmed, 0) = 1"

    con = db()
    rows = con.execute(
        f"""SELECT t.vendor_norm AS vendor_norm,
                   vt.category AS category,
                   MAX(t.category) AS txn_category,
                   COALESCE(vt.confirmed, 0) AS confirmed,
                   COUNT(*) AS count,
                   ROUND(SUM(CASE WHEN t.direction = 'debit' THEN t.amount ELSE 0 END), 2) AS total
            FROM transactions t
            LEFT JOIN vendor_tags vt ON vt.vendor_norm = t.vendor_norm
            WHERE {where}
            GROUP BY t.vendor_norm
            {having}
            ORDER BY total DESC, count DESC""",
        params,
    ).fetchall()
    con.close()
    return jsonify([dict(r) for r in rows])


@app.route("/api/merchants/tag", methods=["POST"])
def tag_merchant():
    data = request.get_json(silent=True) or {}
    vendor = (data.get("vendor_norm") or "").strip()
    category = (data.get("category") or "").strip()
    if not vendor:
        return jsonify({"error": "vendor_norm required"}), 400
    if category not in ALLOWED_CATEGORIES:
        return jsonify({"error": f"invalid category: {category}"}), 400

    con = db()
    con.execute(
        """INSERT INTO vendor_tags (vendor_norm, category, confirmed)
           VALUES (?, ?, 1)
           ON CONFLICT (vendor_norm)
               DO UPDATE SET category = excluded.category, confirmed = 1""",
        (vendor, category),
    )
    # Recompute so this default propagates to all of the vendor's transactions.
    ingimport.recompute_categories(con)
    con.close()
    return jsonify({
        "vendor_norm": vendor,
        "category": category,
        "confirmed": 1,
    })


if __name__ == "__main__":
    print("Dashboard → http://localhost:5000")
    app.run(debug=True, port=5000)
