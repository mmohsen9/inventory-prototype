"""
tests/test_coffee_features_v3.py
فحوصات الميزات المتقدمة لقسم القهوة:
1. تعدد الأصناف في سندات صرف البن المحمص (RoastedCoffeeDispatchLine).
2. احتساب إجماليات السند وفاتورة Xero المتعددة البنود.
3. تعديل وتعدد أصناف البن الأخضر في أمر التحميص.
4. اعتماد وتسوية الجرد الفعلي للمخزون (الأخضر والمحمص) وتحديث الأرصدة.
"""
import pytest
from datetime import date
from models import db, Location, Item, StockBalance, StockMovement, User, record_stock_movement, PurchaseTransaction, PurchaseLine, Supplier
from models_coffee import (
    CoffeeItem, CoffeeItemLink, RoastingOrder, RoastingOrderLine,
    RoastedCoffeeDispatch, RoastedCoffeeDispatchLine, CoffeeAuditLog,
    GreenCoffeeIssue, CoffeeSetting, CoffeeCustomer
)
from coffee_xero import (
    build_roasted_issue_sale_invoice_payload,
    build_roasted_dispatch_branch_credit_note_payload,
    build_green_issue_sale_invoice_payload,
    build_green_issue_credit_note_payload,
    post_roasted_dispatch_to_xero,
    post_green_issue_to_xero,
    _parse_xero_error
)
from unittest.mock import patch, MagicMock


def test_roasted_dispatch_multi_lines_and_xero_payload(db_session):
    """التحقق من إنشاء سند صرف محمص متعدد الأصناف وبناء فاتورة Xero المتعددة."""
    rw = Location(name_ar="مستودع البن المحمص", name_en="Roasted Coffee Warehouse", type="warehouse")
    db_session.add(rw)
    db_session.flush()

    # صنف محمص 1
    it1 = Item(sku="RB-COLOMBIA", name_ar="كولومبيا محمص", storage_unit="كجم", cost=50.0)
    db_session.add(it1)
    db_session.flush()
    ci1 = CoffeeItem(item_id=it1.id, xero_item_code="RB COLOMBIA", prefix="RB", base_code="COLOMBIA", coffee_type="roasted", sale_price_per_kg=85.0)
    db_session.add(ci1)

    # صنف محمص 2
    it2 = Item(sku="RB-ETHIOPIA", name_ar="إثيوبيا محمص", storage_unit="كجم", cost=55.0)
    db_session.add(it2)
    db_session.flush()
    ci2 = CoffeeItem(item_id=it2.id, xero_item_code="RB ETHIOPIA", prefix="RB", base_code="ETHIOPIA", coffee_type="roasted", sale_price_per_kg=95.0)
    db_session.add(ci2)
    db_session.flush()

    # إنشاء سند صرف بيع متعدد الأصناف
    doc_num = RoastedCoffeeDispatch.generate_next_doc_number()
    dispatch = RoastedCoffeeDispatch(
        doc_number=doc_num,
        dispatch_date=date.today(),
        source_warehouse_id=rw.id,
        dispatch_type="sale",
        customer_name="مقهى الأمل",
        unit_cost=52.5,
        total_cost=775.0,
        notes="سند مبيعات متعدد"
    )
    db_session.add(dispatch)
    db_session.flush()

    # سطر 1: 10 كجم كولومبيا بسعر 85
    ln1 = RoastedCoffeeDispatchLine(
        dispatch_id=dispatch.id,
        line_number=1,
        coffee_item_id=ci1.id,
        qty_kg=10.0,
        unit_cost=50.0,
        total_cost=500.0,
        sale_price_per_kg=85.0,
        total_sale_amount=850.0,
        notes="دفعة أولى"
    )
    # سطر 2: 5 كجم إثيوبيا بسعر 95
    ln2 = RoastedCoffeeDispatchLine(
        dispatch_id=dispatch.id,
        line_number=2,
        coffee_item_id=ci2.id,
        qty_kg=5.0,
        unit_cost=55.0,
        total_cost=275.0,
        sale_price_per_kg=95.0,
        total_sale_amount=475.0,
        notes="دفعة ثانية"
    )
    db_session.add_all([ln1, ln2])
    db_session.commit()

    # التحقق من الخاصيات
    assert len(dispatch.all_lines) == 2
    assert dispatch.total_qty_kg == 15.0
    assert dispatch.total_invoice_amount == 1325.0  # 850 + 475

    # فحص بناء فاتورة مبيعات Xero
    payload = build_roasted_issue_sale_invoice_payload(dispatch)
    assert payload["Type"] == "ACCREC"
    assert payload["Contact"]["Name"] == "مقهى الأمل"
    assert payload["Reference"] == doc_num
    assert len(payload["LineItems"]) == 2

    # فحص بنود الفاتورة
    line_codes = [li["ItemCode"] for li in payload["LineItems"]]
    assert "RB COLOMBIA" in line_codes
    assert "RB ETHIOPIA" in line_codes


def test_roasting_order_multi_lines(db_session):
    """التحقق من دعم أوامر التحميص لخطوط بن أخضر متعددة واحتساب الإجماليات."""
    gw = Location(name_ar="مستودع البن الأخضر", name_en="Green Warehouse", type="warehouse")
    rw = Location(name_ar="مستودع البن المحمص", name_en="Roasted Warehouse", type="warehouse")
    db_session.add_all([gw, rw])
    db_session.flush()

    it_g1 = Item(sku="GB-1", name_ar="أخضر 1", storage_unit="كجم", cost=30.0)
    it_g2 = Item(sku="GB-2", name_ar="أخضر 2", storage_unit="كجم", cost=40.0)
    it_r = Item(sku="RB-BLEND", name_ar="مزيج محمص", storage_unit="كجم", cost=45.0)
    db_session.add_all([it_g1, it_g2, it_r])
    db_session.flush()

    ci_g1 = CoffeeItem(item_id=it_g1.id, xero_item_code="GB 1", prefix="GB", base_code="1", coffee_type="green", default_sack_weight=30.0)
    ci_g2 = CoffeeItem(item_id=it_g2.id, xero_item_code="GB 2", prefix="GB", base_code="2", coffee_type="green", default_sack_weight=60.0)
    ci_r = CoffeeItem(item_id=it_r.id, xero_item_code="RB BLEND", prefix="RB", base_code="BLEND", coffee_type="roasted")
    db_session.add_all([ci_g1, ci_g2, ci_r])
    db_session.flush()

    order = RoastingOrder(
        order_number="RO 999",
        order_date=date.today(),
        green_warehouse_id=gw.id,
        roasted_warehouse_id=rw.id,
        status="draft"
    )
    db_session.add(order)
    db_session.flush()

    ln1 = RoastingOrderLine(order_id=order.id, line_number=1, green_item_id=ci_g1.id, roasted_item_id=ci_r.id, green_qty_kg=30.0, green_qty_sacks=1.0, green_unit_cost=30.0, green_total_cost=900.0)
    ln2 = RoastingOrderLine(order_id=order.id, line_number=2, green_item_id=ci_g2.id, roasted_item_id=ci_r.id, green_qty_kg=60.0, green_qty_sacks=1.0, green_unit_cost=40.0, green_total_cost=2400.0)
    db_session.add_all([ln1, ln2])
    db_session.commit()

    assert len(order.lines) == 2
    assert sum(l.green_qty_kg for l in order.lines) == 90.0


def test_inventory_count_adjustment_flow(client, db_session):
    """التحقق من مسار اعتماد وتسوية الجرد الفعلي للمخزون وتسجيل حركات التسوية."""
    from models import record_stock_movement

    admin = User(name="مدير المخزون", role="admin", password="password123", active=True)
    admin.set_password("pass123")
    db_session.add(admin)
    db_session.flush()

    gw = Location(name_ar="مستودع البن الأخضر", name_en="Green Coffee Warehouse", type="warehouse")
    rw = Location(name_ar="مستودع البن المحمص", name_en="Roasted Coffee Warehouse", type="warehouse")
    db_session.add_all([gw, rw])
    db_session.flush()

    it = Item(sku="GB-COUNT", name_ar="بن أخضر للجرد", storage_unit="كجم", cost=40.0)
    db_session.add(it)
    db_session.flush()
    ci = CoffeeItem(item_id=it.id, xero_item_code="GB COUNT", prefix="GB", base_code="COUNT", coffee_type="green", default_sack_weight=30.0)
    db_session.add(ci)
    db_session.flush()

    # رصيد افتتاحي دفتري 50 كجم
    record_stock_movement(it.id, gw.id, "initial", 50.0, notes="رصيد أولي", user_id=admin.id)
    db_session.commit()

    bal_before = StockBalance.query.filter_by(item_id=it.id, location_id=gw.id).first()
    assert bal_before.quantity == 50.0

    # تسجيل الدخول كمدير
    with client.session_transaction() as sess:
        sess["user_id"] = admin.id

    # إرسال نموذج الجرد الفعلي: تم جرد 55 كجم (زيادة 5 كجم)
    resp = client.post("/coffee/inventory-count/apply", data={
        "coffee_type": "green",
        "adjustment_notes": "جرد ربع سنوي فعلي",
        "coffee_item_ids[]": [str(ci.id)],
        "actual_qtys[]": ["55.0"]
    }, follow_redirects=True)

    assert resp.status_code == 200

    # التحقق من تحديث الرصيد الدفتري إلى 55.0 كجم
    bal_after = StockBalance.query.filter_by(item_id=it.id, location_id=gw.id).first()
    assert bal_after.quantity == 55.0

    # التحقق من تسجيل حركة المخزون
    mvt = StockMovement.query.filter_by(item_id=it.id, movement_type="adjustment").first()
    assert mvt is not None
    assert mvt.quantity_change == 5.0

    # التحقق من تسجيل سجل التدقيق
    audit = CoffeeAuditLog.query.filter_by(event_type="inventory_count_applied").first()
    assert audit is not None


def test_roasting_loss_reference_range_17_to_20(db_session):
    """التحقق من اعتماد نطاق نسبة الفاقد 17% إلى 20% للتنبيه."""
    line = RoastingOrderLine(green_qty_kg=100.0, reference_loss_pct=20.0)

    # 1. ناتج 82 كجم -> فاقد 18 كجم (18%) -> ضمن النطاق الطبيعي 17% - 20%
    line.roasted_qty_kg_actual = 82.0
    line.calc_loss()
    assert line.loss_kg == 18.0
    assert line.loss_pct == 18.0
    assert line.loss_exceeds_reference is False

    # 2. ناتج 80 كجم -> فاقد 20 كجم (20%) -> الحد الأقصى للنطاق الطبيعي
    line.roasted_qty_kg_actual = 80.0
    line.calc_loss()
    assert line.loss_kg == 20.0
    assert line.loss_pct == 20.0
    assert line.loss_exceeds_reference is False

    # 3. ناتج 78 كجم -> فاقد 22 كجم (22%) -> يتجاوز 20% -> تنبيه تجاوز
    line.roasted_qty_kg_actual = 78.0
    line.calc_loss()
    assert line.loss_kg == 22.0
    assert line.loss_pct == 22.0
    assert line.loss_exceeds_reference is True


def test_green_coffee_issue_multi_lines_and_xero_payload(db_session):
    """التحقق من إنشاء سند صرف بن أخضر متعدد الأصناف وبناء فاتورة مبيعات مسودة في Xero."""
    from models_coffee import GreenCoffeeIssue, GreenCoffeeIssueLine
    from coffee_xero import build_green_issue_sale_invoice_payload

    gw = Location(name_ar="مستودع الأخضر", name_en="Green Warehouse", type="warehouse")
    db_session.add(gw)
    db_session.flush()

    it1 = Item(sku="GB-COL", name_ar="كولومبيا أخضر", storage_unit="كجم", cost=35.0)
    it2 = Item(sku="GB-BRA", name_ar="برازيلي أخضر", storage_unit="كجم", cost=30.0)
    db_session.add_all([it1, it2])
    db_session.flush()

    ci1 = CoffeeItem(item_id=it1.id, xero_item_code="GB COLOMBIA", prefix="GB", base_code="COLOMBIA", coffee_type="green", default_sack_weight=60.0, sale_price_per_kg=55.0)
    ci2 = CoffeeItem(item_id=it2.id, xero_item_code="GB BRAZIL", prefix="GB", base_code="BRAZIL", coffee_type="green", default_sack_weight=60.0, sale_price_per_kg=48.0)
    db_session.add_all([ci1, ci2])
    db_session.flush()

    doc_num = GreenCoffeeIssue.generate_next_doc_number()
    assert doc_num.startswith(("GB 00", "GB-"))

    issue = GreenCoffeeIssue(
        doc_number=doc_num,
        issue_date=date.today(),
        warehouse_id=gw.id,
        issue_type="sale",
        customer_name="محمصة الشرق",
        issue_reason="بيع بن أخضر خام"
    )
    db_session.add(issue)
    db_session.flush()

    # سطر 1: 120 كجم (2 خيشة) بسعر 55
    ln1 = GreenCoffeeIssueLine(
        issue_id=issue.id, line_number=1, coffee_item_id=ci1.id,
        qty_kg=120.0, qty_sacks=2.0, sack_weight=60.0, unit_cost=35.0, total_cost=4200.0,
        sale_price_per_kg=55.0, total_sale_amount=6600.0
    )
    # سطر 2: 60 كجم (1 خيشة) بسعر 48
    ln2 = GreenCoffeeIssueLine(
        issue_id=issue.id, line_number=2, coffee_item_id=ci2.id,
        qty_kg=60.0, qty_sacks=1.0, sack_weight=60.0, unit_cost=30.0, total_cost=1800.0,
        sale_price_per_kg=48.0, total_sale_amount=2880.0
    )
    db_session.add_all([ln1, ln2])
    db_session.commit()

    assert len(issue.all_lines) == 2
    assert issue.total_qty_kg == 180.0
    assert issue.total_qty_sacks == 3.0
    assert issue.total_invoice_amount == 9480.0

    payload = build_green_issue_sale_invoice_payload(issue)
    assert payload["Type"] == "ACCREC"
    assert payload["Reference"] == doc_num
    assert payload["Reference"].startswith(("GB 00", "GB-"))
    assert payload["Contact"]["Name"] == "محمصة الشرق"
    assert len(payload["LineItems"]) == 2
    codes = [li["ItemCode"] for li in payload["LineItems"]]
    assert "GB COLOMBIA" in codes
    assert "GB BRAZIL" in codes
    amounts = [li["LineAmount"] for li in payload["LineItems"]]
    assert 6600.0 in amounts
    assert 2880.0 in amounts


def test_roasted_sale_dispatch_has_rb00_reference(db_session):
    """التحقق من أن سند صرف المحمص للبيع يولد رقم مرجع RB 00X ويبني فاتورة Xero بنفس المرجع."""
    doc_num = RoastedCoffeeDispatch.generate_next_doc_number()
    assert doc_num.startswith(("RB 00", "RB-"))

    rw = Location(name_ar="مستودع المحمص", name_en="Roasted Warehouse", type="warehouse")
    db_session.add(rw)
    db_session.flush()

    it = Item(sku="RB-SPECIAL", name_ar="مختص محمص", storage_unit="كجم", cost=60.0)
    db_session.add(it)
    db_session.flush()
    ci = CoffeeItem(item_id=it.id, xero_item_code="RB SPECIAL", prefix="RB", base_code="SPECIAL", coffee_type="roasted", sale_price_per_kg=110.0)
    db_session.add(ci)
    db_session.flush()

    dispatch = RoastedCoffeeDispatch(
        doc_number=doc_num,
        dispatch_date=date.today(),
        source_warehouse_id=rw.id,
        dispatch_type="sale",
        customer_name="مؤسسة القهوة الفاخرة",
        coffee_item_id=ci.id,
        qty_kg=25.0,
        sale_price_per_kg=110.0,
        total_sale_amount=2750.0
    )
    db_session.add(dispatch)
    db_session.commit()

    payload = build_roasted_issue_sale_invoice_payload(dispatch)
    assert payload["Type"] == "ACCREC"
    assert payload["Reference"] == doc_num
    assert payload["Reference"].startswith(("RB 00", "RB-"))
    assert payload["Contact"]["Name"] == "مؤسسة القهوة الفاخرة"
    assert payload["LineItems"][0]["ItemCode"] == "RB SPECIAL"
    assert payload["LineItems"][0]["Quantity"] == 25.0
    assert payload["LineItems"][0]["UnitAmount"] == 110.0
    assert payload["LineItems"][0]["LineAmount"] == 2750.0


def test_roasted_branch_dispatch_creates_automatic_transfer_order_and_updates_branch_balance(admin_client, db_session):
    """التحقق من إنشاء سند صرف محمص لفرع يولد سند تحويل وارد للفرع بصنف [sk-0535] بن محمص عام وتحديث المخزون."""
    from models import TransferOrder, TransferLine, StockBalance, record_stock_movement

    rw = Location(name_ar="مستودع البن المحمص", name_en="Roasted Coffee Warehouse", type="warehouse")
    branch = Location(name_ar="فرع الروضة", name_en="Al-Rawdah Branch", type="branch")
    db_session.add_all([rw, branch])
    db_session.flush()

    # إنشاء صنف البن المحمص الفعلي
    item_rb = Item(sku="RB-12025", name_ar="بليند اسبريسو محمص", storage_unit="كجم", cost=50.0)
    db_session.add(item_rb)
    db_session.flush()

    ci_rb = CoffeeItem(item_id=item_rb.id, xero_item_code="RB 12025", prefix="RB", base_code="12025", coffee_type="roasted")
    db_session.add(ci_rb)
    db_session.flush()

    # رصيد أولي في مستودع المحمصة
    record_stock_movement(item_rb.id, rw.id, "initial", 100.0, notes="رصيد أولي بالمحمصة")
    db_session.commit()

    # إرسال طلب صرف للفرع عبر POST /coffee/roasted/dispatches/new
    form_data = {
        "dispatch_type": "branch",
        "branch_id": str(branch.id),
        "dispatch_date": date.today().isoformat(),
        "line_coffee_item_id[]": [str(ci_rb.id)],
        "line_qty[]": ["25.0"],
        "line_unit_cost[]": ["50.0"],
        "line_notes[]": ["شحنة اسبوعية"],
        "notes": "صرف للفرع للتجربة"
    }

    res = admin_client.post("/coffee/roasted/dispatches/new", data=form_data, follow_redirects=True)
    assert res.status_code == 200

    # 1. التحقق من إنشاء سند الصرف doc بسلسلة MP للفروع
    dispatch = RoastedCoffeeDispatch.query.filter_by(branch_location_id=branch.id).first()
    assert dispatch is not None
    assert dispatch.doc_number.startswith(("MP ", "MP-"))
    assert dispatch.qty_kg == 25.0

    # 2. التحقق من خصم رصيد المحمصة للصنف الفعلي RB-12025
    bal_rw = StockBalance.query.filter_by(item_id=item_rb.id, location_id=rw.id).first()
    assert bal_rw.quantity == 75.0  # 100 - 25

    # 3. التحقق من إنشاء سند استلام (مشتريات) للفرع تحت مورد اكياس القهوة المحمصه للفروع
    purchase = PurchaseTransaction.query.filter_by(location_id=branch.id, foodics_reference=dispatch.doc_number).first()
    assert purchase is not None
    assert purchase.status in ("approved", "posted", "failed")
    assert purchase.supplier_name == "اكياس القهوة المحمصه للفروع"

    # 4. التحقق من سطر الاستلام أنه يحمل الصنف العام sk-0535 والملاحظات
    assert len(purchase.lines) == 1
    p_line = purchase.lines[0]
    assert p_line.item.sku == "sk-0535"
    assert p_line.item.name_ar == "بن محمص عام"
    assert p_line.quantity == 25.0
    assert p_line.unit_cost == 50.0
    assert p_line.notes == "بليند اسبريسو محمص"

    # 5. التحقق من زيادة رصيد الفرع بالصنف العام sk-0535
    bal_branch = StockBalance.query.filter_by(item_id=p_line.item_id, location_id=branch.id).first()
    assert bal_branch is not None
    assert bal_branch.quantity == 25.0


def test_operational_locations_and_branches_filtering(db_session):
    """التحقق من حصر المواقع والفروع التشغيلية على فوديكس ومستودعي البن واستبعاد التجريبية."""
    # إنشاء مواقع متنوعة
    # 1. فرع فوديكس حقيقي
    foodics_branch = Location(name_ar="فرع الياسمين", name_en="Alyasmin", type="branch", foodics_id="fd-branch-1")
    # 2. مستودع رئيسي فوديكس
    foodics_wh = Location(name_ar="المستودع الرئيسي", name_en="Main Warehouse", type="warehouse", foodics_id="fd-wh-main")
    # 3. مستودع البن الأخضر
    green_wh = Location(name_ar="مستودع البن الأخضر", name_en="Green Coffee Warehouse", type="warehouse", foodics_id=None)
    # 4. مستودع البن المحمص
    roasted_wh = Location(name_ar="مستودع البن المحمص", name_en="Roasted Coffee Warehouse", type="warehouse", foodics_id=None)
    # 5. فرع تجريبي وهمي غير مربوط بفوديكس
    dummy_branch = Location(name_ar="فرع تجريبي الصحافة", name_en="Dummy Branch", type="branch", foodics_id=None)
    # 6. مستودع تجريبي وهمي
    dummy_wh = Location(name_ar="مستودع تجريبي", name_en="Dummy WH", type="warehouse", foodics_id=None)

    db_session.add_all([foodics_branch, foodics_wh, green_wh, roasted_wh, dummy_branch, dummy_wh])
    db_session.commit()

    # فحص get_operational_locations: يجب أن يعيد فوديكس + مستودعي البن فقط
    op_locs = Location.get_operational_locations()
    op_ids = [loc.id for loc in op_locs]

    assert foodics_branch.id in op_ids
    assert foodics_wh.id in op_ids
    assert green_wh.id in op_ids
    assert roasted_wh.id in op_ids
    assert dummy_branch.id not in op_ids
    assert dummy_wh.id not in op_ids

    # فحص get_operational_branches: يجب أن يعيد فقط فروع فوديكس
    op_branches = Location.get_operational_branches()
    op_branch_ids = [b.id for b in op_branches]

    assert foodics_branch.id in op_branch_ids
    assert dummy_branch.id not in op_branch_ids
    assert foodics_wh.id not in op_branch_ids
    assert green_wh.id not in op_branch_ids
    assert roasted_wh.id not in op_branch_ids


def test_roasted_sale_dispatch_with_customer_signature_and_print(admin_client, db_session):
    """التحقق من حفظ التوقيع الحي للعميل في سند صرف المحمص للبيع وظهوره في صفحة الطباعة."""
    rw = Location(name_ar="مستودع المحمص", name_en="Roasted Warehouse", type="warehouse")
    db_session.add(rw)
    db_session.flush()

    item_rb = Item(sku="RB-COL", name_ar="كولومبيا محمص", storage_unit="كجم", cost=45.0)
    db_session.add(item_rb)
    db_session.flush()

    ci_rb = CoffeeItem(item_id=item_rb.id, xero_item_code="RB 7701", prefix="RB", base_code="7701", coffee_type="roasted", sale_price_per_kg=65.0)
    db_session.add(ci_rb)
    db_session.flush()

    record_stock_movement(item_rb.id, rw.id, "initial", 50.0, notes="رصيد أولي")
    db_session.commit()

    sample_signature_data = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkWPjfDwAEeQHzX7sWvQAAAABJRU5ErkJggg=="

    form_data = {
        "dispatch_type": "sale",
        "customer_name": "محمصة الرواد",
        "dispatch_date": date.today().isoformat(),
        "line_coffee_item_id[]": [str(ci_rb.id)],
        "line_qty[]": ["10.0"],
        "line_sale_price[]": ["70.0"],
        "customer_signature": sample_signature_data,
        "notes": "بيع بن محمص نقدي مع توقيع العميل"
    }

    res = admin_client.post("/coffee/roasted/dispatches/new", data=form_data, follow_redirects=True)
    assert res.status_code == 200

    dispatch = RoastedCoffeeDispatch.query.filter_by(customer_name="محمصة الرواد").first()
    assert dispatch is not None
    assert dispatch.customer_signature == sample_signature_data
    assert dispatch.total_sale_amount == 700.0

    # فحص مسار الطباعة
    print_res = admin_client.get(f"/coffee/roasted/dispatches/{dispatch.id}/print")
    assert print_res.status_code == 200
    html = print_res.data.decode("utf-8")
    assert "سند صرف بن محمص" in html
    assert dispatch.doc_number in html
    assert "محمصة الرواد" in html
    assert sample_signature_data in html
    assert "توقيع العميل الحي" in html
    assert "موقع إلكترونياً باللمس" in html
    assert "التاريخ:" in html
    assert "الوقت:" in html


def test_green_coffee_sale_issue_with_customer_signature_and_print(admin_client, db_session):
    """التحقق من حفظ التوقيع الحي للعميل في سند صرف الأخضر للبيع وظهوره في صفحة الطباعة."""
    gw = Location(name_ar="مستودع الأخضر", name_en="Green Warehouse", type="warehouse")
    db_session.add(gw)
    db_session.flush()

    item_gb = Item(sku="GB-ETH", name_ar="اثيوبي أخضر", storage_unit="كجم", cost=32.0)
    db_session.add(item_gb)
    db_session.flush()

    ci_gb = CoffeeItem(item_id=item_gb.id, xero_item_code="GB ETHIOPIA", prefix="GB", base_code="ETHIOPIA", coffee_type="green", sale_price_per_kg=50.0)
    db_session.add(ci_gb)
    db_session.flush()

    record_stock_movement(item_gb.id, gw.id, "initial", 60.0, notes="رصيد أولي أخضر")
    db_session.commit()

    sample_sig = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkWPjfDwAEeQHzX7sWvQAAAABJRU5ErkJggg=="

    form_data = {
        "issue_type": "sale",
        "issue_reason": "بيع مباشر للعميل",
        "customer_name": "مقهى النخبة المختص",
        "issue_date": date.today().isoformat(),
        "line_coffee_item_id[]": [str(ci_gb.id)],
        "line_qty_kg[]": ["20.0"],
        "line_sale_price[]": ["55.0"],
        "customer_signature": sample_sig,
        "notes": "بيع بن أخضر مع توقيع العميل"
    }

    res = admin_client.post("/coffee/green/issues/new", data=form_data, follow_redirects=True)
    assert res.status_code == 200

    issue = GreenCoffeeIssue.query.filter_by(customer_name="مقهى النخبة المختص").first()
    assert issue is not None
    assert issue.customer_signature == sample_sig

    # فحص مسار الطباعة
    print_res = admin_client.get(f"/coffee/green/issues/{issue.id}/print")
    assert print_res.status_code == 200
    html = print_res.data.decode("utf-8")
    assert "سند صرف بن أخضر" in html
    assert issue.doc_number in html
    assert "مقهى النخبة المختص" in html
    assert sample_sig in html
    assert "موقع إلكترونياً باللمس" in html
    assert "التاريخ:" in html
    assert "الوقت:" in html


def test_roasted_dispatch_draft_mode_has_no_stock_impact(admin_client, db_session):
    """التحقق من حفظ سند صرف المحمص كمسودة وعدم التأثير على المخزون أو إنشاء سند تحويل."""
    from app_coffee import _get_roasted_warehouse
    rw = _get_roasted_warehouse()
    branch = Location.query.filter_by(type="branch").first()
    if not branch:
        branch = Location(name_ar="فرع التحلية", name_en="Tahlia Branch", type="branch")
        db_session.add(branch)
        db_session.flush()

    item_rb = Item(sku="RB-YEMEN", name_ar="يمني محمص", storage_unit="كجم", cost=60.0)
    db_session.add(item_rb)
    db_session.flush()

    ci_rb = CoffeeItem(item_id=item_rb.id, xero_item_code="RB YEMEN", prefix="RB", base_code="YEMEN", coffee_type="roasted")
    db_session.add(ci_rb)
    db_session.flush()

    record_stock_movement(item_rb.id, rw.id, "initial", 100.0, notes="رصيد أولي")
    db_session.commit()

    bal_before = StockBalance.query.filter_by(item_id=item_rb.id, location_id=rw.id).first().quantity

    form_data = {
        "form_action": "draft",  # حفظ كمسودة
        "dispatch_type": "branch",
        "branch_location_id": str(branch.id),
        "dispatch_date": date.today().isoformat(),
        "line_coffee_item_id[]": [str(ci_rb.id)],
        "line_qty_kg[]": ["25.0"],
        "notes": "مسودة صرف للفرع تجريبية"
    }

    res = admin_client.post("/coffee/roasted/dispatches/new", data=form_data, follow_redirects=True)
    assert res.status_code == 200

    dispatch = RoastedCoffeeDispatch.query.filter_by(notes="مسودة صرف للفرع تجريبية").first()
    assert dispatch is not None
    assert dispatch.status == "draft"

    # لم يتم خصم المخزون
    bal_after = StockBalance.query.filter_by(item_id=item_rb.id, location_id=rw.id).first().quantity
    assert bal_after == bal_before

    # لم يتم تسجيل أي حركة صرف محمص
    movements = StockMovement.query.filter_by(reference_type="roasted_dispatch", reference_id=dispatch.id).all()
    assert len(movements) == 0


def test_sale_voucher_approval_requires_signature(admin_client, db_session):
    """التحقق من منع اعتماد سند صرف البيع دون توقيع العميل مع إمكانية حفظه كمسودة."""
    rw = Location(name_ar="مستودع المحمص 2", name_en="Roasted Wh 2", type="warehouse")
    db_session.add(rw)
    db_session.flush()

    item_rb = Item(sku="RB-KENYA", name_ar="كينيا محمص", storage_unit="كجم", cost=55.0)
    db_session.add(item_rb)
    db_session.flush()

    ci_rb = CoffeeItem(item_id=item_rb.id, xero_item_code="RB KENYA", prefix="RB", base_code="KENYA", coffee_type="roasted", sale_price_per_kg=90.0)
    db_session.add(ci_rb)
    db_session.flush()

    record_stock_movement(item_rb.id, rw.id, "initial", 50.0)
    db_session.commit()

    # محاولة الاعتماد دون توقيع -> يجب أن يفشل ويعيد توجيه مع رسالة خطأ
    form_data_no_sig = {
        "form_action": "approve",
        "dispatch_type": "sale",
        "customer_name": "عميل بدون توقيع",
        "dispatch_date": date.today().isoformat(),
        "line_coffee_item_id[]": [str(ci_rb.id)],
        "line_qty_kg[]": ["10.0"],
        "customer_signature": ""  # فارغ!
    }

    res = admin_client.post("/coffee/roasted/dispatches/new", data=form_data_no_sig, follow_redirects=True)
    assert res.status_code == 200
    # يجب ألا ينشأ سند معتمد
    disp = RoastedCoffeeDispatch.query.filter_by(customer_name="عميل بدون توقيع").first()
    assert disp is None

    # ولكن إذا تم اختياره كمسودة دون توقيع -> ينجح حفظه كمسودة
    form_data_draft = form_data_no_sig.copy()
    form_data_draft["form_action"] = "draft"
    form_data_draft["customer_name"] = "عميل مسودة بدون توقيع"

    res_draft = admin_client.post("/coffee/roasted/dispatches/new", data=form_data_draft, follow_redirects=True)
    assert res_draft.status_code == 200
    disp_draft = RoastedCoffeeDispatch.query.filter_by(customer_name="عميل مسودة بدون توقيع").first()
    assert disp_draft is not None
    assert disp_draft.status == "draft"


def test_admin_editing_approved_voucher_updates_stock(admin_client, db_session):
    """التحقق من إمكانية تعديل السند المعتمد لمدير النظام وتحديث حركات وأرصدة المخزون."""
    from app_coffee import _get_roasted_warehouse
    rw = _get_roasted_warehouse()

    item_rb = Item(sku="RB-BRAZIL", name_ar="برازيلي محمص", storage_unit="كجم", cost=45.0)
    db_session.add(item_rb)
    db_session.flush()

    ci_rb = CoffeeItem(item_id=item_rb.id, xero_item_code="RB BRAZIL", prefix="RB", base_code="BRAZIL", coffee_type="roasted")
    db_session.add(ci_rb)
    db_session.flush()

    record_stock_movement(item_rb.id, rw.id, "initial", 100.0)
    db_session.commit()

    branch = Location(name_ar="فرع التخصصي", name_en="Takhassusi Branch", type="branch")
    db_session.add(branch)
    db_session.flush()

    # إنشاء سند صرف فرع معتمد أولي بكمية 10 كجم بدون توقيع عميل لفحص قدرة الإدارة على تعديل الكميات وحركات المخزون
    disp = RoastedCoffeeDispatch(
        doc_number="MP 9901",
        dispatch_date=date.today(),
        source_warehouse_id=rw.id,
        dispatch_type="branch",
        branch_location_id=branch.id,
        coffee_item_id=ci_rb.id,
        qty_kg=10.0,
        status="approved",
        customer_signature=None
    )
    db_session.add(disp)
    record_stock_movement(item_rb.id, rw.id, "roasted_dispatch", -10.0, reference_type="roasted_dispatch", reference_id=disp.id)
    db_session.commit()

    assert disp.status == "approved"
    assert disp.qty_kg == 10.0

    # الرصيد أصبح 90 كجم
    bal = StockBalance.query.filter_by(item_id=item_rb.id, location_id=rw.id).first()
    assert bal.quantity == 90.0

    # تعديل السند من قبل مدير النظام إلى كمية 15 كجم (السند غير موقع فيسمح بتعديل الكميات)
    form_edit = {
        "form_action": "approve",
        "dispatch_type": "branch",
        "branch_location_id": str(branch.id),
        "dispatch_date": date.today().isoformat(),
        "line_coffee_item_id[]": [str(ci_rb.id)],
        "line_qty_kg[]": ["15.0"],
        "notes": "تعديل إداري للكمية"
    }
    edit_res = admin_client.post(f"/coffee/roasted/dispatches/{disp.id}/edit", data=form_edit, follow_redirects=True)
    assert edit_res.status_code == 200

    # فحص تحديث السند
    db_session.refresh(disp)
    assert disp.qty_kg == 15.0

    # فحص تحديث الرصيد: كان 100، خصم 15، فأصبح 85
    bal = StockBalance.query.filter_by(item_id=item_rb.id, location_id=rw.id).first()
    assert bal.quantity == 85.0

def test_roasted_dispatch_branch_credit_note_and_refund(db_session):
    """فحص بناء إشعار دائن مشتريات (ACCPAYCREDIT) لصرف فرع وتسديده بحساب فودكس 631."""
    branch = Location(name_ar="فرع السليمانية", name_en="Sulaymaniyah Branch", type="branch")
    rw = Location(name_ar="مستودع المحمص", name_en="Roasted Wh", type="warehouse")
    db_session.add_all([branch, rw])
    db_session.flush()

    it = Item(sku="RB-YEMEN", name_ar="يمن محمص", storage_unit="كجم", cost=70.0)
    db_session.add(it)
    db_session.flush()
    ci = CoffeeItem(item_id=it.id, xero_item_code="RB YEMEN", prefix="RB", base_code="YEMEN", coffee_type="roasted")
    db_session.add(ci)
    db_session.flush()

    dispatch = RoastedCoffeeDispatch(
        doc_number="RB-3584",
        dispatch_date=date(2026, 10, 19),
        source_warehouse_id=rw.id,
        branch_location_id=branch.id,
        coffee_item_id=ci.id,
        dispatch_type="branch",
        unit_cost=70.0,
        qty_kg=20.0,
        total_cost=1400.0,
        notes="صرف أسبوعي للفرع",
        customer_signature="data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
    )
    db_session.add(dispatch)
    db_session.commit()

    # 1. فحص الـ Payload
    payload = build_roasted_dispatch_branch_credit_note_payload(dispatch)
    assert payload["Type"] == "ACCPAYCREDIT"
    assert payload["Contact"]["Name"] == "سند صرف قهوه محموصه فرع السليمانية"
    assert payload["Reference"] == "RB-3584"
    assert payload["CreditNoteNumber"] == "RB-3584"
    assert payload["Status"] == "AUTHORISED"
    assert len(payload["LineItems"]) == 1
    line = payload["LineItems"][0]
    assert line["ItemCode"] == "RB YEMEN"
    assert line["AccountCode"] == "636"
    assert line["TaxType"] == "NONE"
    assert line["Quantity"] == 20.0
    assert line["LineAmount"] == 1400.0
    assert line["Tracking"] == [{"Name": "Branches", "Option": "Roasted Coffee"}]

    # 2. فحص الترحيل الكامل مع Mock لـ Xero API
    with patch("coffee_xero._get_setting") as mock_setting, \
         patch("requests.post") as mock_post:
        
        def fake_setting(key, default=""):
            if key == "xero_bearer_token": return "fake-token"
            if key == "xero_tenant_id": return "fake-tenant"
            if key == "xero_foodics_inventory_account": return "631"
            if key == "xero_roasted_account": return "636"
            if key == "xero_tracking_category": return "Branches"
            return default
        mock_setting.side_effect = fake_setting

        # Mock رد إنشاء الـ Credit Note
        mock_resp_cn = MagicMock()
        mock_resp_cn.status_code = 200
        mock_resp_cn.json.return_value = {
            "CreditNotes": [{
                "CreditNoteID": "cn-uuid-1234",
                "CreditNoteNumber": "CN-9988"
            }]
        }
        # Mock رد الـ Payment (تسديد 631)
        mock_resp_pay = MagicMock()
        mock_resp_pay.status_code = 200
        mock_resp_pay.json.return_value = {"Payments": [{"PaymentID": "pay-uuid-5678"}]}

        # Mock نجاح إرفاق السند (Attachments)
        mock_resp_att = MagicMock()
        mock_resp_att.status_code = 200
        mock_resp_att.json.return_value = {"Attachments": [{"FileName": "Voucher_RB-3584.pdf"}]}

        mock_post.side_effect = [mock_resp_cn, mock_resp_pay, mock_resp_att]

        res = post_roasted_dispatch_to_xero(dispatch.id)
        assert res["success"] is True
        assert res["type"] == "credit_note"
        assert res["credit_note_id"] == "cn-uuid-1234"

        # التحقق من استدعاء سداد 631 وإرفاق السند
        assert mock_post.call_count == 3
        pay_call = mock_post.call_args_list[1]
        assert "/Payments" in pay_call[0][0]
        pay_json = pay_call[1]["json"]
        payment_record = pay_json["Payments"][0]
        assert payment_record["CreditNote"]["CreditNoteID"] == "cn-uuid-1234"
        assert payment_record["Account"]["Code"] == "631"
        assert payment_record["Amount"] == 1400.0

        att_call = mock_post.call_args_list[2]
        assert "/Attachments/" in att_call[0][0]

        db_session.refresh(dispatch)
        assert dispatch.xero_status == "posted"
        assert dispatch.xero_invoice_id == "cn-uuid-1234"


def test_roasted_dispatch_sale_draft_invoice_with_vat(db_session):
    """فحص بناء فاتورة مبيعات مسودة (Draft ACCREC) لسند صرف بيع بضريبة 15% OUTPUT2."""
    rw = Location(name_ar="مستودع المحمص", name_en="Roasted Wh", type="warehouse")
    db_session.add(rw)
    db_session.flush()

    it = Item(sku="RB-KENYA", name_ar="كينيا محمص", storage_unit="كجم", cost=60.0)
    db_session.add(it)
    db_session.flush()
    ci = CoffeeItem(item_id=it.id, xero_item_code="RB KENYA", prefix="RB", base_code="KENYA", coffee_type="roasted")
    db_session.add(ci)
    db_session.flush()

    dispatch = RoastedCoffeeDispatch(
        doc_number="RB-7711",
        dispatch_date=date(2026, 10, 19),
        source_warehouse_id=rw.id,
        coffee_item_id=ci.id,
        dispatch_type="sale",
        customer_name="شركة المذاق الراقي",
        sale_price_per_kg=120.0,
        qty_kg=10.0,
        total_cost=600.0,
        notes="مبيعات خارجية"
    )
    db_session.add(dispatch)
    db_session.commit()

    # 1. فحص Payload الفاتورة
    payload = build_roasted_issue_sale_invoice_payload(dispatch)
    assert payload["Type"] == "ACCREC"
    assert payload["Status"] == "DRAFT"  # فاتورة مبيعات مسودة في زيرو
    assert payload["Contact"]["Name"] == "شركة المذاق الراقي"
    assert payload["Reference"] == "RB-7711"
    assert len(payload["LineItems"]) == 1
    li = payload["LineItems"][0]
    assert li["TaxType"] == "OUTPUT"  # ضريبة 15%
    assert li["AccountCode"] == "202"  # مبيعات القهوة المحمصة
    assert li["UnitAmount"] == 120.0
    assert li["LineAmount"] == 1200.0

    # 2. فحص الترحيل بـ Mock
    with patch("coffee_xero._get_setting") as mock_setting, \
         patch("requests.post") as mock_post:
        
        def fake_setting(key, default=""):
            if key == "xero_bearer_token": return "fake-token"
            if key == "xero_tenant_id": return "fake-tenant"
            if key == "xero_roasted_sales_account": return "202"
            if key == "xero_sales_tax_type": return "OUTPUT"
            return default
        mock_setting.side_effect = fake_setting

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "Invoices": [{
                "InvoiceID": "inv-uuid-8899",
                "InvoiceNumber": "INV-00123"
            }]
        }
        mock_post.return_value = mock_resp

        res = post_roasted_dispatch_to_xero(dispatch.id)
        assert res["success"] is True
        assert res["type"] == "invoice"
        assert res["invoice_id"] == "inv-uuid-8899"

        db_session.refresh(dispatch)
        assert dispatch.xero_status == "posted"
        assert dispatch.xero_invoice_id == "inv-uuid-8899"


def test_green_coffee_issue_sale_invoice_with_vat(db_session):
    """فحص بناء وترحيل فاتورة مبيعات مسودة لسند صرف بن أخضر للبيع (سلسلة GB)."""
    gw = Location(name_ar="مستودع الأخضر", name_en="Green Wh", type="warehouse")
    db_session.add(gw)
    db_session.flush()

    it = Item(sku="GB-COL-HUILA", name_ar="كولومبيا هويلا أخضر", storage_unit="كجم", cost=40.0)
    db_session.add(it)
    db_session.flush()
    ci = CoffeeItem(item_id=it.id, xero_item_code="GB COLOMBIA HUILA", prefix="GB", base_code="COL-HUILA", coffee_type="green")
    db_session.add(ci)
    db_session.flush()

    issue = GreenCoffeeIssue(
        doc_number="GB-0055",
        status="approved",
        issue_date=date(2026, 10, 19),
        warehouse_id=gw.id,
        coffee_item_id=ci.id,
        issue_type="sale",
        customer_name="محمصة الرواد",
        qty_kg=60.0,
        unit_cost=40.0,
        total_cost=2400.0,
        sale_price_per_kg=65.0,
        total_sale_amount=3900.0,
        notes="بيع أكياس بن أخضر خام"
    )
    db_session.add(issue)
    db_session.commit()

    # 1. فحص بناء الـ Payload
    payload = build_green_issue_sale_invoice_payload(issue)
    assert payload["Type"] == "ACCREC"
    assert payload["Status"] == "DRAFT"  # فاتورة مبيعات مسودة في زيرو
    assert payload["Contact"]["Name"] == "محمصة الرواد"
    assert payload["Reference"] == "GB-0055"
    assert len(payload["LineItems"]) == 1
    li = payload["LineItems"][0]
    assert li["TaxType"] == "OUTPUT"  # 15% VAT
    assert li["AccountCode"] == "201"  # مبيعات القهوة الخضراء
    assert li["UnitAmount"] == 65.0
    assert li["LineAmount"] == 3900.0

    # 2. فحص ترحيل سند الأخضر
    with patch("coffee_xero._get_setting") as mock_setting, \
         patch("requests.post") as mock_post:
        
        def fake_setting(key, default=""):
            if key == "xero_bearer_token": return "fake-token"
            if key == "xero_tenant_id": return "fake-tenant"
            if key == "xero_green_sales_account": return "201"
            if key == "xero_sales_tax_type": return "OUTPUT"
            return default
        mock_setting.side_effect = fake_setting

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "Invoices": [{
                "InvoiceID": "inv-uuid-green-99",
                "InvoiceNumber": "INV-00456"
            }]
        }
        mock_post.return_value = mock_resp

        res = post_green_issue_to_xero(issue.id)
        assert res["success"] is True
        assert res["invoice_id"] == "inv-uuid-green-99"

        db_session.refresh(issue)
        assert issue.xero_status == "posted"
        assert issue.xero_invoice_id == "inv-uuid-green-99"


def test_roasted_branch_dispatch_mp_numbering_and_xero(db_session):
    """
    التحقق من:
    1. توليد أرقام سندات صرف المحمص للفروع بسلسلة MP تبدأ بعد 3580 (أي MP 3581).
    2. استمرار توليد سندات صرف المبيعات بسلسلة RB بشكل مستقل.
    3. احتواء الـ CreditNoteNumber والـ Reference في إشعار Xero الدائن على رقم MP.
    """
    from models_coffee import RoastedCoffeeDispatch, RoastedCoffeeDispatchLine, CoffeeSetting
    from coffee_xero import build_roasted_dispatch_branch_credit_note_payload

    # ضبط بداية التسلسل في الإعدادات
    CoffeeSetting.set("roasted_branch_issue_seq", "1000")
    CoffeeSetting.set("dispatch_seq_MP", "1000")
    CoffeeSetting.set("roasted_issue_seq", "1000")
    CoffeeSetting.set("dispatch_seq_RB", "1000")

    # 1. توليد رقم لسند فرع (يبدأ بسلسلة MP 1001 أوتوماتيكياً)
    mp_num1 = RoastedCoffeeDispatch.generate_next_doc_number(dispatch_type="branch")
    assert mp_num1 == "MP-1001"
    assert CoffeeSetting.get("dispatch_seq_MP") == "1001"

    mp_num2 = RoastedCoffeeDispatch.generate_next_doc_number(dispatch_type="branch")
    assert mp_num2 == "MP-1002"
    assert CoffeeSetting.get("dispatch_seq_MP") == "1002"

    # 2. توليد رقم لسند بيع (مستقل بسلسلة RB 1001 أوتوماتيكياً)
    rb_num = RoastedCoffeeDispatch.generate_next_doc_number(dispatch_type="sale")
    assert rb_num == "RB-1001"
    assert CoffeeSetting.get("dispatch_seq_RB") == "1001"

    # 3. إنشاء سند صرف لفرع بـ MP 3581 وفحص Payload إشعار دائن Xero
    rw = Location(name_ar="مستودع المحمص التجريبي", name_en="Roasted Coffee Warehouse Test", type="warehouse")
    br = Location(name_ar="فرع التخصصي", name_en="Takhasusi Branch", type="branch")
    db_session.add_all([rw, br])
    db_session.flush()

    it = Item(sku="RB-COL-MP", name_ar="كولومبيا محمص", cost=50.0)
    db_session.add(it)
    db_session.flush()

    ci = CoffeeItem(item_id=it.id, xero_item_code="RB COLOMBIA", prefix="RB", base_code="COLOMBIA", coffee_type="roasted")
    db_session.add(ci)
    db_session.flush()

    dispatch = RoastedCoffeeDispatch(
        doc_number=mp_num1,
        dispatch_date=date.today(),
        source_warehouse_id=rw.id,
        branch_location_id=br.id,
        dispatch_type="branch",
        coffee_item_id=ci.id,
        qty_kg=20.0,
        unit_cost=50.0,
        total_cost=1000.0,
        status="approved"
    )
    db_session.add(dispatch)
    db_session.commit()

    line = RoastedCoffeeDispatchLine(
        dispatch_id=dispatch.id,
        line_number=1,
        coffee_item_id=ci.id,
        qty_kg=20.0,
        unit_cost=50.0,
        total_cost=1000.0
    )
    db_session.add(line)
    db_session.commit()

    payload = build_roasted_dispatch_branch_credit_note_payload(dispatch)
    assert payload["Type"] == "ACCPAYCREDIT"
    assert payload["CreditNoteNumber"] == mp_num1
    assert payload["Reference"] == mp_num1
    assert payload["Contact"]["Name"] == "سند صرف قهوه محموصه فرع التخصصي"
    assert payload["LineItems"][0]["ItemCode"] == "RB COLOMBIA"
    assert payload["LineItems"][0]["LineAmount"] == 1000.0


def test_xero_voucher_attachment_upload(db_session, admin_client):
    """
    التحقق من:
    1. توليد مستند السند الرسمي (PDF أو صورة التوقيع الحي).
    2. إرسال طلب المرفق لـ Xero API إلى الـ Endpoint الصحيح برأس X-Include-On-Online.
    3. استجابة مسار إعادة إرفاق السند عبر الويب بنجاح.
    """
    from models_coffee import RoastedCoffeeDispatch, RoastedCoffeeDispatchLine, CoffeeSetting
    from coffee_xero import _generate_voucher_pdf, attach_voucher_to_xero

    CoffeeSetting.set("xero_bearer_token", "fake-token-attach")
    CoffeeSetting.set("xero_tenant_id", "fake-tenant-attach")

    rw = Location(name_ar="مستودع المحمص", name_en="Roasted Warehouse", type="warehouse")
    br = Location(name_ar="فرع النخيل", name_en="Nakheel Branch", type="branch")
    db_session.add_all([rw, br])
    db_session.flush()

    it = Item(sku="RB-YEM-ATT", name_ar="يمن محمص", cost=60.0)
    db_session.add(it)
    db_session.flush()

    ci = CoffeeItem(item_id=it.id, xero_item_code="RB YEMEN", prefix="RB", base_code="YEMEN", coffee_type="roasted")
    db_session.add(ci)
    db_session.flush()

    # إنشاء سند معتمد مع توقيع إلكتروني Base64
    fake_sig = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
    dispatch = RoastedCoffeeDispatch(
        doc_number="MP 3585",
        dispatch_date=date.today(),
        source_warehouse_id=rw.id,
        branch_location_id=br.id,
        dispatch_type="branch",
        coffee_item_id=ci.id,
        qty_kg=15.0,
        unit_cost=60.0,
        total_cost=900.0,
        customer_signature=fake_sig,
        status="approved"
    )
    db_session.add(dispatch)
    db_session.commit()

    line = RoastedCoffeeDispatchLine(
        dispatch_id=dispatch.id,
        line_number=1,
        coffee_item_id=ci.id,
        qty_kg=15.0,
        unit_cost=60.0,
        total_cost=900.0
    )
    db_session.add(line)
    db_session.commit()

    # 1. فحص توليد الملف
    content_bytes, mime_type = _generate_voucher_pdf("roasted_dispatch", dispatch.id)
    assert content_bytes is not None
    assert len(content_bytes) > 0
    assert mime_type in ("application/pdf", "image/png")

    # 2. فحص إرسال المرفق إلى Xero API
    with patch("requests.post") as mock_post:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"Attachments": [{"FileName": "Voucher_MP_3585.pdf"}]}
        mock_post.return_value = mock_resp

        res = attach_voucher_to_xero("CreditNotes", "fake-cn-uuid-123", "roasted_dispatch", dispatch.id, dispatch.doc_number)
        assert res["success"] is True
        assert "Voucher_MP_3585" in res["filename"]

        # التحقق من بارامترات الاستدعاء
        call_args = mock_post.call_args
        assert "CreditNotes/fake-cn-uuid-123/Attachments/Voucher_MP_3585" in call_args[0][0]
        headers = call_args[1]["headers"]
        assert headers["Authorization"] == "Bearer fake-token-attach"
        assert headers["xero-tenant-id"] == "fake-tenant-attach"
        assert headers["X-Include-On-Online"] == "true"
        assert headers["Content-Type"] in ("application/pdf", "image/png")

    # 3. فحص مسار الويب لإعادة الإرفاق
    dispatch.xero_invoice_id = "fake-cn-uuid-123"
    db_session.commit()

    with patch("coffee_xero.attach_voucher_to_xero") as mock_att:
        mock_att.return_value = {"success": True, "filename": "Voucher_MP_3585.pdf"}
        route_res = admin_client.post(f"/coffee/roasted/dispatches/{dispatch.id}/attach-xero", follow_redirects=True)
        assert route_res.status_code == 200
        assert mock_att.called


def test_roasted_branch_dispatch_print_customizations(admin_client, db_session):
    """التحقق من تعديلات طباعة السند: استبدال إجمالي الكميات المستلمة باللون الأسود وعرض الفرع والمستلم."""
    rw = Location(name_ar="مستودع المحمص للطباعة", name_en="Roasted Wh Print", type="warehouse")
    br = Location(name_ar="فرع التخصصي", name_en="Takhassussi Branch", type="branch")
    db_session.add_all([rw, br])
    db_session.flush()

    item = Item(sku="RB-TEST-PRINT", name_ar="صنف طباعة", storage_unit="كجم", cost=50.0)
    db_session.add(item)
    db_session.flush()

    ci = CoffeeItem(item_id=item.id, xero_item_code="RB-PRINT", prefix="RB", base_code="PRINT", coffee_type="roasted")
    db_session.add(ci)
    db_session.flush()

    dispatch = RoastedCoffeeDispatch(
        doc_number="MP 3590",
        dispatch_date=date.today(),
        source_warehouse_id=rw.id,
        branch_location_id=br.id,
        dispatch_type="branch",
        coffee_item_id=ci.id,
        qty_kg=12.5,
        unit_cost=50.0,
        total_cost=625.0,
        status="approved"
    )
    db_session.add(dispatch)
    db_session.commit()

    line = RoastedCoffeeDispatchLine(
        dispatch_id=dispatch.id,
        line_number=1,
        coffee_item_id=ci.id,
        qty_kg=12.5,
        unit_cost=50.0,
        total_cost=625.0
    )
    db_session.add(line)
    db_session.commit()

    # طلب صفحة طباعة السند
    resp = admin_client.get(f"/coffee/roasted/dispatches/{dispatch.id}/print")
    assert resp.status_code == 200
    html = resp.data.decode("utf-8")

    # 1. فحص ظهور 'إجمالي الكميات المستلمة:' باللون الأسود
    assert "إجمالي الكميات المستلمة:" in html
    assert "color: #000000 !important;" in html

    # 2. فحص بطاقة استلام الفرع / المستلم
    assert "استلام الفرع / المستلم" in html
    assert "فرع التخصصي" in html
    assert "عميل نقدي" not in html


def test_green_coffee_issue_sample_and_internal_credit_notes_closed_at_345(admin_client, db_session):
    """
    التحقق من:
    1. بناء إشعار دائن مشتريات (ACCPAYCREDIT) لسند صرف البن الأخضر لغرض عينات فحص وتذوق باسم 'عينات فحص وتذوق'
    2. بناء إشعار دائن لسند صرف البن الأخضر لغرض استخدام داخلي باسم 'استخدام داخلي تجارب'
    3. تحميل البنود على حساب مخزون الأخضر 635 مع مرجع رقم السند GB
    4. ترحيل الإشعار الدائن وسداده بـ Cash Refund على حساب عينات قهوة (345)
    5. إرفاق نسخة السند الرسمي في زيرو
    """
    gw = Location(name_ar="مستودع الأخضر للاختبار", name_en="Green Wh Test", type="warehouse")
    db_session.add(gw)
    db_session.flush()

    item_gb = Item(sku="GB-ETH-SAMPLE", name_ar="إثيوبي أخضر عينات", storage_unit="كجم", cost=45.0)
    db_session.add(item_gb)
    db_session.flush()

    ci_gb = CoffeeItem(item_id=item_gb.id, xero_item_code="GB-ETH-SAMP", prefix="GB", base_code="ETH-SAMP", coffee_type="green")
    db_session.add(ci_gb)
    db_session.flush()

    # 1. اختبار سند عينات فحص وتذوق (sample)
    issue_sample = GreenCoffeeIssue(
        doc_number="GB 010",
        issue_date=date.today(),
        warehouse_id=gw.id,
        issue_type="sample",
        issue_reason="فحص جودة وتذوق كاسات",
        coffee_item_id=ci_gb.id,
        qty_kg=5.0,
        unit_cost=45.0,
        total_cost=225.0,
        status="approved"
    )
    db_session.add(issue_sample)
    db_session.commit()

    payload_sample = build_green_issue_credit_note_payload(issue_sample)
    assert payload_sample["Type"] == "ACCPAYCREDIT"
    assert payload_sample["Contact"]["Name"] == "عينات فحص وتذوق"
    assert payload_sample["CreditNoteNumber"] == "GB 010"
    assert payload_sample["Reference"] == "GB 010"
    assert len(payload_sample["LineItems"]) == 1
    assert payload_sample["LineItems"][0]["AccountCode"] == "635"
    assert payload_sample["LineItems"][0]["LineAmount"] == 225.0

    # 2. اختبار سند استخدام داخلي تجارب (internal_use)
    issue_internal = GreenCoffeeIssue(
        doc_number="GB 011",
        issue_date=date.today(),
        warehouse_id=gw.id,
        issue_type="internal_use",
        issue_reason="تجارب ملفات تحميص داخلية",
        coffee_item_id=ci_gb.id,
        qty_kg=8.0,
        unit_cost=45.0,
        total_cost=360.0,
        status="approved"
    )
    db_session.add(issue_internal)
    db_session.commit()

    payload_internal = build_green_issue_credit_note_payload(issue_internal)
    assert payload_internal["Type"] == "ACCPAYCREDIT"
    assert payload_internal["Contact"]["Name"] == "استخدام داخلي تجارب"
    assert payload_internal["CreditNoteNumber"] == "GB 011"
    assert payload_internal["Reference"] == "GB 011"
    assert payload_internal["LineItems"][0]["AccountCode"] == "635"
    assert payload_internal["LineItems"][0]["LineAmount"] == 360.0

    # 3. اختبار الترحيل الفعلي لـ Xero مع السداد بحساب 345
    with patch("coffee_xero._get_setting") as mock_setting, \
         patch("requests.post") as mock_post, \
         patch("coffee_xero.attach_voucher_to_xero") as mock_att:

        def fake_setting(key, default=""):
            if key == "xero_bearer_token": return "fake-token"
            if key == "xero_tenant_id": return "fake-tenant"
            if key == "xero_green_account": return "635"
            if key == "xero_samples_account": return "345"
            if key == "xero_green_sample_contact": return "عينات فحص وتذوق"
            return default
        mock_setting.side_effect = fake_setting

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "CreditNotes": [{
                "CreditNoteID": "cn-uuid-sample-123",
                "CreditNoteNumber": "GB 010"
            }]
        }
        mock_post.return_value = mock_resp
        mock_att.return_value = {"success": True, "filename": "Voucher_GB_010.pdf"}

        res = post_green_issue_to_xero(issue_sample.id)
        assert res["success"] is True
        assert res["type"] == "credit_note"
        assert res["invoice_id"] == "cn-uuid-sample-123"

        # التحقق من أن Xero تم استدعاؤه لإنشاء الإشعار الدائن وسداده بحساب 345
        assert mock_post.call_count >= 2  # مرة لـ /CreditNotes ومرة لـ /Payments (Cash Refund)
        payment_call = [c for c in mock_post.call_args_list if "/Payments" in c[0][0]][0]
        payment_json = payment_call[1]["json"]
        assert payment_json["Payments"][0]["Account"]["Code"] == "345"
        assert payment_json["Payments"][0]["Amount"] == 225.0
        assert payment_json["Payments"][0]["CreditNote"]["CreditNoteID"] == "cn-uuid-sample-123"

        db_session.refresh(issue_sample)
        assert issue_sample.xero_status == "posted"
        assert issue_sample.xero_invoice_id == "cn-uuid-sample-123"

    # 4. التحقق من مسار الويب لإعادة الترحيل
    issue_sample.xero_status = "failed"
    db_session.commit()
    with patch("coffee_xero.post_green_issue_to_xero") as mock_post_fn:
        mock_post_fn.return_value = {"success": True, "invoice_number": "GB 010", "type": "credit_note"}
        r_post = admin_client.post(f"/coffee/green/issues/{issue_sample.id}/post-xero", follow_redirects=True)
        assert r_post.status_code == 200
        assert mock_post_fn.called


def test_roasting_order_print(admin_client, db_session):
    """التحقق من إمكانية طباعة أمر التحميص وعرض تفاصيل الأخضر والمحمص ونسب الفاقد والتوقيعات."""
    gw = Location.query.filter_by(name_ar="مستودع البن الأخضر").first()
    if not gw:
        gw = Location(name_ar="مستودع البن الأخضر", name_en="Green Coffee Warehouse", type="warehouse")
        db_session.add(gw)
        db_session.flush()

    rw = Location.query.filter_by(name_ar="مستودع البن المحمص").first()
    if not rw:
        rw = Location(name_ar="مستودع البن المحمص", name_en="Roasted Coffee Warehouse", type="warehouse")
        db_session.add(rw)
        db_session.flush()

    # إنشاء صنف أخضر ومحمص
    it_g = Item(sku="GB-PRNT-TEST", name_ar="بن أثيوبي أخضر طباعة", storage_unit="كجم", cost=40.0)
    db_session.add(it_g)
    db_session.flush()
    ci_g = CoffeeItem(item_id=it_g.id, xero_item_code="GB PRNT", prefix="GB", base_code="PRNT", coffee_type="green", default_sack_weight=60.0)
    db_session.add(ci_g)

    it_r = Item(sku="RB-PRNT-TEST", name_ar="بن أثيوبي محمص طباعة", storage_unit="كجم", cost=50.0)
    db_session.add(it_r)
    db_session.flush()
    ci_r = CoffeeItem(item_id=it_r.id, xero_item_code="RB PRNT", prefix="RB", base_code="PRNT", coffee_type="roasted")
    db_session.add(ci_r)
    db_session.flush()

    order = RoastingOrder(
        order_number="RB-9988",
        reference_number="REF-PRINT-01",
        order_date=date.today(),
        green_warehouse_id=gw.id,
        roasted_warehouse_id=rw.id,
        roasting_supervisor="سليمان الحماص",
        receive_to="مستودع المحمص المركزي",
        status="completed",
        roasting_notes="درجة تحميص متوسطة سيتي بلس",
        quality_notes="درجة التذوق 86 - حمضية فاكهية متوازنة",
    )
    db_session.add(order)
    db_session.flush()

    line = RoastingOrderLine(
        order_id=order.id,
        line_number=1,
        green_item_id=ci_g.id,
        green_qty_kg=100.0,
        green_qty_sacks=1.67,
        green_sack_weight=60.0,
        green_unit_cost=40.0,
        green_total_cost=4000.0,
        roasted_item_id=ci_r.id,
        roasted_qty_kg_actual=82.0,
        reference_loss_pct=20.0,
        line_status="completed"
    )
    line.calc_loss()
    db_session.add(line)
    db_session.commit()

    # 1. التحقق من مسار الطباعة المباشر
    print_url = f"/coffee/roasting/{order.id}/print"
    res = admin_client.get(print_url)
    assert res.status_code == 200
    html = res.get_data(as_text=True)

    assert "أمر تشغيل تحميص" in html
    assert "RB-9988" in html
    assert "محمصة نوفمبر المختصة" in html
    assert "GB PRNT" in html
    assert "RB PRNT" in html
    assert "100.00 كجم" in html
    assert "82.00 كجم" in html
    assert "سليمان الحماص" in html
    assert "درجة تحميص متوسطة" in html
    assert "درجة التذوق 86" in html
    assert "إجمالي كميات أمر التحميص:" in html
    assert "أمين مستودع الأخضر" in html
    assert "الحمّاص / المنفّذ" in html
    assert "أمين مستودع المحمص" in html
    assert "window.print()" in html

    # 2. التحقق من ظهور رابط الطباعة في قائمة أوامر التحميص
    list_res = admin_client.get("/coffee/roasting/")
    assert list_res.status_code == 200
    assert print_url in list_res.get_data(as_text=True)

    # 3. التحقق من ظهور رابط الطباعة في صفحة تفاصيل/تعديل الأمر
    edit_res = admin_client.get(f"/coffee/roasting/{order.id}")
    assert edit_res.status_code == 200
    assert print_url in edit_res.get_data(as_text=True)


def test_roasting_order_draft_line_edit(admin_client, db_session):
    """التحقق من إمكانية تعديل أصناف أمر التحميص في حالة المسودة (الأخضر، المحمص، الكمية والملاحظات)."""
    gw = Location.query.filter_by(name_ar="مستودع البن الأخضر").first()
    if not gw:
        gw = Location(name_ar="مستودع البن الأخضر", name_en="Green Coffee Warehouse", type="warehouse")
        db_session.add(gw)
        db_session.flush()

    rw = Location.query.filter_by(name_ar="مستودع البن المحمص").first()
    if not rw:
        rw = Location(name_ar="مستودع البن المحمص", name_en="Roasted Coffee Warehouse", type="warehouse")
        db_session.add(rw)
        db_session.flush()

    # صنف أخضر أصلي وصنف أخضر بديل
    it_g1 = Item(sku="GB-ORIG-EDIT", name_ar="بن كولومبي أخضر قديم", storage_unit="كجم", cost=35.0)
    it_g2 = Item(sku="GB-NEW-EDIT", name_ar="بن برازيلي أخضر جديد", storage_unit="كجم", cost=45.0)
    db_session.add_all([it_g1, it_g2])
    db_session.flush()

    ci_g1 = CoffeeItem(item_id=it_g1.id, xero_item_code="GB ORIG", prefix="GB", base_code="ORIG", coffee_type="green", default_sack_weight=60.0)
    ci_g2 = CoffeeItem(item_id=it_g2.id, xero_item_code="GB NEW", prefix="GB", base_code="NEW", coffee_type="green", default_sack_weight=60.0)
    db_session.add_all([ci_g1, ci_g2])

    # صنف محمص أصلي وصنف محمص بديل
    it_r1 = Item(sku="RB-ORIG-EDIT", name_ar="بن كولومبي محمص قديم", storage_unit="كجم", cost=55.0)
    it_r2 = Item(sku="RB-NEW-EDIT", name_ar="بن برازيلي محمص جديد", storage_unit="كجم", cost=65.0)
    db_session.add_all([it_r1, it_r2])
    db_session.flush()

    ci_r1 = CoffeeItem(item_id=it_r1.id, xero_item_code="RB ORIG", prefix="RB", base_code="ORIG", coffee_type="roasted")
    ci_r2 = CoffeeItem(item_id=it_r2.id, xero_item_code="RB NEW", prefix="RB", base_code="NEW", coffee_type="roasted")
    db_session.add_all([ci_r1, ci_r2])
    db_session.flush()

    order = RoastingOrder(
        order_number="RB-EDIT-TEST",
        order_date=date.today(),
        green_warehouse_id=gw.id,
        roasted_warehouse_id=rw.id,
        roasting_supervisor="أحمد الحماص",
        status="draft"
    )
    db_session.add(order)
    db_session.flush()

    line = RoastingOrderLine(
        order_id=order.id,
        line_number=1,
        green_item_id=ci_g1.id,
        roasted_item_id=ci_r1.id,
        green_qty_kg=50.0,
        green_qty_sacks=0.83,
        green_sack_weight=60.0,
        green_unit_cost=35.0,
        green_total_cost=1750.0,
        line_status="draft"
    )
    db_session.add(line)
    db_session.commit()

    # 1. التأكد من ظهور زر التعديل والـ modal في صفحة العرض
    page_res = admin_client.get(f"/coffee/roasting/{order.id}")
    assert page_res.status_code == 200
    page_html = page_res.get_data(as_text=True)
    assert "openEditLineModal" in page_html
    assert "editLineModal" in page_html
    assert "✏️ تعديل" in page_html

    # 2. تنفيذ تعديل الصنف والكمية في المسودة عبر POST
    update_data = {
        "green_item_id": str(ci_g2.id),
        "roasted_item_id": str(ci_r2.id),
        "green_qty_kg": "120.0",
        "green_sack_weight": "60.0",
        "notes": "تم تعديل الصنف إلى برازيلي وزيادة الكمية"
    }
    update_res = admin_client.post(f"/coffee/roasting/{order.id}/update-line/{line.id}", data=update_data, follow_redirects=True)
    assert update_res.status_code == 200

    db_session.refresh(line)
    db_session.refresh(order)

    # التحقق من أن السطر تم تحديثه بالصنف الأخضر الجديد والمحمص الجديد والكمية الجديدة والتكلفة
    assert line.green_item_id == ci_g2.id
    assert line.roasted_item_id == ci_r2.id
    assert line.green_qty_kg == 120.0
    assert line.green_unit_cost == 45.0
    assert line.green_total_cost == 5400.0  # 120.0 * 45.0
    assert line.green_qty_sacks == 2.0      # 120 / 60
    assert line.notes == "تم تعديل الصنف إلى برازيلي وزيادة الكمية"

    # التحقق من تحديث إجماليات الأمر
    assert order.total_green_kg == 120.0
    assert order.total_green_cost == 5400.0

    # 3. التأكد من رفض التعديل إذا لم تكن الحالة مسودة
    order.status = "approved_green"
    db_session.commit()

    fail_res = admin_client.post(f"/coffee/roasting/{order.id}/update-line/{line.id}", data={"green_qty_kg": "200.0"}, follow_redirects=True)
    assert fail_res.status_code == 200
    assert "لا يمكن تعديل الأصناف لأمر بعد اعتماده" in fail_res.get_data(as_text=True)


def test_xero_error_parsing_and_existing_credit_note_recovery(db_session):
    """
    فحص:
    1. تحليل رسائل أخطاء Xero واستخراج ValidationErrors بوضوح بدون عرض <Response [400]>.
    2. التعافي التلقائي عند محاولة إعادة ترحيل سند موجود مسبقاً في Xero (مثل MP 3582)
       وربطه مباشرة بالإشعار الدائن المسدد في Xero واعتباره posted بنجاح.
    """
    class FakeResponse:
        def __init__(self, text="", json_dict=None):
            self.text = text
            self._json = json_dict
        def json(self):
            if self._json is not None:
                return self._json
            raise ValueError("No json")

    r_expired = FakeResponse("TokenExpired: token expired at 10/19/2026 14:39:15")
    err_msg1 = _parse_xero_error(r_expired)
    assert "انتهت صلاحية Token في Xero" in err_msg1
    assert "TokenExpired" in err_msg1
    assert "<Response" not in err_msg1

    r_400 = FakeResponse(
        "",
        {"Elements": [{"ValidationErrors": [
            {"Message": "Credit Note not of valid status for modification"},
            {"Message": "This document cannot be edited as it has a payment or credit note allocated to it."}
        ]}]}
    )
    err_msg2 = _parse_xero_error(r_400)
    assert "الإشعار الدائن موجود مسبقاً في Xero ولا يمكن تعديله" in err_msg2
    assert "Credit Note not of valid status for modification" in err_msg2
    assert "<Response" not in err_msg2

    branch = Location(name_ar="فرع الملقا", name_en="Malqa Branch", type="branch")
    rw = Location(name_ar="مستودع المحمص", name_en="Roasted Wh", type="warehouse")
    db_session.add_all([branch, rw])
    db_session.flush()

    it = Item(sku="RB-TEST-400", name_ar="بن محمص اختبار", storage_unit="كجم", cost=60.0)
    db_session.add(it)
    db_session.flush()
    ci = CoffeeItem(item_id=it.id, xero_item_code="RB TEST", prefix="RB", base_code="TEST", coffee_type="roasted")
    db_session.add(ci)
    db_session.flush()

    dispatch = RoastedCoffeeDispatch(
        doc_number="MP 3582",
        dispatch_date=date(2026, 10, 19),
        source_warehouse_id=rw.id,
        branch_location_id=branch.id,
        coffee_item_id=ci.id,
        dispatch_type="branch",
        unit_cost=60.0,
        qty_kg=10.0,
        total_cost=600.0,
        notes="اختبار استرجاع السند الموجود مسبقاً",
        customer_signature="data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
    )
    db_session.add(dispatch)
    db_session.commit()

    with patch("coffee_xero._get_setting") as mock_setting, \
         patch("requests.post") as mock_post, \
         patch("requests.get") as mock_get:

        def fake_setting(key, default=""):
            if key == "xero_bearer_token": return "fake-token"
            if key == "xero_tenant_id": return "fake-tenant"
            if key == "xero_foodics_inventory_account": return "631"
            if key == "xero_roasted_account": return "636"
            return default
        mock_setting.side_effect = fake_setting

        # POST يرجع 400 لأن الإشعار موجود مسبقاً ومسدد
        mock_resp_fail = MagicMock()
        mock_resp_fail.status_code = 400
        mock_resp_fail.json.return_value = {
            "Elements": [{"ValidationErrors": [{"Message": "Credit Note not of valid status for modification"}]}]
        }
        # mock_post used for post CreditNotes and possibly attach_voucher_to_xero
        mock_resp_att = MagicMock()
        mock_resp_att.status_code = 200
        mock_resp_att.json.return_value = {"Attachments": [{"FileName": "Voucher_MP_3582.png"}]}
        mock_post.side_effect = [mock_resp_fail, mock_resp_att]

        # GET يتحقق من وجود الإشعار في Xero ويرجع الإشعار الموجود وحالته PAID
        mock_resp_get = MagicMock()
        mock_resp_get.status_code = 200
        mock_resp_get.json.return_value = {
            "CreditNotes": [{
                "CreditNoteID": "cn-existing-uuid-999",
                "CreditNoteNumber": "MP 3582",
                "Total": 600.0,
                "Status": "PAID",
                "RemainingCredit": 0.0
            }]
        }
        mock_get.return_value = mock_resp_get

        res = post_roasted_dispatch_to_xero(dispatch.id)

        assert res["success"] is True
        assert res["credit_note_id"] == "cn-existing-uuid-999"
        assert res["invoice_number"] == "MP 3582"

        db_session.refresh(dispatch)
        assert dispatch.xero_status == "posted"
        assert dispatch.xero_invoice_id == "cn-existing-uuid-999"
        assert dispatch.xero_error is None


def test_draft_roasting_order_actual_output_and_direct_approval(client, db_session):
    """التحقق من إمكانية تسجيل الناتج الفعلي للتحميص في المسودة واعتماد الأمر مباشرة."""
    gw = Location(name_ar="مستودع البن الأخضر للتجربة", name_en="Green Warehouse Test", type="warehouse")
    rw = Location(name_ar="مستودع البن المحمص للتجربة", name_en="Roasted Warehouse Test", type="warehouse")
    db_session.add_all([gw, rw])
    db_session.flush()

    it_g = Item(sku="GB-TEST-OUT", name_ar="بن أخضر تجريبي", name_en="Green Test Out", storage_unit="كجم", cost=40.0)
    it_r = Item(sku="RB-TEST-OUT", name_ar="بن محمص تجريبي", name_en="Roasted Test Out", storage_unit="كجم", cost=50.0)
    db_session.add_all([it_g, it_r])
    db_session.flush()

    cg = CoffeeItem(item_id=it_g.id, xero_item_code="GB TEST OUT", prefix="GB", base_code="TEST-OUT", coffee_type="green")
    cr = CoffeeItem(item_id=it_r.id, xero_item_code="RB TEST OUT", prefix="RB", base_code="TEST-OUT", coffee_type="roasted")
    db_session.add_all([cg, cr])
    db_session.flush()

    # رصيد أولي في الأخضر 100 كجم
    record_stock_movement(
        item_id=it_g.id,
        location_id=gw.id,
        movement_type="initial",
        quantity_change=100.0,
        notes="رصيد ابتدائي"
    )
    db_session.commit()

    # إنشاء أمر تحميص مسودة بـ 60 كجم أخضر
    order = RoastingOrder(
        order_number="RB-9999",
        order_date=date.today(),
        green_warehouse_id=gw.id,
        roasted_warehouse_id=rw.id,
        status="draft"
    )
    db_session.add(order)
    db_session.flush()

    line = RoastingOrderLine(
        order_id=order.id,
        line_number=1,
        green_item_id=cg.id,
        roasted_item_id=cr.id,
        green_qty_kg=60.0,
        green_sack_weight=60.0,
        green_qty_sacks=1.0,
        green_unit_cost=40.0,
        green_total_cost=2400.0
    )
    db_session.add(line)
    db_session.commit()

    # إنشاء مستخدم مسؤول نشط وتسجيل الدخول
    admin_user = User(name="مدير النظام التجريبي", role="admin", password="password123", active=True)
    admin_user.set_password("pass1234")
    db_session.add(admin_user)
    db_session.commit()

    with client.session_transaction() as sess:
        sess["user_id"] = admin_user.id
        sess["role"] = "admin"

    # 1. اختبار تعديل السطر مع إدخال الناتج الفعلي 49.2 كجم (فاقد 18%)
    resp_update = client.post(f"/coffee/roasting/{order.id}/update-line/{line.id}", data={
        "green_item_id": cg.id,
        "roasted_item_id": cr.id,
        "green_qty_kg": 60.0,
        "green_sack_weight": 60.0,
        "roasted_qty_kg_actual": 49.2,
        "notes": "تم تسجيل الناتج الفعلي في المسودة"
    }, follow_redirects=True)
    assert resp_update.status_code == 200

    db_session.refresh(line)
    db_session.refresh(order)
    assert line.roasted_qty_kg_actual == 49.2
    assert line.loss_kg == pytest.approx(10.8, 0.01)
    assert line.loss_pct == pytest.approx(18.0, 0.1)
    assert line.roasted_unit_cost == pytest.approx(2400.0 / 49.2, 0.01)
    assert order.status == "draft"

    # 2. اختبار حفظ نتائج التحميص عبر المرحلة 2 أثناء المسودة
    resp_enter = client.post(f"/coffee/roasting/{order.id}/enter-result", data={
        "completion_date": str(date.today()),
        "completion_time": "14:30",
        "roasting_notes": "دفعة تجريبية مثالية",
        "line_id[]": [line.id],
        "roasted_qty_actual[]": [49.5],
        "loss_exceed_reason[]": [""]
    }, follow_redirects=True)
    assert resp_enter.status_code == 200

    db_session.refresh(line)
    db_session.refresh(order)
    assert line.roasted_qty_kg_actual == 49.5
    assert line.loss_kg == pytest.approx(10.5, 0.01)
    assert order.status == "draft"

    # 3. اختبار الاعتماد والإكمال المباشر من المسودة
    resp_approve = client.post(f"/coffee/roasting/{order.id}/approve-result", follow_redirects=True)
    assert resp_approve.status_code == 200

    db_session.refresh(order)
    assert order.status == "completed"
    assert order.green_approved_at is not None
    assert order.roasting_approved_at is not None

    # التحقق من حركة المخزون للأخضر (خصم 60 كجم)
    bal_green = StockBalance.query.filter_by(item_id=it_g.id, location_id=gw.id).first()
    assert bal_green.quantity == pytest.approx(40.0, 0.01)

    # التحقق من حركة المخزون للمحمص (إضافة 49.5 كجم)
    bal_roasted = StockBalance.query.filter_by(item_id=it_r.id, location_id=rw.id).first()
    assert bal_roasted.quantity == pytest.approx(49.5, 0.01)


def test_branch_roasted_dispatch_creates_foodics_purchase_and_auto_posts_to_xero(client, db_session):
    """
    التحقق من:
    1. ترحيل سند صرف المحمص لفرع إلى Xero تلقائياً فور الحفظ والاعتماد.
    2. إنشاء سند استلام (مشتريات) للفرع تلقائياً بمورد 'اكياس القهوة المحمصه للفروع' وصنف 'بن محمص عام' (sk-0535).
    3. تسجيل اسم الصنف الفعلي في ملاحظات سند الاستلام للفرع وترحيله لطابور فوديكس.
    4. تسوية الحركات المخزنية وسند الاستلام السابق عند التعديل في dispatch_edit.
    """
    from models import PurchaseTransaction, PurchaseLine, Supplier

    rw = Location.query.filter_by(name_ar="مستودع البن المحمص").first()
    if not rw:
        rw = Location(name_ar="مستودع البن المحمص", name_en="Roasted Coffee Warehouse", type="warehouse")
        db_session.add(rw)
    branch = Location(name_ar="فرع التخصصي للتجربة", name_en="Takhassusi Branch Test", type="branch", foodics_id="branch-takh-uuid")
    db_session.add(branch)
    db_session.flush()

    sup = Supplier(name="اكياس القهوة المحمصه للفروع", foodics_id="sup-roasted-uuid-14")
    db_session.add(sup)
    db_session.flush()

    it_gen = Item(sku="sk-0535", name_ar="بن محمص عام", storage_unit="كيلو", cost=40.0, foodics_id="item-sk0535-uuid")
    it_r = Item(sku="RB-CHELCHELE", name_ar="إثيوبي شلشلي محمص", storage_unit="كجم", cost=65.0)
    db_session.add_all([it_gen, it_r])
    db_session.flush()

    ci_r = CoffeeItem(item_id=it_r.id, xero_item_code="RB CHELCHELE", prefix="RB", base_code="CHELCHELE", coffee_type="roasted")
    db_session.add(ci_r)
    db_session.flush()

    # رصيد في مستودع المحمصة 50 كجم
    record_stock_movement(
        item_id=it_r.id,
        location_id=rw.id,
        movement_type="initial",
        quantity_change=50.0,
        notes="رصيد محمص بالمحمصة"
    )
    db_session.commit()

    admin_user = User(name="مشرف المحمصة التجريبي", role="admin", password="password123", active=True)
    admin_user.set_password("pass1234")
    db_session.add(admin_user)
    db_session.commit()

    with client.session_transaction() as sess:
        sess["user_id"] = admin_user.id
        sess["role"] = "admin"

    # محاكاة اتصال Xero وFoodics
    with patch("coffee_xero.post_roasted_dispatch_to_xero") as mock_xero, \
         patch("sync_queue.process_job") as mock_foodics:
        
        mock_xero.return_value = {"success": True, "invoice_number": "MP-TEST", "type": "credit_note"}
        mock_foodics.return_value = (True, "تم التصدير بنجاح")

        # 1. إنشاء سند صرف معتمد لفرع
        res = client.post("/coffee/roasted/dispatches/new", data={
            "form_action": "approve",
            "dispatch_date": str(date.today()),
            "dispatch_time": "10:00",
            "dispatch_type": "branch",
            "branch_location_id": branch.id,
            "notes": "إرسالية صباحية للفرع",
            "coffee_item_id[]": [ci_r.id],
            "qty_kg[]": [12.0],
            "line_notes[]": ["دفعة تحميص رقم 10"]
        }, follow_redirects=True)

        assert res.status_code == 200
        # التأكد من استدعاء ترحيل Xero التلقائي
        assert mock_xero.called is True

        # التأكد من إنشاء سند الصرف
        dispatch = RoastedCoffeeDispatch.query.filter_by(dispatch_type="branch").order_by(RoastedCoffeeDispatch.id.desc()).first()
        assert dispatch is not None
        assert dispatch.total_qty_kg == 12.0
        assert dispatch.status == "approved"

        # 2. التأكد من إنشاء سند استلام المشتريات للفرع
        purchase = PurchaseTransaction.query.filter_by(foodics_reference=dispatch.doc_number).first()
        assert purchase is not None
        assert purchase.location_id == branch.id
        assert purchase.supplier_name == "اكياس القهوة المحمصه للفروع"
        assert purchase.status == "approved"
        assert purchase.invoice_number == dispatch.doc_number

        # بنود سند الاستلام
        assert len(purchase.lines) == 1
        pline = purchase.lines[0]
        assert pline.item.sku == "sk-0535"
        assert pline.quantity == 12.0
        assert pline.unit_cost == 65.0
        assert pline.notes == "إثيوبي شلشلي محمص"

        # حركة مخزون الفرع
        bal_branch = StockBalance.query.filter_by(item_id=it_gen.id, location_id=branch.id).first()
        assert bal_branch is not None
        assert bal_branch.quantity == 12.0

        # خصم المحمصة
        bal_rw = StockBalance.query.filter_by(item_id=it_r.id, location_id=rw.id).first()
        assert bal_rw.quantity == 38.0

        # التأكد من جدولة ترحيل فوديكس
        assert mock_foodics.called is True

        # 3. اختبار التعديل عبر dispatch_edit وتحديث سند الاستلام
        res_edit = client.post(f"/coffee/roasted/dispatches/{dispatch.id}/edit", data={
            "form_action": "approve",
            "dispatch_date": str(date.today()),
            "dispatch_time": "11:00",
            "dispatch_type": "branch",
            "branch_location_id": branch.id,
            "notes": "تعديل الإرسالية للفرع",
            "coffee_item_id[]": [ci_r.id],
            "qty_kg[]": [15.0],
            "line_notes[]": ["تعديل الكمية إلى 15"]
        }, follow_redirects=True)

        assert res_edit.status_code == 200

        # التحقق من تحديث سند الاستلام ومخزون الفرع لـ 15.0 كجم
        db_session.refresh(bal_branch)
        assert bal_branch.quantity == 15.0

        # المحمصة أصبحت 35 كجم
        db_session.refresh(bal_rw)
        assert bal_rw.quantity == 35.0

        updated_purchase = PurchaseTransaction.query.filter_by(foodics_reference=dispatch.doc_number).first()
        assert updated_purchase is not None
        assert updated_purchase.lines[0].quantity == 15.0


def test_roasting_order_xero_payload_reference_and_attachment_upload(client, db_session):
    """
    التحقق من:
    1. أن حقل Reference و InvoiceNumber في فاتورة ناتج المحمص (Bill) يطابق رقم أمر التحميص كاملاً.
    2. أن حقل Reference و CreditNoteNumber في إشعار الأخضر يطابق رقم أمر التحميص كاملاً.
    3. إرفاق مستند أمر التحميص الرسمي (PDF) تلقائياً بفاتورة ناتج التحميص في زيرو عند اكتمال الترحيل.
    4. عمل مسار إعادة الإرفاق roasting_attach_xero.
    """
    from coffee_xero import (
        build_roasted_bill_payload,
        build_green_credit_note_payload,
        post_roasting_order_to_xero,
        attach_voucher_to_xero
    )

    gw = Location(name_ar="مستودع الأخضر للاختبار المرفقات", name_en="Green WH Test Att", type="warehouse")
    rw = Location(name_ar="مستودع المحمص للاختبار المرفقات", name_en="Roasted WH Test Att", type="warehouse")
    db_session.add_all([gw, rw])
    db_session.flush()

    it_g = Item(sku="GB-COL-ATT", name_ar="كولومبيا أخضر", storage_unit="كجم", cost=45.0)
    it_r = Item(sku="RB-COL-ATT", name_ar="كولومبيا محمص", storage_unit="كجم", cost=60.0)
    db_session.add_all([it_g, it_r])
    db_session.flush()

    cg = CoffeeItem(item_id=it_g.id, xero_item_code="GB COL ATT", prefix="GB", base_code="COL-ATT", coffee_type="green")
    cr = CoffeeItem(item_id=it_r.id, xero_item_code="RB COL ATT", prefix="RB", base_code="COL-ATT", coffee_type="roasted")
    db_session.add_all([cg, cr])
    db_session.flush()

    order = RoastingOrder(
        order_number="RB-3585",
        order_date=date.today(),
        green_warehouse_id=gw.id,
        roasted_warehouse_id=rw.id,
        status="completed"
    )
    db_session.add(order)
    db_session.flush()

    line = RoastingOrderLine(
        order_id=order.id,
        line_number=1,
        green_item_id=cg.id,
        roasted_item_id=cr.id,
        green_qty_kg=60.0,
        green_sack_weight=60.0,
        green_qty_sacks=1.0,
        green_unit_cost=45.0,
        green_total_cost=2700.0,
        roasted_qty_kg_actual=50.0,
        roasted_unit_cost=54.0,
        line_status="completed"
    )
    db_session.add(line)
    db_session.commit()

    # 1. فحص Reference و InvoiceNumber في فاتورة المحمص
    bill_payload = build_roasted_bill_payload(order)
    assert bill_payload["Reference"] == "RB-3585"
    assert bill_payload["InvoiceNumber"] == "RB-3585"

    # 2. فحص Reference و CreditNoteNumber في إشعار الأخضر
    cn_payload = build_green_credit_note_payload(order)
    assert cn_payload["Reference"] == "RB-3585"
    assert cn_payload["CreditNoteNumber"] == "RB-3585"

    # 3. فحص الترحيل التلقائي مع إرفاق المستند
    with patch("coffee_xero.post_bill") as mock_bill, \
         patch("coffee_xero.post_credit_note") as mock_cn, \
         patch("coffee_xero.attach_voucher_to_xero") as mock_att:
        
        mock_bill.return_value = (True, "bill-uuid-3585", None)
        mock_cn.return_value = (True, "cn-uuid-3585", None)
        mock_att.return_value = {"success": True, "filename": "Voucher_RB_3585.pdf"}

        res = post_roasting_order_to_xero(order)
        assert res["xero_status"] == "posted"

        # التحقق من استدعاء الإرفاق لفاتورة الناتج Bill
        assert mock_att.called is True
        mock_att.assert_any_call("Invoices", "bill-uuid-3585", "roasting_order", order.id, "RB-3585")

    # 4. فحص مسار إعادة الإرفاق roasting_attach_xero
    admin_user = User(name="مشرف التحميص التجريبي المرفقات", role="admin", password="password123", active=True)
    admin_user.set_password("pass1234")
    db_session.add(admin_user)
    db_session.commit()

    with client.session_transaction() as sess:
        sess["user_id"] = admin_user.id
        sess["role"] = "admin"

    with patch("coffee_xero.attach_voucher_to_xero") as mock_att:
        mock_att.return_value = {"success": True, "filename": "Voucher_RB_3585.pdf"}
        resp = client.post(f"/coffee/roasting/{order.id}/attach-xero", follow_redirects=True)
        assert resp.status_code == 200
        assert mock_att.called is True


def test_roasted_coffee_sample_and_internal_credit_notes_closed_at_345_with_attachment(client, db_session):
    """
    اختبار سند صرف البن المحمص لغرض عينات فحص وتذوق (sample) واستخدام داخلي تجارب (internal_use):
    1. بناء إشعار دائن مشتريات (ACCPAYCREDIT) باسم الجهة 'عينات فحص وتذوق' أو 'استخدام داخلي تجارب'.
    2. التأكد من أن رقم السند يظهر في CreditNoteNumber و Reference للاثنين.
    3. تحميل البنود على حساب مخزون البن المحمص (كود 636) وبدون ضريبة (NONE).
    4. سداد وإقفال الإشعار الدائن تلقائياً بـ Cash Refund على حساب عينات قهوة (كود 345).
    5. إرفاق نسخة السند الرسمي (PDF المعتمد) تلقائياً في زيرو عبر attach_voucher_to_xero.
    6. الترحيل التلقائي عند الحفظ والاعتماد في dispatch_new.
    7. عمل مسار إعادة الإرفاق roasted_dispatch_attach_xero على نقطة CreditNotes.
    """
    from datetime import date
    from unittest.mock import patch, MagicMock
    from models import Location, Item, User
    from models_coffee import CoffeeItem, RoastedCoffeeDispatch, RoastedCoffeeDispatchLine, CoffeeSetting
    from coffee_xero import (
        build_roasted_issue_credit_note_payload,
        post_roasted_dispatch_to_xero,
    )

    from app_coffee import _get_roasted_warehouse
    rw = _get_roasted_warehouse()

    item_r = Item(sku="SKU-RB-SMPL-1", name_ar="محمص تجارب إثيوبي", cost=55.0, category_reference="rb")
    db_session.add(item_r)
    db_session.flush()

    ci_r = CoffeeItem(
        prefix="RB",
        base_code="ETH-SMPL",
        coffee_type="roasted",
        name_ar="بن إثيوبي محمص عينات",
        item_id=item_r.id,
        xero_item_code="RB-ETH-SMPL",
        active=True
    )
    db_session.add(ci_r)
    db_session.flush()

    # 1. اختبار سند عينات فحص وتذوق (sample)
    disp_sample = RoastedCoffeeDispatch(
        doc_number="RB 0088",
        dispatch_date=date(2026, 10, 20),
        source_warehouse_id=rw.id,
        dispatch_type="sample",
        issue_reason="فحص جودة وتذوق ربع سنوي",
        qty_kg=2.5,
        unit_cost=55.0,
        total_cost=137.5,
        coffee_item_id=ci_r.id,
        status="approved"
    )
    db_session.add(disp_sample)
    db_session.flush()

    line_s = RoastedCoffeeDispatchLine(
        dispatch_id=disp_sample.id,
        line_number=1,
        coffee_item_id=ci_r.id,
        qty_kg=2.5,
        unit_cost=55.0,
        total_cost=137.5,
        notes="عينة تحميص"
    )
    db_session.add(line_s)
    db_session.commit()

    # فحص الـ Payload لسند العينات
    payload_sample = build_roasted_issue_credit_note_payload(disp_sample)
    assert payload_sample["Type"] == "ACCPAYCREDIT"
    assert payload_sample["Contact"]["Name"] == "عينات فحص وتذوق"
    assert payload_sample["CreditNoteNumber"] == "RB 0088"
    assert payload_sample["Reference"] == "RB 0088"
    assert payload_sample["Status"] == "AUTHORISED"
    assert len(payload_sample["LineItems"]) == 1
    assert payload_sample["LineItems"][0]["AccountCode"] == "636"
    assert payload_sample["LineItems"][0]["TaxType"] == "NONE"
    assert payload_sample["LineItems"][0]["Quantity"] == 2.5
    assert payload_sample["LineItems"][0]["LineAmount"] == 137.5

    # 2. اختبار سند استخدام داخلي وتجارب (internal_use)
    disp_internal = RoastedCoffeeDispatch(
        doc_number="MP 3599",
        dispatch_date=date(2026, 10, 20),
        source_warehouse_id=rw.id,
        dispatch_type="internal_use",
        issue_reason="تجارب استخلاص وضيافة",
        qty_kg=4.0,
        unit_cost=55.0,
        total_cost=220.0,
        coffee_item_id=ci_r.id,
        status="approved"
    )
    db_session.add(disp_internal)
    db_session.flush()

    line_i = RoastedCoffeeDispatchLine(
        dispatch_id=disp_internal.id,
        line_number=1,
        coffee_item_id=ci_r.id,
        qty_kg=4.0,
        unit_cost=55.0,
        total_cost=220.0,
        notes="استخدام داخلي"
    )
    db_session.add(line_i)
    db_session.commit()

    # فحص الـ Payload لسند الاستخدام الداخلي
    payload_internal = build_roasted_issue_credit_note_payload(disp_internal)
    assert payload_internal["Type"] == "ACCPAYCREDIT"
    assert payload_internal["Contact"]["Name"] == "استخدام داخلي تجارب"
    assert payload_internal["CreditNoteNumber"] == "MP 3599"
    assert payload_internal["Reference"] == "MP 3599"
    assert payload_internal["LineItems"][0]["AccountCode"] == "636"
    assert payload_internal["LineItems"][0]["Quantity"] == 4.0
    assert payload_internal["LineItems"][0]["LineAmount"] == 220.0

    # 3. اختبار الترحيل الفعلي لـ Xero مع السداد بـ 345 وإرفاق السند
    CoffeeSetting.set("xero_bearer_token", "test-bearer-token-123")
    CoffeeSetting.set("xero_samples_account", "345")

    fake_cn_resp = MagicMock()
    fake_cn_resp.status_code = 200
    fake_cn_resp.json.return_value = {
        "CreditNotes": [{
            "CreditNoteID": "cn-sample-roasted-9999",
            "CreditNoteNumber": "RB 0088",
            "Status": "AUTHORISED",
            "Total": 137.5
        }]
    }

    with patch("requests.post") as mock_post, \
         patch("coffee_xero._apply_cash_refund") as mock_refund, \
         patch("coffee_xero.attach_voucher_to_xero") as mock_att:

        mock_post.return_value = fake_cn_resp
        mock_att.return_value = {"success": True, "filename": "Voucher_RB_0088.pdf"}

        res = post_roasted_dispatch_to_xero(disp_sample.id)
        assert res["success"] is True
        assert res["type"] == "credit_note_sample"
        assert res["invoice_id"] == "cn-sample-roasted-9999"

        # التحقق من سداد الإشعار بـ Cash Refund على حساب 345
        assert mock_refund.called is True
        mock_refund.assert_called_once_with(
            "cn-sample-roasted-9999",
            137.5,
            disp_sample.dispatch_date,
            "345"
        )

        # التحقق من رفع نسخة السند الرسمي (PDF المعتمد) إلى CreditNotes في زيرو
        assert mock_att.called is True
        mock_att.assert_called_once_with(
            "CreditNotes",
            "cn-sample-roasted-9999",
            "roasted_dispatch",
            disp_sample.id,
            "RB 0088"
        )

        db_session.refresh(disp_sample)
        assert disp_sample.xero_status == "posted"
        assert disp_sample.xero_invoice_id == "cn-sample-roasted-9999"
        assert disp_sample.xero_invoice_number == "RB 0088"

    # 4. اختبار مسار إعادة الإرفاق roasted_dispatch_attach_xero
    admin_u = User(name="مشرف مرفقات المحمص", role="admin", password="password123", active=True)
    admin_u.set_password("pass1234")
    db_session.add(admin_u)
    db_session.commit()

    with client.session_transaction() as sess:
        sess["user_id"] = admin_u.id
        sess["role"] = "admin"

    with patch("coffee_xero.attach_voucher_to_xero") as mock_att2:
        mock_att2.return_value = {"success": True, "filename": "Voucher_RB_0088.pdf"}
        r_att = client.post(f"/coffee/roasted/dispatches/{disp_sample.id}/attach-xero", follow_redirects=True)
        assert r_att.status_code == 200
        mock_att2.assert_called_once_with("CreditNotes", "cn-sample-roasted-9999", "roasted_dispatch", disp_sample.id, "RB 0088")


def test_xero_number_collision_detection_and_prevention(client, db_session):
    """
    اختبار دقة اكتشاف ومنع تعارض أرقام السندات في Xero:
    1. عند محاولة ترحيل سند صرف برقم موجود مسبقاً في زيرو ولكن بمبلغ أو مستند مختلف:
       - يجب ألا يتم اعتباره مرحلاً بنجاح (posted).
       - يجب أن تصبح حالته failed مع رسالة توضح وجود تعارض في رقم السند.
    2. عند تعديل رقم السند في شاشة التعديل إلى رقم جديد غير مكرر:
       - يتم فك الارتباط الخاطئ وتصبح حالته pending لإعادة الترحيل.
    """
    from models_coffee import RoastedCoffeeDispatch, CoffeeItem
    from models import Location, Item, User
    from coffee_xero import post_roasted_dispatch_to_xero, _find_existing_xero_document
    from unittest.mock import patch, MagicMock

    # 1. فحص مباشر لدالة _find_existing_xero_document عند اختلاف المبلغ
    with patch("requests.get") as mock_get, \
         patch("coffee_xero._get_setting", return_value="fake-token"):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "CreditNotes": [{
                "CreditNoteID": "cn-old-different-123",
                "CreditNoteNumber": "MP 3584",
                "Total": 665.75
            }]
        }
        mock_get.return_value = mock_resp

        # إذا كان المبلغ المتوقع 3003.0 والموجود 665.75، يجب أن ترجع None (تعارض أرقام)
        found = _find_existing_xero_document("CreditNotes", "MP 3584", expected_total=3003.0)
        assert found is None

        # إذا تطابق المبلغ، ترجع المستند
        found_match = _find_existing_xero_document("CreditNotes", "MP 3584", expected_total=665.75)
        assert found_match is not None
        assert found_match["CreditNoteID"] == "cn-old-different-123"

    # 2. فحص دورة الترحيل الكاملة لسند يحدث معه تعارض أرقام
    br = Location(name_ar="فرع حطين التجريبي", name_en="Hitten Branch Test", type="branch")
    rw = Location(name_ar="مستودع المحمص", name_en="Roasted Warehouse Test", type="warehouse")
    db_session.add_all([br, rw])
    db_session.flush()

    it = Item(sku="RB-TEST-COL", name_ar="كولومبي محمص تجريبي", cost=300.3)
    db_session.add(it)
    db_session.flush()
    ci = CoffeeItem(item_id=it.id, xero_item_code="RB0465", prefix="RB", base_code="0465", coffee_type="roasted")
    db_session.add(ci)
    db_session.flush()

    disp = RoastedCoffeeDispatch(
        doc_number="MP 3584",
        dispatch_date=date(2026, 10, 20),
        source_warehouse_id=rw.id,
        branch_location_id=br.id,
        coffee_item_id=ci.id,
        dispatch_type="branch",
        unit_cost=300.3,
        qty_kg=10.0,
        total_cost=3003.0,
        status="approved"
    )
    db_session.add(disp)
    db_session.commit()

    with patch("coffee_xero._get_setting") as mock_set, \
         patch("requests.post") as mock_post, \
         patch("requests.get") as mock_get:

        mock_set.side_effect = lambda k, d="": "fake-token" if k == "xero_bearer_token" else ("fake-tenant" if k == "xero_tenant_id" else d)

        # Xero POST يرجع 400 بسبب تكرار الرقم مع دفعة قديمة
        mock_post_res = MagicMock()
        mock_post_res.status_code = 400
        mock_post_res.text = "This document cannot be edited as it has a payment"
        mock_post_res.json.return_value = {
            "Elements": [{"ValidationErrors": [{"Message": "This document cannot be edited as it has a payment"}]}]
        }
        mock_post.return_value = mock_post_res

        # GET يرجع المستند القديم برصيد 665.75
        mock_get_res = MagicMock()
        mock_get_res.status_code = 200
        mock_get_res.json.return_value = {
            "CreditNotes": [{
                "CreditNoteID": "cn-old-665",
                "CreditNoteNumber": "MP 3584",
                "Total": 665.75
            }]
        }
        mock_get.return_value = mock_get_res

        res = post_roasted_dispatch_to_xero(disp.id)
        assert res["success"] is False
        db_session.refresh(disp)
        assert disp.xero_status == "failed"
        assert disp.xero_invoice_id is None
        assert "مستخدم مسبقاً" in disp.xero_error

    # 3. تعديل رقم السند عبر شاشة التعديل إلى MP 3593 لفك التعارض
    u_admin = User(name="مدير تعديل أرقام السندات", role="admin", password="password123", active=True)
    u_admin.set_password("pass1234")
    db_session.add(u_admin)
    db_session.commit()

    with client.session_transaction() as sess:
        sess["user_id"] = u_admin.id
        sess["role"] = "admin"

    form_data = {
        "doc_number": "MP 3593",
        "dispatch_date": "2026-10-20",
        "dispatch_time": "11:00",
        "dispatch_type": "branch",
        "branch_location_id": str(br.id),
        "coffee_item_id[]": [str(ci.id)],
        "qty_kg[]": ["10.0"],
        "line_notes[]": [""],
        "form_action": "draft"
    }
    r_edit = client.post(f"/coffee/roasted/dispatches/{disp.id}/edit", data=form_data, follow_redirects=True)
    assert r_edit.status_code == 200

    db_session.refresh(disp)
    assert disp.doc_number == "MP 3593"
    assert disp.xero_status == "pending"
    assert disp.xero_invoice_id is None

    # 4. إعادة الترحيل برقم السند الجديد MP 3593 ونجاح العملية في Xero
    with patch("coffee_xero._get_setting") as mock_set, \
         patch("requests.post") as mock_post, \
         patch("coffee_xero.attach_voucher_to_xero") as mock_att:

        mock_set.side_effect = lambda k, d="": "fake-token" if k == "xero_bearer_token" else ("fake-tenant" if k == "xero_tenant_id" else d)

        mock_post_ok = MagicMock()
        mock_post_ok.status_code = 201
        mock_post_ok.json.return_value = {
            "CreditNotes": [{
                "CreditNoteID": "cn-clean-3593",
                "CreditNoteNumber": "MP 3593"
            }]
        }
        mock_post.return_value = mock_post_ok
        mock_att.return_value = {"success": True, "filename": "Voucher_MP_3593.pdf"}

        disp.status = "approved"
        db_session.commit()

        res_ok = post_roasted_dispatch_to_xero(disp.id)
        assert res_ok["success"] is True
        assert res_ok["credit_note_id"] == "cn-clean-3593"

        db_session.refresh(disp)
        assert disp.xero_status == "posted"
        assert disp.xero_invoice_id == "cn-clean-3593"
        assert disp.xero_invoice_number == "MP 3593"
        assert disp.xero_error is None


def test_xero_tracking_categories_roasted_and_green(db_session):
    """
    التحقق من أن توجيه السندات في زيرو يتطابق بدقة مع خيارات الفروع المعتمدة في Xero:
    - البن المحمص (سندات الفروع، البيع، العينات والتجارب) يتوجه إلى فرع 'Roasted Coffee'.
    - البن الأخضر (سندات البيع، العينات والتجارب) يتوجه إلى فرع 'Green Coffee'.
    """
    from models_coffee import (
        CoffeeItem, RoastedCoffeeDispatch, RoastedCoffeeDispatchLine,
        GreenCoffeeIssue, GreenCoffeeIssueLine, CoffeeSetting
    )
    from coffee_xero import (
        build_roasted_dispatch_branch_credit_note_payload,
        build_roasted_issue_credit_note_payload,
        build_roasted_issue_sale_invoice_payload,
        build_green_issue_credit_note_payload,
        build_green_issue_sale_invoice_payload
    )

    CoffeeSetting.set("xero_tracking_category", "Branches")
    CoffeeSetting.set("xero_roasted_tracking_val", "Roasted Coffee")
    CoffeeSetting.set("xero_green_tracking_val", "Green Coffee")

    # صنف محمص وصنف أخضر ومستودع
    wh = Location(name_ar="المستودع الرئيسي", name_en="Main Wh", type="warehouse")
    it_r = Item(sku="RB22025", name_ar="مكسيكي باتش مجففه محمص", storage_unit="كجم", cost=58.5)
    it_g = Item(sku="GB22025", name_ar="مكسيكي باتش مجففه أخضر", storage_unit="كجم", cost=40.0)
    db_session.add_all([wh, it_r, it_g])
    db_session.flush()

    roasted_item = CoffeeItem(item_id=it_r.id, name_ar="مكسيكي باتش مجففه محمص", coffee_type="roasted", prefix="RB", base_code="22025", xero_item_code="RB22025")
    green_item = CoffeeItem(item_id=it_g.id, name_ar="مكسيكي باتش مجففه أخضر", coffee_type="green", prefix="GB", base_code="22025", xero_item_code="GB22025")
    db_session.add_all([roasted_item, green_item])
    db_session.commit()

    # 1. سند صرف فرع بن محمص (مثل MP 3595)
    disp_branch = RoastedCoffeeDispatch(
        doc_number="MP 3595",
        dispatch_type="branch",
        dispatch_date=date(2026, 10, 20),
        source_warehouse_id=wh.id,
        status="approved"
    )
    db_session.add(disp_branch)
    db_session.commit()

    line_b = RoastedCoffeeDispatchLine(
        dispatch_id=disp_branch.id,
        coffee_item_id=roasted_item.id,
        qty_kg=6.0,
        unit_cost=58.5,
        total_cost=351.0
    )
    db_session.add(line_b)
    db_session.commit()

    payload_b = build_roasted_dispatch_branch_credit_note_payload(disp_branch)
    assert payload_b["LineItems"][0]["Tracking"] == [{"Name": "Branches", "Option": "Roasted Coffee"}]

    # 2. سند صرف بن محمص عينات وتجارب
    disp_sample = RoastedCoffeeDispatch(
        doc_number="MP 3596",
        dispatch_type="sample",
        dispatch_date=date(2026, 10, 20),
        source_warehouse_id=wh.id,
        status="approved"
    )
    db_session.add(disp_sample)
    db_session.commit()

    line_s = RoastedCoffeeDispatchLine(
        dispatch_id=disp_sample.id,
        coffee_item_id=roasted_item.id,
        qty_kg=1.0,
        unit_cost=58.5,
        total_cost=58.5
    )
    db_session.add(line_s)
    db_session.commit()

    payload_s = build_roasted_issue_credit_note_payload(disp_sample)
    assert payload_s["LineItems"][0]["Tracking"] == [{"Name": "Branches", "Option": "Roasted Coffee"}]

    # 3. سند صرف بن محمص بيع
    disp_sale = RoastedCoffeeDispatch(
        doc_number="RB 0010",
        dispatch_type="sale",
        dispatch_date=date(2026, 10, 20),
        source_warehouse_id=wh.id,
        customer_name="عميل محمص",
        status="approved"
    )
    db_session.add(disp_sale)
    db_session.commit()

    line_sale = RoastedCoffeeDispatchLine(
        dispatch_id=disp_sale.id,
        coffee_item_id=roasted_item.id,
        qty_kg=5.0,
        sale_price_per_kg=100.0,
        total_cost=250.0
    )
    db_session.add(line_sale)
    db_session.commit()

    payload_rsale = build_roasted_issue_sale_invoice_payload(disp_sale)
    assert payload_rsale["LineItems"][0]["Tracking"] == [{"Name": "Branches", "Option": "Roasted Coffee"}]

    # 4. سند صرف بن أخضر عينات وتجارب
    green_sample = GreenCoffeeIssue(
        doc_number="GB 0011",
        issue_type="sample",
        issue_date=date(2026, 10, 20),
        warehouse_id=wh.id,
        status="approved"
    )
    db_session.add(green_sample)
    db_session.commit()

    line_gs = GreenCoffeeIssueLine(
        issue_id=green_sample.id,
        coffee_item_id=green_item.id,
        qty_kg=2.0,
        unit_cost=40.0,
        total_cost=80.0
    )
    db_session.add(line_gs)
    db_session.commit()

    payload_gs = build_green_issue_credit_note_payload(green_sample)
    assert payload_gs["LineItems"][0]["Tracking"] == [{"Name": "Branches", "Option": "Green Coffee"}]

    # 5. سند صرف بن أخضر بيع
    green_sale = GreenCoffeeIssue(
        doc_number="GB 0012",
        issue_type="sale",
        issue_date=date(2026, 10, 20),
        warehouse_id=wh.id,
        customer_name="عميل أخضر",
        status="approved"
    )
    db_session.add(green_sale)
    db_session.commit()

    line_gsale = GreenCoffeeIssueLine(
        issue_id=green_sale.id,
        coffee_item_id=green_item.id,
        qty_kg=10.0,
        sale_price_per_kg=60.0,
        total_cost=400.0
    )
    db_session.add(line_gsale)
    db_session.commit()

    payload_gsale = build_green_issue_sale_invoice_payload(green_sale)
    assert payload_gsale["LineItems"][0]["Tracking"] == [{"Name": "Branches", "Option": "Green Coffee"}]


def test_xero_oauth_connect_without_credentials(admin_client, db_session):
    """فحص محاولة بدء ربط Xero بدون إدخال Client ID يعيد التوجيه إلى الإعدادات مع تنبيه."""
    CoffeeSetting.set("xero_client_id", "")
    CoffeeSetting.set("xero_client_secret", "")
    db_session.commit()

    resp = admin_client.get("/coffee/xero/connect", follow_redirects=True)
    assert resp.status_code == 200
    html = resp.data.decode("utf-8")
    assert "يرجى إدخال وحفظ Client ID" in html


def test_xero_oauth_connect_redirects_to_xero(admin_client, db_session):
    """فحص توليد رابط OAuth والتحويل إلى صفحة تسجيل دخول Xero مع حفظ الـ state والنطاقات المطلوبة."""
    CoffeeSetting.set("xero_client_id", "TEST_CLIENT_ID")
    CoffeeSetting.set("xero_client_secret", "TEST_CLIENT_SECRET")
    db_session.commit()

    resp = admin_client.get("/coffee/xero/connect", follow_redirects=False)
    assert resp.status_code == 302
    location = resp.headers.get("Location")
    assert location.startswith("https://login.xero.com/identity/connect/authorize")
    assert "client_id=TEST_CLIENT_ID" in location
    assert "response_type=code" in location
    assert "offline_access" in location
    assert "accounting.invoices" in location


def test_xero_oauth_callback_state_mismatch(admin_client):
    """فحص رفض Callback في حال عدم تطابق قيمة State."""
    with admin_client.session_transaction() as sess:
        sess["xero_oauth_state"] = "valid_state_123"

    resp = admin_client.get("/coffee/xero/callback?code=abc&state=wrong_state", follow_redirects=True)
    assert resp.status_code == 200
    html = resp.data.decode("utf-8")
    assert "فشل التحقق من رمز أمان الجلسة" in html


def test_xero_oauth_callback_successful_exchange(admin_client, db_session):
    """فحص اكتمال تبادل الكود بالـ Access Token والـ Refresh Token وحفظ بيانات المستأجر."""
    CoffeeSetting.set("xero_client_id", "TEST_CLIENT_ID")
    CoffeeSetting.set("xero_client_secret", "TEST_CLIENT_SECRET")
    db_session.commit()

    with admin_client.session_transaction() as sess:
        sess["xero_oauth_state"] = "matching_state_456"

    mock_token_resp = MagicMock()
    mock_token_resp.status_code = 200
    mock_token_resp.json.return_value = {
        "access_token": "acc_tok_999",
        "refresh_token": "ref_tok_888",
        "expires_in": 1800
    }

    mock_conn_resp = MagicMock()
    mock_conn_resp.status_code = 200
    mock_conn_resp.json.return_value = [
        {"tenantId": "t-uuid-111", "tenantName": "Roastery Main Org"}
    ]

    with patch("requests.post", return_value=mock_token_resp) as mock_post, \
         patch("requests.get", return_value=mock_conn_resp) as mock_get:
        resp = admin_client.get("/coffee/xero/callback?code=auth_code_xyz&state=matching_state_456", follow_redirects=True)
        assert resp.status_code == 200
        html = resp.data.decode("utf-8")
        assert "تم ربط حساب Xero بنجاح" in html

        assert CoffeeSetting.get("xero_bearer_token") == "acc_tok_999"
        assert CoffeeSetting.get("xero_refresh_token") == "ref_tok_888"
        assert CoffeeSetting.get("xero_tenant_id") == "t-uuid-111"
        assert CoffeeSetting.get("xero_tenant_name") == "Roastery Main Org"
        assert CoffeeSetting.get("xero_token_expires_at") != ""


def test_refresh_xero_token_helper(db_session):
    """فحص دالة refresh_xero_token وتحديث كل من access_token و refresh_token المدور."""
    from coffee_xero import refresh_xero_token

    # 1. حالة عدم وجود مفاتيح
    CoffeeSetting.set("xero_client_id", "")
    CoffeeSetting.set("xero_refresh_token", "")
    db_session.commit()
    ok, err = refresh_xero_token()
    assert ok is False

    # 2. حالة وجود مفاتيح مع Mock لطلب التجديد
    CoffeeSetting.set("xero_client_id", "CID_123")
    CoffeeSetting.set("xero_client_secret", "CSEC_456")
    CoffeeSetting.set("xero_refresh_token", "OLD_REFRESH_TOKEN")
    db_session.commit()

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "access_token": "NEW_ACCESS_TOKEN",
        "refresh_token": "NEW_ROTATED_REFRESH_TOKEN",
        "expires_in": 1800
    }

    with patch("requests.post", return_value=mock_resp) as mock_p:
        ok, err = refresh_xero_token()
        assert ok is True
        assert err is None
        assert CoffeeSetting.get("xero_bearer_token") == "NEW_ACCESS_TOKEN"
        assert CoffeeSetting.get("xero_refresh_token") == "NEW_ROTATED_REFRESH_TOKEN"
        assert CoffeeSetting.get("xero_token_expires_at") != ""


def test_xero_request_auto_retries_on_401(db_session):
    """فحص أن _xero_request يقوم بتجديد التوكن وإعادة الطلب تلقائياً عند استلام 401."""
    from coffee_xero import _xero_request

    CoffeeSetting.set("xero_client_id", "CID_123")
    CoffeeSetting.set("xero_client_secret", "CSEC_456")
    CoffeeSetting.set("xero_refresh_token", "VALID_REFRESH_TOKEN")
    CoffeeSetting.set("xero_bearer_token", "EXPIRED_TOKEN")
    CoffeeSetting.set("xero_tenant_id", "T_ID")
    db_session.commit()

    mock_401 = MagicMock()
    mock_401.status_code = 401
    mock_401.text = "Unauthorized"

    mock_200 = MagicMock()
    mock_200.status_code = 200
    mock_200.text = "Success"

    with patch("coffee_xero.refresh_xero_token", return_value=(True, None)) as mock_refresh, \
         patch("requests.request", side_effect=[mock_401, mock_200]) as mock_req:
        resp = _xero_request("GET", "https://api.xero.com/api.xro/2.0/Invoices")
        assert resp.status_code == 200
        assert mock_refresh.call_count == 1
        assert mock_req.call_count == 2



def test_coffee_settings_xero_disconnect(admin_client, db_session):
    """فحص إلغاء ربط Xero ومسح التوكنات ومعرف المنظمة بأمان."""
    CoffeeSetting.set("xero_bearer_token", "tok_to_clear")
    CoffeeSetting.set("xero_refresh_token", "ref_to_clear")
    CoffeeSetting.set("xero_tenant_id", "tenant_to_clear")
    CoffeeSetting.set("xero_tenant_name", "org_to_clear")
    db_session.commit()

    resp = admin_client.post("/coffee/settings", data={"action": "disconnect_xero"}, follow_redirects=True)
    assert resp.status_code == 200
    assert CoffeeSetting.get("xero_bearer_token") == ""
    assert CoffeeSetting.get("xero_refresh_token") == ""
    assert CoffeeSetting.get("xero_tenant_id") == ""
    assert CoffeeSetting.get("xero_tenant_name") == ""


def test_fetch_xero_items_diff(db_session):
    """فحص استخراج ومقارنة أصناف Xero مع الأصناف المحلية واكتشاف الأصناف الجديدة."""
    from coffee_xero import fetch_xero_items_diff

    # إضافة صنف موجود مسبقاً
    it = Item(sku="GB001A", name_ar="كولومبيا سوبريمو", storage_unit="كجم", cost=40.0)
    db_session.add(it)
    db_session.flush()
    ci = CoffeeItem(item_id=it.id, xero_item_code="GB001A", prefix="GB", base_code="001A", coffee_type="green")
    db_session.add(ci)
    db_session.commit()

    # محاكاة رد Xero
    mock_xero_resp = MagicMock()
    mock_xero_resp.status_code = 200
    mock_xero_resp.json.return_value = {
        "Items": [
            {
                "ItemID": "xero-item-1",
                "Code": "GB001A",
                "Name": "GB001A - كولومبيا سوبريمو",
                "PurchaseDetails": {"UnitPrice": 40.0},
                "SalesDetails": {"UnitPrice": 0.0}
            },
            {
                "ItemID": "xero-item-2",
                "Code": "GB099",
                "Name": "GB099 - إثيوبيا قوجي أخضر",
                "PurchaseDetails": {"UnitPrice": 55.0},
                "SalesDetails": {"UnitPrice": 0.0}
            },
            {
                "ItemID": "xero-item-3",
                "Code": "RB099",
                "Name": "RB099 - إثيوبيا قوجي محمص",
                "PurchaseDetails": {"UnitPrice": 55.0},
                "SalesDetails": {"UnitPrice": 95.0}
            },
            {
                "ItemID": "xero-item-4",
                "Code": "NONCOFFEE",
                "Name": "أكواب ورقية 8 أونص",
                "PurchaseDetails": {"UnitPrice": 10.0},
                "SalesDetails": {"UnitPrice": 15.0}
            }
        ]
    }

    with patch("coffee_xero._xero_request", return_value=mock_xero_resp):
        res = fetch_xero_items_diff(only_with_stock=False)
        assert res["success"] is True
        assert res["total_xero_coffee_items"] == 3  # GB001A, GB099, RB099
        assert res["existing_count"] == 1          # GB001A
        assert res["new_count"] == 2               # GB099, RB099

        codes = [item["code"] for item in res["new_items"]]
        assert "GB099" in codes
        assert "RB099" in codes
        assert "GB001A" not in codes
        assert "NONCOFFEE" not in codes

        # فحص شرط الأصناف ذات الرصيد فقط (only_with_stock=True)
        res_stock = fetch_xero_items_diff(only_with_stock=True)
        assert res_stock["success"] is True
        assert res_stock["new_count"] == 0  # الأصناف بدون رصيد يتم حجبها


def test_import_xero_items(db_session):
    """فحص استيراد أصناف Xero وإنشاء سجلات Item و CoffeeItem والربط التلقائي وسجل التدقيق."""
    from coffee_xero import import_xero_items
    from models import User
    if not User.query.get(1):
        db_session.add(User(id=1, name="admin_tester", role="admin", password="password123"))
        db_session.commit()

    items_to_import = [
        {
            "code": "GB888",
            "name": "GB888 - بن كيني أخضر",
            "category": "green",
            "prefix": "GB",
            "base_code": "888",
            "unit_cost": 60.0,
            "sale_price": 0.0,
            "quantity_on_hand": 50.0
        },
        {
            "code": "RB888",
            "name": "RB888 - بن كيني محمص",
            "category": "roasted",
            "prefix": "RB",
            "base_code": "888",
            "unit_cost": 60.0,
            "sale_price": 110.0,
            "quantity_on_hand": 20.0
        }
    ]

    res = import_xero_items(items_to_import, user_id=1)
    assert res["success"] is True
    assert res["imported_count"] == 2

    # التحقق من إنشاء الأصناف في قاعدة البيانات
    gb_item = Item.query.filter_by(sku="GB888").first()
    assert gb_item is not None
    assert gb_item.name_ar == "بن كيني أخضر"
    assert gb_item.cost == 60.0

    rb_item = Item.query.filter_by(sku="RB888").first()
    assert rb_item is not None
    assert rb_item.name_ar == "بن كيني محمص"

    gb_ci = CoffeeItem.query.filter_by(item_id=gb_item.id).first()
    assert gb_ci.coffee_type == "green"
    assert gb_ci.base_code == "888"

    rb_ci = CoffeeItem.query.filter_by(item_id=rb_item.id).first()
    assert rb_ci.coffee_type == "roasted"
    assert rb_ci.sale_price_per_kg == 110.0

    # التحقق من الربط التلقائي بين الأخضر والمحمص لاشتراكهما في base_code
    link = CoffeeItemLink.query.filter_by(green_item_id=gb_ci.id, roasted_item_id=rb_ci.id).first()
    assert link is not None

    # التحقق من تسجيل العملية في سجل التدقيق
    audit = CoffeeAuditLog.query.filter_by(event_type="xero_items_imported").first()
    assert audit is not None
    assert "GB888" in audit.notes


def test_xero_items_sync_views(admin_client, db_session):
    """فحص مسارات واجهة استعراض ومزامنة أصناف Xero وزر الرجوع وشرط الرصيد (GET و POST)."""
    # 1. فحص صفحة العرض GET والتأكد من وجود عنوان الصفحة وزر الرجوع للخلف
    resp = admin_client.get("/coffee/xero/items/sync")
    assert resp.status_code == 200
    assert "مزامنة واكتشاف أصناف القهوة من Xero".encode("utf-8") in resp.data
    assert "رجوع للخلف".encode("utf-8") in resp.data

    # 2. فحص استدعاء JSON GET عبر mock مع كمية رصيد فعلي
    mock_xero_resp = MagicMock()
    mock_xero_resp.status_code = 200
    mock_xero_resp.json.return_value = {
        "Items": [
            {
                "ItemID": "x1",
                "Code": "GB_TEST_VIEW",
                "Name": "GB_TEST_VIEW - بن فحص الواجهة",
                "QuantityOnHand": 120.0,
                "PurchaseDetails": {"UnitPrice": 50.0},
                "SalesDetails": {"UnitPrice": 0.0}
            }
        ]
    }
    with patch("coffee_xero._xero_request", return_value=mock_xero_resp):
        resp_json = admin_client.get("/coffee/xero/items/sync?format=json&stock_only=1")
        assert resp_json.status_code == 200
        data = resp_json.get_json()
        assert data["success"] is True
        assert data["new_count"] >= 1
        assert data["new_items"][0]["quantity_on_hand"] == 120.0

    # 3. فحص استدعاء الاستيراد POST عبر JSON
    post_data = {
        "items": [
            {
                "code": "GB_SYNC_POST",
                "name": "بن تجربة مزامنة",
                "category": "green",
                "prefix": "GB",
                "base_code": "SYNC_POST",
                "unit_cost": 45.0,
                "sale_price": 0.0,
                "quantity_on_hand": 15.0
            }
        ]
    }
    resp_post = admin_client.post("/coffee/xero/items/sync", json=post_data)
    assert resp_post.status_code == 200
    post_res = resp_post.get_json()
    assert post_res["success"] is True
    assert post_res["imported_count"] == 1


def test_report_item_movements_view_and_horizontal_summary(admin_client, db_session):
    """فحص عرض تقرير حركة الأصناف الشامل والشريط التجميعي الأفقي أسفل الجدول."""
    from app_coffee import _get_green_warehouse, _get_roasted_warehouse
    gw = _get_green_warehouse()
    rw = _get_roasted_warehouse()

    # إنشاء صنف أخضر ومحمص وتسجيل حركات متنوعة
    item_g = Item(sku="GB-REP-TEST", name_ar="بن أخضر تجريبي للتقرير", storage_unit="كجم", cost=40.0)
    item_r = Item(sku="RB-REP-TEST", name_ar="بن محمص تجريبي للتقرير", storage_unit="كجم", cost=50.0)
    db_session.add_all([item_g, item_r])
    db_session.flush()

    ci_g = CoffeeItem(item_id=item_g.id, xero_item_code="GB REP TEST", prefix="GB", base_code="REP_TEST", coffee_type="green", default_sack_weight=60.0)
    ci_r = CoffeeItem(item_id=item_r.id, xero_item_code="RB REP TEST", prefix="RB", base_code="REP_TEST", coffee_type="roasted")
    db_session.add_all([ci_g, ci_r])
    db_session.flush()

    # حركات: رصيد افتتاحي (وارد للأخضر)، صرف أخضر للتحميص (منصرف)، استلام محمص ناتج تحميص (وارد للمحمص)
    record_stock_movement(item_g.id, gw.id, "initial", 120.0, notes="رصيد افتتاحي تجريبي")
    record_stock_movement(item_g.id, gw.id, "roasting_green_out", -60.0, reference_type="roasting_order", reference_id=999, notes="صرف تحميص تجريبي")
    record_stock_movement(item_r.id, rw.id, "roasting_roasted_in", 51.0, reference_type="roasting_order", reference_id=999, notes="ناتج تحميص تجريبي")
    db_session.commit()

    # 1. فحص طلب عرض التقرير العام
    res = admin_client.get("/coffee/reports/item-movements")
    assert res.status_code == 200
    html = res.data.decode("utf-8")
    assert "تقرير حركة الأصناف الشامل (كارت الصنف)" in html
    assert "sticky-horizontal-summary" in html
    assert "GB REP TEST" in html
    assert "RB REP TEST" in html

    # 2. فحص الفلترة بنوع البن (الأخضر فقط) - نفحص صفوف الجدول داخل tbody
    res_green = admin_client.get("/coffee/reports/item-movements?coffee_type=green")
    assert res_green.status_code == 200
    html_g = res_green.data.decode("utf-8")
    tbody_g = html_g.split("<tbody>")[1].split("</tbody>")[0]
    assert "GB REP TEST" in tbody_g
    assert "RB REP TEST" not in tbody_g

    # 3. فحص الفلترة بصنف محدد - نفحص صفوف الجدول داخل tbody
    res_item = admin_client.get(f"/coffee/reports/item-movements?item_id={ci_r.id}")
    assert res_item.status_code == 200
    html_item = res_item.data.decode("utf-8")
    tbody_item = html_item.split("<tbody>")[1].split("</tbody>")[0]
    assert "RB REP TEST" in tbody_item
    assert "GB REP TEST" not in tbody_item

    # 4. فحص الفلترة بنوع العملية (تحميص فقط)
    res_roast = admin_client.get("/coffee/reports/item-movements?movement_type=roasting")
    assert res_roast.status_code == 200
    html_roast = res_roast.data.decode("utf-8")
    assert "صرف أخضر للتحميص" in html_roast
    assert "استلام ناتج تحميص" in html_roast

    # 5. فحص تصدير CSV
    res_csv = admin_client.get("/coffee/reports/item-movements?export=csv")
    assert res_csv.status_code == 200
    assert "text/csv" in res_csv.content_type
    assert "attachment; filename=" in res_csv.headers.get("Content-Disposition", "")
    csv_text = res_csv.data.decode("utf-8-sig")
    assert "كود Xero" in csv_text
    assert "وارد (+ كجم)" in csv_text
    assert "منصرف (- كجم)" in csv_text
    assert "GB REP TEST" in csv_text
    assert "الإجمالي" in csv_text

    # 6. فحص تصدير Excel (.xls منسق وملون)
    res_xls = admin_client.get("/coffee/reports/item-movements?export=excel")
    assert res_xls.status_code == 200
    assert "application/vnd.ms-excel" in res_xls.content_type
    assert ".xls" in res_csv.headers.get("Content-Disposition", "") or ".xls" in res_xls.headers.get("Content-Disposition", "")
    xls_html = res_xls.data.decode("utf-8")
    assert "تقرير حركة الأصناف وسجل العمليات الشامل" in xls_html
    assert "GB REP TEST" in xls_html
    assert "الإجمالي والتجميعات العامة" in xls_html
    assert "background-color: #ecfdf5" in xls_html  # ستايل التلوين الأخضر للوارد


def test_xero_status_interactive_statement_and_filters(admin_client, db_session):
    """
    فحص صفحة متابعة الترحيل المحاسبي لـ Xero للتأكد من:
    1. تفعيل بطاقات الإحصائيات التفاعلية مع شارات مرحل ومعلق ومتعثر (فاشل).
    2. وجود شريط التصفية والبيان الشامل (Master Filter Bar).
    3. وجود أشرطة تصفية الجداول المباشرة في الأقسام الثلاثة.
    4. وجود نافذة المودال لبيان العمليات التفصيلي (Statement Modal) مع الجداول وخاصية الطباعة وتصدير CSV.
    5. وجود زر العودة للخلف ⬅️ رجوع للخلف.
    """
    res = admin_client.get("/coffee/xero/status")
    assert res.status_code == 200
    html = res.data.decode("utf-8")

    # 1. فحص زر الرجوع للخلف
    assert "⬅️ رجوع للخلف" in html

    # 2. فحص شريط التصفية والبيان الشامل
    assert "xero-master-bar" in html
    assert "بيان وتصفية العمليات السريعة" in html
    assert "العمليات المتعثرة (الفاشلة)" in html
    assert "العمليات المعلقة" in html
    assert "العمليات المرحلة بنجاح" in html

    # 3. فحص بطاقات الإحصائيات التفاعلية والشارات القابلة للنقر
    assert "xero-interactive-card" in html
    assert "pill-posted" in html
    assert "pill-pending" in html
    assert "pill-failed" in html
    assert "openStatementModal" in html

    # 4. فحص أشرطة تصفية الأقسام
    assert "section-filter-bar" in html
    assert "applySectionFilter" in html
    assert "section-dispatches" in html
    assert "section-green-issues" in html
    assert "section-roasting" in html

    # 5. فحص نافذة بيان العمليات التفصيلي (Modal)
    assert 'id="xeroStatementModal"' in html
    assert 'id="modalTable"' in html
    assert 'id="kpiCount"' in html
    assert 'id="kpiQty"' in html
    assert 'id="kpiAmount"' in html
    assert "printStatementModal" in html
    assert "exportStatementToCSV" in html
    assert "scrollToSectionAndFilter" in html

    # 6. فحص أزرار الترحيل دفعة واحدة (Batch Post)
    assert "btn-batch-post" in html
    assert "confirmBatchPost" in html
    assert "handleModalBatchSubmit" in html
    assert "btnModalBatchPost" in html
    assert "/coffee/xero/post-batch" in html


def test_xero_post_batch_single_click(admin_client, db_session):
    """
    فحص مسار ترحيل العمليات المعلقة دفعة واحدة بنقرة زر واحدة (xero_post_batch):
    1. فحص التعامل مع عدم وجود عمليات معلقة.
    2. فحص الترحيل الدفعي لسندات المحمص والأخضر والتحميص مع المحاكاة (Mocking).
    3. فحص استجابة الـ AJAX / JSON للمودال والشاشات التفاعلية.
    """
    from unittest.mock import patch
    from models_coffee import RoastedCoffeeDispatch, GreenCoffeeIssue, RoastingOrder, CoffeeItem

    # 1. فحص عندما لا توجد عمليات معلقة
    res = admin_client.post("/coffee/xero/post-batch", data={"scope": "all"}, follow_redirects=True)
    assert res.status_code == 200
    assert "لا توجد أي عمليات معلقة" in res.data.decode("utf-8")

    # 2. إنشاء بيانات اختبار لسند محمص معلق
    from models import Location
    wh = Location.query.filter_by(type="warehouse").first()
    if not wh:
        wh = Location(name_ar="مستودع تجريبي", type="warehouse")
        db_session.add(wh)
        db_session.commit()

    disp = RoastedCoffeeDispatch(
        doc_number="MP 9901",
        dispatch_type="branch",
        source_warehouse_id=wh.id,
        dispatch_date=date(2026, 10, 21),
        xero_status="pending"
    )
    db_session.add(disp)
    db_session.commit()

    with patch("coffee_xero.post_roasted_dispatch_to_xero") as mock_disp_post, \
         patch("coffee_xero.post_green_issue_to_xero") as mock_green_post, \
         patch("coffee_xero.post_roasting_order_to_xero") as mock_roast_post:

        mock_disp_post.return_value = {"success": True, "invoice_number": "CN-9901"}
        mock_green_post.return_value = {"success": True, "invoice_number": "INV-9901"}
        mock_roast_post.return_value = {"xero_status": "posted", "errors": []}

        # ترحيل عبر الفورم العادي
        res_post = admin_client.post("/coffee/xero/post-batch", data={"scope": "dispatches"}, follow_redirects=True)
        assert res_post.status_code == 200
        html = res_post.data.decode("utf-8")
        assert "تم ترحيل كافة العمليات المعلقة بنجاح" in html
        assert mock_disp_post.called

        # فحص استجابة JSON للطلبات التفاعلية
        res_json = admin_client.post("/coffee/xero/post-batch", json={"scope": "dispatches"}, headers={"X-Requested-With": "XMLHttpRequest"})
        assert res_json.status_code == 200
        data = res_json.get_json()
        assert "total_processed" in data
        assert "success_count" in data


def test_xero_rejects_draft_vouchers(admin_client, db_session):
    """التحقق من منع ترحيل السندات المسودة إلى Xero واشتراط أن تكون معتمدة ونهائية."""
    from coffee_xero import post_roasted_dispatch_to_xero, post_green_issue_to_xero

    wh = Location.query.filter_by(type="warehouse").first()
    if not wh:
        wh = Location(name_ar="مستودع رئيسي", name_en="Main Wh", type="warehouse")
        db_session.add(wh)
        db_session.commit()

    # 1. إنشاء سند صرف محمص مسودة
    draft_disp = RoastedCoffeeDispatch(
        doc_number="RB DRAFT 01",
        dispatch_type="sale",
        source_warehouse_id=wh.id,
        dispatch_date=date(2026, 10, 21),
        customer_name="عميل مسودة",
        status="draft",
        xero_status="pending"
    )
    db_session.add(draft_disp)

    # 2. إنشاء سند صرف أخضر مسودة
    draft_green = GreenCoffeeIssue(
        doc_number="GB DRAFT 01",
        issue_type="sale",
        warehouse_id=wh.id,
        issue_date=date(2026, 10, 21),
        customer_name="عميل مسودة أخضر",
        status="draft",
        xero_status="pending"
    )
    db_session.add(draft_green)
    db_session.commit()

    # فحص دالة الترحيل المباشرة
    res_disp = post_roasted_dispatch_to_xero(draft_disp.id)
    assert res_disp["success"] is False
    assert "مسودة" in res_disp["error"]

    res_green = post_green_issue_to_xero(draft_green.id)
    assert res_green["success"] is False
    assert "مسودة" in res_green["error"]

    # فحص مسار الويب الفردي
    res_route = admin_client.post(f"/coffee/roasted/dispatches/{draft_disp.id}/post-xero", follow_redirects=True)
    assert res_route.status_code == 200
    html = res_route.data.decode("utf-8")
    assert "لا يمكن ترحيل السند إلى Xero لأنه مسودة" in html

    # فحص شاشة متابعة Xero: لا تظهر المسودات ضمن قائمة المعلق
    status_res = admin_client.get("/coffee/xero/status")
    assert status_res.status_code == 200
    s_html = status_res.data.decode("utf-8")
    assert "RB DRAFT 01" not in s_html
    assert "GB DRAFT 01" not in s_html


def test_signed_voucher_locks_customer_name_and_quantities(admin_client, db_session):
    """التحقق من قفل الكميات واسم العميل نظامياً عند توقيع العميل مع السماح بتعديل الملاحظات فقط."""
    wh = Location.query.filter_by(type="warehouse").first()
    it = Item(sku="RB-YEMEN-LOCK", name_ar="بن يمني مقفل", storage_unit="كجم", cost=50.0)
    db_session.add(it)
    db_session.flush()

    ci = CoffeeItem(item_id=it.id, xero_item_code="RB YEMEN LOCK", prefix="RB", base_code="YEMEN-LOCK", coffee_type="roasted")
    db_session.add(ci)
    db_session.flush()

    sample_sig = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="

    disp = RoastedCoffeeDispatch(
        doc_number="RB SIGNED 01",
        dispatch_type="sale",
        source_warehouse_id=wh.id,
        dispatch_date=date(2026, 10, 21),
        customer_name="مقهى الأصالة الأصلي",
        customer_signature=sample_sig,
        coffee_item_id=ci.id,
        qty_kg=30.0,
        notes="ملاحظة أولية",
        status="approved"
    )
    db_session.add(disp)
    db_session.flush()

    d_line = RoastedCoffeeDispatchLine(
        dispatch_id=disp.id,
        line_number=1,
        coffee_item_id=ci.id,
        qty_kg=30.0,
        unit_cost=50.0,
        total_cost=1500.0,
        sale_price_per_kg=100.0,
        total_sale_amount=3000.0
    )
    db_session.add(d_line)
    db_session.commit()

    # محاولة تعديل اسم العميل والكميات عبر POST
    edit_payload = {
        "doc_number": "RB SIGNED 01",
        "dispatch_date": "2026-10-21",
        "dispatch_type": "sale",
        "customer_name": "اسم متلاعب به",  # محاولة تلاعب
        "line_coffee_item_id[]": [str(ci.id)],
        "line_qty_kg[]": ["999.0"],  # محاولة تغيير الكمية
        "notes": "تم تحديث الملاحظات العامة بنجاح بعد استلام البضاعة"
    }

    res = admin_client.post(f"/coffee/roasted/dispatches/{disp.id}/edit", data=edit_payload, follow_redirects=True)
    assert res.status_code == 200
    html = res.data.decode("utf-8")
    assert "تم قفل الكميات والأصناف واسم العميل نظامياً" in html

    # التحقق من قاعدة البيانات: اسم العميل لم يتغير والكميات لم تتغير، وتغيرت الملاحظات فقط
    db_session.refresh(disp)
    assert disp.customer_name == "مقهى الأصالة الأصلي"
    assert disp.qty_kg == 30.0
    assert disp.notes == "تم تحديث الملاحظات العامة بنجاح بعد استلام البضاعة"
    assert len(disp.lines) == 1
    assert disp.lines[0].qty_kg == 30.0

    # فحص واجهة التعديل: تظهر شارة الأمان وعلامة القفل
    edit_page = admin_client.get(f"/coffee/roasted/dispatches/{disp.id}/edit")
    assert edit_page.status_code == 200
    page_html = edit_page.data.decode("utf-8")
    assert "السند موثق رسمياً بتوقيع العميل المستلم" in page_html
    assert "🔒 مقفل" in page_html


def test_official_delivery_print_pure_quantities_and_declaration(admin_client, db_session):
    """فحص السند الرسمي للطباعة: كميات صافية فقط، ترويسة الشركة، إقرار استلام قانوني، وبدون أي أسعار."""
    wh = Location.query.filter_by(type="warehouse").first()
    it = Item(sku="RB-SPECIAL", name_ar="قهوة خاصة للبيع", storage_unit="كجم", cost=45.0)
    db_session.add(it)
    db_session.flush()

    ci = CoffeeItem(item_id=it.id, xero_item_code="RB SPEC", prefix="RB", base_code="SPEC", coffee_type="roasted")
    db_session.add(ci)
    db_session.flush()

    sample_sig = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="

    disp = RoastedCoffeeDispatch(
        doc_number="RB SALE 55",
        dispatch_type="sale",
        source_warehouse_id=wh.id,
        dispatch_date=date(2026, 10, 21),
        customer_name="مؤسسة النخبة الفاخرة",
        customer_signature=sample_sig,
        coffee_item_id=ci.id,
        qty_kg=50.0,
        sale_price_per_kg=130.0,
        total_sale_amount=6500.0,
        status="approved"
    )
    db_session.add(disp)
    db_session.flush()

    d_line = RoastedCoffeeDispatchLine(
        dispatch_id=disp.id,
        line_number=1,
        coffee_item_id=ci.id,
        qty_kg=50.0,
        unit_cost=45.0,
        total_cost=2250.0,
        sale_price_per_kg=130.0,
        total_sale_amount=6500.0,
        notes="تسليم دفعة أولى"
    )
    db_session.add(d_line)
    db_session.commit()

    res = admin_client.get(f"/coffee/roasted/dispatches/{disp.id}/print")
    assert res.status_code == 200
    html = res.data.decode("utf-8")

    # 1. عنوان السند الرسمي ورقم السند
    assert "سند صرف بن محمص - سند استلام بضاعة" in html
    assert f"رقم السند: {disp.doc_number}" in html

    # 2. بيانات وهوية الشركة
    assert "November Specialty Coffee" in html
    assert "شركة نوفمبر للقهوة المختصة" in html
    assert "1010642921" in html
    assert "310452367800003" in html

    # 3. شارة الاعتماد النهائي
    assert "معتمد ونهائي" in html

    # 4. إقرار الاستلام الرسمي
    assert "إقرار استلام بضاعة رسمي" in html
    assert "أقر أنا المستلم" in html

    # 5. كميات صافية فقط دون أسعار في جدول الأصناف المطبوع
    assert "50.00 كجم" in html
    assert "إجمالي الكميات المستلمة" in html
    assert "6,500" not in html
    assert "130.0" not in html


def test_roasted_sale_dispatch_with_shipping_bypasses_signature_and_prints_carrier_badge(admin_client, db_session):
    """التحقق من اعتماد سند صرف البن المحمص للبيع عبر شركة شحن دون توقيع، وظهور بطاقة الشحن في الطباعة."""
    wh = Location.query.filter_by(type="warehouse").first()
    it = Item(sku="RB-SHIP-TEST", name_ar="بن محمص للشحن", storage_unit="كجم", cost=40.0)
    db_session.add(it)
    db_session.flush()

    ci = CoffeeItem(item_id=it.id, xero_item_code="RB SHIP", prefix="RB", base_code="SHIP", coffee_type="roasted")
    db_session.add(ci)
    db_session.commit()

    # إنشاء سند صرف للبيع مشحون عبر سمسا إلى الخبر
    payload = {
        "doc_number": "RB-SHIP-001",
        "dispatch_date": "2026-10-22",
        "dispatch_type": "sale",
        "customer_name": "مقهى الساحل بالخبر",
        "delivery_method": "shipping",
        "shipping_company": "سمسا",
        "shipping_destination": "الخبر",
        "shipping_tracking_number": "SMSA-987654",
        "customer_signature": "",  # بدون توقيع عميل
        "line_coffee_item_id[]": [str(ci.id)],
        "line_qty_kg[]": ["25.0"],
        "line_sale_price[]": ["95.0"],
        "notes": "شحنة سريعة عبر سمسا للخبر"
    }

    res = admin_client.post("/coffee/roasted/dispatches/new", data=payload, follow_redirects=True)
    assert res.status_code == 200
    html = res.data.decode("utf-8")
    assert "سند الصرف للبيع" in html

    disp = RoastedCoffeeDispatch.query.filter_by(doc_number="RB-SHIP-001").first()
    assert disp is not None
    assert disp.status == "approved"
    assert disp.delivery_method == "shipping"
    assert disp.shipping_company == "سمسا"
    assert disp.shipping_destination == "الخبر"
    assert disp.shipping_tracking_number == "SMSA-987654"
    assert disp.customer_signature is None

    # التحقق من صفحة الطباعة
    print_res = admin_client.get(f"/coffee/roasted/dispatches/{disp.id}/print")
    assert print_res.status_code == 200
    p_html = print_res.data.decode("utf-8")

    assert "مشحونة عبر سمسا إلى الخبر" in p_html
    assert "SMSA-987654" in p_html
    assert "تم تسليم الشحنة للناقل الرسمي" in p_html
    assert "بدون توقيع عميل" not in p_html


def test_green_issue_sale_with_shipping_bypasses_signature_and_prints_carrier_badge(admin_client, db_session):
    """التحقق من اعتماد سند صرف البن الأخضر للبيع عبر ناقل إلى الدمام دون توقيع وظهوره في الطباعة."""
    wh = Location.query.filter_by(type="warehouse").first()
    it = Item(sku="GB-SHIP-TEST", name_ar="بن أخضر للشحن", storage_unit="كجم", cost=35.0)
    db_session.add(it)
    db_session.flush()

    gi = CoffeeItem(item_id=it.id, xero_item_code="GB SHIP", prefix="GB", base_code="SHIP", coffee_type="green", default_sack_weight=60.0)
    db_session.add(gi)
    db_session.commit()

    # إنشاء سند صرف بن أخضر للبيع مشحون عبر ناقل إلى الدمام
    payload = {
        "issue_date": "2026-10-22",
        "issue_type": "sale",
        "customer_name": "محمصة الشرقية بالدمام",
        "delivery_method": "shipping",
        "shipping_company": "ناقل",
        "shipping_destination": "الدمام",
        "shipping_tracking_number": "NAQEL-445566",
        "customer_signature": "",  # بدون توقيع عميل
        "issue_reason": "بيع بن أخضر مع شحن",
        "form_action": "approve",
        "line_coffee_item_id[]": [str(gi.id)],
        "line_qty_kg[]": ["120.0"],
        "line_qty_sacks[]": ["2.0"],
        "line_sack_weight[]": ["60.0"],
        "notes": "شحن خيشتين إلى الدمام"
    }

    res = admin_client.post("/coffee/green/issues/new", data=payload, follow_redirects=True)
    assert res.status_code == 200
    html = res.data.decode("utf-8")
    assert "سند صرف الأخضر للبيع" in html

    issue = GreenCoffeeIssue.query.filter_by(customer_name="محمصة الشرقية بالدمام").first()
    assert issue is not None
    assert issue.status in ("submitted", "approved")
    assert issue.delivery_method == "shipping"
    assert issue.shipping_company == "ناقل"
    assert issue.shipping_destination == "الدمام"
    assert issue.shipping_tracking_number == "NAQEL-445566"
    assert issue.customer_signature is None

    # التحقق من صفحة الطباعة
    print_res = admin_client.get(f"/coffee/green/issues/{issue.id}/print")
    assert print_res.status_code == 200
    p_html = print_res.data.decode("utf-8")

    assert "مشحونة عبر ناقل إلى الدمام" in p_html
    assert "NAQEL-445566" in p_html
    assert "تم تسليم الشحنة للناقل الرسمي" in p_html
    assert "بدون توقيع عميل" not in p_html


def test_inventory_count_report_is_readonly_and_links_to_record_screen(admin_client, db_session):
    """التحقق من أن تقرير جرد الأصناف تقرير استعراضي وتحليلي بدون نماذج تعديل ويحتوي زر تسجيل جرد جديد وخانة قيمة الصنف الفعلي."""
    res = admin_client.get("/coffee/reports/inventory-count")
    assert res.status_code == 200
    html = res.data.decode("utf-8")
    assert "تقرير جرد الأصناف" in html
    assert "/coffee/inventory-count/record" in html
    assert "تسجيل عملية جرد جديدة" in html
    assert "قيمة الصنف (ريال)" in html
    assert "إجمالي قيمة الفعلي" in html
    # لا توجد حقول إدخال لتعديل الكميات في التقرير
    assert 'name="actual_qtys[]"' not in html
    assert "اعتماد وتسوية الجرد الدفتري" not in html


def test_inventory_count_record_screen_and_session_lifecycle(admin_client, db_session):
    """التحقق من عمل شاشة تسجيل الجرد المستقلة، وحفظ محضر الجرد IC-XXXX، وتسوية الفروقات وعرض خانة قيمة الصنف الفعلي في التقرير."""
    from models import Location, Item, StockBalance, StockMovement, record_stock_movement
    from models_coffee import CoffeeItem, CoffeeCountSession, CoffeeCountLine

    from app_coffee import _get_green_warehouse
    gw = _get_green_warehouse()
    it = Item(sku="GB-CNT-REC", name_ar="بن تسجيل جرد", storage_unit="كجم", cost=50.0)
    db_session.add(it)
    db_session.flush()

    ci = CoffeeItem(item_id=it.id, xero_item_code="GB REC", prefix="GB", base_code="REC", coffee_type="green")
    db_session.add(ci)
    db_session.flush()

    # رصيد أولي 100 كجم
    record_stock_movement(it.id, gw.id, "initial", 100.0, notes="رصيد أولي للاختبار")
    db_session.commit()

    # 1. فتح شاشة تسجيل الجرد GET
    res_get = admin_client.get("/coffee/inventory-count/record?coffee_type=green")
    assert res_get.status_code == 200
    get_html = res_get.data.decode("utf-8")
    assert "تسجيل عملية جرد المخزون" in get_html
    assert 'name="actual_qtys[]"' in get_html
    assert "قيمة الصنف (ريال)" in get_html
    assert "اعتماد وتسوية محضر الجرد الآن" in get_html

    # 2. إرسال عملية الجرد الفعلي: رصيد فعلي 92 كجم (عجز 8 كجم)
    payload = {
        "count_date": "2026-10-22",
        "coffee_type": "green",
        "notes": "جرد دوري لشهر أكتوبر 2026",
        "coffee_item_ids[]": [str(ci.id)],
        "actual_qtys[]": ["92.0"]
    }
    res_post = admin_client.post("/coffee/inventory-count/record", data=payload, follow_redirects=True)
    assert res_post.status_code == 200
    post_html = res_post.data.decode("utf-8")

    # 3. التحقق من إنشاء جلسة الجرد في قاعدة البيانات
    session_obj = CoffeeCountSession.query.filter_by(coffee_type="green").order_by(CoffeeCountSession.id.desc()).first()
    assert session_obj is not None
    assert session_obj.count_number.startswith("IC-")
    assert session_obj.status == "approved"
    assert session_obj.total_items >= 1
    assert session_obj.total_diff_kg == -8.0
    assert session_obj.total_diff_cost == -400.0

    # 4. التحقق من إنشاء بند الجرد وحساب قيمة الصنف الفعلي
    line = CoffeeCountLine.query.filter_by(session_id=session_obj.id, coffee_item_id=ci.id).first()
    assert line is not None
    assert line.book_qty == 100.0
    assert line.actual_qty == 92.0
    assert line.actual_val == 4600.0
    assert line.diff_qty == -8.0
    assert line.status_badge == "shortage"

    # 5. التحقق من تحديث رصيد المستودع وتسجيل حركة التسوية
    bal = StockBalance.query.filter_by(item_id=it.id, location_id=gw.id).first()
    assert bal.quantity == 92.0
    mvt = StockMovement.query.filter_by(item_id=it.id, movement_type="adjustment", reference_id=session_obj.id).first()
    assert mvt is not None
    assert mvt.quantity_change == -8.0

    # 6. التحقق من ظهور محضر الجرد ونتائج الفروقات وخانة قيمة الصنف (التكلفة × الفعلي) في التقرير المعروض
    assert session_obj.count_number in post_html
    assert "92.00 كجم" in post_html
    assert "4,600.00 ريال" in post_html
    assert "-8.00" in post_html
    assert "عجز" in post_html


def test_coffee_stock_report_stagnant_analysis_and_kpis(admin_client, db_session):
    """التحقق من عمل تقرير أرصدة البن (الأخضر والمحمص) بنجاح وحل خطأ TypeError،
    مع احتساب شارات الركود، أيام الخمول، بطاقات KPI لرأس المال المجمد، وفلترة الأصناف الراكدة وتصدير CSV.
    """
    from datetime import datetime, timedelta
    from models import Location, Item, StockBalance, StockMovement, record_stock_movement
    from models_coffee import CoffeeItem
    from app_coffee import _get_green_warehouse

    gw = _get_green_warehouse()

    # صنف 1: راكد منذ 75 يوم (+60 يوم)
    it_stag = Item(sku="GB-STAG-01", name_ar="بن كولومبي راكد", storage_unit="كجم", cost=60.0)
    db_session.add(it_stag)
    db_session.flush()
    ci_stag = CoffeeItem(item_id=it_stag.id, name_ar="بن كولومبي راكد", xero_item_code="GB STAG", prefix="GB", base_code="STAG", coffee_type="green")
    db_session.add(ci_stag)
    db_session.flush()
    record_stock_movement(it_stag.id, gw.id, "initial", 50.0, notes="رصيد راكد قديم")
    mvt1 = StockMovement.query.filter_by(item_id=it_stag.id).first()
    mvt1.created_at = datetime.utcnow() - timedelta(days=75)

    # صنف 2: بطيء الحركة منذ 40 يوم (30-60 يوم)
    it_slow = Item(sku="GB-SLOW-02", name_ar="بن إثيوبي بطيء الحركة", storage_unit="كجم", cost=70.0)
    db_session.add(it_slow)
    db_session.flush()
    ci_slow = CoffeeItem(item_id=it_slow.id, name_ar="بن إثيوبي بطيء الحركة", xero_item_code="GB SLOW", prefix="GB", base_code="SLOW", coffee_type="green")
    db_session.add(ci_slow)
    db_session.flush()
    record_stock_movement(it_slow.id, gw.id, "initial", 20.0, notes="رصيد بطيء")
    mvt2 = StockMovement.query.filter_by(item_id=it_slow.id).first()
    mvt2.created_at = datetime.utcnow() - timedelta(days=40)

    # صنف 3: نشط جداً حركة اليوم (<30 يوم)
    it_act = Item(sku="GB-ACT-03", name_ar="بن برازيلي نشط", storage_unit="كجم", cost=45.0)
    db_session.add(it_act)
    db_session.flush()
    ci_act = CoffeeItem(item_id=it_act.id, name_ar="بن برازيلي نشط", xero_item_code="GB ACT", prefix="GB", base_code="ACT", coffee_type="green")
    db_session.add(ci_act)
    db_session.flush()
    record_stock_movement(it_act.id, gw.id, "initial", 100.0, notes="رصيد نشط")
    mvt3 = StockMovement.query.filter_by(item_id=it_act.id).first()
    mvt3.created_at = datetime.utcnow() - timedelta(days=2)

    # صنف 4: صنف بدون أي حركة مخزنية نهائياً ولكن له رصيد يدوي
    it_never = Item(sku="GB-NEVER-04", name_ar="بن رواندا بدون حركة", storage_unit="كجم", cost=80.0)
    db_session.add(it_never)
    db_session.flush()
    ci_never = CoffeeItem(item_id=it_never.id, name_ar="بن رواندا بدون حركة", xero_item_code="GB NEVER", prefix="GB", base_code="NEVER", coffee_type="green")
    db_session.add(ci_never)
    bal_never = StockBalance(item_id=it_never.id, location_id=gw.id, quantity=15.0)
    db_session.add(bal_never)

    db_session.commit()

    # 1. فتح تقرير أرصدة البن والتحقق من عدم وجود TypeError واستجابة 200
    res = admin_client.get("/coffee/reports/coffee-stock")
    assert res.status_code == 200
    html = res.data.decode("utf-8")

    # التحقق من خلو الصفحة من أي أخطاء ومن وجود كروت الـ KPI الرئيسية والجديدة
    assert "تقرير أرصدة البن (الأخضر والمحمص)" in html
    assert "المخزون الراكد (+60 يوم)" in html
    assert "صنف راكد" in html
    assert "تاريخ آخر حركة" in html
    assert "حالة الركود والدوران" in html

    # التحقق من تصنيفات الركود والشارات
    assert "راكد" in html
    assert "بطيء" in html
    assert "نشط" in html

    # 2. التحقق من فلتر الأصناف الراكدة
    res_filter = admin_client.get("/coffee/reports/coffee-stock?stagnant_filter=stagnant")
    assert res_filter.status_code == 200
    html_filter = res_filter.data.decode("utf-8")
    assert "بن كولومبي راكد" in html_filter
    assert "بن برازيلي نشط" not in html_filter

    # 3. التحقق من فلتر الأصناف بدون حركة
    res_never = admin_client.get("/coffee/reports/coffee-stock?stagnant_filter=never_moved")
    assert res_never.status_code == 200
    html_never = res_never.data.decode("utf-8")
    assert "بن رواندا بدون حركة" in html_never
    assert "بن برازيلي نشط" not in html_never

    # 4. التحقق من تصدير ملف CSV وتضمينه لبيانات الركود
    res_csv = admin_client.get("/coffee/reports/coffee-stock?export=csv")
    assert res_csv.status_code == 200
    assert "text/csv" in res_csv.headers.get("Content-Type", "")
    csv_text = res_csv.data.decode("utf-8-sig")
    assert "تاريخ آخر حركة" in csv_text
    assert "أيام الخمول والركود" in csv_text
    assert "حالة النشاط والركود" in csv_text


def test_shipping_fee_calculation():
    """التحقق من احتساب تكلفة الشحن لشركة سمسا حسب معادلة الوزن والوقود."""
    from models_coffee import calculate_shipping_fee
    # وزن <= 15 كجم: 35.00 + 16% (5.60) = 40.60
    assert calculate_shipping_fee(5.0, "سمسا") == 40.60
    assert calculate_shipping_fee(15.0, "SMSA Express") == 40.60

    # وزن بين 15 و 25 كجم: 45.00 + 16% (7.20) = 52.20
    assert calculate_shipping_fee(19.61, "سمسا") == 52.20
    assert calculate_shipping_fee(25.0, "سمسا") == 52.20

    # وزن > 25 كجم: 80.00 + 16% (12.80) = 92.80
    assert calculate_shipping_fee(40.0, "سمسا") == 92.80

    # وزن 0
    assert calculate_shipping_fee(0.0, "سمسا") == 0.0


def test_roasted_dispatch_shipping_line_in_xero_payload(db_session):
    """التحقق من إضافة بند مصاريف الشحن بحساب 261 وكود 'مصاريف شحن' في فاتورة مبيعات زيرو لسند الصرف المشحون."""
    from coffee_xero import build_roasted_issue_sale_invoice_payload
    rw = Location(name_ar="مستودع المحمص للشحن", name_en="Roasted Warehouse Shipping", type="warehouse")
    db_session.add(rw)
    db_session.flush()

    it = Item(sku="RB-SHIP-01", name_ar="بن محمص شحن", storage_unit="كجم", cost=40.0)
    db_session.add(it)
    db_session.flush()
    ci = CoffeeItem(item_id=it.id, xero_item_code="RB SHIP01", prefix="RB", base_code="SHIP01", coffee_type="roasted", sale_price_per_kg=100.0)
    db_session.add(ci)
    db_session.flush()

    dispatch = RoastedCoffeeDispatch(
        doc_number="RB 9901",
        dispatch_date=date.today(),
        source_warehouse_id=rw.id,
        dispatch_type="sale",
        customer_name="كافيه الشرقية",
        delivery_method="shipping",
        shipping_company="سمسا",
        shipping_destination="الخبر",
        shipping_tracking_number="SMSA12345678",
        shipping_fee=40.60,
        coffee_item_id=ci.id,
        qty_kg=10.0,
        sale_price_per_kg=100.0,
        total_sale_amount=1000.0,
    )
    db_session.add(dispatch)
    db_session.flush()

    ln = RoastedCoffeeDispatchLine(
        dispatch_id=dispatch.id,
        line_number=1,
        coffee_item_id=ci.id,
        qty_kg=10.0,
        unit_cost=40.0,
        total_cost=400.0,
        sale_price_per_kg=100.0,
        total_sale_amount=1000.0,
    )
    db_session.add(ln)
    db_session.commit()

    payload = build_roasted_issue_sale_invoice_payload(dispatch)
    assert payload["Type"] == "ACCREC"
    assert payload["Status"] == "DRAFT"
    assert len(payload["LineItems"]) == 2

    # بند البضاعة
    item_line = next(l for l in payload["LineItems"] if l["ItemCode"] == "RB SHIP01")
    assert item_line["LineAmount"] == 1000.0

    # بند مصاريف الشحن
    ship_line = next(l for l in payload["LineItems"] if l.get("AccountCode") == "261")
    assert ship_line["AccountCode"] == "261"
    assert ship_line["TaxType"] == "OUTPUT"
    assert ship_line["LineAmount"] == 40.60
    assert "ايرادات شحن وتوصيل" in ship_line["Description"] or "SMSA" in ship_line["Description"] or "Shipping" in ship_line["Description"]


def test_green_issue_shipping_line_in_xero_payload(db_session):
    """التحقق من إضافة بند مصاريف الشحن بحساب 261 في فاتورة مبيعات زيرو لسند صرف البن الأخضر المشحون."""
    from coffee_xero import build_green_issue_sale_invoice_payload
    from models_coffee import GreenCoffeeIssueLine

    gw = Location(name_ar="مستودع الأخضر للشحن", name_en="Green Warehouse Shipping", type="warehouse")
    db_session.add(gw)
    db_session.flush()

    it = Item(sku="GB-SHIP-01", name_ar="بن أخضر شحن", storage_unit="كجم", cost=30.0)
    db_session.add(it)
    db_session.flush()
    ci = CoffeeItem(item_id=it.id, xero_item_code="GB SHIP01", prefix="GB", base_code="SHIP01", coffee_type="green", sale_price_per_kg=60.0)
    db_session.add(ci)
    db_session.flush()

    issue = GreenCoffeeIssue(
        doc_number="GB 9901",
        issue_date=date.today(),
        warehouse_id=gw.id,
        issue_type="sale",
        customer_name="محمصة الرواد",
        delivery_method="shipping",
        shipping_company="سمسا",
        shipping_destination="الدمام",
        shipping_fee=52.20,
        coffee_item_id=ci.id,
        qty_kg=20.0,
        sale_price_per_kg=60.0,
        total_sale_amount=1200.0,
    )
    db_session.add(issue)
    db_session.flush()

    g_ln = GreenCoffeeIssueLine(
        issue_id=issue.id,
        line_number=1,
        coffee_item_id=ci.id,
        qty_kg=20.0,
        qty_sacks=1.0,
        sack_weight=20.0,
        unit_cost=30.0,
        total_cost=600.0,
        sale_price_per_kg=60.0,
        total_sale_amount=1200.0,
    )
    db_session.add(g_ln)
    db_session.commit()

    payload = build_green_issue_sale_invoice_payload(issue)
    assert payload["Type"] == "ACCREC"
    assert len(payload["LineItems"]) == 2

    ship_line = next(l for l in payload["LineItems"] if l.get("AccountCode") == "261")
    assert ship_line["AccountCode"] == "261"
    assert ship_line["LineAmount"] == 52.20
    assert ship_line["TaxType"] == "OUTPUT"


def test_direct_delivery_no_shipping_line_in_xero(db_session):
    """التحقق من عدم إضافة بند مصاريف الشحن إذا كانت طريقة التسليم مباشرة (غير مشحونة)."""
    from coffee_xero import build_roasted_issue_sale_invoice_payload
    rw = Location(name_ar="مستودع مباشر", name_en="Direct Warehouse", type="warehouse")
    db_session.add(rw)
    db_session.flush()

    it = Item(sku="RB-DIR-01", name_ar="بن محمص مباشر", storage_unit="كجم", cost=40.0)
    db_session.add(it)
    db_session.flush()
    ci = CoffeeItem(item_id=it.id, xero_item_code="RB DIR01", prefix="RB", base_code="DIR01", coffee_type="roasted", sale_price_per_kg=80.0)
    db_session.add(ci)
    db_session.flush()

    dispatch = RoastedCoffeeDispatch(
        doc_number="RB 9902",
        dispatch_date=date.today(),
        source_warehouse_id=rw.id,
        dispatch_type="sale",
        customer_name="عميل مباشر",
        delivery_method="direct",
        coffee_item_id=ci.id,
        qty_kg=5.0,
        sale_price_per_kg=80.0,
        total_sale_amount=400.0,
    )
    db_session.add(dispatch)
    db_session.commit()

    payload = build_roasted_issue_sale_invoice_payload(dispatch)
    line_codes = [l.get("ItemCode") for l in payload["LineItems"]]
    assert "مصاريف شحن" not in line_codes


def test_print_templates_shipped_carrier_and_touch_signature(admin_client, db_session):
    """التحقق من قوالب الطباعة للسندات المشحونة والموقعة إلكترونياً باللمس."""
    from datetime import datetime
    rw = Location(name_ar="مستودع الطباعة", name_en="Print Warehouse", type="warehouse")
    db_session.add(rw)
    db_session.flush()

    it = Item(sku="RB-PRINT-01", name_ar="بن محمص طباعة", storage_unit="كجم", cost=50.0)
    db_session.add(it)
    db_session.flush()
    ci = CoffeeItem(item_id=it.id, xero_item_code="RB PRINT", prefix="RB", base_code="PRINT", coffee_type="roasted")
    db_session.add(ci)
    db_session.flush()

    # 1. سند مشحون: لا يحتوي على (بدون توقيع عميل) ويحتوي على تم تسليم الشحنة للناقل الرسمي
    disp_shipped = RoastedCoffeeDispatch(
        doc_number="RB-SHIP-TEST",
        dispatch_date=date.today(),
        source_warehouse_id=rw.id,
        dispatch_type="sale",
        status="approved",
        delivery_method="shipping",
        shipping_company="سمسا",
        shipping_destination="الخبر",
        shipping_fee=40.60,
        coffee_item_id=ci.id,
        qty_kg=10.0,
    )
    db_session.add(disp_shipped)

    # 2. سند بتوقيع إلكتروني باللمس: يعرض تاريخ ووقت التوقيع
    disp_signed = RoastedCoffeeDispatch(
        doc_number="RB-SIGN-TEST",
        dispatch_date=date.today(),
        source_warehouse_id=rw.id,
        dispatch_type="sale",
        status="approved",
        delivery_method="direct",
        customer_name="عميل التوقيع",
        customer_signature="data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=",
        customer_signed_at=datetime(2026, 10, 25, 14, 30, 0),
        coffee_item_id=ci.id,
        qty_kg=5.0,
    )
    db_session.add(disp_signed)
    db_session.commit()

    # فحص طباعة السند المشحون
    res_shipped = admin_client.get(f"/coffee/roasted/dispatches/{disp_shipped.id}/print")
    assert res_shipped.status_code == 200
    html_shipped = res_shipped.data.decode("utf-8")
    assert "🚚 تم تسليم الشحنة للناقل الرسمي" in html_shipped
    assert "بدون توقيع عميل" not in html_shipped
    # التأكد من عدم عرض أي أسعار أو مصاريف شحن في سند الصرف المطبوع
    assert "40.60" not in html_shipped
    assert "261" not in html_shipped

    # فحص طباعة السند الموقع إلكترونياً باللمس
    res_signed = admin_client.get(f"/coffee/roasted/dispatches/{disp_signed.id}/print")
    assert res_signed.status_code == 200
    html_signed = res_signed.data.decode("utf-8")
    assert "✅ موقع إلكترونياً باللمس" in html_signed
    assert "2026-10-25" in html_signed
    assert "14:30" in html_signed


def test_coffee_inventory_count_edit_draft_and_delete(admin_client, db_session):
    """التحقق من إمكانية حفظ محضر الجرد كمسودة، وتحويله بين مسودة ومعتمد، وتعديله، وحذفه مع تسوية الأرصدة بدقة."""
    from models import Location, Item, StockBalance, StockMovement, record_stock_movement
    from models_coffee import CoffeeItem, CoffeeCountSession, CoffeeCountLine
    from app_coffee import _get_green_warehouse

    gw = _get_green_warehouse()
    it = Item(sku="GB-EDT-DRF", name_ar="بن اختبار التعديل والمسودة", storage_unit="كجم", cost=60.0)
    db_session.add(it)
    db_session.flush()

    ci = CoffeeItem(item_id=it.id, xero_item_code="GB EDT", prefix="GB", base_code="EDT", coffee_type="green")
    db_session.add(ci)
    db_session.flush()

    # رصيد أولي 100 كجم
    record_stock_movement(it.id, gw.id, "initial", 100.0, notes="رصيد أولي للاختبار")
    db_session.commit()

    # 1. حفظ محضر جرد جديد كمسودة (actual = 85 كجم، عجز 15 كجم)
    payload_draft = {
        "action": "draft",
        "count_date": "2026-10-25",
        "coffee_type": "green",
        "notes": "مسودة جرد تجريبية",
        "coffee_item_ids[]": [str(ci.id)],
        "actual_qtys[]": ["85.0"]
    }
    res_draft = admin_client.post("/coffee/inventory-count/record", data=payload_draft, follow_redirects=True)
    assert res_draft.status_code == 200

    session_draft = CoffeeCountSession.query.filter_by(coffee_type="green").order_by(CoffeeCountSession.id.desc()).first()
    assert session_draft is not None
    assert session_draft.status == "draft"
    assert session_draft.status_label_ar == "مسودة جرد"

    # التحقق من عدم تسجيل أي حركة تسوية مخزنية وعدم تغيير الرصيد (لأنه مسودة)
    bal = StockBalance.query.filter_by(item_id=it.id, location_id=gw.id).first()
    assert bal.quantity == 100.0
    mvt = StockMovement.query.filter_by(reference_type="inventory_count", reference_id=session_draft.id).first()
    assert mvt is None

    # 2. اعتماد وتسوية المحضر عبر نقطة النهاية السريعة to-approve
    res_app = admin_client.post(f"/coffee/inventory-count/{session_draft.id}/to-approve", follow_redirects=True)
    assert res_app.status_code == 200
    db_session.refresh(session_draft)
    assert session_draft.status == "approved"
    assert session_draft.status_label_ar == "معتمد ومسوى"

    # الرصيد أصبح 85 كجم وتم تسجيل حركة تسوية بفارق -15 كجم
    bal = StockBalance.query.filter_by(item_id=it.id, location_id=gw.id).first()
    assert bal.quantity == 85.0
    mvt = StockMovement.query.filter_by(reference_type="inventory_count", reference_id=session_draft.id).first()
    assert mvt is not None
    assert mvt.quantity_change == -15.0

    # 3. تحويل المحضر المعتمد إلى مسودة عبر to-draft
    res_to_draft = admin_client.post(f"/coffee/inventory-count/{session_draft.id}/to-draft", follow_redirects=True)
    assert res_to_draft.status_code == 200
    db_session.refresh(session_draft)
    assert session_draft.status == "draft"

    # تم التراجع عن حركة التسوية وعاد الرصيد إلى 100 كجم
    bal = StockBalance.query.filter_by(item_id=it.id, location_id=gw.id).first()
    assert bal.quantity == 100.0
    mvt = StockMovement.query.filter_by(reference_type="inventory_count", reference_id=session_draft.id).first()
    assert mvt is None

    # 4. فتح شاشة تعديل المحضر GET
    res_edit_get = admin_client.get(f"/coffee/inventory-count/{session_draft.id}/edit")
    assert res_edit_get.status_code == 200
    edit_html = res_edit_get.data.decode("utf-8")
    assert session_draft.count_number in edit_html
    assert "تعديل محضر الجرد" in edit_html
    assert 'value="85.00"' in edit_html or 'value="85.0"' in edit_html or '85' in edit_html

    # 5. تعديل المحضر POST واعتماده بكمية فعلية 95 كجم (عجز 5 كجم فقط)
    payload_edit = {
        "action": "approve",
        "count_date": "2026-10-25",
        "coffee_type": "green",
        "notes": "تم التعديل والاعتماد",
        "coffee_item_ids[]": [str(ci.id)],
        "actual_qtys[]": ["95.0"]
    }
    res_edit_post = admin_client.post(f"/coffee/inventory-count/{session_draft.id}/edit", data=payload_edit, follow_redirects=True)
    assert res_edit_post.status_code == 200

    db_session.refresh(session_draft)
    assert session_draft.status == "approved"
    assert session_draft.total_actual_kg == 95.0
    assert session_draft.total_diff_kg == -5.0

    # تم تحديث الرصيد إلى 95 كجم مع حركة تسوية -5 كجم
    bal = StockBalance.query.filter_by(item_id=it.id, location_id=gw.id).first()
    assert bal.quantity == 95.0
    mvt = StockMovement.query.filter_by(reference_type="inventory_count", reference_id=session_draft.id).first()
    assert mvt is not None
    assert mvt.quantity_change == -5.0

    # 6. حذف المحضر نهائياً عبر delete
    res_del = admin_client.post(f"/coffee/inventory-count/{session_draft.id}/delete", follow_redirects=True)
    assert res_del.status_code == 200

    # التحقق من حذف المحضر والتراجع عن حركته وعودة الرصيد إلى 100 كجم
    deleted_session = CoffeeCountSession.query.get(session_draft.id)
    assert deleted_session is None
    deleted_lines = CoffeeCountLine.query.filter_by(session_id=session_draft.id).all()
    assert len(deleted_lines) == 0

    bal = StockBalance.query.filter_by(item_id=it.id, location_id=gw.id).first()
    assert bal.quantity == 100.0
    mvt = StockMovement.query.filter_by(reference_type="inventory_count", reference_id=session_draft.id).first()
    assert mvt is None


def test_green_issue_draft_with_signature_can_be_approved_and_edited(admin_client, db_session):
    """التحقق من أن سند صرف البن الأخضر المحفوظ كمسودة مع توقيع العميل يمكن تعديله واعتماده بنجاح دون أن يقفل."""
    from models import Location, Item, StockBalance, StockMovement
    from models_coffee import CoffeeItem, GreenCoffeeIssue, GreenCoffeeIssueLine
    from app_coffee import _get_green_warehouse

    gw = _get_green_warehouse()
    it = Item(sku="GB-TEST-SIG-01", name_ar="بن أخضر توقيع", storage_unit="كجم", cost=50.0)
    db_session.add(it)
    db_session.flush()

    bal = StockBalance(item_id=it.id, location_id=gw.id, quantity=200.0)
    db_session.add(bal)

    ci = CoffeeItem(item_id=it.id, xero_item_code="GB-TEST-SIG-01", prefix="GB", base_code="TEST-SIG-01", coffee_type="green", default_sack_weight=60.0)
    db_session.add(ci)
    db_session.flush()

    sample_sig = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="

    issue = GreenCoffeeIssue(
        doc_number="GB 016",
        issue_date=date.today(),
        issue_time="10:00",
        issue_type="sale",
        issue_reason="صرف للبيع مع توقيع العميل",
        warehouse_id=gw.id,
        coffee_item_id=ci.id,
        qty_kg=60.0,
        qty_sacks=1.0,
        sack_weight=60.0,
        unit_cost=50.0,
        total_cost=3000.0,
        customer_name="مؤسسة حبوب القهوة",
        customer_signature=sample_sig,
        delivery_method="direct",
        status="draft",
        notes="مسودة موقعة مسبقاً"
    )
    db_session.add(issue)
    db_session.flush()

    line = GreenCoffeeIssueLine(
        issue_id=issue.id,
        line_number=1,
        coffee_item_id=ci.id,
        qty_kg=60.0,
        qty_sacks=1.0,
        sack_weight=60.0,
        unit_cost=50.0,
        total_cost=3000.0,
        sale_price_per_kg=80.0,
        total_sale_amount=4800.0,
    )
    db_session.add(line)
    db_session.commit()

    # 1. فحص صفحة التعديل GET
    res_get = admin_client.get(f"/coffee/green/issues/{issue.id}/edit")
    assert res_get.status_code == 200
    html_get = res_get.data.decode("utf-8")
    assert "اعتماد وتحديث السند" in html_get
    assert "حفظ كمسودة" in html_get
    assert "السند مقفل بالتوقيع" not in html_get
    assert "عند حفظ السند كمسودة لا يُقبل التوقيع" in html_get

    # 2. اختبار حفظ السند كمسودة مع تمرير توقيع — يجب أن يرفض النظام التوقيع ويجعله None
    payload_draft = {
        "form_action": "draft",
        "issue_date": date.today().strftime("%Y-%m-%d"),
        "issue_time": "10:30",
        "issue_type": "sale",
        "issue_reason": "صرف للبيع كمسودة",
        "customer_name": "مؤسسة حبوب القهوة",
        "customer_signature": sample_sig,
        "delivery_method": "direct",
        "line_coffee_item_id[]": [str(ci.id)],
        "line_qty_kg[]": ["60.0"],
        "line_qty_sacks[]": ["1.0"],
        "line_sack_weight[]": ["60.0"],
        "line_sale_price[]": ["80.0"],
        "notes": "محاولة حفظ توقيع في مسودة"
    }
    res_draft = admin_client.post(f"/coffee/green/issues/{issue.id}/edit", data=payload_draft, follow_redirects=True)
    assert res_draft.status_code == 200
    db_session.refresh(issue)
    assert issue.status == "draft"
    assert issue.customer_signature is None  # المسودة لا تقبل التوقيع نهائياً!

    # 3. اعتماد السند POST بـ form_action = approve مع التوقيع — يتم قبوله وتثبيته
    payload = {
        "form_action": "approve",
        "issue_date": date.today().strftime("%Y-%m-%d"),
        "issue_time": "10:30",
        "issue_type": "sale",
        "issue_reason": "صرف للبيع مع توقيع العميل",
        "customer_name": "مؤسسة حبوب القهوة",
        "customer_signature": sample_sig,
        "delivery_method": "direct",
        "line_coffee_item_id[]": [str(ci.id)],
        "line_qty_kg[]": ["60.0"],
        "line_qty_sacks[]": ["1.0"],
        "line_sack_weight[]": ["60.0"],
        "line_sale_price[]": ["80.0"],
        "notes": "تم الاعتماد بعد التوقيع"
    }

    res_post = admin_client.post(f"/coffee/green/issues/{issue.id}/edit", data=payload, follow_redirects=True)
    assert res_post.status_code == 200

    db_session.refresh(issue)
    assert issue.status == "approved"
    assert issue.approved_at is not None
    assert issue.customer_signature == sample_sig
    assert issue.qty_kg == 60.0

    # خصم المخزون
    db_session.refresh(bal)
    assert bal.quantity == 140.0
    mvt = StockMovement.query.filter_by(reference_type="green_issue", reference_id=issue.id).first()
    assert mvt is not None
    assert mvt.quantity_change == -60.0


def test_roasted_dispatch_draft_with_signature_can_be_approved_and_edited(admin_client, db_session):
    """التحقق من أن سند صرف القهوة المحمصة المحفوظ كمسودة لا يقبل التوقيع، بينما يقبله عند الاعتماد."""
    from models import Location, Item, StockBalance, StockMovement
    from models_coffee import CoffeeItem, RoastedCoffeeDispatch, RoastedCoffeeDispatchLine
    from app_coffee import _get_roasted_warehouse

    rw = _get_roasted_warehouse()
    it = Item(sku="RB-TEST-SIG-02", name_ar="بن محمص توقيع", storage_unit="كجم", cost=70.0)
    db_session.add(it)
    db_session.flush()

    bal = StockBalance(item_id=it.id, location_id=rw.id, quantity=100.0)
    db_session.add(bal)

    ci = CoffeeItem(item_id=it.id, xero_item_code="RB-TEST-SIG-02", prefix="RB", base_code="TEST-SIG-02", coffee_type="roasted")
    db_session.add(ci)
    db_session.flush()

    sample_sig = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="

    disp = RoastedCoffeeDispatch(
        doc_number="RB DRAFT SIG",
        dispatch_date=date.today(),
        dispatch_time="11:00",
        dispatch_type="sale",
        issue_reason="صرف محمص للبيع مع توقيع",
        source_warehouse_id=rw.id,
        coffee_item_id=ci.id,
        qty_kg=15.0,
        unit_cost=70.0,
        total_cost=1050.0,
        customer_name="كافيه الواحة",
        customer_signature=None,
        delivery_method="direct",
        status="draft",
        notes="مسودة محمص"
    )
    db_session.add(disp)
    db_session.flush()

    d_line = RoastedCoffeeDispatchLine(
        dispatch_id=disp.id,
        line_number=1,
        coffee_item_id=ci.id,
        qty_kg=15.0,
        unit_cost=70.0,
        total_cost=1050.0,
        sale_price_per_kg=120.0,
        total_sale_amount=1800.0,
    )
    db_session.add(d_line)
    db_session.commit()

    # 1. فحص صفحة التعديل GET
    res_get = admin_client.get(f"/coffee/roasted/dispatches/{disp.id}/edit")
    assert res_get.status_code == 200
    html_get = res_get.data.decode("utf-8")
    assert "اعتماد وتحديث السند" in html_get
    assert "حفظ كمسودة" in html_get
    assert "السند مقفل بالتوقيع" not in html_get
    assert "عند حفظ السند كمسودة لا يُقبل التوقيع" in html_get

    # 2. حفظ السند كمسودة مع تمرير توقيع — يجب أن يرفض النظام التوقيع ويجعله None
    payload_draft = {
        "form_action": "draft",
        "doc_number": "RB DRAFT SIG",
        "dispatch_date": date.today().strftime("%Y-%m-%d"),
        "dispatch_time": "11:15",
        "dispatch_type": "sale",
        "issue_reason": "صرف محمص للبيع كمسودة",
        "customer_name": "كافيه الواحة",
        "customer_signature": sample_sig,
        "delivery_method": "direct",
        "line_coffee_item_id[]": [str(ci.id)],
        "line_qty_kg[]": ["15.0"],
        "line_sale_price[]": ["120.0"],
        "notes": "محاولة حفظ توقيع في مسودة محمص"
    }
    res_draft = admin_client.post(f"/coffee/roasted/dispatches/{disp.id}/edit", data=payload_draft, follow_redirects=True)
    assert res_draft.status_code == 200
    db_session.refresh(disp)
    assert disp.status == "draft"
    assert disp.customer_signature is None  # المسودة لا تقبل التوقيع نهائياً!

    # 3. اعتماد السند POST بـ form_action = approve مع التوقيع — يتم قبوله وتثبيته
    payload = {
        "form_action": "approve",
        "doc_number": "RB DRAFT SIG",
        "dispatch_date": date.today().strftime("%Y-%m-%d"),
        "dispatch_time": "11:15",
        "dispatch_type": "sale",
        "issue_reason": "صرف محمص للبيع مع توقيع",
        "customer_name": "كافيه الواحة",
        "customer_signature": sample_sig,
        "delivery_method": "direct",
        "line_coffee_item_id[]": [str(ci.id)],
        "line_qty_kg[]": ["15.0"],
        "line_sale_price[]": ["120.0"],
        "notes": "تم الاعتماد بنجاح"
    }

    res_post = admin_client.post(f"/coffee/roasted/dispatches/{disp.id}/edit", data=payload, follow_redirects=True)
    assert res_post.status_code == 200

    db_session.refresh(disp)
    assert disp.status == "approved"
    assert disp.approved_at is not None
    assert disp.customer_signature == sample_sig
    assert disp.qty_kg == 15.0

    # خصم المخزون
    db_session.refresh(bal)
    assert bal.quantity == 85.0
    mvt = StockMovement.query.filter_by(reference_type="roasted_dispatch", reference_id=disp.id).first()
    assert mvt is not None
    assert mvt.quantity_change == -15.0


def test_api_item_movements_and_interactive_modal(client, admin_client, db_session):
    """فحص نقطة نهاية كارت الحركات التفاعلي API وشاشات الأرصدة والنافذة المنبثقة."""
    from app_coffee import _get_green_warehouse, _get_roasted_warehouse
    gw = _get_green_warehouse()
    rw = _get_roasted_warehouse()

    # 1. فحص الحماية ومنع غير المسجلين
    resp_unauth = client.get("/coffee/api/item-movements/1")
    assert resp_unauth.status_code == 401
    assert resp_unauth.get_json()["ok"] is False

    # 2. فحص صنف غير موجود
    resp_404 = admin_client.get("/coffee/api/item-movements/999999")
    assert resp_404.status_code == 404
    assert resp_404.get_json()["ok"] is False

    # 3. إنشاء أصناف وحركات واختبار الـ API بدقة
    item_g = Item(sku="GB-MODAL-TEST", name_ar="بن تجريبي للنافذة التفاعلية", storage_unit="كجم", cost=45.0)
    db_session.add(item_g)
    db_session.flush()

    ci_g = CoffeeItem(
        item_id=item_g.id,
        xero_item_code="GB MODAL TEST",
        prefix="GB",
        base_code="MODAL01",
        coffee_type="green",
        name_ar="بن تجريبي للنافذة التفاعلية",
        name_en="Experimental Green Beans",
        sack_allowed=True,
        default_sack_weight=60.0
    )
    db_session.add(ci_g)
    db_session.flush()

    # تسجيل حركتين: توريد رصيد افتتاحي 120 كجم، ثم صرف 30 كجم
    record_stock_movement(item_g.id, gw.id, "initial", 120.0, notes="رصيد افتتاحي للنافذة")
    record_stock_movement(item_g.id, gw.id, "green_issue_out", -30.0, reference_type="green_issue", reference_id=55, notes="صرف تجريبي")
    db_session.commit()

    # استدعاء API حركات الصنف
    resp_api = admin_client.get(f"/coffee/api/item-movements/{ci_g.id}")
    assert resp_api.status_code == 200
    data = resp_api.get_json()
    assert data["ok"] is True
    assert data["item"]["xero_code"] == "GB MODAL TEST"
    assert data["item"]["name_ar"] == "بن تجريبي للنافذة التفاعلية"
    assert data["item"]["current_balance_kg"] == 90.0
    assert data["summary"]["total_in_kg"] == 120.0
    assert data["summary"]["total_out_kg"] == 30.0
    assert data["summary"]["net_change_kg"] == 90.0
    assert data["summary"]["count"] == 2
    assert len(data["movements"]) == 2
    assert data["full_report_url"] is not None
    assert data["excel_export_url"] is not None
    assert "export=excel" in data["excel_export_url"]

    # فحص تحميل ملف الإكسل الفعلي للصنف المحدد
    resp_excel = admin_client.get(data["excel_export_url"])
    assert resp_excel.status_code == 200
    assert "application/vnd.ms-excel" in resp_excel.content_type
    excel_content = resp_excel.data.decode("utf-8")
    assert "GB MODAL TEST" in excel_content
    assert "بن تجريبي للنافذة التفاعلية" in excel_content
    assert "صرف بن أخضر" in excel_content

    # فحص تفاصيل الحركة الأخيرة (صرف أخضر)
    m_out = data["movements"][0]
    assert m_out["movement_type"] == "green_issue_out"
    assert m_out["op_label"] == "صرف بن أخضر"
    assert m_out["qty_out"] == 30.0
    assert m_out["balance_after"] == 90.0

    # 4. فحص صفحة أرصدة البن الأخضر للتأكد من وجود روابط النقر والنافذة المنبثقة وأزرار الإكسل
    resp_green_page = admin_client.get("/coffee/green/stock")
    assert resp_green_page.status_code == 200
    html_green = resp_green_page.data.decode("utf-8")
    assert f"openCoffeeItemMovements({ci_g.id})" in html_green
    assert "coffeeItemMovementsModal" in html_green
    assert "kf-movement-card-pill" in html_green
    assert "cimTopExcelBtn" in html_green
    assert "cimFooterExcelBtn" in html_green

    # 5. فحص صفحة أرصدة البن المحمص للتأكد من وجود روابط النقر والنافذة المنبثقة وأزرار الإكسل
    resp_roasted_page = admin_client.get("/coffee/roasted/stock")
    assert resp_roasted_page.status_code == 200
    html_roasted = resp_roasted_page.data.decode("utf-8")
    assert "coffeeItemMovementsModal" in html_roasted
    assert "kf-movement-card-pill" in html_roasted
    assert "cimTopExcelBtn" in html_roasted
    assert "cimFooterExcelBtn" in html_roasted


def test_coffee_customer_phone_and_deduplication(admin_client, db_session):
    """
    التحقق من:
    1. إضافة حقل رقم جوال العميل في سندات صرف الأخضر والمحمص للبيع.
    2. الحفظ التلقائي للعميل الجديد في دليل العملاء CoffeeCustomer.
    3. منع تكرار العميل عند تطابق رقم الجوال أو الاسم (Deduplication).
    4. ربط السندات بمعرف العميل customer_id وحفظ رقم الجوال.
    5. عمل مسار البحث في العملاء /coffee/api/customers/search.
    """
    from app_coffee import _get_green_warehouse, _get_roasted_warehouse
    gw = _get_green_warehouse()
    rw = _get_roasted_warehouse()

    # إنشاء صنف أخضر
    it_g = Item(sku="GB-CUST-TEST", name_ar="بن كوستاريكا أخضر", storage_unit="كجم", cost=40.0)
    db_session.add(it_g)
    db_session.flush()
    ci_g = CoffeeItem(item_id=it_g.id, xero_item_code="GB CUST", prefix="GB", base_code="CUST", coffee_type="green", sale_price_per_kg=60.0)
    db_session.add(ci_g)

    # إنشاء صنف محمص
    it_r = Item(sku="RB-CUST-TEST", name_ar="بن كوستاريكا محمص", storage_unit="كجم", cost=50.0)
    db_session.add(it_r)
    db_session.flush()
    ci_r = CoffeeItem(item_id=it_r.id, xero_item_code="RB CUST", prefix="RB", base_code="CUST", coffee_type="roasted", sale_price_per_kg=80.0)
    db_session.add(ci_r)

    record_stock_movement(it_g.id, gw.id, "initial", 100.0)
    record_stock_movement(it_r.id, rw.id, "initial", 100.0)
    db_session.commit()

    cust_name = "مقهى الأمل الراقي"
    cust_phone = "0551122334"

    # 1. إنشاء سند صرف بن أخضر للبيع للعميل الجديد مع رقم الجوال
    form_green = {
        "form_action": "draft",
        "issue_type": "sale",
        "issue_date": date.today().isoformat(),
        "issue_reason": "بيع بن أخضر",
        "customer_name": cust_name,
        "customer_phone": cust_phone,
        "line_coffee_item_id[]": [str(ci_g.id)],
        "line_qty_kg[]": ["10.0"],
        "line_sale_price[]": ["60.0"],
    }
    res1 = admin_client.post("/coffee/green/issues/new", data=form_green, follow_redirects=True)
    assert res1.status_code == 200

    # التحقق من إنشاء العميل في جدول coffee_customers
    cust1 = CoffeeCustomer.query.filter_by(phone=cust_phone).first()
    assert cust1 is not None
    assert cust1.name == cust_name
    assert cust1.phone == cust_phone

    # التحقق من ربط السند بمعرف العميل وجواله
    g_issue = GreenCoffeeIssue.query.filter_by(customer_name=cust_name).first()
    assert g_issue is not None
    assert g_issue.customer_phone == cust_phone
    assert g_issue.customer_id == cust1.id

    # 2. إنشاء سند صرف محمص للبيع لنفس العميل بنفس رقم الجوال (مع اسم مختصر)
    # يجب أن يتم ربطه بنفس العميل ولا ينشأ عميل جديد
    form_roasted = {
        "form_action": "draft",
        "dispatch_type": "sale",
        "dispatch_date": date.today().isoformat(),
        "customer_name": "مقهى الأمل",
        "customer_phone": cust_phone,
        "line_coffee_item_id[]": [str(ci_r.id)],
        "line_qty_kg[]": ["5.0"],
        "line_sale_price[]": ["80.0"],
    }
    res2 = admin_client.post("/coffee/roasted/dispatches/new", data=form_roasted, follow_redirects=True)
    assert res2.status_code == 200

    # التحقق من عدم تكرار العميل (لا يزال عميل واحد برقم الجوال هذا)
    matching_custs = CoffeeCustomer.query.filter_by(phone=cust_phone).all()
    assert len(matching_custs) == 1
    assert matching_custs[0].id == cust1.id

    r_dispatch = RoastedCoffeeDispatch.query.filter_by(customer_name="مقهى الأمل").first()
    assert r_dispatch is not None
    assert r_dispatch.customer_phone == cust_phone
    assert r_dispatch.customer_id == cust1.id

    # 3. اختبار البحث في مسار /coffee/api/customers/search
    res_search_phone = admin_client.get(f"/coffee/api/customers/search?q=0551122")
    assert res_search_phone.status_code == 200
    data_phone = res_search_phone.get_json()
    assert data_phone["success"] is True
    assert any(c["id"] == cust1.id for c in data_phone["customers"])

    res_search_name = admin_client.get(f"/coffee/api/customers/search?q=الأمل")
    assert res_search_name.status_code == 200
    data_name = res_search_name.get_json()
    assert data_name["success"] is True
    assert any(c["id"] == cust1.id for c in data_name["customers"])


def test_customer_directory_and_phone_update_and_new_customer_api(admin_client, db_session):
    """
    التحقق من:
    1. إضافة عميل جديد مباشرة عبر API وزر إضافة عميل (+ عميل جديد).
    2. استعراض صفحة دليل العملاء /coffee/customers/ وكروت المؤشرات والفلاتر.
    3. تأكيد وتحديث رقم هاتف العميل المسجل بدون هاتف وتحديث سجلاته.
    """
    # 1. إضافة عميل جديد بدون هاتف في البداية
    new_res = admin_client.post("/coffee/api/customers/new", json={"name": "مؤسسة القهوة الذهبية", "phone": ""})
    assert new_res.status_code == 200
    res_data = new_res.get_json()
    assert res_data["success"] is True
    cust_id = res_data["customer"]["id"]
    assert res_data["customer"]["name"] == "مؤسسة القهوة الذهبية"
    assert res_data["customer"]["phone"] == ""

    # 2. فحص صفحة دليل العملاء
    page_res = admin_client.get("/coffee/customers/")
    assert page_res.status_code == 200
    page_html = page_res.get_data(as_text=True)
    assert "مؤسسة القهوة الذهبية" in page_html
    assert "دليل عملاء القهوة والمبيعات" in page_html
    assert "بانتظار تأكيد رقم الهاتف" in page_html

    # فحص فلتر العملاء بدون هاتف
    no_phone_page = admin_client.get("/coffee/customers/?phone_filter=no_phone")
    assert no_phone_page.status_code == 200
    assert "مؤسسة القهوة الذهبية" in no_phone_page.get_data(as_text=True)

    # 3. تحديث وتأكيد رقم الهاتف للعميل
    update_res = admin_client.post(f"/coffee/api/customers/{cust_id}/update-phone", json={"phone": "0599887766"})
    assert update_res.status_code == 200
    up_data = update_res.get_json()
    assert up_data["success"] is True
    assert up_data["customer"]["phone"] == "0599887766"

    # التحقق من قاعدة البيانات
    cust = CoffeeCustomer.query.get(cust_id)
    assert cust.phone == "0599887766"

    # فحص فلتر العملاء المؤكدين
    with_phone_page = admin_client.get("/coffee/customers/?phone_filter=with_phone")
    assert with_phone_page.status_code == 200
    assert "مؤسسة القهوة الذهبية" in with_phone_page.get_data(as_text=True)


def test_customer_edit_and_delete_with_voucher_protection(admin_client, db_session):
    """
    التحقق من:
    1. تعديل بيانات العميل (الاسم، الجوال، الملاحظات) عبر /coffee/api/customers/<id>/edit.
    2. التحقق من منع تكرار الاسم أو الجوال عند التعديل.
    3. منع حذف العميل في حال وجود سندات صرف مرتبطة به في النظام لحماية السجلات.
    4. نجاح حذف العميل غير المرتبط بأي سندات.
    """
    # 1. إنشاء عميلين للاختبار
    c1 = CoffeeCustomer(name="عميل التعديل الأول", phone="0511112233", notes="ملاحظة قديمة")
    c2 = CoffeeCustomer(name="عميل التعديل الثاني", phone="0522223344")
    db_session.add_all([c1, c2])
    db_session.commit()

    # محاولة تعديل الاسم ليطابق عميل آخر -> يجب أن يفشل 400
    res_dup_name = admin_client.post(f"/coffee/api/customers/{c1.id}/edit", json={
        "name": "عميل التعديل الثاني",
        "phone": "0511112233"
    })
    assert res_dup_name.status_code == 400
    assert "مسجل مسبقاً" in res_dup_name.get_json()["error"]

    # محاولة تعديل الجوال ليطابق عميل آخر -> يجب أن يفشل 400
    res_dup_phone = admin_client.post(f"/coffee/api/customers/{c1.id}/edit", json={
        "name": "عميل التعديل الأول المعدل",
        "phone": "0522223344"
    })
    assert res_dup_phone.status_code == 400
    assert "مسجل مسبقاً" in res_dup_phone.get_json()["error"]

    # تعديل ناجح
    res_edit_ok = admin_client.post(f"/coffee/api/customers/{c1.id}/edit", json={
        "name": "مقهى النخبة المميز",
        "phone": "0599998888",
        "notes": "فرع طريق الملك فهد"
    })
    assert res_edit_ok.status_code == 200
    assert res_edit_ok.get_json()["success"] is True

    db_session.refresh(c1)
    assert c1.name == "مقهى النخبة المميز"
    assert c1.phone == "0599998888"
    assert c1.notes == "فرع طريق الملك فهد"

    # 2. فحص الحذف:
    # حذف c2 (غير مرتبط بأي سند) -> يجب أن ينجح
    res_del_c2 = admin_client.post(f"/coffee/api/customers/{c2.id}/delete")
    assert res_del_c2.status_code == 200
    assert res_del_c2.get_json()["success"] is True
    assert CoffeeCustomer.query.get(c2.id) is None

    # ربط c1 بسند صرف محمص
    from app_coffee import _get_roasted_warehouse
    rw = _get_roasted_warehouse()
    disp = RoastedCoffeeDispatch(
        doc_number="RB-9999",
        dispatch_date=date.today(),
        source_warehouse_id=rw.id,
        dispatch_type="sale",
        customer_name=c1.name,
        customer_id=c1.id,
        customer_phone=c1.phone
    )
    db_session.add(disp)
    db_session.commit()

    # محاولة حذف c1 المرتبط بسند -> يجب أن يفشل مع كود 400 ورسالة حظر واضحة
    res_del_c1 = admin_client.post(f"/coffee/api/customers/{c1.id}/delete")
    assert res_del_c1.status_code == 400
    assert "لا يمكن حذف" in res_del_c1.get_json()["error"]
    assert CoffeeCustomer.query.get(c1.id) is not None




