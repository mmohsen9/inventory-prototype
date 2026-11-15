"""
tests/test_coffee_draft_lock_and_resume.py — اختبارات قفل واستئناف المسودات المفتوحة لسندات صرف المحمص للفروع
"""
import pytest
from datetime import date
from models import Location, Item, StockBalance, record_stock_movement
from models_coffee import CoffeeItem, RoastedCoffeeDispatch, RoastedCoffeeDispatchLine
from app_coffee import _get_roasted_warehouse


def _setup_test_branch_and_item(db_session):
    rw = _get_roasted_warehouse()
    branches = Location.get_operational_branches()
    if branches:
        branch = branches[0]
    else:
        branch = Location(name_ar="فرع التست للتحقق", type="branch", is_active=True)
        db_session.add(branch)
        db_session.flush()

    item_rb = Item.query.filter_by(sku="RB-DRAFT-TEST-01").first()
    if not item_rb:
        item_rb = Item(sku="RB-DRAFT-TEST-01", name_ar="بن محمص اختبار المسودات", storage_unit="كجم", cost=45.0)
        db_session.add(item_rb)
        db_session.flush()

    ci_rb = CoffeeItem.query.filter_by(xero_item_code="RB-DRAFT-TEST-01").first()
    if not ci_rb:
        ci_rb = CoffeeItem(
            item_id=item_rb.id,
            xero_item_code="RB-DRAFT-TEST-01",
            prefix="RB",
            base_code="DRAFT-TEST-01",
            coffee_type="roasted",
            name_ar="بن محمص اختبار المسودات",
            xero_cost_price=45.0
        )
        db_session.add(ci_rb)
        db_session.flush()

    # رصيد أولي بالمحمصة
    bal = StockBalance.query.filter_by(item_id=item_rb.id, location_id=rw.id).first()
    if not bal:
        record_stock_movement(item_rb.id, rw.id, "initial", 100.0, notes="رصيد أولي لفحص المسودة")
        db_session.commit()

    return branch, ci_rb, rw


def test_api_next_doc_number_detects_branch_draft(admin_client, db_session):
    """التحقق من أن API رقم السند يكشف وجود مسودة مفتوحة للفرع ويعيد تفاصيلها."""
    branch, ci_rb, rw = _setup_test_branch_and_item(db_session)

    # تنظيف أي مسودات سابقة للفرع
    RoastedCoffeeDispatch.query.filter_by(branch_location_id=branch.id, status="draft").delete()
    db_session.commit()

    # 1. الاستعلام بدون وجود مسودة
    res = admin_client.get(f"/coffee/api/next-doc-number?category=roasted&type=branch&branch_id={branch.id}")
    assert res.status_code == 200
    data = res.get_json()
    assert data["success"] is True
    assert data["has_draft"] is False
    assert data["draft"] is None

    # 2. إنشاء مسودة مفتوحة للفرع
    draft = RoastedCoffeeDispatch(
        doc_number="MP-TEST-DRAFT-01",
        dispatch_date=date.today(),
        dispatch_time="11:00",
        dispatch_type="branch",
        issue_reason="مسودة تجريبية للفرع",
        source_warehouse_id=rw.id,
        branch_location_id=branch.id,
        coffee_item_id=ci_rb.id,
        qty_kg=12.0,
        unit_cost=45.0,
        total_cost=540.0,
        status="draft",
        notes="مسودة مفتوحة للاختبار"
    )
    db_session.add(draft)
    db_session.flush()
    line = RoastedCoffeeDispatchLine(
        dispatch_id=draft.id,
        line_number=1,
        coffee_item_id=ci_rb.id,
        qty_kg=12.0,
        unit_cost=45.0,
        total_cost=540.0,
        notes="سطر تجريبي"
    )
    db_session.add(line)
    db_session.commit()

    # 3. الاستعلام مجدداً مع وجود المسودة
    res = admin_client.get(f"/coffee/api/next-doc-number?category=roasted&type=branch&branch_id={branch.id}")
    assert res.status_code == 200
    data = res.get_json()
    assert data["success"] is True
    assert data["has_draft"] is True
    assert data["draft"] is not None
    assert data["draft"]["id"] == draft.id
    assert data["draft"]["doc_number"] == "MP-TEST-DRAFT-01"
    assert data["draft"]["total_qty_kg"] == 12.0
    assert data["doc_number"] == "MP-TEST-DRAFT-01"


def test_get_dispatch_new_redirects_to_open_draft(admin_client, db_session):
    """التحقق من أن طلب صفحة إنشاء السند مع تحديد الفرع يوجّه تلقائياً للمسودة المفتوحة إن وُجدت."""
    branch, ci_rb, rw = _setup_test_branch_and_item(db_session)

    # ضمان وجود مسودة مفتوحة للفرع
    draft = RoastedCoffeeDispatch.query.filter_by(branch_location_id=branch.id, status="draft").first()
    if not draft:
        draft = RoastedCoffeeDispatch(
            doc_number="MP-TEST-DRAFT-02",
            dispatch_date=date.today(),
            dispatch_time="11:00",
            dispatch_type="branch",
            source_warehouse_id=rw.id,
            branch_location_id=branch.id,
            coffee_item_id=ci_rb.id,
            qty_kg=8.0,
            unit_cost=45.0,
            total_cost=360.0,
            status="draft"
        )
        db_session.add(draft)
        db_session.commit()

    # طلب GET مع معرّف الفرع
    res = admin_client.get(f"/coffee/roasted/dispatches/new?branch_id={branch.id}", follow_redirects=False)
    assert res.status_code == 302
    assert f"/coffee/roasted/dispatches/{draft.id}/edit" in res.location

    # طلب GET مع المتابعة follow_redirects
    res_followed = admin_client.get(f"/coffee/roasted/dispatches/new?branch_id={branch.id}", follow_redirects=True)
    assert res_followed.status_code == 200
    assert draft.doc_number.encode("utf-8") in res_followed.data


def test_post_dispatch_new_blocks_duplicate_branch_draft(admin_client, db_session):
    """التحقق من أن حفظ مسودة جديدة للفرع محظور وموجّه للمسودة القائمة لمنع تكرار المسودات."""
    branch, ci_rb, rw = _setup_test_branch_and_item(db_session)

    # تنظيف أي مسودات سابقة ثم إنشاء مسودة واحدة
    RoastedCoffeeDispatch.query.filter_by(branch_location_id=branch.id, status="draft").delete()
    db_session.commit()

    draft = RoastedCoffeeDispatch(
        doc_number="MP-TEST-DRAFT-03",
        dispatch_date=date.today(),
        dispatch_time="11:00",
        dispatch_type="branch",
        source_warehouse_id=rw.id,
        branch_location_id=branch.id,
        coffee_item_id=ci_rb.id,
        qty_kg=5.0,
        unit_cost=45.0,
        total_cost=225.0,
        status="draft"
    )
    db_session.add(draft)
    db_session.commit()

    initial_draft_count = RoastedCoffeeDispatch.query.filter_by(branch_location_id=branch.id, status="draft").count()
    assert initial_draft_count == 1

    # محاولة إرسال POST لحفظ مسودة جديدة لنفس الفرع
    form_data = {
        "dispatch_type": "branch",
        "branch_id": str(branch.id),
        "branch_location_id": str(branch.id),
        "dispatch_date": date.today().isoformat(),
        "line_coffee_item_id[]": [str(ci_rb.id)],
        "line_qty[]": ["10.0"],
        "line_unit_cost[]": ["45.0"],
        "form_action": "draft",
    }
    res = admin_client.post("/coffee/roasted/dispatches/new", data=form_data, follow_redirects=False)
    assert res.status_code == 302
    assert f"/coffee/roasted/dispatches/{draft.id}/edit" in res.location

    # التأكد من عدم زيادة عدد المسودات
    final_draft_count = RoastedCoffeeDispatch.query.filter_by(branch_location_id=branch.id, status="draft").count()
    assert final_draft_count == 1


def test_delete_draft_roasted_dispatch(admin_client, db_session):
    """التحقق من أن مسار حذف المسودة يقوم بإلغائها بالكامل وتحرير الرقم للفرع."""
    branch, ci_rb, rw = _setup_test_branch_and_item(db_session)

    draft = RoastedCoffeeDispatch.query.filter_by(branch_location_id=branch.id, status="draft").first()
    if not draft:
        draft = RoastedCoffeeDispatch(
            doc_number="MP-TEST-DRAFT-04",
            dispatch_date=date.today(),
            dispatch_time="11:00",
            dispatch_type="branch",
            source_warehouse_id=rw.id,
            branch_location_id=branch.id,
            coffee_item_id=ci_rb.id,
            qty_kg=5.0,
            unit_cost=45.0,
            total_cost=225.0,
            status="draft"
        )
        db_session.add(draft)
        db_session.commit()

    draft_id = draft.id

    # إرسال طلب الحذف
    res = admin_client.post(f"/coffee/roasted/dispatches/{draft_id}/delete", follow_redirects=True)
    assert res.status_code == 200

    # التحقق من أن المسودة حُذفت من قاعدة البيانات
    deleted_check = RoastedCoffeeDispatch.query.get(draft_id)
    assert deleted_check is None

    # التحقق من أن API لم يعد يرى مسودة مفتوحة للفرع
    res_api = admin_client.get(f"/coffee/api/next-doc-number?category=roasted&type=branch&branch_id={branch.id}")
    data = res_api.get_json()
    assert data["has_draft"] is False
