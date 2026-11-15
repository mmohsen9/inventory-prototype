# -*- coding: utf-8 -*-
"""
اختبارات ميزة التحديد الجماعي والإجراءات المجمعة (Bulk Selection & Bulk Actions)
لقسم القهوة والمحمصة (سندات الصرف، صرف الأخضر، أوامر التحميص).
"""
import pytest
from datetime import date
from models import db, Item, Location, User, PurchaseTransaction, StockMovement, StockBalance
from models_coffee import (
    CoffeeItem,
    RoastedCoffeeDispatch,
    RoastedCoffeeDispatchLine,
    GreenCoffeeIssue,
    GreenCoffeeIssueLine,
    RoastingOrder,
    RoastingOrderLine
)


@pytest.fixture
def bulk_setup(db_session, test_data):
    """تهيئة الأصناف والمستودعات والمستخدمين للاختبارات."""
    wh = test_data["wh"]
    branch = test_data["br1"]
    admin = test_data["admin"]
    accountant = test_data["accountant"]

    # صنف أخضر وصنف محمص
    item_green = Item(sku="GB-001", name_ar="بن أخضر كولومبي", cost=30.0, storage_unit="kg")
    item_roasted = Item(sku="RB-001", name_ar="بن محمص كولومبي", cost=45.0, storage_unit="kg")
    db_session.add_all([item_green, item_roasted])
    db_session.flush()

    ci_green = CoffeeItem(
        item_id=item_green.id,
        xero_item_code="GB-001",
        prefix="GB",
        base_code="001",
        coffee_type="green",
        name_ar="بن أخضر كولومبي",
        xero_cost_price=30.0,
    )
    ci_roasted = CoffeeItem(
        item_id=item_roasted.id,
        xero_item_code="RB-001",
        prefix="RB",
        base_code="001",
        coffee_type="roasted",
        name_ar="بن محمص كولومبي",
        xero_cost_price=45.0,
        sale_price_per_kg=120.0,
    )
    db_session.add_all([ci_green, ci_roasted])
    db_session.commit()

    return {
        "gw": wh,
        "rw": wh,
        "item_green": item_green,
        "item_roasted": item_roasted,
        "ci_green": ci_green,
        "ci_roasted": ci_roasted,
        "branch": branch,
        "admin": admin,
        "accountant": accountant,
        "wh_user": test_data["wh_user"]
    }


def test_menu_reports_movements_in_templates(client, db_session, test_data):
    """G1: التحقق من وجود رابط سجل حركات المخزون (دفتر الأستاذ) في القائمة الجانبية وقسم الكافيهات."""
    with client.session_transaction() as sess:
        sess["user_id"] = test_data["admin"].id
        sess["role"] = "admin"

    res = client.get("/coffee/audit-log")
    assert res.status_code == 200
    assert "سجل حركات المخزون (دفتر الأستاذ)" in res.get_data(as_text=True)

    res_cafe = client.get("/dashboard")
    assert res_cafe.status_code == 200
    assert "سجل حركات المخزون (دفتر الأستاذ)" in res_cafe.get_data(as_text=True)


def test_bulk_approve_roasted_dispatches(client, db_session, bulk_setup):
    """G2: اعتماد مجمع لسندات صرف البن المحمص: تحديث الحالة وحركات المخزون وفوديكس."""
    rw = bulk_setup["rw"]
    branch = bulk_setup["branch"]
    ci = bulk_setup["ci_roasted"]

    # إنشاء سندين مسودة
    d1 = RoastedCoffeeDispatch(
        doc_number="MP-BULK-001",
        dispatch_date=date.today(),
        dispatch_type="branch",
        branch_location_id=branch.id,
        source_warehouse_id=rw.id,
        status="draft",
        created_by=bulk_setup["admin"].id
    )
    d2 = RoastedCoffeeDispatch(
        doc_number="MP-BULK-002",
        dispatch_date=date.today(),
        dispatch_type="sale",
        customer_name="العميل المعتمد",
        source_warehouse_id=rw.id,
        status="draft",
        created_by=bulk_setup["admin"].id
    )
    db_session.add_all([d1, d2])
    db_session.flush()

    l1 = RoastedCoffeeDispatchLine(
        dispatch_id=d1.id,
        coffee_item_id=ci.id,
        qty_kg=5.0,
        unit_cost=45.0,
        total_cost=225.0
    )
    l2 = RoastedCoffeeDispatchLine(
        dispatch_id=d2.id,
        coffee_item_id=ci.id,
        qty_kg=3.0,
        unit_cost=45.0,
        total_cost=135.0,
        sale_price_per_kg=120.0,
        total_sale_amount=360.0
    )
    db_session.add_all([l1, l2])
    db_session.commit()

    with client.session_transaction() as sess:
        sess["user_id"] = bulk_setup["admin"].id
        sess["role"] = "admin"

    # تنفيذ الاعتماد الجماعي
    res = client.post("/coffee/roasted/dispatches/bulk-action", data={
        "action_type": "approve",
        "selected_ids[]": [str(d1.id), str(d2.id)]
    }, follow_redirects=True)

    assert res.status_code == 200

    # التحقق من قاعدة البيانات
    db_session.expire_all()
    d1_refreshed = db_session.get(RoastedCoffeeDispatch, d1.id)
    d2_refreshed = db_session.get(RoastedCoffeeDispatch, d2.id)

    assert d1_refreshed.status == "approved"
    assert d2_refreshed.status == "approved"

    # التحقق من إنشاء حركات المخزون لمستودع المحمص
    movs = StockMovement.query.filter(
        StockMovement.reference_type == "roasted_dispatch",
        StockMovement.reference_id.in_([d1.id, d2.id])
    ).all()
    assert len(movs) == 2
    assert any(m.quantity_change == -5.0 for m in movs)
    assert any(m.quantity_change == -3.0 for m in movs)

    # التحقق من سند استلام فوديكس للفرع
    ptr = PurchaseTransaction.query.filter_by(invoice_number="MP-BULK-001").first()
    assert ptr is not None
    assert ptr.location_id == branch.id
    assert ptr.status in ("posted", "failed", "foodics_synced")


def test_bulk_delete_roasted_dispatches_admin(client, db_session, bulk_setup):
    """G3: حذف مجمع لسندات صرف البن المحمص للمشرف (Admin) مع التراجع عن المخزون وفوديكس."""
    rw = bulk_setup["rw"]
    branch = bulk_setup["branch"]
    ci = bulk_setup["ci_roasted"]

    d = RoastedCoffeeDispatch(
        doc_number="MP-BULK-DEL",
        dispatch_date=date.today(),
        dispatch_type="branch",
        branch_location_id=branch.id,
        source_warehouse_id=rw.id,
        status="approved",
        created_by=bulk_setup["admin"].id
    )
    db_session.add(d)
    db_session.flush()

    line = RoastedCoffeeDispatchLine(
        dispatch_id=d.id,
        coffee_item_id=ci.id,
        qty_kg=4.0,
        unit_cost=45.0,
        total_cost=180.0
    )
    db_session.add(line)

    # إنشاء حركة مخزون تجريبية
    mov = StockMovement(
        item_id=ci.item_id,
        location_id=rw.id,
        quantity_change=-4.0,
        movement_type="roasted_dispatch_out",
        reference_type="roasted_dispatch",
        reference_id=d.id
    )
    db_session.add(mov)

    # وسند شراء فوديكس
    ptr = PurchaseTransaction(
        invoice_number="MP-BULK-DEL",
        foodics_reference="MP-BULK-DEL",
        location_id=branch.id,
        status="posted"
    )
    db_session.add(ptr)
    db_session.commit()

    with client.session_transaction() as sess:
        sess["user_id"] = bulk_setup["admin"].id
        sess["role"] = "admin"

    res = client.post("/coffee/roasted/dispatches/bulk-action", data={
        "action_type": "delete",
        "selected_ids[]": [str(d.id)]
    }, follow_redirects=True)

    assert res.status_code == 200

    db_session.expire_all()
    # تأكيد حذف السند
    assert db_session.get(RoastedCoffeeDispatch, d.id) is None
    # تأكيد عكس حركة المخزون
    active_movs = StockMovement.query.filter_by(reference_type="roasted_dispatch", reference_id=d.id).all()
    assert len(active_movs) == 0
    # تأكيد حذف سند فوديكس التابع
    assert PurchaseTransaction.query.filter_by(invoice_number="MP-BULK-DEL").first() is None


def test_bulk_delete_button_visible_in_templates(client, db_session, bulk_setup):
    """التحقق من ظهور خيار الحذف في القائمة المنسدلة للمشرف والمحاسب."""
    with client.session_transaction() as sess:
        sess["user_id"] = bulk_setup["admin"].id
        sess["role"] = "admin"

    res_dispatch = client.get("/coffee/roasted/dispatches/")
    assert "حذف السندات المحددة" in res_dispatch.get_data(as_text=True)

    res_green = client.get("/coffee/green/issues/")
    assert "حذف السندات المحددة" in res_green.get_data(as_text=True)

    res_roast = client.get("/coffee/roasting/")
    assert "حذف الأوامر المحددة" in res_roast.get_data(as_text=True)


def test_bulk_delete_roasted_dispatches_unauthorized_forbidden(client, db_session, bulk_setup):
    """G6: التحقق من منع المستخدم غير المصرح له (مثل أمين المستودع) من الحذف المجمع واقتصار الصلاحية على الإدارة والمحاسب."""
    rw = bulk_setup["rw"]
    branch = bulk_setup["branch"]

    d = RoastedCoffeeDispatch(
        doc_number="MP-BULK-ACC-NO-DEL",
        dispatch_date=date.today(),
        dispatch_type="branch",
        branch_location_id=branch.id,
        source_warehouse_id=rw.id,
        status="draft",
        created_by=bulk_setup["admin"].id
    )
    db_session.add(d)
    db_session.commit()

    # تسجيل الدخول كأمين مستودع
    with client.session_transaction() as sess:
        sess["user_id"] = bulk_setup["wh_user"].id
        sess["role"] = "warehouse"

    # محاولة الحذف المجمع
    res = client.post("/coffee/roasted/dispatches/bulk-action", data={
        "action_type": "delete",
        "selected_ids[]": [str(d.id)]
    }, follow_redirects=True)

    # يجب أن يتم رفض الإجراء وإظهار رسالة تفيد بعدم وجود صلاحية
    text = res.get_data(as_text=True)
    assert "للإدارة والمحاسب فقط" in text or "صلاحية" in text or "غير مسموح" in text

    # والتأكد أن السند لم يُحذف من الداتابيس
    db_session.expire_all()
    assert db_session.get(RoastedCoffeeDispatch, d.id) is not None


def test_bulk_approve_and_delete_green_issues(client, db_session, bulk_setup):
    """G4: اعتماد مجمع وحذف مجمع لسندات صرف البن الأخضر."""
    gw = bulk_setup["gw"]
    ci = bulk_setup["ci_green"]

    i1 = GreenCoffeeIssue(
        doc_number="GB-BULK-001",
        issue_date=date.today(),
        issue_type="sale",
        warehouse_id=gw.id,
        status="draft",
        created_by=bulk_setup["admin"].id
    )
    db_session.add(i1)
    db_session.flush()

    line = GreenCoffeeIssueLine(
        issue_id=i1.id,
        coffee_item_id=ci.id,
        qty_kg=20.0,
        unit_cost=30.0,
        total_cost=600.0
    )
    db_session.add(line)
    db_session.commit()

    with client.session_transaction() as sess:
        sess["user_id"] = bulk_setup["admin"].id
        sess["role"] = "admin"

    # اعتماد مجمع
    res = client.post("/coffee/green/issues/bulk-action", data={
        "action_type": "approve",
        "selected_ids[]": [str(i1.id)]
    }, follow_redirects=True)
    assert res.status_code == 200

    db_session.expire_all()
    refreshed_i1 = db_session.get(GreenCoffeeIssue, i1.id)
    assert refreshed_i1.status == "approved"

    # حذف مجمع
    res_del = client.post("/coffee/green/issues/bulk-action", data={
        "action_type": "delete",
        "selected_ids[]": [str(i1.id)]
    }, follow_redirects=True)
    assert res_del.status_code == 200

    db_session.expire_all()
    assert db_session.get(GreenCoffeeIssue, i1.id) is None


def test_bulk_approve_and_delete_roasting_orders(client, db_session, bulk_setup):
    """G5: اعتماد مجمع (صرف الأخضر) وحذف مجمع لأوامر التحميص."""
    gw = bulk_setup["gw"]
    rw = bulk_setup["rw"]
    ci = bulk_setup["ci_green"]

    o1 = RoastingOrder(
        order_number="RB-BULK-ORD-001",
        order_date=date.today(),
        status="draft",
        green_warehouse_id=gw.id,
        roasted_warehouse_id=rw.id,
        created_by=bulk_setup["admin"].id
    )
    db_session.add(o1)
    db_session.flush()

    line = RoastingOrderLine(
        order_id=o1.id,
        line_number=1,
        green_item_id=ci.id,
        green_qty_kg=30.0,
        green_unit_cost=30.0,
        green_total_cost=900.0
    )
    db_session.add(line)
    db_session.commit()

    with client.session_transaction() as sess:
        sess["user_id"] = bulk_setup["admin"].id
        sess["role"] = "admin"

    # اعتماد مجمع
    res = client.post("/coffee/roasting/bulk-action", data={
        "action_type": "approve",
        "selected_ids[]": [str(o1.id)]
    }, follow_redirects=True)
    assert res.status_code == 200

    db_session.expire_all()
    refreshed_o1 = db_session.get(RoastingOrder, o1.id)
    assert refreshed_o1.status == "approved_green"

    # حذف مجمع
    res_del = client.post("/coffee/roasting/bulk-action", data={
        "action_type": "delete",
        "selected_ids[]": [str(o1.id)]
    }, follow_redirects=True)
    assert res_del.status_code == 200

    db_session.expire_all()
    assert db_session.get(RoastingOrder, o1.id) is None


def test_customers_list_live_search_ui(client, db_session, bulk_setup):
    """التحقق من واجهة دليل العملاء والبحث التفاعلي اللحظي بالأحرف وإزالة البيان العلوي."""
    with client.session_transaction() as sess:
        sess["user_id"] = bulk_setup["admin"].id
        sess["role"] = "admin"

    res = client.get("/coffee/customers/")
    assert res.status_code == 200
    html = res.data.decode("utf-8")

    # التحقق من وجود حقل البحث التفاعلي والوظائف المرتبطة
    assert 'id="liveCustomerSearch"' in html
    assert 'onCustomerSearchInput' in html
    assert 'filterCustomersLive' in html
    assert 'normAr' in html
    assert 'id="clearCustomerSearchBtn"' in html
    assert 'id="phoneFilterSelect"' in html
    assert 'id="visibleCustCount"' in html
    assert 'customer-row' in html

    # التأكد من إزالة البيان / التسمية العلوية فوق حقل البحث
    assert 'بحث باسم العميل أو رقم الجوال:' not in html

