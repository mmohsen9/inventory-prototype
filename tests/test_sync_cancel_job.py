import pytest
from models import PurchaseTransaction, PurchaseLine, SyncQueue, SyncLog, StockMovement, StockBalance, Location, Item


@pytest.fixture
def auth_accountant_client(app, test_data):
    with app.test_client() as client:
        with client.session_transaction() as sess:
            sess["user_id"] = test_data["accountant"].id
            sess["role"] = "accountant"
        yield client


def test_sync_cancel_removes_job_only(auth_accountant_client, test_data, db_session):
    """التحقق من إلغاء المهمة من طابور فوديكس فقط مع إبقاء السند محلياً."""
    purchase = PurchaseTransaction(
        location_id=test_data["br1"].id,
        supplier_name="Test Supplier",
        created_by=test_data["accountant"].id,
        status="approved",
        invoice_number="INV-CANCEL-1",
    )
    db_session.add(purchase)
    db_session.flush()

    job = SyncQueue(source_type="purchase", source_id=purchase.id, status="queued")
    db_session.add(job)
    db_session.commit()

    job_id = job.id
    p_id = purchase.id

    resp = auth_accountant_client.post(f"/sync/cancel/{job_id}", data={"delete_voucher": "0"}, follow_redirects=True)
    assert resp.status_code == 200

    # المهمة حُذفت من الطابور
    assert SyncQueue.query.get(job_id) is None
    # السند ما زال موجوداً
    p_after = PurchaseTransaction.query.get(p_id)
    assert p_after is not None
    assert p_after.status == "draft"


def test_sync_cancel_with_voucher_deletion(auth_accountant_client, test_data, db_session):
    """التحقق من إلغاء مهمة الطابور وحذف السند المكرر وعكس حركته المخزنية بالكامل."""
    item = Item(sku="ITEM-TEST-CANCEL", name_ar="صنف تجربة", storage_unit="حبة", cost=10.0)
    db_session.add(item)
    db_session.flush()

    # رصيد أولي 15
    bal = StockBalance(item_id=item.id, location_id=test_data["br1"].id, quantity=15.0)
    db_session.add(bal)
    db_session.flush()

    purchase = PurchaseTransaction(
        location_id=test_data["br1"].id,
        supplier_name="Test Supplier 2",
        created_by=test_data["accountant"].id,
        status="approved",
        invoice_number="INV-CANCEL-2",
    )
    db_session.add(purchase)
    db_session.flush()

    pline = PurchaseLine(purchase_id=purchase.id, item_id=item.id, quantity=5.0, unit_cost=10.0)
    mov = StockMovement(
        item_id=item.id,
        location_id=test_data["br1"].id,
        movement_type="purchase",
        quantity_change=5.0,
        reference_type="purchase",
        reference_id=purchase.id
    )
    # زاد الرصيد إلى 20
    bal.quantity = 20.0
    job = SyncQueue(source_type="purchase", source_id=purchase.id, status="queued")
    db_session.add_all([pline, mov, job])
    db_session.commit()

    job_id = job.id
    p_id = purchase.id

    resp = auth_accountant_client.post(f"/sync/cancel/{job_id}", data={"delete_voucher": "1"}, follow_redirects=True)
    assert resp.status_code == 200

    # المهمة والسند وحركته المخزنية تم حذفها وعكس الرصيد إلى 15
    assert SyncQueue.query.get(job_id) is None
    assert PurchaseTransaction.query.get(p_id) is None
    assert StockMovement.query.filter_by(reference_type="purchase", reference_id=p_id).first() is None
    bal_after = StockBalance.query.filter_by(item_id=item.id, location_id=test_data["br1"].id).first()
    assert bal_after.quantity == 15.0
