"""
Archive Expired Listings Script
====================================

WHAT THIS DOES:
Marks any row whose sale_date has already passed as is_expired = true.
This is an archive, not a delete -- the row and everything on it (including
raw_text, the commercial-detection evidence, etc.) stays in the table
untouched. It's just excluded from the working dashboard view going
forward (regenerate_dashboard.py filters is_expired=false when it pulls
data), and could always be un-archived or queried later since nothing was
destroyed.

WHY ARCHIVE INSTEAD OF DELETE:
This runs unattended on a weekly schedule with no one watching each run.
If the "is this expired" logic ever has a bug, an archived-by-mistake row
is a one-line fix (patch is_expired back to false); a deleted-by-mistake
row is gone. For a dataset this small, there's no real storage cost to
keeping expired rows around.

WHAT COUNTS AS EXPIRED:
Any row where sale_date < today. This applies uniformly across all four
sources -- for Yellowstone Sheriff's Sales specifically, this is separate
from (and in addition to) the existing sale_status field ('completed'/
'cancelled'), which only that source has since the county's own page states
the outcome directly; the other three sources have no such signal beyond
the date itself.

HOW TO RUN:
  python3 archive_expired_listings.py
  python3 archive_expired_listings.py --dry-run
"""

import argparse
import datetime
import os
import sys

import requests

SUPABASE_URL = os.environ.get("SUPABASE_URL", "PASTE_YOUR_SUPABASE_URL_HERE")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "PASTE_YOUR_SUPABASE_SERVICE_KEY_HERE")


def main():
    parser = argparse.ArgumentParser(description="Archive listings whose sale date has passed")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if "PASTE_YOUR" in SUPABASE_URL or "PASTE_YOUR" in SUPABASE_KEY:
        print("Missing Supabase credentials -- set SUPABASE_URL and SUPABASE_SERVICE_KEY.")
        sys.exit(1)

    today = datetime.date.today().isoformat()
    headers = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}

    resp = requests.get(
        f"{SUPABASE_URL}/rest/v1/trustee_sales",
        headers=headers,
        params={
            "select": "id,county,property_address,sale_date",
            "sale_date": f"lt.{today}",
            "is_expired": "eq.false",
        },
        timeout=30,
    )
    resp.raise_for_status()
    rows = resp.json()

    print(f"Found {len(rows)} listing(s) with sale_date before {today} not yet archived:")
    for r in rows:
        print(f"  {r['sale_date']}  {r['county']:12s} {r['property_address']}")

    if args.dry_run:
        print("\n--dry-run set, not archiving anything.")
        return

    if not rows:
        print("\nNothing to archive.")
        return

    patch_headers = dict(headers, **{"Content-Type": "application/json"})
    ids = [r["id"] for r in rows]
    # Supabase REST 'in' filter: id=in.(a,b,c) -- fine at this dataset's scale
    id_list = ",".join(ids)
    resp = requests.patch(
        f"{SUPABASE_URL}/rest/v1/trustee_sales",
        headers=patch_headers,
        params={"id": f"in.({id_list})"},
        json={"is_expired": True},
        timeout=30,
    )
    if resp.status_code in (200, 204):
        print(f"\n✓ Archived {len(rows)} listing(s)")
    else:
        print(f"\n! Archive PATCH failed: {resp.status_code} {resp.text[:300]}")
        sys.exit(1)


if __name__ == "__main__":
    main()
