"""
تحميل البيانات الأولية: الأصناف من ملف تصدير فوديكس + المواقع + المستخدمون التجريبيون
تشغيل مرة واحدة: python seed_data.py
"""
import os
import csv
from app import app
from models import db, Item, Location, User

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ITEMS_CSV = os.path.join(BASE_DIR, "items_export.csv")

LOCATIONS = [
    ("المستودع الرئيسي", "Warehouse 1", "warehouse"),
    ("رفاء", "RAFA", "branch"),
    ("التخصصي", "Takhassussi- HMG", "branch"),
    ("هيتن", "Hitten-branch", "branch"),
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
    if User.query.count() > 0:
        print("المستخدمون موجودون مسبقاً، تخطي.")
        return
    warehouse_loc = Location.query.filter_by(type="warehouse").first()
    branch_loc = Location.query.filter_by(type="branch").first()

    users = [
        User(name="أمين المستودع", role="warehouse", location_id=warehouse_loc.id if warehouse_loc else None, password="1234"),
        User(name="موظف الفرع", role="branch_staff", location_id=branch_loc.id if branch_loc else None, password="1234"),
        User(name="المحاسب", role="accountant", location_id=None, password="1234"),
    ]
    for u in users:
        db.session.add(u)
    db.session.commit()
    print("تم إنشاء 3 مستخدمين تجريبيين (كلمة المرور: 1234).")


if __name__ == "__main__":
    with app.app_context():
        db.create_all()
        seed_locations()
        seed_items()
        seed_users()
    print("اكتمل تحميل البيانات الأولية.")
