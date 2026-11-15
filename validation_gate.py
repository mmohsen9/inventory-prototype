"""
بوابة التحقق المسبق (Data Validation Gate)
تُستدعى في 3 نقاط: عند الحفظ كمسودة (فحص مخفف) / عند الإرسال للمراجعة (فحص كامل) / عند الترحيل (فحص نهائي)
"""
import json
import re
from datetime import datetime, timedelta
from models import Item


UNIT_SYNONYMS = {
    "لتر": ["لتر", "1 لتر", "liter", "litres", "litre", "liters", "l", "ltr", "ل"],
    "مل": ["مل", "مليلتر", "ملل", "ml", "milliliter", "milliliters"],
    "كجم": ["كجم", "كيلو", "كيلوجرام", "كيلو جرام", "kg", "kgs", "kilogram", "kilograms", "1 كجم"],
    "جرام": ["جرام", "غرام", "جم", "g", "gm", "gram", "grams", "1 جرام"],
    "حبة": ["حبة", "حبه", "قطعة", "قطعه", "حبات", "piece", "pieces", "pc", "pcs", "each", "ea", "1 حبة"],
    "كرتون": ["كرتون", "كرتونه", "كرتونة", "box", "carton", "ctn", "case", "صندوق"],
    "علبة": ["علبة", "علبه", "can", "tin", "packet", "باكيت"],
    "كيس": ["كيس", "bag", "sack"],
    "درزن": ["درزن", "dozen", "dz"],
    "جالون": ["جالون", "gallon", "gal"],
}


def normalize_unit_str(u):
    """تطبيع اسم وحدة القياس لإزالة الفروق الشكلية والأرقام والبادئات التعبوية."""
    if not u:
        return ""
    s = str(u).strip().lower()
    if s in ("الوحدة (تلقائي)", "تلقائي", "auto", "-", "none", ""):
        return ""
    # تحويل الأرقام العربية إلى لاتينية
    arabic_digits = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")
    s = s.translate(arabic_digits)
    # توحيد الحروف
    s = s.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا").replace("ة", "ه")
    # إزالة الأرقام والكميات البادئة (مثلاً: '1 لتر' -> 'لتر' ، '2.85 لتر' -> 'لتر')
    no_num = re.sub(r"^[\d\.\s]+", "", s).strip()
    return no_num if no_num else s


def are_units_compatible(u1, u2):
    """التحقق من توافق وحدتي قياس سواء بالتطابق المباشر أو التطبيع أو المترادفات."""
    if not u1 or not u2:
        return True
    u1_raw = str(u1).strip().lower()
    u2_raw = str(u2).strip().lower()
    if u1_raw == u2_raw:
        return True

    n1 = normalize_unit_str(u1)
    n2 = normalize_unit_str(u2)
    if not n1 or not n2:
        return True
    if n1 == n2:
        return True
    if n1 in n2 or n2 in n1:
        return True

    # الفحص عبر جدول المترادفات
    for canon, syns in UNIT_SYNONYMS.items():
        norm_syns = {normalize_unit_str(x) for x in syns}
        norm_syns.add(normalize_unit_str(canon))
        if n1 in norm_syns and n2 in norm_syns:
            return True

    return False


def check_item_exists(sku_or_barcode):
    """يبحث عن الصنف محلياً بالـ SKU أو الـ Barcode."""
    item = Item.query.filter(
        (Item.sku == sku_or_barcode) | (Item.barcode == sku_or_barcode)
    ).first()
    return item


def check_unit_match(item, entered_unit, supplier_name=None):
    """
    يتحقق من مطابقة وحدة القياس المدخلة للصنف:
    1. يتطابق مع وحدة التخزين (storage_unit) سواء بالمقارنة المباشرة أو التطبيع أو المترادفات.
    2. يتطابق مع وحدة المكونات (ingredient_unit).
    3. يتطابق مع أي من وحدات الطلب المعتمدة للموردين المرتبطين بالصنف (order_unit).
    """
    if not entered_unit:
        return True  # لم يُدخل الموظف وحدة يدوياً (تُجلب تلقائياً من النظام غالباً)

    norm_entered = normalize_unit_str(entered_unit)
    if not norm_entered:
        return True

    # 1. فحص وحدة التخزين الأساسية
    if item.storage_unit and are_units_compatible(entered_unit, item.storage_unit):
        return True

    # 2. فحص وحدة المكونات
    if item.ingredient_unit and are_units_compatible(entered_unit, item.ingredient_unit):
        return True

    # 3. فحص وحدات الموردين المعتمدة المسجلة للصنف
    item_suppliers = getattr(item, "item_suppliers", [])
    for assoc in item_suppliers:
        if assoc.order_unit and are_units_compatible(entered_unit, assoc.order_unit):
            return True

    return False


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


def validate_line(sku_or_barcode, quantity, location_id=None, entered_unit=None, supplier_name=None):
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

    if not check_unit_match(item, entered_unit, supplier_name=supplier_name):
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


def is_roasted_coffee_internal_supplier(supplier_name):
    """التحقق مما إذا كان السند خاصاً بتوريد أكياس القهوة المحمصة للفروع داخلياً."""
    if not supplier_name:
        return False
    normalized = (
        str(supplier_name).strip()
        .replace("أ", "ا")
        .replace("إ", "ا")
        .replace("آ", "ا")
        .replace("ة", "ه")
    )
    return "اكياس القهوه المحمصه للفروع" in normalized or "اكياس القهوه" in normalized


def validate_purchase_before_submit(purchase):
    """Gate 2: عند إرسال فاتورة الشراء للمراجعة."""
    errors = []
    # فحص المرفقات: إلزامي للموردين الخارجيين، واختياري لسندات توريد أكياس القهوة المحمصة للفروع
    if not is_roasted_coffee_internal_supplier(getattr(purchase, "supplier_name", None)):
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
