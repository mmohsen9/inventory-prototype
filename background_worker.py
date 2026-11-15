"""
وحدة المعالجة الخلفية المستقلة (Background Worker)
لإدارة وتشغيل مهام المزامنة الخارجية (Foodics و Xero وسلة)
دون تجميد طلبات الـ HTTP أو التسبب في انتهاء مهلة الاتصال (Timeout)
مع الحفاظ الكامل على سلامة البيانات وعزل جلسات قواعد البيانات.
"""
import logging
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

logger = logging.getLogger("background_worker")

_executor = None
_lock = threading.Lock()
_flask_app = None

# فحص بيئة الاختبار لتحديد التزامن
is_test_env = (
    os.environ.get("TESTING") == "1"
    or "pytest" in sys.modules
    or any("test" in arg.lower() for arg in sys.argv)
)


def init_background_worker(app, max_workers=2):
    """تهيئة مشغل المهام الخلفية وربطه بتطبيق Flask."""
    global _executor, _flask_app
    with _lock:
        _flask_app = app
        if _executor is None:
            _executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="bg_sync_worker")
            logger.info("Background Worker ThreadPool initialized with %d workers", max_workers)


def get_executor():
    """استرجاع أو إنشاء الـ ThreadPoolExecutor بأمان."""
    global _executor
    if _executor is None:
        with _lock:
            if _executor is None:
                _executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="bg_sync_worker")
    return _executor


def run_async_task(fn, *args, **kwargs):
    """
    تنفيذ دالة كـ Background Task ضمن Application Context الخاص بـ Flask.
    في بيئة الاختبارات، تُنفذ المهمة تزامناً لضمان موثوقية التحقق (Deterministic Tests).
    """
    app = _flask_app

    def task_wrapper():
        if app:
            with app.app_context():
                try:
                    return fn(*args, **kwargs)
                except Exception as exc:
                    logger.error("Background task %s failed: %s", getattr(fn, "__name__", str(fn)), exc, exc_info=True)
                    return None
        else:
            try:
                return fn(*args, **kwargs)
            except Exception as exc:
                logger.error("Background task %s failed: %s", getattr(fn, "__name__", str(fn)), exc, exc_info=True)
                return None

    # في الاختبارات: تنفيذ فوري ومباشر
    if is_test_env:
        return task_wrapper()

    executor = get_executor()
    if executor:
        return executor.submit(task_wrapper)
    return task_wrapper()


def enqueue_foodics_job_async(job_id):
    """ترحيل مهمة طابور فوديكس محددة في الخلفية."""
    def _worker():
        from sync_queue import process_job
        from models import SyncQueue, db
        job = SyncQueue.query.get(job_id)
        if job:
            res = process_job(job)
            db.session.remove()
            return res
        return None
    return run_async_task(_worker)


def enqueue_foodics_all_queued_async():
    """معالجة كافة مهام طابور فوديكس المعلقة في الخلفية دون تعطيل طلب الـ HTTP."""
    def _worker():
        from sync_queue import process_all_queued
        from models import db
        res = process_all_queued()
        db.session.remove()
        return res
    return run_async_task(_worker)


def enqueue_xero_roasted_dispatch_async(dispatch_id):
    """ترحيل سند صرف محمص إلى زيرو في الخلفية."""
    def _worker():
        from coffee_xero import post_roasted_dispatch_to_xero
        from models import db
        res = post_roasted_dispatch_to_xero(dispatch_id)
        db.session.remove()
        return res
    return run_async_task(_worker)


def enqueue_xero_green_issue_async(issue_id):
    """ترحيل سند صرف بن أخضر إلى زيرو في الخلفية."""
    def _worker():
        from coffee_xero import post_green_issue_to_xero
        from models import db
        res = post_green_issue_to_xero(issue_id)
        db.session.remove()
        return res
    return run_async_task(_worker)


def enqueue_xero_roasting_order_async(order_id_or_obj):
    """ترحيل أمر تحميص إلى زيرو في الخلفية."""
    def _worker():
        from coffee_xero import post_roasting_order_to_xero
        from models import db
        res = post_roasting_order_to_xero(order_id_or_obj)
        db.session.remove()
        return res
    return run_async_task(_worker)
