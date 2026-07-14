"""
ingest_customer_map.py
Loads a customer_salesperson_map source into the customer_salesperson_map
table — the "who owns this customer account" mapping payroll order-incentive
attribution needs, separate from call-based inference (v_order_attribution).

WHY THIS EXISTS AS A MANUAL/DERIVED SOURCE, NOT A SHOPIFY PULL:
The June audit this mirrors used a "customer export" that mapped Shopify
customers to salespeople, but as of 2026-07-14 no Shopify customer in this
store has a populated salesperson tag/metafield (checked: 0 of 264 orders
have rep_attribution set). If a live Shopify field is confirmed later,
replace this script's data source — customer_salesperson_map's shape
doesn't need to change, only how it's filled.

TWO ACCEPTED INPUT FORMATS (auto-detected by column headers):

1. MANUAL — columns: Customer Name, Customer Phone, Salesperson.
   Hand-built, highest confidence — someone explicitly said "this customer
   belongs to this rep." Stored with source='manual_csv'.

2. LEAD EXPORT — Callyzer's native Lead Data Report format (Contact Number,
   Assign To, Company Name/Lead Name, Assigned Date, ...). Confirmed
   2026-07-14: this is real, populated data (Assign To has a value on every
   row of a 4,085-row sample). BUT it is a caveat source, not a verified
   one: it says who a LEAD was assigned to, not who actually closed that
   customer — MasonMart's own export shows most rows still mid-pipeline
   ("Ringing", "Cold Lead", "Remove"), not confirmed conversions. Where the
   same phone number was assigned to more than one rep across the file
   (seen in ~1.2% of a real sample), the most recent Assigned Date wins,
   and the conflict is reported. Stored with source='lead_assignment' so
   it's never confused with a verified manual mapping in the UI or report.

USAGE:
    python payroll/ingest_customer_map.py [path/to/file.csv]
    (defaults to payroll/customer_salesperson_map.csv if no path given;
    accepts either format above)
"""

import csv
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common import get_connection, normalize_phone, canonical_rep_name, now_iso

DEFAULT_CSV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "customer_salesperson_map.csv")

MANUAL_COLUMNS = ["Customer Name", "Customer Phone", "Salesperson"]
LEAD_EXPORT_REQUIRED = ["Contact Number", "Assign To"]


def _detect_format(fieldnames):
    fieldnames = set(fieldnames or [])
    if all(c in fieldnames for c in MANUAL_COLUMNS):
        return "manual"
    if all(c in fieldnames for c in LEAD_EXPORT_REQUIRED):
        return "lead_export"
    return None


def _parse_assigned_date(raw):
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%d %b %Y, %I:%M %p")
    except ValueError:
        return None


def _resolve_rep_and_store(conn, phone_norm, rep_raw, source, warnings):
    """Shared tail end of both format handlers: canonicalize the rep name,
    look up their sim number, upsert the mapping.
    Returns (written: bool, rep_sim_missing: bool)."""
    canonical = canonical_rep_name(conn, rep_raw)
    if not canonical:
        return False, False

    rep_row = conn.execute(
        "SELECT rep_sim_number FROM reps WHERE canonical_name = ?", (canonical,)
    ).fetchone()
    rep_sim = rep_row["rep_sim_number"] if rep_row else None
    rep_sim_missing = rep_sim is None
    if rep_sim_missing:
        warnings.append(f"WARNING '{phone_norm}': salesperson '{rep_raw}' -> '{canonical}' has no "
                         f"call history yet (not in reps table). Stored anyway.")

    conn.execute(
        """INSERT INTO customer_salesperson_map
           (customer_phone_norm, rep_sim_number, canonical_name, source, mapped_at)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(customer_phone_norm) DO UPDATE SET
             rep_sim_number=excluded.rep_sim_number,
             canonical_name=excluded.canonical_name,
             source=excluded.source,
             mapped_at=excluded.mapped_at""",
        (phone_norm, rep_sim, canonical, source, now_iso()),
    )
    return True, rep_sim_missing


def _ingest_manual(conn, reader):
    mapped = skipped_example = unresolved_phone = unresolved_rep_count = 0
    warnings = []
    for row in reader:
        name = (row.get("Customer Name") or "").strip()
        phone_raw = (row.get("Customer Phone") or "").strip()
        rep_raw = (row.get("Salesperson") or "").strip()

        if name.upper().startswith("EXAMPLE"):
            skipped_example += 1
            continue

        phone_norm = normalize_phone(phone_raw)
        if phone_norm is None:
            warnings.append(f"SKIP '{name}': phone '{phone_raw}' isn't a resolvable mobile number.")
            unresolved_phone += 1
            continue

        if not rep_raw:
            warnings.append(f"SKIP '{name}': no salesperson given.")
            continue

        written, rep_sim_missing = _resolve_rep_and_store(conn, phone_norm, rep_raw, "manual_csv", warnings)
        if written:
            mapped += 1
            if rep_sim_missing:
                unresolved_rep_count += 1

    return {
        "mapped": mapped, "skipped_example": skipped_example,
        "unresolved_phone": unresolved_phone, "unresolved_rep": unresolved_rep_count,
        "warnings": warnings, "conflicts_resolved": 0, "source": "manual_csv",
    }


def _ingest_lead_export(conn, reader):
    """Buffers all rows first (need the full file to resolve phones assigned
    to more than one rep at different times — most recent Assigned Date
    wins, consistent with 'who owns this customer right now')."""
    rows = list(reader)
    unresolved_phone = 0
    best_by_phone = {}   # phone_norm -> (parsed_date_or_None, row_index, rep_raw, name)
    conflicts = set()

    for idx, row in enumerate(rows):
        phone_raw = (row.get("Contact Number") or "").strip()
        phone_norm = normalize_phone(phone_raw)
        if phone_norm is None:
            unresolved_phone += 1
            continue

        rep_raw = (row.get("Assign To") or "").strip()
        if not rep_raw:
            continue

        company = (row.get("Company Name") or "").strip()
        lead_name = (row.get("Lead Name") or "").strip()
        name = company if company and company != "." else (lead_name if lead_name and lead_name != "." else phone_norm)

        assigned_dt = _parse_assigned_date(row.get("Assigned Date"))

        if phone_norm in best_by_phone:
            prev_dt, prev_idx, prev_rep, _ = best_by_phone[phone_norm]
            if canonical_rep_name(conn, rep_raw) != canonical_rep_name(conn, prev_rep):
                conflicts.add(phone_norm)
            # Prefer a parsed date over none; among parsed dates, latest wins;
            # among unparsed, last row in file wins (best-effort, not a guess
            # dressed as certainty — see conflicts_resolved in the returned stats).
            better = (
                (assigned_dt is not None and (prev_dt is None or assigned_dt >= prev_dt))
                or (assigned_dt is None and prev_dt is None)
            )
            if not better:
                continue

        best_by_phone[phone_norm] = (assigned_dt, idx, rep_raw, name)

    mapped = unresolved_rep_count = 0
    warnings = []
    for phone_norm, (_, _, rep_raw, name) in best_by_phone.items():
        written, rep_sim_missing = _resolve_rep_and_store(conn, phone_norm, rep_raw, "lead_assignment", warnings)
        if written:
            mapped += 1
            if rep_sim_missing:
                unresolved_rep_count += 1

    if conflicts:
        warnings.append(f"{len(conflicts)} phone number(s) were assigned to more than one rep across the "
                         f"file — kept the most recent Assigned Date for each.")

    return {
        "mapped": mapped, "skipped_example": 0,
        "unresolved_phone": unresolved_phone, "unresolved_rep": unresolved_rep_count,
        "warnings": warnings, "conflicts_resolved": len(conflicts), "source": "lead_assignment",
    }


def ingest_customer_map(conn, csv_path):
    """Core ingest logic, reusable from both the CLI and the web upload
    endpoint. Returns a stats dict rather than printing, so callers can
    format it however they need (console text vs. JSON for the UI).

    Refuses to process a file matching NEITHER accepted format instead of
    silently treating every row as unresolvable — the exact failure mode
    that happened when a Lead Data Report was first dropped into this slot:
    it "succeeded" with 0 mapped and thousands of buried warnings, which
    looks like nothing happened rather than clearly saying what's wrong."""
    with open(csv_path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        fmt = _detect_format(reader.fieldnames)
        if fmt is None:
            return {
                "mapped": 0, "skipped_example": 0, "unresolved_phone": 0, "unresolved_rep": 0,
                "warnings": [], "format_error": True, "conflicts_resolved": 0,
                "message": (f"This doesn't match either accepted format. Headers found: "
                            f"{sorted(reader.fieldnames or [])}. Expected either "
                            f"[{', '.join(MANUAL_COLUMNS)}] (manual mapping) or a Callyzer Lead Data "
                            f"Report export (needs at least {', '.join(LEAD_EXPORT_REQUIRED)})."),
            }
        result = _ingest_manual(conn, reader) if fmt == "manual" else _ingest_lead_export(conn, reader)

    conn.commit()
    result["format_error"] = False
    return result


def main():
    csv_path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_CSV_PATH
    if not os.path.exists(csv_path):
        print(f"ERROR: {csv_path} not found.")
        sys.exit(1)

    conn = get_connection()
    stats = ingest_customer_map(conn, csv_path)
    conn.close()

    if stats.get("format_error"):
        print(f"REFUSING TO GUESS: {stats['message']}")
        sys.exit(1)

    for w in stats["warnings"]:
        print(f"  {w}")
    print(f"\n[{stats['source']}] Mapped {stats['mapped']} customer(s). "
          f"Skipped {stats['skipped_example']} example row(s), "
          f"{stats['unresolved_phone']} unresolvable phone(s), {stats['unresolved_rep']} rep(s) "
          f"with no call history yet, {stats['conflicts_resolved']} multi-rep conflict(s) resolved "
          f"by most-recent-assignment.")


if __name__ == "__main__":
    main()
