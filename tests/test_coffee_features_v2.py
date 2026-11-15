"""
tests/test_coffee_features_v2.py
فحوصات الميزات والتحسينات الجديدة لقسم القهوة:
1. الترقيم التسلسلي لسندات الصرف GB 0001
2. احتساب الأخياش التلقائي حسب وزن الخيشة
3. إمكانية البحث بالاسم عربي/إنجليزي/كود
4. استيراد الأصناف غير الموجودة وتخطي المكررة
5. إدخال الكمية الناتجة الفعلية في أمر التحميص
6. ترحيل سند صرف الأخضر بالبيع لمسودة فاتورة مبيعات في Xero بالمرجع GB 0001
7. تقارير أرصدة البن وجرد الأصناف
"""
import pytest
from datetime import date
from models import db, Location, Item, StockBalance, User
from models_coffee import (
    CoffeeItem, CoffeeItemLink, RoastingOrder, RoastingOrderLine,
    GreenCoffeeIssue, RoastedCoffeeDispatch, CoffeeSetting
)
from coffee_importer import import_coffee_items_from_rows
from coffee_xero import build_green_issue_sale_invoice_payload


def test_green_coffee_issue_doc_number(db_session):
    """التحقق من أن الترقيم يبدأ بـ GB 001 ويتزايد تسلسلياً (GB 00X)."""
    gw = Location(name_ar="مستودع البن الأخضر", name_en="Green Coffee Warehouse", type="warehouse")
    db_session.add(gw)
    db_session.flush()

    n1 = GreenCoffeeIssue.generate_next_doc_number()
    assert n1 == "GB-00001"
    
    item = Item(sku="GB-TEST", name_ar="بن تجريبي", storage_unit="كجم", cost=40.0)
    db_session.add(item)
    db_session.flush()
    ci = CoffeeItem(item_id=item.id, xero_item_code="GB TEST", prefix="GB", base_code="TEST", coffee_type="green", default_sack_weight=30.0)
    db_session.add(ci)
    db_session.flush()
    
    doc1 = GreenCoffeeIssue(
        doc_number=n1,
        issue_date=date.today(),
        warehouse_id=gw.id,
        issue_type="sale",
        coffee_item_id=ci.id,
        qty_kg=30.0,
        unit_cost=40.0,
        total_cost=1200.0,
        sale_price_per_kg=60.0,
        total_sale_amount=1800.0,
        customer_name="عميل تجريبي"
    )
    db_session.add(doc1)
    db_session.commit()
    
    n2 = GreenCoffeeIssue.generate_next_doc_number()
    assert n2 == "GB-00002"


def test_sack_calculation_and_search_text(db_session):
    """التحقق من احتساب الأخياش وخاصية search_text للبحث عربي وإنجليزي وكود."""
    item = Item(sku="GB-ETH-01", name_ar="إثيوبيا قوجي", name_en="Ethiopia Guji", storage_unit="كجم", cost=45.0)
    db_session.add(item)
    db_session.flush()
    
    ci = CoffeeItem(
        item_id=item.id,
        xero_item_code="GB ETH 01",
        prefix="GB",
        base_code="ETH 01",
        coffee_type="green",
        name_ar="إثيوبيا قوجي",
        name_en="Ethiopia Guji",
        sack_allowed=True,
        default_sack_weight=30.0,
        sale_price_per_kg=65.0
    )
    db_session.add(ci)
    db_session.commit()
    
    # 30 كيلو = 1 خيشة
    assert ci.calc_sacks_for_kg(30.0) == 1.0
    # 60 كيلو = 2 خيشة
    assert ci.calc_sacks_for_kg(60.0) == 2.0
    # 45 كيلو = 1.5 خيشة
    assert ci.calc_sacks_for_kg(45.0) == 1.5
    
    # البحث بالنص
    st = ci.search_text.lower()
    assert "gb eth 01" in st
    assert "إثيوبيا" in st
    assert "ethiopia" in st


def test_importer_skip_existing_items(db_session):
    """التحقق من أن استيراد الأصناف يضع فقط الأصناف غير الموجودة مع التنبيه وتخطي المكررة."""
    gw = Location(name_ar="مستودع البن الأخضر", name_en="Green Coffee Warehouse", type="warehouse")
    rw = Location(name_ar="مستودع البن المحمص", name_en="Roasted Coffee Warehouse", type="warehouse")
    db_session.add_all([gw, rw])
    db_session.flush()

    raw_rows = [
        ["GB ETH", "Ethiopia Guji", "", 30.0, "", 45.0, 60.0, "", 150.0],
        ["RB ETH", "Roasted Ethiopia", "", 0, "", 60.0, 90.0, "", 100.0],
    ]
    
    res1 = import_coffee_items_from_rows(raw_rows)
    assert res1["imported"] == 2
    assert res1["skipped"] == 0
    
    # استيراد نفس البيانات مرة أخرى
    res2 = import_coffee_items_from_rows(raw_rows)
    assert res2["imported"] == 0
    assert res2["skipped"] == 2
    skipped_codes = [x["code"] for x in res2["skipped_items"]]
    assert "GB ETH" in skipped_codes
    assert "RB ETH" in skipped_codes


def test_xero_sale_draft_invoice_payload(db_session):
    """التحقق من بناء فاتورة مبيعات مسودة في Xero لسند صرف الأخضر بالبيع مع رقم السند كمرجع."""
    gw = Location(name_ar="مستودع البن الأخضر", name_en="Green Coffee Warehouse", type="warehouse")
    db_session.add(gw)
    db_session.flush()

    item = Item(sku="GB-COL-01", name_ar="كولومبيا سوبريمو", storage_unit="كجم", cost=50.0)
    db_session.add(item)
    db_session.flush()
    ci = CoffeeItem(item_id=item.id, xero_item_code="GB COL 01", prefix="GB", base_code="COL 01", coffee_type="green", default_sack_weight=30.0)
    db_session.add(ci)
    db_session.flush()
    
    doc = GreenCoffeeIssue(
        doc_number="GB 0005",
        issue_date=date(2026, 10, 15),
        warehouse_id=gw.id,
        issue_type="sale",
        coffee_item_id=ci.id,
        qty_kg=60.0,
        unit_cost=50.0,
        total_cost=3000.0,
        sale_price_per_kg=75.0,
        total_sale_amount=4500.0,
        customer_name="مقهى النخبة"
    )
    db_session.add(doc)
    db_session.commit()
    
    payload = build_green_issue_sale_invoice_payload(doc)
    inv = payload
    assert inv["Type"] == "ACCREC"
    assert inv["Status"] == "DRAFT"
    assert inv["Reference"] == "GB 0005"
    assert inv["Contact"]["Name"] == "مقهى النخبة"
    line = inv["LineItems"][0]
    assert line["ItemCode"] == "GB COL 01"
    assert line["Quantity"] == 60.0
    assert line["UnitAmount"] == 75.0


def test_roasted_coffee_dispatch_doc_number(db_session):
    """التحقق من أن ترقيم صرف البن المحمص يبدأ بـ RB 0001 ويتزايد تسلسلياً."""
    rw = Location(name_ar="مستودع البن المحمص", name_en="Roasted Coffee Warehouse", type="warehouse")
    db_session.add(rw)
    db_session.flush()

    n1 = RoastedCoffeeDispatch.generate_next_doc_number()
    assert n1 == "RB-00001"

    item = Item(sku="RB-TEST", name_ar="بن محمص تجريبي", storage_unit="كجم", cost=60.0)
    db_session.add(item)
    db_session.flush()
    ci = CoffeeItem(item_id=item.id, xero_item_code="RB TEST", prefix="RB", base_code="TEST", coffee_type="roasted")
    db_session.add(ci)
    db_session.flush()

    doc1 = RoastedCoffeeDispatch(
        doc_number=n1,
        dispatch_date=date.today(),
        source_warehouse_id=rw.id,
        dispatch_type="sale",
        coffee_item_id=ci.id,
        qty_kg=25.0,
        unit_cost=60.0,
        total_cost=1500.0,
        sale_price_per_kg=90.0,
        total_sale_amount=2250.0,
        customer_name="عميل تجزئة"
    )
    db_session.add(doc1)
    db_session.commit()

    n2 = RoastedCoffeeDispatch.generate_next_doc_number()
    assert n2 == "RB-00002"


def test_roasted_sale_xero_draft_invoice(db_session):
    """التحقق من بناء فاتورة مبيعات مسودة في Xero لسند صرف البن المحمص بالبيع بالمرجع RB 000X."""
    from coffee_xero import build_roasted_issue_sale_invoice_payload

    rw = Location(name_ar="مستودع البن المحمص", name_en="Roasted Coffee Warehouse", type="warehouse")
    db_session.add(rw)
    db_session.flush()

    item = Item(sku="RB-ESP-01", name_ar="إسبريسو بلند محمص", storage_unit="كجم", cost=55.0)
    db_session.add(item)
    db_session.flush()
    ci = CoffeeItem(item_id=item.id, xero_item_code="RB ESP 01", prefix="RB", base_code="ESP 01", coffee_type="roasted")
    db_session.add(ci)
    db_session.flush()

    doc = RoastedCoffeeDispatch(
        doc_number="RB 0010",
        dispatch_date=date(2026, 10, 15),
        source_warehouse_id=rw.id,
        dispatch_type="sale",
        coffee_item_id=ci.id,
        qty_kg=40.0,
        unit_cost=55.0,
        total_cost=2200.0,
        sale_price_per_kg=85.0,
        total_sale_amount=3400.0,
        customer_name="فندق الأندلس"
    )
    db_session.add(doc)
    db_session.commit()

    payload = build_roasted_issue_sale_invoice_payload(doc)
    assert payload["Type"] == "ACCREC"
    assert payload["Status"] == "DRAFT"
    assert payload["Reference"] == "RB 0010"
    assert payload["Contact"]["Name"] == "فندق الأندلس"
    line = payload["LineItems"][0]
    assert line["ItemCode"] == "RB ESP 01"
    assert line["Quantity"] == 40.0
    assert line["UnitAmount"] == 85.0


def test_roasting_order_post_creates_order_and_saves_lines(admin_client, db_session):
    """التحقق من حفظ أمر التحميص بنجاح عند إرسال النموذج وحفظ الأصناف والكميات الناتجة."""
    gw = Location(name_ar="مستودع البن الأخضر", name_en="Green Coffee Warehouse", type="warehouse")
    rw = Location(name_ar="مستودع البن المحمص", name_en="Roasted Coffee Warehouse", type="warehouse")
    db_session.add_all([gw, rw])
    db_session.flush()

    item_g = Item(sku="GB-TEST-SAVE", name_ar="بن أخضر تجريبي", storage_unit="كجم", cost=45.0)
    item_r = Item(sku="RB-TEST-SAVE", name_ar="بن محمص تجريبي", storage_unit="كجم", cost=65.0)
    db_session.add_all([item_g, item_r])
    db_session.flush()

    ci_g = CoffeeItem(item_id=item_g.id, xero_item_code="GB SAVE", prefix="GB", base_code="SAVE", coffee_type="green", default_sack_weight=60.0)
    ci_r = CoffeeItem(item_id=item_r.id, xero_item_code="RB SAVE", prefix="RB", base_code="SAVE", coffee_type="roasted")
    db_session.add_all([ci_g, ci_r])
    db_session.commit()

    # إرسال النموذج بالمدخلات
    post_data = {
        "order_date": "2026-10-15",
        "order_time": "10:30",
        "reference_number": "REF-101",
        "roasting_supervisor": "أحمد الحماص",
        "green_item_id[]": [str(ci_g.id)],
        "roasted_item_id[]": [str(ci_r.id)],
        "green_qty_kg[]": ["60.0"],
        "green_qty_sacks[]": ["1.0"],
        "green_sack_weight[]": ["60.0"],
        "roasted_qty_kg[]": ["50.0"],
        "link_type[]": ["auto"],
        "manual_link_reason[]": [""],
        "line_notes[]": ["تشغيلة أولى"]
    }

    res = admin_client.post("/coffee/roasting/new", data=post_data, follow_redirects=True)
    assert res.status_code == 200

    # التحقق من إنشاء الأمر في قاعدة البيانات
    order = RoastingOrder.query.filter_by(reference_number="REF-101").first()
    assert order is not None
    assert order.status == "draft"
    assert len(order.lines) == 1
    assert order.lines[0].green_item_id == ci_g.id
    assert order.lines[0].green_qty_kg == 60.0
    assert order.lines[0].roasted_qty_kg_actual == 50.0


def test_green_issue_post_creates_doc_with_unified_field(admin_client, db_session):
    """التحقق من إنشاء سند صرف بن أخضر جديد عبر الحقل الموحد وحساب التكلفة والترقيم."""
    gw = Location(name_ar="مستودع البن الأخضر", name_en="Green Coffee Warehouse", type="warehouse")
    db_session.add(gw)
    db_session.flush()

    item_g = Item(sku="GB-TEST-ISSUE", name_ar="بن أخضر للصرف", storage_unit="كجم", cost=40.0)
    db_session.add(item_g)
    db_session.flush()

    ci_g = CoffeeItem(item_id=item_g.id, xero_item_code="GB ISSUE", prefix="GB", base_code="ISSUE", coffee_type="green", default_sack_weight=30.0, sale_price_per_kg=70.0)
    db_session.add(ci_g)
    
    from models import record_stock_movement
    record_stock_movement(item_g.id, gw.id, "initial", 100.0, notes="رصيد افتتاحي")
    db_session.commit()

    post_data = {
        "issue_date": "2026-10-15",
        "issue_time": "11:00",
        "issue_type": "sale",
        "customer_name": "مقهى النجوم",
        "sale_price_per_kg": "70.0",
        "coffee_item_id": str(ci_g.id),
        "qty_kg": "30.0",
        "qty_sacks": "1.0",
        "sack_weight": "30.0",
        "issue_reason": "بيع مباشر لمقهى النجوم",
        "notes": "سند بيع خام"
    }

    res = admin_client.post("/coffee/green/issues/new", data=post_data, follow_redirects=True)
    assert res.status_code == 200

    doc = GreenCoffeeIssue.query.filter_by(customer_name="مقهى النجوم").first()
    assert doc is not None
    assert doc.doc_number.startswith("GB-")
    assert doc.qty_kg == 30.0
    assert doc.total_sale_amount == 2100.0


def test_roasting_order_approve_green_renders_edit_page(admin_client, db_session):
    """التحقق من اعتماد صرف الأخضر للأمر وعرض شاشة تعديل الأمر دون أخطاء Jinja2."""
    from models import record_stock_movement
    gw = Location(name_ar="مستودع البن الأخضر", name_en="Green Coffee Warehouse", type="warehouse")
    rw = Location(name_ar="مستودع البن المحمص", name_en="Roasted Coffee Warehouse", type="warehouse")
    db_session.add_all([gw, rw])
    db_session.flush()

    item_g = Item(sku="GB-APP-01", name_ar="بن أخضر للاعتماد", storage_unit="كجم", cost=50.0)
    item_r = Item(sku="RB-APP-01", name_ar="بن محمص للاعتماد", storage_unit="كجم", cost=70.0)
    db_session.add_all([item_g, item_r])
    db_session.flush()

    ci_g = CoffeeItem(item_id=item_g.id, xero_item_code="GB APP 01", prefix="GB", base_code="APP 01", coffee_type="green", default_sack_weight=60.0)
    ci_r = CoffeeItem(item_id=item_r.id, xero_item_code="RB APP 01", prefix="RB", base_code="APP 01", coffee_type="roasted")
    db_session.add_all([ci_g, ci_r])
    db_session.flush()

    # رصيد في المستودع
    record_stock_movement(item_g.id, gw.id, "initial", 100.0, notes="رصيد أولي")

    order = RoastingOrder(
        order_number="RO-APP-01",
        order_date=date(2026, 10, 15),
        green_warehouse_id=gw.id,
        roasted_warehouse_id=rw.id,
        status="draft"
    )
    db_session.add(order)
    db_session.flush()

    line = RoastingOrderLine(
        order_id=order.id,
        line_number=1,
        green_item_id=ci_g.id,
        roasted_item_id=ci_r.id,
        green_qty_kg=50.0,
        green_unit_cost=50.0,
        green_total_cost=2500.0,
        link_type="auto"
    )
    db_session.add(line)
    db_session.commit()

    # اعتماد صرف الأخضر
    res = admin_client.post(f"/coffee/roasting/{order.id}/approve-green", follow_redirects=True)
    assert res.status_code == 200
    assert "يمكن بدء التحميص" in res.get_data(as_text=True)

    db_session.refresh(order)
    assert order.status == "approved_green"



