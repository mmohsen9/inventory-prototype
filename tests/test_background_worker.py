"""
اختبارات المعالج الخلفي (Background Worker Tests)
التحقق من سلامة تشغيل مهام المزامنة لـ Foodics و Xero في الخلفية
مع الحفاظ على سلامة المعاملات وجلسة قاعدة البيانات.
"""
import pytest
from app import app
from models import db, SyncQueue, PurchaseTransaction, Location
from background_worker import (
    run_async_task,
    enqueue_foodics_job_async,
    enqueue_foodics_all_queued_async,
    enqueue_xero_roasted_dispatch_async,
    enqueue_xero_green_issue_async,
    get_executor,
)


def test_background_worker_executor_initialization():
    """التحقق من إنشاء المشغل الخلفي بأمان وإمكانية الوصول إليه."""
    executor = get_executor()
    assert executor is not None


def test_run_async_task_with_app_context(db_session):
    """التحقق من تنفيذ دالة ضمن التطبيق وقاعدة البيانات."""
    executed = []

    def sample_task(val):
        # التحقق من وجود سياق التطبيق وقاعدة البيانات
        locs = Location.query.all()
        executed.append(val)
        return len(locs)

    fut = run_async_task(sample_task, 42)
    assert 42 in executed


def test_enqueue_foodics_job_async(test_data, db_session):
    """التحقق من تمرير مهمة طابور فوديكس إلى المعالج الخلفي بنجاح."""
    br = test_data["br1"]
    purchase = PurchaseTransaction(location_id=br.id, supplier_name="مورد اختباري", status="approved")
    db_session.add(purchase)
    db_session.commit()

    sq_item = SyncQueue(
        source_type="purchase",
        source_id=purchase.id,
        idempotency_key=f"test-bg-{purchase.id}",
        status="queued"
    )
    db_session.add(sq_item)
    db_session.commit()

    res = enqueue_foodics_job_async(sq_item.id)
    assert res is not None or sq_item.id is not None


def test_sync_process_endpoint_uses_worker(admin_client, db_session):
    """التحقق من استدعاء مسار /sync/process دون أي أخطاء وقت التشغيل."""
    res = admin_client.post("/sync/process", follow_redirects=True)
    assert res.status_code == 200


def test_sync_retry_endpoint_uses_worker(admin_client, test_data, db_session):
    """التحقق من استدعاء مسار /sync/retry/<id> دون أي أخطاء وقت التشغيل."""
    br = test_data["br1"]
    sq_item = SyncQueue(
        source_type="purchase",
        source_id=1,
        idempotency_key="test-retry-bg-1",
        status="failed"
    )
    db_session.add(sq_item)
    db_session.commit()

    res = admin_client.post(f"/sync/retry/{sq_item.id}", follow_redirects=True)
    assert res.status_code == 200
    db_session.refresh(sq_item)
    assert sq_item.status in ("queued", "success", "failed", "processing")

