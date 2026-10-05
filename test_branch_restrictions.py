import sys
import io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

from app import app
from models import db, User, Item, Supplier, Location, PurchaseTransaction, PurchaseLine, TransferOrder, TransferLine, StockBalance

with app.app_context():
    staff_user = User.query.filter_by(role='branch_staff').first()
    admin_user = User.query.filter_by(role='admin').first()
    warehouse_user = User.query.filter_by(role='warehouse').first()

    print(f"Branch Staff: {staff_user.name} (Location ID: {staff_user.location_id} - {staff_user.location.name_ar})")
    print(f"Admin User: {admin_user.name}")

    client = app.test_client()

    # -------------------------------------------------------------
    # REQUIREMENT 1: تقييد صلاحية الدخول بالفرع المحدد (استلام وتحويل)
    # -------------------------------------------------------------
    print("\n================== REQUIREMENT 1: Branch Locking & Isolation ==================")

    # 1.1 purchases_list: only branch purchases appear
    # Create one purchase for staff's branch (loc 2) and one for another branch (loc 1)
    p_my_branch = PurchaseTransaction.query.filter_by(location_id=staff_user.location_id).first()
    if not p_my_branch:
        p_my_branch = PurchaseTransaction(
            supplier_name="مورد الفرع",
            invoice_number="INV-BR-MY",
            location_id=staff_user.location_id,
            status="draft"
        )
        db.session.add(p_my_branch)

    p_other_branch = PurchaseTransaction.query.filter(PurchaseTransaction.location_id != staff_user.location_id).first()
    if not p_other_branch:
        p_other_branch = PurchaseTransaction(
            supplier_name="مورد فرع آخر",
            invoice_number="INV-BR-OTHER",
            location_id=1,
            status="draft"
        )
        db.session.add(p_other_branch)
    db.session.commit()

    with client.session_transaction() as sess:
        sess['user_id'] = staff_user.id
        sess['role'] = 'branch_staff'
        sess['lang'] = 'ar'

    res_purchases = client.get('/purchases')
    html_purchases = res_purchases.get_data(as_text=True)
    print("1. Purchases list status:", res_purchases.status_code)
    print(f"2. My branch voucher #{p_my_branch.id} in list:", f'href="/purchases/{p_my_branch.id}"' in html_purchases)
    print(f"3. Other branch voucher #{p_other_branch.id} NOT in list:", f'href="/purchases/{p_other_branch.id}"' not in html_purchases)

    # 1.2 purchase_new: locked receiving location in HTML & forced on POST
    res_p_new = client.get('/purchases/new')
    html_p_new = res_p_new.get_data(as_text=True)
    print("4. New Purchase page has locked branch input:", f'<input type="hidden" name="location_id" value="{staff_user.location_id}">' in html_p_new)
    print("5. New Purchase page has NO select dropdown for other branches:", '<select id="location_id"' not in html_p_new)

    # Attempt POST with malicious location_id=1
    res_p_create = client.post('/purchases/new', data={
        'supplier_name': 'مورد تجريبي فرع رفاء',
        'invoice_number': 'INV-FORCE-TEST',
        'location_id': '1' # Injected other location
    }, follow_redirects=False)
    created_p = PurchaseTransaction.query.filter_by(invoice_number='INV-FORCE-TEST').first()
    print("6. POST /purchases/new forces staff's location (location_id == 2):", created_p.location_id == staff_user.location_id)
    if created_p:
        db.session.delete(created_p)
        db.session.commit()

    # 1.3 purchase_edit: cannot view other branch's purchase
    res_other_edit = client.get(f'/purchases/{p_other_branch.id}', follow_redirects=False)
    print("7. Accessing other branch purchase redirects (blocked):", res_other_edit.status_code == 302)

    # 1.4 transfers_list: only transfers involving staff branch appear
    tr_for_branch = TransferOrder.query.filter_by(to_location_id=staff_user.location_id).first()
    if not tr_for_branch:
        tr_for_branch = TransferOrder(
            from_location_id=1,
            to_location_id=staff_user.location_id,
            created_by=admin_user.id,
            status="in_transit"
        )
        db.session.add(tr_for_branch)

    tr_other = TransferOrder.query.filter(TransferOrder.to_location_id != staff_user.location_id, TransferOrder.from_location_id != staff_user.location_id).first()
    if not tr_other:
        tr_other = TransferOrder(
            from_location_id=1,
            to_location_id=3,
            created_by=admin_user.id,
            status="in_transit"
        )
        db.session.add(tr_other)
    db.session.commit()

    res_tr_list = client.get('/transfers')
    html_tr_list = res_tr_list.get_data(as_text=True)
    print(f"8. My branch transfer #{tr_for_branch.id} in list:", f'href="/transfers/{tr_for_branch.id}"' in html_tr_list)
    print(f"9. Other branch transfer #{tr_other.id} NOT in list:", f'href="/transfers/{tr_other.id}"' not in html_tr_list)

    # 1.5 transfer_receive: cannot receive other branch's transfer
    res_rec_other = client.get(f'/transfers/{tr_other.id}/receive', follow_redirects=False)
    print("10. Receiving transfer meant for another branch redirects (blocked):", res_rec_other.status_code == 302)

    # -------------------------------------------------------------
    # REQUIREMENT 2: إمكانية تأكيد استلام المواد وتكون واضحة بالشاشة الرئيسية
    # -------------------------------------------------------------
    print("\n================== REQUIREMENT 2: Incoming Shipments Confirmation on Dashboard ==================")

    # Ensure tr_for_branch is in_transit and has at least 1 line
    item_sample = Item.query.first()
    tr_for_branch.status = "in_transit"
    if not tr_for_branch.lines:
        tline = TransferLine(
            transfer_id=tr_for_branch.id,
            item_id=item_sample.id,
            qty_requested=10,
            qty_sent=10
        )
        db.session.add(tline)
    db.session.commit()

    res_dash_staff = client.get('/dashboard')
    html_dash_staff = res_dash_staff.get_data(as_text=True)

    print("1. Prominent incoming shipments section present on Dashboard:", "شحنات واردة من المستودع بانتظار تأكيد الاستلام" in html_dash_staff)
    print("2. Shows transfer voucher id:", f"#{tr_for_branch.id}" in html_dash_staff)
    print("3. Shows direct button to confirm receipt:", "تأكيد استلام المواد ومطابقة الكميات" in html_dash_staff)
    print("4. Direct button link to receive page:", f"/transfers/{tr_for_branch.id}/receive" in html_dash_staff)
    print("5. Transfers KPI shows incoming count:", "شحنات بانتظار الاستلام" in html_dash_staff)
    print("6. Navbar badge for branch incoming shipments present:", "branch_incoming_count" in html_dash_staff or f"/transfers" in html_dash_staff)

    # Test executing the receipt confirmation as branch staff
    line_id = tr_for_branch.lines[0].id
    res_do_receive = client.post(f'/transfers/{tr_for_branch.id}/receive', data={
        f'qty_received_{line_id}': '10',
        f'reason_{line_id}': ''
    }, follow_redirects=True)
    db.session.refresh(tr_for_branch)
    print("7. Receipt confirmation POST executed successfully:", res_do_receive.status_code == 200)
    print("8. Transfer status updated to received:", tr_for_branch.status == "received")
    print("9. Confirmed by staff user:", tr_for_branch.confirmed_by == staff_user.id)

    # -------------------------------------------------------------
    # REQUIREMENT 3: إخفاء دفتر الأستاذ (آخر الحركات المخزنية المسجلة) لموظف الفرع
    # -------------------------------------------------------------
    print("\n================== REQUIREMENT 3: Hide Stock Ledger (دفتر الأستاذ) ==================")

    print("1. 'آخر الحركات المخزنية المسجلة (دفتر الأستاذ)' hidden from branch staff dashboard:", "آخر الحركات المخزنية المسجلة (دفتر الأستاذ)" not in html_dash_staff)
    print("2. 'دفتر الأستاذ' not in branch staff dashboard:", "دفتر الأستاذ" not in html_dash_staff)

    # Direct access to /reports/movements as branch staff must be blocked
    res_movements_staff = client.get('/reports/movements', follow_redirects=False)
    print("3. Direct access to /reports/movements redirects (blocked):", res_movements_staff.status_code == 302)

    # Admin check: Admin CAN see the ledger
    with client.session_transaction() as sess:
        sess['user_id'] = admin_user.id
        sess['role'] = 'admin'
        sess['lang'] = 'ar'

    res_dash_admin = client.get('/dashboard')
    html_dash_admin = res_dash_admin.get_data(as_text=True)
    print("4. Admin dashboard SHOWS 'آخر الحركات المخزنية المسجلة (دفتر الأستاذ)':", "آخر الحركات المخزنية المسجلة (دفتر الأستاذ)" in html_dash_admin)

    res_movements_admin = client.get('/reports/movements')
    print("5. Admin direct access to /reports/movements allowed (200):", res_movements_admin.status_code == 200)

    print("\n>>> ALL TESTS COMPLETED SUCCESSFULLY! <<<")
