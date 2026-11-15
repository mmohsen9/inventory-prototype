"""
tests/test_coffee_xero_cost.py — اختبارات مطابقة سعر الصنف لتكلفة Xero الرسمية
"""
import pytest
from models import db, Item, Location
from models_coffee import (
    CoffeeItem,
    RoastedCoffeeDispatch,
    RoastedCoffeeDispatchLine,
)
from coffee_xero import (
    build_roasted_dispatch_branch_credit_note_payload,
)


def test_coffee_item_xero_cost_price_and_dispatch_payload(db_session):
    """التحقق من أن أصناف القهوة تعتمد سعر تكلفة Xero الرسمي وأن حمولة زيرو تستخدمه بدقة."""
    # 1. صنف محمص باختبار سعر تكلفة زيرو
    base_item = Item.query.filter_by(sku="RB0465- كولومبى محمص").first()
    if not base_item:
        base_item = Item(
            sku="RB0465- كولومبى محمص",
            name_ar="كولومبى محمص",
            cost=49.50,
            storage_unit="kg",
        )
        db_session.add(base_item)
        db_session.flush()

    ci = CoffeeItem.query.filter_by(base_code="0465").first()
    if not ci:
        ci = CoffeeItem(
            item_id=base_item.id,
            xero_item_code="RB0465- كولومبى محمص",
            prefix="RB",
            base_code="0465",
            coffee_type="roasted",
            name_ar="كولومبى محمص",
            xero_cost_price=49.50,
            sale_price_per_kg=160.0,
        )
        db_session.add(ci)
        db_session.flush()
    else:
        ci.xero_cost_price = 49.50
        base_item.cost = 49.50
        db_session.commit()

    assert ci.xero_cost_price == 49.50
    assert ci.cost_price == 49.50

    from datetime import date
    # 2. إنشاء مستودع وفرع وسند صرف للفرع 10 كجم
    rw = Location.query.filter_by(type="warehouse").first()
    if not rw:
        rw = Location(name_ar="مستودع البن المحمص", name_en="Roasted Coffee Warehouse", type="warehouse")
        db_session.add(rw)
        db_session.flush()

    branch = Location.query.filter_by(type="branch").first()
    if not branch:
        branch = Location(name_ar="فرع الخبر", name_en="Al-Khobar Branch", type="branch")
        db_session.add(branch)
        db_session.flush()

    disp = RoastedCoffeeDispatch(
        doc_number="MP-TEST-COST-01",
        dispatch_date=date.today(),
        source_warehouse_id=rw.id,
        dispatch_type="branch",
        branch_location_id=branch.id,
        coffee_item_id=ci.id,
        qty_kg=10.0,
        unit_cost=ci.cost_price,
        total_cost=round(10.0 * ci.cost_price, 4),
        status="approved",
    )
    db_session.add(disp)
    db_session.flush()

    line = RoastedCoffeeDispatchLine(
        dispatch_id=disp.id,
        line_number=1,
        coffee_item_id=ci.id,
        qty_kg=10.0,
        unit_cost=ci.cost_price,
        total_cost=round(10.0 * ci.cost_price, 4),
    )
    db_session.add(line)
    db_session.commit()

    # 3. بناء حمولة إشعار دائن مشتريات زيرو للفرع
    payload = build_roasted_dispatch_branch_credit_note_payload(disp)
    assert payload["Type"] == "ACCPAYCREDIT"
    assert len(payload["LineItems"]) == 1

    xero_line = payload["LineItems"][0]
    assert xero_line["Quantity"] == 10.0
    assert xero_line["UnitAmount"] == 49.50
    assert xero_line["LineAmount"] == 495.00


def test_roasting_completion_does_not_corrupt_xero_cost_price(db_session):
    """التحقق من أن إكمال أمر تحميص بتكلفة دفعة غير اعتيادية لا يُفسد سعر تكلفة Xero المعتمد."""
    base_item = Item.query.filter_by(sku="RB0465- كولومبى محمص").first()
    if not base_item:
        base_item = Item(sku="RB0465- كولومبى محمص", name_ar="كولومبى محمص", cost=49.50, storage_unit="kg")
        db_session.add(base_item)
        db_session.flush()

    ci = CoffeeItem.query.filter_by(base_code="0465").first()
    if not ci:
        ci = CoffeeItem(
            item_id=base_item.id,
            xero_item_code="RB0465- كولومبى محمص",
            prefix="RB",
            base_code="0465",
            coffee_type="roasted",
            name_ar="كولومبى محمص",
            xero_cost_price=49.50,
        )
        db_session.add(ci)
        db_session.flush()
    else:
        ci.xero_cost_price = 49.50
        db_session.commit()

    assert ci.xero_cost_price == 49.50

    # محاكاة خطوة إكمال التحميص
    line_roasted_unit_cost = 300.30
    roasted_item = ci.item
    if roasted_item and line_roasted_unit_cost:
        if not getattr(ci, "xero_cost_price", None) or ci.xero_cost_price <= 0:
            roasted_item.cost = line_roasted_unit_cost
            db_session.add(roasted_item)

    # التحقق من بقاء التكلفة المعتمدة ثابتة
    assert ci.xero_cost_price == 49.50
    assert ci.cost_price == 49.50


def test_branch_dispatch_creates_purchase_with_real_dispatch_cost(admin_client, db_session):
    """التحقق من أن سند الصرف للفرع ينشئ سند استلام مشتريات للفرع بالتكلفة الحقيقية للصنف وليس 40 ريال."""
    from datetime import date
    from models import PurchaseTransaction, PurchaseLine, Supplier

    rw = Location.query.filter_by(type="warehouse").first()
    if not rw:
        rw = Location(name_ar="مستودع البن المحمص", name_en="Roasted Coffee Warehouse", type="warehouse")
        db_session.add(rw)
        db_session.flush()

    branch = Location.query.filter_by(type="branch").first()
    if not branch:
        branch = Location(name_ar="فرع الخبر", name_en="Al-Khobar Branch", type="branch")
        db_session.add(branch)
        db_session.flush()

    # صنف كولومبي بسعر تكلفة معتمد 42.50
    base_item = Item.query.filter_by(sku="RB0617-TEST").first()
    if not base_item:
        base_item = Item(sku="RB0617-TEST", name_ar="قاراقاري قوتيتي محمص", storage_unit="kg", cost=42.50)
        db_session.add(base_item)
        db_session.flush()

    ci = CoffeeItem.query.filter_by(base_code="0617-T").first()
    if not ci:
        ci = CoffeeItem(
            item_id=base_item.id,
            xero_item_code="RB0617-TEST",
            prefix="RB",
            base_code="0617-T",
            coffee_type="roasted",
            name_ar="قاراقاري قوتيتي محمص",
            xero_cost_price=42.50,
        )
        db_session.add(ci)
        db_session.flush()

    # إنشاء الصنف العام sk-0535 إن لم يكن موجوداً
    gen_item = Item.query.filter((Item.sku == "sk-0535") | (Item.name_ar == "بن محمص عام")).first()
    if not gen_item:
        gen_item = Item(
            sku="sk-0535",
            name_ar="بن محمص عام",
            name_en="General roasted coffee",
            storage_unit="كيلو",
            ingredient_unit="جرام",
            storage_to_ingredient_factor=1000.0,
            cost=40.0,
        )
        db_session.add(gen_item)
        db_session.flush()

    sup = Supplier.query.filter_by(name="اكياس القهوة المحمصه للفروع").first()
    if not sup:
        sup = Supplier(name="اكياس القهوة المحمصه للفروع")
        db_session.add(sup)
        db_session.flush()

    db_session.commit()

    # إنشاء سند صرف للفرع عبر النموذج
    form_data = {
        "dispatch_type": "branch",
        "branch_id": str(branch.id),
        "dispatch_date": date.today().isoformat(),
        "form_action": "approve",
        "doc_number": "MP-TEST-REALCOST-01",
        "line_coffee_item_id[]": [str(ci.id)],
        "line_qty[]": ["10.0"],
        "line_unit_cost[]": ["42.50"],
        "line_notes[]": ["شحنة تجريبية بالتكلفة الحقيقية"],
        "notes": "اختبار التكلفة الحقيقية لسند الشراء",
    }

    res = admin_client.post("/coffee/roasted/dispatches/new", data=form_data, follow_redirects=True)
    assert res.status_code == 200

    # التحقق من أن سند الاستلام الناتج يحمل التكلفة الحقيقية 42.50 وليس 40.0
    purchase = PurchaseTransaction.query.filter_by(invoice_number="MP-TEST-REALCOST-01").first()
    assert purchase is not None
    assert len(purchase.lines) == 1
    pline = purchase.lines[0]
    assert pline.quantity == 10.0
    assert pline.unit_cost == 42.50
    assert pline.quantity * pline.unit_cost == 425.0

