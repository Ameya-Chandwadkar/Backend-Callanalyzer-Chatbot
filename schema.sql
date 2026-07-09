-- MasonMart Unified Data Store
-- Callyzer + Shopify Integration
--
-- Design principle (per PRD Section 5.3): raw ingested data is kept
-- separate from derived/joined views, so every number the chat layer
-- reports is traceable back to a specific Callyzer or Shopify record.
--
-- Phone numbers are the join key. They are stored in TWO forms in every
-- table that has one: the original as-seen value (for audit trail) and
-- a normalized value (10-digit, no +91/spaces/dashes) used for joining.
-- This mirrors the manual cross-referencing work already done in past
-- audits, where phone number formatting mismatches were a recurring
-- source of missed matches.

PRAGMA foreign_keys = ON;

-- ─────────────────────────────────────────────
-- RAW LAYER — Callyzer
-- ─────────────────────────────────────────────

-- One row per call record from the "Periodic Call History Report" export.
CREATE TABLE IF NOT EXISTS callyzer_calls (
    call_id             INTEGER PRIMARY KEY AUTOINCREMENT,
    call_timestamp      TEXT NOT NULL,      -- ISO 8601, parsed from export
    direction            TEXT,               -- 'incoming' / 'outgoing'
    duration_seconds     INTEGER,
    connected            INTEGER,            -- 1 if duration > 45s, else 0
    rep_name             TEXT,
    rep_sim_number       TEXT,               -- company SIM, most reliable rep key
    customer_number_raw  TEXT,
    customer_number_norm TEXT,               -- normalized 10-digit
    source_file          TEXT NOT NULL,
    row_hash             TEXT NOT NULL UNIQUE, -- de-dup guard, see ingest script
    ingested_at          TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_calls_customer ON callyzer_calls(customer_number_norm);
CREATE INDEX IF NOT EXISTS idx_calls_rep ON callyzer_calls(rep_sim_number);
CREATE INDEX IF NOT EXISTS idx_calls_timestamp ON callyzer_calls(call_timestamp);

-- One row per lead from the "Lead Data Report" export.
-- Re-ingesting the same lead updates it in place (leads change status/attempts over time).
CREATE TABLE IF NOT EXISTS callyzer_leads (
    lead_no               TEXT PRIMARY KEY,
    lead_name             TEXT,
    contact_number_raw    TEXT,
    contact_number_norm   TEXT,
    assigned_to           TEXT,
    tags                  TEXT,               -- e.g. 'IndiaMart Lead', 'Gold Lead'
    no_of_attempts        INTEGER,
    last_call_datetime    TEXT,
    last_call_note        TEXT,
    lead_status           TEXT,
    created_date          TEXT,
    source_file           TEXT NOT NULL,
    ingested_at            TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_leads_contact ON callyzer_leads(contact_number_norm);
CREATE INDEX IF NOT EXISTS idx_leads_assigned ON callyzer_leads(assigned_to);

-- ─────────────────────────────────────────────
-- RAW LAYER — Shopify
-- ─────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS shopify_orders (
    order_id              TEXT PRIMARY KEY,   -- Shopify order GID or numeric id
    order_number          TEXT,
    created_at            TEXT,
    total_price           REAL,
    financial_status      TEXT,
    customer_phone_raw    TEXT,
    customer_phone_norm   TEXT,
    customer_name         TEXT,
    rep_attribution       TEXT,               -- from order note/tag/custom field if present
    ingested_at           TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_orders_customer ON shopify_orders(customer_phone_norm);
CREATE INDEX IF NOT EXISTS idx_orders_created ON shopify_orders(created_at);

CREATE TABLE IF NOT EXISTS shopify_customers (
    customer_id      TEXT PRIMARY KEY,
    phone_raw         TEXT,
    phone_norm        TEXT,
    name              TEXT,
    email             TEXT,
    ingested_at       TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_customers_phone ON shopify_customers(phone_norm);

-- ─────────────────────────────────────────────
-- OPERATIONAL / AUDIT TABLES
-- ─────────────────────────────────────────────

-- Every ingestion run logs itself here — required by PRD 5.5
-- ("basic data validation ... flag malformed rows, missing fields, duplicates").
CREATE TABLE IF NOT EXISTS ingestion_log (
    log_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    source             TEXT NOT NULL,          -- 'callyzer_calls' / 'callyzer_leads' / 'shopify_orders' / 'shopify_customers'
    file_name           TEXT,
    rows_read           INTEGER,
    rows_inserted        INTEGER,
    rows_updated          INTEGER,
    rows_flagged           INTEGER,             -- malformed / missing fields
    rows_duplicate           INTEGER,
    started_at                TEXT,
    finished_at                TEXT,
    notes                       TEXT
);

-- Individual flagged rows so the product owner can see WHY something
-- didn't load cleanly, instead of it silently disappearing.
CREATE TABLE IF NOT EXISTS ingestion_flags (
    flag_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    log_id         INTEGER REFERENCES ingestion_log(log_id),
    reason          TEXT NOT NULL,             -- e.g. 'missing customer number', 'unparseable date'
    raw_row          TEXT,
    flagged_at        TEXT NOT NULL
);

-- Tracks the last successful sync point per source, so ingestion scripts
-- know where to resume (used mainly by the Shopify API script).
CREATE TABLE IF NOT EXISTS sync_state (
    source        TEXT PRIMARY KEY,
    last_synced_at TEXT
);

-- ─────────────────────────────────────────────
-- DERIVED VIEW — the join Callyzer <> Shopify
-- ─────────────────────────────────────────────

-- Per PRD 5.3: raw data stays separate; this view is the derived join,
-- always re-computed from raw tables, never stored/duplicated.
CREATE VIEW IF NOT EXISTS v_calls_with_orders AS
SELECT
    c.call_id,
    c.call_timestamp,
    c.rep_name,
    c.rep_sim_number,
    c.customer_number_norm,
    c.duration_seconds,
    c.connected,
    o.order_id,
    o.order_number,
    o.created_at   AS order_created_at,
    o.total_price,
    o.financial_status
FROM callyzer_calls c
LEFT JOIN shopify_orders o
    ON c.customer_number_norm = o.customer_phone_norm;

-- Orders that could not be matched to any call by phone number —
-- surfaced per PRD Risks section rather than silently dropped.
CREATE VIEW IF NOT EXISTS v_unmatched_orders AS
SELECT o.*
FROM shopify_orders o
LEFT JOIN callyzer_calls c
    ON o.customer_phone_norm = c.customer_number_norm
WHERE c.call_id IS NULL;
