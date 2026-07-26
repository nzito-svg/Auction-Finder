"""
Dashboard Regeneration Script
==================================

WHY THIS EXISTS:
The dashboard (dashboard.html) is a single static file with the current
dataset baked in as a JS array (`const RECORDS = [...]`) rather than
fetching live from Supabase -- deliberately, so the Supabase service-role
key never has to be embedded in a client-facing page. That means every
time the underlying data changes, the file needs to be regenerated.

Doing that by hand-editing a 250KB+ HTML file (which also has ~180KB of
embedded font data) is exactly the kind of fragile, error-prone task that
shouldn't be left to ad-hoc string surgery -- especially by an agent
running unattended on a schedule with no one watching. This script does
the one part that actually changes (splice in a freshly-pulled RECORDS
array) and leaves everything else in the file -- CSS, fonts, all the
interactive JS logic -- completely untouched.

HOW TO RUN:
  python3 regenerate_dashboard.py
  python3 regenerate_dashboard.py --dry-run   # pull + transform only, don't touch the file
"""

import argparse
import json
import os
import re
import sys

import requests

SUPABASE_URL = os.environ.get("SUPABASE_URL", "PASTE_YOUR_SUPABASE_URL_HERE")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "PASTE_YOUR_SUPABASE_SERVICE_KEY_HERE")

DASHBOARD_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard.html")

SELECT_FIELDS = (
    "id,county,notice_type,borrower_name,is_commercial,commercial_match_keyword,"
    "is_commercial_by_name,is_commercial_by_proptype,is_commercial_by_web_search,"
    "web_search_confidence,web_search_notes,county_prop_type,trustee_name,lender_name,"
    "property_address,sale_date,sale_time,sale_location,principal_balance,"
    "original_loan_amount,source_url,sale_status,case_number,opening_bid"
)


def fetch_rows() -> list:
    resp = requests.get(
        f"{SUPABASE_URL}/rest/v1/trustee_sales",
        headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"},
        params={"select": SELECT_FIELDS, "order": "sale_date.asc.nullslast"},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def transform(rows: list) -> list:
    """Mirrors the compact record shape dashboard.html's JS expects -- see
    the `reasons` construction here against `recordEvidenceHtml`/`bestConfidence`
    in the dashboard's own script if this ever needs to change."""
    out = []
    for r in rows:
        reasons = []
        if r.get("is_commercial_by_name"):
            reasons.append({
                "src": "name",
                "label": f"Borrower name contains “{r.get('commercial_match_keyword')}”",
                "conf": None,
            })
        if r.get("is_commercial_by_proptype"):
            reasons.append({
                "src": "proptype",
                "label": f"County property type: {r.get('county_prop_type')}",
                "conf": None,
            })
        if r.get("is_commercial_by_web_search"):
            reasons.append({
                "src": "web",
                "label": r.get("web_search_notes"),
                "conf": r.get("web_search_confidence"),
            })

        out.append({
            "id": r["id"],
            "co": r["county"],
            "addr": r.get("property_address"),
            "b": r.get("borrower_name"),
            "sd": r.get("sale_date"),
            "st": r.get("sale_time"),
            "sl": r.get("sale_location"),
            "comm": bool(r.get("is_commercial")),
            "reasons": reasons,
            "trustee": r.get("trustee_name"),
            "lender": r.get("lender_name"),
            "pt": r.get("county_prop_type"),
            "pb": r.get("principal_balance"),
            "loan": r.get("original_loan_amount"),
            "nt": r.get("notice_type"),
            "url": r.get("source_url"),
            "ss": r.get("sale_status"),
            "cn": r.get("case_number"),
            "ob": r.get("opening_bid"),
        })
    return out


def splice_into_dashboard(records: list) -> str:
    if not os.path.exists(DASHBOARD_PATH):
        print(f"  ! dashboard.html not found at {DASHBOARD_PATH}")
        sys.exit(1)

    html = open(DASHBOARD_PATH, encoding="utf-8").read()
    records_json = json.dumps(records, separators=(",", ":"))

    new_html, n = re.subn(
        r"const RECORDS = \[.*?\];",
        lambda m: f"const RECORDS = {records_json};",
        html,
        count=1,
        flags=re.DOTALL,
    )
    if n != 1:
        print("  ! Could not find the `const RECORDS = [...]` marker in dashboard.html -- "
              "the file's structure may have changed. Not touching the file.")
        sys.exit(1)
    return new_html


def main():
    parser = argparse.ArgumentParser(description="Regenerate dashboard.html with fresh Supabase data")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if "PASTE_YOUR" in SUPABASE_URL or "PASTE_YOUR" in SUPABASE_KEY:
        print("Missing Supabase credentials -- set SUPABASE_URL and SUPABASE_SERVICE_KEY.")
        sys.exit(1)

    print("Fetching current data from Supabase...")
    rows = fetch_rows()
    print(f"  {len(rows)} rows fetched")

    records = transform(rows)
    commercial = sum(1 for r in records if r["comm"])
    print(f"  {commercial} flagged commercial")

    if args.dry_run:
        print("\n--dry-run set, not writing dashboard.html.")
        return

    new_html = splice_into_dashboard(records)

    # Validate before writing: the new content must still be valid JSON where
    # we just spliced it, and braces must stay balanced, or something about
    # the surrounding file broke -- better to fail loudly than ship a broken page.
    m = re.search(r"const RECORDS = (\[.*?\]);", new_html, re.DOTALL)
    json.loads(m.group(1))
    if new_html.count("{") != new_html.count("}"):
        print("  ! Brace count mismatch after splice -- aborting write, something is wrong.")
        sys.exit(1)

    with open(DASHBOARD_PATH, "w", encoding="utf-8") as f:
        f.write(new_html)
    print(f"  ✓ dashboard.html regenerated ({len(new_html):,} bytes)")


if __name__ == "__main__":
    main()
