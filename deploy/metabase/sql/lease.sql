-- Temporary Palisade lease watch (Nov 18 - Dec 15, 2026). Applied by the lease
-- loader before every load, so it must stay idempotent. Drop the whole thing
-- afterwards with:  DROP SCHEMA lease CASCADE;

CREATE SCHEMA IF NOT EXISTS lease;
GRANT USAGE ON SCHEMA lease TO finance_reader;
ALTER DEFAULT PRIVILEGES IN SCHEMA lease GRANT SELECT ON TABLES TO finance_reader;

-- One row per run: what was found and the recommendation.
CREATE TABLE IF NOT EXISTS lease.runs (
    check_date            date NOT NULL,
    summary               text,
    best_effective_monthly numeric(10,2),
    best_source           text,
    recommendation        text
);

-- One row per matching vehicle per check.
CREATE TABLE IF NOT EXISTS lease.vehicles (
    check_date       date NOT NULL,
    dealer           text NOT NULL,
    location         text,
    state            text,
    miles_from_nb    numeric(6,1),
    model_year       integer,
    trim             text,
    seats            integer,
    exterior         text,
    interior         text,
    vin              text,
    msrp             numeric(10,2),
    advertised_price numeric(10,2),
    doc_fee          numeric(10,2),
    status           text,              -- listed / in transit / gone / unconfirmed
    url              text,
    notes            text
);

-- Offers and estimates: manufacturer programs, broker listings, dealer quotes,
-- and Claude's estimate for a specific car with the Santa Fe trade.
CREATE TABLE IF NOT EXISTS lease.offers (
    check_date             date NOT NULL,
    source                 text NOT NULL,
    kind                   text,        -- manufacturer / broker / dealer_quote / estimate / target (a negotiating goal, not an offer)
    model_year             integer,
    seats                  integer,
    vin                    text,
    msrp                   numeric(10,2),
    selling_price          numeric(10,2),
    monthly_advertised     numeric(10,2),
    term_months            integer,
    miles_per_year         integer,
    due_at_signing         numeric(10,2),
    residual_pct           numeric(5,2),
    money_factor           numeric(8,6),
    expires                date,
    est_monthly_with_trade numeric(10,2),
    est_cash_at_signing    numeric(10,2),
    effective_monthly      numeric(10,2),
    meets_target           boolean,     -- payment <= $550 for 36 months and $0 extra cash
    url                    text,
    notes                  text
);

-- Quote requests to dealers: drafted, sent, answered.
CREATE TABLE IF NOT EXISTS lease.quotes (
    requested_date  date,
    dealer          text NOT NULL,
    vin             text,
    status          text,               -- draft / sent / replied / declined
    last_update     date,
    monthly         numeric(10,2),
    due_at_signing  numeric(10,2),
    selling_price   numeric(10,2),
    money_factor    numeric(8,6),
    residual_pct    numeric(5,2),
    notes           text
);

CREATE TABLE IF NOT EXISTS lease.load_log (
    loaded_at timestamptz NOT NULL DEFAULT now(),
    file      text NOT NULL,
    rows      integer NOT NULL
);

-- Latest sighting of each vehicle, and whether it was still listed last check.
CREATE OR REPLACE VIEW lease.vehicle_status AS
WITH last_check AS (SELECT max(check_date) AS d FROM lease.vehicles),
     latest AS (
        SELECT DISTINCT ON (coalesce(vin, dealer || model_year || exterior)) v.*
        FROM lease.vehicles v
        ORDER BY coalesce(vin, dealer || model_year || exterior), check_date DESC)
SELECT l.*,
       (SELECT min(check_date) FROM lease.vehicles f WHERE f.vin = l.vin) AS first_seen,
       l.check_date = (SELECT d FROM last_check) AS seen_last_check,
       l.check_date = (SELECT d FROM last_check)
         AND coalesce(l.status, '') NOT IN ('gone', 'sold') AS listed_now
FROM latest l;

-- Offers from the most recent check, best first.
CREATE OR REPLACE VIEW lease.current_offers AS
SELECT *
FROM lease.offers
WHERE check_date = (SELECT max(check_date) FROM lease.offers)
ORDER BY meets_target DESC NULLS LAST, effective_monthly NULLS LAST;

GRANT SELECT ON ALL TABLES IN SCHEMA lease TO finance_reader;
