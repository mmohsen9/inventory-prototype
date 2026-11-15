"""

طابور الترحيل الخلفي (Background Sync Queue) + الربط مع Foodics API v5 + أدوات مزامنة الماستر داتا

مبني بالكامل على التوثيق الرسمي لـ Foodics Core API v5:

- Base URLs: Production (https://api.foodics.com/v5)

- Headers: Authorization: Bearer {token}, Accept: application/json, Content-Type: application/json

- Scopes المدعومة:

    - general.read: /branches, /warehouses, /whoami

    - inventory.transactions.read/write: /purchases, /transfers, /inventory_adjustments, /inventory_counts

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

from datetime import datetime, date, timedelta

from models import (

    db, SyncQueue, SyncLog, PurchaseTransaction, PurchaseLine, TransferOrder, NewItemRequest, CountSession, SystemSetting,

    Location, Item, Supplier, Notification, ItemSupplier, StockMovement, record_stock_movement

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

    if "items must be linked to supplier" in err_str.lower():

        return "أصناف السند غير مرتبطة بالمورد في فوديكس — يرجى التحقق من ربط المورد بالأصناف"

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

        base_url = SystemSetting.get_val("foodics_base_url", "https://api.foodics.com/v5")

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

    base_url = (base_url or SystemSetting.get_val("foodics_base_url") or "https://api.foodics.com/v5").rstrip("/")

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

    وإضافته إلى جدول Items وتوليد إشعار فوري في النظام مع دعم ربط عدة موردين (Multi-Supplier)

    واستبعاد الأصناف المحذوفة في فوديكس تلقائياً.

    """

    if not item_data or not isinstance(item_data, dict):

        return None, False, "بيانات الصنف غير صالحة"



    # فحص ما إذا كان الصنف محذوفاً في فوديكس

    if item_data.get("deleted_at") is not None:

        f_id = str(item_data.get("id") or "").strip()

        sku = (item_data.get("sku") or "").strip()

        del_item = None

        if f_id:

            del_item = Item.query.filter_by(foodics_id=f_id).first()

        if not del_item and sku:

            del_item = Item.query.filter_by(sku=sku).first()

        if del_item:

            del_item.sync_status = "deleted_in_foodics"

            # حذف إشعارات هذا الصنف المحذوف

            Notification.query.filter_by(item_sku=del_item.sku, type="new_inventory_item").delete()

        elif sku:

            Notification.query.filter_by(item_sku=sku, type="new_inventory_item").delete()

        return del_item, False, "الصنف محذوف في فوديكس"



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
        sku = barcode or (f"FD-{f_id[:8].upper()}" if f_id else None)

    if not sku:
        return None, False, "رمز الصنف (SKU) غير موجود"

    item = None
    if f_id:
        item = Item.query.filter_by(foodics_id=f_id).first()
    if not item and sku:
        item = Item.query.filter(
            db.or_(
                Item.sku == sku,
                db.func.lower(Item.sku) == sku.lower(),
                Item.barcode == sku,
                ((Item.barcode == barcode) if barcode else False)
            )
        ).first()
    if not item and (name_ar or name):
        clean_ar = (name_ar or "").strip()
        clean_en = (name or "").strip()
        item = Item.query.filter(
            db.or_(
                (db.func.lower(Item.name_ar) == clean_ar.lower()) if clean_ar else False,
                (db.func.lower(Item.name_en) == clean_en.lower()) if clean_en else False
            )
        ).first()



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



    # -------------------------------------------------------------------------

    # فحص وربط جميع الموردين المقترنين بالصنف (Multi-Supplier Support)

    # -------------------------------------------------------------------------

    suppliers_raw = []

    if "suppliers" in item_data and isinstance(item_data["suppliers"], list):

        suppliers_raw.extend(item_data["suppliers"])

    elif item_data.get("supplier_name") or item_data.get("supplier"):

        single_s = item_data.get("supplier_name") or item_data.get("supplier")

        if isinstance(single_s, dict):

            suppliers_raw.append(single_s)

        elif isinstance(single_s, str) and single_s.strip():

            suppliers_raw.append({"name": single_s.strip()})



    for s_entry in suppliers_raw:

        if isinstance(s_entry, dict):

            s_name = (s_entry.get("name") or s_entry.get("name_ar") or "").strip()

            s_code = (s_entry.get("code") or "").strip() or None

            s_foodics_id = s_entry.get("id")

            pivot = s_entry.get("pivot") or {}

            p_order_unit = pivot.get("order_unit") or item.storage_unit or "حبة"

            try:

                p_factor = float(pivot.get("order_to_storage_factor") or 1.0)

            except (ValueError, TypeError):

                p_factor = 1.0

            try:

                p_cost = float(pivot.get("cost") if pivot.get("cost") is not None else (item.cost or 0.0))

            except (ValueError, TypeError):

                p_cost = float(item.cost or 0.0)

        elif isinstance(s_entry, str):

            s_name = s_entry.strip()

            s_code = None

            s_foodics_id = None

            p_order_unit = item.storage_unit or "حبة"

            p_factor = 1.0

            p_cost = float(item.cost or 0.0)

        else:

            continue



        if not s_name:

            continue



        existing_sup = None

        if s_foodics_id:

            existing_sup = Supplier.query.filter_by(foodics_id=str(s_foodics_id)).first()

        if not existing_sup:

            existing_sup = Supplier.query.filter(

                db.or_(

                    db.func.lower(Supplier.name) == s_name.lower(),

                    Supplier.code == (s_code or s_name)

                )

            ).first()



        if not existing_sup:

            existing_sup = Supplier(

                name=s_name,

                code=s_code,

                foodics_id=str(s_foodics_id) if s_foodics_id else None

            )

            db.session.add(existing_sup)

            db.session.flush()

        else:

            if s_foodics_id and not existing_sup.foodics_id:

                existing_sup.foodics_id = str(s_foodics_id)

            if s_code and not existing_sup.code:

                existing_sup.code = s_code



        link = ItemSupplier.query.filter_by(item_id=item.id, supplier_id=existing_sup.id).first()

        if not link:

            link = ItemSupplier(

                item_id=item.id,

                supplier_id=existing_sup.id,

                order_unit=p_order_unit,

                order_to_storage=p_factor,

                cost_per_order_unit=p_cost,

                item_supplier_code=s_code

            )

            db.session.add(link)

            db.session.flush()

        else:

            if p_order_unit:

                link.order_unit = p_order_unit

            if p_factor:

                link.order_to_storage = p_factor

            if p_cost is not None and p_cost > 0:

                link.cost_per_order_unit = p_cost



    # تحديد قائمة الموردين المرتبطين

    suppliers_list = [assoc.supplier for assoc in item.item_suppliers if assoc.supplier]

    has_supplier = len(suppliers_list) > 0

    supplier_display = "، ".join([s.name for s in suppliers_list]) if has_supplier else None



    # إذا كان الصنف غير جديد ولكنه الآن رُبط بمورد، نقوم بتحديث إشعاراته السابقة

    if not is_new and has_supplier:

        old_notifs = Notification.query.filter_by(item_sku=sku, has_supplier=False).all()

        for on in old_notifs:

            on.has_supplier = True

            on.supplier_name = supplier_display

            on.title = f"تم ربط الصنف بالمورد بنجاح ({supplier_display}) 🤝"

            on.is_read = True



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

# تنظيف إشعارات الأصناف المحذوفة في فوديكس

# -----------------------------------------------------------------------------

def cleanup_foodics_deleted_items(token=None, base_url=None):
    """
    فحص الأصناف المحذوفة في حساب فوديكس وإلغاء/حذف أي إشعارات سابقة مرتبطة بها.
    """
    token = token or SystemSetting.get_val("foodics_token")
    base_url = (base_url or SystemSetting.get_val("foodics_base_url") or "https://api.foodics.com/v5").rstrip("/")
    if not token:
        return 0

    headers = dict(REQUIRED_HEADERS_BASE)
    headers["Authorization"] = f"Bearer {token.strip()}"

    cleaned_count = 0
    try:
        page = 1
        while page <= 5:  # فحص سريع لصفحات المحذوفات
            res = requests.get(f"{base_url}/inventory_items?filter[is_deleted]=true&page={page}", headers=headers, timeout=10)
            if res.status_code != 200:
                break
            data = res.json()
            deleted_items = data.get("data", [])
            if not deleted_items:
                break

            for dit in deleted_items:
                f_id = dit.get("id")
                sku = dit.get("sku")
                del_item = None
                if f_id:
                    del_item = Item.query.filter_by(foodics_id=f_id).first()
                if not del_item and sku:
                    del_item = Item.query.filter_by(sku=sku).first()

                if del_item:
                    del_item.sync_status = "deleted_in_foodics"
                    deleted_notifs = Notification.query.filter_by(item_sku=del_item.sku, type="new_inventory_item").delete()
                    cleaned_count += deleted_notifs
                elif sku:
                    deleted_notifs = Notification.query.filter_by(item_sku=sku, type="new_inventory_item").delete()
                    cleaned_count += deleted_notifs

            meta = data.get("meta", {})
            if page >= meta.get("last_page", page):
                break
            page += 1

        db.session.commit()
    except Exception:
        db.session.rollback()

    return cleaned_count





# -----------------------------------------------------------------------------

# مزامنة البيانات التأسيسية: أصناف المخزون الخام

# -----------------------------------------------------------------------------

def sync_inventory_items_from_foodics(token=None, base_url=None):

    token = token or SystemSetting.get_val("foodics_token")

    base_url = (base_url or SystemSetting.get_val("foodics_base_url") or "https://api.foodics.com/v5").rstrip("/")

    if not token:

        return False, "رمز الدخول (Token) غير متوفر."



    headers = dict(REQUIRED_HEADERS_BASE)

    headers["Authorization"] = f"Bearer {token.strip()}"



    try:

        # 1. تنظيف مسبق لإشعارات الأصناف المحذوفة في فوديكس

        cleaned_deleted = cleanup_foodics_deleted_items(token, base_url)



        page = 1

        matched_count = 0

        new_count = 0

        total_fetched = 0

        new_item_names = []



        # استعلام الأصناف النشطة فقط غير المحذوفة مع مورديها
        while page <= 30:  # قراءة حتى 1500 صنف نشط تلقائياً

            res = requests.get(f"{base_url}/inventory_items?filter[is_deleted]=false&include=suppliers&page={page}", headers=headers, timeout=20)

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



        clean_note = f" (تم تنظيف {cleaned_deleted} إشعار لأصناف محذوفة في فوديكس)" if cleaned_deleted > 0 else ""

        if new_count > 0:

            sample_names = "، ".join(new_item_names[:4])

            if len(new_item_names) > 4:

                sample_names += f" و{len(new_item_names) - 4} أصناف أخرى"

            msg = f"تم فحص {total_fetched} صنف نشط من فوديكس: تم ربط ومطابقة {matched_count} صنف، وإضافة {new_count} صنف جديد ({sample_names}) مع إشعار للمخزون.{clean_note}"

        else:

            msg = f"تم فحص {total_fetched} صنف نشط من فوديكس: تم ربط ومطابقة {matched_count} صنف وتحديث بيانات الموردين.{clean_note}"



        return True, msg

    except Exception as exc:

        db.session.rollback()

        return False, f"خطأ أثناء مزامنة أصناف المخزون: {str(exc)}"





# -----------------------------------------------------------------------------

# جلب قائمة فروع ومستودعات فوديكس للاختيار والمطابقة اليدوية

# -----------------------------------------------------------------------------

def get_foodics_locations_remote(token=None, base_url=None):

    token = token or SystemSetting.get_val("foodics_token")

    base_url = (base_url or SystemSetting.get_val("foodics_base_url") or "https://api.foodics.com/v5").rstrip("/")

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

    base_url = (base_url or SystemSetting.get_val("foodics_base_url") or "https://api.foodics.com/v5").rstrip("/")

    if not token:

        return False, "رمز الدخول (Token) غير متوفر."



    headers = dict(REQUIRED_HEADERS_BASE)

    headers["Authorization"] = f"Bearer {token.strip()}"



    try:

        # 1. جلب كافة الموردين المسجلين في فوديكس عبر جميع الصفحات ومطابقتهم

        foodics_suppliers = []

        page = 1

        while True:

            res = requests.get(f"{base_url}/suppliers?page={page}", headers=headers, timeout=15)

            if res.status_code != 200:

                break

            body = res.json()

            data = body.get("data", [])

            if not data:

                break

            foodics_suppliers.extend(data)

            meta = body.get("meta", {})

            last_page = meta.get("last_page", page)

            if page >= last_page:

                break

            page += 1



        synced_count = 0

        new_from_foodics = 0



        # ترتيب الموردين من فوديكس بحيث نأخذ الأقدم أولاً (لحماية المعرفات الأصلية من التكرار)

        foodics_suppliers.sort(key=lambda x: x.get("created_at") or "9999")



        for s in foodics_suppliers:

            s_id = str(s.get("id"))

            s_name = (s.get("name") or "").strip()

            s_code = s.get("code")



            sup = Supplier.query.filter_by(foodics_id=s_id).first()

            if not sup and s_name:

                sup = Supplier.query.filter(db.func.lower(Supplier.name) == s_name.lower()).first()



            if sup:

                if not sup.foodics_id:

                    sup.foodics_id = s_id

                if s_code and not sup.code:

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



        # 2. تصدير الموردين المحليين غير المربوطين إلى فوديكس بعد التحقق من عدم وجودهم مسبقاً

        unmapped_local = Supplier.query.filter(Supplier.foodics_id.is_(None)).all()

        exported_count = 0



        existing_f_names = {(s.get("name") or "").strip().lower(): str(s.get("id")) for s in foodics_suppliers if s.get("name")}



        for loc_sup in unmapped_local:

            if not loc_sup.name:

                continue

            clean_name = loc_sup.name.strip().lower()

            if clean_name in existing_f_names:

                loc_sup.foodics_id = existing_f_names[clean_name]

                synced_count += 1

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

    """يطابق المورد ويبحث عنه في فوديكس أولاً قبل محاولة تصديره، لتجنب إنشاء موردين مكررين."""

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

            # 1. البحث أولاً في فوديكس بالاسم لمنع إنشاء موردين مكررين

            import urllib.parse

            encoded_name = urllib.parse.quote(sup.name)

            s_search = requests.get(f"{base_url}/suppliers?filter[name]={encoded_name}", headers=headers, timeout=10)

            if s_search.status_code == 200:

                f_data = s_search.json().get("data", [])

                if f_data:

                    f_data.sort(key=lambda x: x.get("created_at") or "9999")

                    sup.foodics_id = str(f_data[0].get("id"))

                    db.session.commit()

                    return sup.foodics_id



            # 2. إذا لم يكن مسجلاً نهائياً في فوديكس، نقوم بإنشائه

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

    - purchase: POST /v5/purchases (فاتورة مشتريات مسودة Draft)

    - transfer: POST /v5/transfers (تحويلات المخزون مسودة Draft)

    - count: POST /v5/inventory_counts

    """

    blocked, remaining_seconds = is_sync_temporarily_blocked()

    if blocked:

        return False, None, "BLOCKED"



    token = SystemSetting.get_val("foodics_token") or os.environ.get("FOODICS_API_TOKEN")

    base_url = (SystemSetting.get_val("foodics_base_url") or "https://api.foodics.com/v5").rstrip("/")

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



            # دمج الأصناف المكررة ومطابقة معرّفات فوديكس (مع تصدير إجمالي التكلفة لكل صنف)

            items_map = {}

            for line in source_obj.lines:

                if not line.item:

                    continue

                item_f_id = resolve_item_foodics_id(line.item, base_url, headers)

                if not item_f_id:

                    return False, None, f"الصنف '{line.item.name_ar}' (كود: {line.item.sku}) غير معرّف في حساب فوديكس — يرجى مزامنة الأصناف."

                qty = float(line.quantity or 0)

                cost = float(line.unit_cost or 0)

                line_total = round(float(line.total_cost if line.total_cost is not None else (qty * cost)), 2)

                if item_f_id in items_map:

                    items_map[item_f_id]["quantity"] = round(items_map[item_f_id]["quantity"] + qty, 4)

                    items_map[item_f_id]["cost"] = round(items_map[item_f_id]["cost"] + line_total, 2)

                else:

                    items_map[item_f_id] = {

                        "id": item_f_id,

                        "quantity": round(qty, 4),

                        "cost": line_total,

                    }



            line_notes = [l.notes for l in source_obj.lines if l.notes]

            notes_parts = [f"سند استلام #{source_obj.id} - فاتورة {source_obj.invoice_number or '-'}"]

            if getattr(source_obj, "notes", None):

                notes_parts.append(source_obj.notes)

            if line_notes:

                notes_parts.append(" | ".join(line_notes))

            purchase_notes = " — ".join(notes_parts)[:500]



            target_date = getattr(source_obj, "invoice_date", None) or getattr(source_obj, "transaction_date", None)
            inv_date_str = (
                target_date.strftime("%Y-%m-%d")
                if target_date
                else (
                    source_obj.created_at.strftime("%Y-%m-%d")
                    if getattr(source_obj, "created_at", None)
                    else datetime.utcnow().strftime("%Y-%m-%d")
                )
            )

            # في فوديكس v5: فواتير/عمليات الشراء تُنشأ كمسودات Draft عبر inventory_transactions (type: 1, status: 1)
            # لتظهر مباشرة في لوحة تحكم فوديكس تحت قسم "المخزون -> الشراء" (PUR-...) كمسودة
            payload = {
                "type": 1,
                "status": 1,
                "branch_id": branch_id,
                "supplier_id": supplier_id,
                "creator_id": creator_id,
                "invoice_number": source_obj.invoice_number or f"REC-{source_obj.id}",
                "invoice_date": inv_date_str,
                "business_date": inv_date_str,
                "notes": purchase_notes,
                "items": list(items_map.values()),
            }



            res = requests.post(f"{base_url}/inventory_transactions", json=payload, headers=headers, timeout=18)



        elif source_type == "transfer":

            from_loc = source_obj.from_location

            to_loc = source_obj.to_location

            from_branch_id = from_loc.foodics_id if from_loc else None

            to_branch_id = to_loc.foodics_id if to_loc else None



            if not from_branch_id or not to_branch_id:

                missing_parts = []

                if not from_branch_id:

                    name = from_loc.name_ar if from_loc else "غير محدد"

                    missing_parts.append(f"المُرسِل '{name}'")

                if not to_branch_id:

                    name = to_loc.name_ar if to_loc else "غير محدد"

                    missing_parts.append(f"المُستلِم '{name}'")

                return False, None, f"موقع التحويل ({' و '.join(missing_parts)}) غير مربوط بفوديكس — يرجى مزامنة الفروع أو اختيار موقع مربوط."



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



            business_date = (

                source_obj.transfer_date.strftime("%Y-%m-%d")

                if getattr(source_obj, "transfer_date", None)

                else (

                    source_obj.created_at.strftime("%Y-%m-%d")

                    if getattr(source_obj, "created_at", None)

                    else datetime.utcnow().strftime("%Y-%m-%d")

                )

            )



            # في فوديكس v5: تحويلات المخزون تُنشأ كمسودات Draft عبر inventory_transactions (type: 2, status: 1)

            # لتظهر مباشرة في لوحة تحكم فوديكس تحت قسم "المخزون -> التحويلات" (TRS-...) كتحويل صادر مسودة

            payload = {

                "type": 2,

                "status": 1,

                "branch_id": from_branch_id,

                "other_branch_id": to_branch_id,

                "creator_id": creator_id,

                "business_date": business_date,

                "notes": f"سند صرف مواد #{source_obj.id}",

                "items": list(items_map.values()),

            }



            res = requests.post(f"{base_url}/inventory_transactions", json=payload, headers=headers, timeout=18)



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

    try:
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
    except Exception as exc:
        db.session.rollback()
        job.status = "failed"
        log = SyncLog(
            queue_id=job.id,
            api_status="failed",
            raw_error=str(exc),
            translated_error_ar=f"خطأ أثناء معالجة العملية: {str(exc)}",
        )
        db.session.add(log)
        db.session.commit()
        return False





def process_all_queued():

    blocked, remaining_seconds = is_sync_temporarily_blocked()

    if blocked:

        return [("BLOCKED", False)]



    jobs = SyncQueue.query.filter(SyncQueue.status.in_(["queued", "retrying", "processing"])).all()

    results = []

    for idx, job in enumerate(jobs, start=1):

        ok = process_job(job)

        results.append((job.id, ok))

        if idx % FOODICS_RATE_LIMIT_PER_MINUTE == 0:

            time.sleep(60)

    return results





# -----------------------------------------------------------------------------

# مزامنة استهلاك مواد المخزون من مبيعات نقاط البيع فوديكس (POS Order Consumption)

# -----------------------------------------------------------------------------

def sync_foodics_pos_consumption(date_from=None, date_to=None, branch_id=None, max_pages=5):

    """

    يجلب كميات استهلاك مواد المخزون الناتجة عن مبيعات نقاط البيع (POS) من فوديكس

    (Inventory Transactions of Type 8 - Order Consumption)

    ويقوم بتسجيل حركات خصم المخزون آلياً لمنع وجود حلقة مفرغة بين النظام وفوديكس،

    مع ضمان عدم التكرار (Idempotency) باستخدام معرف الحركة في فوديكس.

    """

    token = SystemSetting.get_val("foodics_token") or os.environ.get("FOODICS_API_TOKEN")

    base_url = (SystemSetting.get_val("foodics_base_url") or "https://api.foodics.com/v5").rstrip("/")

    if not token or not requests:

        return False, "رمز الدخول (Token) الخاص بفوديكس غير متوفر."



    headers = dict(REQUIRED_HEADERS_BASE)

    headers["Authorization"] = f"Bearer {token.strip()}"



    # Type 8 = Order Consumption (استهلاك الطلبات والمبيعات في فوديكس)

    endpoint = f"{base_url}/inventory_transactions?filter[type]=8&sort=-created_at&include=branch,items"



    if branch_id:

        loc = db.session.get(Location, int(branch_id))

        if loc and loc.foodics_id:

            endpoint += f"&filter[branch_id]={loc.foodics_id}"



    if date_from:

        endpoint += f"&filter[business_date_after]={date_from}"

    if date_to:

        endpoint += f"&filter[business_date_before]={date_to}"



    total_transactions_processed = 0

    total_movements_recorded = 0

    total_quantity_consumed = 0.0

    affected_branches = set()



    try:

        page = 1

        while page <= max_pages:

            url = f"{endpoint}&page={page}"

            res = requests.get(url, headers=headers, timeout=18)

            if res.status_code != 200:

                break

            body = res.json()

            transactions = body.get("data", [])

            if not transactions:

                break



            for tx in transactions:

                tx_id = str(tx.get("id"))

                tx_ref = tx.get("reference") or f"OC-{tx_id[:8]}"

                b_date = tx.get("business_date") or ""

                branch_obj = tx.get("branch")

                if not branch_obj:

                    continue



                b_fid = str(branch_obj.get("id"))

                local_loc = Location.query.filter_by(foodics_id=b_fid).first()

                if not local_loc:

                    continue



                # التحقق من عدم التكرار (Idempotency)

                tx_token = f"[{tx_id}]"

                already_recorded = StockMovement.query.filter(

                    StockMovement.notes.like(f"%{tx_token}%")

                ).first()

                if already_recorded:

                    continue



                items_list = tx.get("items", [])

                tx_has_movements = False



                for it in items_list:

                    it_fid = str(it.get("id"))

                    it_sku = it.get("sku")

                    qty = float(it.get("pivot", {}).get("quantity") or 0.0)

                    if qty <= 0:

                        continue



                    local_item = Item.query.filter_by(foodics_id=it_fid).first()

                    if not local_item and it_sku:

                        local_item = Item.query.filter_by(sku=it_sku).first()

                    if not local_item:

                        continue



                    record_stock_movement(

                        item_id=local_item.id,

                        location_id=local_loc.id,

                        movement_type="pos_consumption",

                        quantity_change=-qty,

                        reference_type="foodics_consumption",

                        reference_id=None,

                        notes=f"استهلاك مبيعات فوديكس ({tx_ref}) {tx_token} - تاريخ: {b_date}",

                        user_id=None

                    )

                    total_movements_recorded += 1

                    total_quantity_consumed += qty

                    affected_branches.add(local_loc.name_ar)

                    tx_has_movements = True



                if tx_has_movements:

                    total_transactions_processed += 1



            meta = body.get("meta", {})

            last_page = meta.get("last_page", page)

            if page >= last_page:

                break

            page += 1



        db.session.commit()



        SystemSetting.set_val("foodics_last_consumption_sync", datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"))

        SystemSetting.set_val("foodics_total_consumed_movements", str(total_movements_recorded))



        if total_movements_recorded > 0:

            branches_str = "، ".join(affected_branches)

            msg = f"تمت مزامنة استهلاك المبيعات بنجاح: تم تسجيل {total_movements_recorded} حركة استهلاك بإجمالي {round(total_quantity_consumed, 2)} وحدة مستهلكة عبر ({branches_str})."

        else:

            msg = "تم فحص استهلاك فوديكس: لا توجد حركات استهلاك جديدة غير مسجلة مسبقاً."

        return True, msg



    except Exception as exc:

        db.session.rollback()

        return False, f"حدث خطأ أثناء مزامنة استهلاك مبيعات فوديكس: {str(exc)}"





# ---------------------------------------------------------------------------
# المزامنة الواردة لمشتريات فوديكس والمزامنة الشاملة الموحدة بنقرة واحدة
# ---------------------------------------------------------------------------

def sync_purchases_from_foodics(token=None, base_url=None, max_pages=5):
    """
    استيراد فواتير المشتريات المعتمدة والمغلقة في فوديكس (Status: Closed) تلقائياً:
    - تفحص الفواتير وتستبعد الفواتير الصادرة من برنامجنا (تبدأ بـ PUR-).
    - إذا كانت الفاتورة مدخلة في فوديكس مباشرة ولم تسجل لدينا:
      1. تنشئ سند استلام مشتريات معتمد محلياً PurchaseTransaction.
      2. تطابق المورد والفرع والأصناف ووحدات القياس.
      3. تقيد حركة دخول مخزنية (+) في دفتر الأستاذ وتحدث الرصيد الفعلي في StockBalance.
      4. ترسل إشعاراً بنجاح الاستيراد.
    """
    token = token or SystemSetting.get_val("foodics_token")
    base_url = (base_url or SystemSetting.get_val("foodics_base_url") or "https://api.foodics.com/v5").rstrip("/")

    if SystemSetting.get_val("foodics_mock_mode", "0") == "1":
        return True, "تم فحص فواتير مشتريات فوديكس بنجاح (وضع الاختبار المحاكي: لا توجد فواتير خارجية جديدة)."

    if not token or not requests:
        return False, "رمز الدخول (Token) الخاص بفوديكس غير متوفر."

    headers = dict(REQUIRED_HEADERS_BASE)
    headers["Authorization"] = f"Bearer {token.strip()}"

    imported_count = 0
    skipped_count = 0
    total_qty_imported = 0.0

    try:
        page = 1
        while page <= max_pages:
            url = f"{base_url}/purchases?include=items,supplier,branch&page={page}"
            res = requests.get(url, headers=headers, timeout=18)
            if res.status_code == 401:
                return False, "انتهت صلاحية رمز الدخول (Token) لفوديكس."
            if res.status_code != 200:
                break

            body = res.json()
            data = body.get("data", [])
            if not data:
                break

            for p in data:
                # 1. تحقق من الحالة: فقط الفواتير المغلقة (Closed: status 3) تؤثر على أرصدة المخزون
                status_val = str(p.get("status") or "")
                if status_val not in ("3", "closed", "Closed"):
                    continue

                ref = str(p.get("business_reference") or p.get("reference") or "").strip()
                p_id = str(p.get("id") or "")

                # 2. الفلترة الذكية لمنع التكرار (Deduplication):
                # إذا كانت الفاتورة صادرة من برنامجنا (تبدأ بـ PUR-) نتخطاها فوراً
                if ref.startswith("PUR-"):
                    skipped_count += 1
                    continue

                # التحقق هل تم استيرادها مسبقاً
                existing = PurchaseTransaction.query.filter(
                    (PurchaseTransaction.foodics_reference == p_id) |
                    ((PurchaseTransaction.invoice_number == ref) if ref else False)
                ).first()
                if existing:
                    skipped_count += 1
                    continue

                # 3. مطابقة الفرع أو المستودع المستلم
                branch_obj = p.get("branch") or {}
                b_fid = str(branch_obj.get("id") or "")
                local_loc = None
                if b_fid:
                    local_loc = Location.query.filter_by(foodics_id=b_fid).first()
                if not local_loc:
                    local_loc = Location.query.filter_by(type="warehouse").first() or Location.query.first()
                if not local_loc:
                    continue

                # 4. مطابقة المورد
                sup_obj = p.get("supplier") or {}
                sup_name = str(sup_obj.get("name") or "مورد فوديكس").strip()

                # استخراج تاريخ الفاتورة
                inv_date_str = str(p.get("business_date") or p.get("closed_at") or p.get("created_at") or "")
                inv_date = date.today()
                if inv_date_str:
                    try:
                        inv_date = datetime.fromisoformat(inv_date_str.split("T")[0]).date()
                    except Exception:
                        inv_date = date.today()

                # إنشاء سند المشتريات المعتمد محلياً
                purchase = PurchaseTransaction(
                    location_id=local_loc.id,
                    supplier_name=sup_name,
                    invoice_number=ref or f"FD-PUR-{p_id[:8].upper()}",
                    invoice_date=inv_date,
                    status="approved",
                    foodics_reference=p_id,
                    created_by=1
                )
                db.session.add(purchase)
                db.session.flush()

                # 5. مطابقة الأصناف والكميات وقيد حركات الدخول في دفتر الأستاذ
                items_list = p.get("items", [])
                lines_added = 0

                for itm in items_list:
                    itm_fid = str(itm.get("id") or (itm.get("pivot") or {}).get("inventory_item_id") or "")
                    itm_sku = str(itm.get("sku") or "")

                    local_item = None
                    if itm_fid:
                        local_item = Item.query.filter_by(foodics_id=itm_fid).first()
                    if not local_item and itm_sku:
                        local_item = Item.query.filter_by(sku=itm_sku).first()

                    if not local_item:
                        continue

                    qty = float((itm.get("pivot") or {}).get("quantity") or itm.get("quantity") or 0.0)
                    cost = float((itm.get("pivot") or {}).get("cost") or itm.get("cost") or itm.get("unit_price") or local_item.cost or 0.0)
                    if qty <= 0:
                        continue

                    pline = PurchaseLine(
                        purchase_id=purchase.id,
                        item_id=local_item.id,
                        quantity=qty,
                        unit_cost=cost,
                        notes="مستورد تلقائياً من فوديكس"
                    )
                    db.session.add(pline)

                    # قيد الدخول المخزني الفعلي في دفتر الأستاذ وتحديث رصيد StockBalance
                    record_stock_movement(
                        item_id=local_item.id,
                        location_id=local_loc.id,
                        movement_type="purchase_in",
                        quantity_change=qty,
                        reference_type="purchase",
                        reference_id=purchase.id,
                        notes=f"استيراد تلقائي لمشتريات فوديكس فاتورة {purchase.invoice_number} [{p_id}]"
                    )
                    lines_added += 1
                    total_qty_imported += qty

                if lines_added > 0:
                    imported_count += 1
                    notif = Notification(
                        title="📥 استيراد مشتريات من فوديكس",
                        message=f"تم استيراد فاتورة المشتريات #{purchase.invoice_number} من فوديكس وتحديث مخزون ({local_loc.name_ar}) بـ {lines_added} صنف.",
                        type="foodics_purchase",
                        item_sku=purchase.invoice_number,
                        is_read=False,
                        created_at=datetime.utcnow()
                    )
                    db.session.add(notif)
                else:
                    db.session.rollback()

            db.session.commit()
            meta = body.get("meta", {})
            last_page = meta.get("last_page", page)
            if page >= last_page:
                break
            page += 1

        if imported_count > 0:
            msg = f"تم استيراد {imported_count} فاتورة مشتريات جديدة من فوديكس وتحديث المخزون بنجاح (تم تخطي {skipped_count} فاتورة مسجلة مسبقاً)."
        else:
            msg = f"تم فحص مشتريات فوديكس: لا توجد فواتير خارجية جديدة بحاجة للاستيراد (تم تخطي {skipped_count} فاتورة مسجلة مسبقاً)."
        return True, msg

    except Exception as exc:
        db.session.rollback()
        return False, f"حدث خطأ أثناء مزامنة مشتريات فوديكس: {str(exc)}"


def sync_all_from_foodics(token=None, base_url=None):
    """
    المزامنة الشاملة الكاملة مع فوديكس بنقرة واحدة (One-Click Full Sync):
    1. مزامنة الفروع والمستودعات
    2. مزامنة وتصدير الموردين
    3. مزامنة الأصناف والتكاليف ووحدات القياس
    4. استيراد فواتير المشتريات المغلقة وتحديث المخزون
    5. مزامنة استهلاك المبيعات (POS)
    6. ترحيل المعلقات في طابور المزامنة
    """
    token = token or SystemSetting.get_val("foodics_token")
    base_url = (base_url or SystemSetting.get_val("foodics_base_url") or "https://api.foodics.com/v5").rstrip("/")

    if not token and SystemSetting.get_val("foodics_mock_mode", "0") != "1":
        return False, "رمز الدخول (Token) الخاص بفوديكس غير متوفر — يرجى إدخال وحفظ التوكن أولاً."

    summary = []

    # 1. الفروع والمستودعات
    ok_b, msg_b = sync_branches_from_foodics(token, base_url)
    summary.append(f"📍 الفروع: {msg_b}")

    # 2. الموردين
    ok_s, msg_s = sync_suppliers_from_foodics(token, base_url)
    summary.append(f"🤝 الموردين: {msg_s}")

    # 3. الأصناف ووحدات القياس
    ok_i, msg_i = sync_inventory_items_from_foodics(token, base_url)
    summary.append(f"📦 الأصناف: {msg_i}")

    # 4. مشتريات فوديكس وتحديث المخزون
    ok_p, msg_p = sync_purchases_from_foodics(token, base_url)
    summary.append(f"🛒 المشتريات: {msg_p}")

    # 5. استهلاك مبيعات نقاط البيع
    ok_c, msg_c = sync_foodics_pos_consumption()
    summary.append(f"⚡ استهلاك المبيعات: {msg_c}")

    # 6. ترحيل المعلقات
    queued_res = process_all_queued()
    if isinstance(queued_res, list):
        posted = sum(1 for _, ok in queued_res if ok)
        if posted > 0:
            summary.append(f"🚀 تم ترحيل {posted} حركة معلقة لفوديكس")

    overall_ok = any([ok_b, ok_s, ok_i, ok_p, ok_c])
    full_report = " | ".join(summary)
    return overall_ok, full_report
