from common import get_connection

conn = get_connection()

print("--- flag reason breakdown ---")
for row in conn.execute("SELECT reason, COUNT(*) as n FROM ingestion_flags GROUP BY reason"):
    print(dict(row))

print()
print("--- sample of flagged rows for each reason ---")
for reason_row in conn.execute("SELECT DISTINCT reason FROM ingestion_flags"):
    reason = reason_row["reason"]
    print(f"\n=== {reason} ===")
    for row in conn.execute(
        "SELECT raw_row FROM ingestion_flags WHERE reason = ? ORDER BY flag_id DESC LIMIT 3",
        (reason,)
    ):
        print(row["raw_row"])