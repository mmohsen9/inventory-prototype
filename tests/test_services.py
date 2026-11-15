"""
اختبارات طبقة الخدمات (Service Layer Tests)
التحقق من صحة وقوة منطق أعمال المخزون والتحويلات المفصول عن المتحكمات.
"""
import pytest
from models import db, Item, Location, StockBalance, TransferOrder, TransferLine, StockMovement
from services.inventory_service import adjust_stock_quantity, import_stock_from_csv, get_stock_balance
from services.transfer_service import confirm_transfer_receipt, approve_transfer_with_variance


def test_adjust_stock_quantity_service(test_data, db_session):
    """التحقق من دالة تعديل الرصيد في خدمة المخزون."""
    wh = test_data["wh"]
    user = test_data["admin"]
    item = Item(sku="SKU-SVC-01", name_ar="صنف اختباري للخدمة", storage_unit="حبة", cost=10.0)
    db_session.add(item)
    db_session.commit()

    # 1. تعديل الرصيد إلى 50
    ok, msg, diff = adjust_stock_quantity(item.id, wh.id, 50.0, "رصيد افتتاحي", user.id)
    assert ok is True
    assert diff == 50.0
    assert get_stock_balance(item.id, wh.id) == 50.0

    # 2. تعديل الرصيد إلى 35 (نقص 15)
    ok2, msg2, diff2 = adjust_stock_quantity(item.id, wh.id, 35.0, "تسوية جرد", user.id)
    assert ok2 is True
    assert diff2 == -15.0
    assert get_stock_balance(item.id, wh.id) == 35.0


def test_import_stock_from_csv_service(test_data, db_session):
    """التحقق من دالة استيراد الأرصدة عبر CSV في خدمة المخزون."""
    wh = test_data["wh"]
    user = test_data["admin"]
    item1 = Item(sku="SKU-CSV-A", name_ar="صنف CSV 1", storage_unit="حبة", cost=15.0)
    item2 = Item(sku="SKU-CSV-B", name_ar="صنف CSV 2", storage_unit="حبة", cost=25.0)
    db_session.add_all([item1, item2])
    db_session.commit()

    csv_data = f"""sku,location_id,quantity,notes
SKU-CSV-A,{wh.id},100.0,استيراد A
SKU-CSV-B,{wh.id},200.0,استيراد B
SKU-NOT-EXIST,{wh.id},50.0,صنف غير موجود
"""
    updated, errors = import_stock_from_csv(csv_data, user.id)
    assert updated == 2
    assert len(errors) == 1
    assert "غير موجود" in errors[0]
    assert get_stock_balance(item1.id, wh.id) == 100.0
    assert get_stock_balance(item2.id, wh.id) == 200.0


def test_transfer_service_confirm_and_approve(test_data, db_session):
    """التحقق من تأكيد واعتماد التحويلات عبر خدمة التحويلات."""
    wh = test_data["wh"]
    br = test_data["br1"]
    user = test_data["admin"]

    item = Item(sku="SKU-TR-01", name_ar="صنف تحويل", storage_unit="حبة", cost=20.0)
    db_session.add(item)
    db_session.commit()

    # رصيد في المستودع
    adjust_stock_quantity(item.id, wh.id, 100.0, "أولي", user.id)

    # إنشاء تحويل مرسل
    transfer = TransferOrder(from_location_id=wh.id, to_location_id=br.id, status="sent", created_by=user.id)
    db_session.add(transfer)
    db_session.flush()

    line = TransferLine(transfer_id=transfer.id, item_id=item.id, qty_sent=30.0)
    db_session.add(line)
    db_session.commit()

    # 1. استلام بفرق (استلم 28 بدلاً من 30) -> needs_review
    rec_data_var = {line.id: {"qty_received": 28.0, "variance_reason": "نقص حبتين في الكرتون"}}
    has_var, msg_var = confirm_transfer_receipt(transfer, rec_data_var, user.id)
    assert has_var is True
    assert transfer.status == "needs_review"

    # 2. اعتماد المحاسب للفروقات
    ok_app, msg_app = approve_transfer_with_variance(transfer, user.id)
    assert ok_app is True
    assert transfer.status == "approved"
    # التحقق من إضافة 28 لفرع الوجهة
    assert get_stock_balance(item.id, br.id) == 28.0

