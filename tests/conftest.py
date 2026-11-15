import os
import pytest
import io
from app import app as flask_app
from models import (
    db, User, Location, Item, Supplier, StockBalance, record_stock_movement
)

@pytest.fixture(scope="session")
def app():
    flask_app.config["TESTING"] = True
    flask_app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    flask_app.config["WTF_CSRF_ENABLED"] = False
    flask_app.config["SERVER_NAME"] = "localhost"
    return flask_app

@pytest.fixture(autouse=True)
def db_session(app):
    with app.app_context():
        db.create_all()
        yield db.session
        db.session.remove()
        db.drop_all()

@pytest.fixture
def test_data(db_session):
    # Locations
    wh = Location(name_ar="المستودع المركزي", name_en="Central Warehouse", type="warehouse", foodics_id="wh_01")
    br1 = Location(name_ar="فرع العليا", name_en="Olaya Branch", type="branch", foodics_id="br_01")
    br2 = Location(name_ar="فرع النخيل", name_en="Nakheel Branch", type="branch", foodics_id="br_02")
    db_session.add_all([wh, br1, br2])
    db_session.flush()

    # Users
    admin = User(name="مدير الاختبار", role="admin", active=True)
    admin.set_password("AdminPass123!")

    accountant = User(name="محاسب الاختبار", role="accountant", active=True)
    accountant.set_password("AcctPass123!")

    wh_user = User(name="أمين المستودع", role="warehouse", location_id=wh.id, active=True)
    wh_user.set_password("WhPass123!")

    br1_user = User(name="موظف العليا", role="branch_staff", location_id=br1.id, active=True)
    br1_user.set_password("Branch1Pass!")

    br2_user = User(name="موظف النخيل", role="branch_staff", location_id=br2.id, active=True)
    br2_user.set_password("Branch2Pass!")

    # Legacy plaintext password user for upgrade test
    legacy_user = User(name="مستخدم قديم", role="branch_staff", location_id=br1.id, password="old_plain_text_123", active=True)

    db_session.add_all([admin, accountant, wh_user, br1_user, br2_user, legacy_user])
    db_session.flush()

    # Supplier
    sup = Supplier(name="شركة البن الفاخر", code="SUP-001", phone="0501112233")
    db_session.add(sup)
    db_session.flush()

    # Items
    item_coffee = Item(
        sku="SKU-COF-01",
        name_ar="حبوب قهوة كولومبي",
        name_en="Colombian Coffee Beans",
        storage_unit="كجم",
        cost=60.0,
        barcode="62810001001",
        foodics_id="fdx_c01"
    )
    item_milk = Item(
        sku="SKU-MLK-01",
        name_ar="حليب مبستر",
        name_en="Pasteurized Milk",
        storage_unit="لتر",
        cost=5.5,
        barcode="62810001002",
        foodics_id="fdx_m01"
    )
    db_session.add_all([item_coffee, item_milk])
    db_session.flush()

    # Initial stock balances
    record_stock_movement(item_coffee.id, wh.id, "initial", 100.0, notes="رصيد افتتاحي", user_id=admin.id)
    record_stock_movement(item_milk.id, wh.id, "initial", 200.0, notes="رصيد افتتاحي", user_id=admin.id)
    record_stock_movement(item_coffee.id, br1.id, "initial", 15.0, notes="رصيد افتتاحي", user_id=admin.id)

    db_session.commit()

    return {
        "wh": wh,
        "br1": br1,
        "br2": br2,
        "admin": admin,
        "accountant": accountant,
        "wh_user": wh_user,
        "br1_user": br1_user,
        "br2_user": br2_user,
        "legacy_user": legacy_user,
        "supplier": sup,
        "item_coffee": item_coffee,
        "item_milk": item_milk
    }

def login_client(client, user_id):
    with client.session_transaction() as sess:
        sess["user_id"] = user_id

@pytest.fixture
def client(app):
    return app.test_client()

@pytest.fixture
def admin_client(app, test_data):
    c = app.test_client()
    login_client(c, test_data["admin"].id)
    return c

@pytest.fixture
def accountant_client(app, test_data):
    c = app.test_client()
    login_client(c, test_data["accountant"].id)
    return c

@pytest.fixture
def warehouse_client(app, test_data):
    c = app.test_client()
    login_client(c, test_data["wh_user"].id)
    return c

@pytest.fixture
def branch1_client(app, test_data):
    c = app.test_client()
    login_client(c, test_data["br1_user"].id)
    return c

@pytest.fixture
def branch2_client(app, test_data):
    c = app.test_client()
    login_client(c, test_data["br2_user"].id)
    return c

