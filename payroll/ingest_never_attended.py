"""
ingest_never_attended.py
Loads Callyzer's "Never Attended Report" export (missed calls) into
callyzer_never_attended.

**STATUS: UNVERIFIED FORMAT.** As of 2026-07-14 no real sample of this
export has been seen — this project only has confirmed formats for
"Periodic Call History Report" and "Lead Data Report" (see
ingest_callyzer.py). The header aliases below are a best-effort guess
based on Callyzer's Periodic Call History column vocabulary (same
platform, same export family), NOT a verified format.

This script deliberately REFUSES to ingest if it can't confidently match
the required columns — it prints exactly what headers it saw so the
alias list can be corrected against a real file, rather than silently
mis-mapping columns into a payroll calculation. Getting this wrong would
mean a real employee's "Never Attended" count (which feeds directly into
their evaluation) is silently wrong — the exact failure mode this whole
project has been hardened against all session. Do not relax the "refuse
to guess" behavior below to make a real file "just work" without
checking the mapping is actually correct.

USAGE:
    python payroll/ingest_never_attended.py incoming/Never_Attended_Report_....csv
    python payroll/ingest_never_attended.py payroll/incoming/*.csv
"""

import csv
import os
import sys
import glob
import shutil

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common import get_connection, normalize_phone, row_hash, now_iso, \
    start_log, finish_log, flag_row
from ingest_callyzer import _parse_timestamp, _build_timestamp_raw

INCOMING_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "incoming")
PROCESSED_DIR = os.path.join(INCOMING_DIR, "processed")

# Best-effort guess — see module docstring. Required = must be present to
# proceed at all; without all of these, the export can't be safely parsed.
REQUIRED_FIELDS = ["timestamp_or_date", "rep_sim", "customer_number"]

HEADER_ALIASES = {
    "timestamp": ["call date & time", "date & time", "timestamp"],
    "call_date": ["call date"],
    "call_time": ["call time"],
    "rep_name": ["employee name", "employee", "agent", "rep name"],
    "rep_sim": ["employee number", "sim number", "sim", "agent number"],
    "customer_number": ["to number", "customer number", "phone number", "contact number"],
    "call_uid": ["uniqueid", "unique id", "call id", "callid", "call unique id"],
}


def _match_headers(fieldnames):
    lower_map = {fn.strip().lower(): fn for fn in fieldnames}
    resolved = {}
    for logical, aliases in HEADER_ALIASES.items():
        for alias in aliases:
            if alias in lower_map:
                resolved[logical] = lower_map[alias]
                break
    return resolved


def ingest_never_attended(conn, path, reader, headers):
    log_id = start_log(conn, "callyzer_never_attended", os.path.basename(path))
    read = inserted = flagged = duplicate = 0

    for raw_row in reader:
        read += 1
        ts = _parse_timestamp(_build_timestamp_raw(raw_row, headers))
        cust_raw = raw_row.get(headers.get("customer_number"), "")
        cust_norm = normalize_phone(cust_raw)
        rep_name = (raw_row.get(headers.get("rep_name"), "") or "").strip()
        rep_sim = (raw_row.get(headers.get("rep_sim"), "") or "").strip()
        call_uid = (raw_row.get(headers.get("call_uid"), "") or "").strip()

        if ts is None:
            flagged += 1
            flag_row(conn, log_id, "unparseable timestamp", raw_row)
            continue

        h = call_uid if call_uid else row_hash(ts, cust_norm, rep_sim, "never_attended")
        existing = conn.execute(
            "SELECT missed_id FROM callyzer_never_attended WHERE row_hash = ?", (h,)
        ).fetchone()
        if existing:
            duplicate += 1
            continue

        try:
            conn.execute(
                """INSERT INTO callyzer_never_attended
                   (call_timestamp, rep_name, rep_sim_number, customer_number_raw,
                    customer_number_norm, call_uid, source_file, row_hash, ingested_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (ts, rep_name, rep_sim, cust_raw, cust_norm, call_uid or None,
                 os.path.basename(path), h, now_iso()),
            )
            inserted += 1
        except Exception:
            duplicate += 1

    conn.commit()
    finish_log(conn, log_id, read, inserted, 0, flagged, duplicate)
    return read, inserted, flagged, duplicate


def process_file(conn, path):
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            print(f"  SKIP {path}: empty or unreadable file")
            return False

        headers = _match_headers(reader.fieldnames)
        has_timestamp = "timestamp" in headers or ("call_date" in headers and "call_time" in headers)
        missing = []
        if not has_timestamp:
            missing.append("a date/time column")
        if "rep_sim" not in headers:
            missing.append("an employee/SIM number column")
        if "customer_number" not in headers:
            missing.append("a customer/to-number column")

        if missing:
            print(f"  REFUSING TO GUESS on {os.path.basename(path)}: could not confidently find "
                  f"{', '.join(missing)}.")
            print(f"  Headers actually seen: {reader.fieldnames}")
            print(f"  Fix HEADER_ALIASES in payroll/ingest_never_attended.py to match, then re-run. "
                  f"Nothing was ingested from this file.")
            return False

        read, ins, flg, dup = ingest_never_attended(conn, path, reader, headers)
        print(f"  NEVER-ATTENDED  {os.path.basename(path)}: read={read} inserted={ins} "
              f"duplicate={dup} flagged={flg}")

    os.makedirs(PROCESSED_DIR, exist_ok=True)
    shutil.move(path, os.path.join(PROCESSED_DIR, os.path.basename(path)))
    return True


def main():
    os.makedirs(INCOMING_DIR, exist_ok=True)
    args = sys.argv[1:]
    if args:
        paths = []
        for a in args:
            paths.extend(glob.glob(a))
    else:
        paths = glob.glob(os.path.join(INCOMING_DIR, "*.csv"))

    if not paths:
        print(f"No CSV files found. Drop the Never Attended Report export into: {INCOMING_DIR}")
        return

    conn = get_connection()
    print(f"Processing {len(paths)} file(s)...")
    any_ok = False
    for path in paths:
        if process_file(conn, path):
            any_ok = True
    conn.close()
    print("Done." if any_ok else "Nothing was ingested — see warnings above.")


if __name__ == "__main__":
    main()
