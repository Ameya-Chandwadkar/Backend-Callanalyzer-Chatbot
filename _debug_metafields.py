"""
One-off debug script: fetches ONE order directly from Shopify and prints
its raw metafields exactly as the API returns them, so we can see the
real key/namespace instead of guessing.
"""
from ingest_shopify import get_access_token, graphql_query
import ingest_shopify as ing
import json

ing.ACCESS_TOKEN = get_access_token()

query = """
query {
  orders(first: 3, sortKey: UPDATED_AT, reverse: true) {
    edges {
      node {
        name
        tags
        customAttributes { key value }
        metafields(first: 20) {
          edges { node { namespace key value type } }
        }
      }
    }
  }
}
"""

data = graphql_query(query)
for edge in data["orders"]["edges"]:
    node = edge["node"]
    print("=" * 40)
    print("Order:", node["name"])
    print("Tags:", node["tags"])
    print("Custom attributes:", node["customAttributes"])
    print("Metafields:", json.dumps(node["metafields"]["edges"], indent=2))