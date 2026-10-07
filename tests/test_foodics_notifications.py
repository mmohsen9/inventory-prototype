import pytest
import json
from app import app
from models import db, Item, Notification, User, Location, Supplier


@pytest.fixture
def client():
    app.config["TESTING"] = True
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    app.config["WTF_CSRF_ENABLED"] = False

    with app.app_context():
        db.create_all()
        # Create test locations and admin user
        wh = Location(name_ar="المستودع الرئيسي", name_en="Warehouse 1", type="warehouse", foodics_id="wh_101")
        br = Location(name_ar="فرع التخصصي", name_en="Branch 1", type="branch", foodics_id="br_101")
        db.session.add_all([wh, br])
        db.session.flush()

        admin = User(name="مدير النظام", role="admin", password="password123", active=True)
        accountant = User(name="المحاسب", role="accountant", password="password123", active=True)
        db.session.add_all([admin, accountant])
        db.session.commit()

        with app.test_client() as test_client:
            yield test_client

        db.session.remove()
        db.drop_all()


def test_foodics_webhook_new_item_creates_item_and_notification(client):
    """التحقق من أن Webhook فوديكس يستقبل الصنف الجديد، وينشئه في جدول Items، ويولد إشعاراً في جدول Notifications."""
    payload = {
        "event": "inventory_item.created",
        "data": {
            "id": "fdx_item_999",
            "sku": "COF-TEST-999",
            "name": "Ethiopian Specialty Coffee",
            "name_localized": "بن إثيوبي مختص",
            "storage_unit": "كجم",
            "ingredient_unit": "جرام",
            "storage_to_ingredient_factor": 1000.0,
            "cost": 55.0,
            "barcode": "628123456789"
        }
    }

    res = client.post("/api/foodics/webhook",
                      data=json.dumps(payload),
                      content_type="application/json")

    assert res.status_code == 200
    data = res.get_json()
    assert data["status"] == "success"
    assert data["is_new"] is True
    assert data["sku"] == "COF-TEST-999"

    # التحقق من قاعدة البيانات
    with app.app_context():
        item = Item.query.filter_by(sku="COF-TEST-999").first()
        assert item is not None
        assert item.name_ar == "بن إثيوبي مختص"
        assert item.cost == 55.0
        assert item.storage_unit == "كجم"
        assert item.foodics_id == "fdx_item_999"

        # التحقق من توليد الإشعار
        notif = Notification.query.filter_by(item_sku="COF-TEST-999").first()
        assert notif is not None
        assert notif.is_read is False
        assert "بن إثيوبي مختص" in notif.message
        assert "COF-TEST-999" in notif.message


def test_foodics_webhook_update_existing_item(client):
    """التحقق من تحديث صنف موجود عند إرسال حدث من فوديكس دون تكرار الصنف."""
    # صنف موجود مسبقاً
    with app.app_context():
        it = Item(sku="EXIST-SKU-1", name_ar="صنف قديم", name_en="Old Item", cost=10.0, storage_unit="علبة")
        db.session.add(it)
        db.session.commit()

    payload = {
        "event": "inventory_item.updated",
        "data": {
            "id": "fdx_exist_1",
            "sku": "EXIST-SKU-1",
            "name": "Old Item Updated",
            "cost": 15.5
        }
    }

    res = client.post("/api/foodics/webhook",
                      data=json.dumps(payload),
                      content_type="application/json")

    assert res.status_code == 200
    data = res.get_json()
    assert data["is_new"] is False

    with app.app_context():
        items_count = Item.query.filter_by(sku="EXIST-SKU-1").count()
        assert items_count == 1
        item = Item.query.filter_by(sku="EXIST-SKU-1").first()
        assert item.cost == 15.5
        assert item.foodics_id == "fdx_exist_1"


def test_foodics_test_webhook_simulation(client):
    """التحقق من نقطة محاكاة إرسال صنف تجريبي لاختبار الإشعارات من الواجهة."""
    # تسجيل الدخول كمسؤول
    with client.session_transaction() as sess:
        sess["user_id"] = 1

    res = client.post("/api/foodics/test_webhook",
                      data=json.dumps({
                          "name_ar": "سكر بني عضوي",
                          "sku": "SUG-ORG-01",
                          "storage_unit": "كيس",
                          "cost": 18.0
                      }),
                      headers={"X-Requested-With": "XMLHttpRequest"},
                      content_type="application/json")

    assert res.status_code == 200
    data = res.get_json()
    assert data["status"] == "success"
    assert data["sku"] == "SUG-ORG-01"

    with app.app_context():
        notif = Notification.query.filter_by(item_sku="SUG-ORG-01").first()
        assert notif is not None
        assert "سكر بني عضوي" in notif.message


def test_notifications_unread_and_mark_read(client):
    """التحقق من واجهة برمجة جلب وتحديث الإشعارات غير المقروءة."""
    with app.app_context():
        n1 = Notification(title="إشعار 1", message="محتوى 1", type="new_inventory_item", is_read=False)
        n2 = Notification(title="إشعار 2", message="محتوى 2", type="new_inventory_item", is_read=False)
        db.session.add_all([n1, n2])
        db.session.commit()
        n1_id = n1.id

    with client.session_transaction() as sess:
        sess["user_id"] = 1

    # جلب غير المقروءة
    res = client.get("/api/notifications/unread")
    assert res.status_code == 200
    data = res.get_json()
    assert data["unread_count"] == 2

    # تعليم واحد كمقروء
    res_mark = client.post(f"/api/notifications/mark_read/{n1_id}")
    assert res_mark.status_code == 200

    res2 = client.get("/api/notifications/unread")
    data2 = res2.get_json()
    assert data2["unread_count"] == 1

    # تعليم الكل كمقروء
    res_all = client.post("/api/notifications/mark_all_read", headers={"X-Requested-With": "XMLHttpRequest"})
    assert res_all.status_code == 200

    res3 = client.get("/api/notifications/unread")
    data3 = res3.get_json()
    assert data3["unread_count"] == 0


def test_foodics_webhook_item_with_supplier(client):
    """التحقق من أن الصنف المربوط بمورد يظهر في الإشعار أنه مربوط ويحمل اسم المورد."""
    payload = {
        "event": "inventory_item.created",
        "data": {
            "id": "fdx_item_sup_1",
            "sku": "COF-SUP-01",
            "name": "Coffee Beans With Supplier",
            "name_localized": "بن بالمورد",
            "storage_unit": "كجم",
            "cost": 50.0,
            "supplier_name": "شركة المطاحن الحديثة"
        }
    }

    res = client.post("/api/foodics/webhook",
                      data=json.dumps(payload),
                      content_type="application/json")

    assert res.status_code == 200
    with app.app_context():
        notif = Notification.query.filter_by(item_sku="COF-SUP-01").first()
        assert notif is not None
        assert notif.has_supplier is True
        assert notif.supplier_name == "شركة المطاحن الحديثة"
        assert "مربوط بمورد" in notif.title
        assert "شركة المطاحن الحديثة" in notif.message


def test_foodics_webhook_item_without_supplier(client):
    """التحقق من أن الصنف غير المربوط بمورد يظهر في الإشعار بوضوح أنه غير مربوط."""
    payload = {
        "event": "inventory_item.created",
        "data": {
            "id": "fdx_item_nosup_2",
            "sku": "COF-NOSUP-02",
            "name": "Coffee Beans No Supplier",
            "name_localized": "بن بدون مورد",
            "storage_unit": "كجم",
            "cost": 40.0
        }
    }

    res = client.post("/api/foodics/webhook",
                      data=json.dumps(payload),
                      content_type="application/json")

    assert res.status_code == 200
    with app.app_context():
        notif = Notification.query.filter_by(item_sku="COF-NOSUP-02").first()
        assert notif is not None
        assert notif.has_supplier is False
        assert notif.supplier_name is None
        assert "غير مربوط بمورد" in notif.title
        assert "غير مربوط بأي مورد" in notif.message


def test_api_item_details_and_resolve_observation(client):
    """التحقق من استعلام تفاصيل الصنف ومعالجة ملاحظة عدم ربط المورد وربطه بنجاح."""
    # تسجيل الدخول كمدير نظام
    with client.session_transaction() as sess:
        sess["user_id"] = 1

    # إنشاء صنف تجريبي وإشعار غير مربوط
    with app.app_context():
        it = Item(sku="ITEM-UNLINKED-9", name_ar="ماتشا يابانية", storage_unit="علبة", cost=85.0)
        db.session.add(it)
        db.session.flush()

        sup = Supplier(name="مورد الشاي والماتشا المختص", code="SUP-MAT-01")
        db.session.add(sup)
        db.session.flush()

        notif = Notification(
            type="new_inventory_item",
            title="مادة مخزون جديدة من فوديكس (غير مربوط بمورد ⚠️)",
            message="الصنف ماتشا يابانية غير مربوط بأي مورد حالياً",
            item_sku=it.sku,
            has_supplier=False,
            is_read=False
        )
        db.session.add(notif)
        db.session.commit()
        notif_id = notif.id
        sup_id = sup.id

    # 1. اختبار استعلام تفاصيل الصنف GET /api/items/<sku>/details
    res_get = client.get("/api/items/ITEM-UNLINKED-9/details")
    assert res_get.status_code == 200
    data_get = res_get.get_json()
    assert data_get["status"] == "success"
    assert data_get["item"]["name_ar"] == "ماتشا يابانية"
    assert data_get["item"]["has_supplier"] is False
    assert len(data_get["suppliers"]) >= 1

    # 2. اختبار معالجة الملاحظة وربط المورد POST /api/items/resolve-observation
    resolve_payload = {
        "sku": "ITEM-UNLINKED-9",
        "name_ar": "ماتشا يابانية فاخرة",
        "storage_unit": "كيلو",
        "cost": 90.0,
        "supplier_id": sup_id,
        "notification_id": notif_id
    }
    res_post = client.post("/api/items/resolve-observation",
                           data=json.dumps(resolve_payload),
                           content_type="application/json")
    assert res_post.status_code == 200
    data_post = res_post.get_json()
    assert data_post["status"] == "success"
    assert data_post["has_supplier"] is True
    assert data_post["supplier_name"] == "مورد الشاي والماتشا المختص"

    # 3. التحقق من قاعدة البيانات بعد المعالجة
    with app.app_context():
        updated_item = Item.query.filter_by(sku="ITEM-UNLINKED-9").first()
        assert updated_item.name_ar == "ماتشا يابانية فاخرة"
        assert updated_item.storage_unit == "كيلو"
        assert updated_item.cost == 90.0
        assert len(updated_item.suppliers) == 1
        assert updated_item.suppliers[0].name == "مورد الشاي والماتشا المختص"

        updated_notif = db.session.get(Notification, notif_id)
        assert updated_notif.has_supplier is True
        assert updated_notif.supplier_name == "مورد الشاي والماتشا المختص"
        assert updated_notif.is_read is True
        assert "تم ربط الصنف بالمورد بنجاح" in updated_notif.title


