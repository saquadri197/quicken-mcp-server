"""Create the temporary "Lease Watch" dashboard in Metabase through its API.

Run once on the Metabase VM, after the lease-loader has created schema "lease":

    cd /opt/quicken-mcp-server/deploy/metabase
    python3 lease_watch/create_dashboard.py            # asks for your Metabase admin login

It re-scans the Finance database so Metabase sees the new schema, then builds
the questions and the dashboard in the "Finance" collection. Run it again with
--replace to rebuild (the old dashboard and its questions are moved to trash).
Standard library only.
"""

import argparse
import getpass
import json
import sys
import time
import urllib.error
import urllib.request

DASHBOARD = "Lease Watch (Palisade, until Dec 15)"
TAG = "[lease-watch]"   # put in each question's description so --replace can find them

CARDS = [
    # name, display, sql, (col, row, width, height), visualization settings
    ("Lowest monthly payment this week", "scalar", """
SELECT min(est_monthly_with_trade) AS "Monthly payment"
FROM lease.current_offers WHERE kind <> 'target' AND coalesce(est_cash_at_signing, 0) = 0
""", (0, 0, 6, 3), {"scalar.prefix": "$"}),
    ("Lowest total lease cost / 36 this week", "scalar", """
SELECT min(effective_monthly) AS "Total lease cost / 36" FROM lease.current_offers WHERE kind <> 'target'
""", (6, 0, 6, 3), {"scalar.prefix": "$"}),
    ("Deals at $550/month or less, $0 down", "scalar", """
SELECT count(*) FILTER (WHERE meets_target AND kind <> 'target') AS "Meeting target" FROM lease.current_offers
""", (12, 0, 6, 3), {}),
    ("Deals listed now", "scalar", """
SELECT count(*) AS "Deals" FROM lease.vehicle_status WHERE listed_now AND deal_id IS NOT NULL
""", (18, 0, 6, 3), {}),
    ("Lowest monthly payment by week", "line", """
SELECT check_date AS "Week",
       min(est_monthly_with_trade) AS "Monthly payment",
       min(effective_monthly)      AS "Total lease cost / 36",
       550                         AS "Target"
FROM lease.offers
WHERE est_monthly_with_trade IS NOT NULL AND kind <> 'target'
GROUP BY check_date ORDER BY check_date
""", (0, 3, 12, 6), {"graph.dimensions": ["Week"],
                     "graph.metrics": ["Monthly payment", "Total lease cost / 36", "Target"]}),
    ("Advertised price by car", "line", """
SELECT check_date AS "Week", coalesce(deal_id || ' ', '') || dealer || ' ' || right(coalesce(vin, ''), 6) AS "Car",
       max(advertised_price) AS "Advertised price"
FROM lease.vehicles WHERE advertised_price IS NOT NULL AND coalesce(status, '') NOT IN ('gone', 'sold')
GROUP BY 1, 2 ORDER BY 1
""", (12, 3, 12, 6), {"graph.dimensions": ["Week", "Car"], "graph.metrics": ["Advertised price"]}),
    ("Current offers and estimates", "table", """
SELECT deal_id AS "Deal", source AS "Source", kind AS "Type", model_year::text AS "Year", seats AS "Seats",
       right(coalesce(vin, ''), 6) AS "VIN", monthly_advertised AS "Advertised / mo",
       due_at_signing AS "Due at signing", est_monthly_with_trade AS "Monthly payment",
       est_cash_at_signing AS "Extra cash", effective_monthly AS "Total cost / 36",
       meets_target AS "Meets target", expires AS "Expires", notes AS "Notes", url AS "Link"
FROM lease.current_offers
""", (0, 9, 24, 7), {}),
    ("Matching cars", "table", """
SELECT deal_id AS "Deal", dealer AS "Dealer", location AS "Where", miles_from_nb AS "Miles",
       model_year::text AS "Year", seats AS "Seats", exterior || ' / ' || interior AS "Colors",
       vin AS "VIN", msrp AS "MSRP", advertised_price AS "Advertised", doc_fee AS "Doc fee",
       status AS "Status", first_seen AS "First seen", check_date AS "Last seen",
       listed_now AS "Listed now", url AS "Link"
FROM lease.vehicle_status
ORDER BY listed_now DESC, model_year DESC, miles_from_nb
""", (0, 16, 24, 7), {}),
    ("Quote requests", "table", """
SELECT deal_id AS "Deal", dealer AS "Dealer", right(coalesce(vin, ''), 6) AS "VIN", status AS "Status",
       requested_date AS "Requested", last_update AS "Updated", monthly AS "Quoted / mo",
       due_at_signing AS "Due", selling_price AS "Price", money_factor AS "MF",
       residual_pct AS "Residual %", notes AS "Notes"
FROM lease.quotes ORDER BY last_update DESC NULLS LAST
""", (0, 23, 12, 6), {}),
    ("Weekly notes", "table", """
SELECT check_date AS "Week", recommendation AS "Recommendation", summary AS "What changed"
FROM lease.runs ORDER BY check_date DESC
""", (12, 23, 12, 6), {}),
]


class Metabase:
    def __init__(self, url):
        self.url = url.rstrip("/")
        self.session = None

    def call(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.url + path, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if self.session:
            req.add_header("X-Metabase-Session", self.session)
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                raw = r.read()
        except urllib.error.HTTPError as e:
            sys.exit(f"{method} {path} failed: {e.code} {e.read().decode()[:500]}")
        return json.loads(raw) if raw else None

    def login(self, user, password):
        self.session = self.call("POST", "/api/session", {"username": user, "password": password})["id"]


def items(resp):
    return resp["data"] if isinstance(resp, dict) and "data" in resp else resp


def main():
    ap = argparse.ArgumentParser(description="Create the Lease Watch dashboard")
    ap.add_argument("--url", default="http://localhost:3000")
    ap.add_argument("--user", help="Metabase admin email")
    ap.add_argument("--database", default="Finance", help="Metabase name of the finance database")
    ap.add_argument("--collection", default="Finance")
    ap.add_argument("--replace", action="store_true", help="trash the old dashboard and rebuild")
    a = ap.parse_args()

    mb = Metabase(a.url)
    mb.login(a.user or input("Metabase admin email: "), getpass.getpass("Password: "))

    db = next((d for d in items(mb.call("GET", "/api/database")) if d["name"] == a.database), None)
    if not db:
        sys.exit(f"No Metabase database named {a.database!r}. Use --database.")
    mb.call("POST", f"/api/database/{db['id']}/sync_schema")
    print("Re-scanning the database so Metabase sees schema 'lease'...")
    time.sleep(20)

    coll = next((c for c in mb.call("GET", "/api/collection")
                 if c.get("name") == a.collection and not c.get("archived")), None)
    coll_id = coll["id"] if coll else None

    old = [d for d in items(mb.call("GET", "/api/dashboard")) if d["name"] == DASHBOARD]
    if old and not a.replace:
        sys.exit(f"'{DASHBOARD}' already exists (id {old[0]['id']}). Use --replace to rebuild.")
    for d in old:
        mb.call("PUT", f"/api/dashboard/{d['id']}", {"archived": True})
        for c in items(mb.call("GET", "/api/card")):
            if TAG in (c.get("description") or "") and not c.get("archived"):
                mb.call("PUT", f"/api/card/{c['id']}", {"archived": True})

    dashcards = []
    for i, (name, display, sql, (col, row, w, h), viz) in enumerate(CARDS):
        card = mb.call("POST", "/api/card", {
            "name": name,
            "description": f"{TAG} Palisade lease watch",
            "display": display,
            "collection_id": coll_id,
            "visualization_settings": viz,
            "dataset_query": {"type": "native", "database": db["id"],
                              "native": {"query": sql.strip()}},
        })
        dashcards.append({"id": -(i + 1), "card_id": card["id"], "col": col, "row": row,
                          "size_x": w, "size_y": h, "parameter_mappings": [],
                          "visualization_settings": {}})
        print(f"  question: {name}")

    dash = mb.call("POST", "/api/dashboard", {
        "name": DASHBOARD, "collection_id": coll_id,
        "description": "Weekly Palisade Hybrid SEL Premium AWD lease watch, Nov 18 - Dec 15, 2026. "
                       "Target: $550/month or less for 36 months with the Santa Fe trade and $0 extra "
                       "at signing. 'Total lease cost / 36' spreads everything, Santa Fe included, over the 36 months."})
    mb.call("PUT", f"/api/dashboard/{dash['id']}", {"dashcards": dashcards})
    print(f"Done: {a.url}/dashboard/{dash['id']}")


if __name__ == "__main__":
    main()
