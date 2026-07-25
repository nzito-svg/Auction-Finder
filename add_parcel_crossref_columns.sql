-- Adds property-classification cross-reference columns to trustee_sales.
-- Run this once in the Supabase SQL Editor (same project, after the base
-- trustee_sales_schema.sql has already been run).
--
-- WHY: the original commercial classifier only looked at the borrower's
-- name (LLC, Inc, Trust, etc.), which misses a commercial property held
-- in an individual's own name -- exactly the case that prompted this.
-- These columns store an independent second signal: Montana's own parcel
-- classification (DOR/county CAMA data), looked up by property address or
-- owner name via enrich_with_parcel_data.py, regardless of how the
-- borrower's name reads.

alter table trustee_sales
    add column if not exists is_commercial_by_name boolean,       -- the original borrower-name heuristic, preserved distinctly
    add column if not exists is_commercial_by_proptype boolean,   -- from Montana's own parcel PropType field
    add column if not exists county_prop_type text,               -- e.g. "Industrial Property", "Apartment", "Improved Property"
    add column if not exists county_owner_name text,               -- owner name on file with the county/DOR, for cross-checking against borrower_name
    add column if not exists county_legal_description text,
    add column if not exists county_match_confidence text,        -- 'address', 'owner_unique', 'owner_ambiguous', 'no_match'
    add column if not exists county_matched_at timestamptz,
    -- Manual web-search review pass (searched by hand in chat -- OSM geocoding/
    -- POI lookup and scripted DuckDuckGo scraping were both tried first and
    -- rejected: OSM only resolved 71% of addresses, DDG got bot-blocked after
    -- a handful of requests). is_commercial_by_web_search is a signal, not an
    -- auto-dismiss -- a 'none_found' review means nothing turned up in search,
    -- not a confirmed-clean result, since search coverage itself is imperfect.
    add column if not exists is_commercial_by_web_search boolean,
    add column if not exists web_search_confidence text,          -- 'strong', 'moderate', 'weak', 'none_found', 'not_reviewed'
    add column if not exists web_search_notes text,                -- what was found, e.g. "A & D Sprinklers, exact address match"
    add column if not exists web_search_reviewed_at timestamptz;

-- Backfill is_commercial_by_name from the existing is_commercial values
-- (all rows so far were classified by name only, before this migration).
update trustee_sales
set is_commercial_by_name = is_commercial
where is_commercial_by_name is null;
