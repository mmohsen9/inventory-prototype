# -*- coding: utf-8 -*-
"""
اختبارات تقرير كشف حساب واستلامات عملاء البن (المحمص والأخضر)
Coffee Customers Sales & Dispatches Statement Report Tests
"""
import pytest
from datetime import date
from models import db, Item, Location, User
from models_coffee import (
    CoffeeItem,
    CoffeeCustomer,
    RoastedCoffeeDispatch,
    RoastedCoffeeDispatchLine,
    GreenCoffeeIssue,
    GreenCoffeeIssueLine,
)


@pytest.fixture
def statement_test_data(db_session, test_data):
    """تهيئة عملاء وأصناف وسندات بيع للبن المحمص والأخضر لاختبار كشف الحساب."""
    wh = test_data["wh"]
    admin = test_data["admin"]

    # أصناف المخزون الأساسية
    item_g = Item(sku="GB-TEST", name_ar="بن أخضر تجريبي", cost=35.0, storage_unit="kg")
    item_r = Item(sku="RB-TEST", name_ar="بن محمص تجريبي", cost=50.0, storage_unit="kg")
    db_session.add_all([item_g, item_r])
    db_session.flush()

    # أصناف قهوة
    ci_green = CoffeeItem(
        item_id=item_g.id,
        xero_item_code="GB-TEST",
        prefix="GB",
        base_code="901",
        coffee_type="green",
        name_ar="بن أخضر تجريبي",
        xero_cost_price=35.0,
    )
    ci_roasted = CoffeeItem(
        item_id=item_r.id,
        xero_item_code="RB-TEST",
        prefix="RB",
        base_code="901",
        coffee_type="roasted",
        name_ar="بن محمص تجريبي",
        xero_cost_price=50.0,
    )
    db_session.add_all([ci_green, ci_roasted])
    db_session.flush()

    # عملاء
    cust_direct = CoffeeCustomer(name="عميل مباشر تجريبي", phone="0501234567")
    cust_salla = CoffeeCustomer(name="عملاء موقع سلة Salla website", phone="0500000000")
    db_session.add_all([cust_direct, cust_salla])
    db_session.flush()

    # سند بيع محمص للعميل المباشر (معتمد)
    disp1 = RoastedCoffeeDispatch(
        doc_number="RB-9901",
        dispatch_date=date.today(),
        source_warehouse_id=wh.id,
        dispatch_type="sale",
        customer_id=cust_direct.id,
        customer_name=cust_direct.name,
        customer_phone=cust_direct.phone,
        status="approved",
        total_sale_amount=1500.0,
        qty_kg=15.0,
    )
    db_session.add(disp1)
    db_session.flush()

    disp1_line = RoastedCoffeeDispatchLine(
        dispatch_id=disp1.id,
        coffee_item_id=ci_roasted.id,
        qty_kg=15.0,
        sale_price_per_kg=100.0,
        total_sale_amount=1500.0,
        package_type="1kg",
        package_qty=15,
    )
    db_session.add(disp1_line)

    # سند بيع أخضر لنفس العميل المباشر (معتمد)
    issue1 = GreenCoffeeIssue(
        doc_number="GB-9901",
        issue_date=date.today(),
        warehouse_id=wh.id,
        issue_type="sale",
        customer_id=cust_direct.id,
        customer_name=cust_direct.name,
        customer_phone=cust_direct.phone,
        status="approved",
        total_sale_amount=1200.0,
        qty_kg=30.0,
    )
    db_session.add(issue1)
    db_session.flush()

    issue1_line = GreenCoffeeIssueLine(
        issue_id=issue1.id,
        coffee_item_id=ci_green.id,
        qty_kg=30.0,
        qty_sacks=1.0,
        sale_price_per_kg=40.0,
        total_sale_amount=1200.0,
    )
    db_session.add(issue1_line)

    # سند بيع محمص مسودة (draft)
    disp_draft = RoastedCoffeeDispatch(
        doc_number="RB-9902",
        dispatch_date=date.today(),
        source_warehouse_id=wh.id,
        dispatch_type="sale",
        customer_id=cust_direct.id,
        customer_name=cust_direct.name,
        customer_phone=cust_direct.phone,
        status="draft",
        total_sale_amount=500.0,
        qty_kg=5.0,
    )
    db_session.add(disp_draft)

    # سند مبيعات سلة (يجب استبعاده من كشف العملاء المباشرين)
    disp_salla = RoastedCoffeeDispatch(
        doc_number="SL-9901",
        dispatch_date=date.today(),
        source_warehouse_id=wh.id,
        dispatch_type="sale",
        customer_id=cust_salla.id,
        customer_name=cust_salla.name,
        customer_phone=cust_salla.phone,
        status="approved",
        total_sale_amount=800.0,
        qty_kg=8.0,
    )
    db_session.add(disp_salla)
    db_session.commit()

    return {
        "admin": admin,
        "cust_direct": cust_direct,
        "cust_salla": cust_salla,
        "disp1": disp1,
        "issue1": issue1,
        "disp_draft": disp_draft,
        "disp_salla": disp_salla,
    }


def test_report_route_and_grouping(client, statement_test_data):
    """فحص فتح تقرير كشف الحساب وتجميع مبيعات المحمص والأخضر تحت العميل."""
    with client.session_transaction() as sess:
        sess["user_id"] = statement_test_data["admin"].id
        sess["role"] = "admin"

    res = client.get("/coffee/reports/customers")
    assert res.status_code == 200
    html = res.data.decode("utf-8")

    assert "كشف حساب واستلامات عملاء البن" in html
    assert statement_test_data["cust_direct"].name in html
    assert "RB-9901" in html
    assert "GB-9901" in html


def test_template_elements_and_kpis(client, statement_test_data):
    """التحقق من عناصر القالب وبطاقات الـ KPIs والأكورديون."""
    with client.session_transaction() as sess:
        sess["user_id"] = statement_test_data["admin"].id
        sess["role"] = "admin"

    res = client.get("/coffee/reports/customers")
    assert res.status_code == 200
    html = res.data.decode("utf-8")

    assert "إجمالي مبيعات العملاء" in html
    assert "إجمالي الكمية المسلمة" in html
    assert "إجمالي السندات" in html
    assert "customer-statement-card" in html
    assert "toggleAccordion" in html
    assert "toggleAllAccordions" in html


def test_direct_sales_and_status_filters(client, statement_test_data):
    """التحقق من حصر التقرير على المبيعات المباشرة واستبعاد سلة وفلترة الحالة المعتمدة افتراضياً."""
    with client.session_transaction() as sess:
        sess["user_id"] = statement_test_data["admin"].id
        sess["role"] = "admin"

    # 1. الاستعلام الافتراضي (معتمد ومباشر فقط)
    res_default = client.get("/coffee/reports/customers")
    assert res_default.status_code == 200
    html_default = res_default.data.decode("utf-8")

    # التأكد من استبعاد مبيعات موقع سلة
    assert "SL-9901" not in html_default
    assert "عملاء موقع سلة" not in html_default

    # التأكد من استبعاد المسودة افتراضياً
    assert "RB-9902" not in html_default

    # 2. الاستعلام مع تفعيل كافة السندات (شامل المسودات)
    res_all = client.get("/coffee/reports/customers?status=all")
    assert res_all.status_code == 200
    html_all = res_all.data.decode("utf-8")
    assert "RB-9902" in html_all  # ظهرت المسودة
    assert "SL-9901" not in html_all  # سلة لا تزال مستبعدة

    # 3. فلترة نوع البن محمص فقط
    res_roasted = client.get("/coffee/reports/customers?coffee_type=roasted")
    assert res_roasted.status_code == 200
    html_roasted = res_roasted.data.decode("utf-8")
    assert "RB-9901" in html_roasted
    assert "GB-9901" not in html_roasted


def test_navigation_and_customer_list_links(client, statement_test_data):
    """التحقق من ظهور روابط التقرير في مركز التقارير والقائمة الجانبية ودليل العملاء."""
    with client.session_transaction() as sess:
        sess["user_id"] = statement_test_data["admin"].id
        sess["role"] = "admin"

    # 1. مركز التقارير
    res_reports = client.get("/coffee/reports/")
    assert res_reports.status_code == 200
    html_reports = res_reports.data.decode("utf-8")
    assert "/coffee/reports/customers" in html_reports
    assert "كشف حساب واستلامات عملاء البن" in html_reports

    # 2. القائمة الجانبية (coffee_base)
    res_dash = client.get("/coffee/dashboard")
    assert res_dash.status_code == 200
    html_dash = res_dash.data.decode("utf-8")
    assert "/coffee/reports/customers" in html_dash
    assert "كشف حساب العملاء" in html_dash

    # 3. دليل العملاء (customers_list)
    res_cust = client.get("/coffee/customers/")
    assert res_cust.status_code == 200
    html_cust = res_cust.data.decode("utf-8")
    assert "/coffee/reports/customers" in html_cust
    assert "كشف حساب" in html_cust


def test_export_csv(client, statement_test_data):
    """التحقق من تصدير التقرير إلى ملف CSV بترميز UTF-8 BOM وتضمين البيانات بدقة."""
    with client.session_transaction() as sess:
        sess["user_id"] = statement_test_data["admin"].id
        sess["role"] = "admin"

    res = client.get("/coffee/reports/customers/export")
    assert res.status_code == 200
    assert "text/csv" in res.mimetype
    csv_text = res.data.decode("utf-8-sig")

    assert "اسم العميل" in csv_text
    assert "رقم السند" in csv_text
    assert "نوع البن" in csv_text
    assert statement_test_data["cust_direct"].name in csv_text
    assert "RB-9901" in csv_text
    assert "GB-9901" in csv_text
    assert "الإجمالي العام" in csv_text
    assert "SL-9901" not in csv_text  # استبعاد سلة
