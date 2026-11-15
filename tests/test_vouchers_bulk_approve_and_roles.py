"""
Test Suite: Bulk Voucher Approval, Accountant Notifications, Warehouse Coffee Permissions, and Branch Staff Adjustments
Tests:
1. Bulk approval of receipt vouchers across different branches in one click.
2. Notifications for accountants when receipt vouchers need review.
3. Warehouse role access to Coffee module (operational allowed, reports/xero/settings blocked).
4. Branch staff blocked from coffee module.
5. Branch staff costs hidden on receipt vouchers.
6. Multi-branch staff assignment and access.
"""
import pytest
import json
from datetime import datetime, date
from models import (
    db, User, Location, Item, PurchaseTransaction, PurchaseLine,
    StockBalance, StockMovement, SyncQueue, Notification, UserLocation
)
from models_coffee import CoffeeItem, has_coffee_permission
from tests.conftest import login_client


def test_bulk_approve_receipt_vouchers_across_branches(app, client, test_data, db_session):
    """Test 1: Bulk approval of multiple receipt vouchers across different branches in one click."""
    br1 = test_data["br1"]
    br2 = test_data["br2"]
    item1 = test_data["item_coffee"]
    item2 = test_data["item_milk"]
    admin = test_data["admin"]
    accountant = test_data["accountant"]

    # إنشاء سند استلام 1 لفرع 1
    p1 = PurchaseTransaction(
        location_id=br1.id,
        supplier_name="شركة البن الفاخر",
        invoice_number="INV-BR1-001",
        invoice_date=date.today(),
        status="draft",
        created_by=admin.id
    )
    db_session.add(p1)
    db_session.flush()
    line1 = PurchaseLine(purchase_id=p1.id, item_id=item1.id, quantity=20.0, unit_cost=50.0)
    db_session.add(line1)

    # إنشاء سند استلام 2 لفرع 2
    p2 = PurchaseTransaction(
        location_id=br2.id,
        supplier_name="شركة البن الفاخر",
        invoice_number="INV-BR2-002",
        invoice_date=date.today(),
        status="submitted",
        created_by=admin.id
    )
    db_session.add(p2)
    db_session.flush()
    line2 = PurchaseLine(purchase_id=p2.id, item_id=item2.id, quantity=15.0, unit_cost=5.5)
    db_session.add(line2)
    db_session.commit()

    p1_id = p1.id
    p2_id = p2.id

    # تسجيل الدخول كمحاسب
    login_client(client, accountant.id)

    # تنفيذ الاعتماد الجماعي بضغطة زر واحدة
    resp = client.post("/purchases/bulk-approve", data={
        "purchase_ids": [str(p1_id), str(p2_id)]
    }, follow_redirects=True)

    assert resp.status_code == 200

    p1_fresh = db_session.get(PurchaseTransaction, p1_id)
    p2_fresh = db_session.get(PurchaseTransaction, p2_id)
    assert p1_fresh.status == "approved"
    assert p2_fresh.status == "approved"

    # التحقق من تسجيل حركات المخزون في كلا الفرعين
    mov1 = StockMovement.query.filter_by(reference_type="purchase", reference_id=p1_id).first()
    mov2 = StockMovement.query.filter_by(reference_type="purchase", reference_id=p2_id).first()
    assert mov1 is not None
    assert mov1.location_id == br1.id
    assert mov1.quantity_change == 20.0

    assert mov2 is not None
    assert mov2.location_id == br2.id
    assert mov2.quantity_change == 15.0

    # التحقق من الإدراج في طابور الترحيل لفوديكس
    q1 = SyncQueue.query.filter_by(source_type="purchase", source_id=p1_id).first()
    q2 = SyncQueue.query.filter_by(source_type="purchase", source_id=p2_id).first()
    assert q1 is not None
    assert q2 is not None


def test_accountant_notifications_for_pending_vouchers(app, client, test_data, db_session):
    """Test 2: Notifications for accountant when receipt vouchers need review."""
    br1 = test_data["br1"]
    br1_user = test_data["br1_user"]
    accountant = test_data["accountant"]
    item = test_data["item_coffee"]

    # إنشاء سند كمسودة مع مرفق لاجتياز بوابة الفحص
    p = PurchaseTransaction(
        location_id=br1.id,
        supplier_name="مورد اختبار الإشعار",
        invoice_number="INV-NOTIF-99",
        status="draft",
        attachments='["/uploads/test_invoice.pdf"]',
        created_by=br1_user.id
    )
    db_session.add(p)
    db_session.flush()
    line = PurchaseLine(purchase_id=p.id, item_id=item.id, quantity=5.0, unit_cost=60.0)
    db_session.add(line)
    db_session.commit()
    pid = p.id

    # تسجيل الدخول كموظف فرع وإرسال السند للمراجعة
    login_client(client, br1_user.id)
    resp = client.post(f"/purchases/{pid}", data={
        "action": "submit_for_review"
    }, follow_redirects=True)
    assert resp.status_code == 200

    # التحقق من توليد إشعار من نوع purchase_pending_review
    notif = Notification.query.filter_by(type="purchase_pending_review", related_id=pid).first()
    assert notif is not None
    assert notif.is_read is False
    assert "سند استلام بانتظار المراجعة" in notif.title

    # تسجيل الدخول كمحاسب والاستعلام عن الإشعارات غير المقروءة عبر API
    login_client(client, accountant.id)
    resp = client.get("/api/notifications/unread")
    assert resp.status_code == 200
    data = json.loads(resp.data)
    assert data["status"] == "success"
    matched = [n for n in data["notifications"] if n["id"] == notif.id]
    assert len(matched) == 1
    assert matched[0]["link_url"] == f"/purchases/{pid}"

    # إنشاء إشعار كتالوج عام
    general_notif = Notification(
        title="مادة جديدة من فوديكس",
        message="تمت إضافة مادة جديدة للكتالوج العام",
        type="new_inventory_item",
        is_read=False
    )
    db_session.add(general_notif)
    db_session.commit()
    gid = general_notif.id

    # عند استعلام موظف الفرع، لا يظهر له إشعار الكتالوج العام
    login_client(client, br1_user.id)
    resp = client.get("/api/notifications/unread")
    assert resp.status_code == 200
    branch_data = json.loads(resp.data)
    gen_in_branch = [n for n in branch_data["notifications"] if n["id"] == gid]
    assert len(gen_in_branch) == 0


def test_warehouse_role_coffee_permissions(app, client, test_data, db_session):
    """Test 3: Warehouse employee has operational coffee access, but reports/xero/settings are blocked and hidden."""
    wh_user = test_data["wh_user"]

    # فحص دوال الصلاحيات في models_coffee
    assert has_coffee_permission(wh_user, "coffee.view_dashboard") is True
    assert has_coffee_permission(wh_user, "coffee.view_green_stock") is True
    assert has_coffee_permission(wh_user, "coffee.view_roasted_stock") is True
    assert has_coffee_permission(wh_user, "coffee.create_roasting_order") is True
    assert has_coffee_permission(wh_user, "coffee.create_green_issue") is True
    assert has_coffee_permission(wh_user, "coffee.create_dispatch") is True

    # المحظورات لموظف المستودع:
    assert has_coffee_permission(wh_user, "coffee.view_reports") is False
    assert has_coffee_permission(wh_user, "coffee.export_reports") is False
    assert has_coffee_permission(wh_user, "coffee.view_audit_log") is False
    assert has_coffee_permission(wh_user, "coffee.post_to_xero") is False
    assert has_coffee_permission(wh_user, "coffee.settings") is False
    assert has_coffee_permission(wh_user, "coffee.manage_settings") is False

    login_client(client, wh_user.id)

    # العمليات المسموحة: لوحة تحكم القهوة
    resp = client.get("/coffee/dashboard")
    assert resp.status_code == 200
    html = resp.data.decode("utf-8")
    assert 'class="xero-conn-pill"' not in html
    assert "/coffee/xero/status" not in html
    assert "فتح مركز ترحيل ومزامنة Xero" not in html

    # العمليات المسموحة: مخزون الأخضر
    resp = client.get("/coffee/green/stock")
    assert resp.status_code == 200

    # المحظورات: التقارير -> يتم الحظر والتحويل إلى dashboard القهوة
    resp = client.get("/coffee/reports", follow_redirects=True)
    assert resp.status_code == 200
    assert "التقارير والمحاسبة والإعدادات مخصصة لمدير النظام والمحاسب فقط" in resp.data.decode("utf-8")

    # المحظورات: ترحيل Xero -> يتم الحظر والتحويل
    resp = client.get("/coffee/xero/status", follow_redirects=True)
    assert resp.status_code == 200
    assert "التقارير والمحاسبة والإعدادات مخصصة لمدير النظام والمحاسب فقط" in resp.data.decode("utf-8")

    # المحظورات: الإعدادات -> يتم الحظر والتحويل
    resp = client.get("/coffee/settings", follow_redirects=True)
    assert resp.status_code == 200
    assert "التقارير والمحاسبة والإعدادات مخصصة لمدير النظام والمحاسب فقط" in resp.data.decode("utf-8")

    # المحظورات: سجل التدقيق -> يتم الحظر والتحويل
    resp = client.get("/coffee/audit-log", follow_redirects=True)
    assert resp.status_code == 200
    assert "التقارير والمحاسبة والإعدادات مخصصة لمدير النظام والمحاسب فقط" in resp.data.decode("utf-8")


def test_branch_staff_blocked_from_coffee(app, client, test_data):
    """Test 4: Branch staff completely blocked from coffee module."""
    br1_user = test_data["br1_user"]
    login_client(client, br1_user.id)

    # محاولة الدخول لأي مسار في القهوة
    resp = client.get("/coffee/dashboard", follow_redirects=True)
    assert resp.status_code == 200
    assert "قسم القهوة والمحمصة غير متاح لموظفي الفروع" in resp.data.decode("utf-8")


def test_branch_staff_costs_hidden_on_purchase(app, client, test_data, db_session):
    """Test 5: Hide item unit costs and total money on purchases for branch staff."""
    br1 = test_data["br1"]
    br1_user = test_data["br1_user"]
    item = test_data["item_coffee"]

    p = PurchaseTransaction(
        location_id=br1.id,
        supplier_name="مورد التكلفة المخفية",
        invoice_number="INV-COST-01",
        status="draft",
        created_by=br1_user.id
    )
    db_session.add(p)
    db_session.flush()
    line = PurchaseLine(purchase_id=p.id, item_id=item.id, quantity=10.0, unit_cost=45.5)
    db_session.add(line)
    db_session.commit()
    pid = p.id

    login_client(client, br1_user.id)
    resp = client.get(f"/purchases/{pid}")
    assert resp.status_code == 200
    html = resp.data.decode("utf-8")

    # يجب ألا تظهر تكلفة الوحدة للموظف
    assert 'name="unit_cost"' not in html
    assert 'id="unit_cost"' not in html
    assert 'id="line_total_preview"' not in html
    assert "45.50" not in html
    # تظهر الكمية
    assert "10.0" in html or "10" in html


def test_multi_branch_user_save_and_access(app, client, test_data, db_session):
    """Test 6: Multi-branch user assignment and permissions across authorized branches."""
    admin = test_data["admin"]
    br1 = test_data["br1"]
    br2 = test_data["br2"]
    br1_user = test_data["br1_user"]
    uid = br1_user.id

    login_client(client, admin.id)

    # تعديل بيانات المستخدم وإسناد فرعين وتفعيل خاصية التنقل
    resp = client.post(f"/users/{uid}/edit", data={
        "name": br1_user.name,
        "role": "branch_staff",
        "location_id": str(br1.id),
        "is_roving": "1",
        "assigned_location_ids": [str(br1.id), str(br2.id)]
    }, follow_redirects=True)
    assert resp.status_code == 200

    u = db_session.get(User, uid)
    assert u.is_roving is True
    assert u.can_access_location(br1.id) is True
    assert u.can_access_location(br2.id) is True
    assert u.is_multi_branch is True
    assert br1.id in u.authorized_location_ids
    assert br2.id in u.authorized_location_ids


def test_attachment_upload_and_serving(app, client, test_data, db_session):
    """Test 7: Uploading and serving receipt attachments with nested folders and permission checks."""
    import io
    br1 = test_data["br1"]
    br1_user = test_data["br1_user"]
    admin = test_data["admin"]

    p = PurchaseTransaction(
        location_id=br1.id,
        supplier_name="مورد مرفقات",
        invoice_number="INV-ATT-01",
        status="draft",
        created_by=br1_user.id
    )
    db_session.add(p)
    db_session.commit()
    pid = p.id

    login_client(client, br1_user.id)

    # رفع مرفق صورة PNG حقيقية
    png_bytes = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15c4"
    data = {
        "action": "upload_attachment",
        "attachment": (io.BytesIO(png_bytes), "invoice_receipt.png")
    }
    resp = client.post(f"/purchases/{pid}", data=data, content_type="multipart/form-data", follow_redirects=True)
    assert resp.status_code == 200

    db_session.refresh(p)
    atts = json.loads(p.attachments or "[]")
    assert len(atts) == 1
    att_url = atts[0]  # /uploads/purchases/<id>/<uuid>.png
    assert f"/uploads/purchases/{pid}/" in att_url

    # تحميل المرفق بواسطة موظف الفرع المصرح له -> 200 OK
    resp_get = client.get(att_url)
    assert resp_get.status_code == 200
    assert resp_get.data == png_bytes


def test_multi_branch_employee_switch_and_count_sessions(app, client, test_data, db_session):
    """Test 8: Multi-branch employee branch switching, voucher creation and count session authorization."""
    br1 = test_data["br1"]
    br2 = test_data["br2"]
    br1_user = test_data["br1_user"]

    # منح br1_user صلاحية العمل في br2 أيضاً
    db_session.add(UserLocation(user_id=br1_user.id, location_id=br2.id))
    db_session.commit()

    login_client(client, br1_user.id)

    # 1. تبديل الفرع النشط إلى br2
    resp = client.get(f"/switch-branch/{br2.id}", follow_redirects=True)
    assert resp.status_code == 200
    html = resp.data.decode("utf-8")
    assert "تم تبديل فرع العمل الحالي بنجاح" in html

    # 2. بدء جلسة جرد لفرع br2
    resp_count = client.post("/counts/new", data={
        "location_id": str(br2.id),
        "status": "open",
        "action": "start"
    }, follow_redirects=True)
    assert resp_count.status_code == 200
    count_html = resp_count.data.decode("utf-8")
    assert "تم فتح جلسة جرد جديدة" in count_html


def test_clickable_voucher_rows_in_lists(app, client, test_data, db_session):
    """Test 9: Voucher rows in purchase and transfer lists are interactive with clickable-row and data-href."""
    admin = test_data["admin"]
    br1 = test_data["br1"]
    login_client(client, admin.id)

    # إنشاء سند استلام
    p = PurchaseTransaction(
        location_id=br1.id,
        supplier_name="مورد الاختبار",
        invoice_number="INV-CLICK-01",
        status="draft",
        created_by=admin.id
    )
    db_session.add(p)
    db_session.commit()

    resp = client.get("/purchases")
    assert resp.status_code == 200
    html = resp.data.decode("utf-8")
    assert "clickable-row" in html
    assert f'data-href="/purchases/{p.id}"' in html
    assert "handleVoucherRowClick" in html


def test_item_search_exclude_skus_and_no_duplicate_lines(app, client, test_data, db_session):
    """Test 10: Search excludes already added SKUs and voucher rejects duplicate item lines."""
    admin = test_data["admin"]
    br1 = test_data["br1"]
    item1 = test_data["item_coffee"]
    login_client(client, admin.id)

    # 1. فحص استبعاد الأصناف عبر API
    resp_api = client.get(f"/api/search_items?q={item1.sku}&exclude_skus={item1.sku}")
    assert resp_api.status_code == 200
    data = json.loads(resp_api.data.decode("utf-8"))
    skus_returned = [r["sku"] for r in data.get("results", [])]
    assert item1.sku not in skus_returned

    # 2. إنشاء سند استلام وإضافة الصنف الأول
    p = PurchaseTransaction(
        location_id=br1.id,
        supplier_name="مورد الاختبار 2",
        invoice_number="INV-NO-DUP-01",
        status="draft",
        created_by=admin.id
    )
    db_session.add(p)
    db_session.commit()

    # إضافة الصنف للمرة الأولى -> تنجح
    resp_add1 = client.post(f"/purchases/{p.id}", data={
        "action": "add_line",
        "code": item1.sku,
        "quantity": "5",
        "unit": item1.storage_unit,
        "unit_cost": "25.0"
    }, follow_redirects=True)
    assert resp_add1.status_code == 200
    assert len(p.lines) == 1

    # محاولة إضافة نفس الصنف للمرة الثانية -> تُرفض وتمنع التكرار
    resp_add2 = client.post(f"/purchases/{p.id}", data={
        "action": "add_line",
        "code": item1.sku,
        "quantity": "3",
        "unit": item1.storage_unit,
        "unit_cost": "25.0"
    }, follow_redirects=True)
    assert resp_add2.status_code == 200
    db_session.refresh(p)
    assert len(p.lines) == 1  # بقي سطراً واحداً فقط دون تكرار
    html2 = resp_add2.data.decode("utf-8")
    assert "مضاف بالفعل في هذا السند" in html2



