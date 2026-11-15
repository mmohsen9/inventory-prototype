import time
from app import app

routes_to_test = [
    ("/finance/", "Dashboard Index"),
    ("/finance/analysis?subtab=ratios&year=2026", "Analysis Ratios 2026"),
    ("/finance/analysis?subtab=monthly&year_a=2024&year_b=2026", "Analysis Monthly"),
    ("/finance/analysis?subtab=annual", "Analysis Annual"),
    ("/finance/budget?year=2026", "Budget 2026"),
    ("/finance/inventory?subtab=current", "Inventory Current"),
    ("/finance/inventory?subtab=stagnant", "Inventory Stagnant"),
    ("/finance/inventory?subtab=monthly", "Inventory Monthly"),
    ("/finance/inventory?subtab=yearly", "Inventory Yearly"),
    ("/finance/inventory?subtab=statement", "Inventory Statement"),
    ("/finance/inventory?subtab=movements", "Inventory Movements"),
    ("/finance/sales?basis=yearly&year=2026", "Sales Yearly 2026"),
    ("/finance/sales?basis=all", "Sales All"),
    ("/finance/channels?year=2026", "Channels 2026"),
    ("/finance/pnl?year=2026", "PnL 2026"),
    ("/finance/balance-sheet?year=2026", "Balance Sheet 2026"),
    ("/finance/api/drilldown/month?year=2026&month=1", "API Drilldown Month"),
    ("/finance/api/forecast?ahead=12&cat=all&branch=all", "API Forecast"),
]

with app.test_client() as c:
    with c.session_transaction() as s:
        s["user_id"] = 1

    print(f"{'Route':<55} | {'Time (ms)':<10} | {'Status'}")
    print("-" * 75)

    for route, name in routes_to_test:
        t0 = time.perf_counter()
        r = c.get(route)
        dt = (time.perf_counter() - t0) * 1000
        print(f"{name + ' (' + route + ')':<55} | {dt:>8.1f}ms | {r.status_code}")

