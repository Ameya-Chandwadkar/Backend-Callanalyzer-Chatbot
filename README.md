# MasonMart Callyzer + Shopify Integration
Implementation of the PRD, built as three independent, testable phases.

## What this actually is

Three small Python scripts and one SQLite database file:

```
masonmart_integration/
├── schema.sql          the unified data store's table definitions
├── common.py            shared helpers (phone normalization, DB connection, logging)
├── ingest_callyzer.py    Phase 1 — loads Callyzer CSV exports into the store
├── ingest_shopify.py      Phase 2 — pulls Shopify orders via API into the store
├── chat_query.py            Phase 3 — natural-language questions over the store
├── requirements.txt
├── .env.example
└── masonmart.sqlite          (created automatically the first time you run anything)
```

No cloud hosting, no server, no monthly infrastructure cost. Everything
runs as local scripts on your machine (same pattern as your existing
`clarity_report.py` setup), scheduled via Windows Task Scheduler. The only
running cost is the Anthropic API calls made by `chat_query.py` when you
ask it a question — a few cents each.

## Why this design, in plain terms

The PRD's core idea is: **stop cross-referencing exports by hand.**
Instead, both systems' data lands in one small database automatically,
and you ask it questions in plain English instead of opening spreadsheets.

The three files map directly to Section 7 (Rollout Plan) of the PRD:

| Phase | File | What it proves works before moving on |
|---|---|---|
| 1 | `ingest_callyzer.py` | Callyzer data loads cleanly and matches a known manual export |
| 2 | `ingest_shopify.py` | Shopify orders load and join to calls by phone number |
| 3 | `chat_query.py` | You can ask questions and get numbers that trace back to real rows |

You can run Phase 1 alone for a week to build trust in the data before
touching Phase 2 or 3 — nothing downstream needs to exist yet.

## Setup — step by step

### 1. Install Python (if not already installed)
Download from python.org, version 3.10 or later. During install, tick
"Add Python to PATH."

### 2. Get the files onto your machine
Copy this whole `masonmart_integration` folder to somewhere permanent —
e.g. `D:\masonmart-integration` (matching where your Clarity scripts
already live).

### 3. Install the one dependency
Open Command Prompt in that folder and run:
```
pip install -r requirements.txt
```
This installs `requests`, the only library used beyond what Python
ships with. (SQLite support is built into Python — no separate install.)

### 4. Set up your credentials
Copy `.env.example` to a new file named `.env` in the same folder, then
fill in the real values. Two separate credentials are needed, for two
separate scripts:

#### Shopify (needed for `ingest_shopify.py`)
1. In your Shopify admin (masonmart.in backend), go to
   **Settings → Apps and sales channels → Develop apps**.
2. Click **Create an app**, name it something like "MasonMart Data Sync."
3. Under **Configuration → Admin API scopes**, enable:
   - `read_orders`
   - `read_customers`
4. Click **Install app**, then go to the **API credentials** tab and
   reveal the **Admin API access token**. This is a one-time reveal —
   copy it immediately into `.env` as `SHOPIFY_ACCESS_TOKEN`.
5. `SHOPIFY_STORE_DOMAIN` is your `*.myshopify.com` domain, found on the
   same settings page (not your custom masonmart.in domain).

#### Anthropic API key (needed only for `chat_query.py`)
1. Go to console.anthropic.com → API Keys → Create Key.
2. Copy it into `.env` as `ANTHROPIC_API_KEY`.
3. This is billed separately from your claude.ai subscription — it's
   pay-per-use, typically a few rupees per week at this volume.

### 5. Phase 1 — Callyzer ingestion (start here)
No credentials needed for this phase — it works from files you already
export.

1. Create a folder named `incoming` inside `masonmart_integration`.
2. Export from Callyzer exactly as you do today: **Periodic Call History
   Report** and **Lead Data Report**, as CSV.
3. Drop both files into `incoming/`.
4. Run:
   ```
   python ingest_callyzer.py
   ```
5. You'll see a line per file showing rows read, inserted, duplicates,
   and flagged. Processed files move to `incoming/processed/` so
   re-running the script never double-counts them.
6. **Validate it** (per PRD Phase 1 note): pick a day you already
   audited manually, and compare a count from the database against
   your manual numbers:
   ```
   python -c "from common import get_connection; c = get_connection(); print(c.execute(\"SELECT rep_name, COUNT(*) FROM callyzer_calls WHERE date(call_timestamp)='2026-07-08' GROUP BY rep_name\").fetchall())"
   ```

Once this matches your manual audit for a known day, Phase 1 is trustworthy.

### 6. Phase 2 — Shopify ingestion
1. With `.env` filled in (step 4), run:
   ```
   python ingest_shopify.py --full
   ```
   The `--full` flag is only for the very first run, to pull all
   historical orders. Every run after that, just use:
   ```
   python ingest_shopify.py
   ```
   which only pulls orders updated since the last successful run.
2. Check the join works:
   ```
   python -c "from common import get_connection; c = get_connection(); print(c.execute('SELECT COUNT(*) FROM v_calls_with_orders WHERE order_id IS NOT NULL').fetchone())"
   ```
3. Check for unmatched orders (expected — not every order will have a
   matching call, per PRD Risks):
   ```
   python -c "from common import get_connection; c = get_connection(); print(c.execute('SELECT COUNT(*) FROM v_unmatched_orders').fetchone())"
   ```

**On order-to-rep attribution:** the script looks for a `rep:sara`-style
order tag or a `rep`/`salesperson` note attribute. If MasonMart doesn't
currently tag orders that way, `rep_attribution` will come back empty —
that's expected and matches the PRD's position that attribution logic
is defined later, not baked into the integration layer. The join by
phone number (`v_calls_with_orders`) works regardless.

### 7. Phase 3 — the chat interface
1. Run one-off questions:
   ```
   python chat_query.py "how many calls did Sara make yesterday?"
   python chat_query.py "which IndiaMart leads have not been called in 3 or more days?"
   python chat_query.py "how many Shopify orders in the last 7 days have no matching call?"
   ```
2. Or run it interactively:
   ```
   python chat_query.py
   ```
   and type questions one after another until you type `quit`.

**Why answers are trustworthy, not guessed:** the model never invents a
number. It writes a SQL query, the query actually runs against your
real data, and only the query's real output gets turned into a
sentence. If you ask something the schema can't answer (e.g. call
recordings, sentiment), it will tell you that plainly instead of making
something up.

### 8. Automate it — Windows Task Scheduler
Once Phases 1 and 2 are validated, put them on a schedule so ingestion
happens without you running commands by hand.

1. Open **Task Scheduler** → **Create Basic Task**.
2. Name: "MasonMart Callyzer Ingest." Trigger: **Daily**, pick a time
   after your Callyzer export habit (e.g. 8 PM).
3. Action: **Start a program**.
   - Program: `python`
   - Arguments: `ingest_callyzer.py`
   - Start in: the full path to `masonmart_integration`
4. Repeat for "MasonMart Shopify Ingest" with arguments `ingest_shopify.py`.

Callyzer ingestion still needs you to drop the CSV export into
`incoming/` before the scheduled run — that's the "semi-automated bridge
step" the PRD anticipated for Phase 1, given API access isn't confirmed.
Shopify ingestion needs no manual step at all once scheduled.

## Data validation, as required by PRD Section 5.5

Every run writes to `ingestion_log` (counts) and `ingestion_flags`
(specific rows that didn't load, with a reason). To see what's been
flagged recently:
```
python -c "from common import get_connection; c = get_connection(); [print(dict(r)) for r in c.execute('SELECT * FROM ingestion_flags ORDER BY flag_id DESC LIMIT 20')]"
```
Nothing is silently dropped — malformed rows are skipped from the
tables but kept visible here.

## Answering the PRD's open questions (Section 9)

1. **Does Callyzer expose an API?** Not confirmed. This build uses the
   CSV-bridge approach for Phase 1. If you later confirm API access,
   only `ingest_callyzer.py` needs rewriting to call it instead of
   reading files — `schema.sql`, `ingest_shopify.py`, and
   `chat_query.py` don't change at all.
2. **Where should the chat live?** As built, it's a command-line script
   you run from your machine. If you want it reachable from your phone
   or WhatsApp later, that's a thin layer on top of `chat_query.py`'s
   `ask()` function — the grounded query logic doesn't need to change.
3. **Data to exclude for privacy?** Nothing is excluded by default —
   customer phone numbers and names flow into the store as-is, since
   that's needed for the join. If there's specific data you want kept
   out (e.g. certain customer categories), that's a filter to add in
   `ingest_shopify.py` before it's stored, not after.

## What's deliberately not built here

Per PRD Out of Scope: no compliance rules, no scoring formulas, no
fixed report templates, no alerting. Those get built as prompts against
`chat_query.py`, or as new scripts reading from the same
`masonmart.sqlite` — the foundation is meant to be reused, not rebuilt,
every time a rule changes (which your compliance rules have done
multiple times already between June and July).
