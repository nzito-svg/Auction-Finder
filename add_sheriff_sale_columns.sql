-- Adds columns needed for Yellowstone County Sheriff's Sales -- a 4th,
-- distinct source from the other three (yellowstone_fmt, bozeman_chronicle_column,
-- missoulian_column). Run this once in the Supabase SQL Editor.
--
-- WHY THIS IS A SEPARATE SOURCE:
-- Sheriff's sales are judicial (a lawsuit + court judgment + execution),
-- not a deed-of-trust default -- so they never appear in First Montana
-- Title's report or in newspaper "Notice of Trustee's Sale" listings.
-- The County Sheriff's Office posts its own sale schedule independently:
-- https://www.yellowstonecountymt.gov/sheriff/sheriffsales.asp
-- That page also lists non-real-estate sales (mobile homes, vehicles,
-- equipment) and keeps completed/cancelled sales visible alongside
-- upcoming ones, hence the two new filter columns below.

alter table trustee_sales
    add column if not exists case_number text,      -- e.g. "DV 24 0081"
    add column if not exists sale_status text,       -- 'upcoming', 'completed', 'cancelled'
    add column if not exists property_kind text,     -- 'real_property', 'personal_property'
    add column if not exists opening_bid numeric;
