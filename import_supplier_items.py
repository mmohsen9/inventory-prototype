import csv
import os
from app import app, db, Supplier, Item, ItemSupplier

def run_import():
    with app.app_context():
        # 1. Create table if not exists
        db.create_all()

        csv_path = os.path.join(os.path.dirname(__file__), 'supplier_items_clean.csv')
        if not os.path.exists(csv_path):
            print(f"Error: {csv_path} not found")
            return

        with open(csv_path, 'r', encoding='utf-8') as f:
            rows = list(csv.DictReader(f))

        print(f"Found {len(rows)} rows to process.")

        # Cache existing suppliers and items
        suppliers_by_name = {s.name.strip(): s for s in Supplier.query.all()}
        suppliers_by_code = {s.code.strip(): s for s in Supplier.query.all() if s.code}
        items_by_sku = {i.sku.strip().lower(): i for i in Item.query.all()}

        imported_count = 0
        new_items_count = 0
        new_suppliers_count = 0

        for r in rows:
            s_name = (r.get('supplier_name') or '').strip()
            s_code = (r.get('supplier_code') or '').strip()
            sku = (r.get('inventory_item_sku') or '').strip()
            sku_lower = sku.lower()
            name_en = (r.get('inventory_item_name') or '').strip()
            name_ar = (r.get('inventory_item_name_localized') or '').strip() or name_en
            order_unit = (r.get('order_unit') or '').strip()
            
            try:
                order_to_storage = float(r.get('order_to_storage') or 1.0)
            except Exception:
                order_to_storage = 1.0

            try:
                order_qty = float(r.get('order_quantity') or 0.0) if r.get('order_quantity') else None
            except Exception:
                order_qty = None

            try:
                cost_per_unit = float(r.get('cost_per_order_unit') or 0.0) if r.get('cost_per_order_unit') else None
            except Exception:
                cost_per_unit = None

            item_supplier_code = (r.get('item_supplier_code') or '').strip()

            if not s_name:
                continue

            # 1. Resolve Supplier
            sup = suppliers_by_name.get(s_name) or (suppliers_by_code.get(s_code) if s_code else None)
            if not sup:
                # Search partial
                for k, v in suppliers_by_name.items():
                    if k.lower() == s_name.lower() or s_name.lower() in k.lower():
                        sup = v
                        break
            if not sup:
                sup = Supplier(name=s_name, code=s_code or None)
                db.session.add(sup)
                db.session.flush()
                suppliers_by_name[s_name] = sup
                if s_code:
                    suppliers_by_code[s_code] = sup
                new_suppliers_count += 1
            elif s_code and not sup.code:
                sup.code = s_code

            # 2. Resolve Item
            if not sku:
                continue

            it = items_by_sku.get(sku_lower)
            if not it:
                it = Item(
                    sku=sku,
                    name_en=name_en or sku,
                    name_ar=name_ar or name_en or sku,
                    storage_unit=order_unit or 'حبة',
                    cost=cost_per_unit or 0.0,
                    sync_status='synced'
                )
                db.session.add(it)
                db.session.flush()
                items_by_sku[sku_lower] = it
                new_items_count += 1
            else:
                # Update cost if 0
                if (not it.cost or it.cost == 0) and cost_per_unit and cost_per_unit > 0:
                    it.cost = cost_per_unit

            # 3. Create or update ItemSupplier link
            link = ItemSupplier.query.filter_by(item_id=it.id, supplier_id=sup.id).first()
            if not link:
                link = ItemSupplier(
                    item_id=it.id,
                    supplier_id=sup.id,
                    order_unit=order_unit or it.storage_unit,
                    order_to_storage=order_to_storage,
                    order_quantity=order_qty,
                    cost_per_order_unit=cost_per_unit,
                    item_supplier_code=item_supplier_code or None
                )
                db.session.add(link)
                imported_count += 1
            else:
                if order_unit and not link.order_unit:
                    link.order_unit = order_unit
                if cost_per_unit and not link.cost_per_order_unit:
                    link.cost_per_order_unit = cost_per_unit
                if item_supplier_code and not link.item_supplier_code:
                    link.item_supplier_code = item_supplier_code

        db.session.commit()
        total_links = ItemSupplier.query.count()
        print(f"Import finished successfully!")
        print(f"- New suppliers created: {new_suppliers_count}")
        print(f"- New items created: {new_items_count}")
        print(f"- New links added: {imported_count}")
        print(f"- Total item-supplier mappings in DB: {total_links}")

if __name__ == '__main__':
    run_import()
