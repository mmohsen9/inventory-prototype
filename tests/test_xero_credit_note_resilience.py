import pytest
from unittest.mock import patch, MagicMock
from models import Location, Item, User, db
from models_coffee import CoffeeItem, RoastedCoffeeDispatch, RoastedCoffeeDispatchLine, CoffeeSetting
from coffee_xero import _find_existing_xero_document, post_roasted_dispatch_to_xero
from datetime import date


def test_find_existing_credit_note_by_query_param(db_session):
    """التحقق من أن البحث عن إشعار دائن موجود في زيرو يستعلم عبر query parameter وليس عبر مسار GUID."""
    with patch("coffee_xero.requests.get") as mock_get, \
         patch("coffee_xero._xero_headers", return_value={"Authorization": "Bearer test"}):
        
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "CreditNotes": [{
                "CreditNoteID": "cn-guid-12345",
                "CreditNoteNumber": "MP-00015",
                "Total": 1190.50
            }]
        }
        mock_get.return_value = mock_resp

        doc = _find_existing_xero_document("CreditNotes", "MP-00015", expected_total=1190.50)
        assert doc is not None
        assert doc["CreditNoteID"] == "cn-guid-12345"

        # تأكيد أن الرابط احتوى على CreditNoteNumber كمعامل استعلام
        call_url = mock_get.call_args[0][0]
        assert "CreditNotes?CreditNoteNumber=MP-00015" in call_url or "where=" in call_url


def test_item_code_validation_fallback(app, db_session):
    """التحقق من إعادة المحاولة التلقائية بدون كود الصنف ItemCode عند رفض زيرو له لعدم وجوده في دليل الأصناف."""
    rw = Location(name_ar="مستودع المحمص للاختبار", name_en="Roasted Warehouse Test", type="warehouse")
    br = Location(name_ar="فرع رفاء للاختبار", name_en="Rafa Branch Test", type="branch", foodics_id="br-rafa-1")
    db_session.add_all([rw, br])
    db_session.flush()

    it = Item(sku="RB-RAFA-TEST", name_ar="بن رفاء محمص", storage_unit="كجم", cost=45.0)
    db_session.add(it)
    db_session.flush()

    ci = CoffeeItem(item_id=it.id, xero_item_code="RB-RAFA-UNMAPPED", prefix="RB", base_code="RAFA", coffee_type="roasted")
    db_session.add(ci)
    db_session.flush()

    u = User(name="مشرف فحص زيرو", role="admin")
    u.set_password("pass1234")
    db_session.add(u)
    db_session.flush()

    disp = RoastedCoffeeDispatch(
        doc_number="MP-00014",
        dispatch_date=date.today(),
        source_warehouse_id=rw.id,
        branch_location_id=br.id,
        dispatch_type="branch",
        status="approved",
        qty_kg=24.25,
        unit_cost=45.0,
        total_cost=1091.25,
        coffee_item_id=ci.id,
        created_by=u.id
    )
    db_session.add(disp)
    db_session.commit()

    with patch("coffee_xero._get_setting") as mock_setting, \
         patch("coffee_xero._xero_headers", return_value={"Authorization": "Bearer test"}), \
         patch("coffee_xero.requests.post") as mock_post, \
         patch("coffee_xero._apply_cash_refund") as mock_refund, \
         patch("coffee_xero.attach_voucher_to_xero", return_value={"success": True}):

        mock_setting.side_effect = lambda k, d=None: "dummy_token" if "token" in k else d

        # أول استدعاء يرجع خطأ كود الصنف، والاستدعاء الثاني ينجح بدون ItemCode
        resp_fail = MagicMock()
        resp_fail.status_code = 400
        resp_fail.text = '{"Elements":[{"ValidationErrors":[{"Message":"The Item code RB-RAFA-UNMAPPED is not valid"}]}]}'
        resp_fail.json.return_value = {"Elements": [{"ValidationErrors": [{"Message": "The Item code RB-RAFA-UNMAPPED is not valid"}]}]}

        resp_ok = MagicMock()
        resp_ok.status_code = 200
        resp_ok.json.return_value = {
            "CreditNotes": [{
                "CreditNoteID": "cn-rafa-guid-999",
                "CreditNoteNumber": "MP-00014"
            }]
        }

        mock_post.side_effect = [resp_fail, resp_ok]

        res = post_roasted_dispatch_to_xero(disp.id)
        assert res.get("success") is True
        assert res.get("credit_note_id") == "cn-rafa-guid-999"

        disp_after = RoastedCoffeeDispatch.query.get(disp.id)
        assert disp_after.xero_status == "posted"
        assert disp_after.xero_invoice_id == "cn-rafa-guid-999"
