"""
Yellowstone County Trustee Sale Report Ingestion Script
=========================================================

WHAT THIS DOES:
Downloads First Montana Title's monthly "Notice of Trustee Sale Report" PDF
for Yellowstone County, parses out every listing (residential AND
commercial), classifies each by borrower name (business-entity name =
commercial), and stores everything in Supabase.

SOURCE:
https://www.firstmontanatitle.com/media/Yellowstone-Foreclosure-Report-{Month}-{Year}.pdf
e.g. https://www.firstmontanatitle.com/media/Yellowstone-Foreclosure-Report-October-2024.pdf
Published monthly (confirmed working from Jan 2022 through at least Mar 2026).

WHY THIS PARSES RELIABLY:
The PDF is a LibreOffice-generated spreadsheet-as-PDF, so pdfplumber's
extract_tables() recovers a clean 5-column table directly (no jumbled
two-column text layout to fight, unlike the MDT bid tabs PDFs) --
confirmed against the October 2024 report:
  col 0: notice number, "Original Deed of Trust <#>[, re-recorded <#>]",
         "Borrower: <name(s)>", "Lender: <name>" (Lender sometimes absent)
  col 1: recording date, deed-of-trust date[, re-recorded date]
  col 2: property address (may wrap across 2 lines)
  col 3: "Principal Balance: $X" / "Original Loan Amount: $X"
         (one sample row had a stray "Principal Balance;" semicolon typo --
         parsed with a character class, not a literal colon)
  col 4: sale date/time, optionally followed by a trustee-override location
         when the sale isn't "at courthouse" (the report's default)

COMMERCIAL CLASSIFICATION:
Borrower name is checked against a list of business-entity keywords
(LLC, Inc, Corp, Trust, Partners, etc. -- see COMMERCIAL_KEYWORDS below)
using word-boundary matching. This is a heuristic, not a legal
determination -- borrower_name and the matched keyword are both stored so
it can be spot-checked and refined later.

HOW TO RUN:
  pip install pdfplumber requests --break-system-packages
  python yellowstone_trustee_sales_parser.py --month October --year 2024

SUPABASE TABLE THIS SCRIPT EXPECTS:
  See trustee_sales_schema.sql -- run it once in the Supabase SQL Editor.
"""

import argparse
import os
import re
import sys
from datetime import datetime

import requests

try:
    import pdfplumber
except ImportError:
    print("Missing dependency. Run: pip install pdfplumber --break-system-packages")
    sys.exit(1)


# ── CONFIG ───────────────────────────────────────────────────────────────
SUPABASE_URL = os.environ.get("SUPABASE_URL", "PASTE_YOUR_SUPABASE_URL_HERE")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "PASTE_YOUR_SUPABASE_SERVICE_KEY_HERE")

REPORT_URL_TEMPLATE = (
    "https://www.firstmontanatitle.com/media/"
    "Yellowstone-Foreclosure-Report-{month}-{year}.pdf"
)

MONEY = r"[\d,]+\.\d{2}"

# Word-boundary keywords that mark a borrower as a business entity rather
# than a person. Order matters only for reporting which one matched first.
COMMERCIAL_KEYWORDS = [
    "LLC", "L.L.C", "LLP", "L.L.P", "LP", "L.P",
    "INC", "INCORPORATED", "CORP", "CORPORATION",
    "TRUST", "PARTNERS", "PARTNERSHIP",
    "LTD", "LIMITED", "COMPANY",
    "ENTERPRISES", "HOLDINGS", "PROPERTIES", "GROUP",
    "ASSOCIATES", "VENTURES", "INVESTMENTS",
]
COMMERCIAL_PATTERN = re.compile(
    r"\b(" + "|".join(re.escape(k) for k in COMMERCIAL_KEYWORDS) + r")\b",
    re.IGNORECASE,
)


def classify_borrower(borrower_name: str):
    """Returns (is_commercial, matched_keyword) for a borrower name string."""
    if not borrower_name:
        return False, None
    m = COMMERCIAL_PATTERN.search(borrower_name)
    return (True, m.group(1).upper()) if m else (False, None)


def download_pdf(month: str, year: int, dest_path: str) -> None:
    url = REPORT_URL_TEMPLATE.format(month=month, year=year)
    print(f"Downloading {url} ...")
    resp = requests.get(url, timeout=60, headers={"User-Agent": "Mozilla/5.0"})
    resp.raise_for_status()
    with open(dest_path, "wb") as f:
        f.write(resp.content)
    print(f"  Saved to {dest_path} ({len(resp.content):,} bytes)")
    return url


def clean(text: str) -> str:
    """Collapse a multi-line PDF cell into normalized single-line text."""
    return " ".join(text.split()) if text else ""


def parse_col0(col0: str) -> dict:
    """
    Handles two known report layouts: the modern one (confirmed 2024-2026,
    e.g. "Original Deed of Trust 3714583" / "Borrower: NAME" / "Lender: X")
    and the older 2022-era one (e.g. "Original Deed of Trust: 3660626" /
    "Borrowers:\nNAME" / "Lender:\nX", names separated by ";" instead of
    "and", sometimes multiple "Original Deed of Trust" mentions per row).
    """
    lines = col0.strip()
    notice_number = lines.splitlines()[0].strip()

    dot_match = re.search(r"Original Deed of Trust:?\s+([\d,]+)", lines)

    # "Lender[:;]" -- at least one sample row has a "Lender;" semicolon typo
    # in place of the colon, which let the borrower capture run past it and
    # swallow the lender name too when the boundary only accepted a colon.
    borrower_match = re.search(
        r"Borrowers?:\s*(.+?)(?=\n\s*Lender[:;]|\Z)", lines, re.DOTALL
    )
    lender_match = re.search(r"Lender[:;]\s*(.+?)\Z", lines, re.DOTALL)

    return {
        "notice_number": notice_number,
        "deed_of_trust_number": dot_match.group(1) if dot_match else None,
        "borrower_name": clean(borrower_match.group(1)) if borrower_match else None,
        "lender_name": clean(lender_match.group(1)) if lender_match else None,
    }


def parse_date(date_str: str):
    date_str = date_str.strip()
    for fmt in ("%m/%d/%Y",):
        try:
            return datetime.strptime(date_str, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def parse_col1(col1: str) -> dict:
    lines = [l.strip() for l in col1.strip().splitlines() if l.strip()]
    return {
        "recording_date": parse_date(lines[0]) if len(lines) > 0 else None,
        "original_deed_of_trust_date": parse_date(lines[1]) if len(lines) > 1 else None,
    }


def parse_col3(col3: str) -> dict:
    principal_match = re.search(r"Principal Balance[;:]\s*\$?(" + MONEY + r")", col3)
    # "Original Loan Amount:" is the modern label; older reports just say
    # "Loan Amount:", and at least one sample has a typo ("Origianl").
    loan_match = re.search(r"Orig\w*\s+Loan Amount:?\s*\$?(" + MONEY + r")", col3, re.IGNORECASE)
    if not loan_match:
        loan_match = re.search(r"(?<!Original )(?<!Origianl )Loan Amount:?\s*\$?(" + MONEY + r")", col3, re.IGNORECASE)
    return {
        "principal_balance": float(principal_match.group(1).replace(",", "")) if principal_match else None,
        "original_loan_amount": float(loan_match.group(1).replace(",", "")) if loan_match else None,
    }


def parse_long_date(date_str: str):
    """Parses e.g. 'February 10,2025' or 'March 14, 2025' (comma spacing varies)."""
    date_str = re.sub(r",(?=\S)", ", ", date_str.strip())
    for fmt in ("%B %d, %Y",):
        try:
            return datetime.strptime(date_str, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def parse_col4(col4: str) -> dict:
    """
    Handles two known formats: modern "Month DD, YYYY at HH:MM AM[, location]"
    and older "MM-DD-YYYY HH:MMAM".
    """
    m = re.match(
        r"(.+?(?:AM|PM|a\.m\.|p\.m\.))\.?,?\s*(.*)", col4.strip(), re.DOTALL | re.IGNORECASE
    )
    if not m:
        return {"sale_date": None, "sale_time": None, "sale_location": None}

    datetime_str, location_str = m.group(1).strip(), clean(m.group(2))

    dm = re.match(r"(.+?\d{4})\s+at\s+(.+)", datetime_str, re.IGNORECASE)
    if dm:
        sale_date = parse_long_date(dm.group(1))
        sale_time = dm.group(2).strip()
    else:
        nm = re.match(r"(\d{1,2}-\d{1,2}-\d{4})\s+(\d{1,2}:\d{2}\s*(?:AM|PM))", datetime_str, re.IGNORECASE)
        if nm:
            try:
                sale_date = datetime.strptime(nm.group(1), "%m-%d-%Y").date().isoformat()
            except ValueError:
                sale_date = None
            sale_time = nm.group(2).strip()
        else:
            sale_date, sale_time = None, None

    return {
        "sale_date": sale_date,
        "sale_time": sale_time,
        "sale_location": location_str or None,
    }


def parse_row(row: list) -> dict:
    col0, col1, col2, col3, col4 = row
    parsed = {}
    parsed.update(parse_col0(col0 or ""))
    parsed.update(parse_col1(col1 or ""))
    parsed["property_address"] = clean(col2 or "")
    parsed.update(parse_col3(col3 or ""))
    parsed.update(parse_col4(col4 or ""))
    return parsed


def build_record(row: list, source_url: str) -> dict:
    parsed = parse_row(row)
    is_commercial, keyword = classify_borrower(parsed.get("borrower_name"))

    return {
        "id": f"yellowstone_fmt-{parsed['notice_number']}",
        "source": "yellowstone_fmt",
        "external_id": parsed["notice_number"],
        "state": "MT",
        "county": "Yellowstone",
        "notice_type": "Notice of Trustee Sale",
        "borrower_name": parsed.get("borrower_name"),
        "is_commercial": is_commercial,
        "commercial_match_keyword": keyword,
        "trustee_name": None,
        "lender_name": parsed.get("lender_name"),
        "property_address": parsed.get("property_address"),
        "recording_date": parsed.get("recording_date"),
        "original_deed_of_trust_date": parsed.get("original_deed_of_trust_date"),
        "sale_date": parsed.get("sale_date"),
        "sale_time": parsed.get("sale_time"),
        "sale_location": parsed.get("sale_location") or "Yellowstone County Courthouse",
        "principal_balance": parsed.get("principal_balance"),
        "original_loan_amount": parsed.get("original_loan_amount"),
        "raw_text": " | ".join(c.replace("\n", " ") for c in row if c),
        "source_url": source_url,
        "published_at": None,
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
    parser = argparse.ArgumentParser(description="Parse Yellowstone County trustee sale report PDF")
    parser.add_argument("--month", required=True, help="Full month name, e.g. October")
    parser.add_argument("--year", type=int, required=True, help="e.g. 2024")
    parser.add_argument("--pdf-path", default=None, help="Skip download, use local PDF path instead")
    parser.add_argument("--dry-run", action="store_true", help="Parse only, don't write to Supabase")
    args = parser.parse_args()

    month = args.month.capitalize()
    pdf_path = args.pdf_path or f"yellowstone_{month.lower()}_{args.year}.pdf"
    source_url = REPORT_URL_TEMPLATE.format(month=month, year=args.year)

    if not args.pdf_path:
        source_url = download_pdf(month, args.year, pdf_path)

    print("Extracting table from PDF...")
    records = []
    skipped = 0
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            for table in page.extract_tables():
                for row in table[1:]:  # skip that table's own header row
                    if not any(row):
                        continue
                    # A real notice number is the whole first line and pure
                    # digits (e.g. "4133431"). Checking only "starts with a
                    # digit" also matches the report's date-range subtitle
                    # row ("2026-05-01 through 2026-05-31, ...") since that
                    # starts with a digit too -- that one previously slipped
                    # through as a garbage all-blank "listing".
                    first_line = row[0].strip().splitlines()[0].strip() if row[0] else ""
                    if not re.match(r"^\d{5,}$", first_line):
                        # Harmless most of the time: the report's title/
                        # subtitle lines and a repeated column-header row on
                        # later pages all land here (table[1:] only strips
                        # one leading header row per page-table, but this
                        # report sometimes has more). Genuinely distinct
                        # from a real listing missing its notice number,
                        # which still has data in the OTHER columns -- that
                        # case is data loss worth flagging.
                        if any(c and c.strip() for c in row[1:] if c):
                            skipped += 1
                        continue
                    records.append(build_record(row, source_url))
    if skipped:
        print(f"  ! Skipped {skipped} row(s) with data but no notice number -- check the PDF manually")

    commercial = [r for r in records if r["is_commercial"]]
    print(
        f"\nParsed {len(records)} listings -- "
        f"{len(commercial)} flagged commercial, {len(records) - len(commercial)} residential"
    )

    if args.dry_run:
        print("\n--dry-run set, not writing to Supabase.")
        for r in records:
            flag = "COMMERCIAL" if r["is_commercial"] else "residential"
            print(f"  [{flag:10s}] {r['borrower_name']!r:45s} {r['property_address']}")
        return

    if "PASTE_YOUR" in SUPABASE_URL or "PASTE_YOUR" in SUPABASE_KEY:
        print("\nMissing Supabase credentials -- set SUPABASE_URL and SUPABASE_SERVICE_KEY.")
        sys.exit(1)

    upsert_table("trustee_sales", records)


if __name__ == "__main__":
    main()
