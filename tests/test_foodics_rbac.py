"""
اختبارات شاملة لنظام إدارة المستخدمين والصلاحيات بنمط فوديكس (Foodics RBAC)
"""
import pytest
from app import app, ensure_seed_data
from models import db, User, Role, RolePermission, Location


@pytest.fixture
def rbac_client():
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    with app.test_client() as client:
        with app.app_context():
            db.create_all()
            ensure_seed_data()
        yield client


def test_default_system_roles_exist_and_have_permissions(rbac_client):
    with app.app_context():
        roles = Role.query.all()
        assert len(roles) >= 4
        role_names = [r.name for r in roles]
        assert "مدير النظام" in role_names
        assert "أمين مستودع" in role_names
        assert "موظف فرع" in role_names
        assert "محاسب" in role_names

        admin_role = Role.query.filter_by(name="مدير النظام").first()
        assert admin_role.is_system is True
        assert admin_role.has_perm("purchases.approve") is True
        assert admin_role.has_perm("admin.users") is True

        wh_role = Role.query.filter_by(name="أمين مستودع").first()
        assert wh_role.has_perm("purchases.create") is True
        assert wh_role.has_perm("transfers.create") is True
        assert wh_role.has_perm("purchases.approve") is False  # أمين المستودع لا يملك الاعتماد المالي


def test_user_has_permission_logic(rbac_client):
    with app.app_context():
        wh_role = Role.query.filter_by(name="أمين مستودع").first()
        br_role = Role.query.filter_by(name="موظف فرع").first()

        wh_user = User(name="اختبار مستودع", role="warehouse", role_id=wh_role.id, active=True)
        wh_user.set_password("pass123")
        db.session.add(wh_user)

        br_user = User(name="اختبار فرع", role="branch_staff", role_id=br_role.id, active=True)
        br_user.set_password("pass123")
        db.session.add(br_user)
        db.session.commit()

        # فحص صلاحيات أمين المستودع
        assert wh_user.has_permission("purchases.create") is True
        assert wh_user.has_permission("purchases.approve") is False

        # فحص صلاحيات موظف الفرع
        assert br_user.has_permission("transfers.receive") is True
        assert br_user.has_permission("purchases.create") is False


def test_custom_role_creation_and_editing(rbac_client):
    with app.app_context():
        admin = User.query.filter_by(role="admin").first()
        if not admin:
            admin = User(name="مدير فحص", role="admin", active=True)
            admin.set_password("admin123")
            db.session.add(admin)
            db.session.commit()
        admin_id = admin.id

    with rbac_client.session_transaction() as sess:
        sess["user_id"] = admin_id

    # 1. إنشاء دور جديد
    res = rbac_client.post("/roles/new", data={
        "name": "أخصائي مشتريات تجريبي",
        "name_en": "Test Purchasing Officer",
        "description": "مسؤول عن استلام فواتير المشتريات فقط",
        "permissions": ["purchases.view", "purchases.create", "purchases.view_cost"]
    }, follow_redirects=True)
    assert res.status_code == 200

    with app.app_context():
        custom_role = Role.query.filter_by(name="أخصائي مشتريات تجريبي").first()
        assert custom_role is not None
        assert custom_role.is_system is False
        assert custom_role.has_perm("purchases.create") is True
        assert custom_role.has_perm("purchases.view_cost") is True
        assert custom_role.has_perm("transfers.create") is False
        role_id = custom_role.id

    # 2. تعديل الدور وإضافة صلاحية تحويلات
    res_edit = rbac_client.post(f"/roles/{role_id}/edit", data={
        "name": "أخصائي مشتريات وتوريد",
        "name_en": "Purchasing and Supply",
        "description": "وصف محدث",
        "permissions": ["purchases.view", "purchases.create", "transfers.view"]
    }, follow_redirects=True)
    assert res_edit.status_code == 200

    with app.app_context():
        updated_role = Role.query.get(role_id)
        assert updated_role.name == "أخصائي مشتريات وتوريد"
        assert updated_role.has_perm("transfers.view") is True
        assert updated_role.has_perm("purchases.view_cost") is False  # أزيلت


def test_system_role_deletion_is_prevented(rbac_client):
    with app.app_context():
        admin = User.query.filter_by(role="admin").first()
        admin_role = Role.query.filter_by(name="مدير النظام").first()
        admin_id = admin.id
        admin_role_id = admin_role.id

    with rbac_client.session_transaction() as sess:
        sess["user_id"] = admin_id

    res = rbac_client.post(f"/roles/{admin_role_id}/delete", follow_redirects=True)
    assert res.status_code == 200

    with app.app_context():
        still_exists = Role.query.get(admin_role_id)
        assert still_exists is not None  # الدور الأساسي لا يحذف


def test_user_creation_with_pin_and_pin_login(rbac_client):
    with app.app_context():
        admin = User.query.filter_by(role="admin").first()
        wh_role = Role.query.filter_by(name="أمين مستودع").first()
        admin_id = admin.id
        wh_role_id = wh_role.id

    with rbac_client.session_transaction() as sess:
        sess["user_id"] = admin_id

    res = rbac_client.post("/users/new", data={
        "name": "مستخدم برمز سريع",
        "role_id": wh_role_id,
        "password": "Password123!",
        "pin_code": "7890",
        "phone": "0555555555",
        "email": "quick@rafa.com",
    }, follow_redirects=True)
    assert res.status_code == 200

    # الخروج
    with rbac_client.session_transaction() as sess:
        sess.clear()

    # تسجيل الدخول باستخدام PIN Code ككلمة مرور
    login_res = rbac_client.post("/login", data={
        "name": "مستخدم برمز سريع",
        "password": "7890"
    }, follow_redirects=True)
    assert login_res.status_code == 200
    with rbac_client.session_transaction() as sess:
        assert sess.get("user_id") is not None


def test_updated_permission_groups_and_counts():
    from models import SYSTEM_PERMISSION_GROUPS
    assert len(SYSTEM_PERMISSION_GROUPS) == 6
    group_keys = [g["key"] for g in SYSTEM_PERMISSION_GROUPS]
    assert "purchases" in group_keys
    assert "transfers" in group_keys
    assert "counts" in group_keys
    assert "coffee" in group_keys
    assert "finance" in group_keys
    assert "reports_admin" in group_keys

    total_perms = sum(len(g["permissions"]) for g in SYSTEM_PERMISSION_GROUPS)
    assert total_perms == 39


def test_new_permissions_role_isolation(rbac_client):
    with app.app_context():
        acc = User.query.filter_by(role="accountant").first()
        wh = User.query.filter_by(role="warehouse").first()
        br = User.query.filter_by(role="branch_staff").first()

        # المحاسب يملك كافة صلاحيات الداشبورد المالي وسلة وإعدادات القهوة
        assert acc.has_permission("finance.dashboard") is True
        assert acc.has_permission("finance.pnl") is True
        assert acc.has_permission("finance.balance_sheet") is True
        assert acc.has_permission("finance.budget") is True
        assert acc.has_permission("coffee.salla") is True
        assert acc.has_permission("coffee.settings") is True
        assert acc.has_permission("foodics.hub") is True
        assert acc.has_permission("items.manage_units") is True

        # أمين المستودع يملك سلة والوحدات، ومحجوب تماماً عن قوائم الدخل والميزانية
        assert wh.has_permission("coffee.salla") is True
        assert wh.has_permission("items.manage_units") is True
        assert wh.has_permission("finance.pnl") is False
        assert wh.has_permission("finance.balance_sheet") is False
        assert wh.has_permission("finance.budget") is False

        # موظف الفرع محجوب تماماً عن الداشبورد المالي وسلة والمحمصة
        assert br.has_permission("finance.dashboard") is False
        assert br.has_permission("finance.pnl") is False
        assert br.has_permission("coffee.salla") is False
        assert br.has_permission("coffee.roasting") is False


def test_findash_and_salla_route_access_by_role(rbac_client):
    with app.app_context():
        acc = User.query.filter_by(role="accountant").first()
        br = User.query.filter_by(role="branch_staff").first()
        wh = User.query.filter_by(role="warehouse").first()

        acc_id = acc.id
        br_id = br.id
        wh_id = wh.id

    # 1. موظف الفرع يحاول دخول الداشبورد المالي -> يُرفض ويُحوّل
    with rbac_client.session_transaction() as sess:
        sess["user_id"] = br_id
    res_br_fin = rbac_client.get("/finance/", follow_redirects=False)
    assert res_br_fin.status_code == 302
    assert "/finance" not in res_br_fin.headers.get("Location", "")

    # موظف الفرع يحاول دخول سلة -> يُرفض
    res_br_salla = rbac_client.get("/coffee/salla/orders", follow_redirects=False)
    assert res_br_salla.status_code == 302

    # 2. المحاسب يدخل الداشبورد المالي وسلة -> ينجح 200
    with rbac_client.session_transaction() as sess:
        sess["user_id"] = acc_id
    res_acc_fin = rbac_client.get("/finance/")
    assert res_acc_fin.status_code == 200

    res_acc_salla = rbac_client.get("/coffee/salla/orders")
    assert res_acc_salla.status_code == 200

    # 3. أمين المستودع يدخل سلة -> ينجح، لكن الداشبورد المالي PnL -> يُرفض
    with rbac_client.session_transaction() as sess:
        sess["user_id"] = wh_id
    res_wh_salla = rbac_client.get("/coffee/salla/orders")
    assert res_wh_salla.status_code == 200

    res_wh_pnl = rbac_client.get("/finance/pnl", follow_redirects=False)
    assert res_wh_pnl.status_code == 302


def test_roving_and_multi_branch_employee_access(rbac_client):
    """اختبار موظف الفرع المتنقل بين الفروع والموظف متعدد الفروع المحددة"""
    with app.app_context():
        from models import Location, UserLocation

        branches = Location.query.filter_by(type="branch").all()
        if len(branches) < 3:
            loc1 = Location(name_ar="فرع حطين التجريبي", name_en="Hittin Test", type="branch", foodics_id="test_br_1")
            loc2 = Location(name_ar="فرع الياسمين التجريبي", name_en="Yasmin Test", type="branch", foodics_id="test_br_2")
            loc3 = Location(name_ar="فرع الملقا التجريبي", name_en="Malqa Test", type="branch", foodics_id="test_br_3")
            db.session.add_all([loc1, loc2, loc3])
            db.session.commit()
            branches = Location.query.filter_by(type="branch").all()

        loc1, loc2, loc3 = branches[0], branches[1], branches[2]

        # 1. موظف متنقل عام (Roving)
        roving_user = User(
            name="سعد المتنقل",
            role="branch_staff",
            location_id=loc1.id,
            is_roving=True,
            active=True
        )
        roving_user.set_password("123456")
        db.session.add(roving_user)

        # 2. موظف متعدد الفروع محدد (فرع 1 وفرع 2 فقط)
        multi_user = User(
            name="فيصل الثنائي",
            role="branch_staff",
            location_id=loc1.id,
            is_roving=False,
            active=True
        )
        multi_user.set_password("123456")
        db.session.add(multi_user)
        db.session.flush()

        db.session.add(UserLocation(user_id=multi_user.id, location_id=loc1.id))
        db.session.add(UserLocation(user_id=multi_user.id, location_id=loc2.id))
        db.session.commit()

        roving_id = roving_user.id
        multi_id = multi_user.id
        loc1_id = loc1.id
        loc2_id = loc2.id
        loc3_id = loc3.id

    # تحقق من صلاحيات وسلوك الموظف المتنقل
    with app.app_context():
        ru = User.query.get(roving_id)
        assert ru.is_roving is True
        assert ru.is_multi_branch is True
        assert ru.can_access_location(loc1_id) is True
        assert ru.can_access_location(loc2_id) is True
        assert ru.can_access_location(loc3_id) is True

    # تبديل الفرع للموظف المتنقل عبر مسار switch-branch
    with rbac_client.session_transaction() as sess:
        sess["user_id"] = roving_id
    res_switch = rbac_client.get(f"/switch-branch/{loc2_id}", follow_redirects=False)
    assert res_switch.status_code == 302
    with rbac_client.session_transaction() as sess:
        assert sess.get("active_location_id") == loc2_id

    # تحقق من سلوك الموظف متعدد الفروع المحددة
    with app.app_context():
        mu = User.query.get(multi_id)
        assert mu.is_roving is False
        assert mu.is_multi_branch is True
        assert mu.can_access_location(loc1_id) is True
        assert mu.can_access_location(loc2_id) is True
        assert mu.can_access_location(loc3_id) is False  # غير مصرح له بفرع 3

    # محاولة فيصل التبديل لفرع 3 غير المصرح به -> يتم رفضه ولا يتغير الفرع
    with rbac_client.session_transaction() as sess:
        sess["user_id"] = multi_id
        sess.pop("active_location_id", None)
    res_denied = rbac_client.get(f"/switch-branch/{loc3_id}", follow_redirects=False)
    assert res_denied.status_code == 302
    with rbac_client.session_transaction() as sess:
        assert sess.get("active_location_id") != loc3_id

    # تبديل فيصل لفرع 2 المصرح به -> ينجح
    res_ok = rbac_client.get(f"/switch-branch/{loc2_id}", follow_redirects=False)
    assert res_ok.status_code == 302
    with rbac_client.session_transaction() as sess:
        assert sess.get("active_location_id") == loc2_id


