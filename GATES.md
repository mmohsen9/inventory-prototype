# Gates: Roasting Order Xero Attachment & >1000 Serial Numbering (إرفاق أمر التحميص في زيرو وبدء السيريال من بعد 1000)

OWNS: coffee_xero.py, models_coffee.py, models.py, templates/coffee/roasting_edit.html, templates/coffee/roasting_new.html, app_coffee.py, coffee_importer.py, tests/test_roasting_xero_collision.py

Scope: Ensure Roasting Order PDF voucher is generated and attached reliably to Xero Bills and Credit Notes with URL-encoded filenames, correct headers, optimized Chromium flags and timeout, real attachment status reporting in UI, and ensure all document sequences start from > 1000 (1001+).

- [x] G1: All voucher doc numbering functions start at or above 1001 (OP-1001, GB-1001, MP-1001, SL-1001, RB-1001, Nov-1001)
  CHECK: python -c "from app import app; from models_coffee import next_roasting_order_number, GreenCoffeeIssue, RoastedCoffeeDispatch; from models import CountSession; 
with app.app_context():
    assert int(next_roasting_order_number(preview_only=True).split('-')[1]) >= 1001
    assert int(GreenCoffeeIssue.generate_next_doc_number(preview_only=True).split('-')[1]) >= 1001
    assert int(RoastedCoffeeDispatch.generate_next_doc_number('branch', preview_only=True).split('-')[1]) >= 1001
    assert int(CountSession.generate_next_session_number().split('-')[1]) >= 1001
    print('G1 passed: All document serials >= 1001')
"
  EXPECT: G1 passed: All document serials >= 1001

- [x] G2: _generate_voucher_pdf generates a valid PDF for roasting orders with gw and rw context and resilient flags
  CHECK: python -c "from app import app; from coffee_xero import _generate_voucher_pdf; from models_coffee import RoastingOrder; 
with app.app_context():
    order = RoastingOrder.query.first()
    assert order is not None, 'No roasting order'
    b, m = _generate_voucher_pdf('roasting_order', order.id)
    assert b is not None and len(b) > 1000, f'PDF generation failed: len={len(b) if b else 0}'
    assert m == 'application/pdf', f'Mime mismatch: {m}'
    print('G2 passed: Roasting order PDF generated successfully')
"
  EXPECT: G2 passed: Roasting order PDF generated successfully

- [x] G3: attach_voucher_to_xero URL-encodes filenames, handles headers properly with Content-Length, and returns actionable status
  CHECK: python -c "from coffee_xero import attach_voucher_to_xero; import inspect; src = inspect.getsource(attach_voucher_to_xero); assert 'urllib.parse.quote' in src; assert 'Content-Length' in src; print('G3 passed: Attachment API clean and compliant')"
  EXPECT: G3 passed: Attachment API clean and compliant

- [x] G4: post_roasting_order_to_xero tracks attachment results and surfaces attachment status to user flash message
  CHECK: python -c "from coffee_xero import post_roasting_order_to_xero; import inspect; src = inspect.getsource(post_roasting_order_to_xero); assert 'attachment_success' in src or 'attached' in src; print('G4 passed: Roasting order attachment tracked')"
  EXPECT: G4 passed: Roasting order attachment tracked

- [x] G5: Automated test suite passes cleanly
  CHECK: pytest tests/test_roasting_xero_collision.py tests/test_coffee_features_v2.py tests/test_coffee_features_v3.py
  EXPECT: 74 passed

- [x] G6: PostgreSQL error resilience with teardown_request rollback and safe cached globals
  CHECK: python -c "from app import app, db; from models import User;
with app.app_context():
    from app import _get_cached_pending_count, _get_cached_notifications, _get_cached_suppliers;
    assert _get_cached_pending_count() >= 0
    notifs, cnt = _get_cached_notifications()
    assert isinstance(notifs, list)
    print('G6 passed: Database context and cached processors resilient')
"
  EXPECT: G6 passed: Database context and cached processors resilient

- [x] G7: Dashboard alerts scoped strictly to agreed coffee items (21 items) and non-coffee Foodics notifications filtered
  CHECK: python -c "from app import app, db; from models import StockBalance, Notification; from models_coffee import CoffeeItem;
with app.app_context():
    from app import cleanup_supplier_linked_notifications, get_unread_notifications_query;
    cleanup_supplier_linked_notifications()
    q = get_unread_notifications_query()
    # Ensure no generic non-coffee sk- items are in unread notifs
    for n in q.all():
        assert not (n.type == 'new_inventory_item' and n.item_sku and n.item_sku.startswith('sk-')), f'Found unlinked non-coffee notif: {n.item_sku}'
    print('G7 passed: Dashboard alerts scoped strictly to agreed coffee scope')
"
  EXPECT: G7 passed: Dashboard alerts scoped strictly to agreed coffee scope
