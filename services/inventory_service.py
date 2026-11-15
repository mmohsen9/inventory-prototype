"""
خدمة إدارة وتسويات المخزون (Inventory Service)
معالجة تسويات الأرصدة، الاستيراد، وحركات دفتر الأستاذ.
"""
import csv
import io
from models import db, Item, Location, StockBalance, record_stock_movement


def get_stock_balance(item_id, location_id):
    """جلب رصيد صنف في موقع محدد."""
    bal = StockBalance.query.filter_by(item_id=item_id, location_id=location_id).first()
    return bal.quantity if bal else 0.0


def adjust_stock_quantity(item_id, location_id, new_quantity, reason, user_id):
    """
    تعديل وتسوية رصيد صنف يدوياً مع تسجيل حركة دفتر الأستاذ (StockMovement).
    يعيد (نجاح: bool, رسالة: str, فرق الكمية: float)
    """
    item = Item.query.get(item_id)
    if not item:
        return False, "الصنف المحدد غير موجود بالنظام", 0.0

    location = Location.query.get(location_id)
    if not location:
        return False, "الموقع المحدد غير موجود بالنظام", 0.0

    if new_quantity is None or new_quantity < 0:
        return False, "الكمية الجديدة المدخلة غير صالحة", 0.0

    current_qty = get_stock_balance(item_id, location_id)
    diff = round(new_quantity - current_qty, 4)

    if diff != 0:
        record_stock_movement(
            item_id=item_id,
            location_id=location_id,
            movement_type="manual_adjustment",
            quantity_change=diff,
            reference_type="manual",
            reference_id=0,
            notes=f"{reason or 'تسوية وتصحيح رصيد يدوي'} (من {current_qty} إلى {new_quantity})",
            user_id=user_id,
        )
        db.session.commit()
        return True, f"تم تعديل رصيد الصنف '{item.name_ar}' في '{location.name_ar}' بنجاح إلى {new_quantity}", diff

    return True, f"رصيد الصنف '{item.name_ar}' في '{location.name_ar}' مطابق بالفعل للقيمة المدخلة ({new_quantity})", 0.0


def import_stock_from_csv(csv_text, user_id):
    """
    استيراد أرصدة افتتاحية أو تصحيحية عبر ملف CSV مع فحص وتدقيق كل سطر.
    يعيد (عدد_الأسطر_المحدثة: int, قائمة_الأخطاء: list[str])
    """
    stream = io.StringIO(csv_text, newline=None)
    reader = csv.DictReader(stream)
    updated_count = 0
    errors = []

    for idx, row in enumerate(reader, start=2):
        sku = (row.get("sku") or row.get("SKU") or "").strip()
        loc_val = (row.get("location_id") or row.get("location") or "").strip()
        qty_val = (row.get("quantity") or row.get("qty") or "").strip()
        notes = (row.get("notes") or "استيراد أرصدة عبر ملف CSV").strip()

        if not sku or not loc_val or not qty_val:
            continue

        try:
            new_qty = float(qty_val)
        except ValueError:
            errors.append(f"السطر {idx}: كمية غير صالحة ({qty_val})")
            continue

        item = Item.query.filter_by(sku=sku).first()
        if not item:
            errors.append(f"السطر {idx}: كود الصنف SKU '{sku}' غير موجود")
            continue

        location = None
        if loc_val.isdigit():
            location = Location.query.get(int(loc_val))
        if not location:
            location = Location.query.filter(
                db.or_(Location.name_ar == loc_val, Location.name_en.ilike(loc_val))
            ).first()

        if not location:
            errors.append(f"السطر {idx}: الموقع '{loc_val}' غير معرف بالنظام")
            continue

        ok, msg, _ = adjust_stock_quantity(
            item_id=item.id,
            location_id=location.id,
            new_quantity=new_qty,
            reason=notes,
            user_id=user_id
        )
        if ok:
            updated_count += 1
        else:
            errors.append(f"السطر {idx}: {msg}")

    return updated_count, errors

