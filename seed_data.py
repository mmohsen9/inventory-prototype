"""
تحميل البيانات الأولية: الأصناف من ملف تصدير فوديكس + المواقع + المستخدمون التجريبيون + الأرصدة الافتتاحية
تشغيل مرة واحدة: python seed_data.py
"""
import os
import csv
import random
from app import app
from models import db, Item, Location, User, StockBalance, record_stock_movement

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ITEMS_CSV = os.path.join(BASE_DIR, "items_export.csv")

LOCATIONS = [
    ("المستودع الرئيسي", "Warehouse 1", "warehouse"),
    ("رفاء", "RAFA", "branch"),
    ("التخصصي", "Takhassussi- HMG", "branch"),
    ("حطين", "Hitten-branch", "branch"),
    ("الصحافة", "Alsahafa - HMG", "branch"),
    ("النزهة", "Alnuzha", "branch"),
    ("الياسمين", "Alyasamin -Brunch", "branch"),
]


def seed_locations():
    if Location.query.count() > 0:
        print("المواقع موجودة مسبقاً، تخطي.")
        return
    for name_ar, name_en, type_ in LOCATIONS:
        db.session.add(Location(name_ar=name_ar, name_en=name_en, type=type_))
    db.session.commit()
    print(f"تمت إضافة {len(LOCATIONS)} موقعاً.")


def seed_items():
    if not os.path.exists(ITEMS_CSV):
        print(f"تحذير: لم يتم العثور على {ITEMS_CSV} — ضع نسخة تصدير أصناف فوديكس بهذا الاسم.")
        return
    if Item.query.count() > 0:
        print("الأصناف موجودة مسبقاً، تخطي. (احذف قاعدة البيانات لإعادة التحميل)")
        return

    count = 0
    with open(ITEMS_CSV, encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            sku = (row.get("sku") or "").strip()
            if not sku:
                continue
            try:
                cost = float(row.get("cost") or 0)
            except ValueError:
                cost = 0
            try:
                factor = float(row.get("storage_to_ingredient_factor") or 1)
            except ValueError:
                factor = 1
            item = Item(
                foodics_id=row.get("id"),
                sku=sku,
                name_en=row.get("name"),
                name_ar=row.get("name_localized") or row.get("name"),
                storage_unit=row.get("storage_unit"),
                ingredient_unit=row.get("ingredient_unit"),
                storage_to_ingredient_factor=factor,
                cost=cost,
                barcode=row.get("barcode") or None,
                category_reference=row.get("category_reference") or None,
                active_branches="[]",  # فارغة = مفعّل في كل الفروع افتراضياً بالنموذج الأولي
                sync_status="synced",
            )
            db.session.add(item)
            count += 1
    db.session.commit()
    print(f"تم تحميل {count} صنفاً من {ITEMS_CSV}.")


def seed_users():
    warehouse_loc = Location.query.filter_by(type="warehouse").first()
    branch_loc = Location.query.filter_by(type="branch").first()

    default_users = [
        ("مدير النظام", "admin", None, "admin"),
        ("أمين المستودع", "warehouse", warehouse_loc.id if warehouse_loc else None, "1234"),
        ("موظف الفرع", "branch_staff", branch_loc.id if branch_loc else None, "1234"),
        ("المحاسب", "accountant", None, "1234"),
    ]

    created = 0
    for name, role, loc_id, pwd in default_users:
        existing = User.query.filter_by(name=name).first()
        if not existing:
            db.session.add(User(name=name, role=role, location_id=loc_id, password=pwd, active=True))
            created += 1
    db.session.commit()
    if created:
        print(f"تم إنشاء {created} مستخدمين تجريبيين جدد.")
    else:
        print("المستخدمون موجودون مسبقاً.")


def seed_initial_stock():
    if StockBalance.query.count() > 0:
        print("أرصدة المخزون موجودة مسبقاً، تخطي.")
        return

    locations = Location.query.all()
    items = Item.query.limit(30).all()
    if not locations or not items:
        return

    admin_user = User.query.filter_by(role="admin").first()
    admin_id = admin_user.id if admin_user else None

    count = 0
    for loc in locations:
        for it in items:
            base_qty = 120.0 if loc.type == "warehouse" else 25.0
            qty = round(base_qty * (0.4 + random.random()), 1)
            record_stock_movement(
                item_id=it.id,
                location_id=loc.id,
                movement_type="initial",
                quantity_change=qty,
                notes="رصيد افتتاحي تمهيدي للتشغيل",
                user_id=admin_id,
            )
            count += 1

    db.session.commit()
    print(f"تم توليد {count} رصيداً افتتاحي في المستودعات والفروع.")


if __name__ == "__main__":
    with app.app_context():
        db.create_all()
        seed_locations()
        seed_items()
        seed_users()
        seed_initial_stock()
    print("اكتملت تهيئة وتحميل كافة البيانات بنجاح.")
