"""
Regression tests for the Callyzer ingest pipeline.

Why this exists: on 2026-07-10 and 2026-07-14 the chatbot silently reported
wrong call counts (287 vs the real 343, and rep-attribution drift) because
of two independent ingest bugs — landline calls being dropped, and
same-minute re-dials being collapsed by a too-coarse dedup key. Both were
only caught by a human manually cross-checking a CSV row count. These
tests turn that manual check into something that runs every time the
ingest logic changes, so a regression is caught before it reaches anyone
asking the chatbot a question.

Uses an isolated in-memory SQLite database — never touches masonmart.sqlite.

USAGE:
    python -m unittest discover -s tests -v
    (or double-click run_tests.bat in the project root)
"""

import csv
import os
import sqlite3
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common import SCHEMA_PATH, rebuild_rep_directory, canonical_rep_name
from ingest_callyzer import (
    _match_headers, _detect_file_type,
    CALL_HEADER_ALIASES, LEAD_HEADER_ALIASES,
    ingest_calls, ingest_leads,
)

FIXTURES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def isolated_connection():
    """A fresh in-memory DB with the real schema — fully isolated from
    the production masonmart.sqlite, and from other tests."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
        conn.executescript(f.read())
    return conn


def ingest_call_fixture(conn, filename):
    path = os.path.join(FIXTURES_DIR, filename)
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        assert _detect_file_type(reader.fieldnames) == "calls", \
            f"{filename} was not recognized as a call-history export"
        headers = _match_headers(reader.fieldnames, CALL_HEADER_ALIASES)
        return ingest_calls(conn, path, reader, headers)


def ingest_lead_fixture(conn, filename):
    path = os.path.join(FIXTURES_DIR, filename)
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        assert _detect_file_type(reader.fieldnames) == "leads", \
            f"{filename} was not recognized as a lead-data export"
        headers = _match_headers(reader.fieldnames, LEAD_HEADER_ALIASES)
        return ingest_leads(conn, path, reader, headers)


class TestCallCounting(unittest.TestCase):
    """calls_basic.csv (6 rows) encodes every known counting bug in one file:
    a mobile call, a landline call, an exact re-import duplicate (same
    UniqueId), a genuine same-minute re-dial (different UniqueId), an
    unparseable date, and a call with a missing/blank number."""

    def setUp(self):
        self.conn = isolated_connection()
        self.read, self.inserted, self.flagged, self.duplicate = \
            ingest_call_fixture(self.conn, "calls_basic.csv")

    def test_row_accounting_is_exhaustive(self):
        # Every row must land in exactly one bucket — inserted, flagged, or
        # duplicate. If this fails, some row vanished silently (the exact
        # failure mode that caused the original undercount).
        self.assertEqual(self.read, 6)
        self.assertEqual(self.inserted + self.flagged + self.duplicate, self.read)

    def test_landline_call_is_counted_not_dropped(self):
        # Regression: landline calls (10-digit, non-mobile prefix) used to
        # be flagged "unresolvable customer number" and silently dropped.
        row = self.conn.execute(
            "SELECT customer_number_norm FROM callyzer_calls WHERE call_uid = 'UID-LANDLINE-002'"
        ).fetchone()
        self.assertIsNotNone(row, "landline call must be stored, not dropped")
        self.assertIsNone(row["customer_number_norm"], "landline has no valid mobile norm")

    def test_exact_reimport_is_deduplicated(self):
        # UID-MOBILE-001 appears twice in the fixture (simulating an
        # overlapping export window) — must be stored exactly once.
        rows = self.conn.execute(
            "SELECT call_id FROM callyzer_calls WHERE call_uid = 'UID-MOBILE-001'"
        ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(self.duplicate, 1)

    def test_same_minute_redial_is_not_collapsed(self):
        # Regression: two distinct calls to the SAME customer, by the SAME
        # rep, in the SAME minute, used to collapse into one row because
        # the old dedup key only had minute-resolution. UID-MOBILE-001 and
        # UID-REDIAL-004 are both real calls in this fixture and must both
        # be stored.
        rows = self.conn.execute(
            "SELECT call_uid FROM callyzer_calls WHERE customer_number_norm = '9820000001'"
        ).fetchall()
        uids = {r["call_uid"] for r in rows}
        self.assertEqual(uids, {"UID-MOBILE-001", "UID-REDIAL-004"})

    def test_unparseable_date_is_flagged_not_inserted(self):
        self.assertEqual(self.flagged, 1)
        row = self.conn.execute(
            "SELECT call_id FROM callyzer_calls WHERE call_uid = 'UID-BADDATE-005'"
        ).fetchone()
        self.assertIsNone(row)

    def test_blank_number_call_still_counts(self):
        # A call with no usable number at all is still a real call attempt
        # (just unattributable to a customer) — must be stored, not flagged.
        row = self.conn.execute(
            "SELECT customer_number_norm FROM callyzer_calls WHERE call_uid = 'UID-BLANKNUM-006'"
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertIsNone(row["customer_number_norm"])

    def test_final_inserted_count(self):
        # 6 read - 1 duplicate - 1 flagged = 4 genuinely new calls.
        self.assertEqual(self.inserted, 4)


class TestRepCanonicalization(unittest.TestCase):
    """calls_rep_variants.csv has the same rep_sim_number under two name
    spellings ('sara' x3, 'Sara' x1) — the exact pattern that used to
    split one person's numbers across two rows on the dashboard."""

    def setUp(self):
        self.conn = isolated_connection()
        ingest_call_fixture(self.conn, "calls_rep_variants.csv")
        ingest_lead_fixture(self.conn, "leads_basic.csv")
        rebuild_rep_directory(self.conn)

    def test_one_canonical_rep_per_sim_number(self):
        reps = self.conn.execute("SELECT * FROM reps").fetchall()
        self.assertEqual(len(reps), 1)

    def test_majority_spelling_wins(self):
        # 'sara' (3 occurrences) must beat 'Sara' (1 occurrence).
        row = self.conn.execute(
            "SELECT canonical_name FROM reps WHERE rep_sim_number = '9990000002'"
        ).fetchone()
        self.assertEqual(row["canonical_name"], "sara")

    def test_both_spellings_resolve_to_same_canonical_name(self):
        self.assertEqual(canonical_rep_name(self.conn, "sara"), "sara")
        self.assertEqual(canonical_rep_name(self.conn, "Sara"), "sara")
        self.assertEqual(canonical_rep_name(self.conn, "  SARA  "), "sara")

    def test_lead_assignee_with_annotation_resolves_to_same_rep(self):
        # leads_basic.csv assigns to 'Sara(+91-9990000002)' — must resolve
        # to the same canonical identity as the calls, despite the
        # trailing phone annotation and the capitalization difference.
        self.assertEqual(canonical_rep_name(self.conn, "Sara(+91-9990000002)"), "sara")

    def test_calls_grouped_by_sim_number_combine_both_spellings(self):
        total = self.conn.execute(
            "SELECT COUNT(*) FROM callyzer_calls WHERE rep_sim_number = '9990000002'"
        ).fetchone()[0]
        self.assertEqual(total, 4)


if __name__ == "__main__":
    unittest.main()
