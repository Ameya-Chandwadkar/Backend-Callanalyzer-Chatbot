"""
common.py
Shared helpers used by every ingestion script and the chat layer.
No external dependencies beyond the Python standard library.
"""

import os
import re
import sqlite3
import hashlib
from datetime import datetime, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(SCRIPT_DIR, "masonmart.sqlite")
SCHEMA_PATH = os.path.join(SCRIPT_DIR, "schema.sql")


def get_connection():
    """Open a connection to the unified store, creating tables if needed."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
        conn.executescript(f.read())
    return conn


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def normalize_phone(raw):
    """
    Normalize an Indian mobile number to a bare 10-digit string.
    Handles: +91 98765 43210 | 0091-9876543210 | 98765-43210 | 9876543210
    Returns None if it can't be resolved to a 10-digit number
    (these get flagged, not silently dropped).
    """
    if raw is None:
        return None
    digits = re.sub(r"\D", "", str(raw))
    if digits.startswith("0091"):
        digits = digits[4:]
    elif digits.startswith("91") and len(digits) == 12:
        digits = digits[2:]
    elif digits.startswith("0") and len(digits) == 11:
        digits = digits[1:]
    if len(digits) == 10 and digits[0] in "6789":
        return digits
    return None


def row_hash(*parts):
    """Stable hash of a row's key fields, used to de-duplicate re-imports
    of overlapping CSV export windows (a common occurrence when exports
    are pulled daily but cover a rolling multi-day window)."""
    joined = "|".join(str(p) for p in parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def start_log(conn, source, file_name):
    cur = conn.execute(
        "INSERT INTO ingestion_log (source, file_name, started_at) VALUES (?, ?, ?)",
        (source, file_name, now_iso()),
    )
    conn.commit()
    return cur.lastrowid


def finish_log(conn, log_id, rows_read, rows_inserted, rows_updated, rows_flagged, rows_duplicate, notes=""):
    conn.execute(
        """UPDATE ingestion_log
           SET rows_read=?, rows_inserted=?, rows_updated=?, rows_flagged=?,
               rows_duplicate=?, finished_at=?, notes=?
           WHERE log_id=?""",
        (rows_read, rows_inserted, rows_updated, rows_flagged, rows_duplicate,
         now_iso(), notes, log_id),
    )
    conn.commit()


def flag_row(conn, log_id, reason, raw_row):
    conn.execute(
        "INSERT INTO ingestion_flags (log_id, reason, raw_row, flagged_at) VALUES (?, ?, ?, ?)",
        (log_id, reason, str(raw_row), now_iso()),
    )
