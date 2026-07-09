from common import get_connection

conn = get_connection()

print("--- rep_attribution value counts ---")
for row in conn.execute("SELECT rep_attribution, COUNT(*) as n FROM shopify_orders GROUP BY rep_attribution"):
    print(dict(row))

print()
print("--- orders matching 'uman' (case-insensitive) ---")
for row in conn.execute("SELECT order_number, rep_attribution FROM shopify_orders WHERE rep_attribution LIKE '%uman%'"):
    print(dict(row))