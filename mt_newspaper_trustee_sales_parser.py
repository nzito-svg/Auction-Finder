"""
Gallatin & Missoula County Trustee Sale Notice Ingestion Script
==================================================================

WHAT THIS DOES:
Unlike Yellowstone County (which has a title company publishing a clean
monthly PDF report), Gallatin and Missoula counties have no equivalent
aggregator. Their trustee-sale notices are published as legal notices in
the Bozeman Daily Chronicle (Gallatin) and the Missoulian (Missoula) --
both of which run on "Column" (getcolumn.com), a legal-notice publishing
platform used by Lee Enterprises newspapers. This script queries Column's
search backend directly, pulls every notice tagged as a trustee/
foreclosure sale, and parses the notice text (which follows Montana's
statutorily-required Notice of Trustee's Sale format, so the boilerplate
is consistent across different trustee firms).

HOW THE API CALL WAS FOUND (for future reference if it breaks):
missoulian.column.us and bozemandailychronicle.column.us are Next.js apps
that embed their search config (including facets and the backend URL) in
the page's __NEXT_DATA__ blob. The actual search page's logic is lazy-
loaded, but the request it makes was found by pulling that JS chunk (see
module "M9RC" in the vendor bundle) -- it POSTs to
{backend}/search/public-notices with a flat JSON body (NOT wrapped in a
"data" key at the HTTP layer -- that wrapping happens inside the JS before
the fetch call). Needs an Origin/Referer header matching the paper's
column.us subdomain or the filters are silently ignored and it falls back
to returning an unfiltered global result set.

NOTICE TYPES:
Column tags these under noticetype "Notice of Trustee's Sale", "Trustee
Sale", or occasionally "Foreclosure Sale" -- all three are queried.
Non-judicial trustee sales ("Grantor... conveyed... as Trustee" language)
parse cleanly with the regexes below. Judicial foreclosures ("NOTICE OF
SHERIFF'S SALE" / "NOTICE OF SALE" with a case number) use a different
template -- these are still stored (raw_text + best-effort address/date)
but borrower/trustee/lender fields are more likely to come back empty;
flagged via `notice_type`.

DEDUPING REPUBLISHED NOTICES:
Montana law requires publishing the same notice weekly for 3 consecutive
weeks, so the same sale shows up as 2-3 near-identical search results with
different publishedtimestamp values. Dedup key is the trustee's file
number (almost always the very first line of the notice, e.g. "MT23695"
or "File No. 50079") -- falls back to a hash of grantor+address+sale_date
when no file number is found.

HOW TO RUN:
  pip install requests --break-system-packages
  python mt_newspaper_trustee_sales_parser.py --county gallatin --days 120
  python mt_newspaper_trustee_sales_parser.py --county missoula --days 120

SUPABASE TABLE THIS SCRIPT EXPECTS:
  Same trustee_sales table as yellowstone_trustee_sales_parser.py --
  see trustee_sales_schema.sql.
"""

import argparse
import hashlib
import os
import re
import sys
import time

import requests

# ── CONFIG ───────────────────────────────────────────────────────────────
SUPABASE_URL = os.environ.get("SUPABASE_URL", "PASTE_YOUR_SUPABASE_URL_HERE")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "PASTE_YOUR_SUPABASE_SERVICE_KEY_HERE")

API_BASE = "https://us-central1-enotice-production.cloudfunctions.net/api"
NOTICE_TYPES = ["Notice of Trustee's Sale", "Trustee Sale", "Foreclosure Sale"]

COUNTIES = {
    "gallatin": {
        "county": "Gallatin",
        "newspaper": "Bozeman Daily Chronicle",
        "column_subdomain": "bozemandailychronicle",
        "source": "bozeman_chronicle_column",
    },
    "missoula": {
        "county": "Missoula",
        "newspaper": "Missoulian",
        "column_subdomain": "missoulian",
        "source": "missoulian_column",
    },
}

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
    r"\b(" + "|".join(re.escape(k) for k in COMMERCIAL_KEYWORDS) + r")\b",
    re.IGNORECASE,
)


def classify_borrower(borrower_name):
    if not borrower_name:
        return False, None
    m = COMMERCIAL_PATTERN.search(borrower_name)
    return (True, m.group(1).upper()) if m else (False, None)


def fetch_notices(cfg: dict, from_ts_ms: int, to_ts_ms: int) -> list:
    """Pages through Column's search API for one newspaper's trustee-sale notices."""
    headers = {
        "Content-Type": "application/json",
        "Origin": f"https://{cfg['column_subdomain']}.column.us",
        "Referer": f"https://{cfg['column_subdomain']}.column.us/search",
    }
    all_results = []
    page = 1
    page_size = 50
    while True:
        body = {
            "search": "",
            "allFilters": [
                {"publishedtimestamp": {"from": from_ts_ms, "to": to_ts_ms}},
                {"newspapername": [cfg["newspaper"]]},
                {"noticetype": NOTICE_TYPES},
            ],
            "noneFilters": [],
            "sort": [{"publishedtimestamp": "desc"}],
            "pageSize": page_size,
            "current": page,
            "isDemo": False,
        }
        resp = requests.post(f"{API_BASE}/search/public-notices", headers=headers, json=body, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        results = data.get("results", [])
        all_results.extend(results)
        total_pages = data.get("page", {}).get("total_pages", 1)
        print(f"  fetched page {page}/{total_pages} ({len(results)} notices)")
        if page >= total_pages or not results:
            break
        page += 1
        time.sleep(0.3)
    return all_results


def clean_text(text: str) -> str:
    """De-hyphenate line-wrapped words and collapse excess whitespace.

    The API's extracted text doesn't consistently keep a newline at the
    wrap point (some notices render "Bel-\\ngrade", others "Bel- grade"),
    so both are treated as wrap artifacts -- true hyphenated compounds
    almost never occur in this boilerplate, so this is a safe trade-off.
    """
    text = re.sub(r"-\s+(?=[a-z])", "", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{2,}", "\n", text)
    return text.strip()


def strip_billing_header(text: str) -> str:
    """Column's raw text sometimes includes an ad-billing/proof header before
    the actual notice. Cut everything before the notice title if present."""
    m = re.search(r"NOTICE OF (TRUSTEE.?S? SALE|SHERIFF.?S? SALE|SALE)", text, re.IGNORECASE)
    return text[m.start():] if m else text


def extract_file_number(raw_text: str) -> str:
    first_lines = [l.strip() for l in raw_text.strip().splitlines()[:3] if l.strip()]
    for line in first_lines:
        if re.match(r"^(File No\.?\s*)?[A-Z]{0,4}-?\d{3,}[A-Z0-9-]*$", line, re.IGNORECASE):
            return re.sub(r"^File No\.?\s*", "", line, flags=re.IGNORECASE).strip()
    m = re.search(r"File No\.?\s+([A-Z0-9-]+)", raw_text, re.IGNORECASE)
    return m.group(1) if m else None


def parse_hud_foreclosure_notice(text: str) -> dict:
    """
    Fallback for a second, distinct legal template: HUD/FHA 'NOTICE OF DEFAULT
    AND FORECLOSURE SALE' notices (reverse-mortgage defaults foreclosed under
    the Single Family Mortgage Foreclosure Act of 1994, 12 U.S.C. 3751). These
    use "trustor(s) ... in favor of" instead of "Grantor(s) ... conveyed ...
    as Trustee", so none of the standard fields match. HUD is always the
    beneficiary/lender of record by the time these are published (the loan
    has already been assigned to the Secretary of HUD), so that's hardcoded
    rather than pattern-matched.
    """
    borrower_match = re.search(r"executed by (.+?) as trustor", text, re.IGNORECASE)
    # Ends with a newline, not necessarily a period -- e.g. "Commonly known
    # as: 2352 Clydesdale Lane, Missoula, MT 59804-9783\nThis sale will..."
    address_match = re.search(r"[Cc]ommonly known as:?\s*([^.\n]+)", text)
    sale_match = re.search(
        r"on ([A-Za-z]+ \d{1,2},?\s*\d{4})\s+at\s+(\d{1,2}:\d{2}\s*[ap]\.?m\.?)", text, re.IGNORECASE
    )
    total_owed_match = re.search(r"amount delinquent[^$]*\$(" + MONEY + r")", text, re.IGNORECASE)
    # The Foreclosure Commissioner's name isn't in the boilerplate sentence
    # ("...designation of me as Foreclosure Commissioner") -- it's in the
    # signature block near the end: "/s/NAME\nNAME\nFirm\nForeclosure
    # Commissioner". The plain-text repeat of the name (not the /s/ line
    # itself) is what we want.
    commissioner_match = re.search(r"/s/[^\n]+\n([A-Z][^\n]+)\n", text)

    return {
        "borrower_name": borrower_match.group(1).strip() if borrower_match else None,
        "trustee_name": commissioner_match.group(1).strip() if commissioner_match else None,
        "lender_name": "U.S. Dept. of Housing and Urban Development (HUD)",
        "property_address": address_match.group(1).strip() if address_match else None,
        "sale_date_raw": sale_match.group(1) if sale_match else None,
        "sale_time": sale_match.group(2).upper().replace(".", "") if sale_match else None,
        "sale_location": None,
        "total_amount_owed": float(total_owed_match.group(1).replace(",", "")) if total_owed_match else None,
    }


MONTH_NUM = {
    m: i for i, m in enumerate(
        ["january","february","march","april","may","june","july",
         "august","september","october","november","december"], start=1
    )
}


def parse_sheriff_sale_notice(text: str) -> dict:
    """
    Third distinct legal template found in these newspapers: a judicial
    execution sale ("NOTICE OF SHERIFF'S SALE" -- a lawsuit -> judgment ->
    writ of execution, not a deed-of-trust default). Column tags these
    under the same noticetype as ordinary trustee sales ("Trustee Sale"),
    so they're already being fetched -- they just weren't being parsed,
    which is what silently routed them all into the same collided/
    overwritten row (see build_record's dedup fix).

    Distinguishing structure: "<Plaintiff>,\\nPlaintiff,\\nv.\\n<Defendant
    block>...commonly known as:\\n<address>,\\nDefendants." followed later
    by a plain-language repeat: "Commonly known as <address>." and
    "Notice is hereby given that on <Month Day> at <time> ... the above-
    described property will be sold...", with the notice's own filing
    date given separately as "Date: <Month Day>, <year>" -- the sale's
    own year isn't stated next to the sale date, so it's inferred from
    the filing date (same year if the sale month is >= the filing month,
    else the following year, since these are always noticed forward).
    """
    plaintiff_match = re.search(r"\n([A-Z][^\n]+?),\s*\nPlaintiff", text)
    defendant_match = re.search(
        r"\nv\.\s*\n(.+?)(?:\s+the real property commonly known as|\nDefendants\.)", text, re.DOTALL
    )
    address_match = re.search(r"Commonly known as\s+([^.\n]+)\.", text, re.IGNORECASE)
    case_match = re.search(r"Case No\.?:?\s*([A-Za-z0-9\-]+)", text)
    sale_match = re.search(
        r"on\s+([A-Za-z]+\s+\d{1,2})(?:st|nd|rd|th)?\s+at\s+(\d{1,2}:\d{2}\s*[ap]\.?m\.?)", text, re.IGNORECASE
    )
    filing_date_match = re.search(r"Date:\s*([A-Za-z]+)\s+\d{1,2}(?:st|nd|rd|th)?,?\s+(\d{4})", text)
    sheriff_match = re.search(r"\nBy:\s*([A-Z][A-Za-z.,'\- ]+)", text)
    # Bounded to stop at a zip code -- otherwise the address-like phrase
    # runs straight into the following sentence ("...the above-described
    # property will be sold...") since that text has no commas to stop at.
    location_match = re.search(
        r"on the [^,]*?steps of the ([^,\n]+Courthouse[^,\n]*(?:,\s*[^,\n]+){0,2}?\d{5})", text, re.IGNORECASE
    )

    sale_date_raw = None
    if sale_match and filing_date_match:
        month_name, day = sale_match.group(1).rsplit(" ", 1)
        sale_month = MONTH_NUM.get(month_name.strip().lower())
        filing_month = MONTH_NUM.get(filing_date_match.group(1).strip().lower())
        filing_year = int(filing_date_match.group(2))
        if sale_month and filing_month:
            year = filing_year if sale_month >= filing_month else filing_year + 1
            sale_date_raw = f"{month_name.strip()} {day}, {year}"

    defendant = None
    if defendant_match:
        defendant = re.sub(r"\s+", " ", defendant_match.group(1)).strip().rstrip(";,")
        # Standard boilerplate tacked onto every defendant list, naming the
        # property rather than a real party -- strip it if present so
        # borrower_name reflects only the actual named defendant(s).
        defendant = re.sub(
            r";?\s*and\s+Unknown Parties in possession of or with an interest in\s*$",
            "", defendant, flags=re.IGNORECASE,
        ).strip()

    return {
        "borrower_name": defendant,
        "trustee_name": sheriff_match.group(1).strip() if sheriff_match else None,
        "lender_name": plaintiff_match.group(1).strip() if plaintiff_match else None,
        "property_address": address_match.group(1).strip() if address_match else None,
        "sale_date_raw": sale_date_raw,
        "sale_time": sale_match.group(2).upper().replace(".", "") if sale_match else None,
        "sale_location": location_match.group(1).strip() if location_match else None,
        "total_amount_owed": None,
        "case_number": case_match.group(1) if case_match else None,
    }


def parse_trustee_sale_notice(text: str) -> dict:
    """Parses the standard non-judicial 'NOTICE OF TRUSTEE'S SALE' template."""
    # Grantor names routinely contain periods (middle initials, "Jr."), so
    # excluding periods from the capture truncates them -- anchor on the
    # newline that starts the grantor sentence instead. Falls back to a
    # period-bounded match (accepts truncation risk) for the rare notice
    # where that sentence isn't on its own line, so it can't run away
    # across the whole notice looking for the next "...conveyed".
    grantor_match = re.search(
        r"\n([A-Z][^\n]{0,150}?),?\s+as Grantors?\(?s?\)?,?\s+conveyed", text
    )
    if not grantor_match:
        grantor_match = re.search(
            r"([A-Z][^.\n]{0,120}?),?\s+as Grantors?\(?s?\)?,?\s+conveyed", text
        )
    trustee_match = re.search(r"conveyed said real property to ([^,]+?),\s+as Trustee", text)
    beneficiary_match = re.search(
        r"obligation owed to ([^,]+?)(?:,\s+as designated nominee for ([^,]+?))?,\s+Beneficiary", text
    )
    # Bounded length rather than excluding newlines outright -- an address
    # occasionally wraps mid-line right at the state/zip ("MT\n59714"), and
    # excluding \n entirely made the whole match fail whenever that happened.
    # Any embedded newline gets collapsed to a space where this is used below.
    address_match = re.search(
        r"(?:more commonly known as|common address:?)\s*([^.]{1,100}?)\.", text, re.IGNORECASE
    )
    sale_match = re.search(
        r"Trustee.?s Sale on ([A-Za-z]+ \d{1,2},?\s*\d{4}),?\s+at\s+(\d{1,2}:\d{2}\s*[AP]M)", text
    )
    location_match = re.search(
        r"\d{1,2}:\d{2}\s*[AP]M\s+(?:at|on)\s+(?:the\s+)?([^,]+(?:Courthouse|steps)[^,\n]*(?:,\s*[^,\n]+){0,2})",
        text, re.IGNORECASE,
    )
    total_owed_match = re.search(r"total amount (?:owing|due)[^$]*\$(" + MONEY + r")", text)

    lender = None
    if beneficiary_match:
        lender = beneficiary_match.group(2) or beneficiary_match.group(1)

    if not grantor_match and not address_match:
        # Doesn't match the standard trustee-sale template. Route by an
        # explicit template signal rather than "try one, see if anything
        # matched" -- the HUD template's "commonly known as" address regex
        # is generic enough to also match a Sheriff's Sale notice's own
        # "commonly known as" phrasing, which previously caused every
        # Sheriff's Sale to get silently mislabeled as a HUD sale (with a
        # hardcoded HUD lender_name) since HUD was tried first.
        if re.search(r"sheriff'?s sale|writ of execution", text, re.IGNORECASE):
            sheriff_parsed = parse_sheriff_sale_notice(text)
            if sheriff_parsed["borrower_name"] or sheriff_parsed["property_address"]:
                return sheriff_parsed
        hud_parsed = parse_hud_foreclosure_notice(text)
        if hud_parsed["borrower_name"] or hud_parsed["property_address"]:
            return hud_parsed

    return {
        "borrower_name": grantor_match.group(1).strip() if grantor_match else None,
        "trustee_name": trustee_match.group(1).strip() if trustee_match else None,
        "lender_name": lender.strip() if lender else None,
        "property_address": re.sub(r"\s+", " ", address_match.group(1)).strip() if address_match else None,
        "sale_date_raw": sale_match.group(1) if sale_match else None,
        "sale_time": sale_match.group(2) if sale_match else None,
        "sale_location": location_match.group(1).strip() if location_match else None,
        "total_amount_owed": float(total_owed_match.group(1).replace(",", "")) if total_owed_match else None,
    }


def parse_sale_date(date_str):
    if not date_str:
        return None
    date_str = re.sub(r",(?=\d)", ", ", date_str.strip())
    from datetime import datetime
    try:
        return datetime.strptime(date_str, "%B %d, %Y").date().isoformat()
    except ValueError:
        return None


def build_record(hit: dict, cfg: dict) -> dict:
    raw = strip_billing_header(clean_text(hit.get("text", "")))
    file_number = extract_file_number(raw)
    parsed = parse_trustee_sale_notice(raw)

    if file_number:
        dedup_source = file_number
    elif parsed.get("case_number"):
        # Sheriff's Sale notices don't have the trustee-firm "File No." that
        # extract_file_number looks for, but a court case number is just as
        # stable a dedup key across the notice's 3 weekly republications.
        dedup_source = parsed["case_number"]
    elif parsed.get("borrower_name") or parsed.get("property_address") or parsed.get("sale_date_raw"):
        dedup_source = hashlib.sha1(
            f"{parsed.get('borrower_name')}|{parsed.get('property_address')}|{parsed.get('sale_date_raw')}".encode()
        ).hexdigest()[:16]
    else:
        # Total parse failure (an unrecognized notice template): the three
        # fields above are all None, which would hash identically for EVERY
        # such notice -- silently colliding unrelated sales into one row
        # that just gets overwritten each ingestion run. Hash the raw text
        # instead so distinct notices don't clobber each other. Tradeoff:
        # weekly republications of the same unparsed notice (MT law requires
        # 3 consecutive weeks) may dedupe less precisely than the
        # structured-field path above, since re-fetched text isn't always
        # byte-identical -- duplicate rows here are a much smaller problem
        # than silently losing sales.
        dedup_source = hashlib.sha1(raw.encode()).hexdigest()[:16]

    is_commercial, keyword = classify_borrower(parsed.get("borrower_name"))

    return {
        "id": f"{cfg['source']}-{dedup_source}",
        "source": cfg["source"],
        "external_id": dedup_source,
        "state": "MT",
        "county": cfg["county"],
        "notice_type": hit.get("noticetype"),
        "borrower_name": parsed.get("borrower_name"),
        "is_commercial": is_commercial,
        "commercial_match_keyword": keyword,
        "trustee_name": parsed.get("trustee_name"),
        "lender_name": parsed.get("lender_name"),
        "property_address": parsed.get("property_address"),
        "recording_date": None,
        "original_deed_of_trust_date": None,
        "sale_date": parse_sale_date(parsed.get("sale_date_raw")),
        "sale_time": parsed.get("sale_time"),
        "sale_location": parsed.get("sale_location"),
        "principal_balance": None,
        "original_loan_amount": None,
        "case_number": parsed.get("case_number"),
        "raw_text": raw[:20000],
        "source_url": hit.get("pdfurl") or None,
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
    parser = argparse.ArgumentParser(description="Parse Gallatin/Missoula trustee sale newspaper notices")
    parser.add_argument("--county", required=True, choices=list(COUNTIES.keys()))
    parser.add_argument("--days", type=int, default=120, help="Lookback window in days")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    cfg = COUNTIES[args.county]

    import time as time_mod
    to_ts = int(time_mod.time() * 1000)
    from_ts = to_ts - args.days * 86400 * 1000

    print(f"Fetching {cfg['newspaper']} trustee sale notices (last {args.days} days)...")
    hits = fetch_notices(cfg, from_ts, to_ts)
    print(f"  {len(hits)} raw notice hits (includes repeat weekly publications)")

    by_id = {}
    for hit in hits:
        record = build_record(hit, cfg)
        # keep the most recently-fetched version per dedup id (later weekly
        # republication text is identical; harmless to overwrite)
        by_id[record["id"]] = record
    records = list(by_id.values())

    commercial = [r for r in records if r["is_commercial"]]
    print(
        f"\nDeduped to {len(records)} unique notices -- "
        f"{len(commercial)} flagged commercial, {len(records) - len(commercial)} residential/unclassified"
    )

    if args.dry_run:
        print("\n--dry-run set, not writing to Supabase.")
        for r in records:
            flag = "COMMERCIAL" if r["is_commercial"] else "residential"
            print(f"  [{flag:10s}] {r['borrower_name']!r:40s} {r['property_address']} ({r['notice_type']})")
        return

    if "PASTE_YOUR" in SUPABASE_URL or "PASTE_YOUR" in SUPABASE_KEY:
        print("\nMissing Supabase credentials -- set SUPABASE_URL and SUPABASE_SERVICE_KEY.")
        sys.exit(1)

    upsert_table("trustee_sales", records)


if __name__ == "__main__":
    main()
