# -*- coding: utf-8 -*-
"""
اختبارات ميزة صرف أكياس التجزئة (250g / 1kg / بالكيلو) في سندات صرف البن المحمص (سلسلة MP)
والتكامل بين خصم مستودع المحمصة بالكيلو وسند استلام فوديكس بعدد الأكياس وترحيل زيرو.
"""
import pytest
from datetime import date
from models import (
    db, Item, Supplier, Location, User, PurchaseTransaction, PurchaseLine, StockMovement
)
from models_coffee import (
    CoffeeItem,
    RoastedCoffeeDispatch,
    RoastedCoffeeDispatchLine,
    CoffeeRetailPackage,
    ensure_coffee_retail_packages_seed
)
from coffee_xero import build_roasted_dispatch_branch_credit_note_payload


@pytest.fixture
def coffee_setup(db_session, test_data):
    """تهيئة الأصناف المحمصة وأصناف التجزئة المقابلة لها في قاعدة البيانات الاختبارية."""
    # 1. صنف محمص اريكا
    base_erika = Item(
        sku="RB0619-  اريكا ووبنشي محمص",
        name_ar="اريكا ووبنشي محمص",
        cost=42.50,
        storage_unit="kg"
    )
    # صنف تجزئة اريكا 250g في فوديكس
    retail_erika = Item(
        sku="sk-0668",
        name_ar="اريكا 250 g",
        cost=10.625,
        storage_unit="كيس",
        foodics_id="foodics-sku-0668"
    )

    # 2. صنف محمص توليما
    base_tolima = Item(
        sku="RB0465- كولومبى محمص",
        name_ar="كولومبى محمص",
        cost=49.50,
        storage_unit="kg"
    )
    # صنف تجزئة توليما 250g في فوديكس
    retail_tolima = Item(
        sku="sk-0512",
        name_ar="توليما كولومبي",
        cost=12.375,
        storage_unit="كيس",
        foodics_id="foodics-sku-0512"
    )

    # 3. صنف بن محمص عام sk-0535
    gen_roasted = Item(
        sku="sk-0535",
        name_ar="بن محمص عام",
        cost=40.0,
        storage_unit="كيلو",
        foodics_id="foodics-sku-0535"
    )

    db_session.add_all([base_erika, retail_erika, base_tolima, retail_tolima, gen_roasted])
    db_session.flush()

    ci_erika = CoffeeItem(
        item_id=base_erika.id,
        xero_item_code="RB0619-  اريكا ووبنشي محمص",
        prefix="RB",
        base_code="0619",
        coffee_type="roasted",
        name_ar="اريكا ووبنشي محمص",
        xero_cost_price=42.50,
        sale_price_per_kg=140.0,
    )
    ci_tolima = CoffeeItem(
        item_id=base_tolima.id,
        xero_item_code="RB0465- كولومبى محمص",
        prefix="RB",
        base_code="0465",
        coffee_type="roasted",
        name_ar="كولومبى محمص",
        xero_cost_price=49.50,
        sale_price_per_kg=160.0,
    )
    db_session.add_all([ci_erika, ci_tolima])
    db_session.flush()

    # إنشاء مستودع محمص رئيسي
    rw = Location.query.filter_by(type="warehouse").first()
    if not rw:
        rw = Location(name_ar="مستودع البن المحمص", name_en="Roasted Coffee Warehouse", type="warehouse", code="WH-ROAST")
        db_session.add(rw)
        db_session.flush()

    db_session.commit()

    # تشغيل عملية زرع وتطابق التجزئة
    ensure_coffee_retail_packages_seed(db_session)

    return {
        "ci_erika": ci_erika,
        "retail_erika": retail_erika,
        "ci_tolima": ci_tolima,
        "retail_tolima": retail_tolima,
        "gen_roasted": gen_roasted,
        "rw": rw,
    }


def test_seed_coffee_retail_packages(coffee_setup):
    """التحقق من زرع وتطابق أصناف التجزئة الـ 250g في جدول coffee_retail_packages."""
    ci_erika = coffee_setup["ci_erika"]
    pkg_erika = ci_erika.get_retail_package("250g")
    assert pkg_erika is not None
    assert pkg_erika.weight_kg == 0.25
    assert pkg_erika.foodics_item.sku == "sk-0668"

    ci_tolima = coffee_setup["ci_tolima"]
    pkg_tolima = ci_tolima.get_retail_package("250g")
    assert pkg_tolima is not None
    assert pkg_tolima.weight_kg == 0.25
    assert pkg_tolima.foodics_item.sku == "sk-0512"


def test_dispatch_new_with_retail_package(client, coffee_setup):
    """
    اختبار صرف أكياس تجزئة 250g لفرع:
    - خصم مستودع المحمصة يكون بالوزن الكلي (كجم = عدد الأكياس * 0.25)
    - سند استلام فوديكس للفرع يسجل بصنف التجزئة المقابل، والكمية = عدد الأكياس
    - تكلفة الوحدة بسند الفرع = ربع تكلفة الكيلو بالضبط
    """
    ci = coffee_setup["ci_erika"]
    retail_item = coffee_setup["retail_erika"]
    rw = coffee_setup["rw"]
    branch = Location.get_operational_branches()[0]

    admin = User.query.filter_by(role="admin").first()

    with client.session_transaction() as sess:
        sess["user_id"] = admin.id
        sess["role"] = "admin"

    # إرسال 20 كيس ربع كيلو (تساوي 5.0 كجم)
    bag_count = 20
    total_kg = bag_count * 0.25

    resp = client.post("/coffee/roasted/dispatches/new", data={
        "dispatch_date": date.today().isoformat(),
        "dispatch_time": "11:00",
        "dispatch_type": "branch",
        "branch_location_id": str(branch.id),
        "line_coffee_item_id[]": [str(ci.id)],
        "line_qty_kg[]": [str(total_kg)],
        "line_package_type[]": ["250g"],
        "line_package_qty[]": [str(bag_count)],
        "line_package_weight_kg[]": ["0.25"],
        "line_foodics_item_id[]": [str(retail_item.id)],
        "line_notes[]": ["طلبية تجزئة للفرع"],
        "form_action": "approve"
    }, follow_redirects=True)

    assert resp.status_code == 200

    # التحقق من إنشاء سند الصرف
    dispatch = RoastedCoffeeDispatch.query.filter_by(
        branch_location_id=branch.id,
        dispatch_type="branch"
    ).order_by(RoastedCoffeeDispatch.id.desc()).first()

    assert dispatch is not None
    assert abs(dispatch.qty_kg - total_kg) < 0.001
    assert len(dispatch.lines) == 1

    line = dispatch.lines[0]
    assert line.package_type == "250g"
    assert line.package_qty == bag_count
    assert line.package_weight_kg == 0.25
    assert line.foodics_item_id == retail_item.id
    assert "20 كيس (250g)" in line.package_label_ar

    # التحقق من خصم المحمصة بالكيلو
    wh_mv = StockMovement.query.filter_by(
        reference_type="roasted_dispatch",
        reference_id=dispatch.id,
        location_id=dispatch.source_warehouse_id
    ).first()
    assert wh_mv is not None
    assert abs(wh_mv.quantity_change - (-total_kg)) < 0.001

    # التحقق من إنشاء سند استلام المشتريات للفرع بصنف التجزئة
    purchase = PurchaseTransaction.query.filter(
        (PurchaseTransaction.foodics_reference == dispatch.doc_number) |
        (PurchaseTransaction.invoice_number == dispatch.doc_number)
    ).first()

    assert purchase is not None
    assert purchase.location_id == branch.id
    assert len(purchase.lines) == 1

    p_line = purchase.lines[0]
    assert p_line.item_id == retail_item.id    # صنف التجزئة sk-0668
    assert p_line.quantity == bag_count        # 20 كيس
    expected_unit_cost = round(ci.cost_price * 0.25, 4)
    assert abs(p_line.unit_cost - expected_unit_cost) < 0.001
    # إجمالي مبلغ البند يطابق إجمالي تكلفة الصرف
    assert abs((p_line.quantity * p_line.unit_cost) - line.total_cost) < 0.05

    # التحقق من حركة الوارد للفرع بصنف التجزئة بعدد الأكياس
    br_mv = StockMovement.query.filter_by(
        reference_type="purchase",
        reference_id=purchase.id,
        item_id=retail_item.id
    ).first()
    assert br_mv is not None
    assert br_mv.quantity_change == bag_count


def test_dispatch_new_bulk_kg(client, coffee_setup):
    """
    اختبار صرف بن محمص عام بالكيلو (حبوب) لفرع:
    - يجب أن يسجل بسند استلام فوديكس بالصنف العام [sk-0535 بن محمص عام] وبالكيلو
    """
    ci = coffee_setup["ci_erika"]
    branch = Location.get_operational_branches()[0]
    admin = User.query.filter_by(role="admin").first()

    with client.session_transaction() as sess:
        sess["user_id"] = admin.id
        sess["role"] = "admin"

    bulk_kg = 12.5

    resp = client.post("/coffee/roasted/dispatches/new", data={
        "dispatch_date": date.today().isoformat(),
        "dispatch_time": "11:00",
        "dispatch_type": "branch",
        "branch_location_id": str(branch.id),
        "line_coffee_item_id[]": [str(ci.id)],
        "line_qty_kg[]": [str(bulk_kg)],
        "line_package_type[]": ["bulk"],
        "line_package_qty[]": [str(bulk_kg)],
        "line_package_weight_kg[]": ["1.0"],
        "line_foodics_item_id[]": [""],
        "form_action": "approve"
    }, follow_redirects=True)

    assert resp.status_code == 200

    dispatch = RoastedCoffeeDispatch.query.filter_by(
        branch_location_id=branch.id,
        dispatch_type="branch"
    ).order_by(RoastedCoffeeDispatch.id.desc()).first()

    assert dispatch is not None
    assert abs(dispatch.qty_kg - bulk_kg) < 0.001

    purchase = PurchaseTransaction.query.filter(
        (PurchaseTransaction.foodics_reference == dispatch.doc_number) |
        (PurchaseTransaction.invoice_number == dispatch.doc_number)
    ).first()

    assert purchase is not None
    assert len(purchase.lines) == 1
    p_line = purchase.lines[0]
    # الصنف العام بن محمص عام sk-0535
    assert p_line.item.sku == "sk-0535" or p_line.item.name_ar == "بن محمص عام"
    assert abs(p_line.quantity - bulk_kg) < 0.001


def test_xero_credit_note_payload_description_with_packaging(coffee_setup):
    """
    التحقق من أن payload الإشعار الدائن لـ Xero يذكر عدد الأكياس في البيان
    مع الحفاظ على الكود الرسمي RB والوزن بالكيلو والسعر الدقيق.
    """
    ci = coffee_setup["ci_erika"]
    rw = coffee_setup["rw"]
    branch = Location.get_operational_branches()[0]

    dispatch = RoastedCoffeeDispatch(
        doc_number="MP-TEST-PKG-01",
        dispatch_date=date.today(),
        dispatch_time="11:00",
        source_warehouse_id=rw.id,
        branch_location_id=branch.id,
        dispatch_type="branch",
        qty_kg=5.0,
        unit_cost=ci.cost_price,
        total_cost=round(5.0 * ci.cost_price, 4),
        coffee_item_id=ci.id,
        status="approved"
    )
    db.session.add(dispatch)
    db.session.flush()

    line = RoastedCoffeeDispatchLine(
        dispatch_id=dispatch.id,
        line_number=1,
        coffee_item_id=ci.id,
        qty_kg=5.0,
        unit_cost=ci.cost_price,
        total_cost=round(5.0 * ci.cost_price, 4),
        package_type="250g",
        package_qty=20.0,
        package_weight_kg=0.25,
        notes="طلب الفرع"
    )
    db.session.add(line)
    db.session.commit()

    payload = build_roasted_dispatch_branch_credit_note_payload(dispatch)
    assert payload is not None
    assert len(payload["LineItems"]) == 1
    xline = payload["LineItems"][0]

    # كود الصنف الرسمي في زيرو يظل RB0619
    assert xline["ItemCode"] == ci.xero_item_code
    # الكمية في زيرو بالكيلو
    assert xline["Quantity"] == 5.0
    # سعر الكيلو يظل كاملاً
    assert xline["UnitAmount"] == ci.cost_price
    # البيان (Description) يذكر صراحة عدد الأكياس
    assert "20 كيس ربع كيلو 250g" in xline["Description"]


def test_dispatch_edit_with_retail_package(client, coffee_setup):
    """
    التحقق من تعديل سند صرف قائم لفرع وتحديث أصناف وأكياس التجزئة:
    - تحديث السند وخصم المخزون
    - تحديث سند استلام فوديكس للفرع بالصنف الجديد والكمية بالأكياس
    """
    ci_tolima = coffee_setup["ci_tolima"]
    retail_tolima = coffee_setup["retail_tolima"]
    branch = Location.get_operational_branches()[0]
    admin = User.query.filter_by(role="admin").first()

    with client.session_transaction() as sess:
        sess["user_id"] = admin.id
        sess["role"] = "admin"

    # 1. إنشاء سند أولي
    resp = client.post("/coffee/roasted/dispatches/new", data={
        "dispatch_date": date.today().isoformat(),
        "dispatch_time": "11:00",
        "dispatch_type": "branch",
        "branch_location_id": str(branch.id),
        "line_coffee_item_id[]": [str(ci_tolima.id)],
        "line_qty_kg[]": ["2.5"],
        "line_package_type[]": ["bulk"],
        "line_package_qty[]": ["2.5"],
        "line_package_weight_kg[]": ["1.0"],
        "line_foodics_item_id[]": [""],
        "form_action": "approve"
    }, follow_redirects=True)
    assert resp.status_code == 200

    dispatch = RoastedCoffeeDispatch.query.filter_by(
        branch_location_id=branch.id,
        dispatch_type="branch"
    ).order_by(RoastedCoffeeDispatch.id.desc()).first()
    assert dispatch is not None

    # 2. تعديل السند إلى 16 كيس ربع كيلو (4.0 كجم)
    new_bag_count = 16
    new_total_kg = new_bag_count * 0.25

    edit_resp = client.post(f"/coffee/roasted/dispatches/{dispatch.id}/edit", data={
        "dispatch_date": date.today().isoformat(),
        "dispatch_time": "12:00",
        "doc_number": dispatch.doc_number,
        "dispatch_type": "branch",
        "branch_location_id": str(branch.id),
        "line_coffee_item_id[]": [str(ci_tolima.id)],
        "line_qty_kg[]": [str(new_total_kg)],
        "line_package_type[]": ["250g"],
        "line_package_qty[]": [str(new_bag_count)],
        "line_package_weight_kg[]": ["0.25"],
        "line_foodics_item_id[]": [str(retail_tolima.id)],
        "line_notes[]": ["تعديل لـ 16 كيس ربع كيلو"],
        "form_action": "approve"
    }, follow_redirects=True)
    assert edit_resp.status_code == 200

    # التحقق بعد التعديل
    db.session.refresh(dispatch)
    assert abs(dispatch.qty_kg - new_total_kg) < 0.001
    assert len(dispatch.lines) == 1
    assert dispatch.lines[0].package_type == "250g"
    assert dispatch.lines[0].package_qty == new_bag_count
    assert dispatch.lines[0].foodics_item_id == retail_tolima.id

    # التحقق من سند الاستلام للفرع
    purchase = PurchaseTransaction.query.filter(
        (PurchaseTransaction.foodics_reference == dispatch.doc_number) |
        (PurchaseTransaction.invoice_number == dispatch.doc_number)
    ).first()
    assert purchase is not None
    assert len(purchase.lines) == 1
    assert purchase.lines[0].item_id == retail_tolima.id
    assert purchase.lines[0].quantity == new_bag_count


def test_accountant_can_view_approved_dispatch_and_cannot_modify(client, coffee_setup, test_data):
    """التحقق من أن المحاسب يستطيع فتح وعرض تفاصيل السند المعتمد (View Mode) ويُمنع من تعديله أو حفظه."""
    branch = test_data["br1"]
    wh = test_data["wh"]
    ci_tolima = coffee_setup["ci_tolima"]

    # إنشاء سند معتمد
    dispatch = RoastedCoffeeDispatch(
        doc_number="MP-TEST-ACC",
        dispatch_date=date.today(),
        dispatch_type="branch",
        source_warehouse_id=wh.id,
        branch_location_id=branch.id,
        coffee_item_id=ci_tolima.id,
        qty_kg=5.0,
        unit_cost=49.50,
        total_cost=247.50,
        status="approved"
    )
    db.session.add(dispatch)
    db.session.commit()

    # إنشاء مستخدم محاسب
    accountant = User.query.filter_by(role="accountant").first()
    if not accountant:
        accountant = User(name="محاسب تجريبي", email="acc@example.com", role="accountant", active=True)
        db.session.add(accountant)
        db.session.commit()

    # تسجيل الدخول كمحاسب
    with client.session_transaction() as sess:
        sess["user_id"] = accountant.id
        sess["role"] = "accountant"

    # 1. المحاسب يستطيع فتح السند المعتمد واستعراضه بنجاح
    view_resp = client.get(f"/coffee/roasted/dispatches/{dispatch.id}/edit")
    assert view_resp.status_code == 200
    html = view_resp.data.decode("utf-8")
    assert "MP-TEST-ACC" in html
    assert "وضع العرض والاطلاع" in html
    assert "عرض تفاصيل سند صرف بن محمص" in html
    assert "disabled" in html  # الحقول مقفلة للمطالعة

    # 2. قائمة السندات تظهر زر "عرض" للمحاسب
    list_resp = client.get("/coffee/roasted/dispatches/")
    assert list_resp.status_code == 200
    list_html = list_resp.data.decode("utf-8")
    assert "عرض" in list_html

    # 3. محاولة التعديل أو الحفظ (POST) من قبل المحاسب يتم رفضها وإرجاعه للقائمة مع رسالة منع أمنية
    post_resp = client.post(
        f"/coffee/roasted/dispatches/{dispatch.id}/edit",
        data={"notes": "محاولة تعديل غير مصرحة"},
        follow_redirects=True
    )
    post_html = post_resp.data.decode("utf-8")
    assert "تعديل وحفظ السندات المعتمدة متاح حصرياً لمدير النظام" in post_html


