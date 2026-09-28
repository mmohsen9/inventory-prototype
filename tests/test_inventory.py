"""
مجموعة الاختبارات الآلية لنظام إدارة المخزون - مساكن رفاء
"""
import os
import unittest
import json
from datetime import datetime

# استخدام قاعدة بيانات مؤقتة للاختبارات
os.environ["DATABASE_URL"] = "sqlite:///:memory:"

from app import app
from models import (
    db, User, Location, Item, PurchaseTransaction, PurchaseLine,
    TransferOrder, TransferLine, CountSession, CountLine,
    StockBalance, StockMovement, record_stock_movement, Supplier
)


class InventorySystemTestCase(unittest.TestCase):
    def setUp(self):
        app.config["TESTING"] = True
        app.config["WTF_CSRF_ENABLED"] = False
        self.client = app.test_client()
        self.ctx = app.app_context()
        self.ctx.push()
        db.create_all()

        # إنشاء مواقع تجريبية
        self.loc_wh = Location(name_ar="المستودع الرئيسي", name_en="Main Warehouse", type="warehouse")
        self.loc_br = Location(name_ar="فرع رفاء", name_en="Rafa Branch", type="branch")
        db.session.add_all([self.loc_wh, self.loc_br])
        db.session.commit()

        # إنشاء مستخدمين تجريبيين
        self.admin = User(name="مدير النظام", role="admin", password="admin", active=True)
        self.accountant = User(name="المحاسب", role="accountant", password="1234", active=True)
        self.warehouse = User(name="أمين المستودع", role="warehouse", location_id=self.loc_wh.id, password="1234", active=True)
        self.staff = User(name="موظف الفرع", role="branch_staff", location_id=self.loc_br.id, password="1234", active=True)
        db.session.add_all([self.admin, self.accountant, self.warehouse, self.staff])
        db.session.commit()

        # إنشاء أصناف تجريبية
        self.item1 = Item(
            sku="sk-TEST1",
            name_ar="قهوة اسبريسو",
            name_en="Espresso Coffee",
            storage_unit="كجم",
            cost=50.0,
            barcode="62810001",
        )
        self.item2 = Item(
            sku="sk-TEST2",
            name_ar="حليب طازج",
            name_en="Fresh Milk",
            storage_unit="لتر",
            cost=6.0,
            barcode="62810002",
        )
        db.session.add_all([self.item1, self.item2])
        db.session.commit()

        # تهيئة رصيد افتتاحي
        record_stock_movement(self.item1.id, self.loc_wh.id, "initial", 100.0, notes="رصيد أولي", user_id=self.admin.id)
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def login_as(self, user):
        with self.client.session_transaction() as sess:
            sess["user_id"] = user.id

    # 1. اختبار تسجيل الدخول والخروج والصلاحيات
    def test_auth_flow(self):
        # محاولة دخول خاطئة
        res = self.client.post("/login", data={"name": "المحاسب", "password": "wrong"}, follow_redirects=True)
        self.assertIn("اسم المستخدم أو كلمة المرور غير صحيحة".encode("utf-8"), res.data)

        # دخول صحيح
        res = self.client.post("/login", data={"name": "المحاسب", "password": "1234"}, follow_redirects=True)
        self.assertIn("أهلاً بك يا المحاسب".encode("utf-8"), res.data)

        # تسجيل الخروج
        res = self.client.get("/logout", follow_redirects=True)
        self.assertIn("تم تسجيل الخروج بنجاح".encode("utf-8"), res.data)

    # 2. اختبار إدارة المستخدمين (إضافة، تعديل، إيقاف)
    def test_user_management(self):
        self.login_as(self.admin)

        # فتح شاشة المستخدمين
        res = self.client.get("/users")
        self.assertEqual(res.status_code, 200)
        self.assertIn("إدارة المستخدمين".encode("utf-8"), res.data)

        # إضافة مستخدم جديد
        res = self.client.post("/users/new", data={
            "name": "محمود المشرف",
            "role": "warehouse",
            "password": "pass",
            "email": "mahmoud@rafa.com",
            "location_id": self.loc_wh.id
        }, follow_redirects=True)
        self.assertEqual(res.status_code, 200)
        new_u = User.query.filter_by(name="محمود المشرف").first()
        self.assertIsNotNone(new_u)
        self.assertEqual(new_u.role, "warehouse")

        # تعديل المستخدم
        res = self.client.post(f"/users/{new_u.id}/edit", data={
            "name": "محمود المشرف المعدل",
            "role": "accountant",
            "password": "",
            "email": "m_edit@rafa.com",
            "location_id": ""
        }, follow_redirects=True)
        self.assertEqual(res.status_code, 200)
        db.session.refresh(new_u)
        self.assertEqual(new_u.name, "محمود المشرف المعدل")
        self.assertEqual(new_u.role, "accountant")

        # تعطيل وتفعيل المستخدم
        self.client.post(f"/users/{new_u.id}/toggle-status", follow_redirects=True)
        db.session.refresh(new_u)
        self.assertFalse(new_u.active)

        self.client.post(f"/users/{new_u.id}/toggle-status", follow_redirects=True)
        db.session.refresh(new_u)
        self.assertTrue(new_u.active)

    # 3. اختبار دورة حياة فاتورة الشراء وتأثيرها على المخزون
    def test_purchase_lifecycle_and_stock(self):
        self.login_as(self.warehouse)

        # إنشاء فاتورة
        res = self.client.post("/purchases/new", data={
            "supplier_name": "شركة القهوة المختصة",
            "invoice_number": "INV-777",
            "invoice_date": "2026-09-17",
            "location_id": self.loc_wh.id
        }, follow_redirects=True)
        self.assertEqual(res.status_code, 200)

        purchase = PurchaseTransaction.query.filter_by(invoice_number="INV-777").first()
        self.assertIsNotNone(purchase)
        self.assertEqual(purchase.status, "draft")

        # إضافة بند للفاتورة
        res = self.client.post(f"/purchases/{purchase.id}", data={
            "action": "add_line",
            "code": self.item1.sku,
            "quantity": "25.0",
            "unit": self.item1.storage_unit,
            "unit_cost": "48.0"
        }, follow_redirects=True)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(len(purchase.lines), 1)
        self.assertEqual(purchase.lines[0].quantity, 25.0)

        # إضافة مرفق وهمي لإجازة التحقق
        purchase.attachments = json.dumps(["/uploads/purchases/test.jpg"])
        db.session.commit()

        # إرسال للمراجعة
        res = self.client.post(f"/purchases/{purchase.id}", data={"action": "submit_for_review"}, follow_redirects=True)
        self.assertEqual(res.status_code, 200)
        db.session.refresh(purchase)
        self.assertEqual(purchase.status, "submitted")

        # اعتماد المحاسب للفاتورة
        self.login_as(self.accountant)
        wh_stock_before = self.item1.get_stock_in_location(self.loc_wh.id)

        res = self.client.post(f"/purchases/{purchase.id}/review", data={"action": "approve"}, follow_redirects=True)
        self.assertEqual(res.status_code, 200)

        # التحقق من أن الرصيد زاد بمقدار 25
        wh_stock_after = self.item1.get_stock_in_location(self.loc_wh.id)
        self.assertEqual(wh_stock_after, wh_stock_before + 25.0)

        # التحقق من تسجيل الحركة في دفتر الأستاذ
        movement = StockMovement.query.filter_by(reference_id=purchase.id, movement_type="purchase_in").first()
        self.assertIsNotNone(movement)
        self.assertEqual(movement.quantity_change, 25.0)

    # 4. اختبار دورة حياة التحويل بين المستودع والفرع مع الفروقات
    def test_transfer_lifecycle_and_variance(self):
        self.login_as(self.warehouse)

        # رصيد المستودع الحالي 100
        # إنشاء تحويل
        res = self.client.post("/transfers/new", data={
            "from_location_id": self.loc_wh.id,
            "to_location_id": self.loc_br.id
        }, follow_redirects=True)
        transfer = TransferOrder.query.filter_by(from_location_id=self.loc_wh.id, to_location_id=self.loc_br.id).first()
        self.assertIsNotNone(transfer)

        # إضافة صنف للشحنة
        self.client.post(f"/transfers/{transfer.id}", data={
            "action": "add_line",
            "code": self.item1.sku,
            "quantity": "20.0"
        }, follow_redirects=True)

        # صرف الشحنة
        res = self.client.post(f"/transfers/{transfer.id}", data={"action": "send"}, follow_redirects=True)
        db.session.refresh(transfer)
        self.assertEqual(transfer.status, "in_transit")

        # التحقق من خصم الكمية من المستودع
        wh_stock = self.item1.get_stock_in_location(self.loc_wh.id)
        self.assertEqual(wh_stock, 80.0)

        # موظف الفرع يستلم الشحنة مع وجود نقص (وصل 18 بدل 20)
        self.login_as(self.staff)
        line = transfer.lines[0]
        res = self.client.post(f"/transfers/{transfer.id}/receive", data={
            f"qty_received_{line.id}": "18.0",
            f"reason_{line.id}": "تلف كرتونين أثناء التحميل"
        }, follow_redirects=True)

        db.session.refresh(transfer)
        self.assertEqual(transfer.status, "needs_review")
        self.assertEqual(line.variance_qty, -2.0)

        # المحاسب يعتمد التحويل بالفروقات
        self.login_as(self.accountant)
        self.client.post(f"/transfers/{transfer.id}/approve", follow_redirects=True)
        db.session.refresh(transfer)
        self.assertEqual(transfer.status, "approved")

        # التحقق من إضافة الـ 18 حبة المستلمة فعلياً لفرع رفاء
        br_stock = self.item1.get_stock_in_location(self.loc_br.id)
        self.assertEqual(br_stock, 18.0)

    # 5. اختبار جلسة الجرد وتسوية الأرصدة
    def test_count_session_and_reconciliation(self):
        self.login_as(self.warehouse)

        # رصيد item1 في المستودع حالياً 80.0 (بعد التحويل في الاختبار السابق لو تتابع، هنا 100)
        # إنشاء جلسة جرد
        res = self.client.post("/counts/new", data={"location_id": self.loc_wh.id}, follow_redirects=True)
        session_obj = CountSession.query.filter_by(location_id=self.loc_wh.id, status="open").first()
        self.assertIsNotNone(session_obj)

        # تسجيل عد فعلي: 95.0 (بدل 100.0)
        res = self.client.post(f"/counts/{session_obj.id}", data={
            "action": "add_line",
            "code": self.item1.sku,
            "counted_quantity": "95.0"
        }, follow_redirects=True)

        line = session_obj.lines[0]
        self.assertEqual(line.book_quantity, 100.0)
        self.assertEqual(line.counted_quantity, 95.0)
        self.assertEqual(line.variance_quantity, -5.0)

        # إنهاء العد وإغلاق الجلسة
        self.client.post(f"/counts/{session_obj.id}", data={"action": "close_session"}, follow_redirects=True)
        db.session.refresh(session_obj)
        self.assertEqual(session_obj.status, "pending_review")

        # اعتماد المحاسب للجرد
        self.login_as(self.accountant)
        self.client.post(f"/counts/{session_obj.id}", data={"action": "approve_session"}, follow_redirects=True)
        db.session.refresh(session_obj)
        self.assertEqual(session_obj.status, "approved")

        # التحقق من تعديل الرصيد إلى 95.0
        updated_stock = self.item1.get_stock_in_location(self.loc_wh.id)
        self.assertEqual(updated_stock, 95.0)

    # 6. اختبار مسارات التقارير وتصدير ملفات CSV
    def test_reports_and_export(self):
        self.login_as(self.admin)

        # تقرير الأرصدة
        res = self.client.get("/reports/stock")
        self.assertEqual(res.status_code, 200)
        self.assertIn("تقرير أرصدة المخزون".encode("utf-8"), res.data)

        # تقرير الحركات
        res = self.client.get("/reports/movements")
        self.assertEqual(res.status_code, 200)
        self.assertIn("سجل حركات المخزون".encode("utf-8"), res.data)

        # تقرير الفروقات
        res = self.client.get("/reports/variance")
        self.assertEqual(res.status_code, 200)
        self.assertIn("تقرير فروقات الجرد".encode("utf-8"), res.data)

        # تصدير CSV
        res_stock = self.client.get("/reports/export?type=stock")
        self.assertEqual(res_stock.status_code, 200)
        self.assertIn("text/csv", res_stock.headers.get("Content-Type"))

        res_mov = self.client.get("/reports/export?type=movements")
        self.assertEqual(res_mov.status_code, 200)
        self.assertIn("text/csv", res_mov.headers.get("Content-Type"))

    # 7. اختبار واجهات الـ API للبحث والإكمال التلقائي
    def test_api_search_and_lookup(self):
        self.login_as(self.warehouse)

        # Lookup by SKU
        res = self.client.get(f"/api/lookup_item?code={self.item1.sku}")
        data = res.get_json()
        self.assertTrue(data["found"])
        self.assertEqual(data["name_ar"], "قهوة اسبريسو")

        # Lookup by Barcode
        res = self.client.get(f"/api/lookup_item?code={self.item1.barcode}")
        data = res.get_json()
        self.assertTrue(data["found"])

        # Search autocomplete
        res = self.client.get("/api/search_items?q=اسبريسو")
        data = res.get_json()
        self.assertGreaterEqual(len(data.get("results", [])), 1)
        self.assertEqual(data["results"][0]["sku"], self.item1.sku)
        self.assertEqual(data["results"][0]["name_en"], "Espresso Coffee")

    # 8. اختبار تبديل اللغة وتعديل واستيراد الأرصدة وإعدادات فوديكس
    def test_new_features(self):
        self.login_as(self.admin)

        # تبديل اللغة
        res = self.client.get("/toggle-lang", follow_redirects=True)
        self.assertEqual(res.status_code, 200)

        # تعديل الرصيد يدوياً
        res = self.client.post("/stock/adjust", data={
            "item_id": self.item1.id,
            "location_id": self.loc_wh.id,
            "new_quantity": "85.5",
            "reason": "تسوية جردية"
        }, follow_redirects=True)
        self.assertEqual(res.status_code, 200)
        bal = StockBalance.query.filter_by(item_id=self.item1.id, location_id=self.loc_wh.id).first()
        self.assertEqual(bal.quantity, 85.5)

        # تنزيل نموذج الاستيراد
        res = self.client.get("/stock/template")
        self.assertEqual(res.status_code, 200)
        self.assertIn("text/csv", res.headers.get("Content-Type"))

        # حفظ إعدادات فوديكس
        res = self.client.post("/foodics/settings", data={
            "foodics_token": "test_token_123",
            "foodics_base_url": "https://api.foodics.com/v5",
            "action": "save"
        }, follow_redirects=True)
        self.assertEqual(res.status_code, 200)

    # 9. اختبار إضافة وبحث الموردين
    def test_suppliers_feature(self):
        self.login_as(self.admin)

        # إضافة مورد جديد عبر API
        res = self.client.post("/api/suppliers/add", json={
            "name": "مورد القهوة الذهبية",
            "code": "GOLD-001",
            "phone": "0555555555"
        })
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data.get("success"))
        self.assertEqual(data["supplier"]["name"], "مورد القهوة الذهبية")

        # التحقق من وجود المورد في البحث
        res = self.client.get("/api/suppliers?q=الذهبية")
        self.assertEqual(res.status_code, 200)
        suppliers = res.get_json()
        self.assertTrue(any(s["name"] == "مورد القهوة الذهبية" for s in suppliers))

        # إنشاء سند استلام بمورد جديد مباشرة
        res = self.client.post("/purchases/new", data={
            "supplier_name": "مورد الأكواب الورقية الجديد",
            "invoice_number": "INV-TEST-999",
            "invoice_date": "2026-09-27",
            "location_id": self.loc_wh.id,
        }, follow_redirects=True)
        self.assertEqual(res.status_code, 200)

        # التحقق من حفظ المورد الجديد في قاعدة البيانات
        s = Supplier.query.filter_by(name="مورد الأكواب الورقية الجديد").first()
        self.assertIsNotNone(s)

    # 10. اختبار تغيير فرع الوجهة في سند صرف المواد قبل الحفظ/الإرسال
    def test_transfer_change_destination(self):
        self.login_as(self.warehouse)

        # إضافة فرع ثالث للاختبار
        loc3 = Location(name_ar="فرع التخصصي", name_en="Takhassusi Branch", type="branch")
        db.session.add(loc3)
        db.session.commit()

        # إنشاء سند تحويل جديد من المستودع إلى فرع رفاء
        transfer = TransferOrder(
            from_location_id=self.loc_wh.id,
            to_location_id=self.loc_br.id,
            created_by=self.warehouse.id,
            status="draft",
            attachments="[]",
        )
        db.session.add(transfer)
        db.session.commit()

        # إضافة بند للشحنة
        self.client.post(f"/transfers/{transfer.id}", data={
            "action": "add_line",
            "code": self.item1.sku,
            "quantity": "5"
        }, follow_redirects=True)

        # تغيير فرع الوجهة إلى فرع التخصصي
        res = self.client.post(f"/transfers/{transfer.id}", data={
            "action": "update_destination",
            "to_location_id": str(loc3.id)
        }, follow_redirects=True)
        self.assertEqual(res.status_code, 200)

        updated_transfer = db.session.get(TransferOrder, transfer.id)
        self.assertEqual(updated_transfer.to_location_id, loc3.id)

    # 11. اختبار التغيير الكامل للمسميات عند التحويل للغة الإنجليزية
    def test_english_localization(self):
        self.login_as(self.admin)

        # التبديل إلى الإنجليزية
        res = self.client.get("/toggle-lang", follow_redirects=True)
        self.assertEqual(res.status_code, 200)

        # فحص لوحة التحكم: التحقق من وجود مسميات إنجليزية
        res = self.client.get("/dashboard")
        html = res.data.decode("utf-8")
        self.assertIn("Welcome", html)
        self.assertIn("Dashboard", html)
        self.assertIn("System Admin", html)
        self.assertIn("SAR", html)

        # فحص سندات الاستلام
        res = self.client.get("/purchases")
        html = res.data.decode("utf-8")
        self.assertIn("Receipt Vouchers", html)
        self.assertIn("Supplier", html)


if __name__ == "__main__":
    unittest.main()


