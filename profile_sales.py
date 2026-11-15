import time
from app import app
from services.findash_service import get_customer_branch_sales

with app.app_context():
    t0 = time.perf_counter()
    res = get_customer_branch_sales(basis="yearly", year=2026)
    print(f"Total get_customer_branch_sales: {(time.perf_counter() - t0)*1000:.1f}ms")
    print(f"Customers: {len(res['by_customer'])}, Invoices: {res['kpis']['invoices_count']}")

