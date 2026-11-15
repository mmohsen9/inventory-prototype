# tests/test_foodics_full_sync.py
import pytest
from unittest.mock import patch, MagicMock
from models import (
    db, Location, Item, Supplier, PurchaseTransaction, PurchaseLine,
    StockBalance, StockMovement, Notification, SystemSetting
)
import sync_queue as sq

def test_sync_purchases_from_foodics_skips_local_purchases(test_data):
    """التحقق من أن مزامنة المشتريات تتخطى الفواتير التي بدأت من تطبيقنا (PUR-...) لمنع التكرار."""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "data": [
            {
                "id": "fd-po-local-01",
                "reference": "PUR-202611-001",  # Local receipt prefix
                "status": 3,
                "business_date": "2026-11-09",
                "items": []
            }
        ],
        "links": {}
    }

    with patch("requests.get", return_value=mock_response):
        ok, msg = sq.sync_purchases_from_foodics("fake-token", "https://api.foodics.com/v5")
        assert ok is True
        assert "تم تخطي 1 فاتورة" in msg or "مزامنة 0" in msg

def test_sync_purchases_from_foodics_imports_direct_foodics_purchase(test_data):
    """التحقق من استيراد فواتير المشتريات المدخلة بفوديكس مباشرة وتحديث المخزون المحلي."""
    # Ensure a mapped location and item exist
    loc = Location.query.first()
    loc.foodics_id = "fd-branch-99"
    
    item = Item.query.first()
    item.foodics_id = "fd-item-88"
    
    # Initial balance
    sb = StockBalance.query.filter_by(item_id=item.id, location_id=loc.id).first()
    initial_qty = sb.quantity if sb else 0.0

    db.session.commit()

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "data": [
            {
                "id": "fd-po-direct-100",
                "reference": "FD-INV-777",  # Direct Foodics invoice
                "status": 3,  # Closed
                "business_date": "2026-11-09",
                "branch": {"id": "fd-branch-99"},
                "supplier": {"id": "fd-sup-55", "name": "شركة المورد التجريبي"},
                "subtotal": 500.0,
                "tax": 75.0,
                "total": 575.0,
                "items": [
                    {
                        "id": "fd-item-88",
                        "sku": item.sku,
                        "quantity": 10.0,
                        "unit_price": 50.0,
                        "unit": "كجم"
                    }
                ]
            }
        ],
        "links": {}
    }

    with patch("requests.get", return_value=mock_response):
        ok, msg = sq.sync_purchases_from_foodics("fake-token", "https://api.foodics.com/v5")
        assert ok is True
        assert "تم استيراد 1 فاتورة مشتريات" in msg

    # Verify local PurchaseTransaction created
    tx = PurchaseTransaction.query.filter_by(foodics_reference="fd-po-direct-100").first()
    assert tx is not None
    assert tx.status == "approved"
    assert tx.location_id == loc.id
    assert tx.total_amount == 500.0

    # Verify stock balance updated
    sb_after = StockBalance.query.filter_by(item_id=item.id, location_id=loc.id).first()
    assert sb_after.quantity == pytest.approx(initial_qty + 10.0)

    # Verify stock movement recorded
    mov = StockMovement.query.filter_by(item_id=item.id, location_id=loc.id, movement_type="purchase_in").order_by(StockMovement.id.desc()).first()
    assert mov is not None
    assert mov.quantity_change == pytest.approx(10.0)

    # Verify in-app notification sent
    notif = Notification.query.filter(Notification.message.like("%FD-INV-777%")).first()
    assert notif is not None

def test_sync_all_from_foodics(test_data):
    """التحقق من تشغيل المزامنة الشاملة بنقرة واحدة لتنفيذ كافة الخطوات بالتتابع."""
    with patch("sync_queue.sync_branches_from_foodics", return_value=(True, "تم تحديث 5 فروع")):
        with patch("sync_queue.sync_inventory_items_from_foodics", return_value=(True, "تم تحديث 10 أصناف")):
            with patch("sync_queue.sync_suppliers_from_foodics", return_value=(True, "تم تحديث 3 موردين")):
                with patch("sync_queue.sync_purchases_from_foodics", return_value=(True, "تم استيراد 2 فواتير مشتريات")):
                    with patch("sync_queue.sync_foodics_pos_consumption", return_value=(True, "تم تسجيل 8 حركات استهلاك")):
                        with patch("sync_queue.process_all_queued", return_value=[(1, True), (2, True)]):
                            ok, msg = sq.sync_all_from_foodics("fake-token", "https://api.foodics.com/v5")
                            assert ok is True
                            assert "📍 الفروع" in msg
                            assert "📦 الأصناف" in msg
                            assert "🛒 المشتريات" in msg
                            assert "⚡ استهلاك المبيعات" in msg
                            assert "🚀 تم ترحيل 2 حركة معلقة لفوديكس" in msg

def test_foodics_sync_entity_routes(client, test_data):
    """التحقق من استجابة مسار /foodics/sync/all ومسار /foodics/sync/purchases من الويب."""
    SystemSetting.set_val("foodics_token", "test-live-token")

    with client.session_transaction() as sess:
        sess["user_id"] = test_data["admin"].id
        sess["user_role"] = "admin"

    with patch("sync_queue.sync_all_from_foodics", return_value=(True, "المزامنة الشاملة اكتملت")):
        res = client.post("/foodics/sync/all", follow_redirects=True)
        assert res.status_code == 200
        assert "المزامنة الشاملة اكتملت" in res.data.decode("utf-8")

    with patch("sync_queue.sync_purchases_from_foodics", return_value=(True, "تمت مزامنة المشتريات")):
        res = client.post("/foodics/sync/purchases", follow_redirects=True)
        assert res.status_code == 200
        assert "تمت مزامنة المشتريات" in res.data.decode("utf-8")
