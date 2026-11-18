-- Finance schema for Metabase. Applied by the loader on every load, so it must
-- stay idempotent. Edit the rule tables (payee_rules, one_offs, budget_lines) with
-- psql as the admin user (Metabase only reads); your edits are never overwritten.

CREATE SCHEMA IF NOT EXISTS finance;

CREATE TABLE IF NOT EXISTS finance.transactions (
    tx_id             integer PRIMARY KEY,
    date              date NOT NULL,
    account           text,
    account_type      text,
    payee             text,
    original_payee    text,
    category          text,
    amount            numeric(12,2) NOT NULL,   -- negative = money out
    memo              text,
    tags              text,
    business_personal text,
    excluded          boolean,
    is_transfer       boolean,
    one_off           boolean,
    number            text,
    cleared           text
);

CREATE TABLE IF NOT EXISTS finance.load_log (
    loaded_at   timestamptz NOT NULL DEFAULT now(),
    file        text NOT NULL,
    file_mtime  timestamptz NOT NULL,
    rows        integer NOT NULL
);

-- How to treat a payee, overriding Simplifi. The longest matching pattern wins.
-- treat_as: spend (counts as spending even if Simplifi calls it a transfer),
--           credit (refund / points: reduces spending), income, ignore.
CREATE TABLE IF NOT EXISTS finance.payee_rules (
    pattern      text PRIMARY KEY,              -- ILIKE pattern, e.g. 'Zelle Payment To%'
    treat_as     text NOT NULL CHECK (treat_as IN ('spend','credit','income','ignore')),
    budget_line  text,                          -- optional: force a budget line
    note         text
);

-- Specific transactions to report separately from normal spending.
CREATE TABLE IF NOT EXISTS finance.one_offs (
    date           date NOT NULL,
    payee_pattern  text NOT NULL,
    amount         numeric(12,2) NOT NULL,      -- positive
    note           text,
    PRIMARY KEY (date, payee_pattern, amount)
);

CREATE TABLE IF NOT EXISTS finance.budget_lines (
    line            text PRIMARY KEY,
    monthly_budget  numeric(12,2) NOT NULL,
    sort            integer NOT NULL
);

INSERT INTO finance.payee_rules (pattern, treat_as, budget_line, note) VALUES
  ('Zelle Payment To Syeda F. Quadri%', 'spend',  'Housekeeping', 'House cleaning, $200 twice a month'),
  ('Zelle Payment To%',                 'spend',  NULL,           'Zelle to other people is spending, not a transfer'),
  ('Withdrawal To Orange Savings%',     'ignore', NULL,           'Moving money to own savings'),
  ('Deposit From My Orange Checking%',  'ignore', NULL,           'Other side of the savings move'),
  ('Applecard%',                        'ignore', NULL,           'Credit card payment'),
  ('American Express Internal Transaction%', 'credit', NULL,      'Amex Pay with Points and card credits'),
  ('Gainwell%',                         'income', NULL,           'Payroll')
ON CONFLICT (pattern) DO NOTHING;

INSERT INTO finance.one_offs (date, payee_pattern, amount, note) VALUES
  ('2026-10-29', 'Hyundai%',                      321.24, 'Auto repair'),
  ('2026-11-03', 'Rtsp%',                         255.66, 'Permit to Carry course and certification'),
  ('2026-11-17', 'Zelle Payment To Habeeb Syed%', 807.17, 'Auto repair paid through son')
ON CONFLICT DO NOTHING;

INSERT INTO finance.budget_lines (line, monthly_budget, sort) VALUES
  ('Rent', 3000.00, 1),
  ('LightStream loan', 987.68, 2),
  ('PSE&G', 650.00, 3),
  ('Housekeeping', 400.00, 4),
  ('Health & pharmacy', 400.00, 5),
  ('Landscaping reserve', 166.67, 6),
  ('Snow reserve', 66.67, 7),
  ('Aura', 38.25, 8),
  ('Everything else', 740.00, 9)
ON CONFLICT (line) DO NOTHING;

-- One row per transaction, with Syed's corrections applied.
--   kind:        spend | credit | income | ignore
--   spend:       positive = money spent (credits come out negative)
--   budget_line: which line of the personal budget it counts against
CREATE OR REPLACE VIEW finance.ledger AS
WITH ruled AS (
    SELECT t.*, r.treat_as, r.budget_line AS rule_line
    FROM finance.transactions t
    LEFT JOIN LATERAL (
        SELECT pr.treat_as, pr.budget_line
        FROM finance.payee_rules pr
        WHERE t.payee ILIKE pr.pattern
        ORDER BY length(pr.pattern) DESC
        LIMIT 1
    ) r ON true
), classified AS (
    SELECT ruled.*,
        CASE
            WHEN treat_as IS NOT NULL THEN treat_as
            WHEN is_transfer OR excluded THEN 'ignore'
            WHEN amount < 0 THEN 'spend'
            WHEN account_type = 'CCard' THEN 'credit'
            ELSE 'income'
        END AS kind
    FROM ruled
)
SELECT
    c.tx_id,
    c.date,
    date_trunc('month', c.date)::date AS month,
    c.account,
    c.payee,
    c.category,
    CASE WHEN c.business_personal ILIKE 'HM Syed%' THEN 'Business' ELSE 'Personal' END AS scope,
    c.kind,
    c.amount,
    CASE WHEN c.kind IN ('spend','credit') THEN -c.amount ELSE 0 END AS spend,
    COALESCE(c.rule_line,
        CASE
            WHEN c.category ILIKE 'Home:Rent%'                 THEN 'Rent'
            WHEN c.payee ILIKE '%lightstream%'                 THEN 'LightStream loan'
            WHEN c.category ILIKE 'Utilities:Gas & Electric%'  THEN 'PSE&G'
            WHEN c.category ILIKE 'Health%'                    THEN 'Health & pharmacy'
            WHEN c.payee ILIKE '%aura%'                        THEN 'Aura'
            WHEN c.category ILIKE '%lawn%' OR c.category ILIKE '%landscap%' THEN 'Landscaping reserve'
            WHEN c.payee ILIKE '%snow%'                        THEN 'Snow reserve'
            ELSE 'Everything else'
        END) AS budget_line,
    (c.one_off OR EXISTS (
        SELECT 1 FROM finance.one_offs o
        WHERE o.date = c.date AND c.payee ILIKE o.payee_pattern AND abs(c.amount) = o.amount
    )) AS is_one_off
FROM classified c;

-- Personal spending by month and budget line (one-offs left out), against the budget.
CREATE OR REPLACE VIEW finance.budget_vs_actual AS
WITH months AS (
    SELECT DISTINCT month FROM finance.ledger
), actual AS (
    SELECT month, budget_line, sum(spend) AS spent
    FROM finance.ledger
    WHERE scope = 'Personal' AND kind IN ('spend','credit') AND NOT is_one_off
    GROUP BY 1, 2
)
SELECT m.month, b.sort, b.line AS budget_line, b.monthly_budget,
       COALESCE(a.spent, 0) AS spent,
       COALESCE(a.spent, 0) - b.monthly_budget AS over_under
FROM months m
CROSS JOIN finance.budget_lines b
LEFT JOIN actual a ON a.month = m.month AND a.budget_line = b.line;

-- One row per month: the headline numbers.
CREATE OR REPLACE VIEW finance.monthly_summary AS
SELECT
    month,
    sum(spend) FILTER (WHERE scope = 'Personal' AND kind IN ('spend','credit') AND NOT is_one_off) AS personal_spending,
    sum(spend) FILTER (WHERE scope = 'Personal' AND kind IN ('spend','credit') AND is_one_off)     AS personal_one_offs,
    6450.00                                                                                         AS ceiling,
    sum(amount) FILTER (WHERE scope = 'Personal' AND kind = 'income' AND payee ILIKE 'Gainwell%')   AS payroll_income,
    sum(spend) FILTER (WHERE scope = 'Business' AND kind IN ('spend','credit'))                     AS business_spending,
    sum(amount) FILTER (WHERE scope = 'Business' AND kind = 'income')                               AS business_income
FROM finance.ledger
GROUP BY month;
