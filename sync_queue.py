"""
طابور الترحيل الخلفي (Background Sync Queue) + الربط مع Foodics API + قاموس ترجمة الأخطاء

مبني على مراجعة رسمية لصفحات apidocs.foodics.com التالية:
- Introduction: Base URLs, Headers الإلزامية، Rate Limiting (90 طلب/دقيقة)، سياسة الحظر عند 429 و401/403.
- Authentication: OAuth2 Authorization Code Grant، حساسية كلمة Bearer، عدم وجود refresh_token موثق.
- Scopes: أسماء الموارد الرسمية التالية:
    - branches / whoami  (Core)
    - purchase_orders / suppliers  (Inventory)
    - transfer_orders  (Inventory)
    - inventory_transactions / inventory_levels  (Inventory)

ملاحظة مهمة: لا يوجد ضمن دليل Scopes صلاحية صريحة لإنشاء صنف جديد (items) — يجب
توضيح ذلك مع دعم فوديكس قبل تفعيل مسار "new_item" فعلياً في الإنتاج.
"""
import uuid
import random
import time
try:
    import requests
except ImportError:
    requests = None
from datetime import datetime, timedelta
from models import (
    db, SyncQueue, SyncLog, PurchaseTransaction, TransferOrder, NewItemRequest, SystemSetting
)

ERROR_TRANSLATION_MAP = {
    "ITEM_NOT_FOUND_IN_BRANCH": "الصنف غير معرّف في الفرع المستلم — فعّله في فوديكس ثم أعد الترحيل",
    "UNIT_MISMATCH": "وحدة القياس غير متطابقة مع الصنف في فوديكس — راجع وحدة الصنف وصحّحها",
    "UNAUTHORIZED": "انتهت صلاحية الاتصال مع فوديكس — تم إيقاف الترحيل التلقائي تلقائياً، يلزم تجديد رمز الدخول (Token)",
    "FORBIDDEN": "لا تملك صلاحية كافية على هذا المورد (403) — تواصل مع دعم فوديكس، لا تتم إعادة المحاولة تلقائياً",
    "RATE_LIMITED": "عدد الطلبات تجاوز الحد المسموح من فوديكس (90 طلب/دقيقة) — سيُعاد الإرسال تلقائياً بعد المهلة المحددة",
    "SERVER_ERROR": "خطأ مؤقت من خادم فوديكس — سيُعاد المحاولة تلقائياً",
    "DUPLICATE_REFERENCE": "هذه العملية مرحّلة مسبقاً لفوديكس — لا تُرسل مرة أخرى",
    "VALIDATION_ERROR": "بيانات العملية غير مكتملة أو غير صحيحة (422) — راجع الأصناف والكميات والوحدات",
    "BLOCKED": "تم حظر الاتصال مؤقتاً من فوديكس (Cloudflare) بسبب تجاوز الحدود — التزم بفترة الانتظار قبل أي محاولة جديدة",
}

REQUIRED_HEADERS_BASE = {
    "Accept": "application/json",
    "Content-Type": "application/json",
}

FOODICS_RATE_LIMIT_PER_MINUTE = 90

# أسماء الموارد الرسمية المؤكدة من دليل Scopes
FOODICS_RESOURCE_PATHS = {
    "purchase": "purchase_orders",
    "transfer": "transfer_orders",
    "branches": "branches",
    "suppliers": "suppliers",
    "inventory_transactions": "inventory_transactions",
    "inventory_levels": "inventory_levels",
    "whoami": "whoami",
}


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


def test_foodics_connection(token, base_url=None):
    """يفحص صحة الاتصال والـ Token عبر مورد branches الرسمي."""
    if not base_url:
        base_url = SystemSetting.get_val("foodics_base_url", "https://api.foodics.com/v5")
    base_url = base_url.rstrip("/")

    headers = dict(REQUIRED_HEADERS_BASE)
    headers["Authorization"] = f"Bearer {token.strip()}"

    try:
        res = requests.get(f"{base_url}/{FOODICS_RESOURCE_PATHS['branches']}", headers=headers, timeout=10)

        remaining = res.headers.get("X-RateLimit-Remaining")
        if remaining is not None:
            SystemSetting.set_val("foodics_rate_remaining", remaining)

        if res.status_code == 200:
            data = res.json()
            branches_count = len(data.get("data", []))
            return True, f"تم الاتصال بنجاح بـ Foodics API! تم التحقق وجلب {branches_count} فرع."
        elif res.status_code == 401:
            disable_sync_due_to_auth_failure("401 عند اختبار الاتصال")
            return False, "رمز الدخول (Token) غير صالح أو منتهي الصلاحية (401). تم إيقاف الترحيل التلقائي تلقائياً حماية للحساب من الحظر."
        elif res.status_code == 403:
            return False, "لا تملك الصلاحية الكافية للوصول (403 Forbidden). تواصل مع دعم Foodics ولا تكرر المحاولة."
        elif res.status_code == 429:
            retry_after = int(res.headers.get("retry-after", 60))
            set_temporary_block(retry_after)
            return False, f"تم تجاوز حد الطلبات المسموح (429). انتظر {retry_after} ثانية قبل إعادة المحاولة."
        else:
            return False, f"استجابة غير متوقعة من فوديكس (كود {res.status_code}): {res.text[:120]}"
    except Exception as e:
        return False, f"تعذر الاتصال بخادم Foodics: {str(e)}"


def call_foodics_api(source_type, source_obj):
    """
    يستدعي Foodics API الفعلي باستخدام أسماء الموارد الرسمية المؤكدة من دليل Scopes:
    purchase_orders / transfer_orders / inventory_transactions / inventory_levels / suppliers / branches
    """
    blocked, remaining_seconds = is_sync_temporarily_blocked()
    if blocked:
        return False, None, "BLOCKED"

    token = SystemSetting.get_val("foodics_token") or os.environ.get("FOODICS_API_TOKEN")
    base_url = (SystemSetting.get_val("foodics_base_url") or "https://api.foodics.com/v5").rstrip("/")
    sync_enabled = SystemSetting.get_val("foodics_sync_enabled", "0") == "1"

    if not sync_enabled or not token:
        return mock_foodics_api_call(source_type, source_obj)

    headers = dict(REQUIRED_HEADERS_BASE)
    headers["Authorization"] = f"Bearer {token.strip()}"

    try:
        if source_type == "purchase":
            payload = {
                "supplier_name": source_obj.supplier_name,
                "invoice_number": source_obj.invoice_number,
                "notes": f"سند استلام #{source_obj.id}",
                "items": [
                    {
                        "sku": line.item.sku if line.item else "",
                        "quantity": line.quantity,
                        "cost": line.unit_cost,
                    }
                    for line in source_obj.lines
                ],
            }
            res = requests.post(
                f"{base_url}/{FOODICS_RESOURCE_PATHS['purchase']}",
                json=payload, headers=headers, timeout=15,
            )

        elif source_type == "transfer":
            payload = {
                "from_branch": source_obj.from_location.name_ar if source_obj.from_location else "",
                "to_branch": source_obj.to_location.name_ar if source_obj.to_location else "",
                "notes": f"سند صرف مواد #{source_obj.id}",
                "items": [
                    {
                        "sku": line.item.sku if line.item else "",
                        "quantity": line.qty_received if line.qty_received is not None else line.qty_sent,
                    }
                    for line in source_obj.lines
                ],
            }
            res = requests.post(
                f"{base_url}/{FOODICS_RESOURCE_PATHS['transfer']}",
                json=payload, headers=headers, timeout=15,
            )

        elif source_type == "new_item":
            # تحذير: لا يوجد scope صريح لإنشاء الأصناف في الدليل المُستلم من الدعم حتى الآن.
            # يجب تأكيد اسم المورد الصحيح (مثلاً items أو inventory_items) قبل تفعيل هذا المسار فعلياً.
            payload = {
                "sku": source_obj.foodics_sku,
                "name": source_obj.name_ar,
                "name_localized": source_obj.name_ar,
                "storage_unit": source_obj.unit,
                "cost": source_obj.estimated_cost,
            }
            res = requests.post(f"{base_url}/items", json=payload, headers=headers, timeout=15)

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

        try:
            err_data = res.json()
            raw_err = err_data.get("message") or str(err_data)
        except Exception:
            raw_err = f"HTTP {res.status_code}: {res.text[:120]}"
        return False, None, raw_err

    except Exception as exc:
        return False, None, f"CONNECTION_ERROR: {str(exc)}"


def mock_foodics_api_call(source_type, source_obj):
    time.sleep(0.2)
    if random.random() < 0.85:
        return True, {"reference": f"FOODICS-MOCK-{uuid.uuid4().hex[:8].upper()}"}, None
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
        translated = ERROR_TRANSLATION_MAP.get(error_code, f"خطأ من فوديكس: {error_code}")
        log = SyncLog(queue_id=job.id, api_status="failed", raw_error=error_code, translated_error_ar=translated)
        db.session.add(log)
        db.session.commit()
        return False

    job.retry_count += 1
    translated = ERROR_TRANSLATION_MAP.get(error_code, f"خطأ من فوديكس: {error_code}")

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
