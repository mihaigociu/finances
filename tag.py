#!/usr/bin/env python3
"""Assign categories to vendor_norm values using Claude API, then apply to transactions."""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import anthropic

DB_PATH = Path("transactions.db")

# Reuse recompute_categories from import.py (can't `import import`, it's a keyword).
_import_spec = importlib.util.spec_from_file_location(
    "ingimport", str(Path(__file__).resolve().parent / "import.py")
)
ingimport = importlib.util.module_from_spec(_import_spec)
_import_spec.loader.exec_module(ingimport)

CATEGORIES = [
    "groceries",
    "bakery",
    "restaurants",
    "gas",
    "health",
    "transport",
    "telecom",
    "utilities",
    "subscriptions",
    "shopping",
    "education",
    "insurance",
    "government",
    "other",
]

SYSTEM_PROMPT = """You are a personal finance assistant. Assign spending categories to merchant/vendor names from Romanian bank transactions.

Available categories:
- groceries: Supermarkets, food markets, fruit/vegetable vendors, butchers, delis, farm produce (MEGAIMAGE, LIDL, Auchan, AGROPAN, VECSEI FRUCT, Kaufland, Carrefour, PROFI, SAFEWAY)
- bakery: Bakeries, bread shops, pastry shops (PANEMAR, Brutaria, PATRY, MAX BAKERY, FEZH, PRALINA)
- restaurants: Restaurants, cafes, fast food, ice cream, gelato, food delivery, bistros (HOMEMADE PIZZA, CAROLINA CAFE, DNZ GELATO, GRADINA, DUCFARM, LEMNUL VERDE, MAMUCA)
- gas: Fuel / petrol stations (LUKOIL, OMV, MOL, ROMPETROL, FLOREA OIL — note "OIL" in name is a strong signal)
- health: Pharmacies, medical clinics, hospitals, labs, optical/vision care (REMEDIUM FARM, CATENA, HELP NET, ANAFARM, SYNEVO, MEDLIFE, PROVISUAL, OMNIMEDICAL, CENTRUL MEDICAL, SPIT CLINIC)
- transport: Ride-sharing, parking meters/lots, car rentals, auto services, vignette/road tolls, public transport (PayU*bolt.eu, DRM CLUJ, CTP CLUJ, DEZAUTO, AUTO, ROVIGNETA)
- telecom: Mobile operators, internet providers (Orange, Vodafone, WWW.ORANGE.RO, PayU*vodafone.ro)
- utilities: Electricity, gas/energy, water, building administration, property management (EON, E-BLOC.RO, CASTLE MANAGEMENT)
- subscriptions: Streaming, cloud storage, digital subscriptions, online platforms, news/author subscriptions (Google One, YouTube Premium, Disney+, PAUL KINGSNORTH, ANTHROPIC, NYX*UNIVERSTSA if online platform)
- shopping: General retail, clothing, drugstores, household goods, home improvement, electronics, books, toys, sports goods, gifts, online marketplaces (DM DROGERIE MARKT, PEPCO, H&M, ZARA, DECATHLON, LEROY MERLIN, DEDEMAN, IKEA, CARTURESTI, eMAG, NORIEL, PRIMIGI)
- education: Schools, universities, courses, educational content (UNIV. BABES-BOLYAI, NYX*UNIVERSTSA if university fee, BIBL OCTAVIAN GOGA)
- insurance: Insurance brokers, premiums
- government: Local taxes, fines, permits, government services, public institutions (MUNICIPIUL CLUJ-NAPOCA, CJ CLUJ NAPOCA, GHISEUL.RO, DOMENIUL PUBLIC NAPOCA)
- other: Leisure, museums, escape rooms, events, gyms, florists, unclear vendors, courier services

Context clues:
- PayU* prefix = Romanian online payment processor; the vendor name follows the *
- NTP* prefix = another Romanian payment processor
- EP* prefix = Romanian online payment platform
- PlatiOn* / PayPoint* = payment processors
- PAYPAL * = international PayPal payment
- Vendors with "FARM" in the name might be pharmacies (REMEDIUM FARM, ANAFARM) or actual farms (DUCFARM = duck farm → groceries)
- DUCFARM (and variants DUCFARM ALBINI, DUCFARM DEP) = medical clinic → health
- "SRL", "S.R.L.", "PFA", "I.I.", "S.A." are Romanian legal entity suffixes, not meaningful for categorization
- Romanian city names after the vendor name (CLUJ NAPOCA, CLUJ-NAPOCA) are location info, not vendor names
- CARTURESTI = bookstore chain → shopping
- LIBRIS, LITERA, DIDACTICA, DISTRIBOOK, EDITURA, Librex = books/publishers → shopping
- BARDAC ANAD = textile/fabric vendor → shopping
- IRIKA KNITTING = yarn/knitting supplies → shopping
- CADORABIL = gift shop → shopping
- FLORARIA, POSY FLORALMARKS = flower shops → shopping
- EXITROOM = escape room → other
- ADRENALIN PARK, JIMSYM (gym?) = leisure → other
- MUZEUL = museum → other
- NOI SI FAMILIA = children's activity center → other
- COLETE ONLINE = courier/delivery → other
- INTERNATIONAL PAPER BUSI = office/paper supplies → shopping
- SUPERGRAPH = printing → shopping
- ELECTROSIGMA = electronics → shopping
- VALTHERM CONFORT = home heating/comfort → shopping
- BRICK DEPOT = construction materials → shopping
- DIREKTIA CULTURA SI SPOR, BIBL OCTAVIAN GOGA, MUZEUL = cultural institutions → other
- SILVER FLO SENSE = cosmetics/beauty → shopping
- MAYA & ALEXA variants = children's products → shopping
- ONE DISTRIBUTION, TINERVIS, VENDOSTORE, unclear vendors → other"""


SCHEMA = {
    "type": "object",
    "properties": {
        "tags": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "vendor_norm": {"type": "string"},
                    "category": {"type": "string", "enum": CATEGORIES},
                },
                "required": ["vendor_norm", "category"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["tags"],
    "additionalProperties": False,
}


def get_untagged_vendors(db_path: Path) -> list[str]:
    con = sqlite3.connect(db_path)
    rows = con.execute(
        """SELECT DISTINCT vendor_norm
           FROM transactions
           WHERE vendor_norm IS NOT NULL AND category IS NULL
           ORDER BY vendor_norm"""
    ).fetchall()
    con.close()
    return [r[0] for r in rows]


def tag_vendors(vendors: list[str]) -> dict[str, str]:
    client = anthropic.Anthropic()

    vendor_list = "\n".join(f"- {v}" for v in vendors)
    user_message = (
        f"Categorize each of these {len(vendors)} merchant names from Romanian bank transactions. "
        f"Return every vendor exactly as written.\n\n{vendor_list}"
    )

    response = client.messages.create(
        model="claude-opus-4-7",
        max_tokens=8192,
        system=[{
            "type": "text",
            "text": SYSTEM_PROMPT,
            "cache_control": {"type": "ephemeral"},
        }],
        output_config={"format": {"type": "json_schema", "schema": SCHEMA}},
        messages=[{"role": "user", "content": user_message}],
    )

    text = next(b.text for b in response.content if b.type == "text")
    data = json.loads(text)
    tags = {item["vendor_norm"]: item["category"] for item in data["tags"]}

    # Warn about vendors that came back but weren't in the input
    missing = set(vendors) - set(tags)
    extra = set(tags) - set(vendors)
    if missing:
        print(f"  Warning: {len(missing)} vendors not returned by Claude: {missing}", file=sys.stderr)
    if extra:
        print(f"  Warning: {len(extra)} unexpected vendors in response (ignored)", file=sys.stderr)
        for k in extra:
            del tags[k]

    usage = response.usage
    print(
        f"  Tokens — input: {usage.input_tokens}, "
        f"output: {usage.output_tokens}, "
        f"cache_write: {getattr(usage, 'cache_creation_input_tokens', 0)}, "
        f"cache_read: {getattr(usage, 'cache_read_input_tokens', 0)}"
    )
    return tags


def save_and_apply(tags: dict[str, str], db_path: Path) -> int:
    con = sqlite3.connect(db_path)
    con.executemany(
        """INSERT INTO vendor_tags (vendor_norm, category, confirmed)
           VALUES (?, ?, 0)
           ON CONFLICT (vendor_norm) DO UPDATE SET category = excluded.category""",
        [(v, c) for v, c in tags.items()],
    )
    # Recompute all categories so newly-inserted tags apply while existing
    # vendor_tags defaults are preserved (not clobbered).
    ingimport.recompute_categories(con)
    (applied,) = con.execute(
        "SELECT COUNT(*) FROM transactions WHERE category IS NOT NULL AND type LIKE '%POS%'"
    ).fetchone()
    con.close()
    return applied


def print_results(tags: dict[str, str]) -> None:
    by_cat: dict[str, list[str]] = {}
    for vendor, cat in tags.items():
        by_cat.setdefault(cat, []).append(vendor)

    print("\nTagging results:")
    for cat in CATEGORIES:
        vendors_in_cat = sorted(by_cat.get(cat, []))
        if vendors_in_cat:
            print(f"  {cat} ({len(vendors_in_cat)}):")
            for v in vendors_in_cat:
                print(f"    {v}")


if __name__ == "__main__":
    vendors = get_untagged_vendors(DB_PATH)
    if not vendors:
        print("No untagged vendors found.")
        sys.exit(0)

    print(f"Tagging {len(vendors)} unique vendor names via Claude API...")
    tags = tag_vendors(vendors)
    applied = save_and_apply(tags, DB_PATH)

    print(f"\n{len(tags)} vendors tagged, {applied} POS transactions now categorized")
    print_results(tags)
