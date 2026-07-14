"""
rebuild_rep_directory.py
One-off / manual rebuild of the reps and rep_name_aliases tables from
existing callyzer_calls and callyzer_leads data.

Normally you never need to run this by hand — ingest_callyzer.py calls
common.rebuild_rep_directory() automatically after every ingest. This
script exists for the rare case you want to force a rebuild without
ingesting a new file (e.g. right after upgrading from a version of the
database that didn't have the reps table yet).

USAGE:
    python rebuild_rep_directory.py
"""

from common import get_connection, rebuild_rep_directory


def main():
    conn = get_connection()
    rebuild_rep_directory(conn)
    print("Rep directory rebuilt.\n")
    for row in conn.execute("SELECT rep_sim_number, canonical_name FROM reps ORDER BY canonical_name"):
        aliases = [r["alias_key"] for r in conn.execute(
            "SELECT alias_key FROM rep_name_aliases WHERE rep_sim_number = ?", (row["rep_sim_number"],)
        )]
        print(f"  {row['canonical_name']} ({row['rep_sim_number']}): {', '.join(aliases)}")
    conn.close()


if __name__ == "__main__":
    main()
