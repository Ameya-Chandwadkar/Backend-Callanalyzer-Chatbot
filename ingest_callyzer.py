"""
ingest_callyzer.py
Phase 1 of the PRD: automated ingestion of Callyzer data.

WHY CSV, NOT API:
Callyzer's API access for this account hasn't been confirmed. Every audit
done so far has used manually exported CSVs (Periodic Call History Report,
Lead Data Report). This script builds Phase 1 around that reality — per
PRD Risks & Assumptions: "if only manual CSV export exists, ingestion
will need a semi-automated bridge step." That bridge is: you export the
CSV the same way you already do, drop it in the `incoming/` folder, and
this script does the rest. If Callyzer API access is confirmed later,
only this file changes — the schema, joins, and chat layer don't.

USAGE:
    python ingest_callyzer.py incoming/Periodic_Call_History_2026-07-08.csv
    python ingest_callyzer.py incoming/Lead_Data_Report_2026-07-08.csv
    python ingest_callyzer.py incoming/*.csv          (process everything waiting)

The script auto-detects whether a file is a call-history export or a
lead-data export by inspecting its column headers, so you don't need to
tell it which is which.

After a successful ingest, the source file is moved to incoming/processed/
so re-running the script doesn't double-count it.
"""

import csv
import os
import sys
import glob
import shutil
from datetime import datetime

from common import get_connection, normalize_phone, row_hash, now_iso, \
    start_log, finish_log, flag_row, SCRIPT_DIR

INCOMING_DIR = os.path.join(SCRIPT_DIR, "incoming")
PROCESSED_DIR = os.path.join(INCOMING_DIR, "processed")

# Header aliases: Callyzer's export column names have drifted before
# (per PRD 5.1: "handle schema or export-format changes without silently
# dropping data"). Add new aliases here as you spot them rather than
# rewriting parsing logic.
CALL_HEADER_ALIASES = {
    "timestamp": ["call date & time", "date & time", "timestamp"],
    "call_date": ["call date"],
    "call_time": ["call time"],
    "direction": ["call type", "direction", "call direction"],
    "duration": ["duration", "call duration", "duration (sec)", "talk time"],
    "rep_name": ["employee name", "employee", "agent", "rep name"],
    "rep_sim": ["employee number", "sim number", "sim", "agent number"],
    "customer_number": ["to number", "customer number", "phone number", "contact number"],
    "call_uid": ["uniqueid", "unique id", "call id", "callid", "call unique id"],
}

LEAD_HEADER_ALIASES = {
    "lead_no": ["lead no", "lead no.", "lead id"],
    "lead_name": ["lead name", "name"],
    "contact_number": ["contact number", "phone number", "mobile"],
    "assigned_to": ["assign to", "assigned to"],
    "tags": ["tags", "tag"],
    "attempts": ["no of attempts", "no. of attempts", "attempts"],
    "last_call_dt": ["last call - date & time", "last call date", "last call datetime"],
    "last_call_note": ["last call - note", "last call note"],
    "status": ["lead status", "status"],
    "created_date": ["created date", "created on"],
}


def _match_headers(fieldnames, alias_map):
    """Map each logical field to the actual CSV column name present."""
    lower_map = {fn.strip().lower(): fn for fn in fieldnames}
    resolved = {}
    for logical, aliases in alias_map.items():
        for alias in aliases:
            if alias in lower_map:
                resolved[logical] = lower_map[alias]
                break
    return resolved


def _detect_file_type(fieldnames):
    call_hit = len(_match_headers(fieldnames, CALL_HEADER_ALIASES))
    lead_hit = len(_match_headers(fieldnames, LEAD_HEADER_ALIASES))
    if lead_hit > call_hit and lead_hit >= 3:
        return "leads"
    if call_hit >= 3:
        return "calls"
    return None


def _parse_timestamp(raw):
    if not raw:
        return None
    formats = [
        "%d-%m-%Y %H:%M:%S", "%d-%m-%Y %H:%M", "%d/%m/%Y %H:%M:%S",
        "%d/%m/%Y %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S",
        "%d %b %Y %I:%M %p",  # e.g. "09 Jul 2026 05:25 PM" — real Callyzer export format
        "%d %B %Y %I:%M %p",
    ]
    for fmt in formats:
        try:
            return datetime.strptime(raw.strip(), fmt).isoformat()
        except ValueError:
            continue
    return None  # unparseable -> caller flags the row


def _parse_duration_seconds(raw):
    if raw is None:
        return 0
    text = str(raw).strip().lower()
    if not text:
        return 0

    try:
        return int(float(text))
    except ValueError:
        pass

    # Formats seen in exports: "0h 0m 47s", "1m 12s", "47s", "00:01:12".
    if ":" in text:
        parts = []
        for part in text.split(":"):
            part = part.strip()
            if not part:
                return 0
            try:
                parts.append(int(float(part)))
            except ValueError:
                return 0
        if len(parts) == 3:
            h, m, s = parts
            return h * 3600 + m * 60 + s
        if len(parts) == 2:
            m, s = parts
            return m * 60 + s
        if len(parts) == 1:
            return parts[0]
        return 0

    import re

    hours = minutes = seconds = 0
    mh = re.search(r"(\d+)\s*h", text)
    mm = re.search(r"(\d+)\s*m", text)
    ms = re.search(r"(\d+)\s*s", text)
    if mh:
        hours = int(mh.group(1))
    if mm:
        minutes = int(mm.group(1))
    if ms:
        seconds = int(ms.group(1))
    total = hours * 3600 + minutes * 60 + seconds
    return total


def _build_timestamp_raw(raw_row, headers):
    """
    Callyzer's export has been seen in two shapes: a single combined
    "Call Date & Time" column, or separate "Call Date" + "Call Time"
    columns (e.g. "09 Jul 2026" + "05:25 PM"). Support both without
    the caller needing to know which shape this particular export used.
    """
    combined_col = headers.get("timestamp")
    if combined_col and raw_row.get(combined_col):
        return raw_row[combined_col]

    date_col = headers.get("call_date")
    time_col = headers.get("call_time")
    if date_col and time_col:
        date_val = (raw_row.get(date_col) or "").strip()
        time_val = (raw_row.get(time_col) or "").strip()
        if date_val and time_val:
            return f"{date_val} {time_val}"
    return ""


def ingest_calls(conn, path, reader, headers):
    log_id = start_log(conn, "callyzer_calls", os.path.basename(path))
    read = inserted = updated = duplicate = flagged = 0

    for raw_row in reader:
        read += 1
        ts = _parse_timestamp(_build_timestamp_raw(raw_row, headers))
        cust_raw = raw_row.get(headers.get("customer_number"), "")
        # A landline (e.g. a Mumbai "22…" number) is a REAL call that was
        # made — it just can't be normalized to a 10-digit mobile, so it
        # won't join to a customer by phone. We still store it (norm=NULL)
        # so call-volume counts stay accurate. Only a genuinely unusable
        # row — one we can't even place in time — is flagged and skipped.
        cust_norm = normalize_phone(cust_raw)

        if ts is None:
            flagged += 1
            flag_row(conn, log_id, "unparseable call timestamp", raw_row)
            continue

        duration_raw = raw_row.get(headers.get("duration"), "0")
        duration = _parse_duration_seconds(duration_raw)

        direction = (raw_row.get(headers.get("direction"), "") or "").strip().lower()
        rep_name = (raw_row.get(headers.get("rep_name"), "") or "").strip()
        rep_sim = (raw_row.get(headers.get("rep_sim"), "") or "").strip()
        call_uid = (raw_row.get(headers.get("call_uid"), "") or "").strip()

        # Callyzer stamps every call with a UniqueId — the authoritative
        # de-dup key. Two calls to the same number in the same minute are
        # distinct rows with distinct UniqueIds, so keying on the uid (not
        # a timestamp-to-the-minute composite) stops us from silently
        # collapsing genuine re-dials. Fall back to a composite hash only
        # for the rare export that lacks a uid.
        h = call_uid if call_uid else row_hash(ts, cust_norm, rep_sim, direction, cust_raw)

        existing = conn.execute(
            "SELECT call_id FROM callyzer_calls WHERE row_hash = ?", (h,)
        ).fetchone()

        if existing:
            conn.execute(
                """UPDATE callyzer_calls
                   SET call_timestamp = ?,
                       direction = ?,
                       duration_seconds = ?,
                       connected = ?,
                       rep_name = ?,
                       rep_sim_number = ?,
                       customer_number_raw = ?,
                       customer_number_norm = ?,
                       call_uid = ?,
                       source_file = ?,
                       ingested_at = ?
                   WHERE call_id = ?""",
                (ts, direction, duration, 1 if duration > 0 else 0,
                 rep_name, rep_sim, cust_raw, cust_norm, call_uid or None,
                 os.path.basename(path), now_iso(), existing["call_id"]),
            )
            duplicate += 1
            continue

        try:
            conn.execute(
                """INSERT INTO callyzer_calls
                   (call_timestamp, direction, duration_seconds, connected,
                    rep_name, rep_sim_number, customer_number_raw,
                    customer_number_norm, call_uid, source_file, row_hash, ingested_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (ts, direction, duration, 1 if duration > 0 else 0,
                 rep_name, rep_sim, cust_raw, cust_norm, call_uid or None,
                 os.path.basename(path), h, now_iso()),
            )
            inserted += 1
        except Exception:  # UNIQUE constraint on row_hash -> already ingested
            duplicate += 1

    conn.commit()
    finish_log(conn, log_id, read, inserted, updated, flagged, duplicate)
    return read, inserted, flagged, duplicate


def ingest_leads(conn, path, reader, headers):
    log_id = start_log(conn, "callyzer_leads", os.path.basename(path))
    read = inserted = updated = flagged = 0

    for raw_row in reader:
        read += 1
        lead_no = (raw_row.get(headers.get("lead_no"), "") or "").strip()
        if not lead_no:
            flagged += 1
            flag_row(conn, log_id, "missing lead no", raw_row)
            continue

        contact_raw = raw_row.get(headers.get("contact_number"), "")
        contact_norm = normalize_phone(contact_raw)

        try:
            attempts = int(float(raw_row.get(headers.get("attempts"), "0") or 0))
        except ValueError:
            attempts = 0

        existing = conn.execute(
            "SELECT lead_no FROM callyzer_leads WHERE lead_no = ?", (lead_no,)
        ).fetchone()

        conn.execute(
            """INSERT INTO callyzer_leads
               (lead_no, lead_name, contact_number_raw, contact_number_norm,
                assigned_to, tags, no_of_attempts, last_call_datetime,
                last_call_note, lead_status, created_date, source_file, ingested_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(lead_no) DO UPDATE SET
                 lead_name=excluded.lead_name,
                 contact_number_raw=excluded.contact_number_raw,
                 contact_number_norm=excluded.contact_number_norm,
                 assigned_to=excluded.assigned_to,
                 tags=excluded.tags,
                 no_of_attempts=excluded.no_of_attempts,
                 last_call_datetime=excluded.last_call_datetime,
                 last_call_note=excluded.last_call_note,
                 lead_status=excluded.lead_status,
                 source_file=excluded.source_file,
                 ingested_at=excluded.ingested_at""",
            (lead_no,
             raw_row.get(headers.get("lead_name"), ""),
             contact_raw, contact_norm,
             raw_row.get(headers.get("assigned_to"), ""),
             raw_row.get(headers.get("tags"), ""),
             attempts,
             raw_row.get(headers.get("last_call_dt"), ""),
             raw_row.get(headers.get("last_call_note"), ""),
             raw_row.get(headers.get("status"), ""),
             raw_row.get(headers.get("created_date"), ""),
             os.path.basename(path), now_iso()),
        )
        if existing:
            updated += 1
        else:
            inserted += 1

    conn.commit()
    finish_log(conn, log_id, read, inserted, updated, flagged, 0)
    return read, inserted, updated, flagged


def process_file(conn, path):
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            print(f"  SKIP {path}: empty or unreadable file")
            return
        file_type = _detect_file_type(reader.fieldnames)
        if file_type == "calls":
            headers = _match_headers(reader.fieldnames, CALL_HEADER_ALIASES)
            read, ins, flg, dup = ingest_calls(conn, path, reader, headers)
            print(f"  CALLS  {os.path.basename(path)}: read={read} inserted={ins} "
                  f"duplicate={dup} flagged={flg}")
        elif file_type == "leads":
            headers = _match_headers(reader.fieldnames, LEAD_HEADER_ALIASES)
            read, ins, upd, flg = ingest_leads(conn, path, reader, headers)
            print(f"  LEADS  {os.path.basename(path)}: read={read} inserted={ins} "
                  f"updated={upd} flagged={flg}")
        else:
            print(f"  UNKNOWN FORMAT {path}: could not detect call-history "
                  f"or lead-data columns. Headers seen: {reader.fieldnames}")
            return

    os.makedirs(PROCESSED_DIR, exist_ok=True)
    shutil.move(path, os.path.join(PROCESSED_DIR, os.path.basename(path)))


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
        print(f"No CSV files found. Drop Callyzer exports into: {INCOMING_DIR}")
        return

    conn = get_connection()
    print(f"Processing {len(paths)} file(s)...")
    for path in paths:
        process_file(conn, path)
    conn.close()
    print("Done.")


if __name__ == "__main__":
    main()
