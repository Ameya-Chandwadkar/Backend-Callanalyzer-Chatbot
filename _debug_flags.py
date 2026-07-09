from common import get_connection

conn = get_connection()

print("--- most recent ingestion_log entries ---")
for row in conn.execute("SELECT * FROM ingestion_log ORDER BY log_id DESC LIMIT 5"):
    print(dict(row))

print()
print("--- sample of flagged rows (reason + raw row) ---")
for row in conn.execute("SELECT reason, raw_row FROM ingestion_flags ORDER BY flag_id DESC LIMIT 5"):
    print("Reason:", row["reason"])
    print("Raw row:", row["raw_row"])
    print("---")