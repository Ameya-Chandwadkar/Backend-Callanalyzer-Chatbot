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


def _strip_lead_assignee(raw_name):
    """leads.assigned_to sometimes carries a trailing '(...)' annotation,
    e.g. 'Sara (Team Lead)' — matches the stripping ingest_callyzer's
    dashboard query already did ad hoc; centralized here so every caller
    (rep directory build, chat SQL) agrees on the same rule."""
    name = (raw_name or "").strip()
    paren = name.find("(")
    if paren > 0:
        name = name[:paren].strip()
    return name


def rebuild_rep_directory(conn):
    """
    Rebuild the reps / rep_name_aliases tables from callyzer_calls.

    rep_sim_number is the one stable identity Callyzer gives us; rep_name
    drifts across exports ("sara" / "Sara" / "Sara K"). For each sim
    number, the most frequently seen name becomes the canonical display
    name, and every variant seen (plus leads.assigned_to variants that
    match one of those names once parenthetical annotations are stripped)
    becomes an alias pointing back to it.

    Call this after every Callyzer ingest (ingest_callyzer.py does), or
    run rebuild_rep_directory.py for a one-off rebuild of existing data.
    Safe to re-run — fully replaces both tables each time.
    """
    variant_counts = {}  # rep_sim_number -> {rep_name: count}
    for row in conn.execute(
        """SELECT rep_sim_number, rep_name, COUNT(*) AS n, MAX(ingested_at) AS latest
           FROM callyzer_calls
           WHERE rep_sim_number IS NOT NULL AND TRIM(rep_sim_number) != ''
             AND rep_name IS NOT NULL AND TRIM(rep_name) != ''
           GROUP BY rep_sim_number, rep_name"""
    ):
        sim = row["rep_sim_number"].strip()
        name = row["rep_name"].strip()
        variant_counts.setdefault(sim, {})[name] = (row["n"], row["latest"] or "")

    canonical_by_sim = {}
    for sim, variants in variant_counts.items():
        # Winner = most frequent name; ties broken by most recently seen,
        # then alphabetically for full determinism.
        winner = max(variants.items(), key=lambda kv: (kv[1][0], kv[1][1], kv[0]))[0]
        canonical_by_sim[sim] = winner

    conn.execute("DELETE FROM rep_name_aliases")
    conn.execute("DELETE FROM reps")

    ts = now_iso()
    for sim, canonical in canonical_by_sim.items():
        conn.execute(
            "INSERT INTO reps (rep_sim_number, canonical_name, updated_at) VALUES (?, ?, ?)",
            (sim, canonical, ts),
        )
        for variant in variant_counts[sim]:
            alias_key = variant.strip().lower()
            conn.execute(
                """INSERT INTO rep_name_aliases (alias_key, rep_sim_number, canonical_name)
                   VALUES (?, ?, ?)
                   ON CONFLICT(alias_key) DO UPDATE SET
                     rep_sim_number=excluded.rep_sim_number,
                     canonical_name=excluded.canonical_name""",
                (alias_key, sim, canonical),
            )

    # Also alias leads.assigned_to spellings (once "(...)" is stripped) to
    # whichever canonical name they case-insensitively match, so lead
    # counts group under the same identity as call counts.
    canonical_lookup = {c.lower(): c for c in canonical_by_sim.values()}
    for row in conn.execute(
        "SELECT DISTINCT assigned_to FROM callyzer_leads WHERE assigned_to IS NOT NULL AND TRIM(assigned_to) != ''"
    ):
        stripped = _strip_lead_assignee(row["assigned_to"])
        if not stripped:
            continue
        match = canonical_lookup.get(stripped.lower())
        if not match:
            continue
        alias_key = stripped.lower()
        conn.execute(
            """INSERT INTO rep_name_aliases (alias_key, rep_sim_number, canonical_name)
               VALUES (?, NULL, ?)
               ON CONFLICT(alias_key) DO NOTHING""",
            (alias_key, match),
        )

    conn.commit()


def canonical_rep_name(conn, raw_name):
    """Resolve any name spelling to its canonical form, via rep_name_aliases.
    Falls back to the trimmed input if no alias is known (e.g. a brand new
    rep before the directory has been rebuilt) — never raises, never blanks
    out a real name just because it's not catalogued yet."""
    stripped = _strip_lead_assignee(raw_name)
    if not stripped:
        return stripped
    row = conn.execute(
        "SELECT canonical_name FROM rep_name_aliases WHERE alias_key = ?",
        (stripped.lower(),),
    ).fetchone()
    return row["canonical_name"] if row else stripped
