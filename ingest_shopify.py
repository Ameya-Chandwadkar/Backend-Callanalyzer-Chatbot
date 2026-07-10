"""
ingest_shopify.py
Phase 2 of the PRD: automated ingestion of Shopify order/customer data,
joined to Callyzer data by phone number.

Unlike Callyzer, Shopify's Admin API is confirmed and well documented,
so this script pulls live via API rather than needing a CSV bridge.

WHAT YOU NEED (see README "Shopify setup" section for click-by-click steps):
    1. An app created in the Shopify Dev Dashboard (dev.shopify.com), with
       read_orders and read_customers scopes on a released version, installed
       on your own store.
    2. That app's Client ID and Client secret, in .env as SHOPIFY_CLIENT_ID
       and SHOPIFY_CLIENT_SECRET.
    3. Your store domain in .env as SHOPIFY_STORE_DOMAIN (e.g. masonmart.myshopify.com)

AUTHENTICATION (client credentials grant):
Shopify retired the old "reveal a static Admin API token in the browser"
flow for new apps from January 2026 onward. This script instead requests
a fresh access token at the start of every run using the client
credentials grant (RFC 6749 section 4.4) — trading your Client ID and
Client secret for a token that's valid 24 hours. This only works because
the app and the store belong to the same Shopify organization (i.e. it's
your own store, not a merchant you're building for) — exactly this
script's use case.

INCREMENTAL SYNC:
The script remembers the last order `updated_at` it successfully pulled
(in the sync_state table) and only asks Shopify for orders updated after
that point on every subsequent run — so a daily scheduled run stays fast
and cheap regardless of total order history size.

USAGE:
    python ingest_shopify.py            (incremental — normal daily run)
    python ingest_shopify.py --full     (re-pull everything, e.g. first run)
"""

import os
import sys
import json
import time

from common import get_connection, normalize_phone, now_iso, \
    start_log, finish_log, flag_row, SCRIPT_DIR

try:
    import requests
except ImportError:
    print("ERROR: 'requests' module not found. Run: pip install requests")
    sys.exit(1)


def load_env():
    env_path = os.path.join(SCRIPT_DIR, ".env")
    env = {}
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env


ENV = load_env()
STORE_DOMAIN = ENV.get("SHOPIFY_STORE_DOMAIN")
CLIENT_ID = ENV.get("SHOPIFY_CLIENT_ID")
CLIENT_SECRET = ENV.get("SHOPIFY_CLIENT_SECRET")
API_VERSION = "2025-01"
ACCESS_TOKEN = None  # populated by get_access_token() in main()


def get_access_token():
    """
    Client credentials grant: exchange Client ID + Client secret for a
    fresh Admin API access token. Tokens are valid ~24 hours, so this
    runs once per script execution rather than being cached to disk —
    simplest option for a script that only runs once or twice a day.
    """
    url = f"https://{STORE_DOMAIN}/admin/oauth/access_token"
    resp = requests.post(
        url,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={
            "grant_type": "client_credentials",
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
        },
    )
    if resp.status_code != 200:
        raise RuntimeError(
            f"Failed to obtain access token ({resp.status_code}): {resp.text}\n"
            f"Common cause: the app isn't installed on {STORE_DOMAIN}, or the "
            f"app and store aren't in the same Shopify organization."
        )
    return resp.json()["access_token"]

# MasonMart stores salesperson attribution in a Shopify order metafield
# named "Salesperson Name" (confirmed by the person, not guessed) — the
# namespace varies by how it was created, so we match on the key text
# case-insensitively regardless of namespace. Tags/note-attribute checks
# are kept as a fallback in case attribution conventions change later.
def _extract_rep_attribution(order):
    for edge in (order.get("metafields") or {}).get("edges", []):
        node = edge.get("node", {})
        key = (node.get("key") or "").strip().lower()
        if key in ("salesperson name", "salesperson_name", "salesperson"):
            value = (node.get("value") or "").strip()
            if value:
                return value

    for tag in (order.get("tags") or "").split(","):
        tag = tag.strip()
        if tag.lower().startswith("rep:"):
            return tag.split(":", 1)[1].strip()

    for attr in (order.get("customAttributes") or []):
        if attr.get("key", "").lower() in ("rep", "salesperson", "attributed_to", "salesperson name"):
            return attr.get("value")

    return None


def graphql_query(query, variables=None):
    url = f"https://{STORE_DOMAIN}/admin/api/{API_VERSION}/graphql.json"
    headers = {
        "X-Shopify-Access-Token": ACCESS_TOKEN,
        "Content-Type": "application/json",
    }
    resp = requests.post(url, headers=headers,
                          json={"query": query, "variables": variables or {}})
    resp.raise_for_status()
    data = resp.json()
    if "errors" in data:
        raise RuntimeError(f"Shopify GraphQL error: {data['errors']}")
    return data["data"]


ORDERS_QUERY = """
query($cursor: String, $queryFilter: String) {
  orders(first: 50, after: $cursor, query: $queryFilter, sortKey: UPDATED_AT) {
    pageInfo { hasNextPage endCursor }
    edges {
      node {
        id
        name
        createdAt
        updatedAt
        displayFinancialStatus
        totalPriceSet { shopMoney { amount } }
        tags
        customer { id firstName lastName phone }
        billingAddress { phone }
        customAttributes { key value }
        metafields(first: 20) {
          edges { node { namespace key value } }
        }
      }
    }
  }
}
"""


def fetch_orders_since(updated_after):
    query_filter = f"updated_at:>'{updated_after}'" if updated_after else ""
    cursor = None
    all_orders = []
    while True:
        data = graphql_query(ORDERS_QUERY, {"cursor": cursor, "queryFilter": query_filter})
        edges = data["orders"]["edges"]
        all_orders.extend(e["node"] for e in edges)
        if data["orders"]["pageInfo"]["hasNextPage"]:
            cursor = data["orders"]["pageInfo"]["endCursor"]
            time.sleep(0.5)  # gentle on rate limits
        else:
            break
    return all_orders


def upsert_order(conn, log_id, node):
    phone_raw = None
    if node.get("customer") and node["customer"].get("phone"):
        phone_raw = node["customer"]["phone"]
    elif node.get("billingAddress") and node["billingAddress"].get("phone"):
        phone_raw = node["billingAddress"]["phone"]

    phone_norm = normalize_phone(phone_raw)
    if phone_norm is None:
        flag_row(conn, log_id, "order has no resolvable customer phone", node.get("name"))

    customer = node.get("customer") or {}
    customer_name = " ".join(filter(None, [customer.get("firstName"), customer.get("lastName")])).strip()

    order = {
        "tags": ",".join(node.get("tags", [])),
        "customAttributes": node.get("customAttributes") or [],
        "metafields": node.get("metafields") or {"edges": []},
    }

    conn.execute(
        """INSERT INTO shopify_orders
           (order_id, order_number, created_at, total_price, financial_status,
            customer_phone_raw, customer_phone_norm, customer_name,
            rep_attribution, ingested_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(order_id) DO UPDATE SET
             financial_status=excluded.financial_status,
             customer_phone_raw=excluded.customer_phone_raw,
             customer_phone_norm=excluded.customer_phone_norm,
             rep_attribution=excluded.rep_attribution,
             ingested_at=excluded.ingested_at""",
        (node["id"], node["name"], node["createdAt"],
         float(node["totalPriceSet"]["shopMoney"]["amount"]),
         node.get("displayFinancialStatus"),
         phone_raw, phone_norm, customer_name,
         _extract_rep_attribution(order), now_iso()),
    )

    if customer.get("id"):
        conn.execute(
            """INSERT INTO shopify_customers (customer_id, phone_raw, phone_norm, name, email, ingested_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(customer_id) DO UPDATE SET
                 phone_raw=excluded.phone_raw, phone_norm=excluded.phone_norm,
                 name=excluded.name, ingested_at=excluded.ingested_at""",
            (customer["id"], phone_raw, phone_norm, customer_name, None, now_iso()),
        )


def main():
    global ACCESS_TOKEN
    if not STORE_DOMAIN or not CLIENT_ID or not CLIENT_SECRET:
        print("ERROR: SHOPIFY_STORE_DOMAIN, SHOPIFY_CLIENT_ID and SHOPIFY_CLIENT_SECRET "
              "must be set in .env")
        print("See README.md 'Shopify setup' section.")
        sys.exit(1)

    print("Requesting access token via client credentials grant...")
    ACCESS_TOKEN = get_access_token()

    full_sync = "--full" in sys.argv
    conn = get_connection()

    last_sync_row = conn.execute(
        "SELECT last_synced_at FROM sync_state WHERE source='shopify_orders'"
    ).fetchone()
    updated_after = None if full_sync else (last_sync_row["last_synced_at"] if last_sync_row else None)

    log_id = start_log(conn, "shopify_orders", "api:graphql")
    print(f"Fetching orders {'(full sync)' if full_sync else f'updated after {updated_after}'} ...")

    try:
        orders = fetch_orders_since(updated_after)
    except Exception as e:
        finish_log(conn, log_id, 0, 0, 0, 0, 0, notes=f"FAILED: {e}")
        print(f"ERROR fetching orders: {e}")
        sys.exit(1)

    inserted = 0
    for node in orders:
        upsert_order(conn, log_id, node)
        inserted += 1
    conn.commit()

    latest_updated_at = max((o["updatedAt"] for o in orders), default=updated_after)
    if latest_updated_at:
        conn.execute(
            """INSERT INTO sync_state (source, last_synced_at) VALUES ('shopify_orders', ?)
               ON CONFLICT(source) DO UPDATE SET last_synced_at=excluded.last_synced_at""",
            (latest_updated_at,),
        )
        conn.commit()

    finish_log(conn, log_id, len(orders), inserted, 0, 0, 0)
    print(f"Done. {inserted} order(s) upserted. Next run will fetch orders updated after {latest_updated_at}.")
    conn.close()


if __name__ == "__main__":
    main()