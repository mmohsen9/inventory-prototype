import pytest
from models import (
    db, Location, User, PurchaseTransaction, PurchaseLine, 
    TransferOrder, TransferLine, Item, StockMovement, StockBalance, SyncQueue
)


def test_purchase_save_draft_and_to_draft(admin_client, db_session):
    """التحقق من حفظ سند الاستلام كمسودة، وإعادة فتح السند كمسودة بعد الاعتماد وعكس الأثر المخزني."""
    wh = Location.query.filter_by(type="warehouse").first()
    if not wh:
        wh = Location(name_ar="المستودع الرئيسي", name_en="Main Warehouse", type="warehouse")
        db_session.add(wh)
        db_session.commit()

    item = Item.query.first()
    if not item:
        item = Item(sku="SK-DRAFT-P1", name_ar="صنف استلام مسودة", cost=25.0, storage_unit="حبة")
        db_session.add(item)
        db_session.commit()

    # 1. إنشاء سند استلام جديد
    purchase = PurchaseTransaction(
        invoice_number="INV-TEST-001",
        location_id=wh.id,
        status="draft",
        supplier_name="مورد الاختبار",
        attachments='["invoice.pdf"]'
    )
    db_session.add(purchase)
    db_session.commit()

    line = PurchaseLine(
        purchase_id=purchase.id,
        item_id=item.id,
        quantity=10,
        unit_cost=25.0
    )
    db_session.add(line)
    db_session.commit()

    # 2. اختبار action="save_draft"
    res = admin_client.post(f"/purchases/{purchase.id}", data={
        "action": "save_draft",
        "supplier_name": "مورد الاختبار المعدل",
        "invoice_number": "INV-MODIFIED-001"
    }, follow_redirects=True)
    assert res.status_code == 200
    db_session.refresh(purchase)
    assert purchase.status == "draft"
    assert purchase.supplier_name == "مورد الاختبار المعدل"
    assert purchase.invoice_number == "INV-MODIFIED-001"

    # 3. اختبار التقديم للاعتماد
    res_sub = admin_client.post(f"/purchases/{purchase.id}", data={
        "action": "submit_for_review"
    }, follow_redirects=True)
    assert res_sub.status_code == 200
    db_session.refresh(purchase)
    assert purchase.status == "submitted"

    # 4. إعادة الفتح كمسودة قبل الاعتماد
    res_draft1 = admin_client.post(f"/purchases/{purchase.id}", data={
        "action": "to_draft"
    }, follow_redirects=True)
    assert res_draft1.status_code == 200
    db_session.refresh(purchase)
    assert purchase.status == "draft"

    # 5. تقديم السند ثم اعتماده للتحقق من الأثر المخزني وعكسه
    admin_client.post(f"/purchases/{purchase.id}", data={"action": "submit_for_review"}, follow_redirects=True)
    admin_client.post(f"/purchases/{purchase.id}/review", data={"action": "approve"}, follow_redirects=True)
    db_session.refresh(purchase)
    assert purchase.status == "approved"

    # التحقق من وجود حركة مخزنية
    movements_count = StockMovement.query.filter_by(reference_type="purchase", reference_id=purchase.id).count()
    assert movements_count > 0

    # 6. إعادة الفتح كمسودة بعد الاعتماد
    res_draft2 = admin_client.post(f"/purchases/{purchase.id}", data={
        "action": "to_draft"
    }, follow_redirects=True)
    assert res_draft2.status_code == 200
    db_session.refresh(purchase)
    assert purchase.status == "draft"

    # التحقق من حذف الحركة المخزنية بعد العودة للمسودة
    movements_after = StockMovement.query.filter_by(reference_type="purchase", reference_id=purchase.id).count()
    assert movements_after == 0


def test_purchase_delete_endpoint(admin_client, db_session):
    """التحقق من حذف سند الاستلام عبر مسار الحذف المباشر purchase_delete."""
    wh = Location.query.filter_by(type="warehouse").first()
    if not wh:
        wh = Location(name_ar="المستودع الرئيسي", name_en="Main Warehouse", type="warehouse")
        db_session.add(wh)
        db_session.commit()

    item = Item.query.first()
    if not item:
        item = Item(sku="SK-DEL-P1", name_ar="صنف حذف استلام", cost=15.0, storage_unit="حبة")
        db_session.add(item)
        db_session.commit()

    purchase = PurchaseTransaction(
        invoice_number="INV-DEL-001",
        location_id=wh.id,
        status="draft",
        supplier_name="مورد قابل للحذف"
    )
    db_session.add(purchase)
    db_session.commit()

    p_id = purchase.id
    line = PurchaseLine(
        purchase_id=p_id,
        item_id=item.id,
        quantity=5,
        unit_cost=15.0
    )
    db_session.add(line)
    db_session.commit()

    # تنفيذ الحذف عبر POST /purchases/<id>/delete
    res = admin_client.post(f"/purchases/{p_id}/delete", follow_redirects=True)
    assert res.status_code == 200

    # التحقق من حذف السند وبنوده
    assert PurchaseTransaction.query.get(p_id) is None
    assert PurchaseLine.query.filter_by(purchase_id=p_id).first() is None


def test_transfer_save_draft_and_to_draft(admin_client, db_session):
    """التحقق من حفظ سند صرف المواد كمسودة، وإلغاء الصرف وإعادته كمسودة للتعديل وعكس الأثر المخزني."""
    wh = Location.query.filter_by(type="warehouse").first()
    branch = Location.query.filter_by(type="branch").first()
    if not wh or not branch:
        wh = Location(name_ar="المستودع الرئيسي", name_en="Main Warehouse", type="warehouse")
        branch = Location(name_ar="فرع التحلية", name_en="Tahlia Branch", type="branch")
        db_session.add_all([wh, branch])
        db_session.commit()

    item = Item.query.first()
    if not item:
        item = Item(sku="SK-DRAFT-T1", name_ar="صنف صرف مسودة", cost=30.0, storage_unit="حبة")
        db_session.add(item)
        db_session.commit()

    # ضبط رصيد أولي للمستودع
    sb = StockBalance.query.filter_by(item_id=item.id, location_id=wh.id).first()
    if not sb:
        sb = StockBalance(item_id=item.id, location_id=wh.id, quantity=50)
        db_session.add(sb)
    else:
        sb.quantity = 50
    db_session.commit()

    # 1. إنشاء سند صرف
    transfer = TransferOrder(
        from_location_id=wh.id,
        to_location_id=branch.id,
        status="draft"
    )
    db_session.add(transfer)
    db_session.commit()

    t_line = TransferLine(
        transfer_id=transfer.id,
        item_id=item.id,
        qty_requested=10,
        qty_sent=10
    )
    db_session.add(t_line)
    db_session.commit()

    # 2. اختبار action="save_draft"
    res_save = admin_client.post(f"/transfers/{transfer.id}", data={
        "action": "save_draft",
        "to_location_id": branch.id
    }, follow_redirects=True)
    assert res_save.status_code == 200
    db_session.refresh(transfer)
    assert transfer.status == "draft"

    # 3. إرسال الشحنة (action="send")
    res_send = admin_client.post(f"/transfers/{transfer.id}", data={
        "action": "send"
    }, follow_redirects=True)
    assert res_send.status_code == 200
    db_session.refresh(transfer)
    assert transfer.status in ["sent", "in_transit"]

    # التحقق من خصم الرصيد من المستودع
    db_session.refresh(sb)
    assert sb.quantity == 40

    # 4. التراجع عن الإرسال وإعادة السند كمسودة (action="to_draft")
    res_revert = admin_client.post(f"/transfers/{transfer.id}", data={
        "action": "to_draft"
    }, follow_redirects=True)
    assert res_revert.status_code == 200
    db_session.refresh(transfer)
    assert transfer.status == "draft"

    # التحقق من استعادة رصيد المستودع وحذف الحركات
    db_session.refresh(sb)
    assert sb.quantity == 50
    mv_count = StockMovement.query.filter_by(reference_type="transfer", reference_id=transfer.id).count()
    assert mv_count == 0


def test_transfer_delete_endpoint(admin_client, db_session):
    """التحقق من حذف سند صرف المواد عبر مسار الحذف المباشر transfer_delete."""
    wh = Location.query.filter_by(type="warehouse").first()
    branch = Location.query.filter_by(type="branch").first()
    if not wh or not branch:
        wh = Location(name_ar="المستودع الرئيسي", name_en="Main Warehouse", type="warehouse")
        branch = Location(name_ar="فرع التحلية", name_en="Tahlia Branch", type="branch")
        db_session.add_all([wh, branch])
        db_session.commit()

    item = Item.query.first()
    if not item:
        item = Item(sku="SK-DEL-T1", name_ar="صنف حذف صرف", cost=20.0, storage_unit="حبة")
        db_session.add(item)
        db_session.commit()

    transfer = TransferOrder(
        from_location_id=wh.id,
        to_location_id=branch.id,
        status="draft"
    )
    db_session.add(transfer)
    db_session.commit()

    t_id = transfer.id
    t_line = TransferLine(
        transfer_id=t_id,
        item_id=item.id,
        qty_requested=5,
        qty_sent=5
    )
    db_session.add(t_line)
    db_session.commit()

    # تنفيذ الحذف عبر POST /transfers/<id>/delete
    res = admin_client.post(f"/transfers/{t_id}/delete", follow_redirects=True)
    assert res.status_code == 200

    # التحقق من حذف سند الصرف وبنوده
    assert TransferOrder.query.get(t_id) is None
    assert TransferLine.query.filter_by(transfer_id=t_id).first() is None
