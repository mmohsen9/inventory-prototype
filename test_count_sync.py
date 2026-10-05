"""
اختبارات شاملة لترحيل جلسات الجرد إلى فوديكس (Foodics Inventory Count Sync)
"""
import unittest
import os
import sys

from app import app, db
from models import Location, Item, User, CountSession, CountLine, SyncQueue, SyncLog, StockBalance, SystemSetting
import sync_queue as sq


class TestCountSyncToFoodics(unittest.TestCase):
    def setUp(self):
        app.config["TESTING"] = True
        app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
        self.client = app.test_client()

        with app.app_context():
            db.create_all()

            # إنشاء موقع وفرع مربوط بفوديكس
            branch = Location(
                name_ar="فرع رفاء التجريبي",
                name_en="RAFA Test Branch",
                type="branch",
                foodics_id="branch-uuid-12345"
            )
            db.session.add(branch)

            # فرع غير مربوط بفوديكس للاختبار
            unlinked = Location(
                name_ar="فرع غير مربوط",
                name_en="Unlinked Branch",
                type="branch",
                foodics_id=None
            )
            db.session.add(unlinked)

            # صنف مربوط بفوديكس
            item = Item(
                sku="sk-test-01",
                foodics_id="item-uuid-67890",
                name_ar="بن برازيلي فاخر",
                name_en="Brazilian Coffee",
                storage_unit="kg",
                cost=45.0,
            )
            db.session.add(item)

            # مستخدم محاسب
            accountant = User(
                name="محاسب النظام",
                role="accountant",
                password="123",
                active=True
            )
            db.session.add(accountant)

            db.session.commit()

            self.branch_id = branch.id
            self.unlinked_branch_id = unlinked.id
            self.item_id = item.id
            self.accountant_id = accountant.id

            # إعدادات النظام للمزامنة
            SystemSetting.set_val("foodics_sync_enabled", "1")
            SystemSetting.set_val("foodics_token", "test-token")
            SystemSetting.set_val("foodics_base_url", "https://api-sandbox.foodics.com/v5")
            SystemSetting.set_val("foodics_creator_id", "test-creator-id")

    def tearDown(self):
        with app.app_context():
            db.session.remove()
            db.drop_all()

    def test_enqueue_and_process_count_mock(self):
        with app.app_context():
            # 1. إنشاء جلسة جرد
            session_obj = CountSession(
                location_id=self.branch_id,
                created_by=self.accountant_id,
                status="open"
            )
            db.session.add(session_obj)
            db.session.commit()

            # 2. إضافة بند جرد
            line = CountLine(
                session_id=session_obj.id,
                item_id=self.item_id,
                book_quantity=10.0,
                counted_quantity=12.0
            )
            db.session.add(line)
            db.session.commit()

            # 3. محاكاة نجاح استدعاء فوديكس
            original_call = sq.call_foodics_api
            sq.call_foodics_api = lambda s_type, s_obj: (True, {"reference": "FD-CNT-REF-999"}, None)

            try:
                # إدراج في الطابور ومعالجة
                job = sq.enqueue("count", session_obj.id)
                self.assertEqual(job.status, "queued")
                self.assertEqual(job.source_type, "count")

                ok = sq.process_job(job)
                self.assertTrue(ok)
                self.assertEqual(job.status, "success")

                # التحقق من تحديث الجلسة
                db.session.refresh(session_obj)
                self.assertEqual(session_obj.status, "posted")
                self.assertEqual(session_obj.foodics_reference, "FD-CNT-REF-999")
                self.assertEqual(session_obj.status_label_ar, "مرحل لفوديكس ✅")

                # التحقق من سجل المزامنة
                log = SyncLog.query.filter_by(queue_id=job.id).first()
                self.assertIsNotNone(log)
                self.assertEqual(log.api_status, "success")
            finally:
                sq.call_foodics_api = original_call

    def test_count_sync_unlinked_branch_error(self):
        """فحص اعتراض ترحيل جرد لفرع غير مربوط بفوديكس"""
        with app.app_context():
            session_obj = CountSession(
                location_id=self.unlinked_branch_id,
                created_by=self.accountant_id,
                status="open"
            )
            db.session.add(session_obj)
            db.session.commit()

            line = CountLine(
                session_id=session_obj.id,
                item_id=self.item_id,
                book_quantity=5.0,
                counted_quantity=5.0
            )
            db.session.add(line)
            db.session.commit()

            # استدعاء call_foodics_api الفعلي
            success, result, err = sq.call_foodics_api("count", session_obj)
            self.assertFalse(success)
            self.assertIn("غير مربوط بفوديكس", err)

    def test_count_edit_route_approve_and_sync_now(self):
        """اختبار مسار اعتماد الجرد مع الترحيل الفوري عبر Flask Test Client"""
        with app.app_context():
            session_obj = CountSession(
                location_id=self.branch_id,
                created_by=self.accountant_id,
                status="pending_review"
            )
            db.session.add(session_obj)
            db.session.commit()

            line = CountLine(
                session_id=session_obj.id,
                item_id=self.item_id,
                book_quantity=8.0,
                counted_quantity=10.0
            )
            db.session.add(line)
            db.session.commit()

            session_id = session_obj.id

        # تسجيل الدخول كمحاسب
        with self.client.session_transaction() as sess:
            sess["user_id"] = self.accountant_id

        original_call = sq.call_foodics_api
        sq.call_foodics_api = lambda s_type, s_obj: (True, {"reference": "FD-CNT-AUTO-100"}, None)

        try:
            # اعتماد مع sync_now=1
            resp = self.client.post(f"/counts/{session_id}", data={
                "action": "approve_session",
                "sync_now": "1"
            }, follow_redirects=True)
            self.assertEqual(resp.status_code, 200)

            with app.app_context():
                s = db.session.get(CountSession, session_id)
                self.assertEqual(s.status, "posted")
                self.assertEqual(s.foodics_reference, "FD-CNT-AUTO-100")
        finally:
            sq.call_foodics_api = original_call


if __name__ == "__main__":
    unittest.main()
