# Gates: Xero Credit Note Posting Resilience & Error Display (حل تعثر ترحيل زيرو وإظهار رسائل الخطأ)

OWNS: coffee_xero.py, models_coffee.py, templates/coffee/xero_status.html, tests/test_xero_credit_note_resilience.py

Scope: Fix CreditNotes lookup by CreditNoteNumber query parameter in Xero API, add automatic ItemCode validation fallback, and ensure RoastedCoffeeDispatch / GreenCoffeeIssue xero_error is properly exposed and rendered in xero_status.html instead of displaying an empty dash.

- [x] G1: _find_existing_xero_document in coffee_xero.py queries CreditNotes by query parameter instead of path GUID
  CHECK: python -c "from coffee_xero import _find_existing_xero_document; import inspect; src = inspect.getsource(_find_existing_xero_document); assert 'CreditNotes?CreditNoteNumber=' in src or 'CreditNotes?where=' in src; print('G1 passed: CreditNotes query path fixed')"
  EXPECT: G1 passed: CreditNotes query path fixed

- [x] G2: models_coffee.py and templates/coffee/xero_status.html correctly expose and display xero_error
  CHECK: python -c "from models_coffee import RoastedCoffeeDispatch; d = RoastedCoffeeDispatch(xero_error='test error'); assert d.xero_error_message == 'test error'; print('G2 passed: xero_error_message property active')"
  EXPECT: G2 passed: xero_error_message property active

- [x] G3: Automated tests in tests/test_xero_credit_note_resilience.py verify CreditNote existing lookup and ItemCode fallback
  CHECK: pytest tests/test_xero_credit_note_resilience.py -v
  EXPECT: 2 passed

- [x] G4: Full regression suite passes cleanly
  CHECK: pytest tests/test_sync_cancel_job.py tests/test_duplicate_invoice_resolution.py tests/test_xero_nav_under_accountant_review.py
  EXPECT: 6 passed
