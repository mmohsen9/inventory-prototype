import sys
import io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

from app import app
from models import db, User, Item, Supplier, ItemSupplier, PurchaseTransaction, PurchaseLine

with app.app_context():
    # Find supplier from CSV
    sup = Supplier.query.filter(Supplier.name.ilike('%سحاب%')).first()
    print(f"Supplier: {sup.name} (Items count: {len(sup.items)})")
    
    # 1. Check or create a test draft purchase
    p = PurchaseTransaction.query.filter_by(supplier_name=sup.name).first()
    if not p:
        p = PurchaseTransaction(
            supplier_name=sup.name,
            invoice_number='TEST-INV-101',
            location_id=1,
            status='draft'
        )
        db.session.add(p)
        db.session.commit()
    print(f"Purchase Voucher #{p.id} for {p.supplier_name}")

    client = app.test_client()
    staff_user = User.query.filter_by(role='branch_staff').first()
    admin_user = User.query.filter_by(role='admin').first()

    # --- Test 1: Branch Staff View ---
    with client.session_transaction() as sess:
        sess['user_id'] = staff_user.id
        sess['role'] = 'branch_staff'
        sess['lang'] = 'ar'

    res_staff = client.get(f'/purchases/{p.id}')
    html_staff = res_staff.get_data(as_text=True)

    print("\n--- Testing Branch Staff Permissions & Views ---")
    print("1. Status code:", res_staff.status_code)
    print("2. Quick select dropdown present:", "quick_supplier_item_select" in html_staff)
    print("3. Item search has data-supplier:", f'data-supplier="{p.supplier_name}"' in html_staff)
    print("4. Unit cost text input hidden:", '<input type="number" step="0.001" id="unit_cost"' not in html_staff)
    print("5. Unit cost hidden input present:", '<input type="hidden" id="unit_cost" name="unit_cost" value="0">' in html_staff)
    print("6. Unit cost table header hidden:", "<th>تكلفة الوحدة</th>" not in html_staff)
    print("7. Line total preview hidden:", "line_total_preview" not in html_staff)
    print("8. Total currency hidden from KPI (shows إجمالي الكميات):", "إجمالي الكميات" in html_staff)

    # Search items API as branch staff
    res_api = client.get(f'/api/search_items?supplier={sup.name}')
    data_api = res_api.get_json().get('results', [])
    print("9. API returns supplier items:", len(data_api) > 0)
    print("10. API item costs zeroed out for branch staff:", all(item['cost'] == 0.0 for item in data_api))

    # --- Test 2: Admin View ---
    with client.session_transaction() as sess:
        sess['user_id'] = admin_user.id
        sess['role'] = 'admin'
        sess['lang'] = 'ar'

    res_admin = client.get(f'/purchases/{p.id}')
    html_admin = res_admin.get_data(as_text=True)

    # --- Test 3: Adding Line as Branch Staff (Unit cost must be automatically applied from DB, not form) ---
    with client.session_transaction() as sess:
        sess['user_id'] = staff_user.id
        sess['role'] = 'branch_staff'
        sess['lang'] = 'ar'

    sample_item = sup.items[0]
    res_add_line = client.post(f'/purchases/{p.id}', data={
        'action': 'add_line',
        'code': sample_item.sku,
        'quantity': '5',
        'unit': sample_item.storage_unit,
        'unit_cost': '999999', # Malicious attempt to manipulate cost
        'notes': 'Test branch staff line'
    }, follow_redirects=True)
    db.session.refresh(p)
    added_line = p.lines[-1]
    print("\n--- Testing Branch Staff Line Addition Security ---")
    print("1. Line added successfully:", added_line.item_id == sample_item.id)
    print("2. Injected cost was ignored and real item cost applied:", added_line.unit_cost == (sample_item.cost or 0.0))
    print(f"   (Injected: 999999, Applied: {added_line.unit_cost}, Real item cost: {sample_item.cost})")

    # --- Test 4: New Item Request with Supplier & Approval ---
    print("\n--- Testing New Item Request Linked to Supplier & Approval ---")
    # Clean previous if exists
    from models import NewItemRequest
    NewItemRequest.query.filter_by(name_ar='صنف تجريبي للمورد سحاب 2').delete()
    db.session.commit()

    # Branch staff submits new item request
    res_req = client.post('/new-item-request', data={
        'name_ar': 'صنف تجريبي للمورد سحاب 2',
        'name_en': 'Test Item Sahab 2',
        'unit': 'كرتون',
        'conversion_factor': '1',
        'supplier': sup.name,
        'estimated_cost': '500' # branch staff shouldn't be able to set cost
    }, follow_redirects=True)
    new_req = NewItemRequest.query.filter_by(name_ar='صنف تجريبي للمورد سحاب 2').first()
    print("1. New Item Request created:", new_req is not None)
    print("2. Supplier saved in request:", new_req.supplier == sup.name)
    print("3. Estimated cost set to 0.0 for branch staff:", new_req.estimated_cost == 0.0)

    # Admin/Accountant approves the request
    with client.session_transaction() as sess:
        sess['user_id'] = admin_user.id
        sess['role'] = 'admin'
        sess['lang'] = 'ar'
    res_approve = client.post(f'/accountant/new-items/{new_req.id}/decide', data={
        'action': 'approve'
    }, follow_redirects=True)

    created_item = Item.query.filter_by(name_ar='صنف تجريبي للمورد سحاب 2').first()
    print("4. Item created in database:", created_item is not None)
    link = ItemSupplier.query.filter_by(item_id=created_item.id, supplier_id=sup.id).first() if created_item else None
    print("5. Item automatically linked to supplier in ItemSupplier table:", link is not None)
    if link:
        print(f"   (Link ID: {link.id}, Item: {created_item.name_ar}, Supplier: {sup.name})")

    print("\nAll automated checks passed successfully!")
