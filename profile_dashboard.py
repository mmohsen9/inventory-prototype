import time
from app import app
from services.findash_service import get_dashboard_summary

with app.app_context():
    t0 = time.perf_counter()
    res = get_dashboard_summary(year=2026)
    print(f"Total get_dashboard_summary: {(time.perf_counter() - t0)*1000:.1f}ms")

