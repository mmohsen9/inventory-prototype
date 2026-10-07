"""
طابور الترحيل الخلفي (Background Sync Queue) + الربط مع Foodics API v5 + أدوات مزامنة الماستر داتا
مبني بالكامل على التوثيق الرسمي لـ Foodics Core API v5:
- Base URLs: Sandbox (https://api-sandbox.foodics.com/v5) و Production (https://api.foodics.com/v5)
- Headers: Authorization: Bearer {token}, Accept: application/json, Content-Type: application/json
- Scopes المدعومة:
    - general.read: /branches, /warehouses, /whoami
    - inventory.transactions.read/write: /purchase_orders, /transfer_orders, /inventory_adjustments, /inventory_counts
    - inventory.settings.read/write: /inventory_items, /suppliers
"""
import uuid
import random
import time
import os
try:
    import requests
except ImportError:
    requests = None
from datetime import datetime, timedelta
from models import (
    db, SyncQueue, SyncLog, PurchaseTransaction, TransferOrder, NewItemRequest, CountSession, SystemSetting,
    Location, Item, Supplier, Notification, ItemSupplier
)

ERROR_TRANSLATION_MAP = {
    "ITEM_NOT_FOUND_IN_BRANCH": "الصنف غير معرّف في الفرع المستلم — فعّله في فوديكس ثم أعد الترحيل",
    "UNIT_MISMATCH": "وحدة القياس غير متطابقة مع الصنف في فوديكس — راجع وحدة الصنف وصحّحها",
    "UNAUTHORIZED": "انتهت صلاحية الاتصال مع فوديكس — تم إيقاف الترحيل التلقائي تلقائياً، يلزم تجديد رمز الدخول (Token)",
    "FORBIDDEN": "لا تملك صلاحية كافية على هذا المورد (403) — تواصل مع دعم فوديكس للتأكد من الصلاحيات",
    "RATE_LIMITED": "عدد الطلبات تجاوز الحد المسموح من فوديكس (90 طلب/دقيقة) — سيُعاد الإرسال تلقائياً بعد المهلة المحددة",
    "SERVER_ERROR": "خطأ مؤقت من خادم فوديكس — سيُعاد المحاولة تلقائياً",
    "DUPLICATE_REFERENCE": "هذه العملية مرحّلة مسبقاً لفوديكس — لا تُرسل مرة أخرى",
    "VALIDATION_ERROR": "بيانات العملية غير مكتملة أو غير صحيحة (422) — راجع الأصناف والكميات والوحدات",
    "BLOCKED": "تم حظر الاتصال مؤقتاً من فوديكس (Cloudflare) بسبب تجاوز الحدود — التزم بفترة الانتظار",
}


def translate_foodics_error(error_code):
    if not error_code:
        return "خطأ غير محدد"
    if error_code in ERROR_TRANSLATION_MAP:
        return ERROR_TRANSLATION_MAP[error_code]
    err_str = str(error_code)
    if "creator id" in err_str.lower():
        return "معرف المستخدم (creator_id) مطلوب من فوديكس — تحقق من صلاحيات الرمز عبر فحص الاتصال"
    if "selected items" in err_str.lower() and "invalid" in err_str.lower():
        return "أحد الأصناف في العملية غير معرّف في حساب فوديكس الحالي — يرجى الضغط على 'مزامنة الأصناف' لتحديث الربط"
    if "branch_id" in err_str.lower():
        return "الفرع المحدد غير صالح أو غير معرّف في فوديكس — حدّث مزامنة الفروع"
    return f"خطأ من فوديكس: {err_str}"


REQUIRED_HEADERS_BASE = {
    "Accept": "application/json",
    "Content-Type": "application/json",
}

FOODICS_RATE_LIMIT_PER_MINUTE = 90


def generate_idempotency_key(source_type, source_id):
    return f"{source_type}-{source_id}-{uuid.uuid4().hex[:12]}"


def enqueue(source_type, source_id):
    key = generate_idempotency_key(source_type, source_id)
    job = SyncQueue(
        source_type=source_type,
        source_id=source_id,
        idempotency_key=key,
        status="queued",
    )
    db.session.add(job)
    db.session.commit()
    return job


def is_sync_temporarily_blocked():
    blocked_until_str = SystemSetting.get_val("foodics_blocked_until")
    if not blocked_until_str:
        return False, None
    try:
        blocked_until = datetime.fromisoformat(blocked_until_str)
    except Exception:
        return False, None
    if datetime.utcnow() < blocked_until:
        remaining = (blocked_until - datetime.utcnow()).total_seconds()
        return True, remaining
    return False, None


def set_temporary_block(seconds):
    blocked_until_dt = datetime.utcnow() + timedelta(seconds=seconds)
    SystemSetting.set_val("foodics_blocked_until", blocked_until_dt.isoformat())


def disable_sync_due_to_auth_failure(reason):
    SystemSetting.set_val("foodics_sync_enabled", "0")
    SystemSetting.set_val("foodics_last_auth_error", reason)


# -----------------------------------------------------------------------------
# فحص الاتصال بـ Foodics API (Sandbox أو Production)
# -----------------------------------------------------------------------------
def test_foodics_connection(token, base_url=None):
    """
    يفحص صحة الاتصال وصلاحيات الـ Token عبر استدعاء /whoami و /branches
    """
    if not token or not token.strip():
        return False, "يرجى إدخال رمز الدخول (Token) أولاً لاختبار الاتصال."

    if not base_url:
        base_url = SystemSetting.get_val("foodics_base_url", "https://api-sandbox.foodics.com/v5")
    base_url = base_url.rstrip("/")

    headers = dict(REQUIRED_HEADERS_BASE)
    headers["Authorization"] = f"Bearer {token.strip()}"

    try:
        business_name = None
        # 1. فحص بيانات الحساب والنشاط عبر /whoami
        try:
            whoami_res = requests.get(f"{base_url}/whoami", headers=headers, timeout=10)
            if whoami_res.status_code == 200:
                w_data = whoami_res.json().get("data", {})
                business_name = w_data.get("business", {}).get("name") or w_data.get("name")
                creator_id = w_data.get("user", {}).get("id") or w_data.get("business", {}).get("owner_id")
                if business_name:
                    SystemSetting.set_val("foodics_business_name", business_name)
                if creator_id:
                    SystemSetting.set_val("foodics_creator_id", creator_id)
        except Exception:
            pass

        # 2. فحص الفروع وصلاحية general.read عبر /branches
        res = requests.get(f"{base_url}/branches", headers=headers, timeout=12)

        remaining = res.headers.get("X-RateLimit-Remaining")
        if remaining is not None:
            SystemSetting.set_val("foodics_rate_remaining", remaining)

        if res.status_code == 200:
            data = res.json()
            branches_count = len(data.get("data", []))
            SystemSetting.set_val("foodics_sync_enabled", "1")
            SystemSetting.set_val("foodics_last_auth_error", "")
            SystemSetting.set_val("foodics_last_connected", datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"))
            
            b_info = f" لمنشأة '{business_name}'" if business_name else ""
            env_label = "تجريبية (Sandbox)" if "sandbox" in base_url.lower() else "إنتاجية (Production)"
            return True, f"تم الاتصال بنجاح ببيئة فوديكس {env_label}{b_info}! تم جلب {branches_count} فرع، والربط جاهز للعمل."

        elif res.status_code == 401:
            disable_sync_due_to_auth_failure("401 رمز الدخول غير صالح أو منتهي")
            return False, "رمز الدخول (Token) غير صالح أو منتهي الصلاحية (401). تأكد من صحة التوكن المنسوخ من إيميل المالك."
        elif res.status_code == 403:
            return False, "لا تملك الصلاحية الكافية للوصول (403 Forbidden). تأكد من توفر صلاحية general.read على التوكن."
        elif res.status_code == 429:
            retry_after = int(res.headers.get("retry-after", 60))
            set_temporary_block(retry_after)
            return False, f"تم تجاوز حد الطلبات المسموح (429 Rate Limit). انتظر {retry_after} ثانية."
        else:
            return False, f"استجابة غير متوقعة من فوديكس (كود {res.status_code}): {res.text[:120]}"
    except Exception as e:
        return False, f"تعذر الاتصال بخادم Foodics: {str(e)}"


# -----------------------------------------------------------------------------
# مزامنة البيانات التأسيسية: الفروع والمستودعات
# -----------------------------------------------------------------------------
def sync_branches_from_foodics(token=None, base_url=None):
    token = token or SystemSetting.get_val("foodics_token")
    base_url = (base_url or SystemSetting.get_val("foodics_base_url") or "https://api-sandbox.foodics.com/v5").rstrip("/")
    if not token:
        return False, "رمز الدخول (Token) غير متوفر. يرجى حفظ التوكن أولاً."

    headers = dict(REQUIRED_HEADERS_BASE)
    headers["Authorization"] = f"Bearer {token.strip()}"

    try:
        res = requests.get(f"{base_url}/branches", headers=headers, timeout=15)
        if res.status_code != 200:
            return False, f"فشل جلب الفروع (كود {res.status_code}): {res.text[:120]}"
        branches = res.json().get("data", [])

        # جلب المستودعات أيضاً إن وُجدت
        warehouses = []
        try:
            w_res = requests.get(f"{base_url}/warehouses", headers=headers, timeout=12)
            if w_res.status_code == 200:
                warehouses = w_res.json().get("data", [])
        except Exception:
            pass

        all_locations = branches + warehouses
        synced_count = 0
        new_count = 0

        for b in all_locations:
            b_id = str(b.get("id"))
            b_name = b.get("name") or ""
            b_name_ar = b.get("name_localized") or b_name
            is_warehouse = (b.get("type") == 2 or "warehouse" in b_name.lower() or "مستودع" in b_name_ar)

            loc = Location.query.filter_by(foodics_id=b_id).first()
            if not loc:
                loc = Location.query.filter(
                    db.or_(
                        Location.name_ar == b_name_ar,
                        Location.name_en.ilike(b_name),
                        Location.name_ar.ilike(b_name)
                    )
                ).first()

            if loc:
                loc.foodics_id = b_id
                if not loc.name_en and b_name:
                    loc.name_en = b_name
                synced_count += 1
            else:
                new_loc = Location(
                    foodics_id=b_id,
                    name_ar=b_name_ar,
                    name_en=b_name or b_name_ar,
                    type="warehouse" if is_warehouse else "branch"
                )
                db.session.add(new_loc)
                new_count += 1

        db.session.commit()
        return True, f"تمت مزامنة الفروع بنجاح: تم ربط ومطابقة {synced_count} موقع، وإضافة {new_count} موقع جديد."
    except Exception as exc:
        db.session.rollback()
        return False, f"خطأ أثناء مزامنة الفروع: {str(exc)}"


# -----------------------------------------------------------------------------
# مزامنة البيانات التأسيسية: أصناف المخزون الخام
# -----------------------------------------------------------------------------
def process_foodics_item_payload(item_data, trigger_notification=True):
    """
    معالجة صنف مخزون مستلم من فوديكس (عبر الويب هوك أو المزامنة المباشرة)
    وإضافته إلى جدول Items وتوليد إشعار فوري في النظام.
    """
    if not item_data or not isinstance(item_data, dict):
        return None, False, "بيانات الصنف غير صالحة"

    f_id = str(item_data.get("id") or "").strip()
    sku = (item_data.get("sku") or "").strip()
    name = item_data.get("name") or ""
    name_ar = item_data.get("name_localized") or name
    storage_unit = item_data.get("storage_unit") or ""
    ingredient_unit = str(item_data.get("ingredient_unit") or "")

    try:
        factor = float(item_data.get("storage_to_ingredient_factor") or 1.0)
    except (ValueError, TypeError):
        factor = 1.0

    try:
        cost = float(item_data.get("cost") or 0.0)
    except (ValueError, TypeError):
        cost = 0.0

    barcode = str(item_data.get("barcode") or "").strip()

    if not sku:
        return None, False, "رمز الصنف (SKU) غير موجود"

    item = None
    if f_id:
        item = Item.query.filter_by(foodics_id=f_id).first()
    if not item and sku:
        item = Item.query.filter_by(sku=sku).first()

    is_new = False
    if item:
        if f_id:
            item.foodics_id = f_id
        if storage_unit:
            item.storage_unit = storage_unit
        if ingredient_unit:
            item.ingredient_unit = ingredient_unit
        if cost > 0:
            item.cost = cost
        if name:
            item.name_en = name
        if name_ar:
            item.name_ar = name_ar
        if barcode and not item.barcode:
            item.barcode = barcode
        item.sync_status = "synced"
        item.last_synced_at = datetime.utcnow()
    else:
        is_new = True
        item = Item(
            foodics_id=f_id or None,
            sku=sku,
            name_ar=name_ar,
            name_en=name,
            storage_unit=storage_unit,
            ingredient_unit=ingredient_unit,
            storage_to_ingredient_factor=factor,
            cost=cost,
            barcode=barcode or None,
            sync_status="synced",
            last_synced_at=datetime.utcnow(),
        )
        db.session.add(item)
        db.session.flush()

    # فحص وربط المورد إذا وُجد في البيانات الواردة
    incoming_supplier = None
    sup_name = item_data.get("supplier_name") or item_data.get("supplier")
    if isinstance(sup_name, dict):
        sup_name = sup_name.get("name") or sup_name.get("name_ar")
    if not sup_name and "suppliers" in item_data and isinstance(item_data["suppliers"], list) and len(item_data["suppliers"]) > 0:
        first_s = item_data["suppliers"][0]
        if isinstance(first_s, dict):
            sup_name = first_s.get("name") or first_s.get("name_ar")
        elif isinstance(first_s, str):
            sup_name = first_s

    if sup_name and isinstance(sup_name, str) and sup_name.strip():
        clean_s_name = sup_name.strip()
        existing_sup = Supplier.query.filter(
            db.or_(
                db.func.lower(Supplier.name) == clean_s_name.lower(),
                Supplier.code == clean_s_name
            )
        ).first()
        if not existing_sup:
            existing_sup = Supplier(name=clean_s_name)
            db.session.add(existing_sup)
            db.session.flush()

        link = ItemSupplier.query.filter_by(item_id=item.id, supplier_id=existing_sup.id).first()
        if not link:
            link = ItemSupplier(
                item_id=item.id,
                supplier_id=existing_sup.id,
                order_unit=item.storage_unit,
                cost_per_order_unit=item.cost
            )
            db.session.add(link)
            db.session.flush()

    # تحديد ما إذا كان الصنف مربوطاً بمورد أم لا
    suppliers_list = [assoc.supplier for assoc in item.item_suppliers if assoc.supplier]
    has_supplier = len(suppliers_list) > 0
    supplier_display = "، ".join([s.name for s in suppliers_list]) if has_supplier else None

    if is_new and trigger_notification:
        item_display_name = name_ar or name or sku
        if has_supplier:
            sup_msg = f"🤝 المورد: {supplier_display} (مربوط بنجاح ✅)"
            title_tag = "مربوط بمورد ✅"
        else:
            sup_msg = "⚠️ حالة المورد: غير مربوط بأي مورد حالياً (يلزم تعيين مورد للصنف)"
            title_tag = "غير مربوط بمورد ⚠️"

        notif = Notification(
            title=f"تمت إضافة مادة مخزون جديدة من فوديكس ({title_tag}) 📦",
            message=f"تم استلام الصنف الجديد '{item_display_name}' (كود SKU: {sku}) من فوديكس بوحدة تخزين '{storage_unit or 'قطعة'}' وتكلفة {cost:.2f} ر.س. {sup_msg}",
            type="new_inventory_item",
            item_sku=sku,
            related_id=item.id,
            has_supplier=has_supplier,
            supplier_name=supplier_display,
            is_read=False,
            created_at=datetime.utcnow()
        )
        db.session.add(notif)

    return item, is_new, "تمت المعالجة بنجاح"


# -----------------------------------------------------------------------------
# مزامنة البيانات التأسيسية: أصناف المخزون الخام
# -----------------------------------------------------------------------------
def sync_inventory_items_from_foodics(token=None, base_url=None):
    token = token or SystemSetting.get_val("foodics_token")
    base_url = (base_url or SystemSetting.get_val("foodics_base_url") or "https://api-sandbox.foodics.com/v5").rstrip("/")
    if not token:
        return False, "رمز الدخول (Token) غير متوفر."

    headers = dict(REQUIRED_HEADERS_BASE)
    headers["Authorization"] = f"Bearer {token.strip()}"

    try:
        page = 1
        matched_count = 0
        new_count = 0
        total_fetched = 0
        new_item_names = []

        while page <= 10:  # حد أمان لقراءة حتى 500 صنف
            res = requests.get(f"{base_url}/inventory_items?page={page}", headers=headers, timeout=20)
            if res.status_code != 200:
                if page == 1:
                    return False, f"فشل جلب أصناف المخزون (كود {res.status_code}): {res.text[:120]}"
                break
            data = res.json()
            items = data.get("data", [])
            if not items:
                break
            total_fetched += len(items)

            for fi in items:
                item_obj, is_new, _ = process_foodics_item_payload(fi, trigger_notification=True)
                if is_new:
                    new_count += 1
                    if item_obj:
                        new_item_names.append(item_obj.name_ar or item_obj.name_en or item_obj.sku)
                elif item_obj:
                    matched_count += 1

            meta = data.get("meta", {})
            last_page = meta.get("last_page", page)
            if page >= last_page:
                break
            page += 1

        db.session.commit()

        if new_count > 0:
            sample_names = "، ".join(new_item_names[:4])
            if len(new_item_names) > 4:
                sample_names += f" و{len(new_item_names) - 4} أصناف أخرى"
            msg = f"تم فحص {total_fetched} صنف من فوديكس: تم ربط ومطابقة {matched_count} صنف، وإضافة {new_count} صنف جديد ({sample_names}) مع إشعار للمخزون."
        else:
            msg = f"تم فحص {total_fetched} صنف من فوديكس: تم ربط ومطابقة {matched_count} صنف، ولم يتم العثور على أصناف جديدة غير مضافة."

        return True, msg
    except Exception as exc:
        db.session.rollback()
        return False, f"خطأ أثناء مزامنة أصناف المخزون: {str(exc)}"


# -----------------------------------------------------------------------------
# جلب قائمة فروع ومستودعات فوديكس للاختيار والمطابقة اليدوية
# -----------------------------------------------------------------------------
def get_foodics_locations_remote(token=None, base_url=None):
    token = token or SystemSetting.get_val("foodics_token")
    base_url = (base_url or SystemSetting.get_val("foodics_base_url") or "https://api-sandbox.foodics.com/v5").rstrip("/")
    if not token:
        return []
    headers = dict(REQUIRED_HEADERS_BASE)
    headers["Authorization"] = f"Bearer {token.strip()}"
    results = []
    try:
        r = requests.get(f"{base_url}/branches", headers=headers, timeout=10)
        if r.status_code == 200:
            for b in r.json().get("data", []):
                results.append({
                    "id": str(b.get("id")),
                    "name": b.get("name") or "",
                    "name_localized": b.get("name_localized") or b.get("name") or "",
                    "type_label": "فرع (Branch)"
                })
        w = requests.get(f"{base_url}/warehouses", headers=headers, timeout=10)
        if w.status_code == 200:
            for wh in w.json().get("data", []):
                results.append({
                    "id": str(wh.get("id")),
                    "name": wh.get("name") or "",
                    "name_localized": wh.get("name_localized") or wh.get("name") or "",
                    "type_label": "مستودع (Warehouse)"
                })
    except Exception:
        pass
    return results


# -----------------------------------------------------------------------------
# مزامنة البيانات التأسيسية: الموردين (مزامنة ثنائية: استيراد وتصدير)
# -----------------------------------------------------------------------------
def sync_suppliers_from_foodics(token=None, base_url=None):
    token = token or SystemSetting.get_val("foodics_token")
    base_url = (base_url or SystemSetting.get_val("foodics_base_url") or "https://api-sandbox.foodics.com/v5").rstrip("/")
    if not token:
        return False, "رمز الدخول (Token) غير متوفر."

    headers = dict(REQUIRED_HEADERS_BASE)
    headers["Authorization"] = f"Bearer {token.strip()}"

    try:
        # 1. جلب الموردين المسجلين في فوديكس ومطابقتهم
        res = requests.get(f"{base_url}/suppliers", headers=headers, timeout=15)
        foodics_suppliers = []
        if res.status_code == 200:
            foodics_suppliers = res.json().get("data", [])

        synced_count = 0
        new_from_foodics = 0

        for s in foodics_suppliers:
            s_id = str(s.get("id"))
            s_name = (s.get("name") or "").strip()
            s_code = s.get("code")

            sup = Supplier.query.filter_by(foodics_id=s_id).first()
            if not sup and s_name:
                sup = Supplier.query.filter_by(name=s_name).first()

            if sup:
                sup.foodics_id = s_id
                if s_code:
                    sup.code = s_code
                synced_count += 1
            elif s_name:
                new_sup = Supplier(
                    foodics_id=s_id,
                    name=s_name,
                    code=s_code,
                    phone=s.get("phone"),
                    email=s.get("email")
                )
                db.session.add(new_sup)
                new_from_foodics += 1

        db.session.commit()

        # 2. تصدير الموردين المحليين غير المربوطين إلى فوديكس
        unmapped_local = Supplier.query.filter(Supplier.foodics_id.is_(None)).all()
        exported_count = 0

        for loc_sup in unmapped_local:
            if not loc_sup.name:
                continue
            payload = {
                "name": loc_sup.name,
                "code": loc_sup.code or None,
                "phone": loc_sup.phone or None,
                "email": loc_sup.email or None
            }
            try:
                c_res = requests.post(f"{base_url}/suppliers", json=payload, headers=headers, timeout=12)
                if c_res.status_code in (200, 201):
                    new_f_id = c_res.json().get("data", {}).get("id")
                    if new_f_id:
                        loc_sup.foodics_id = str(new_f_id)
                        exported_count += 1
                elif c_res.status_code == 429:
                    time.sleep(2)
            except Exception:
                pass

        db.session.commit()
        msg = f"تمت مزامنة الموردين: مطابقة {synced_count} مورد، وتصدير {exported_count} مورد محلي إلى فوديكس بنجاح!"
        return True, msg
    except Exception as exc:
        db.session.rollback()
        return False, f"خطأ أثناء مزامنة الموردين: {str(exc)}"


def resolve_item_foodics_id(item, base_url, headers):
    """يضمن إرجاع Foodics ID صالح للصنف، ويبحث عنه برقم SKU في فوديكس تلقائياً إذا لم يكن مربوطاً."""
    if not item:
        return None
    if item.foodics_id:
        return item.foodics_id
    if item.sku and requests:
        try:
            sr = requests.get(f"{base_url}/inventory_items?filter[sku]={item.sku}", headers=headers, timeout=8)
            if sr.status_code == 200:
                found_items = sr.json().get("data", [])
                if found_items:
                    item.foodics_id = str(found_items[0].get("id"))
                    db.session.commit()
                    return item.foodics_id
        except Exception:
            pass
    return None


def resolve_supplier_foodics_id(supplier_name, base_url, headers):
    """يطابق المورد ويصدره فوراً إلى فوديكس إذا لم يكن له معرّف."""
    if not supplier_name:
        return None
    try:
        from app import find_supplier_by_name
        sup = find_supplier_by_name(supplier_name)
    except Exception:
        sup = Supplier.query.filter_by(name=supplier_name).first()

    if not sup:
        return None
    if sup.foodics_id:
        return sup.foodics_id

    if requests:
        try:
            c_res = requests.post(f"{base_url}/suppliers", json={
                "name": sup.name,
                "code": sup.code or None,
                "phone": sup.phone or None,
                "email": sup.email or None
            }, headers=headers, timeout=10)
            if c_res.status_code in (200, 201):
                sup.foodics_id = str(c_res.json().get("data", {}).get("id"))
                db.session.commit()
                return sup.foodics_id
        except Exception:
            pass
    return sup.foodics_id


# -----------------------------------------------------------------------------
# ترحيل العمليات والحركات المخزنية الفعلي (Transactional Integration)
# -----------------------------------------------------------------------------
def call_foodics_api(source_type, source_obj):
    """
    يستدعي نقاط نهاية Foodics API v5 الرسمية:
    - purchase: POST /v5/purchase_orders
    - transfer: POST /v5/transfer_orders
    - count: POST /v5/inventory_counts
    """
    blocked, remaining_seconds = is_sync_temporarily_blocked()
    if blocked:
        return False, None, "BLOCKED"

    token = SystemSetting.get_val("foodics_token") or os.environ.get("FOODICS_API_TOKEN")
    base_url = (SystemSetting.get_val("foodics_base_url") or "https://api-sandbox.foodics.com/v5").rstrip("/")
    sync_enabled = SystemSetting.get_val("foodics_sync_enabled", "0") == "1"

    if not sync_enabled:
        if SystemSetting.get_val("foodics_mock_mode", "0") == "1":
            return mock_foodics_api_call(source_type, source_obj)
        return False, None, "الربط مع فوديكس معطل حالياً — يرجى تفعيل المزامنة في إعدادات فوديكس أولاً."

    if not token or not token.strip():
        if SystemSetting.get_val("foodics_mock_mode", "0") == "1":
            return mock_foodics_api_call(source_type, source_obj)
        return False, None, "رمز الدخول (Token) الخاص بفوديكس غير مدخل — يرجى إدخال وحفظ الرمز في إعدادات فوديكس."

    headers = dict(REQUIRED_HEADERS_BASE)
    headers["Authorization"] = f"Bearer {token.strip()}"

    try:
        # استخراج معرف المنشئ (creator_id) المطلوب إلزامياً في فوديكس v5
        creator_id = SystemSetting.get_val("foodics_creator_id")
        if not creator_id:
            try:
                whoami_res = requests.get(f"{base_url}/whoami", headers=headers, timeout=8)
                if whoami_res.status_code == 200:
                    w_data = whoami_res.json().get("data", {})
                    creator_id = w_data.get("user", {}).get("id") or w_data.get("business", {}).get("owner_id")
                    if creator_id:
                        SystemSetting.set_val("foodics_creator_id", creator_id)
            except Exception:
                pass

        if not creator_id:
            return False, None, "تعذّر جلب معرّف المستخدم (creator_id) من فوديكس — تحقق من صلاحيات الرمز عبر فحص الاتصال."

        if source_type == "purchase":
            # مطابقة الفرع المستلم
            branch_id = source_obj.location.foodics_id if source_obj.location else None
            if not branch_id:
                return False, None, "الفرع أو المستودع المستلم غير مربوط بفوديكس — يرجى الضغط على 'مزامنة الفروع' أولاً."

            # مطابقة المورد بمرونة وتصديره تلقائياً إذا لزم
            supplier_id = resolve_supplier_foodics_id(source_obj.supplier_name, base_url, headers)

            # دمج الأصناف المكررة ومطابقة معرّفات فوديكس
            items_map = {}
            for line in source_obj.lines:
                if not line.item:
                    continue
                item_f_id = resolve_item_foodics_id(line.item, base_url, headers)
                if not item_f_id:
                    return False, None, f"الصنف '{line.item.name_ar}' (كود: {line.item.sku}) غير معرّف في حساب فوديكس — يرجى مزامنة الأصناف."
                qty = float(line.quantity or 0)
                cost = float(line.unit_cost or 0)
                if item_f_id in items_map:
                    items_map[item_f_id]["quantity"] += qty
                else:
                    items_map[item_f_id] = {
                        "id": item_f_id,
                        "quantity": qty,
                        "cost": cost,
                    }

            payload = {
                "branch_id": branch_id,
                "notes": f"سند استلام #{source_obj.id} - فاتورة {source_obj.invoice_number or '-'}",
                "status": 1,  # Draft في فوديكس ليتيح المراجعة
                "items": list(items_map.values()),
            }
            if creator_id:
                payload["creator_id"] = creator_id
            if supplier_id:
                payload["supplier_id"] = supplier_id

            res = requests.post(f"{base_url}/purchase_orders", json=payload, headers=headers, timeout=18)

        elif source_type == "transfer":
            from_branch_id = source_obj.from_location.foodics_id if source_obj.from_location else None
            to_branch_id = source_obj.to_location.foodics_id if source_obj.to_location else None

            if not from_branch_id or not to_branch_id:
                return False, None, "أحد مواقع التحويل (المُرسل أو المُستلم) غير مربوط بفوديكس — يرجى مزامنة الفروع."

            # دمج الأصناف المكررة
            items_map = {}
            for line in source_obj.lines:
                if not line.item:
                    continue
                item_f_id = resolve_item_foodics_id(line.item, base_url, headers)
                if not item_f_id:
                    return False, None, f"الصنف '{line.item.name_ar}' (كود: {line.item.sku}) غير معرّف في حساب فوديكس."
                qty = float(line.qty_received if line.qty_received is not None else line.qty_sent or 0)
                if item_f_id in items_map:
                    items_map[item_f_id]["quantity"] += qty
                else:
                    items_map[item_f_id] = {
                        "id": item_f_id,
                        "quantity": qty,
                    }

            # فوديكس API v5 في /transfer_orders تشترط warehouse_id و branch_id
            from_type = getattr(source_obj.from_location, "type", "")
            to_type = getattr(source_obj.to_location, "type", "")
            if from_type == "warehouse":
                warehouse_id = from_branch_id
                branch_id = to_branch_id
            elif to_type == "warehouse":
                warehouse_id = to_branch_id
                branch_id = from_branch_id
            else:
                warehouse_id = from_branch_id
                branch_id = to_branch_id

            payload = {
                "warehouse_id": warehouse_id,
                "branch_id": branch_id,
                "status": 1,  # Draft
                "notes": f"أمر تحويل مواد #{source_obj.id}",
                "items": list(items_map.values()),
            }
            if creator_id:
                payload["creator_id"] = creator_id

            res = requests.post(f"{base_url}/transfer_orders", json=payload, headers=headers, timeout=18)

        elif source_type == "new_item":
            payload = {
                "sku": source_obj.foodics_sku,
                "name": source_obj.name_ar,
                "name_localized": source_obj.name_ar,
                "storage_unit": source_obj.unit,
                "cost": source_obj.estimated_cost,
            }
            res = requests.post(f"{base_url}/inventory_items", json=payload, headers=headers, timeout=15)

        elif source_type == "count":
            branch_id = source_obj.location.foodics_id if source_obj.location else None
            if not branch_id:
                return False, None, "الفرع أو المستودع الخاص بجلسة الجرد غير مربوط بفوديكس — يرجى مزامنة الفروع أولاً."

            items_map = {}
            for line in source_obj.lines:
                if not line.item:
                    continue
                item_f_id = resolve_item_foodics_id(line.item, base_url, headers)
                if not item_f_id:
                    return False, None, f"الصنف '{line.item.name_ar}' (كود: {line.item.sku}) غير معرّف في حساب فوديكس — يرجى مزامنة الأصناف."
                qty = float(line.counted_quantity if line.counted_quantity is not None else 0.0)
                if item_f_id in items_map:
                    items_map[item_f_id]["quantity"] += qty
                else:
                    items_map[item_f_id] = {
                        "id": item_f_id,
                        "quantity": qty,
                    }

            business_date = source_obj.count_date.strftime("%Y-%m-%d") if source_obj.count_date else datetime.utcnow().strftime("%Y-%m-%d")
            payload = {
                "branch_id": branch_id,
                "business_date": business_date,
                "notes": f"جلسة جرد دوري #{source_obj.id}",
                "status": 1,  # 1 = Draft
                "creator_id": creator_id,
                "items": list(items_map.values()),
            }

            res = requests.post(f"{base_url}/inventory_counts", json=payload, headers=headers, timeout=20)

        else:
            return False, None, "UNKNOWN_SOURCE_TYPE"

        remaining = res.headers.get("X-RateLimit-Remaining")
        if remaining is not None:
            SystemSetting.set_val("foodics_rate_remaining", remaining)
            if int(remaining) <= 5:
                time.sleep(1.5)

        if res.status_code in (200, 201):
            data = res.json()
            ref = (
                data.get("data", {}).get("reference")
                or data.get("data", {}).get("id")
                or data.get("reference")
                or f"FD-{source_type.upper()}-{source_obj.id}"
            )
            return True, {"reference": ref}, None

        if res.status_code == 401:
            disable_sync_due_to_auth_failure(f"401 عند ترحيل {source_type}#{source_obj.id}")
            return False, None, "UNAUTHORIZED"

        if res.status_code == 403:
            return False, None, "FORBIDDEN"

        if res.status_code == 429:
            retry_after = int(res.headers.get("retry-after", 60))
            set_temporary_block(retry_after)
            return False, None, "RATE_LIMITED"

        if res.status_code >= 500:
            return False, None, "SERVER_ERROR"

        # محاولة تعافي ذاتية فورية إذا كانت المعرفات قديمة أو غير صالحة في حساب فوديكس الحالي
        if res.status_code == 422 and "items." in res.text and "is invalid" in res.text and not getattr(source_obj, "_retried_auto_healing", False):
            setattr(source_obj, "_retried_auto_healing", True)
            repaired_count = 0
            for line in getattr(source_obj, "lines", []):
                if line.item and line.item.sku:
                    line.item.foodics_id = None
                    fresh_id = resolve_item_foodics_id(line.item, base_url, headers)
                    if fresh_id:
                        repaired_count += 1
            if repaired_count > 0:
                return call_foodics_api(source_type, source_obj)

        try:
            err_data = res.json()
            if "errors" in err_data and isinstance(err_data["errors"], dict):
                error_lines = []
                for field, msgs in err_data["errors"].items():
                    if isinstance(msgs, list):
                        error_lines.append(f"{field}: {', '.join(msgs)}")
                    else:
                        error_lines.append(f"{field}: {msgs}")
                raw_err = "; ".join(error_lines)
            else:
                raw_err = err_data.get("message") or str(err_data)
        except Exception:
            raw_err = f"HTTP {res.status_code}: {res.text[:120]}"
        return False, None, raw_err

    except Exception as exc:
        return False, None, f"CONNECTION_ERROR: {str(exc)}"


def mock_foodics_api_call(source_type, source_obj):
    time.sleep(0.2)
    if random.random() < 0.90:
        prefix = "FD-COUNT" if source_type == "count" else "FD-MOCK"
        return True, {"reference": f"{prefix}-{uuid.uuid4().hex[:8].upper()}"}, None
    error_code = random.choice([
        "ITEM_NOT_FOUND_IN_BRANCH", "UNIT_MISMATCH", "RATE_LIMITED",
        "SERVER_ERROR", "VALIDATION_ERROR",
    ])
    return False, None, error_code


def process_job(job):
    blocked, remaining_seconds = is_sync_temporarily_blocked()
    if blocked:
        job.status = "retrying"
        db.session.commit()
        return False

    job.status = "processing"
    db.session.commit()

    source_obj = None
    if job.source_type == "purchase":
        source_obj = db.session.get(PurchaseTransaction, job.source_id)
    elif job.source_type == "transfer":
        source_obj = db.session.get(TransferOrder, job.source_id)
    elif job.source_type == "count":
        source_obj = db.session.get(CountSession, job.source_id)
    elif job.source_type == "new_item":
        source_obj = db.session.get(NewItemRequest, job.source_id)

    if source_obj is None:
        job.status = "failed"
        db.session.commit()
        return False

    success, result, error_code = call_foodics_api(job.source_type, source_obj)

    if success:
        job.status = "success"
        job.processed_at = datetime.utcnow()
        if hasattr(source_obj, "status"):
            source_obj.status = "posted"
        if hasattr(source_obj, "foodics_reference"):
            source_obj.foodics_reference = result["reference"]
        log = SyncLog(queue_id=job.id, api_status="success", raw_error=None, translated_error_ar="تم الترحيل بنجاح")
        db.session.add(log)
        db.session.commit()
        return True

    if error_code in ("UNAUTHORIZED", "FORBIDDEN", "BLOCKED"):
        job.status = "failed"
        if hasattr(source_obj, "status"):
            source_obj.status = "failed"
        translated = translate_foodics_error(error_code)
        log = SyncLog(queue_id=job.id, api_status="failed", raw_error=error_code, translated_error_ar=translated)
        db.session.add(log)
        db.session.commit()
        return False

    job.retry_count += 1
    translated = translate_foodics_error(error_code)

    if job.retry_count < 3 and error_code in ("RATE_LIMITED", "SERVER_ERROR"):
        job.status = "retrying"
        db.session.commit()
        time.sleep(min(2 ** job.retry_count, 8))
        return process_job(job)

    job.status = "failed"
    if hasattr(source_obj, "status"):
        source_obj.status = "failed"
    log = SyncLog(
        queue_id=job.id,
        api_status="failed",
        raw_error=error_code,
        translated_error_ar=translated,
    )
    db.session.add(log)
    db.session.commit()
    return False


def process_all_queued():
    blocked, remaining_seconds = is_sync_temporarily_blocked()
    if blocked:
        return [("BLOCKED", False)]

    jobs = SyncQueue.query.filter(SyncQueue.status.in_(["queued", "retrying"])).all()
    results = []
    for idx, job in enumerate(jobs, start=1):
        ok = process_job(job)
        results.append((job.id, ok))
        if idx % FOODICS_RATE_LIMIT_PER_MINUTE == 0:
            time.sleep(60)
    return results
