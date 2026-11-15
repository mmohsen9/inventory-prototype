"""
migrate_branch_purchase_costs.py
تحديث تكلفة سطور سندات الاستلام (المشتريات) الناتجة عن سندات صرف البن المحمص للفروع لتطابق التكلفة الحقيقية في سند الصرف الأصلي بدلاً من 40.0 ر.س.
"""
from app import app
from models import db, PurchaseTransaction, PurchaseLine
from models_coffee import RoastedCoffeeDispatch

def migrate():
    with app.app_context():
        purchases = PurchaseTransaction.query.filter(
            PurchaseTransaction.supplier_name.like("%اكياس القهوة المحمصه للفروع%")
        ).all()
        updated_lines = 0
        for p in purchases:
            disp = RoastedCoffeeDispatch.query.filter(
                (RoastedCoffeeDispatch.doc_number == p.invoice_number) |
                (RoastedCoffeeDispatch.doc_number == p.foodics_reference)
            ).first()
            if not disp:
                continue
            
            d_lines = list(disp.lines)
            p_lines = list(p.lines)
            if len(d_lines) == len(p_lines):
                for dl, pl in zip(d_lines, p_lines):
                    # التكلفة المعتمدة من سند الصرف (إذا كانت الدفعة السابقة مشوهة 300.3 نستخدم cost_price المعتمد)
                    target_cost = dl.coffee_item.cost_price if dl.unit_cost == 300.3 else (dl.unit_cost or dl.coffee_item.cost_price)
                    if target_cost and round(pl.unit_cost, 2) != round(target_cost, 2):
                        print(f"Updating Purchase #{p.id} ({p.invoice_number}) Line #{pl.id}: {pl.unit_cost} -> {target_cost}")
                        pl.unit_cost = round(target_cost, 4)
                        updated_lines += 1

        db.session.commit()
        print(f"Migration completed. Updated {updated_lines} purchase lines.")

if __name__ == "__main__":
    migrate()

