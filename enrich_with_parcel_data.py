"""
Parcel Cross-Reference Enrichment Script
===========================================

WHY THIS EXISTS:
The original commercial classifier only looks at the borrower's name (LLC,
Inc, Trust, Partners, etc.), which misses a commercial property held in an
individual's own name -- e.g. a husband and wife who personally own a
commercial building, not through an entity. This script adds a second,
independent signal: Montana's own parcel classification, looked up by
property address or owner name, regardless of how the borrower's name reads.

DATA SOURCE:
Montana State Library's statewide cadastral parcel layer (MSDI Framework),
sourced from the Dept. of Revenue's CAMA database (plus locally-maintained
data for Flathead/Missoula/Ravalli/Silver Bow/Yellowstone counties):
  https://gisservicemt.gov/arcgis/rest/services/MSDI_Framework/Parcels/MapServer/0
Public, no auth required, queryable via standard ArcGIS REST "query" params.

WHAT IT CAN AND CAN'T CATCH:
The parcel layer's PropType field is a physical/use classification
(Vacant Land, Improved Property, Apartment, Industrial Property, Golf
Course, Mobile/RV Parks, Condominium, Townhouse, etc.) -- NOT a clean
"Commercial vs Residential" flag. It reliably catches special-use
categories (Apartment, Industrial Property, Golf Course, Mobile/RV Parks,
Gravel Pit, Mining Claim, centrally-assessed utility property) regardless
of who owns them. But the majority category, "Improved Property", is a
catch-all that covers both an ordinary house AND a standalone small
commercial building (office, retail, shop) -- there's no field in this
layer that separates those. So this does NOT fully solve "individual owns
a commercial building" -- it narrows the miss to that one ambiguous
bucket. Getting finer than that would need the county's actual building/
improvement records (Property Record Card), which live behind a much
harder-to-automate ASP.NET system -- not yet built.

MATCHING STRATEGY:
1. Try an address match first (house number + first word of street name,
   within the row's county) -- most precise when it resolves to exactly
   one parcel.
2. Falls back to an owner-name match (surname of the first borrower listed,
   within the row's county) when the address is missing/unparseable
   ("n/a" shows up in real source data) or matches zero parcels.
3. If either match returns MULTIPLE parcels (e.g. an LLC that owns a whole
   townhome complex), it's recorded as 'owner_ambiguous'/'address_ambiguous'
   rather than guessing -- the PropType/legal description of the first hit
   is stored for reference but shouldn't be trusted as authoritative.
4. Zero matches -> 'no_match', all new fields left null. Common for
   Yellowstone rows where the source PDF itself just says "n/a" for the
   address, or for newspaper notices whose legal description doesn't
   resolve to a DOR-recognized address string.

HOW TO RUN:
  python enrich_with_parcel_data.py             # enrich rows not yet matched
  python enrich_with_parcel_data.py --force      # re-match everything
  python enrich_with_parcel_data.py --dry-run
"""

import argparse
import os
import re
import sys
import time

import requests

SUPABASE_URL = os.environ.get("SUPABASE_URL", "PASTE_YOUR_SUPABASE_URL_HERE")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "PASTE_YOUR_SUPABASE_SERVICE_KEY_HERE")

PARCELS_URL = "https://gisservicemt.gov/arcgis/rest/services/MSDI_Framework/Parcels/MapServer/0/query"

# PropType values that unambiguously signal non-single-family-residential
# use, regardless of who owns the parcel. Deliberately excludes "Improved
# Property", "Condominium", "Townhouse", "Vacant Land" -- those are
# genuinely ambiguous (could be a house, could be commercial) with this
# field alone.
COMMERCIAL_PROP_TYPES = {
    "Industrial Property", "Apartment", "Golf Course", "Mobile/RV Parks",
    "Gravel Pit", "Mining Claim", "Locally Assessed Utility",
    "CA - Centrally Assessed", "CN - Centrally Assessed Non-Valued Property",
}

NAME_SUFFIXES = {"jr", "sr", "ii", "iii", "iv"}


def sql_escape(s: str) -> str:
    return s.replace("'", "''")


def parse_address_for_match(address):
    if not address:
        return None
    address = address.strip()
    if address.lower() in ("n/a", "na", ""):
        return None
    m = re.match(r"(\d+[A-Za-z]?)\s+([A-Za-z0-9]+)", address)
    if not m:
        return None
    return m.group(1), m.group(2)


def guess_surname(borrower_name):
    """Best-effort surname of the first person listed in a borrower_name string."""
    if not borrower_name:
        return None
    first_person = re.split(r"\s+and\s+|,|;", borrower_name, flags=re.IGNORECASE)[0].strip()
    words = [w.strip(".") for w in first_person.split() if w.strip(".")]
    words = [w for w in words if w.lower() not in NAME_SUFFIXES]
    if not words:
        return None
    return words[-1]


def query_parcels(where_clause, top=25):
    resp = requests.get(
        PARCELS_URL,
        params={
            "where": where_clause,
            "outFields": "AddressLine1,CityStateZip,OwnerName,PropType,LegalDescriptionShort",
            "returnGeometry": "false",
            "resultRecordCount": top,
            "f": "json",
        },
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    return data.get("features", [])


def match_parcel(county, property_address, borrower_name):
    """Returns (attributes_dict_or_None, confidence_string)."""
    addr = parse_address_for_match(property_address)
    if addr:
        house_num, street_word = addr
        where = (
            f"CountyName='{sql_escape(county)}' AND "
            f"UPPER(AddressLine1) LIKE '{sql_escape(house_num.upper())} {sql_escape(street_word.upper())}%'"
        )
        hits = query_parcels(where)
        if len(hits) == 1:
            return hits[0]["attributes"], "address"
        if len(hits) > 1:
            return hits[0]["attributes"], "address_ambiguous"

    surname = guess_surname(borrower_name)
    if surname:
        where = (
            f"CountyName='{sql_escape(county)}' AND "
            f"UPPER(OwnerName) LIKE '%{sql_escape(surname.upper())}%'"
        )
        hits = query_parcels(where)
        if len(hits) == 1:
            return hits[0]["attributes"], "owner_unique"
        if len(hits) > 1:
            return hits[0]["attributes"], "owner_ambiguous"

    return None, "no_match"


def fetch_rows_to_enrich(force: bool) -> list:
    url = f"{SUPABASE_URL}/rest/v1/trustee_sales"
    params = {"select": "id,county,property_address,borrower_name,is_commercial_by_name,is_commercial_by_web_search"}
    if not force:
        params["county_matched_at"] = "is.null"
    headers = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}
    resp = requests.get(url, headers=headers, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


def update_row(row_id: str, patch: dict, retries: int = 3) -> bool:
    url = f"{SUPABASE_URL}/rest/v1/trustee_sales"
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=minimal",
    }
    for attempt in range(retries):
        try:
            resp = requests.patch(url, headers=headers, params={"id": f"eq.{row_id}"}, json=patch, timeout=30)
            return resp.status_code in (200, 204)
        except requests.exceptions.RequestException as e:
            if attempt == retries - 1:
                print(f"  ! Network error updating {row_id} after {retries} attempts: {e}")
                return False
            time.sleep(2 ** attempt)  # 1s, 2s backoff before retrying
    return False


def main():
    parser = argparse.ArgumentParser(description="Cross-reference trustee_sales rows against MT parcel data")
    parser.add_argument("--force", action="store_true", help="Re-match rows that already have a county match")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if "PASTE_YOUR" in SUPABASE_URL or "PASTE_YOUR" in SUPABASE_KEY:
        print("Missing Supabase credentials -- set SUPABASE_URL and SUPABASE_SERVICE_KEY.")
        sys.exit(1)

    rows = fetch_rows_to_enrich(args.force)
    print(f"Enriching {len(rows)} row(s)...")

    confidence_counts = {}
    newly_commercial = []

    skipped = []
    for row in rows:
        try:
            attrs, confidence = match_parcel(row["county"], row.get("property_address"), row.get("borrower_name"))
        except requests.exceptions.RequestException as e:
            print(f"  ! Network error matching {row['id']}, will need a re-run to pick it up: {e}")
            skipped.append(row["id"])
            continue
        confidence_counts[confidence] = confidence_counts.get(confidence, 0) + 1

        prop_type = attrs.get("PropType") if attrs else None
        is_commercial_by_proptype = prop_type in COMMERCIAL_PROP_TYPES

        patch = {
            "county_prop_type": prop_type,
            "county_owner_name": attrs.get("OwnerName") if attrs else None,
            "county_legal_description": attrs.get("LegalDescriptionShort") if attrs else None,
            "county_match_confidence": confidence,
            "is_commercial_by_proptype": is_commercial_by_proptype,
            # OR all three independent signals -- must include is_commercial_by_web_search
            # (added after this script was first written) or this overwrite would silently
            # wipe out every web-search-only commercial flag back to false.
            "is_commercial": (
                bool(row.get("is_commercial_by_name"))
                or bool(row.get("is_commercial_by_web_search"))
                or is_commercial_by_proptype
            ),
        }

        if is_commercial_by_proptype and not row.get("is_commercial_by_name"):
            newly_commercial.append((row["id"], row.get("borrower_name"), prop_type, confidence))

        if not args.dry_run:
            patch["county_matched_at"] = _now_iso()
            ok = update_row(row["id"], patch)
            if not ok:
                print(f"  ! Failed to update {row['id']}")
        time.sleep(0.15)  # be polite to the public ArcGIS endpoint

    print("\nMatch confidence breakdown:")
    for k, v in sorted(confidence_counts.items()):
        print(f"  {k:20s} {v}")

    print(f"\n{len(newly_commercial)} row(s) newly flagged commercial by property type (missed by the name heuristic):")
    for row_id, borrower, prop_type, confidence in newly_commercial:
        print(f"  [{confidence:16s}] {borrower!r:45s} -> {prop_type}")

    if skipped:
        print(f"\n{len(skipped)} row(s) skipped due to network errors -- just re-run the script to pick them up:")
        for row_id in skipped:
            print(f"  {row_id}")

    if args.dry_run:
        print("\n--dry-run set, no rows were updated.")


def _now_iso():
    import datetime
    return datetime.datetime.utcnow().isoformat() + "Z"


if __name__ == "__main__":
    main()
