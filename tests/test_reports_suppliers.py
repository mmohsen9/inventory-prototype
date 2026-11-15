import pytest
import csv
import io
from datetime import date, timedelta
from models import PurchaseTransaction, PurchaseLine, Supplier, Location, Item


@pytest.fixture
def sample_suppliers_data(db_session, test_data):
    """Seed data specifically for testing the suppliers statement report."""
    br1 = test_data["br1"]
    br2 = test_data["br2"]
    admin = test_data["admin"]
    item_coffee = test_data["item_coffee"]
    item_milk = test_data["item_milk"]

    # 1. المورد الأول: شركة المراعي
    sup_almarai = Supplier(name="شركة المراعي", code="SUP-ALM", phone="0500000001")
    db_session.add(sup_almarai)
    db_session.flush()

    p1 = PurchaseTransaction(
        location_id=br1.id,
        supplier_name="شركة المراعي",
        invoice_number="INV-ALM-101",
        invoice_date=date.today() - timedelta(days=5),
        status="posted",
        created_by=admin.id
    )
    db_session.add(p1)
    db_session.flush()
    l1 = PurchaseLine(purchase_id=p1.id, item_id=item_milk.id, quantity=100.0, unit_cost=5.0)
    db_session.add(l1)

    p2 = PurchaseTransaction(
        location_id=br2.id,
        supplier_name="شركة المراعي",
        invoice_number="INV-ALM-102",
        invoice_date=date.today() - timedelta(days=2),
        status="approved",
        created_by=admin.id
    )
    db_session.add(p2)
    db_session.flush()
    l2 = PurchaseLine(purchase_id=p2.id, item_id=item_milk.id, quantity=50.0, unit_cost=5.0)
    db_session.add(l2)

    # 2. المورد الثاني: مخابز لولو
    sup_lulu = Supplier(name="مخابز لولو", code="SUP-LUL", phone="0500000002")
    db_session.add(sup_lulu)
    db_session.flush()

    p3 = PurchaseTransaction(
        location_id=br1.id,
        supplier_name="مخابز لولو",
        invoice_number="INV-LUL-001",
        invoice_date=date.today() - timedelta(days=1),
        status="draft",  # مسودة
        created_by=admin.id
    )
    db_session.add(p3)
    db_session.flush()
    l3 = PurchaseLine(purchase_id=p3.id, item_id=item_coffee.id, quantity=10.0, unit_cost=20.0)
    db_session.add(l3)

    p4 = PurchaseTransaction(
        location_id=br1.id,
        supplier_name="مخابز لولو",
        invoice_number="INV-LUL-002",
        invoice_date=date.today(),
        status="queued",
        created_by=admin.id
    )
    db_session.add(p4)
    db_session.flush()
    l4 = PurchaseLine(purchase_id=p4.id, item_id=item_coffee.id, quantity=15.0, unit_cost=20.0)
    db_session.add(l4)

    db_session.commit()
    return {
        "p1": p1, "p2": p2, "p3": p3, "p4": p4,
        "sup_almarai": sup_almarai, "sup_lulu": sup_lulu
    }


def test_suppliers_report_rbac(client, admin_client, accountant_client, branch1_client, warehouse_client):
    """G1: Verify RBAC: only admin and accountant can access suppliers report, branch staff is blocked."""
    # 1. Unauthenticated -> 302 to login
    res = client.get("/reports/suppliers", follow_redirects=False)
    assert res.status_code == 302
    assert "/login" in res.headers["Location"]

    # 2. Branch staff -> 302 to dashboard
    res_branch = branch1_client.get("/reports/suppliers", follow_redirects=False)
    assert res_branch.status_code == 302

    # 3. Warehouse staff -> 302 to dashboard
    res_wh = warehouse_client.get("/reports/suppliers", follow_redirects=False)
    assert res_wh.status_code == 302

    # 4. Accountant -> 200 OK
    res_acc = accountant_client.get("/reports/suppliers")
    assert res_acc.status_code == 200
    assert "كشف استلامات الموردين".encode("utf-8") in res_acc.data

    # 5. Admin -> 200 OK
    res_admin = admin_client.get("/reports/suppliers")
    assert res_admin.status_code == 200
    assert "كشف استلامات الموردين".encode("utf-8") in res_admin.data


def test_suppliers_report_view_and_defaults(admin_client, sample_suppliers_data):
    """G3: Verify default view filters to approved/posted/queued and groups by supplier."""
    res = admin_client.get("/reports/suppliers")
    assert res.status_code == 200
    html = res.data.decode("utf-8")

    # Almarai has p1 (500) and p2 (250) = 750
    assert "شركة المراعي" in html
    assert "INV-ALM-101" in html
    assert "INV-ALM-102" in html

    # Lulu has p4 (300, queued) which is included
    assert "مخابز لولو" in html
    assert "INV-LUL-002" in html

    # Lulu draft p3 (200) should be excluded by default
    assert "INV-LUL-001" not in html

    # Grand total should be 500 + 250 + 300 = 1,050.00
    assert "1,050.00" in html


def test_suppliers_report_filtering(admin_client, sample_suppliers_data, test_data):
    """G4: Verify filtering by supplier_name, location_id, date range, and status."""
    br1 = test_data["br1"]
    br2 = test_data["br2"]

    # 1. Filter by full supplier_name="شركة المراعي"
    res1 = admin_client.get("/reports/suppliers?supplier_name=شركة المراعي")
    html1 = res1.data.decode("utf-8")
    assert "شركة المراعي" in html1
    assert "INV-ALM-101" in html1
    assert "INV-LUL-002" not in html1

    # 1.b Filter by 2 or 3 letters e.g. "مرا" (matching شركة المراعي)
    res_partial = admin_client.get("/reports/suppliers?supplier_name=مرا")
    html_partial = res_partial.data.decode("utf-8")
    assert "شركة المراعي" in html_partial
    assert "INV-ALM-101" in html_partial
    assert "INV-LUL-002" not in html_partial

    # 1.c Filter by 2 or 3 letters e.g. "لول" (matching مخابز لولو)
    res_lulu = admin_client.get("/reports/suppliers?supplier_name=لول")
    html_lulu = res_lulu.data.decode("utf-8")
    assert "مخابز لولو" in html_lulu
    assert "INV-LUL-002" in html_lulu
    assert "INV-ALM-101" not in html_lulu

    # 2. Filter by location_id = br2
    res2 = admin_client.get(f"/reports/suppliers?location_id={br2.id}")
    html2 = res2.data.decode("utf-8")
    assert "INV-ALM-102" in html2
    assert "INV-ALM-101" not in html2

    # 3. Filter by status="draft" -> only p3 appears
    res3 = admin_client.get("/reports/suppliers?status=draft")
    html3 = res3.data.decode("utf-8")
    assert "INV-LUL-001" in html3
    assert "INV-ALM-101" not in html3

    # 4. Filter by status="all" -> draft p3 appears along with others
    res4 = admin_client.get("/reports/suppliers?status=all")
    html4 = res4.data.decode("utf-8")
    assert "INV-LUL-001" in html4
    assert "INV-ALM-101" in html4


def test_suppliers_report_excel_export(accountant_client, sample_suppliers_data):
    """G5: Verify Excel export produces valid UTF-8 BOM CSV with supplier totals and invoice rows."""
    res = accountant_client.get("/reports/suppliers/export")
    assert res.status_code == 200
    assert "text/csv" in res.headers["Content-Type"]
    assert "attachment" in res.headers["Content-Disposition"]

    raw = res.data
    # Check UTF-8 BOM
    assert raw.startswith(b"\xef\xbb\xbf")

    text = raw.decode("utf-8-sig")
    lines = text.strip().splitlines()
    reader = csv.reader(lines)
    rows = list(reader)

    # First row is header
    header = rows[0]
    assert "المورد" in header
    assert "رقم السند" in header
    assert "رقم فاتورة المورد" in header
    assert "إجمالي الفاتورة (ر.س)" in header

    # Contains invoices
    invoices = [r[2] for r in rows if len(r) > 2]
    assert "INV-ALM-101" in invoices
    assert "INV-ALM-102" in invoices
    assert "INV-LUL-002" in invoices

    # Contains Grand Total row
    grand_total_row = [r for r in rows if r and "الإجمالي العام" in r[0]]
    assert len(grand_total_row) == 1
    assert "1,050.00" in grand_total_row[0][7]


def test_reports_export_integration(admin_client, branch1_client, sample_suppliers_data):
    """Verify /reports/export?type=suppliers delegates properly with RBAC."""
    # Branch staff is denied
    res_b = branch1_client.get("/reports/export?type=suppliers", follow_redirects=False)
    assert res_b.status_code == 302

    # Admin is granted
    res_a = admin_client.get("/reports/export?type=suppliers")
    assert res_a.status_code == 200
    assert "text/csv" in res_a.headers["Content-Type"]
    assert "شركة المراعي" in res_a.data.decode("utf-8-sig")
