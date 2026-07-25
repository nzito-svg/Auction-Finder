"""
Web Search Cross-Reference Enrichment Script
=================================================

WHY THIS EXISTS:
Two prior attempts at an independent "is this property commercial" signal
both fell short:
  1. Borrower-name keywords (LLC, Inc, Trust...) miss a business owned by
     an individual -- the case that prompted this whole line of work.
  2. Montana's own parcel classification (enrich_with_parcel_data.py)
     lumps ordinary houses and small commercial buildings into the same
     "Improved Property" bucket -- doesn't separate them.
  3. OpenStreetMap geocoding (tested, not shipped) only resolved 71% of
     our real addresses, and just 33% in Gallatin County -- a geocode
     miss looks identical to "no business found," which would wrongly
     clear exactly the properties we're least sure about. Rejected.

This one instead scrapes DuckDuckGo's HTML search (no API key, no billing,
no signup) for each property address and looks for signs of a business
actually operating there -- business directories, review sites, company
listings -- as opposed to real-estate listing sites and people-search
sites, which is what shows up for an ordinary house.

HOW IT WORKS:
1. Search DuckDuckGo HTML results for the quoted street address.
2. Classify each result's domain as one of:
     - real_estate  (zillow, redfin, realtor.com, trulia, realtytrac...)
     - people_search (whitepages, spokeo, truepeoplesearch...)
     - business_signal (bizapedia, yelp, manta, bbb.org, chamber sites,
       linkedin company pages, mapquest business listings, yellowpages...)
     - other
2. A business_signal hit only counts if the RESULT SNIPPET actually
   contains the house number (not just the city/state) -- otherwise a
   generic "businesses in this town" page can false-positive just for
   sharing a city name. This is the same failure mode observed in initial
   testing (a "NOTABLE HANDMADE CRAFTS in Belgrade, MT" result surfaced
   for an address search, with no actual address match in the snippet).
3. Rows get a confidence-scored flag, NOT an auto-dismiss. Per the
   decision behind building this: false negatives (wrongly clearing a
   real commercial property) are costly, false positives just mean an
   extra manual look -- so this stays a research aid, surfacing what it
   found for a human to weigh, not a silent filter.

KNOWN FRAGILITY:
Scraping a search engine's HTML is inherently less stable than the ArcGIS/
column.us APIs used elsewhere in this project -- DuckDuckGo can change
their markup or start blocking automated requests at any time. If this
script starts returning zero results across the board, that's the first
thing to check (re-run the diagnostic in the project chat history to see
the current result HTML structure).

HOW TO RUN:
  python enrich_with_web_search.py             # only rows not yet checked
  python enrich_with_web_search.py --force      # re-check everything
  python enrich_with_web_search.py --dry-run
"""

import argparse
import os
import re
import sys
import time
import urllib.parse

import requests

SUPABASE_URL = os.environ.get("SUPABASE_URL", "PASTE_YOUR_SUPABASE_URL_HERE")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "PASTE_YOUR_SUPABASE_SERVICE_KEY_HERE")

SEARCH_URL = "https://html.duckduckgo.com/html/"
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"

REAL_ESTATE_DOMAINS = {
    "zillow.com", "redfin.com", "realtor.com", "trulia.com", "realtytrac.com",
    "homes.com", "movoto.com", "propertyshark.com", "auction.com",
}
PEOPLE_SEARCH_DOMAINS = {
    "whitepages.com", "spokeo.com", "truepeoplesearch.com", "fastpeoplesearch.com",
    "beenverified.com", "intelius.com", "radaris.com",
}
BUSINESS_SIGNAL_DOMAINS = {
    "bizapedia.com", "yelp.com", "manta.com", "bbb.org", "yellowpages.com",
    "mapquest.com", "linkedin.com", "dnb.com", "buzzfile.com", "chamberofcommerce.com",
    "opencorporates.com", "loopnet.com", "crexi.com",  # loopnet/crexi = commercial listing sites, strong signal
}

RESULT_LINK_RE = re.compile(r'<a[^>]*class="result__a"[^>]*href="([^"]*)"[^>]*>(.*?)</a>', re.DOTALL)
RESULT_SNIPPET_RE = re.compile(r'<a[^>]*class="result__snippet"[^>]*>(.*?)</a>', re.DOTALL)
TAG_STRIP_RE = re.compile(r"<[^>]+>")


def strip_tags(html_fragment: str) -> str:
    return TAG_STRIP_RE.sub("", html_fragment).strip()


def extract_domain(ddg_redirect_url: str) -> str:
    m = re.search(r"uddg=([^&]+)", ddg_redirect_url)
    if not m:
        return ""
    real_url = urllib.parse.unquote(m.group(1))
    try:
        return urllib.parse.urlparse(real_url).netloc.replace("www.", "")
    except Exception:
        return ""


def house_number(address: str):
    if not address:
        return None
    m = re.match(r"\s*(\d+)", address.strip())
    return m.group(1) if m else None


def search_address(address: str) -> list:
    """Returns list of {domain, title, snippet} for the top DDG results."""
    q = f'"{address}"'
    resp = requests.get(
        SEARCH_URL,
        params={"q": q},
        headers={"User-Agent": USER_AGENT},
        timeout=20,
    )
    resp.raise_for_status()
    html = resp.text

    links = RESULT_LINK_RE.findall(html)
    snippets = RESULT_SNIPPET_RE.findall(html)

    results = []
    for i, (href, title_html) in enumerate(links):
        domain = extract_domain(href)
        title = strip_tags(title_html)
        snippet = strip_tags(snippets[i]) if i < len(snippets) else ""
        results.append({"domain": domain, "title": title, "snippet": snippet})
    return results


def classify_results(address: str, results: list) -> dict:
    num = house_number(address)
    real_estate_hits, people_search_hits, business_hits = [], [], []

    for r in results:
        domain = r["domain"]
        combined_text = f"{r['title']} {r['snippet']}"
        address_confirmed = bool(num) and num in combined_text

        if domain in REAL_ESTATE_DOMAINS:
            real_estate_hits.append(r)
        elif domain in PEOPLE_SEARCH_DOMAINS:
            people_search_hits.append(r)
        elif domain in BUSINESS_SIGNAL_DOMAINS and address_confirmed:
            business_hits.append(r)

    return {
        "is_commercial_by_web_search": len(business_hits) > 0,
        "web_search_business_domains": ", ".join(sorted({h["domain"] for h in business_hits})) or None,
        "web_search_top_result_domains": ", ".join(r["domain"] for r in results[:8] if r["domain"]),
        "web_search_result_count": len(results),
    }


def fetch_rows_to_check(force: bool) -> list:
    url = f"{SUPABASE_URL}/rest/v1/trustee_sales"
    params = {"select": "id,property_address"}
    if not force:
        params["web_search_checked_at"] = "is.null"
    headers = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}
    resp = requests.get(url, headers=headers, params=params, timeout=30)
    resp.raise_for_status()
    rows = resp.json()
    return [
        r for r in rows
        if r.get("property_address") and r["property_address"].strip().lower() not in ("n/a", "na", "")
    ]


def update_row(row_id: str, patch: dict) -> bool:
    url = f"{SUPABASE_URL}/rest/v1/trustee_sales"
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=minimal",
    }
    resp = requests.patch(url, headers=headers, params={"id": f"eq.{row_id}"}, json=patch, timeout=30)
    return resp.status_code in (200, 204)


def _now_iso():
    import datetime
    return datetime.datetime.utcnow().isoformat() + "Z"


def main():
    parser = argparse.ArgumentParser(description="Cross-reference trustee_sales addresses against web search results")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if "PASTE_YOUR" in SUPABASE_URL or "PASTE_YOUR" in SUPABASE_KEY:
        print("Missing Supabase credentials -- set SUPABASE_URL and SUPABASE_SERVICE_KEY.")
        sys.exit(1)

    rows = fetch_rows_to_check(args.force)
    print(f"Checking {len(rows)} row(s)...")

    newly_commercial = []
    errors = 0

    for i, row in enumerate(rows):
        try:
            results = search_address(row["property_address"])
            classification = classify_results(row["property_address"], results)
        except Exception as e:
            print(f"  ! Search failed for {row['id']}: {e}")
            errors += 1
            time.sleep(2)
            continue

        if classification["is_commercial_by_web_search"]:
            newly_commercial.append((row["id"], row["property_address"], classification["web_search_business_domains"]))

        if not args.dry_run:
            patch = dict(classification)
            patch["web_search_checked_at"] = _now_iso()
            if not update_row(row["id"], patch):
                print(f"  ! Failed to update {row['id']}")

        if (i + 1) % 10 == 0:
            print(f"  ...{i+1}/{len(rows)}")
        time.sleep(1.5)  # be a polite, low-volume scraper

    print(f"\n{len(newly_commercial)} row(s) flagged by web search (business directory/listing found at the exact address):")
    for row_id, address, domains in newly_commercial:
        print(f"  {address} -> {domains}")
    if errors:
        print(f"\n{errors} row(s) failed to search (network/parsing error, not a clean 'no business' result -- worth re-running)")

    if args.dry_run:
        print("\n--dry-run set, no rows were updated.")


if __name__ == "__main__":
    main()
