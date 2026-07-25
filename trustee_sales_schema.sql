-- Real estate auction finder: trustee/foreclosure sale tracking
-- Companion project to the highway bid analyzer (mdt_bid_tabs_parser.py) --
-- run this once in the Supabase SQL Editor (same project as bid-analyzer).
--
-- One row per trustee-sale notice, regardless of which county/source it
-- came from. `source` + `external_id` identify where a row came from and
-- dedup it on re-runs (the ingestion scripts build id = "{source}-{external_id}").

create table if not exists trustee_sales (
    id text primary key,                 -- e.g. "yellowstone_fmt-4086916"
    source text not null,                -- 'yellowstone_fmt', 'missoulian_column', 'bozeman_column', ...
    external_id text,                    -- the source's own record id/number
    state text not null default 'MT',
    county text not null,                -- 'Yellowstone', 'Gallatin', 'Missoula'
    notice_type text,                    -- e.g. "Notice of Trustee's Sale", "Foreclosure Sale"

    borrower_name text,
    is_commercial boolean,               -- heuristic: does borrower_name look like a business entity?
    commercial_match_keyword text,       -- which keyword tripped the classifier (LLC, Inc, Trust, ...), null if none

    trustee_name text,
    lender_name text,

    property_address text,

    recording_date date,                 -- when the Notice of Trustee Sale was recorded
    original_deed_of_trust_date date,
    sale_date date,
    sale_time text,
    sale_location text,                  -- defaults to "courthouse" per source; only set when overridden

    principal_balance numeric,
    original_loan_amount numeric,

    raw_text text,                       -- original block/notice text, for re-parsing later
    source_url text,                     -- PDF or notice URL this row came from
    published_at date,                   -- for newspaper notices: date the notice ran

    fetched_at timestamptz default now()
);

create index if not exists trustee_sales_county_idx on trustee_sales (county);
create index if not exists trustee_sales_is_commercial_idx on trustee_sales (is_commercial);
create index if not exists trustee_sales_sale_date_idx on trustee_sales (sale_date);
