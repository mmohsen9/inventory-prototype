import json
import pytest
from models import (
    db, PurchaseTransaction, PurchaseLine, TransferOrder, TransferLine,
    CountSession, CountLine, StockBalance, StockMovement, SyncQueue
)

def test_full_purchase_lifecycle(test_data, warehouse_client, accountant_client):
    """دورة حياة سند الشراء كاملة: إنشاء -> إضافة بنود ومرفق -> تقديم للمراجعة -> اعتماد المحاسب -> تحديث المخزون وحركة الدفتر وطابور المزامنة."""
    wh = test_data["wh"]
    item = test_data["item_coffee"]
    sup = test_data["supplier"]

    # 1. إنشاء مسودة الشراء
    res = warehouse_client.post("/purchases/new", data={
        "supplier_name": sup.name,
        "invoice_number": "PURCH-2026-001",
        "invoice_date": "2026-10-07",
        "location_id": wh.id
    }, follow_redirects=True)
    assert res.status_code == 200

    purchase = PurchaseTransaction.query.filter_by(invoice_number="PURCH-2026-001").first()
    assert purchase is not None
    assert purchase.status == "draft"

    # 2. إضافة بند
    res_line = warehouse_client.post(f"/purchases/{purchase.id}", data={
        "action": "add_line",
        "code": item.sku,
        "quantity": "50.0",
        "unit": item.storage_unit,
        "unit_cost": "58.0"
    }, follow_redirects=True)
    assert res_line.status_code == 200
    db.session.refresh(purchase)
    assert len(purchase.lines) == 1
    assert purchase.lines[0].quantity == 50.0

    # 3. إرفاق صورة الفاتورة
    purchase.attachments = json.dumps(["/uploads/purchases/inv_scan.pdf"])
    db.session.commit()

    # 4. تقديم للمراجعة
    res_submit = warehouse_client.post(f"/purchases/{purchase.id}", data={
        "action": "submit_for_review"
    }, follow_redirects=True)
    assert res_submit.status_code == 200
    db.session.refresh(purchase)
    assert purchase.status == "submitted"

    # 5. أمين المستودع لا يمكنه الاعتماد
    res_unauth_appr = warehouse_client.post(f"/purchases/{purchase.id}/review", data={
        "action": "approve"
    }, follow_redirects=False)
    assert res_unauth_appr.status_code in (302, 403)
    db.session.refresh(purchase)
    assert purchase.status == "submitted"


    # 6. اعتماد المحاسب
    wh_stock_before = item.get_stock_in_location(wh.id)
    res_appr = accountant_client.post(f"/purchases/{purchase.id}/review", data={
        "action": "approve"
    }, follow_redirects=True)
    assert res_appr.status_code == 200

    db.session.refresh(purchase)
    assert purchase.status == "approved"

    # 7. التحقق من زيادة رصيد المستودع بـ 50 كجم بدقة
    wh_stock_after = item.get_stock_in_location(wh.id)
    assert wh_stock_after == wh_stock_before + 50.0

    # 8. التحقق من تسجيل الحركة في دفتر الأستاذ (StockMovement)
    mov = StockMovement.query.filter_by(reference_id=purchase.id, movement_type="purchase_in").first()
    assert mov is not None
    assert mov.quantity_change == 50.0

    # 9. التحقق من عدم إمكانية تعديل أو حذف الفاتورة بعد الاعتماد (Immutability)
    res_edit_attempt = warehouse_client.post(f"/purchases/{purchase.id}", data={
        "action": "add_line",
        "code": item.sku,
        "quantity": "10.0"
    }, follow_redirects=True)
    db.session.refresh(purchase)
    assert len(purchase.lines) == 1  # لم يتغير

def test_full_transfer_lifecycle_with_variance(test_data, warehouse_client, branch1_client, accountant_client):
    """دورة حياة التحويل بين المواقع: إنشاء -> إرسال وخصم من المستودع -> استلام الفرع مع نقص -> مراجعة المحاسب واعتماد الفروقات -> إضافة للفرع."""
    wh = test_data["wh"]
    br1 = test_data["br1"]
    item = test_data["item_coffee"]

    wh_initial_stock = item.get_stock_in_location(wh.id)
    br1_initial_stock = item.get_stock_in_location(br1.id)

    # 1. إنشاء سند تحويل
    res = warehouse_client.post("/transfers/new", data={
        "from_location_id": wh.id,
        "to_location_id": br1.id
    }, follow_redirects=True)
    transfer = TransferOrder.query.filter_by(from_location_id=wh.id, to_location_id=br1.id).order_by(TransferOrder.id.desc()).first()
    assert transfer is not None

    # 2. إضافة 20 كجم قهوة للشحنة
    warehouse_client.post(f"/transfers/{transfer.id}", data={
        "action": "add_line",
        "code": item.sku,
        "quantity": "20.0"
    }, follow_redirects=True)

    # 3. إرسال الشحنة (صرف من المستودع)
    res_send = warehouse_client.post(f"/transfers/{transfer.id}", data={"action": "send"}, follow_redirects=True)
    assert res_send.status_code == 200
    db.session.refresh(transfer)
    assert transfer.status in ("in_transit", "sent")

    # رصيد المستودع ينقص 20
    assert item.get_stock_in_location(wh.id) == wh_initial_stock - 20.0

    # 4. موظف فرع 1 يستلم الشحنة مع وجود نقص (استلم 18 بدل 20)
    line = transfer.lines[0]
    res_rcv = branch1_client.post(f"/transfers/{transfer.id}/receive", data={
        f"qty_received_{line.id}": "18.0",
        f"reason_{line.id}": "كيسين ممزقين أثناء النقل"
    }, follow_redirects=True)
    assert res_rcv.status_code == 200

    db.session.refresh(transfer)
    db.session.refresh(line)
    assert transfer.status in ("needs_review", "pending_review")
    assert line.variance_qty == -2.0

    # 5. المحاسب يعتمد الفروقات
    res_appr = accountant_client.post(f"/transfers/{transfer.id}/approve", follow_redirects=True)
    assert res_appr.status_code == 200

    db.session.refresh(transfer)
    assert transfer.status in ("approved", "completed")

    # رصيد الفرع يزيد بمقدار المستلم الفعلي (18 كجم)
    assert item.get_stock_in_location(br1.id) == br1_initial_stock + 18.0

def test_full_inventory_count_lifecycle(test_data, warehouse_client, accountant_client):
    """دورة حياة الجرد الفعلي: بدء جلسة جرد -> إدخال الكمية الفعلية -> إغلاق الجلسة -> اعتماد المحاسب -> تسوية الرصيد والدفتر."""
    wh = test_data["wh"]
    item = test_data["item_coffee"]
    current_book_stock = item.get_stock_in_location(wh.id)

    # 1. فتح جلسة جرد للمستودع
    res = warehouse_client.post("/counts/new", data={"location_id": wh.id}, follow_redirects=True)
    assert res.status_code == 200

    session_obj = CountSession.query.filter_by(location_id=wh.id, status="open").order_by(CountSession.id.desc()).first()
    assert session_obj is not None

    # 2. تسجيل الجرد الفعلي (عد 88 كجم)
    res_line = warehouse_client.post(f"/counts/{session_obj.id}", data={
        "action": "add_line",
        "code": item.sku,
        "counted_quantity": "88.0"
    }, follow_redirects=True)
    assert res_line.status_code == 200

    line = session_obj.lines[0]
    assert line.counted_quantity == 88.0
    assert line.variance_quantity == 88.0 - current_book_stock

    # 3. إغلاق الجلسة وإحالتها للمراجعة
    warehouse_client.post(f"/counts/{session_obj.id}", data={"action": "close_session"}, follow_redirects=True)
    db.session.refresh(session_obj)
    assert session_obj.status == "pending_review"

    # 4. اعتماد المحاسب للتسوية
    res_appr = accountant_client.post(f"/counts/{session_obj.id}", data={"action": "approve_session"}, follow_redirects=True)
    assert res_appr.status_code == 200

    db.session.refresh(session_obj)
    assert session_obj.status == "approved"

    # 5. التحقق من مطابقة الرصيد الفعلي لرقم الجرد (88.0)
    assert item.get_stock_in_location(wh.id) == 88.0
