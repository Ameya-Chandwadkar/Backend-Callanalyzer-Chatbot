# Payroll / Combined Audit Report

Generates a report matching the structure of `MasonMart_Combined_Audit_Salary_Jun2026.xlsx`
(the June 2026 manual audit) automatically from the same database everything
else in this project uses — no separate system, no re-keying data.

## What works today, with zero extra input

Run it right now and you get a real, correct performance snapshot (outgoing
attempts, connected calls >45s, talk time, active days) for every rep in
`payroll_config.json`, for whatever date range you ask for (or the full
range of data on file, clearly labeled, if you don't specify one):

```
python payroll/generate_report.py
```

Output lands in `payroll/output/`.

## What's gated behind real input, and why

The generator refuses to guess. Every number that depends on something not
yet provided prints as `NOT AVAILABLE — <exact reason>` instead of a 0 or a
blank — a silent wrong number is worse than an honest gap. Three things are
gating full output right now:

### 1. `valid_call_definition` — a genuine unresolved ambiguity

The June audit itself flagged this as unconfirmed: part-time call-based pay
is either `₹5 × connected calls (>45s)` or `₹5 × any outgoing attempt` —
roughly a 6x difference. Set `valid_call_definition` in `payroll_config.json`
to `"connected_45s"` or `"any_outgoing_attempt"` once you've confirmed which
one the contract actually means.

### 2. Never Attended Report — not yet ingested

Drop a Callyzer "Never Attended Report" export into `payroll/incoming/` and run:

```
python payroll/ingest_never_attended.py
```

**This parser's column mapping is an educated guess**, not a verified
format (see the big warning at the top of `ingest_never_attended.py`) — no
real sample of this export has been seen yet. It will refuse to ingest and
print exactly what headers it saw if they don't match; that's expected on
the first real file. Fix the `HEADER_ALIASES` dict in that script to match,
then re-run.

Once ingested, callback recovery ("missed call, but a later outgoing call
to the same number exists") is computed automatically via `v_never_attended_final`
— it's a live join against `callyzer_calls`, so it can never go stale.

### 3. Customer → salesperson mapping — manual for now

`shopify_orders.rep_attribution` is 0% populated (checked directly) — there
is no live Shopify field to pull this from yet. Edit
`payroll/customer_salesperson_map.csv` (delete the EXAMPLE row, add real
rows: customer name, phone, salesperson), then:

```
python payroll/ingest_customer_map.py
```

This is deliberately a *different* source from the chat's `v_order_attribution`
view (which infers attribution from call recency, for the dashboard/chat
use case). Payroll order incentives pay on account *ownership*, not on
"whoever called most recently" — don't merge these two concepts even though
they sound similar.

## Files

| File | Purpose |
|---|---|
| `payroll_config.json` | Contract terms — the only place salary numbers live. See its `_readme`. |
| `customer_salesperson_map.csv` | Manual customer-ownership mapping (input) |
| `ingest_customer_map.py` | Loads the CSV above into the database |
| `ingest_never_attended.py` | Parses the Never Attended Report (format unverified — see warning) |
| `generate_report.py` | Builds the .xlsx from everything above |
| `incoming/` | Drop zone for the Never Attended Report export |
| `output/` | Generated reports land here |

## What's not built yet

Gold Lead 30-minute response tracking, first-call/morning/hourly discipline
rules — all computable from data we already have (`callyzer_calls` timestamps,
`callyzer_leads.created_date`), just not coded yet. Worth doing once the
three gaps above are closed and there's a real report to validate the
discipline math against.
