"""
طابور الترحيل الخلفي (Background Sync Queue) + قاموس ترجمة أخطاء Foodics
"""
import uuid
import random
import time
from datetime import datetime
from models import db, SyncQueue, SyncLog, PurchaseTransaction, TransferOrder, NewItemRequest

# قاموس ترجمة الأخطاء الشائعة من Foodics إلى رسائل عربية عملية
ERROR_TRANSLATION_MAP = {
    "ITEM_NOT_FOUND_IN_BRANCH": "الصنف غير معرّف في الفرع المستلم — فعّله في فوديكس ثم أعد الترحيل",
    "UNIT_MISMATCH": "وحدة القياس غير متطابقة مع الصنف في فوديكس — راجع وحدة الصنف وصحّحها",
    "UNAUTHORIZED": "انتهت صلاحية الاتصال مع فوديكس — يلزم تجديد رمز الدخول (Token)",
    "RATE_LIMITED": "عدد الطلبات تجاوز الحد المسموح من فوديكس — سيُعاد الإرسال تلقائياً خلال دقائق",
    "SERVER_ERROR": "خطأ مؤقت من خادم فوديكس — سيُعاد المحاولة تلقائياً",
    "DUPLICATE_REFERENCE": "هذه العملية مرحّلة مسبقاً لفوديكس — لا تُرسل مرة أخرى",
    "VALIDATION_ERROR": "بيانات العملية غير مكتملة أو غير صحيحة (422) — راجع الأصناف والكميات والوحدات",
}


def generate_idempotency_key(source_type, source_id):
    return f"{source_type}-{source_id}-{uuid.uuid4().hex[:12]}"


def enqueue(source_type, source_id):
    """يُستدعى فور اعتماد المحاسب للعملية، يضيفها لطابور الترحيل الخلفي."""
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


def mock_foodics_api_call(source_type, source_obj):
    """
    ============================================================
    نقطة الاستبدال الوحيدة المطلوبة للربط الفعلي مع Foodics API.
    استبدل هذه الدالة باستدعاء requests.post(...) حقيقي بعد استلام
    صلاحيات API من دعم فوديكس (راجع README القسم 5).
    ============================================================
    تُحاكي هذه الدالة استجابة Foodics عشوائياً لأغراض تجربة النموذج فقط:
    - 80% نجاح
    - 20% فشل موزّع على أخطاء شائعة واقعية
    """
    time.sleep(0.3)  # محاكاة زمن استجابة الشبكة
    if random.random() < 0.8:
        return True, {"reference": f"FOODICS-{uuid.uuid4().hex[:8].upper()}"}, None
    error_code = random.choice([
        "ITEM_NOT_FOUND_IN_BRANCH", "UNIT_MISMATCH", "RATE_LIMITED",
        "SERVER_ERROR", "VALIDATION_ERROR",
    ])
    return False, None, error_code


def process_job(job):
    """يعالج بند واحد من الطابور، مع إعادة محاولة تلقائية حتى 3 مرات."""
    job.status = "processing"
    db.session.commit()

    source_obj = None
    if job.source_type == "purchase":
        source_obj = PurchaseTransaction.query.get(job.source_id)
    elif job.source_type == "transfer":
        source_obj = TransferOrder.query.get(job.source_id)
    elif job.source_type == "new_item":
        source_obj = NewItemRequest.query.get(job.source_id)

    if source_obj is None:
        job.status = "failed"
        db.session.commit()
        return

    success, result, error_code = mock_foodics_api_call(job.source_type, source_obj)

    if success:
        job.status = "success"
        job.processed_at = datetime.utcnow()
        source_obj.status = "posted" if hasattr(source_obj, "status") else source_obj.status
        if hasattr(source_obj, "foodics_reference"):
            source_obj.foodics_reference = result["reference"]
        log = SyncLog(queue_id=job.id, api_status="success", raw_error=None, translated_error_ar=None)
        db.session.add(log)
        db.session.commit()
        return True

    # فشل: إعادة محاولة أو تصنيف نهائي
    job.retry_count += 1
    translated = ERROR_TRANSLATION_MAP.get(error_code, "خطأ غير معروف من فوديكس، راجع الدعم الفني")

    if job.retry_count < 3 and error_code in ("RATE_LIMITED", "SERVER_ERROR"):
        job.status = "retrying"
        db.session.commit()
        time.sleep(min(2 ** job.retry_count, 8))  # Exponential backoff مبسّط
        return process_job(job)  # إعادة محاولة فورية داخل نفس الطلب (للنموذج الأولي)

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
    """تُستدعى من مسار /sync/process (زر يدوي في النموذج الأولي، أو Cron في الإنتاج)."""
    jobs = SyncQueue.query.filter(SyncQueue.status.in_(["queued", "retrying"])).all()
    results = []
    for job in jobs:
        ok = process_job(job)
        results.append((job.id, ok))
    return results
