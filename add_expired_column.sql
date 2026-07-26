-- Adds a uniform "is_expired" flag across all sources (Yellowstone trustee
-- sales, Yellowstone Sheriff's sales, Gallatin/Missoula newspaper notices).
-- Run this once in the Supabase SQL Editor.
--
-- WHY A SEPARATE FLAG FROM sale_status:
-- sale_status ('upcoming'/'completed'/'cancelled') only exists for Sheriff's
-- Sales, where the county's own page tells us the outcome. The other three
-- sources have no such signal -- all we know is the sale_date. is_expired
-- is a simple, source-agnostic "the sale date has passed" flag, set by
-- archive_expired_listings.py. Archiving (soft-remove), not deleting --
-- rows stay in the table for reference, just excluded from the working
-- dashboard view.

alter table trustee_sales
    add column if not exists is_expired boolean not null default false;

create index if not exists trustee_sales_is_expired_idx on trustee_sales (is_expired);
