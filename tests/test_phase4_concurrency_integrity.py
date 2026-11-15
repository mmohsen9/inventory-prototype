"""
اختبارات المرحلة 4 و 5: سلامة العمليات والمخزون والتزامن ونقاط فحص النشر
- التحقق من نقاط الفحص /health و /readiness
- التحقق من منع الاعتماد والاستلام المزدوج (Idempotency) في المشتريات والتحويلات والجرد
- التحقق من سلامة تسجيل حركات المخزون والمطابقة الرياضية 100%
"""
import pytest
from datetime import datetime
from models import (
    db, User, Location, Item, PurchaseTransaction, PurchaseLine,
    TransferOrder, TransferLine, CountSession, CountLine,
    StockBalance, StockMovement, record_stock_movement
)
from services.transfer_service import confirm_transfer_receipt, approve_transfer_with_variance


@pytest.fixture
def phase4_setup(app):
    """تهيئة بيانات اختبار معزولة للمرحلة 4."""
    with app.app_context():
        # مستخدمين
        admin = User(name="مدير النظام", email="admin4@example.com", role="admin")
        admin.set_password("AdminPass123!")
        accountant = User(name="المحاسب", email="acct4@example.com", role="accountant")
        accountant.set_password("AcctPass123!")
        staff = User(name="موظف الفرع", email="staff4@example.com", role="branch_staff")
        staff.set_password("StaffPass123!")

        # مواقع
        wh = Location(name_ar="المستودع الرئيسي", name_en="Central Warehouse", type="warehouse", foodics_id="wh-401")
        br = Location(name_ar="فرع النرجس", name_en="Al-Narjis Branch", type="branch", foodics_id="br-401")

        # صنف
        item = Item(sku="ITM-4001", name_ar="حليب كامل الدسم", storage_unit="لتر", ingredient_unit="مل", cost=5.5)

        db.session.add_all([admin, accountant, staff, wh, br, item])
        db.session.commit()

        staff.location_id = br.id
        db.session.commit()

        yield {
            "admin": admin,
            "accountant": accountant,
            "staff": staff,
            "wh": wh,
            "br": br,
            "item": item,
        }


def test_health_and_readiness_endpoints(client):
    """فحص نقطتي الصحة والجاهزية /health و /readiness."""
    res_h = client.get("/health")
    assert res_h.status_code == 200
    data_h = res_h.get_json()
    assert data_h["status"] == "healthy"
    assert data_h["database"] == "ok"
    assert "timestamp" in data_h

    res_r = client.get("/readiness")
    assert res_r.status_code == 200
    data_r = res_r.get_json()
    assert data_r["status"] == "ready"


def test_purchase_approval_idempotency(client, phase4_setup):
    """التحقق من منع الاعتماد المزدوج لسند الاستلام ومنع تكرار حركات المخزون."""
    data = phase4_setup
    item = data["item"]
    wh = data["wh"]

    # إنشاء سند استلام بحالة pending
    purchase = PurchaseTransaction(
        invoice_number="INV-IDEMP-01",
        supplier_name="شركة الألبان",
        location_id=wh.id,
        status="pending",
        created_by=data["staff"].id,
    )
    db.session.add(purchase)
    db.session.flush()

    line = PurchaseLine(
        purchase_id=purchase.id,
        item_id=item.id,
        quantity=50.0,
        unit_cost=5.5,
    )
    db.session.add(line)
    db.session.commit()

    with client.session_transaction() as sess:
        sess["user_id"] = data["accountant"].id
        sess["role"] = data["accountant"].role

    # الاعتماد الأول
    res1 = client.post(f"/purchases/{purchase.id}/review", data={"action": "approve"}, follow_redirects=True)
    assert res1.status_code == 200

    db.session.refresh(purchase)
    assert purchase.status == "approved"

    bal1 = StockBalance.query.filter_by(item_id=item.id, location_id=wh.id).first()
    assert bal1 is not None
    assert bal1.quantity == 50.0

    movements_count1 = StockMovement.query.filter_by(reference_type="purchase", reference_id=purchase.id).count()
    assert movements_count1 == 1

    # محاولة الاعتماد الثاني (نقرة مزدوجة أو تكرار الطلب)
    res2 = client.post(f"/purchases/{purchase.id}/review", data={"action": "approve"}, follow_redirects=True)
    assert res2.status_code == 200

    # التحقق الحاسم: عدم تكرار حركات المخزون وبقاء الرصيد 50 دون زيادة
    db.session.refresh(bal1)
    assert bal1.quantity == 50.0

    movements_count2 = StockMovement.query.filter_by(reference_type="purchase", reference_id=purchase.id).count()
    assert movements_count2 == 1


def test_transfer_receipt_idempotency(phase4_setup):
    """التحقق من منع الاستلام المزدوج للتحويلات ومنع تكرار دخول المخزون للوجهة."""
    data = phase4_setup
    item = data["item"]
    wh = data["wh"]
    br = data["br"]

    transfer = TransferOrder(
        from_location_id=wh.id,
        to_location_id=br.id,
        created_by=data["admin"].id,
        status="sent",
    )
    db.session.add(transfer)
    db.session.flush()

    t_line = TransferLine(
        transfer_id=transfer.id,
        item_id=item.id,
        qty_sent=20.0,
    )
    db.session.add(t_line)
    db.session.commit()

    # الاستلام الأول
    received_data = {t_line.id: {"qty_received": 20.0, "variance_reason": ""}}
    has_var, msg = confirm_transfer_receipt(transfer, received_data, data["staff"].id)
    assert not has_var
    assert transfer.status == "received"

    bal_br = StockBalance.query.filter_by(item_id=item.id, location_id=br.id).first()
    assert bal_br is not None
    assert bal_br.quantity == 20.0

    m_count1 = StockMovement.query.filter_by(reference_type="transfer", reference_id=transfer.id).count()
    assert m_count1 == 1

    # الاستلام الثاني المكرر
    has_var2, msg2 = confirm_transfer_receipt(transfer, received_data, data["staff"].id)
    assert not has_var2
    assert "مسبقاً" in msg2

    db.session.refresh(bal_br)
    assert bal_br.quantity == 20.0

    m_count2 = StockMovement.query.filter_by(reference_type="transfer", reference_id=transfer.id).count()
    assert m_count2 == 1


def test_count_session_approval_idempotency(client, phase4_setup):
    """التحقق من منع الاعتماد المزدوج لجلسة الجرد."""
    data = phase4_setup
    item = data["item"]
    br = data["br"]

    # رصيد أولي 100
    record_stock_movement(item.id, br.id, "initial", 100.0, notes="رصيد أولي")
    db.session.commit()

    session_obj = CountSession(
        location_id=br.id,
        created_by=data["staff"].id,
        status="pending_review",
    )
    db.session.add(session_obj)
    db.session.flush()

    line = CountLine(
        session_id=session_obj.id,
        item_id=item.id,
        book_quantity=100.0,
        counted_quantity=95.0,  # عجز 5
    )
    db.session.add(line)
    db.session.commit()

    with client.session_transaction() as sess:
        sess["user_id"] = data["accountant"].id
        sess["role"] = data["accountant"].role

    # اعتماد أول
    res1 = client.post(f"/counts/{session_obj.id}", data={"action": "approve_session"}, follow_redirects=True)
    assert res1.status_code == 200

    db.session.refresh(session_obj)
    assert session_obj.status == "approved"

    bal = StockBalance.query.filter_by(item_id=item.id, location_id=br.id).first()
    assert bal.quantity == 95.0  # 100 - 5

    adj_count1 = StockMovement.query.filter_by(reference_type="count", reference_id=session_obj.id).count()
    assert adj_count1 == 1

    # اعتماد ثاني مكرر
    res2 = client.post(f"/counts/{session_obj.id}", data={"action": "approve_session"}, follow_redirects=True)
    assert res2.status_code == 200

    db.session.refresh(bal)
    assert bal.quantity == 95.0  # لم يُخصم مرة أخرى!

    adj_count2 = StockMovement.query.filter_by(reference_type="count", reference_id=session_obj.id).count()
    assert adj_count2 == 1


def test_stock_balance_ledger_mathematical_reconciliation(phase4_setup):
    """التحقق من التطابق الرياضي التام بين أرصدة StockBalance ومجموع حركات StockMovement."""
    from sqlalchemy import func
    data = phase4_setup
    item = data["item"]
    wh = data["wh"]
    br = data["br"]

    # تنفيذ سلسلة حركات متعددة
    record_stock_movement(item.id, wh.id, "initial", 200.0, notes="رصيد بداية")
    record_stock_movement(item.id, wh.id, "purchase_in", 50.0, notes="شراء إضافي")
    record_stock_movement(item.id, wh.id, "transfer_out", -30.0, notes="تحويل لفرع")
    record_stock_movement(item.id, br.id, "transfer_in", 30.0, notes="استلام تحويل")
    record_stock_movement(item.id, br.id, "waste_out", -2.0, notes="تالف بالفرع")
    db.session.commit()

    # فحص التطابق الرياضي
    movement_sums = db.session.query(
        StockMovement.item_id,
        StockMovement.location_id,
        func.sum(StockMovement.quantity_change).label("total_qty")
    ).group_by(StockMovement.item_id, StockMovement.location_id).all()

    m_dict = {(item_id, loc_id): round(float(t_qty or 0.0), 4) for item_id, loc_id, t_qty in movement_sums}

    balances = StockBalance.query.filter(StockBalance.item_id == item.id).all()
    assert len(balances) >= 2

    for b in balances:
        expected = m_dict.get((b.item_id, b.location_id), 0.0)
        actual = round(float(b.quantity or 0.0), 4)
        assert abs(actual - expected) < 0.0001
