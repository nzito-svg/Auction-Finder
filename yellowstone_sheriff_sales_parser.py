"""
Yellowstone County Sheriff's Sales Ingestion Script
========================================================

WHY THIS EXISTS:
Discovered because a specific property (1207 and 1219 S 32nd St W, Billings)
the user was personally bidding on never showed up in any of the other three
sources. It's a Sheriff's Sale -- a judicial sale (lawsuit -> judgment ->
writ of execution), not a deed-of-trust default -- so it was NEVER going to
appear in First Montana Title's report or in a newspaper "Notice of
Trustee's Sale". The County Sheriff's Office runs and posts these
independently of both:
  https://www.yellowstonecountymt.gov/sheriff/sheriffsales.asp

WHAT'S ON THAT PAGE:
A single HTML table, one row per sale, mixing:
  - Real property (what this project cares about)
  - Personal property: mobile homes, vehicles, equipment (skipped)
  - Upcoming, completed, and cancelled sales, all left visible together
    (status shown via a <span class="text-success">Sale Completed</span> or
    <span class="text-danger">Cancelled</span> immediately before the date --
    absence of either span means the sale is still upcoming)
Each row also links to a per-sale PDF ("Notice of Sheriff's Sale") -- but
those are SCANNED IMAGES with no extractable text (confirmed: 0 chars, 1
image per page via pdfplumber), and this machine doesn't have `tesseract`
installed for OCR. That means the defendant/debtor's name -- and therefore
the name-based commercial classifier -- isn't available for this source.
The property-type (county parcel data) and web-search checks don't need a
name, only the address, so 2 of the 3 commercial signals still work fine;
only is_commercial_by_name stays null here. Revisit with OCR if that gap
ever matters enough to justify the new dependency.

DEDUPING / STATUS CHANGES:
Control number (e.g. "26001956") is a stable id straight from the page --
used as external_id. Upserting on it naturally picks up a sale flipping
from "upcoming" to "completed" or "cancelled" on the next run, rather than
needing separate logic to detect that.

HOW TO RUN:
  pip install requests beautifulsoup4 --break-system-packages
  python yellowstone_sheriff_sales_parser.py
  python yellowstone_sheriff_sales_parser.py --dry-run

SUPABASE COLUMNS THIS SCRIPT EXPECTS:
  See add_sheriff_sale_columns.sql -- run it once in the Supabase SQL Editor
  (in addition to the base trustee_sales_schema.sql).
"""

import argparse
import os
import re
import sys
from datetime import datetime

import requests
from bs4 import BeautifulSoup

SUPABASE_URL = os.environ.get("SUPABASE_URL", "PASTE_YOUR_SUPABASE_URL_HERE")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "PASTE_YOUR_SUPABASE_SERVICE_KEY_HERE")

SHERIFF_SALES_URL = "https://www.yellowstonecountymt.gov/sheriff/sheriffsales.asp"
MONEY = r"[\d,]+\.\d{2}"

COMMERCIAL_KEYWORDS = [
    "LLC", "L.L.C", "LLP", "L.L.P", "LP", "L.P",
    "INC", "INCORPORATED", "CORP", "CORPORATION",
    "TRUST", "PARTNERS", "PARTNERSHIP",
    "LTD", "LIMITED", "COMPANY",
    "ENTERPRISES", "HOLDINGS", "PROPERTIES", "GROUP",
    "ASSOCIATES", "VENTURES", "INVESTMENTS",
]
COMMERCIAL_PATTERN = re.compile(
    r"\b(" + "|".join(re.escape(k) for k in COMMERCIAL_KEYWORDS) + r")\b", re.IGNORECASE
)


def classify_borrower(name):
    if not name:
        return False, None
    m = COMMERCIAL_PATTERN.search(name)
    return (True, m.group(1).upper()) if m else (False, None)


def fetch_page() -> str:
    resp = requests.get(SHERIFF_SALES_URL, headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
    resp.raise_for_status()
    return resp.text


def parse_datetime_cell(cell) -> dict:
    text = cell.get_text(" ", strip=True)
    status = "upcoming"
    if "Sale Completed" in text:
        status = "completed"
    elif "Cancelled" in text:
        status = "cancelled"
    # strip the status label out, leaving "7/28/2026 11:00 a.m."
    clean = re.sub(r"Sale Completed|Cancelled", "", text).strip()
    m = re.match(r"(\d{1,2}/\d{1,2}/\d{4})\s+(.+)", clean)
    sale_date, sale_time = None, None
    if m:
        try:
            sale_date = datetime.strptime(m.group(1), "%m/%d/%Y").date().isoformat()
        except ValueError:
            pass
        sale_time = m.group(2).strip().upper().replace(".", "")
    return {"status": status, "sale_date": sale_date, "sale_time": sale_time}


def parse_row(row) -> dict:
    cells = row.find_all("td")
    if len(cells) < 4:
        return None

    dt = parse_datetime_cell(cells[0])
    ctrl_link = cells[1].find("a")
    control_number = ctrl_link.get_text(strip=True) if ctrl_link else cells[1].get_text(strip=True)
    pdf_url = ctrl_link["href"] if ctrl_link and ctrl_link.has_attr("href") else None
    address = cells[2].get_text(" ", strip=True)
    notes = cells[3].get_text("\n", strip=True)

    case_match = re.search(r"Case #:\s*([A-Z]{2}\s*\d{2}\s*\d+)", notes)
    property_kind = "personal_property" if "PERSONAL PROPERTY" in notes.upper() else "real_property"
    bid_match = re.search(r"OPENING BID[^$]*\$(" + MONEY + r")", notes, re.IGNORECASE)

    return {
        "control_number": control_number,
        "pdf_url": pdf_url,
        "address": address,
        "notes": notes,
        "case_number": case_match.group(1).strip() if case_match else None,
        "property_kind": property_kind,
        "opening_bid": float(bid_match.group(1).replace(",", "")) if bid_match else None,
        **dt,
    }


def build_record(row: dict) -> dict:
    is_commercial, keyword = classify_borrower(None)  # no defendant name available (see docstring)

    return {
        "id": f"yellowstone_sheriff-{row['control_number']}",
        "source": "yellowstone_sheriff",
        "external_id": row["control_number"],
        "state": "MT",
        "county": "Yellowstone",
        "notice_type": "Sheriff's Sale",
        "borrower_name": None,
        "is_commercial": is_commercial,
        "commercial_match_keyword": keyword,
        "is_commercial_by_name": is_commercial,
        "trustee_name": None,
        "lender_name": None,
        "property_address": row["address"],
        "recording_date": None,
        "original_deed_of_trust_date": None,
        "sale_date": row["sale_date"],
        "sale_time": row["sale_time"],
        "sale_location": "Yellowstone County Courthouse Lobby, 217 N 27th St, Billings MT 59101",
        "principal_balance": None,
        "original_loan_amount": None,
        "raw_text": row["notes"],
        "source_url": row["pdf_url"],
        "published_at": None,
        "case_number": row["case_number"],
        "sale_status": row["status"],
        "property_kind": row["property_kind"],
        "opening_bid": row["opening_bid"],
    }


def upsert_table(table: str, records: list) -> None:
    if not records:
        return
    url = f"{SUPABASE_URL}/rest/v1/{table}"
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates",
    }
    for i in range(0, len(records), 500):
        batch = records[i : i + 500]
        resp = requests.post(url, headers=headers, json=batch, timeout=60)
        if resp.status_code not in (200, 201):
            print(f"  ! Upsert to {table} failed: {resp.status_code} {resp.text[:300]}")
        else:
            print(f"  ✓ Upserted {len(batch)} rows into {table}")


def main():
    parser = argparse.ArgumentParser(description="Parse Yellowstone County Sheriff's Sales page")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--include-personal-property", action="store_true",
                         help="Also ingest mobile home/vehicle/equipment sales (skipped by default)")
    args = parser.parse_args()

    print(f"Fetching {SHERIFF_SALES_URL} ...")
    html = fetch_page()
    soup = BeautifulSoup(html, "html.parser")

    table = soup.find("table", class_="data-table")
    if not table:
        print("  ! Could not find the sales table -- page structure may have changed.")
        sys.exit(1)

    rows = table.find_all("tr")[1:]  # skip header row
    parsed = [parse_row(r) for r in rows]
    parsed = [r for r in parsed if r]

    if not args.include_personal_property:
        skipped = sum(1 for r in parsed if r["property_kind"] == "personal_property")
        parsed = [r for r in parsed if r["property_kind"] == "real_property"]
        if skipped:
            print(f"  (skipped {skipped} personal-property sale(s) -- mobile homes/vehicles/equipment)")

    records = [build_record(r) for r in parsed]

    by_status = {}
    for r in records:
        by_status[r["sale_status"]] = by_status.get(r["sale_status"], 0) + 1

    print(f"\nParsed {len(records)} real-property sheriff's sale(s):")
    for k, v in sorted(by_status.items()):
        print(f"  {k:12s} {v}")

    if args.dry_run:
        print("\n--dry-run set, not writing to Supabase.")
        for r in records:
            print(f"  [{r['sale_status']:10s}] {r['sale_date'] or '??':10s} {r['property_address']} (case {r['case_number']})")
        return

    if "PASTE_YOUR" in SUPABASE_URL or "PASTE_YOUR" in SUPABASE_KEY:
        print("\nMissing Supabase credentials -- set SUPABASE_URL and SUPABASE_SERVICE_KEY.")
        sys.exit(1)

    upsert_table("trustee_sales", records)


if __name__ == "__main__":
    main()
