import sys
import io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
from app import app, db, User, Supplier, ItemSupplier, Item, PurchaseTransaction, find_supplier_by_name

with app.app_context():
    print("=== 1. Testing Supplier Normalization ===")
    sup1 = find_supplier_by_name('مصنع الكوب الذهبي للمنتجات الورقية')
    sup2 = find_supplier_by_name('مصنع الكوب الذهبى للمنتجات الورقية')
    sup3 = find_supplier_by_name('الكوب الذهبي')
    assert sup1 is not None and sup1.id == 52, f"Failed sup1: {sup1}"
    assert sup2 is not None and sup2.id == 52, f"Failed sup2: {sup2}"
    assert sup3 is not None and sup3.id == 52, f"Failed sup3: {sup3}"
    print("  [PASS] All 3 variations found Supplier 52!")

    print("\n=== 2. Testing API /api/search_items with Golden Cup (focused, no query) ===")
    client = app.test_client()
    admin = User.query.filter_by(role='admin').first()
    with client.session_transaction() as s:
        s['user_id'] = admin.id
        s['role'] = 'admin'

    res = client.get('/api/search_items?supplier=مصنع%20الكوب%20الذهبي%20للمنتجات%20الورقية')
    data = res.get_json()
    items = data.get('results', [])
    print(f"  Returned {len(items)} items")
    assert len(items) == 13, f"Expected 13 items, got {len(items)}"
    assert data.get('is_supplier_filtered') is True
    assert data.get('has_supplier_items') is True
    for it in items:
        assert 'كيك' not in it['name_ar'] and 'شاي' not in it['name_ar'], f"Invalid item: {it}"
        sku = it['sku']
        name = it['name_ar']
        unit = it['unit']
        cost = it['cost']
        print(f"    - [{sku}] {name} ({unit}) @ {cost} SAR")
    print("  [PASS] 13 packaging items returned, 0 random cakes/teas!")

    print("\n=== 3. Testing API with an unlinked supplier (no items) ===")
    res_empty = client.get('/api/search_items?supplier=شركة%20بعد%20للأكواب')
    data_empty = res_empty.get_json()
    assert len(data_empty.get('results', [])) == 0, "Expected 0 items for unlinked supplier"
    assert data_empty.get('has_supplier_items') is False
    print(f"  Message: {data_empty.get('message')}")
    print("  [PASS] Unlinked supplier cleanly returns 0 items without random fallback!")

    print("\n=== 4. Testing purchase_edit View with Golden Cup ===")
    p = PurchaseTransaction.query.filter(PurchaseTransaction.supplier_name.ilike('%الكوب%')).first()
    if not p:
        p = PurchaseTransaction(
            supplier_name='مصنع الكوب الذهبي للمنتجات الورقية',
            invoice_number='INV-CUP-01',
            location_id=1,
            created_by=admin.id,
            status='draft'
        )
        db.session.add(p)
        db.session.commit()

    res_view = client.get(f'/purchases/{p.id}')
    html = res_view.get_data(as_text=True)
    assert 'أصناف المورد المعتمدة (13 صنف' in html, "Expected approved items banner with 13 items"
    assert 'quick_supplier_item_select' in html
    assert 'اكواب 12 اونز جديد' in html
    assert 'مناديل طاولة' in html
    print("  [PASS] Purchase edit HTML displays 13 approved supplier items and quick select!")

    print("\nALL VERIFICATION CHECKS PASSED PERFECTLY!")
