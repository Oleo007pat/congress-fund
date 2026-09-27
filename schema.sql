-- =========================================================
-- Congress-Tracking Fund — PostgreSQL skema
-- =========================================================

CREATE TABLE members (
    member_id       SERIAL PRIMARY KEY,
    bioguide_id     VARCHAR(20) UNIQUE NOT NULL,   -- officiel Congress.gov ID
    full_name       VARCHAR(200) NOT NULL,
    chamber         VARCHAR(10) CHECK (chamber IN ('house','senate')),
    state           CHAR(2),
    party           VARCHAR(20),
    is_active       BOOLEAN DEFAULT TRUE,
    updated_at      TIMESTAMP DEFAULT now()
);

CREATE TABLE committees (
    committee_id    SERIAL PRIMARY KEY,
    name            VARCHAR(150) NOT NULL,
    sector_tags     TEXT[]        -- fx {'financials','banking'} bruges til relevans-mapping
);

CREATE TABLE member_committees (
    id              SERIAL PRIMARY KEY,
    member_id       INT REFERENCES members(member_id),
    committee_id    INT REFERENCES committees(committee_id),
    start_date      DATE NOT NULL,
    end_date        DATE,                -- NULL = stadig aktiv
    role            VARCHAR(50)          -- 'chair','ranking_member','member'
);

CREATE TABLE disclosures (
    disclosure_id     SERIAL PRIMARY KEY,
    member_id         INT REFERENCES members(member_id),
    ticker            VARCHAR(15) NOT NULL,
    asset_description VARCHAR(255),
    transaction_type  VARCHAR(10) CHECK (transaction_type IN ('buy','sell','exchange')),
    transaction_date  DATE NOT NULL,
    disclosure_date   DATE NOT NULL,
    amount_low        NUMERIC(14,2),
    amount_high       NUMERIC(14,2),
    owner             VARCHAR(20),        -- self / spouse / joint / dependent
    source            VARCHAR(50),        -- 'senate_efd','house_clerk','aggregator:quiver'
    source_url        TEXT,
    ingested_at       TIMESTAMP DEFAULT now(),
    UNIQUE (member_id, ticker, transaction_date, transaction_type, amount_low)
);

CREATE TABLE performance_scores (
    member_id       INT REFERENCES members(member_id),
    period_start    DATE,
    period_end      DATE,
    return_pct      NUMERIC(10,4),
    cagr_pct        NUMERIC(10,4),
    trade_count     INT,
    computed_at     TIMESTAMP DEFAULT now(),
    PRIMARY KEY (member_id, period_start, period_end)
);

CREATE TABLE investors (
    investor_id     SERIAL PRIMARY KEY,
    name            VARCHAR(200) NOT NULL,
    email           VARCHAR(200) UNIQUE NOT NULL,
    kyc_status      VARCHAR(20) DEFAULT 'pending',
    plaid_item_id   VARCHAR(100),
    created_at      TIMESTAMP DEFAULT now()
);

CREATE TABLE fund_nav (
    nav_date        DATE PRIMARY KEY,
    total_assets    NUMERIC(18,2) NOT NULL,
    total_units     NUMERIC(18,6) NOT NULL,
    nav_per_unit    NUMERIC(18,6) NOT NULL
);

CREATE TABLE investor_ledger (
    entry_id        SERIAL PRIMARY KEY,
    investor_id     INT REFERENCES investors(investor_id),
    entry_type      VARCHAR(20) CHECK (entry_type IN ('deposit','withdrawal','fee','pnl_accrual')),
    amount_usd      NUMERIC(18,2) NOT NULL,
    units_delta     NUMERIC(18,6) NOT NULL,
    nav_per_unit    NUMERIC(18,6) NOT NULL,
    created_at      TIMESTAMP DEFAULT now()
);

CREATE TABLE fees (
    fee_id          SERIAL PRIMARY KEY,
    period_start    DATE NOT NULL,
    period_end      DATE NOT NULL,
    basis_points    NUMERIC(6,4) NOT NULL,   -- 1 bp = 0.01%
    total_fee_usd   NUMERIC(18,2) NOT NULL,
    applied_at      TIMESTAMP DEFAULT now()
);

CREATE TABLE positions (
    ticker          VARCHAR(15) PRIMARY KEY,
    quantity        NUMERIC(18,4) NOT NULL DEFAULT 0,
    avg_cost        NUMERIC(18,4) NOT NULL DEFAULT 0,
    last_updated    TIMESTAMP DEFAULT now()
);

CREATE TABLE orders (
    order_id            SERIAL PRIMARY KEY,
    source_disclosure_id INT REFERENCES disclosures(disclosure_id),
    ticker              VARCHAR(15) NOT NULL,
    side                VARCHAR(4) CHECK (side IN ('buy','sell')),
    quantity            NUMERIC(18,4),
    order_type          VARCHAR(10) DEFAULT 'market',
    status              VARCHAR(20) DEFAULT 'pending',  -- pending/skipped/filled/rejected
    skip_reason         VARCHAR(200),
    broker_order_id     VARCHAR(100),
    submitted_at        TIMESTAMP,
    filled_at           TIMESTAMP,
    fill_price          NUMERIC(18,4),
    expected_slippage_bps NUMERIC(10,4),
    created_at          TIMESTAMP DEFAULT now()
);

CREATE INDEX idx_disclosures_member_date ON disclosures(member_id, transaction_date);
CREATE INDEX idx_orders_status ON orders(status);
