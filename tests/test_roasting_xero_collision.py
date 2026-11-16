import pytest
from datetime import date
from models import db, User, Location
from models_coffee import RoastingOrder, RoastingOrderLine, CoffeeItem
from unittest.mock import patch, MagicMock

def test_roasting_change_number(client, db_session, test_data):
    # Setup test locations and coffee items
    gw = Location(name_ar="مستودع البن الأخضر", name_en="Green Warehouse", type="warehouse")
    rw = Location(name_ar="مستودع البن المحمص", name_en="Roasted Warehouse", type="warehouse")
    db_session.add_all([gw, rw])
    db_session.flush()

    order = RoastingOrder(
        order_number="OP-00001",
        order_date=date.today(),
        green_warehouse_id=gw.id,
        roasted_warehouse_id=rw.id,
        status="completed",
        xero_status="failed",
        created_by=1
    )
    db_session.add(order)
    db_session.commit()

    # Login as admin
    admin = User.query.filter_by(role="admin").first()
    with client.session_transaction() as sess:
        sess["user_id"] = admin.id
        sess["user_role"] = admin.role

    # Post change number
    resp = client.post(f"/coffee/roasting/{order.id}/change-number", data={
        "new_order_number": "OP-7273"
    }, follow_redirects=True)

    assert resp.status_code == 200
    refreshed = RoastingOrder.query.get(order.id)
    assert refreshed.order_number == "OP-7273"
    assert refreshed.xero_error_message is None

def test_post_credit_note_and_bill_existing_xero_recovery():
    from coffee_xero import post_credit_note, post_bill

    order = MagicMock()
    order.id = 1
    order.order_number = "OP-00001"
    order.order_date = date.today()
    order.total_green_cost = 1000.0
    order.total_roasted_cost = 1000.0
    line = MagicMock()
    line.line_status = "completed"
    line.green_total_cost = 1000.0
    line.roasted_total_cost = 1000.0
    line.green_qty_kg = 50.0
    line.green_unit_cost = 20.0
    line.green_item = MagicMock(name_ar="بن أخضر", xero_item_code="GB-01")
    line.roasted_qty_kg_actual = 42.0
    line.roasted_unit_cost = 23.8095
    line.roasted_item = MagicMock(name_ar="بن محمص", xero_item_code="RB-01")
    order.lines = [line]

    # Mock resilient error that fails, but _find_existing_xero_document finds it
    with patch("coffee_xero._post_credit_note_resilient") as mock_cn_post, \
         patch("coffee_xero._find_existing_xero_document") as mock_find, \
         patch("coffee_xero._apply_cash_refund") as mock_refund:

        mock_resp = MagicMock()
        mock_resp.status_code = 400
        mock_resp.text = "Credit Note not of valid status for modification"
        mock_resp.json.return_value = {"Elements": [{"ValidationErrors": [{"Message": "Credit Note not of valid status for modification"}]}]}
        mock_cn_post.return_value = mock_resp

        # Found existing credit note
        mock_find.return_value = {"CreditNoteID": "cn-uuid-123", "RemainingCredit": 0.0}

        success, cn_id, err = post_credit_note(order)
        assert success is True
        assert cn_id == "cn-uuid-123"
        assert err is None
        mock_find.assert_called_once_with("CreditNotes", "OP-00001", expected_total=1000.0)

    # Mock post_bill
    with patch("requests.post") as mock_req, \
         patch("coffee_xero._xero_headers", return_value={}), \
         patch("coffee_xero._find_existing_xero_document") as mock_find, \
         patch("coffee_xero._apply_payment") as mock_pay:

        mock_resp = MagicMock()
        mock_resp.status_code = 400
        mock_resp.text = "Invoice not of valid status for modification"
        mock_resp.json.return_value = {"Elements": [{"ValidationErrors": [{"Message": "Invoice not of valid status for modification"}]}]}
        mock_req.return_value = mock_resp

        # Found existing bill
        mock_find.return_value = {"InvoiceID": "inv-uuid-456", "AmountDue": 0.0}

        success, bill_id, err = post_bill(order)
        assert success is True
        assert bill_id == "inv-uuid-456"
        assert err is None
        mock_find.assert_called_once_with("Invoices", "OP-00001", expected_total=1000.0)


def test_serial_numbers_above_1000(db_session):
    from models_coffee import (
        next_roasting_order_number, GreenCoffeeIssue,
        RoastedCoffeeDispatch, CoffeeSetting, RoastingOrder,
        ensure_serial_numbers_above_1000
    )
    from models import CountSession

    # 1. Test next_roasting_order_number starts at 1001
    roast_num = next_roasting_order_number()
    assert roast_num == "OP-1001"
    assert next_roasting_order_number(preview_only=True) == "OP-1002"

    # 2. Test GreenCoffeeIssue starts at 1001
    green_num = GreenCoffeeIssue.generate_next_doc_number()
    assert green_num == "GB-1001"

    # 3. Test RoastedCoffeeDispatch prefixes start at 1001
    mp_num = RoastedCoffeeDispatch.generate_next_doc_number("branch")
    assert mp_num == "MP-1001"

    sl_num = RoastedCoffeeDispatch.generate_next_doc_number("salla")
    assert sl_num == "SL-1001"

    rb_num = RoastedCoffeeDispatch.generate_next_doc_number("sale")
    assert rb_num == "RB-1001"

    # 4. Test CountSession starts at 1001
    cs_num = CountSession.generate_next_session_number()
    assert cs_num == "Nov-1001"

    # 5. Test auto-migration of stuck OP-00001
    from models import Location
    gw = Location(name_ar="مستودع الأخضر", name_en="Green WH", type="warehouse")
    rw = Location(name_ar="مستودع المحمص", name_en="Roasted WH", type="warehouse")
    db_session.add_all([gw, rw])
    db_session.flush()

    stuck = RoastingOrder(
        order_number="OP-00001",
        order_date=date.today(),
        green_warehouse_id=gw.id,
        roasted_warehouse_id=rw.id,
        xero_status="failed",
        status="completed"
    )
    db_session.add(stuck)
    db_session.commit()

    ensure_serial_numbers_above_1000()
    refreshed = RoastingOrder.query.get(stuck.id)
    assert refreshed.order_number == "OP-1001"

