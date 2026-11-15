"""
tests/test_all_vouchers_draft_lock_and_resume.py — اختبارات قفل واستئناف المسودات المفتوحة لكافة سندات النظام
"""
import pytest
from datetime import date
from models import (
    db, Location, Item, StockBalance, Supplier, PurchaseTransaction,
    TransferOrder, CountSession, record_stock_movement
)
from models_coffee import (
    CoffeeItem, GreenCoffeeIssue, GreenCoffeeIssueLine,
    RoastedCoffeeDispatch, RoastedCoffeeDispatchLine,
    RoastingOrder, RoastingOrderLine
)
from app_coffee import _get_green_warehouse, _get_roasted_warehouse


def _setup_test_environment(db_session):
    gw = _get_green_warehouse()
    rw = _get_roasted_warehouse()

    branches = Location.get_operational_branches()
    if branches:
        branch = branches[0]
    else:
        branch = Location(name_ar="فرع التست للتحقق", type="branch", is_active=True)
        db_session.add(branch)
        db_session.flush()

    item_g = Item.query.filter_by(sku="GB-TEST-DRAFT-01").first()
    if not item_g:
        item_g = Item(sku="GB-TEST-DRAFT-01", name_ar="بن أخضر اختبار مسودات", storage_unit="كجم", cost=35.0)
        db_session.add(item_g)
        db_session.flush()

    ci_g = CoffeeItem.query.filter_by(xero_item_code="GB-TEST-DRAFT-01").first()
    if not ci_g:
        ci_g = CoffeeItem(
            item_id=item_g.id,
            xero_item_code="GB-TEST-DRAFT-01",
            prefix="GB",
            base_code="DRAFT-G01",
            coffee_type="green",
            name_ar="بن أخضر اختبار مسودات",
            xero_cost_price=35.0
        )
        db_session.add(ci_g)
        db_session.flush()

    item_r = Item.query.filter_by(sku="RB-TEST-DRAFT-SALE").first()
    if not item_r:
        item_r = Item(sku="RB-TEST-DRAFT-SALE", name_ar="بن محمص اختبار بيع مسودات", storage_unit="كجم", cost=55.0)
        db_session.add(item_r)
        db_session.flush()

    ci_r = CoffeeItem.query.filter_by(xero_item_code="RB-TEST-DRAFT-SALE").first()
    if not ci_r:
        ci_r = CoffeeItem(
            item_id=item_r.id,
            xero_item_code="RB-TEST-DRAFT-SALE",
            prefix="RB",
            base_code="DRAFT-R01",
            coffee_type="roasted",
            name_ar="بن محمص اختبار بيع مسودات",
            xero_cost_price=55.0,
            sale_price_per_kg=120.0
        )
        db_session.add(ci_r)
        db_session.flush()

    db_session.commit()
    return gw, rw, branch, ci_g, ci_r


def test_green_issue_draft_lock_and_resume(admin_client, db_session):
    """التحقق من كشف وقفل مسودات سندات صرف البن الأخضر (للتحميص وللبيع)."""
    gw, rw, branch, ci_g, ci_r = _setup_test_environment(db_session)

    # 1. تنظيف مسودات الأخضر للتحميص السابقة
    GreenCoffeeIssue.query.filter_by(issue_type="roasting", status="draft").delete()
    db_session.commit()

    # فحص API بدون مسودة
    res = admin_client.get("/coffee/api/next-doc-number?category=green&type=roasting")
    assert res.status_code == 200
    data = res.get_json()
    assert data["has_draft"] is False

    # إنشاء مسودة صرف بن أخضر للتحميص
    draft_gi = GreenCoffeeIssue(
        doc_number="GI-TEST-DRAFT-01",
        issue_date=date.today(),
        issue_time="10:00",
        issue_type="roasting",
        issue_reason="صرف للتحميص تجريبي",
        warehouse_id=gw.id,
        coffee_item_id=ci_g.id,
        qty_kg=60.0,
        unit_cost=35.0,
        total_cost=2100.0,
        status="draft"
    )
    db_session.add(draft_gi)
    db_session.commit()

    # فحص API مجدداً مع وجود المسودة
    res = admin_client.get("/coffee/api/next-doc-number?category=green&type=roasting")
    assert res.status_code == 200
    data = res.get_json()
    assert data["has_draft"] is True
    assert data["draft"]["id"] == draft_gi.id
    assert data["draft"]["doc_number"] == "GI-TEST-DRAFT-01"

    # فحص طلب GET مع نوع roasting يوجه للمسودة المفتوحة
    res_get = admin_client.get("/coffee/green/issues/new?type=roasting", follow_redirects=False)
    assert res_get.status_code == 302
    assert f"/coffee/green/issues/{draft_gi.id}/edit" in res_get.location

    # فحص طلب POST لمنع إنشاء مسودة ثانية لنفس الغرض
    form_data = {
        "issue_type": "roasting",
        "issue_reason": "محاولة مسودة جديدة مكررة",
        "issue_date": date.today().isoformat(),
        "line_coffee_item_id[]": [str(ci_g.id)],
        "line_qty_kg[]": ["60.0"],
        "form_action": "draft"
    }
    res_post = admin_client.post("/coffee/green/issues/new", data=form_data, follow_redirects=False)
    assert res_post.status_code == 302
    assert f"/coffee/green/issues/{draft_gi.id}/edit" in res_post.location


def test_roasted_sale_dispatch_draft_lock_and_resume(admin_client, db_session):
    """التحقق من كشف وقفل مسودة سند صرف البن المحمص للبيع لعميل معين."""
    gw, rw, branch, ci_g, ci_r = _setup_test_environment(db_session)
    test_cust = "كافيه الصفا التجريبي"

    # تنظيف مسودات سابقة لهذا العميل
    RoastedCoffeeDispatch.query.filter_by(dispatch_type="sale", customer_name=test_cust, status="draft").delete()
    db_session.commit()

    # إنشاء مسودة سند صرف للبيع للعميل
    draft_sale = RoastedCoffeeDispatch(
        doc_number="RB-TEST-DRAFT-SALE-01",
        dispatch_date=date.today(),
        dispatch_time="11:00",
        dispatch_type="sale",
        issue_reason="بيع محمص للعميل",
        source_warehouse_id=rw.id,
        coffee_item_id=ci_r.id,
        qty_kg=10.0,
        unit_cost=55.0,
        total_cost=550.0,
        customer_name=test_cust,
        delivery_method="shipping",
        shipping_company="سمسا",
        shipping_destination="الرياض",
        status="draft"
    )
    db_session.add(draft_sale)
    db_session.commit()

    # فحص API لكشف المسودة للعميل
    res = admin_client.get(f"/coffee/api/next-doc-number?category=roasted&type=sale&customer_name={test_cust}")
    assert res.status_code == 200
    data = res.get_json()
    assert data["has_draft"] is True
    assert data["draft"]["id"] == draft_sale.id

    # فحص طلب GET مع اسم العميل يوجه لمسودته القائمة
    res_get = admin_client.get(f"/coffee/roasted/dispatches/new?type=sale&customer_name={test_cust}", follow_redirects=False)
    assert res_get.status_code == 302
    assert f"/coffee/roasted/dispatches/{draft_sale.id}/edit" in res_get.location

    # فحص طلب POST مع حفظ مسودة لنفس العميل يوجه لمسودته القائمة ويمنع التكرار
    form_data = {
        "dispatch_type": "sale",
        "customer_name": test_cust,
        "dispatch_date": date.today().isoformat(),
        "line_coffee_item_id[]": [str(ci_r.id)],
        "line_qty_kg[]": ["5.0"],
        "form_action": "draft"
    }
    res_post = admin_client.post("/coffee/roasted/dispatches/new", data=form_data, follow_redirects=False)
    assert res_post.status_code == 302
    assert f"/coffee/roasted/dispatches/{draft_sale.id}/edit" in res_post.location


def test_roasting_order_draft_lock_and_resume(admin_client, db_session):
    """التحقق من كشف وقفل مسودة أمر تشغيل التحميص."""
    gw, rw, branch, ci_g, ci_r = _setup_test_environment(db_session)

    # تنظيف مسودات أوامر التحميص السابقة
    RoastingOrder.query.filter_by(status="draft").delete()
    db_session.commit()

    # إنشاء مسودة أمر تحميص
    draft_ro = RoastingOrder(
        order_number="RO-TEST-DRAFT-01",
        order_date=date.today(),
        order_time="09:00",
        green_warehouse_id=gw.id,
        roasted_warehouse_id=rw.id,
        status="draft"
    )
    db_session.add(draft_ro)
    db_session.commit()

    # فحص API أمر التحميص
    res = admin_client.get("/coffee/api/next-doc-number?category=roasting")
    assert res.status_code == 200
    data = res.get_json()
    assert data["has_draft"] is True
    assert data["draft"]["id"] == draft_ro.id
    assert data["draft"]["doc_number"] == "RO-TEST-DRAFT-01"

    # فحص POST لإنشاء أمر تحميص جديد بوجود مسودة مفتوحة: يوجه للمسودة القائمة
    form_data = {
        "order_date": date.today().isoformat(),
        "order_time": "10:00",
        "green_item_id[]": [str(ci_g.id)],
        "green_qty_kg[]": ["30.0"],
        "roasted_item_id[]": [str(ci_r.id)],
    }
    res_post = admin_client.post("/coffee/roasting/new", data=form_data, follow_redirects=False)
    assert res_post.status_code == 302
    assert f"/coffee/roasting/{draft_ro.id}" in res_post.location


def test_transfer_and_purchase_draft_lock_and_resume(admin_client, db_session):
    """التحقق من قفل واستئناف مسودات التحويل المخزني وسندات الاستلام للمشتريات."""
    gw, rw, branch, ci_g, ci_r = _setup_test_environment(db_session)

    # 1. التحويل المخزني: إنشاء مسودة تحويل بين موقعين
    TransferOrder.query.filter_by(from_location_id=rw.id, to_location_id=branch.id, status="draft").delete()
    db_session.commit()

    draft_tr = TransferOrder(
        from_location_id=rw.id,
        to_location_id=branch.id,
        status="draft",
        attachments="[]"
    )
    db_session.add(draft_tr)
    db_session.commit()

    # محاولة إرسال POST لإنشاء مسودة ثانية لنفس المسار: توجه للمسودة القائمة
    res_tr = admin_client.post("/transfers/new", data={"from_location_id": rw.id, "to_location_id": branch.id}, follow_redirects=False)
    assert res_tr.status_code == 302
    assert f"/transfers/{draft_tr.id}" in res_tr.location

    # فحص طلب GET مع المسار
    res_tr_get = admin_client.get(f"/transfers/new?from_location_id={rw.id}&to_location_id={branch.id}", follow_redirects=False)
    assert res_tr_get.status_code == 302
    assert f"/transfers/{draft_tr.id}" in res_tr_get.location

    # 2. سندات الاستلام: إنشاء مسودة لمورد وفرع
    test_supplier = "مورد البن الدولي التجريبي"
    PurchaseTransaction.query.filter_by(location_id=branch.id, supplier_name=test_supplier, status="draft").delete()
    db_session.commit()

    draft_p = PurchaseTransaction(
        location_id=branch.id,
        supplier_name=test_supplier,
        invoice_number="INV-DRAFT-TEST-01",
        invoice_date=date.today(),
        status="draft",
        attachments="[]"
    )
    db_session.add(draft_p)
    db_session.commit()

    # محاولة إنشاء مسودة شراء لنفس المورد والفرع: توجه للمسودة القائمة
    p_data = {
        "location_id": branch.id,
        "supplier_name": test_supplier,
        "invoice_number": "INV-DRAFT-TEST-DUP",
        "invoice_date": date.today().isoformat()
    }
    res_p = admin_client.post("/purchases/new", data=p_data, follow_redirects=False)
    assert res_p.status_code == 302
    assert f"/purchases/{draft_p.id}" in res_p.location


def test_count_session_draft_lock_and_resume(admin_client, db_session):
    """التحقق من قفل واستئناف جلسات الجرد المفتوحة لنفس الموقع."""
    gw, rw, branch, ci_g, ci_r = _setup_test_environment(db_session)

    # تنظيف جلسات سابقة
    CountSession.query.filter(CountSession.location_id == branch.id, CountSession.status.in_(["draft", "open", "in_progress"])).delete()
    db_session.commit()

    draft_cs = CountSession(
        session_number="CNT-TEST-DRAFT-01",
        location_id=branch.id,
        status="draft",
        count_date=date.today()
    )
    db_session.add(draft_cs)
    db_session.commit()

    # محاولة فتح جلسة جرد جديدة لنفس الفرع: توجه للجلسة المفتوحة
    res_c = admin_client.post("/counts/new", data={"location_id": branch.id}, follow_redirects=False)
    assert res_c.status_code == 302
    assert f"/counts/{draft_cs.id}" in res_c.location

    # فحص طلب GET
    res_c_get = admin_client.get(f"/counts/new?location_id={branch.id}", follow_redirects=False)
    assert res_c_get.status_code == 302
    assert f"/counts/{draft_cs.id}" in res_c_get.location


def test_delete_draft_green_issue(admin_client, db_session):
    """التحقق من مسار حذف مسودة سند صرف البن الأخضر وإلغائها وتحرير الرقم."""
    gw, rw, branch, ci_g, ci_r = _setup_test_environment(db_session)

    draft_gi = GreenCoffeeIssue(
        doc_number="GI-TEST-DEL-01",
        issue_date=date.today(),
        issue_time="10:00",
        issue_type="roasting",
        issue_reason="مسودة للحذف",
        warehouse_id=gw.id,
        coffee_item_id=ci_g.id,
        qty_kg=20.0,
        unit_cost=35.0,
        total_cost=700.0,
        status="draft"
    )
    db_session.add(draft_gi)
    db_session.commit()

    draft_id = draft_gi.id

    # إرسال طلب الحذف
    res = admin_client.post(f"/coffee/green/issues/{draft_id}/delete", follow_redirects=True)
    assert res.status_code == 200

    # التأكد من حذف السند من قاعدة البيانات
    assert GreenCoffeeIssue.query.get(draft_id) is None

