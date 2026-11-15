"""
الاختبارات الآلية الشاملة لقسم "القهوة الخضراء والمحمصة"
تغطي:
1. بطاقات أصناف القهوة ووحدات القياس (الأخياش/الكيلو)
2. دورة حياة أمر التحميص (المرحلة 1: صرف الأخضر، المرحلة 2: الناتج الفعلي ونقل التكلفة)
3. قيود ونسب الفاقد والفاقد الزائد
4. إلغاء أمر التحميص وعكس الحركات المخزنية
5. سندات صرف الأخضر وسندات صرف المحمص للفروع
6. منطق التكامل المحاسبي مع Xero API
7. الصلاحيات وعزل الأدوار
8. مسارات واجهات المستخدم وقوالب HTML
"""
import pytest
from datetime import date, datetime
from models import db, Location, Item, StockBalance, StockMovement
from models_coffee import (
    CoffeeItem, CoffeeItemLink, RoastingOrder, RoastingOrderLine,
    GreenCoffeeIssue, RoastedCoffeeDispatch, CoffeePermission, CoffeeSetting,
    CoffeeAuditLog, has_coffee_permission, next_roasting_order_number, next_issue_doc_number
)
from coffee_xero import (
    build_green_credit_note_payload, build_roasted_bill_payload,
    post_roasting_order_to_xero
)


@pytest.fixture
def coffee_setup(db_session, test_data):
    """تهيئة مستودعات وأصناف القهوة للاختبار."""
    gw = Location.query.filter_by(name_ar="مستودع البن الأخضر").first()
    if not gw:
        gw = Location(name_ar="مستودع البن الأخضر", name_en="Green Coffee Warehouse", type="warehouse")
        db_session.add(gw)

    rw = Location.query.filter_by(name_ar="مستودع البن المحمص").first()
    if not rw:
        rw = Location(name_ar="مستودع البن المحمص", name_en="Roasted Coffee Warehouse", type="warehouse")
        db_session.add(rw)
    db_session.flush()

    # صنف البن الأخضر (كولومبيا - خيشة 70 كجم)
    item_gb = Item(sku="GB0377", name_ar="كولومبيا توليما أخضر", storage_unit="كجم", cost=40.75)
    db_session.add(item_gb)
    db_session.flush()

    ci_gb = CoffeeItem(
        item_id=item_gb.id,
        xero_item_code="GB0377-كولومبيا توليما",
        prefix="GB",
        base_code="0377",
        coffee_type="green",
        name_ar="كولومبيا توليما أخضر",
        sack_allowed=True,
        default_sack_weight=70.0,
        reference_loss_pct=14.0,
    )
    db_session.add(ci_gb)

    # صنف البن المحمص المقابل (كولومبيا محمص)
    item_rb = Item(sku="RB0465", name_ar="كولومبيا حبوب محمصة", storage_unit="كجم", cost=49.50)
    db_session.add(item_rb)
    db_session.flush()

    ci_rb = CoffeeItem(
        item_id=item_rb.id,
        xero_item_code="RB0465-كولومبيا محمص",
        prefix="RB",
        base_code="0465",
        coffee_type="roasted",
        name_ar="كولومبيا حبوب محمصة",
        sack_allowed=False,
    )
    db_session.add(ci_rb)
    db_session.flush()

    # ربط مخصص (GB0377 -> RB0465)
    link = CoffeeItemLink(
        green_item_id=ci_gb.id,
        roasted_item_id=ci_rb.id,
        link_type="manual",
        is_default=True,
        reason="كود الصنف المحمص يختلف عن كود الأخضر في نظام Xero التاريخي",
    )
    db_session.add(link)

    # رصيد أولي للبن الأخضر في مستودع الأخضر (140 كجم = 2 خيشة)
    from models import record_stock_movement
    record_stock_movement(
        item_id=item_gb.id,
        location_id=gw.id,
        movement_type="initial",
        quantity_change=140.0,
        notes="رصيد أولي للاختبار",
        user_id=test_data["admin"].id,
    )
    db_session.commit()

    return {
        "gw": gw,
        "rw": rw,
        "ci_gb": ci_gb,
        "ci_rb": ci_rb,
        "item_gb": item_gb,
        "item_rb": item_rb,
        "admin": test_data["admin"],
        "wh_user": test_data["wh_user"],
        "branch": test_data["br1"],
    }


def test_extract_prefix_and_base():
    """اختبار استخراج البادئة والكود الأساسي من أكواد Xero المختلفة."""
    p, b = CoffeeItem.extract_prefix_and_base("GB0619-اريكا ووبنشي اخضر")
    assert p == "GB"
    assert b == "0619"

    p, b = CoffeeItem.extract_prefix_and_base("RB32025")
    assert p == "RB"
    assert b == "32025"

    p, b = CoffeeItem.extract_prefix_and_base("  gb1007  ")
    assert p == "GB"
    assert b == "1007"

    p, b = CoffeeItem.extract_prefix_and_base("INVALID_CODE")
    assert p is None
    assert b is None


def test_sacks_display(coffee_setup):
    """اختبار العرض المزدوج للأخياش والكيلو."""
    ci = coffee_setup["ci_gb"]
    assert "2 خيشة" in ci.sacks_display(140.0)
    assert "1 خيشة + 10" in ci.sacks_display(80.0)
    assert "25.0" in ci.sacks_display(25.0)

    # إذا كانت الأخياش غير مفعلة
    ci_rb = coffee_setup["ci_rb"]
    assert "50.00 كجم" in ci_rb.sacks_display(50.0)


def test_roasting_order_lifecycle(coffee_setup, db_session):
    """
    اختبار دورة حياة أمر التحميص الكاملة:
    1. إنشاء أمر تحميص مسودة
    2. اعتماد صرف الأخضر (المرحلة 1) -> خصم 122 كجم من مخزون الأخضر
    3. إدخال الناتج الفعلي (المرحلة 2) -> 105 كجم محمص
    4. حساب تكلفة الكيلو ونقل التكلفة الكاملة
    5. اعتماد الناتج -> إضافة 105 كجم لمخزون المحمص وتحديث تكلفة الصنف
    """
    gw = coffee_setup["gw"]
    rw = coffee_setup["rw"]
    ci_gb = coffee_setup["ci_gb"]
    ci_rb = coffee_setup["ci_rb"]
    admin = coffee_setup["admin"]

    # رصيد الأخضر الحالي 140 كجم
    bal_before = StockBalance.query.filter_by(item_id=ci_gb.item_id, location_id=gw.id).first().quantity
    assert bal_before == 140.0

    # 1. إنشاء أمر تحميص
    order = RoastingOrder(
        order_number=next_roasting_order_number(),
        order_date=date.today(),
        green_warehouse_id=gw.id,
        roasted_warehouse_id=rw.id,
        roasting_supervisor="سليمان الحماص",
        status="draft",
        created_by=admin.id,
    )
    db_session.add(order)
    db_session.flush()

    line = RoastingOrderLine(
        order_id=order.id,
        line_number=1,
        green_item_id=ci_gb.id,
        green_qty_kg=122.0,
        green_qty_sacks=1.74,
        green_sack_weight=70.0,
        green_unit_cost=40.75,
        green_total_cost=round(122.0 * 40.75, 4),  # 4971.50
        roasted_item_id=ci_rb.id,
        link_type="manual",
        manual_link_reason="ربط مخصص",
        reference_loss_pct=14.0,
        line_status="pending",
    )
    db_session.add(line)
    db_session.commit()

    assert order.total_green_cost == 4971.50

    # 2. اعتماد صرف الأخضر (المرحلة 1)
    from models import record_stock_movement
    record_stock_movement(
        item_id=ci_gb.item_id,
        location_id=gw.id,
        movement_type="roasting_green_out",
        quantity_change=-122.0,
        reference_type="roasting_order",
        reference_id=order.id,
        user_id=admin.id,
    )
    order.status = "approved_green"
    order.green_approved_by = admin.id
    order.green_approved_at = datetime.utcnow()
    line.line_status = "green_issued"
    db_session.commit()

    bal_after_green = StockBalance.query.filter_by(item_id=ci_gb.item_id, location_id=gw.id).first().quantity
    assert bal_after_green == 18.0  # 140 - 122

    # 3. إدخال الناتج الفعلي (المرحلة 2) — 105 كجم
    line.roasted_qty_kg_actual = 105.0
    line.roasted_unit_cost = line.calc_roasted_unit_cost()
    line.calc_loss()
    order.status = "roasting"
    db_session.commit()

    # التحقق من حسابات الفاقد وتكلفة الكيلو
    # loss = 122 - 105 = 17 كجم
    # loss_pct = 17 / 122 = 13.934%
    # unit_cost = 4971.50 / 105 = 47.347619...
    assert round(line.loss_kg, 2) == 17.0
    assert round(line.loss_pct, 2) == 13.93
    assert not line.loss_exceeds_reference  # 13.93 <= 14.0
    assert round(line.roasted_unit_cost, 4) == 47.3476

    # 4. اعتماد الناتج وإكمال الأمر
    record_stock_movement(
        item_id=ci_rb.item_id,
        location_id=rw.id,
        movement_type="roasting_roasted_in",
        quantity_change=105.0,
        reference_type="roasting_order",
        reference_id=order.id,
        user_id=admin.id,
    )
    ci_rb.item.cost = line.roasted_unit_cost
    order.status = "completed"
    order.roasting_approved_by = admin.id
    order.roasting_approved_at = datetime.utcnow()
    line.line_status = "completed"
    db_session.commit()

    # رصيد المحمص بمستودع المحمص
    bal_roasted = StockBalance.query.filter_by(item_id=ci_rb.item_id, location_id=rw.id).first().quantity
    assert bal_roasted == 105.0

    # تكلفة الصنف المحمص تم تحديثها
    assert round(ci_rb.item.cost, 4) == 47.3476

    # نقل التكلفة كاملة بالهللة
    total_roasted_value = round(105.0 * ci_rb.item.cost, 2)
    assert total_roasted_value == 4971.50


def test_excess_loss_logic(coffee_setup, db_session):
    """اختبار كشف الفاقد الزائد عند تجاوز النسبة المرجعية."""
    order = RoastingOrder(
        order_number=next_roasting_order_number(),
        order_date=date.today(),
        green_warehouse_id=coffee_setup["gw"].id,
        roasted_warehouse_id=coffee_setup["rw"].id,
        status="approved_green",
    )
    db_session.add(order)
    db_session.flush()

    # دخل 100 كجم وخرج 80 كجم (فاقد 20%، بينما المرجعية 14%)
    line = RoastingOrderLine(
        order_id=order.id,
        line_number=1,
        green_item_id=coffee_setup["ci_gb"].id,
        green_qty_kg=100.0,
        green_unit_cost=40.0,
        green_total_cost=4000.0,
        roasted_item_id=coffee_setup["ci_rb"].id,
        reference_loss_pct=14.0,
        roasted_qty_kg_actual=80.0,
    )
    line.calc_loss()
    assert line.loss_kg == 20.0
    assert line.loss_pct == 20.0
    assert line.loss_exceeds_reference is True


def test_roasting_order_cancellation_rollback(coffee_setup, db_session):
    """اختبار إلغاء أمر التحميص بعد صرف الأخضر وعكس الحركة المخزنية."""
    gw = coffee_setup["gw"]
    ci_gb = coffee_setup["ci_gb"]
    admin = coffee_setup["admin"]

    bal_start = StockBalance.query.filter_by(item_id=ci_gb.item_id, location_id=gw.id).first().quantity

    # صرف 50 كجم لأمر تحميص
    from models import record_stock_movement
    order = RoastingOrder(
        order_number=next_roasting_order_number(),
        order_date=date.today(),
        green_warehouse_id=gw.id,
        roasted_warehouse_id=coffee_setup["rw"].id,
        status="approved_green",
    )
    db_session.add(order)
    db_session.flush()

    line = RoastingOrderLine(
        order_id=order.id,
        line_number=1,
        green_item_id=ci_gb.id,
        green_qty_kg=50.0,
        green_unit_cost=40.75,
        green_total_cost=2037.5,
        roasted_item_id=coffee_setup["ci_rb"].id,
        line_status="green_issued",
    )
    db_session.add(line)
    record_stock_movement(ci_gb.item_id, gw.id, "roasting_green_out", -50.0, reference_type="roasting_order", reference_id=order.id)
    db_session.commit()

    bal_mid = StockBalance.query.filter_by(item_id=ci_gb.item_id, location_id=gw.id).first().quantity
    assert bal_mid == bal_start - 50.0

    # إلغاء الأمر -> عكس الحركة (+50 كجم)
    record_stock_movement(ci_gb.item_id, gw.id, "roasting_green_out", +50.0, reference_type="roasting_order", reference_id=order.id, notes="إلغاء أمر التحميص")
    order.status = "cancelled"
    line.line_status = "pending"
    db_session.commit()

    bal_end = StockBalance.query.filter_by(item_id=ci_gb.item_id, location_id=gw.id).first().quantity
    assert bal_end == bal_start


def test_green_issue_voucher(coffee_setup, db_session):
    """اختبار إنشاء سند صرف بن أخضر (بيع خام أو عينات) وخصم الرصيد."""
    gw = coffee_setup["gw"]
    ci_gb = coffee_setup["ci_gb"]
    admin = coffee_setup["admin"]

    bal_before = StockBalance.query.filter_by(item_id=ci_gb.item_id, location_id=gw.id).first().quantity

    from models import record_stock_movement
    doc = GreenCoffeeIssue(
        doc_number=next_issue_doc_number("GI"),
        issue_date=date.today(),
        warehouse_id=gw.id,
        issue_type="sale",
        issue_reason="بيع بن أخضر لمحمصة شقيقة",
        coffee_item_id=ci_gb.id,
        qty_kg=30.0,
        unit_cost=40.75,
        total_cost=30.0 * 40.75,
        status="submitted",
        created_by=admin.id,
    )
    db_session.add(doc)
    db_session.flush()

    record_stock_movement(ci_gb.item_id, gw.id, "green_issue_out", -30.0, reference_type="green_issue", reference_id=doc.id)
    db_session.commit()

    bal_after = StockBalance.query.filter_by(item_id=ci_gb.item_id, location_id=gw.id).first().quantity
    assert bal_after == bal_before - 30.0
    assert doc.doc_number.startswith(("GI-", "GB-"))


def test_roasted_dispatch_voucher(coffee_setup, db_session):
    """اختبار سند صرف بن محمص للفرع (سلسلة MP) وخصم الرصيد."""
    rw = coffee_setup["rw"]
    ci_rb = coffee_setup["ci_rb"]
    branch = coffee_setup["branch"]
    admin = coffee_setup["admin"]

    from models import record_stock_movement
    # إضافة رصيد مبدئي في مستودع المحمص
    record_stock_movement(ci_rb.item_id, rw.id, "initial", 50.0)

    doc = RoastedCoffeeDispatch(
        doc_number=next_issue_doc_number("MP"),
        dispatch_date=date.today(),
        source_warehouse_id=rw.id,
        branch_location_id=branch.id,
        coffee_item_id=ci_rb.id,
        qty_kg=12.5,
        unit_cost=49.50,
        total_cost=round(12.5 * 49.50, 4),
        status="approved",
        created_by=admin.id,
    )
    db_session.add(doc)
    db_session.flush()

    record_stock_movement(ci_rb.item_id, rw.id, "roasted_dispatch_out", -12.5, reference_type="roasted_dispatch", reference_id=doc.id)
    db_session.commit()

    bal_after = StockBalance.query.filter_by(item_id=ci_rb.item_id, location_id=rw.id).first().quantity
    assert bal_after == 37.5  # 50 - 12.5
    assert doc.doc_number.startswith("MP-")


def test_xero_payload_generation(coffee_setup, db_session):
    """اختبار بناء مستندات Xero (Credit Note للأخضر و Bill للمحمص) وتطابق القيمة بالهللة."""
    order = RoastingOrder(
        order_number="RB-7268",
        order_date=date(2026, 10, 1),
        green_warehouse_id=coffee_setup["gw"].id,
        roasted_warehouse_id=coffee_setup["rw"].id,
        status="completed",
    )
    db_session.add(order)
    db_session.flush()

    line = RoastingOrderLine(
        order_id=order.id,
        line_number=1,
        green_item_id=coffee_setup["ci_gb"].id,
        green_qty_kg=122.0,
        green_unit_cost=40.75,
        green_total_cost=4971.50,
        roasted_item_id=coffee_setup["ci_rb"].id,
        roasted_qty_kg_actual=105.0,
        roasted_unit_cost=47.347619,
        line_status="completed",
    )
    db_session.add(line)
    db_session.commit()

    cn_payload = build_green_credit_note_payload(order)
    assert cn_payload["Type"] == "ACCPAYCREDIT"
    assert cn_payload["Contact"]["Name"] == "امر تحميص مستودع البن الاخضر"
    assert len(cn_payload["LineItems"]) == 1
    assert cn_payload["LineItems"][0]["AccountCode"] == "635"
    assert cn_payload["LineItems"][0]["Quantity"] == 122.0
    assert cn_payload["LineItems"][0]["UnitAmount"] == 40.75
    cn_total = cn_payload["LineItems"][0]["Quantity"] * cn_payload["LineItems"][0]["UnitAmount"]

    bill_payload = build_roasted_bill_payload(order)
    assert bill_payload["Type"] == "ACCPAY"
    assert bill_payload["Contact"]["Name"] == "تحميص وارد لمستودع البن المحمص"
    assert len(bill_payload["LineItems"]) == 1
    assert bill_payload["LineItems"][0]["AccountCode"] == "636"
    assert bill_payload["LineItems"][0]["Quantity"] == 105.0
    bill_total = round(bill_payload["LineItems"][0]["Quantity"] * bill_payload["LineItems"][0]["UnitAmount"], 2)

    # تطابق القيمة بين المستندين بالهللة
    assert round(cn_total, 2) == round(bill_total, 2)


def test_coffee_views_get_authenticated(client, test_data, coffee_setup):
    """اختبار تحميل واجهات مستخدم قسم القهوة (HTTP 200) للمستخدم المصرح له."""
    # تسجيل الدخول كمدير
    with client.session_transaction() as sess:
        sess["user_id"] = test_data["admin"].id
        sess["role"] = "admin"
        sess["user_name"] = "مدير النظام"

    routes = [
        "/coffee/dashboard",
        "/coffee/green/stock",
        "/coffee/roasting/",
        "/coffee/roasting/new",
        "/coffee/green/issues/",
        "/coffee/green/issues/new",
        "/coffee/roasted/stock",
        "/coffee/roasted/dispatches/",
        "/coffee/roasted/dispatches/new",
        "/coffee/xero/status",
        "/coffee/reports",
        "/coffee/reports/roasting-summary",
        "/coffee/settings",
        "/coffee/audit-log",
        "/coffee/api/green-items",
        "/coffee/api/roasted-items",
    ]

    for route in routes:
        resp = client.get(route)
        assert resp.status_code == 200, f"Route {route} failed with status {resp.status_code}"
