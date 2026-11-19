import json
import pytest
from datetime import datetime, date
from models import db, Location, Item, StockBalance, record_stock_movement, Notification, User
from models_coffee import (
    CoffeeItem, RoastedCoffeeDispatch, CoffeeCustomer,
    SallaItemMapping, SallaOrderQueue, ensure_salla_schema
)
from coffee_salla import process_salla_webhook_payload, execute_salla_daily_consolidation
from coffee_xero import build_roasted_issue_sale_invoice_payload


@pytest.fixture(autouse=True)
def init_test_salla_db(app, db_session):
    """تهيئة قاعدة بيانات الفحص والتأكد من زرع جداول وعميل سلة."""
    with app.app_context():
        ensure_salla_schema()


def test_salla_mappings_seeded(db_session):
    """التحقق من زرع أصناف سلة الـ 17 والعميل الافتراضي بدقة."""
    mappings = SallaItemMapping.query.all()
    assert len(mappings) >= 17

    # التحقق من العميل
    cust = CoffeeCustomer.query.filter_by(name="عملاء موقع سلة Salla website").first()
    assert cust is not None

    # التحقق من وجود أصناف القهوة
    colombia = SallaItemMapping.query.filter_by(salla_product_name="كولمبيا توليمــا").first()
    assert colombia is not None
    assert colombia.weight_kg == 0.25
    assert colombia.source_warehouse == "roasted"
    assert colombia.xero_account_code == "203"

    # التحقق من الأدوات
    chemex = SallaItemMapping.query.filter_by(salla_product_name="اداة الكيمكس - 3 اكواب").first()
    assert chemex is not None
    assert chemex.item_type == "tool"
    assert chemex.source_warehouse == "main"
    assert chemex.weight_kg == 0.0

    # التحقق من بكج نوفمبر بلند
    blend = SallaItemMapping.query.filter_by(salla_product_name="نوفمبر بلند").all()
    assert len(blend) == 2
    assert sum(b.weight_kg for b in blend) == 0.25


def test_salla_webhook_new_product_alert(db_session):
    """التحقق من إنشاء تنبيه فوري عند وصول حدث إضافة منتج جديد في سلة."""
    payload = {
        "event": "product.created",
        "data": {
            "id": "999888777",
            "name": "محصول يمني جديد حراز 250g"
        }
    }
    res = process_salla_webhook_payload(payload)
    assert res.get("success") is True
    assert res.get("action") == "notification_created"

    notif = Notification.query.filter_by(type="salla_new_product", item_sku="999888777").first()
    assert notif is not None
    assert "حراز" in notif.message
    assert notif.is_read is False


def test_salla_webhook_order_with_unmapped_item_alert(db_session):
    """التحقق من تعليق الطلب وتوليد تنبيه عند احتواء طلب سلة على صنف غير مطابق."""
    order_id = "SL-TEST-UNMAPPED-101"
    payload = {
        "event": "order.created",
        "data": {
            "id": order_id,
            "reference_id": "ORD-501",
            "total": {"amount": 120.0},
            "shipping_cost": {"amount": 25.0},
            "customer": {
                "name": "عبدالله محمد",
                "mobile": "0555112233"
            },
            "items": [
                {
                    "product_id": "888777666",
                    "name": "كوب سيراميك نوفمبر مميز",
                    "quantity": 1
                }
            ]
        }
    }
    res = process_salla_webhook_payload(payload)
    assert res.get("success") is True
    assert res.get("has_unmapped") is True

    # التحقق من تخزين الطلب كمعلق
    ord = SallaOrderQueue.query.filter_by(salla_order_id=order_id).first()
    assert ord is not None
    assert ord.status == "pending_mapping"
    assert ord.has_unmapped_items is True
    assert "كوب سيراميك" in ord.unmapped_items_details

    # التحقق من صدور إشعار تنبيهي للنظام
    notif = Notification.query.filter_by(type="salla_unmapped_item", item_sku=order_id).first()
    assert notif is not None
    assert "غير مسجلة" in notif.message


def test_salla_webhook_valid_order_and_daily_consolidation(db_session):
    """التحقق من استقبال طلب مطابق وتنفيذ الإغلاق والترحيل اليومي المجمع وخصم المخزون."""
    rw = Location.query.filter_by(name_ar="مستودع البن المحمص").first()
    if not rw:
        rw = Location(name_ar="مستودع البن المحمص", name_en="Roasted Warehouse", type="warehouse")
        db_session.add(rw)
        db_session.flush()

    mw = Location.query.filter(Location.name_ar.like("%الرئيسي%")).first()
    if not mw:
        mw = Location(name_ar="المستودع الرئيسي", name_en="Main Warehouse", type="warehouse")
        db_session.add(mw)
        db_session.flush()

    # صنف كولومبيا في test db
    it_col = Item.query.filter_by(sku="RB0465").first()
    if not it_col:
        it_col = Item(sku="RB0465", name_ar="كولومبي محمص", cost=50.0)
        db_session.add(it_col)
        db_session.flush()

    ci_col = CoffeeItem.query.filter_by(base_code="0465", coffee_type="roasted").first()
    if not ci_col:
        ci_col = CoffeeItem(
            item_id=it_col.id,
            xero_item_code="RB0465- كولومبى محمص",
            prefix="RB",
            base_code="0465",
            coffee_type="roasted",
            name_ar="كولومبي محمص",
            sale_price_per_kg=120.0
        )
        db_session.add(ci_col)
        db_session.flush()

    # ربط mapping بكولومبيا
    colombia_m = SallaItemMapping.query.filter_by(salla_product_name="كولمبيا توليمــا").first()
    if colombia_m:
        colombia_m.coffee_item_id = ci_col.id
        db_session.commit()

    # صنف الكيمكس
    it_chem = Item.query.filter_by(sku="SW-0016").first()
    if not it_chem:
        it_chem = Item(sku="SW-0016", name_ar="اداة الكيمكس - 3 اكواب", cost=80.0)
        db_session.add(it_chem)
        db_session.flush()

    chem_m = SallaItemMapping.query.filter_by(salla_product_name="اداة الكيمكس - 3 اكواب").first()
    if chem_m:
        chem_m.general_item_id = it_chem.id
        db_session.commit()

    # رصيد افتتاحي
    record_stock_movement(it_col.id, rw.id, "initial", 50.0)
    record_stock_movement(it_chem.id, mw.id, "initial", 20.0)

    order_id = "SL-TEST-VALID-202"
    payload = {
        "event": "order.invoice.created",
        "data": {
            "id": order_id,
            "reference_id": "ORD-602",
            "total": {"amount": 170.0},
            "shipping_cost": {"amount": 20.0},
            "customer": {
                "name": "فهد السعيد",
                "mobile": "0500123456"
            },
            "items": [
                {
                    "product_id": "766085130",  # كولمبيا توليما 250g
                    "name": "كولمبيا توليمــا",
                    "quantity": 2  # 2 * 0.25 = 0.5 kg
                },
                {
                    "product_id": "528060831",  # اداة الكيمكس 3 اكواب
                    "name": "اداة الكيمكس - 3 اكواب",
                    "quantity": 1
                }
            ]
        }
    }

    res = process_salla_webhook_payload(payload)
    assert res.get("success") is True
    assert res.get("has_unmapped") is False

    ord = SallaOrderQueue.query.filter_by(salla_order_id=order_id).first()
    assert ord.status == "pending"

    # اختبار إضافي: هيكل Webhook v2 الرسمي مع amount ككائن مفرد
    v2_order_id = "300654573"
    v2_payload = {
        "event": "invoice.created",
        "data": {
            "id": v2_order_id,
            "reference_id": "300654573",
            "amount": {
                "total": 171.84,
                "shipping": 19.0
            },
            "customer": {
                "first_name": "May",
                "last_name": "Alosaimi",
                "mobile": "+966500878780"
            },
            "items": [
                {
                    "id": "766085130",
                    "product_id": "766085130",
                    "name": "كولمبيا توليمــا",
                    "quantity": 1
                }
            ]
        }
    }
    v2_res = process_salla_webhook_payload(v2_payload)
    assert v2_res.get("success") is True
    v2_ord = SallaOrderQueue.query.filter_by(salla_order_id=v2_order_id).first()
    assert v2_ord is not None
    assert v2_ord.total_amount == 171.84
    assert v2_ord.shipping_amount == 19.0
    assert "May Alosaimi" in v2_ord.customer_name

    # اختبار إضافي: حدث تم إنشاء شحنة shipment.created
    shp_order_id = "300640520"
    shp_payload = {
        "event": "shipment.created",
        "data": {
            "id": 998877,
            "order_id": shp_order_id,
            "order_reference": "300640520",
            "cost": 19.0,
            "customer": {
                "name": "رهف",
                "mobile": "+966555123456"
            },
            "shipment_items": [
                {
                    "id": "528060831",
                    "product_id": "528060831",
                    "name": "اداة الكيمكس - 3 اكواب",
                    "quantity": 1
                }
            ]
        }
    }
    shp_res = process_salla_webhook_payload(shp_payload)
    assert shp_res.get("success") is True
    shp_ord = SallaOrderQueue.query.filter_by(salla_order_id=shp_order_id).first()
    assert shp_ord is not None
    assert shp_ord.shipping_amount == 19.0
    assert shp_ord.customer_name == "رهف"

    # تنفيذ الإغلاق اليومي
    close_res = execute_salla_daily_consolidation([ord.id])
    assert close_res.get("success") is True
    assert close_res.get("total_qty_kg") == 0.5
    assert close_res.get("tools_count") == 1

    # التحقق من السند المنشأ
    disp_id = close_res.get("dispatch_id")
    disp = RoastedCoffeeDispatch.query.get(disp_id)
    assert disp is not None
    assert disp.customer_name == "عملاء موقع سلة Salla website"
    assert disp.doc_number.startswith("SL-")
    assert disp.status == "approved"

    # التحقق من أن حساب مبيعات Xero هو 203
    payload_xero = build_roasted_issue_sale_invoice_payload(disp)
    assert payload_xero["Contact"]["Name"] == "عملاء موقع سلة Salla website"
    for line in payload_xero["LineItems"]:
        if line.get("AccountCode") != "261":
            assert line.get("AccountCode") == "203"

    # التحقق من تحديث حالة الطلب بالطابور
    db_session.refresh(ord)
    assert ord.status == "dispatched"
    assert ord.roasted_dispatch_id == disp.id


def test_salla_ui_and_mapping_crud(admin_client, db_session):
    """التحقق من عمل شاشات سلة وإمكانية إضافة وتعديل وحذف مطابقة صنف."""
    # 1. شاشة قائمة الطلبات
    r = admin_client.get("/coffee/salla/orders")
    assert r.status_code == 200
    assert "طلبات ومبيعات متجر سلة" in r.get_data(as_text=True)

    # 2. شاشة جدول المطابقة
    r = admin_client.get("/coffee/salla/mappings")
    assert r.status_code == 200
    assert "جدول مطابقة وتحويل أصناف سلة" in r.get_data(as_text=True)

    # 3. حفظ ربط صنف جديد
    r = admin_client.post("/coffee/salla/mappings/save", data={
        "salla_product_name": "محصول تجريبي جديد 250g",
        "salla_product_id": "999111",
        "item_type": "coffee",
        "source_warehouse": "roasted",
        "weight_kg": "0.250",
        "xero_account_code": "203"
    }, follow_redirects=True)
    assert r.status_code == 200
    assert "محصول تجريبي جديد 250g" in r.get_data(as_text=True)

    m = SallaItemMapping.query.filter_by(salla_product_id="999111").first()
    assert m is not None

    # 4. حذف الربط
    r = admin_client.post(f"/coffee/salla/mappings/{m.id}/delete", follow_redirects=True)
    assert r.status_code == 200
    assert SallaItemMapping.query.get(m.id) is None


def test_salla_orders_manual_add(admin_client, db_session):
    """التحقق من إمكانية تسجيل طلب سلة يدوياً في حال تعطل الـ Webhook."""
    resp = admin_client.post("/coffee/salla/orders/manual-add", data={
        "order_id": "196107821",
        "customer_name": "الهنوف الحبيب",
        "customer_phone": "+966535330022",
        "total_amount": "198.86",
        "items_summary": "1x بن كولومبيا 250g"
    }, follow_redirects=True)
    assert resp.status_code == 200

    order = SallaOrderQueue.query.filter_by(salla_order_id="196107821").first()
    assert order is not None
    assert order.customer_name == "الهنوف الحبيب"
    assert order.total_amount == 198.86
    assert order.status == "pending"

    # محاولة إضافة نفس الطلب مرة أخرى (منع التكرار)
    resp2 = admin_client.post("/coffee/salla/orders/manual-add", data={
        "order_id": "196107821",
        "total_amount": "198.86"
    }, follow_redirects=True)
    assert resp2.status_code == 200
    assert SallaOrderQueue.query.filter_by(salla_order_id="196107821").count() == 1


def test_salla_shipping_revenue_account_261(db_session):
    """التحقق من أن مصاريف الشحن ترحل لـ Xero تحت حساب رقم 261 باسم 'ايرادات شحن وتوصيل'."""
    rw = Location(name_ar="مستودع البن المحمص", name_en="Roasted Coffee Warehouse", type="warehouse")
    db_session.add(rw)
    db_session.flush()

    item = Item(sku="RB-TEST-SHIP", name_ar="بن محمص تجريبي", storage_unit="كجم", cost=50.0)
    db_session.add(item)
    db_session.flush()
    ci = CoffeeItem(item_id=item.id, xero_item_code="RB TEST SHIP", prefix="RB", base_code="TEST_SHIP", coffee_type="roasted", sale_price_per_kg=120.0)
    db_session.add(ci)
    db_session.flush()

    dispatch = RoastedCoffeeDispatch(
        doc_number="RB 00099",
        dispatch_date=date.today(),
        source_warehouse_id=rw.id,
        dispatch_type="sale",
        issue_reason="مبيعات متجر سلة",
        customer_name="عملاء موقع سلة Salla website",
        delivery_method="shipping",
        shipping_company="متجر سلة (Salla)",
        shipping_fee=19.0,
        coffee_item_id=ci.id,
        qty_kg=2.0,
        unit_cost=50.0,
        total_cost=100.0,
        sale_price_per_kg=120.0,
        total_sale_amount=240.0
    )
    db_session.add(dispatch)
    db_session.commit()

    payload = build_roasted_issue_sale_invoice_payload(dispatch)
    lines = payload.get("LineItems", [])
    
    # البحث عن سطر إيرادات الشحن والتوصيل
    ship_line = next((l for l in lines if l.get("AccountCode") == "261"), None)
    assert ship_line is not None, "لم يتم العثور على سطر حساب 261 في فاتورة Xero!"
    assert ship_line["Description"] == "ايرادات شحن وتوصيل"
    assert ship_line["UnitAmount"] == 19.0
    assert ship_line["AccountCode"] == "261"
    assert ship_line["TaxType"] == "OUTPUT"


def test_manual_add_option_excluded_from_salla_orders_ui(admin_client):
    """التحقق من استبعاد الخيار اليدوي من واجهة طلبات سلة لتكون العملية مؤتمتة كلياً."""
    r = admin_client.get("/coffee/salla/orders")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert "manualAddOrderModal" not in html
    assert "تسجيل طلب يدوياً" not in html


def test_salla_paste_payload_route(admin_client, db_session):
    """التحقق من عمل مسار لصق حمولة الـ Webhook (JSON Payload) واستيراد الطلب فوراً."""
    payload = {
        "event": "invoice.created",
        "merchant": 1174910479,
        "data": {
            "id": 999111,
            "order_id": 888777666,
            "invoice_number": 1999,
            "total": {"amount": 75.0, "currency": "SAR"},
            "shipping_cost": {"amount": 0, "currency": "SAR"},
            "customer": {
                "first_name": "فهد",
                "last_name": "العتيبي",
                "mobile": "0501234567"
            },
            "items": [
                {
                    "name": "كولمبيا توليمــا",
                    "quantity": 1,
                    "type": "product",
                    "total": {"amount": 75.0, "currency": "SAR"}
                }
            ]
        }
    }
    r = admin_client.post("/coffee/salla/orders/paste-payload", data={
        "payload_json": json.dumps(payload)
    }, follow_redirects=True)
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert "888777666" in html
    assert "فهد العتيبي" in html

    # فحص وجوده في قاعدة البيانات
    q_ord = SallaOrderQueue.query.filter_by(salla_order_id="888777666").first()
    assert q_ord is not None
    assert q_ord.total_amount == 75.0
    assert q_ord.customer_name == "فهد العتيبي"


def test_salla_orders_modals_rendered_and_clickable(admin_client):
    """التحقق من تصيير نوافذ الاستيراد واللصق بداخل الصفحة وربطها بدوال فتح صريحة."""
    r = admin_client.get("/coffee/salla/orders")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert 'openPastePayloadModal()' in html
    assert 'id="pasteSallaPayloadModal"' in html
    assert 'openImportCsvModal()' in html
    assert 'id="importSallaCsvModal"' in html


def test_salla_paste_markdown_and_raw_inner_data(admin_client, db_session):
    """التحقق من معالجة كود الـ JSON حتى لو كان محاطاً بماركداون أو كائناً داخلياً فقط."""
    # 1. كود محاط بـ ```json
    md_payload = """```json
    {
      "event": "invoice.created",
      "data": {
        "order_id": 777666555,
        "customer": {"first_name": "سعد", "last_name": "الغامدي"},
        "total": {"amount": 55.0},
        "items": [{"name": "كولمبيا توليمــا", "quantity": 1}]
      }
    }
    ```"""
    r1 = admin_client.post("/coffee/salla/orders/paste-payload", data={"payload_json": md_payload}, follow_redirects=True)
    assert r1.status_code == 200
    assert "777666555" in r1.get_data(as_text=True)

    # 2. كائن داخلي مباشرة دون حقل event
    inner_payload = """{
      "order_id": 444333222,
      "customer": {"first_name": "خالد", "last_name": "الدوسري"},
      "total": {"amount": 120.0},
      "items": [{"name": "كولمبيا توليمــا", "quantity": 2}]
    }"""
    r2 = admin_client.post("/coffee/salla/orders/paste-payload", data={"payload_json": inner_payload}, follow_redirects=True)
    assert r2.status_code == 200
    assert "444333222" in r2.get_data(as_text=True)


def test_salla_mappings_auto_seed_and_manual_seed(admin_client, db_session):
    """التحقق من الزرع التلقائي للأصناف في صفحة المطابقة وزر الاستيراد اليدوي."""
    from models_coffee import SallaItemMapping

    # حذف كافة الأصناف لمحاكاة بيئة إنتاج جديدة تماماً
    SallaItemMapping.query.delete()
    db_session.commit()
    assert SallaItemMapping.query.count() == 0

    # زيارة صفحة المطابقة GET يجب أن تفعل الزرع التلقائي فوراً
    res = admin_client.get("/coffee/salla/mappings")
    assert res.status_code == 200
    assert SallaItemMapping.query.count() >= 17
    html = res.get_data(as_text=True)
    assert "كولمبيا توليمــا" in html
    assert "استيراد الأصناف الافتراضية" in html

    # اختبار استدعاء زر استيراد الأصناف الافتراضية POST
    res_seed = admin_client.post("/coffee/salla/mappings/seed-defaults", follow_redirects=True)
    assert res_seed.status_code == 200
    # يجب ألا تتكرر الأصناف بفضل الحماية من التكرار
    assert SallaItemMapping.query.count() == 17

