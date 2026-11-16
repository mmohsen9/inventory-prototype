import pytest
from models import Location, User, PurchaseTransaction, PurchaseLine, SyncQueue


@pytest.fixture
def auth_admin_client(app, test_data):
    with app.test_client() as client:
        with client.session_transaction() as sess:
            sess["user_id"] = test_data["admin"].id
            sess["role"] = "admin"
        yield client


def test_purchase_edit_safeguard_none_dates(auth_admin_client, test_data, db_session):
    """التحقق من عدم حدوث خطأ 500 عند عرض سند استلام تواريخه فارغة أو غير مهيأة."""
    p = PurchaseTransaction(
        location_id=test_data["br1"].id,
        supplier_name="Test Supplier",
        created_by=test_data["admin"].id,
        status="failed",
        invoice_number="INV-INIT-100",
        created_at=None,
        invoice_date=None,
        attachments=""
    )
    db_session.add(p)
    db_session.commit()

    resp = auth_admin_client.get(f"/purchases/{p.id}")
    assert resp.status_code == 200
    assert "سند استلام".encode("utf-8") in resp.data


def test_sync_resolve_duplicate_endpoint(auth_admin_client, test_data, db_session):
    """التحقق من عمل مسار حل تعارض رقم الفاتورة وتحديث السند وإعادة الجدولة."""
    p = PurchaseTransaction(
        location_id=test_data["br1"].id,
        supplier_name="Test Supplier",
        created_by=test_data["admin"].id,
        status="failed",
        invoice_number="INV-DUP-100"
    )
    db_session.add(p)
    db_session.flush()

    job = SyncQueue(
        source_type="purchase",
        source_id=p.id,
        idempotency_key="dup-test-key",
        status="failed"
    )
    db_session.add(job)
    db_session.commit()

    resp = auth_admin_client.post(
        f"/sync/resolve-duplicate/{job.id}",
        data={"new_invoice_number": "INV-RESOLVED-999"},
        follow_redirects=True
    )
    assert resp.status_code == 200

    updated_p = db_session.get(PurchaseTransaction, p.id)
    assert updated_p.invoice_number == "INV-RESOLVED-999"

    updated_job = db_session.get(SyncQueue, job.id)
    # مفتاح التحقق يجب أن يتغير برمز جديد
    assert updated_job.idempotency_key != "dup-test-key"
