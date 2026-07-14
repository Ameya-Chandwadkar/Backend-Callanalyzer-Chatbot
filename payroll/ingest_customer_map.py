"""
ingest_customer_map.py
Loads a customer_salesperson_map CSV into the customer_salesperson_map
table — the "who owns this customer account" mapping payroll order-incentive
attribution needs, separate from call-based inference (v_order_attribution).

WHY THIS EXISTS AS A MANUAL CSV, NOT A SHOPIFY PULL:
The June audit this mirrors used a "customer export" that mapped Shopify
customers to salespeople, but as of 2026-07-14 no Shopify customer in this
store has a populated salesperson tag/metafield (checked: 0 of 264 orders
have rep_attribution set). Until that's confirmed as a real, live field,
maintain the mapping here by hand. If a live Shopify field is confirmed
later, replace this script's data source — customer_salesperson_map's
shape doesn't need to change, only how it's filled.

USAGE:
    Edit payroll/customer_salesperson_map.csv (Customer Name, Customer Phone,
    Salesperson columns — Salesperson can be any spelling already known to
    the rep directory; run rebuild_rep_directory.py first if it's a brand
    new rep with no call history yet).

    python payroll/ingest_customer_map.py [path/to/file.csv]
    (defaults to payroll/customer_salesperson_map.csv if no path given)
"""

import csv
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common import get_connection, normalize_phone, canonical_rep_name, now_iso

DEFAULT_CSV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "customer_salesperson_map.csv")


def ingest_customer_map(conn, csv_path):
    """Core ingest logic, reusable from both the CLI and the web upload
    endpoint. Returns a stats dict rather than printing, so callers can
    format it however they need (console text vs. JSON for the UI)."""
    mapped = skipped_example = unresolved_phone = unresolved_rep = 0
    warnings = []

    with open(csv_path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
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

            canonical = canonical_rep_name(conn, rep_raw)
            if not canonical:
                warnings.append(f"SKIP '{name}': no salesperson given.")
                continue

            rep_row = conn.execute(
                "SELECT rep_sim_number FROM reps WHERE canonical_name = ?", (canonical,)
            ).fetchone()
            rep_sim = rep_row["rep_sim_number"] if rep_row else None
            if rep_sim is None:
                warnings.append(f"WARNING '{name}': salesperson '{rep_raw}' -> '{canonical}' has no "
                                 f"call history yet (not in reps table). Stored anyway.")
                unresolved_rep += 1

            conn.execute(
                """INSERT INTO customer_salesperson_map
                   (customer_phone_norm, rep_sim_number, canonical_name, source, mapped_at)
                   VALUES (?, ?, ?, 'manual_csv', ?)
                   ON CONFLICT(customer_phone_norm) DO UPDATE SET
                     rep_sim_number=excluded.rep_sim_number,
                     canonical_name=excluded.canonical_name,
                     source=excluded.source,
                     mapped_at=excluded.mapped_at""",
                (phone_norm, rep_sim, canonical, now_iso()),
            )
            mapped += 1

    conn.commit()
    return {
        "mapped": mapped, "skipped_example": skipped_example,
        "unresolved_phone": unresolved_phone, "unresolved_rep": unresolved_rep,
        "warnings": warnings,
    }


def main():
    csv_path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_CSV_PATH
    if not os.path.exists(csv_path):
        print(f"ERROR: {csv_path} not found.")
        sys.exit(1)

    conn = get_connection()
    stats = ingest_customer_map(conn, csv_path)
    conn.close()

    for w in stats["warnings"]:
        print(f"  {w}")
    print(f"\nMapped {stats['mapped']} customer(s). Skipped {stats['skipped_example']} example row(s), "
          f"{stats['unresolved_phone']} unresolvable phone(s), {stats['unresolved_rep']} rep(s) "
          f"with no call history yet.")


if __name__ == "__main__":
    main()
