import json
import pytest
from models import PurchaseTransaction, PurchaseLine
from tests.conftest import login_client


def test_api_parse_invoice_no_file(client, test_data):
    login_client(client, test_data["admin"].id)
    res = client.post("/api/ai/parse-invoice")
    assert res.status_code == 400
    data = res.get_json()
    assert data["status"] == "error"


def test_api_purchase_ai_import_lines(client, test_data, db_session):
    admin = test_data["admin"]
    br1 = test_data["br1"]
    item = test_data["item_coffee"]
    login_client(client, admin.id)

    # 1. إنشاء مسودة سند استلام
    p = PurchaseTransaction(
        location_id=br1.id,
        supplier_name="شركة التوريد الذكية",
        invoice_number="INV-AI-TEST-99",
        created_by=admin.id,
        status="draft"
    )
    db_session.add(p)
    db_session.commit()

    # 2. استيراد بنود مستخرجة بالذكاء الاصطناعي
    payload = {
        "items": [
            {
                "matched_item_id": item.id,
                "matched_item_sku": item.sku,
                "normalized_quantity": 10.0,
                "normalized_unit_price": 45.0,
                "tax_amount": 67.5,
                "total_price": 517.5
            }
        ],
        "attached_file_path": "/static/uploads/purchases_temp/ai_invoice_doc.pdf"
    }

    res = client.post(f"/api/purchases/{p.id}/ai-import-lines", json=payload)
    assert res.status_code == 200
    res_data = res.get_json()
    assert res_data["status"] == "success"
    assert res_data["added_count"] == 1

    # 3. التحقق من إدراج البند والمرفق في قاعدة البيانات
    db_session.refresh(p)
    assert len(p.lines) == 1
    assert p.lines[0].item_id == item.id
    assert p.lines[0].quantity == 10.0
    assert p.lines[0].unit_cost == 45.0
    assert "/static/uploads/purchases_temp/ai_invoice_doc.pdf" in (p.attachments or "")


def test_api_purchase_ai_import_lines_blocked_on_posted(client, test_data, db_session):
    admin = test_data["admin"]
    br1 = test_data["br1"]
    login_client(client, admin.id)

    p_posted = PurchaseTransaction(
        location_id=br1.id,
        supplier_name="شركة التوريد",
        invoice_number="INV-POSTED-01",
        created_by=admin.id,
        status="posted"
    )
    db_session.add(p_posted)
    db_session.commit()

    res = client.post(f"/api/purchases/{p_posted.id}/ai-import-lines", json={"items": [{"matched_item_id": 1, "quantity": 1}]})
    assert res.status_code == 400
    assert "لا يمكن استيراد" in res.get_json()["message"]


def test_purchase_new_with_ai_lines(client, test_data, db_session):
    admin = test_data["admin"]
    br1 = test_data["br1"]
    item = test_data["item_coffee"]
    login_client(client, admin.id)

    form_data = {
        "location_id": str(br1.id),
        "supplier_name": "مورد الفاتورة الذكية",
        "invoice_number": "INV-NEW-AI-01",
        "invoice_date": "2026-11-18",
        "ai_attached_file": "/static/uploads/purchases_temp/scanned_inv.pdf",
        "ai_parsed_lines_json": json.dumps([
            {
                "matched_item_id": item.id,
                "matched_item_sku": item.sku,
                "normalized_quantity": 25.0,
                "normalized_unit_price": 50.0,
                "tax_amount": 0.0,
                "total_price": 1250.0
            }
        ])
    }

    res = client.post("/purchases/new", data=form_data, follow_redirects=True)
    assert res.status_code == 200

    created_p = PurchaseTransaction.query.filter_by(invoice_number="INV-NEW-AI-01").first()
    assert created_p is not None
    assert len(created_p.lines) == 1
    assert created_p.lines[0].item_id == item.id
    assert created_p.lines[0].quantity == 25.0
    assert created_p.lines[0].unit_cost == 50.0
    assert "/static/uploads/purchases_temp/scanned_inv.pdf" in (created_p.attachments or "")
