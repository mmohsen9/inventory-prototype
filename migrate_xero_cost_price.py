"""
migrate_xero_cost_price.py — ترحيل وتثبيت أسعار تكلفة الأصناف من Xero
يضيف عمود xero_cost_price لجدول coffee_items ويحدث أسعار التكلفة لجميع الأصناف الـ 21،
ويصحح تكلفة السند MP-00001 إلى سعر التكلفة المعتمد على زيرو.
"""
import json
from app import app
from models import db, Item
from models_coffee import CoffeeItem, RoastedCoffeeDispatch, RoastedCoffeeDispatchLine

OFFICIAL_XERO_COSTS = {
    "GB1007": 18.25,
    "GB22025": 48.60,
    "GB52023": 24.50,
    "GB0617": 35.20,
    "GB0377": 40.75,
    "GB52025": 39.57,
    "GB32025": 47.77,
    "GB12025": 45.29,
    "GB0620": 50.24,
    "GB0619": 35.20,
    "GB002A": 40.61,
    "GB00466": 250.25,
    "GB42025": 30.40,
    "RB0617": 42.50,
    "RB12025": 54.50,
    "RB22025": 58.50,
    "RB52025": 47.50,
    "RB42025": 37.00,
    "RB0619": 42.50,
    "RB0465": 49.50,
    "RB32025": 57.50,
}

def run_migration():
    with app.app_context():
        # 1. فحص وإضافة العمود لقاعدة البيانات
        cols = [c[1] for c in db.session.execute(db.text("PRAGMA table_info(coffee_items)")).fetchall()]
        if "xero_cost_price" not in cols:
            db.session.execute(db.text("ALTER TABLE coffee_items ADD COLUMN xero_cost_price FLOAT DEFAULT 0.0"))
            db.session.commit()
            print("Added column xero_cost_price to coffee_items table.")
        else:
            print("Column xero_cost_price already exists.")

        # 2. تحديث الأصناف الـ 21 بتكلفة زيرو الرسمية
        updated_items = 0
        for ci in CoffeeItem.query.all():
            code_key = f"{ci.prefix}{ci.base_code}".upper()
            cost = OFFICIAL_XERO_COSTS.get(code_key)
            if cost is None:
                # محاولة بالـ base_code فقط
                cost = OFFICIAL_XERO_COSTS.get(f"{ci.prefix}{ci.base_code}")
            if cost is not None:
                ci.xero_cost_price = cost
                if ci.item:
                    ci.item.cost = cost
                    db.session.add(ci.item)
                db.session.add(ci)
                updated_items += 1
                print(f"Updated {code_key}: xero_cost_price = {cost}")

        db.session.commit()
        print(f"Successfully updated {updated_items} coffee items with official Xero costs.")

        # 3. تصحيح السند MP-00001
        doc = RoastedCoffeeDispatch.query.filter_by(doc_number="MP-00001").first()
        if doc:
            ci = CoffeeItem.query.get(doc.coffee_item_id) if doc.coffee_item_id else None
            correct_cost = ci.cost_price if ci else 49.50
            correct_total = round(float(doc.qty_kg or 0.0) * correct_cost, 4)
            doc.unit_cost = correct_cost
            doc.total_cost = correct_total

            for line in doc.lines:
                line_ci = line.coffee_item
                l_cost = line_ci.cost_price if line_ci else correct_cost
                line.unit_cost = l_cost
                line.total_cost = round(float(line.qty_kg or 0.0) * l_cost, 4)
                db.session.add(line)

            db.session.add(doc)
            db.session.commit()
            print(f"Updated MP-00001: unit_cost = {correct_cost}, total_cost = {correct_total}")

if __name__ == "__main__":
    run_migration()

