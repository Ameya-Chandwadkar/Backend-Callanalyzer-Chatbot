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
    connected            INTEGER,            -- 1 if duration > 0s, else 0
    rep_name             TEXT,
    rep_sim_number       TEXT,               -- company SIM, most reliable rep key
    customer_number_raw  TEXT,
    customer_number_norm TEXT,               -- normalized 10-digit mobile, NULL for landlines
    call_uid             TEXT,               -- Callyzer's own per-call UniqueId; authoritative de-dup key
    source_file          TEXT NOT NULL,
    row_hash             TEXT NOT NULL UNIQUE, -- de-dup guard = call_uid when present, else composite hash
    ingested_at          TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_calls_customer ON callyzer_calls(customer_number_norm);
CREATE INDEX IF NOT EXISTS idx_calls_uid ON callyzer_calls(call_uid);
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
-- REP DIRECTORY — canonical identity for name-variant reps
-- ─────────────────────────────────────────────

-- rep_sim_number (the company SIM) is the one stable identity Callyzer
-- gives us; rep_name drifts ("sara" / "Sara" / "Sara K"). One row per
-- sim number, holding the display name to use everywhere.
CREATE TABLE IF NOT EXISTS reps (
    rep_sim_number  TEXT PRIMARY KEY,
    canonical_name  TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

-- Every distinct name spelling ever seen for a rep, mapped to their
-- canonical name — lets name-only sources (leads.assigned_to, Shopify
-- rep_attribution) resolve to the same identity as sim-number-backed
-- call data. alias_key is lower(trim(name)) for case/whitespace-insensitive
-- lookup. Rebuilt by common.rebuild_rep_directory() after every Callyzer ingest.
CREATE TABLE IF NOT EXISTS rep_name_aliases (
    alias_key       TEXT PRIMARY KEY,
    rep_sim_number  TEXT REFERENCES reps(rep_sim_number),
    canonical_name  TEXT NOT NULL
);

-- ─────────────────────────────────────────────
-- REP TARGETS — for progress tracking on the dashboard
-- ─────────────────────────────────────────────

-- One row per rep. Nullable columns mean "no target set" (dashboard
-- shows '—' rather than a misleading 0%). Set via the dashboard's
-- inline target editor (POST /set-target in chat_web.py).
CREATE TABLE IF NOT EXISTS rep_targets (
    rep_sim_number        TEXT PRIMARY KEY REFERENCES reps(rep_sim_number),
    daily_call_target     INTEGER,
    weekly_revenue_target REAL,
    updated_at            TEXT NOT NULL
);

-- Every question asked through chat_query.py / chat_web.py, with the SQL
-- used and the answer given. This is what lets the assistant understand
-- follow-up questions ("and what about Suman?") and gives you a
-- permanent log of what's been asked, independent of any one browser
-- session or server restart.
CREATE TABLE IF NOT EXISTS chat_history (
    turn_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    question      TEXT NOT NULL,
    sql_used       TEXT,
    answer          TEXT,
    asked_at         TEXT NOT NULL
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
    AND c.customer_number_norm IS NOT NULL
WHERE c.call_id IS NULL;

-- ─────────────────────────────────────────────
-- DERIVED VIEW — lead-to-order attribution
-- ─────────────────────────────────────────────

-- Credits each order to whichever rep placed the most recent OUTGOING
-- call to that customer's number in the ATTRIBUTION_WINDOW_DAYS days
-- before the order — the call most plausibly responsible for the sale.
-- If multiple reps called in that window, only the latest call counts;
-- if no call qualifies, the order is unattributed (attributed_rep_name
-- IS NULL) rather than guessed at.
--
-- Both timestamps are converted to IST before comparing: call_timestamp
-- is already IST, shopify_orders.created_at is UTC ('...Z'). Getting this
-- wrong was the exact bug fixed in chat_query.py's schema notes — see
-- that file's shopify_orders TABLE_NOTES entry.
--
-- Window is 7 days, matching the dashboard's existing 7-day reporting
-- period. Change ATTRIBUTION_WINDOW_DAYS below (both places) if the
-- business wants a different cutoff.
CREATE VIEW IF NOT EXISTS v_order_attribution AS
SELECT order_id, order_number, created_at, total_price, customer_phone_norm,
       attributing_call_id, attributing_call_timestamp,
       attributed_rep_sim, attributed_rep_name, days_between_call_and_order
FROM (
    SELECT
        o.order_id, o.order_number, o.created_at, o.total_price, o.customer_phone_norm,
        c.call_id AS attributing_call_id,
        c.call_timestamp AS attributing_call_timestamp,
        c.rep_sim_number AS attributed_rep_sim,
        COALESCE(r.canonical_name, c.rep_name) AS attributed_rep_name,
        ROUND(julianday(datetime(o.created_at, 'localtime')) - julianday(c.call_timestamp), 2)
            AS days_between_call_and_order,
        ROW_NUMBER() OVER (
            PARTITION BY o.order_id ORDER BY c.call_timestamp DESC
        ) AS rn
    FROM shopify_orders o
    LEFT JOIN callyzer_calls c
        ON c.customer_number_norm = o.customer_phone_norm
        AND c.customer_number_norm IS NOT NULL
        AND c.direction = 'outgoing'
        AND datetime(c.call_timestamp) <= datetime(o.created_at, 'localtime')
        AND datetime(c.call_timestamp) >= datetime(o.created_at, 'localtime', '-7 days')
    LEFT JOIN reps r ON r.rep_sim_number = c.rep_sim_number
) ranked
WHERE rn = 1;
