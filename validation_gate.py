"""
بوابة التحقق المسبق (Data Validation Gate)
تُستدعى في 3 نقاط: عند الحفظ كمسودة (فحص مخفف) / عند الإرسال للمراجعة (فحص كامل) / عند الترحيل (فحص نهائي)
"""
import json
from datetime import datetime, timedelta
from models import Item


def check_item_exists(sku_or_barcode):
    """يبحث عن الصنف محلياً بالـ SKU أو الـ Barcode."""
    item = Item.query.filter(
        (Item.sku == sku_or_barcode) | (Item.barcode == sku_or_barcode)
    ).first()
    return item


def check_unit_match(item, entered_unit):
    if not entered_unit:
        return True  # لم يُدخل الموظف وحدة يدوياً (تُجلب تلقائياً من النظام غالباً)
    return item.storage_unit.strip() == entered_unit.strip()


def check_item_active_in_branch(item, location_id):
    try:
        active_list = json.loads(item.active_branches or "[]")
    except Exception:
        active_list = []
    if not active_list:
        # إذا لم تُحدَّد قائمة فروع مفعّلة، نعتبره متاحاً في كل الفروع (سلوك افتراضي آمن للنموذج الأولي)
        return True
    return location_id in active_list


def check_sync_freshness(item, max_age_hours=1):
    if not item.last_synced_at:
        return False
    return datetime.utcnow() - item.last_synced_at <= timedelta(hours=max_age_hours)


def validate_line(sku_or_barcode, quantity, location_id=None, entered_unit=None):
    """
    يُستخدم عند إضافة كل بند لفاتورة/تحويل/جرد.
    يُرجع: (is_valid: bool, item_or_none, error_message_ar: str)
    """
    if not sku_or_barcode:
        return False, None, "لم يتم إدخال كود الصنف أو مسحه"

    item = check_item_exists(sku_or_barcode)
    if not item:
        return False, None, "الصنف غير موجود في القائمة المحلية — يمكنك إرسال طلب صنف جديد"

    if quantity is None or quantity <= 0:
        return False, item, "الكمية غير صحيحة، يجب أن تكون أكبر من صفر"

    if not check_unit_match(item, entered_unit):
        return False, item, f"وحدة القياس المدخلة لا تطابق وحدة الصنف في فوديكس (المطلوب: {item.storage_unit})"

    if location_id and not check_item_active_in_branch(item, location_id):
        return False, item, "هذا الصنف غير معرف في هذا الفرع — يجب تفعيله في فوديكس أولاً"

    if not check_sync_freshness(item):
        # تحذير فقط وليس منع كامل في النموذج الأولي، لتفادي إيقاف العمل بسبب تأخر مهمة المزامنة
        return True, item, "تنبيه: قائمة الأصناف قديمة (أكثر من ساعة)، يُفضّل تحديث المزامنة"

    return True, item, ""


def validate_attachment(attachments_json):
    """يمنع إتمام العملية بدون مرفق واحد على الأقل برابط صالح."""
    try:
        attachments = json.loads(attachments_json or "[]")
    except Exception:
        attachments = []
    valid = [a for a in attachments if a and isinstance(a, str) and len(a) > 3]
    if not valid:
        return False, "لا يمكن إتمام العملية بدون رفع صورة الفاتورة أو السند"
    return True, ""


def validate_purchase_before_submit(purchase):
    """Gate 2: عند إرسال فاتورة الشراء للمراجعة."""
    errors = []
    ok, msg = validate_attachment(purchase.attachments)
    if not ok:
        errors.append(msg)
    if not purchase.lines:
        errors.append("لا يمكن إرسال فاتورة بدون أي أصناف")
    for line in purchase.lines:
        if not line.item:
            errors.append("يوجد بند بصنف غير معرّف، احذفه أو أكمل بياناته")
            continue
        if line.quantity <= 0:
            errors.append(f"الكمية غير صحيحة للصنف: {line.item.name_ar or line.item.name_en}")
    return (len(errors) == 0), errors


def validate_purchase_before_posting(purchase, location_id):
    """Gate 3: فحص نهائي فوري قبل استدعاء Foodics API فعلياً (تحسباً لتغيّر البيانات بعد الاعتماد)."""
    errors = []
    for line in purchase.lines:
        item = line.item
        if not item:
            errors.append("صنف غير معرّف داخل الفاتورة")
            continue
        if not check_item_active_in_branch(item, location_id):
            errors.append(f"الصنف {item.name_ar or item.name_en} لم يعد مفعّلاً في هذا الموقع")
    return (len(errors) == 0), errors


def validate_transfer_before_send(transfer):
    errors = []
    if not transfer.lines:
        errors.append("لا يمكن إرسال تحويل بدون أصناف")
    for line in transfer.lines:
        if not line.item:
            errors.append("يوجد بند بصنف غير معرّف")
        elif line.qty_sent <= 0:
            errors.append(f"الكمية المصروفة غير صحيحة للصنف: {line.item.name_ar or line.item.name_en}")
    return (len(errors) == 0), errors
